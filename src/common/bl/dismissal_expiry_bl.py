"""
Business logic for handling dismissal expiry.

This module provides functionality to automatically expire alert dismissals
when their dismissed_until timestamp has passed.

Dismiss state lives on typed LastAlert columns (status, dismiss_mode,
dismissed_until) instead of the alertenrichment JSONB column.
"""

import datetime
import logging
import os
from typing import List, Optional

import requests
from sqlmodel import Session, select

from src.common.core.db import get_session_sync
from src.common.core.elastic import ElasticClient
from src.common.models.action_type import ActionType
from src.common.models.alert import AlertDto
from src.common.models.db.alert import Alert, AlertAudit, LastAlert


class DismissalExpiryBl:
    @staticmethod
    def get_alerts_with_expired_dismissals(session: Session) -> List[LastAlert]:
        """
        Get all LastAlert records whose dismiss_until has expired.

        Returns rows where:
        1. dismiss_mode = 'dismiss_until'
        2. dismissed_until is not null and in the past

        Args:
            session: Database session

        Returns:
            List of LastAlert objects with expired dismissals
        """
        logger = logging.getLogger(__name__)
        now = datetime.datetime.now(tz=datetime.timezone.utc)

        logger.info("Searching for last alerts with expired dismissals")

        query = session.exec(
            select(LastAlert).where(
                LastAlert.dismiss_mode == "dismiss_until",
                LastAlert.dismissed_until.isnot(None),
                LastAlert.dismissed_until < now,
            )
        )

        expired = query.all()

        logger.info(f"Found {len(expired)} last alerts with expired dismissals")
        return expired

    @staticmethod
    def check_dismissal_expiry(
        logger: logging.Logger, session: Optional[Session] = None
    ):
        """
        Check for alerts with expired dismissed_until and restore them.

        This function:
        1. Finds LastAlert records with expired dismissed_until timestamps
        2. Resets status=NULL, dismiss_mode=NULL, dismissed_until=NULL
        3. Updates Elasticsearch indexes
        4. Notifies UI of changes
        5. Adds audit trail

        Args:
            logger: Logger instance for detailed logging
            session: Optional database session (creates new if None)
        """
        logger.info("Starting dismissal expiry check")

        owns_session = session is None
        if owns_session:
            session = get_session_sync()

        try:
            expired_last_alerts = DismissalExpiryBl.get_alerts_with_expired_dismissals(
                session
            )

            if not expired_last_alerts:
                logger.info("No last alerts with expired dismissals found")
                return

            logger.info(
                f"Processing {len(expired_last_alerts)} expired dismissal last alerts"
            )

            for last_alert in expired_last_alerts:
                logger.info(
                    f"Processing expired dismissal for fingerprint {last_alert.fingerprint}",
                    extra={
                        "tenant_id": last_alert.tenant_id,
                        "fingerprint": last_alert.fingerprint,
                        "dismissed_until": str(last_alert.dismissed_until),
                    },
                )

                # Store original values for audit
                original_dismissed_until = last_alert.dismissed_until

                # Reset dismiss state on the LastAlert row.
                last_alert.status = None
                last_alert.dismiss_mode = None
                last_alert.dismissed_until = None
                session.add(last_alert)

                # Add audit trail
                try:
                    audit = AlertAudit(
                        tenant_id=last_alert.tenant_id,
                        fingerprint=last_alert.fingerprint,
                        user_id="system",
                        action=ActionType.DISMISSAL_EXPIRED.value,
                        description=(
                            f"Dismissal expired at {original_dismissed_until}, "
                            f"alert restored (status/dismiss_mode/dismissed_until cleared)"
                        ),
                    )
                    session.add(audit)
                    logger.info(
                        "Added audit trail for expired dismissal",
                        extra={
                            "tenant_id": last_alert.tenant_id,
                            "fingerprint": last_alert.fingerprint,
                        },
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to add audit trail for fingerprint {last_alert.fingerprint}: {e}",
                        extra={
                            "tenant_id": last_alert.tenant_id,
                            "fingerprint": last_alert.fingerprint,
                        },
                    )

                # Update Elasticsearch index
                try:
                    latest_alert = session.exec(
                        select(Alert)
                        .where(Alert.tenant_id == last_alert.tenant_id)
                        .where(Alert.fingerprint == last_alert.fingerprint)
                        .order_by(Alert.timestamp.desc())
                        .limit(1)
                    ).first()

                    if latest_alert:
                        # AlertDto built from provider data; dismiss state is now
                        # cleared, so the DTO reflects the original alert status.
                        alert_data = latest_alert.dict()
                        # Alert.id is a UUID; AlertDto.id (alias event_id) requires str.
                        alert_data["id"] = (
                            str(latest_alert.id) if latest_alert.id else None
                        )
                        alert_data["dismiss_until"] = None

                        alert_dto = AlertDto(**alert_data)

                        elastic_client = ElasticClient(last_alert.tenant_id)
                        elastic_client.index_alert(alert_dto)
                        logger.info(
                            f"Updated Elasticsearch index for fingerprint {last_alert.fingerprint}",
                            extra={
                                "tenant_id": last_alert.tenant_id,
                                "fingerprint": last_alert.fingerprint,
                            },
                        )
                    else:
                        logger.warning(
                            f"No alert found for fingerprint {last_alert.fingerprint}, skipping Elasticsearch update",
                            extra={
                                "tenant_id": last_alert.tenant_id,
                                "fingerprint": last_alert.fingerprint,
                            },
                        )

                except Exception as e:
                    logger.error(
                        f"Failed to update Elasticsearch for fingerprint {last_alert.fingerprint}: {e}",
                        extra={
                            "tenant_id": last_alert.tenant_id,
                            "fingerprint": last_alert.fingerprint,
                        },
                    )

            # Collect tenants before commit (attribute access after commit
            # would trigger a refresh query per row).
            tenants_to_notify = {la.tenant_id for la in expired_last_alerts}

            # Commit all changes
            session.commit()
            logger.info(
                f"Successfully processed {len(expired_last_alerts)} expired dismissal last alerts",
                extra={"processed_count": len(expired_last_alerts)},
            )

            # Notify the UI after commit, via the gateway's /sse/notify bridge.
            # The watcher runs outside the gateway process, so the local
            # in-memory SSE broadcaster cannot reach browser connections —
            # same pattern as the event handler's post-persist notify. An
            # empty poll-alerts payload is the UI's "refetch alerts" signal.
            api_url = os.environ.get("KEEP_API_URL", "http://localhost:8080")
            for tenant_id in tenants_to_notify:
                try:
                    response = requests.post(
                        f"{api_url}/sse/notify",
                        json={
                            "tenant_id": tenant_id,
                            "event": "poll-alerts",
                            "data": {},
                        },
                        timeout=5,
                    )
                    response.raise_for_status()
                    logger.info(
                        "Notified UI to refetch alerts after dismissal expiry",
                        extra={"tenant_id": tenant_id},
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to notify UI after dismissal expiry: {e}",
                        extra={"tenant_id": tenant_id},
                    )

        except Exception as e:
            logger.error(f"Error during dismissal expiry check: {e}", exc_info=True)
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
            logger.info("Dismissal expiry check completed")
