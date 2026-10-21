"""Alerts hidden by a "Hide From Feed" maintenance window.

1. The watcher step clears lastalert.maintenance_window_id once the window is
   no longer active and tells the tenant's open feeds to refetch.
2. The mirrored pipeline stores hidden alerts instead of dropping them, tags
   them with the window's id, and keeps them out of workflows.
"""

import datetime
import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

from src.common.bl.maintenance_window_reveal_bl import MaintenanceWindowRevealBl
from src.common.core.db import get_last_alert_by_fingerprint
from src.common.core.dependencies import SINGLE_TENANT_UUID
from src.common.models.alert import AlertStatus
from src.common.models.db.alert import Alert, LastAlert
from src.common.models.db.maintenance_window import MaintenanceWindowRule

logger = logging.getLogger(__name__)

SOURCE = "test-source"


def _now():
    return datetime.datetime.utcnow()


def _window(session, **overrides):
    values = dict(
        tenant_id=SINGLE_TENANT_UUID,
        name="DB upgrade",
        description="",
        start_time=_now() - datetime.timedelta(hours=1),
        end_time=_now() + datetime.timedelta(hours=1),
        created_by="test_user",
        cel_query=f'source == "{SOURCE}"',
        enabled=True,
        suppress=False,
        ignore_statuses=[AlertStatus.RESOLVED.value, AlertStatus.ACKNOWLEDGED.value],
    )
    values.update(overrides)
    window = MaintenanceWindowRule(**values)
    session.add(window)
    session.commit()
    session.refresh(window)
    return window


def _hidden_alert(session, fingerprint, window_id):
    alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="test",
        provider_id="test",
        name=fingerprint,
        status="firing",
        severity="critical",
        source=[SOURCE],
        fingerprint=fingerprint,
        alert_hash="hash-1",
    )
    session.add(alert)
    session.commit()
    session.refresh(alert)
    last_alert = LastAlert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint=fingerprint,
        alert_id=alert.id,
        timestamp=_now(),
        first_timestamp=_now(),
        maintenance_window_id=window_id,
    )
    session.add(last_alert)
    session.commit()
    return last_alert


def _reveal(session):
    with patch(
        "src.common.bl.maintenance_window_reveal_bl.requests"
    ) as requests_mock:
        MaintenanceWindowRevealBl.reveal_alerts_of_inactive_windows(
            logger, session=session
        )
    return requests_mock


def _window_id_of(session, fingerprint):
    session.expire_all()
    return session.exec(
        select(LastAlert.maintenance_window_id).where(
            LastAlert.fingerprint == fingerprint
        )
    ).one()


@pytest.mark.parametrize(
    "overrides",
    [
        {"end_time": _now() - datetime.timedelta(minutes=1)},
        {"enabled": False},
        {"suppress": True},
    ],
    ids=["ended", "disabled", "switched-to-suppress"],
)
def test_inactive_window_is_cleared_and_notified(db_session, monkeypatch, overrides):
    monkeypatch.setenv("KEEP_API_URL", "http://gateway:8080")
    window = _window(db_session, **overrides)
    _hidden_alert(db_session, "fp-revealed", window.id)

    requests_mock = _reveal(db_session)

    assert _window_id_of(db_session, "fp-revealed") is None
    requests_mock.post.assert_called_once_with(
        "http://gateway:8080/sse/notify",
        json={"tenant_id": SINGLE_TENANT_UUID, "event": "poll-alerts", "data": {}},
        headers={},
        timeout=5,
    )


def test_deleted_window_is_cleared_and_notified(db_session):
    window = _window(db_session)
    _hidden_alert(db_session, "fp-deleted", window.id)
    db_session.delete(window)
    db_session.commit()

    requests_mock = _reveal(db_session)

    assert _window_id_of(db_session, "fp-deleted") is None
    requests_mock.post.assert_called_once()


def test_active_window_is_left_alone(db_session):
    window = _window(db_session)
    _hidden_alert(db_session, "fp-hidden", window.id)

    requests_mock = _reveal(db_session)

    assert _window_id_of(db_session, "fp-hidden") == window.id
    requests_mock.post.assert_not_called()


def test_second_tick_does_not_notify_again(db_session):
    window = _window(db_session, end_time=_now() - datetime.timedelta(minutes=1))
    _hidden_alert(db_session, "fp-once", window.id)

    _reveal(db_session)
    requests_mock = _reveal(db_session)

    requests_mock.post.assert_not_called()


def test_reveal_closes_owned_session(db_session):
    tracking_session = MagicMock(wraps=db_session)
    with patch(
        "src.common.bl.maintenance_window_reveal_bl.get_session_sync",
        return_value=tracking_session,
    ), patch("src.common.bl.maintenance_window_reveal_bl.requests"):
        MaintenanceWindowRevealBl.reveal_alerts_of_inactive_windows(logger)
    tracking_session.close.assert_called_once()


# --------------------------------------------------------------------------- #
# Mirrored pipeline
# --------------------------------------------------------------------------- #


def _send(fingerprint, status, minutes, source=SOURCE):
    from src.common.event_management.process_event_task import process_event

    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=30 - minutes
    )
    return process_event(
        ctx={"job_try": 1},
        trace_id="test",
        tenant_id=SINGLE_TENANT_UUID,
        provider_id="test",
        provider_type=source,
        fingerprint=fingerprint,
        api_key_name="test",
        event={
            "name": f"test-{fingerprint}",
            "last_received": ts.isoformat(),
            "status": status.value,
            "severity": "high",
            "source": [source],
            "fingerprint": fingerprint,
        },
        notify_client=False,
        timestamp_forced=ts,
    )


def test_pipeline_stores_hidden_alert_and_skips_workflows(db_session):
    window = _window(db_session)

    with patch(
        "src.common.event_management.process_event_task.WorkflowManager"
    ) as workflow_manager:
        _send("fp-pipeline", AlertStatus.FIRING, 0)

    alerts = db_session.exec(
        select(Alert).where(Alert.fingerprint == "fp-pipeline")
    ).all()
    assert len(alerts) == 1
    db_session.expire_all()
    last_alert = get_last_alert_by_fingerprint(
        SINGLE_TENANT_UUID, "fp-pipeline", session=db_session
    )
    assert last_alert.maintenance_window_id == window.id
    insert_events = workflow_manager.get_instance.return_value.insert_events
    assert all(call.args[1] == [] for call in insert_events.call_args_list)


def test_pipeline_non_matching_occurrence_clears_the_window(db_session):
    _window(db_session)

    _send("fp-pipeline-resolve", AlertStatus.FIRING, 0)
    _send("fp-pipeline-resolve", AlertStatus.RESOLVED, 1)

    db_session.expire_all()
    last_alert = get_last_alert_by_fingerprint(
        SINGLE_TENANT_UUID, "fp-pipeline-resolve", session=db_session
    )
    assert last_alert.maintenance_window_id is None
