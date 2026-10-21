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
"""

import datetime
import logging
import os
from typing import Optional

import requests
from sqlalchemy import exists
from sqlmodel import Session, select

from src.common.consts import SSE_NOTIFY_HEADERS, WATCHER_LAPSED_TIME
from src.common.core.db import get_session_sync
from src.common.models.db.alert import LastAlert
from src.common.models.db.maintenance_window import MaintenanceWindowRule

# Extra lookback on the first run after start, so a window that ended while the
# previous process was going down is still notified.
FIRST_RUN_SLACK_SECONDS = 30


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

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
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

            # An empty poll-alerts payload is the UI's "refetch alerts" signal.
            api_url = os.environ.get("KEEP_API_URL", "http://localhost:8080")
            for tenant_id in tenant_ids:
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
                f"Error during maintenance window end check: {e}", exc_info=True
            )
            raise
        finally:
            if owns_session:
                session.close()
            logger.info("Maintenance window end check completed")
