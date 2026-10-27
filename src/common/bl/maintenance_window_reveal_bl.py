"""
Business logic for telling open feeds about alerts revealed by ended windows.

The event-handler stores alerts matched by a hide-mode maintenance window and
records the window's id on lastalert.maintenance_window_id. The gateway's feed
query decides visibility at read time (hidden only while the window is
running), so the alerts reappear on the next feed load with nothing written
here, and re-enabling or extending a window hides them again. This step only
makes open feeds update when a window ends on its own: it finds windows whose
end_time passed since the previous run and that still tag alerts, and tells
those tenants' UIs to refetch. Editing or deleting a window is notified by the
gateway's maintenance routes.

Alerts that were already on screen when a window matched them are frozen
instead (the row keeps its pre-window occurrence and holds the latest one
back). Catching those up is a write, done by catch_up_released_alerts once the
freezing window is no longer active, however it stopped: it ended, or was
disabled, deleted, rescheduled or switched to suppress mode. A window edit
releases the alerts it no longer matches through the gateway.
"""

import datetime
import logging
import os
from typing import Optional

import requests
from sqlalchemy import exists, or_
from sqlmodel import Session, select

from src.common.consts import SSE_NOTIFY_HEADERS, WATCHER_LAPSED_TIME
from src.common.core.db import (
    fold_held_back_occurrence,
    get_last_alert_by_fingerprint,
    get_session_sync,
)
from src.common.models.db.alert import LastAlert
from src.common.models.db.maintenance_window import MaintenanceWindowRule

# Extra lookback on the first run after start, so a window that ended while the
# previous process was going down is still notified.
FIRST_RUN_SLACK_SECONDS = 30

# Rows caught up per watcher run; the rest wait for the next run.
CATCH_UP_BATCH_SIZE = 500


def _utcnow() -> datetime.datetime:
    """Window times are naive UTC."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _freezing_window_active(now: datetime.datetime):
    """True while a row's freezing window still freezes it. Same definition of
    "active" as the gateway's hidden_by_maintenance_window_filter."""
    return exists().where(
        MaintenanceWindowRule.id == LastAlert.maintenance_frozen_window_id,
        MaintenanceWindowRule.tenant_id == LastAlert.tenant_id,
        MaintenanceWindowRule.enabled.is_(True),
        MaintenanceWindowRule.suppress.is_(False),
        MaintenanceWindowRule.start_time <= now,
        MaintenanceWindowRule.end_time > now,
    )


def _released(now: datetime.datetime):
    """The row holds something back and nothing freezes it any more."""
    return LastAlert.maintenance_pending_firing_count.is_not(None) & or_(
        LastAlert.maintenance_frozen_window_id.is_(None),
        ~_freezing_window_active(now),
    )


def _notify_refetch(logger: logging.Logger, tenant_id: str, reason: str) -> None:
    """An empty poll-alerts payload is the UI's "refetch alerts" signal."""
    api_url = os.environ.get("KEEP_API_URL", "http://localhost:8080")
    try:
        response = requests.post(
            f"{api_url}/sse/notify",
            json={"tenant_id": tenant_id, "event": "poll-alerts", "data": {}},
            headers=SSE_NOTIFY_HEADERS,
            timeout=5,
        )
        response.raise_for_status()
        logger.info(
            f"Notified UI to refetch alerts {reason}", extra={"tenant_id": tenant_id}
        )
    except Exception as e:
        logger.warning(
            f"Failed to notify UI {reason}: {e}", extra={"tenant_id": tenant_id}
        )


class MaintenanceWindowRevealBl:
    # End of the interval covered by the previous run in this process. Each run
    # covers (previous run, now]; a repeat notification is harmless.
    _last_run: Optional[datetime.datetime] = None

    @staticmethod
    def get_tenants_with_ended_windows(
        session: Session, since: datetime.datetime, until: datetime.datetime
    ) -> list[str]:
        """Tenants with a window that ended in (since, until] and still tags
        lastalert rows. Window times are naive UTC."""
        tags_alerts = exists().where(
            LastAlert.tenant_id == MaintenanceWindowRule.tenant_id,
            LastAlert.maintenance_window_id == MaintenanceWindowRule.id,
        )
        return session.exec(
            select(MaintenanceWindowRule.tenant_id)
            .where(
                MaintenanceWindowRule.end_time > since,
                MaintenanceWindowRule.end_time <= until,
                tags_alerts,
            )
            .distinct()
        ).all()

    @staticmethod
    def notify_feeds_of_ended_windows(
        logger: logging.Logger, session: Optional[Session] = None
    ):
        """Tell open feeds of tenants whose hide-mode windows just ended to
        refetch, via the gateway's /sse/notify bridge."""
        logger.info("Starting maintenance window end check")

        now = _utcnow()
        since = MaintenanceWindowRevealBl._last_run or now - datetime.timedelta(
            seconds=WATCHER_LAPSED_TIME + FIRST_RUN_SLACK_SECONDS
        )

        owns_session = session is None
        if owns_session:
            session = get_session_sync()

        try:
            tenant_ids = MaintenanceWindowRevealBl.get_tenants_with_ended_windows(
                session, since, now
            )
            MaintenanceWindowRevealBl._last_run = now

            if not tenant_ids:
                logger.info("No maintenance windows with hidden alerts ended")
                return

            for tenant_id in tenant_ids:
                _notify_refetch(logger, tenant_id, "after maintenance window ended")

        except Exception as e:
            logger.error(
                f"Error during maintenance window end check: {e}", exc_info=True
            )
            raise
        finally:
            if owns_session:
                session.close()
            logger.info("Maintenance window end check completed")

    @staticmethod
    def get_released_frozen_alerts(
        session: Session, now: datetime.datetime, limit: int = CATCH_UP_BATCH_SIZE
    ) -> list[tuple[str, str]]:
        """(tenant_id, fingerprint) of rows that hold back occurrences no
        active window freezes any more."""
        return session.exec(
            select(LastAlert.tenant_id, LastAlert.fingerprint)
            .where(_released(now))
            .limit(limit)
        ).all()

    @staticmethod
    def catch_up_released_alerts(
        logger: logging.Logger, session: Optional[Session] = None
    ):
        """Point frozen alerts whose window stopped being active at the latest
        occurrence it held back, and tell those tenants' feeds to refetch.

        Each row is re-checked under its lock: a new occurrence may have caught
        it up already, or a window may have been re-enabled or extended since
        the scan."""
        logger.info("Starting maintenance window freeze catch-up")

        owns_session = session is None
        if owns_session:
            session = get_session_sync()

        caught_up_tenants: set[str] = set()
        try:
            candidates = MaintenanceWindowRevealBl.get_released_frozen_alerts(
                session, _utcnow()
            )
            if not candidates:
                logger.info("No frozen alerts to catch up")
                return

            for tenant_id, fingerprint in candidates:
                extra = {"tenant_id": tenant_id, "fingerprint": fingerprint}
                try:
                    last_alert = get_last_alert_by_fingerprint(
                        tenant_id, fingerprint, session, for_update=True
                    )
                    still_released = session.exec(
                        select(LastAlert.fingerprint).where(
                            LastAlert.tenant_id == tenant_id,
                            LastAlert.fingerprint == fingerprint,
                            _released(_utcnow()),
                        )
                    ).first()
                    if last_alert is None or still_released is None:
                        session.rollback()
                        continue
                    if fold_held_back_occurrence(last_alert, session):
                        session.add(last_alert)
                        session.commit()
                        caught_up_tenants.add(tenant_id)
                    else:
                        session.rollback()
                except Exception:
                    session.rollback()
                    logger.exception(
                        "Failed to catch up alert frozen by a maintenance window",
                        extra=extra,
                    )

            for tenant_id in caught_up_tenants:
                _notify_refetch(
                    logger, tenant_id, "after catching up frozen alerts"
                )
        finally:
            if owns_session:
                session.close()
            logger.info(
                "Maintenance window freeze catch-up completed",
                extra={"tenants": len(caught_up_tenants)},
            )
