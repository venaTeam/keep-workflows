"""
Business logic for revealing alerts hidden by a "Hide From Feed" window.

The event-handler stores alerts matched by a hide-mode maintenance window and
records the window's id on lastalert.maintenance_window_id. The gateway's feed
query hides such rows only while the window is active, so they reappear on the
next feed load without anything here. This step only makes open feeds update:
once a window is no longer active it clears the id on its rows and tells the
UI to refetch, the same way DismissalExpiryBl does for expired dismissals.
"""

import datetime
import logging
import os
from typing import Optional

import requests
from sqlalchemy import exists, update
from sqlmodel import Session, select

from src.common.consts import SSE_NOTIFY_HEADERS
from src.common.core.db import get_session_sync
from src.common.models.db.alert import LastAlert
from src.common.models.db.maintenance_window import MaintenanceWindowRule


class MaintenanceWindowRevealBl:
    @staticmethod
    def _window_no_longer_active():
        """True for a lastalert row whose hiding window has been deleted,
        disabled, switched to suppress mode, or has ended. Window times are
        naive UTC."""
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        window_active = exists().where(
            MaintenanceWindowRule.id == LastAlert.maintenance_window_id,
            MaintenanceWindowRule.tenant_id == LastAlert.tenant_id,
            MaintenanceWindowRule.enabled.is_(True),
            MaintenanceWindowRule.suppress.is_(False),
            MaintenanceWindowRule.end_time > now,
        )
        return LastAlert.maintenance_window_id.isnot(None) & ~window_active

    @staticmethod
    def reveal_alerts_of_inactive_windows(
        logger: logging.Logger, session: Optional[Session] = None
    ):
        """
        Clear maintenance_window_id on rows whose window is no longer active and
        notify each affected tenant's open feeds to refetch.

        The update re-checks the condition, so a row a newer occurrence tagged
        with a still-active window in the meantime is left alone. Clearing the
        id is what stops a tenant being notified again on every tick.
        """
        logger.info("Starting maintenance window reveal check")

        owns_session = session is None
        if owns_session:
            session = get_session_sync()

        try:
            tenant_ids = session.exec(
                select(LastAlert.tenant_id)
                .where(MaintenanceWindowRevealBl._window_no_longer_active())
                .distinct()
            ).all()

            if not tenant_ids:
                logger.info("No alerts to reveal from ended maintenance windows")
                return

            tenants_to_notify = []
            for tenant_id in tenant_ids:
                result = session.execute(
                    update(LastAlert)
                    .where(
                        LastAlert.tenant_id == tenant_id,
                        MaintenanceWindowRevealBl._window_no_longer_active(),
                    )
                    .values(maintenance_window_id=None)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount:
                    tenants_to_notify.append(tenant_id)
                    logger.info(
                        "Revealed alerts hidden by ended maintenance windows",
                        extra={"tenant_id": tenant_id, "count": result.rowcount},
                    )

            session.commit()

            # Notify the UI after commit, via the gateway's /sse/notify bridge.
            # An empty poll-alerts payload is the UI's "refetch alerts" signal.
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
                        headers=SSE_NOTIFY_HEADERS,
                        timeout=5,
                    )
                    response.raise_for_status()
                    logger.info(
                        "Notified UI to refetch alerts after maintenance window ended",
                        extra={"tenant_id": tenant_id},
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to notify UI after maintenance window ended: {e}",
                        extra={"tenant_id": tenant_id},
                    )

        except Exception as e:
            logger.error(
                f"Error during maintenance window reveal check: {e}", exc_info=True
            )
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
            logger.info("Maintenance window reveal check completed")
