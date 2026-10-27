"""Alerts hidden by a "Hide From Feed" maintenance window.

1. The watcher step tells a tenant's open feeds to refetch when one of its
   windows that still tags alerts has just ended. It never clears the tag:
   visibility is decided at read time by the gateway.
2. The mirrored pipeline stores hidden alerts instead of dropping them, tags
   them with the window's id, and keeps them out of workflows. An alert already
   on screen is frozen instead: its row holds the latest occurrence back.
3. The watcher step catches frozen alerts up once their window is no longer
   active, however it stopped.
"""

import datetime
import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

from src.common.bl.maintenance_window_reveal_bl import MaintenanceWindowRevealBl
from src.common.consts import SSE_NOTIFY_HEADERS
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


@pytest.fixture(autouse=True)
def fresh_watcher_state(monkeypatch):
    monkeypatch.setattr(MaintenanceWindowRevealBl, "_last_run", None)


def _tick(session):
    with patch("src.common.bl.maintenance_window_reveal_bl.requests") as requests_mock:
        MaintenanceWindowRevealBl.notify_feeds_of_ended_windows(logger, session=session)
    return requests_mock


def _window_id_of(session, fingerprint):
    session.expire_all()
    return session.exec(
        select(LastAlert.maintenance_window_id).where(
            LastAlert.fingerprint == fingerprint
        )
    ).one()


def test_window_that_just_ended_notifies_and_keeps_the_tag(db_session, monkeypatch):
    monkeypatch.setenv("KEEP_API_URL", "http://gateway:8080")
    window = _window(db_session, end_time=_now() - datetime.timedelta(seconds=5))
    _hidden_alert(db_session, "fp-ended", window.id)

    requests_mock = _tick(db_session)

    requests_mock.post.assert_called_once_with(
        "http://gateway:8080/sse/notify",
        json={"tenant_id": SINGLE_TENANT_UUID, "event": "poll-alerts", "data": {}},
        headers=SSE_NOTIFY_HEADERS,
        timeout=5,
    )
    assert _window_id_of(db_session, "fp-ended") == window.id


def test_running_window_is_not_notified(db_session):
    window = _window(db_session)
    _hidden_alert(db_session, "fp-hidden", window.id)

    _tick(db_session).post.assert_not_called()


def test_window_that_ended_long_ago_is_not_notified(db_session):
    window = _window(db_session, end_time=_now() - datetime.timedelta(hours=2))
    _hidden_alert(db_session, "fp-old", window.id)

    _tick(db_session).post.assert_not_called()


def test_ended_window_without_tagged_alerts_is_not_notified(db_session):
    _window(db_session, end_time=_now() - datetime.timedelta(seconds=5))

    _tick(db_session).post.assert_not_called()


def test_each_ended_window_is_notified_once(db_session):
    window = _window(db_session, end_time=_now() - datetime.timedelta(seconds=5))
    _hidden_alert(db_session, "fp-once", window.id)

    _tick(db_session)
    _tick(db_session).post.assert_not_called()


def test_notify_closes_owned_session(db_session):
    tracking_session = MagicMock(wraps=db_session)
    with patch(
        "src.common.bl.maintenance_window_reveal_bl.get_session_sync",
        return_value=tracking_session,
    ), patch("src.common.bl.maintenance_window_reveal_bl.requests"):
        MaintenanceWindowRevealBl.notify_feeds_of_ended_windows(logger)
    tracking_session.close.assert_called_once()


# --------------------------------------------------------------------------- #
# Mirrored pipeline
# --------------------------------------------------------------------------- #


def _send(fingerprint, status, minutes, source=SOURCE, severity="high"):
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
            "severity": severity,
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


def _last(session, fingerprint):
    session.expire_all()
    return get_last_alert_by_fingerprint(SINGLE_TENANT_UUID, fingerprint, session=session)


def _latest(session, fingerprint):
    alerts = session.exec(select(Alert).where(Alert.fingerprint == fingerprint)).all()
    return max(alerts, key=lambda a: a.timestamp)


def test_pipeline_keeps_an_already_firing_alert_visible_but_frozen(db_session):
    _send("fp-pipeline-firing", AlertStatus.FIRING, 0)
    shown_alert_id = _last(db_session, "fp-pipeline-firing").alert_id
    window = _window(db_session)

    with patch(
        "src.common.event_management.process_event_task.WorkflowManager"
    ) as workflow_manager:
        _send("fp-pipeline-firing", AlertStatus.FIRING, 1, severity="critical")
        _send("fp-pipeline-firing", AlertStatus.FIRING, 2, severity="low")

    last_alert = _last(db_session, "fp-pipeline-firing")
    assert last_alert.maintenance_window_id is None
    assert last_alert.alert_id == shown_alert_id
    assert last_alert.maintenance_frozen_window_id == window.id
    assert last_alert.maintenance_pending_firing_count == 2
    assert (
        last_alert.maintenance_pending_alert_id
        == _latest(db_session, "fp-pipeline-firing").id
    )
    insert_events = workflow_manager.get_instance.return_value.insert_events
    assert all(call.args[1] == [] for call in insert_events.call_args_list)


# --------------------------------------------------------------------------- #
# Freeze catch-up
# --------------------------------------------------------------------------- #


def _frozen(session, fingerprint, held_back=2):
    """An alert on screen, then `held_back` occurrences frozen by a window."""
    _send(fingerprint, AlertStatus.FIRING, 0)
    window = _window(session)
    for minute in range(1, held_back + 1):
        _send(fingerprint, AlertStatus.FIRING, minute, severity=f"sev-{minute}")
    return window


def _catch_up(session):
    with patch("src.common.bl.maintenance_window_reveal_bl.requests") as requests_mock:
        MaintenanceWindowRevealBl.catch_up_released_alerts(logger, session=session)
    return requests_mock.post


def test_catch_up_after_the_window_is_disabled(db_session):
    window = _frozen(db_session, "fp-catch-up", held_back=3)
    before = _last(db_session, "fp-catch-up")
    firing_counter_before = before.firing_counter
    held_received = before.maintenance_pending_last_received

    window.enabled = False
    db_session.add(window)
    db_session.commit()
    post = _catch_up(db_session)

    last_alert = _last(db_session, "fp-catch-up")
    assert last_alert.alert_id == _latest(db_session, "fp-catch-up").id
    assert last_alert.firing_counter == firing_counter_before + 3
    assert last_alert.last_received == held_received
    assert last_alert.maintenance_pending_firing_count is None
    assert last_alert.maintenance_frozen_window_id is None
    assert last_alert.maintenance_pending_alert_id is None
    post.assert_called_once()
    assert post.call_args.kwargs["json"] == {
        "tenant_id": SINGLE_TENANT_UUID,
        "event": "poll-alerts",
        "data": {},
    }


def test_no_catch_up_while_the_window_is_active(db_session):
    _frozen(db_session, "fp-still-frozen")
    shown_alert_id = _last(db_session, "fp-still-frozen").alert_id

    post = _catch_up(db_session)

    last_alert = _last(db_session, "fp-still-frozen")
    assert last_alert.alert_id == shown_alert_id
    assert last_alert.maintenance_pending_firing_count == 2
    post.assert_not_called()


def test_catch_up_after_the_window_is_deleted(db_session):
    window = _frozen(db_session, "fp-deleted-window")
    db_session.delete(window)
    db_session.commit()

    _catch_up(db_session)

    last_alert = _last(db_session, "fp-deleted-window")
    assert last_alert.alert_id == _latest(db_session, "fp-deleted-window").id
    assert last_alert.maintenance_pending_firing_count is None


def test_catch_up_of_a_row_released_by_a_window_edit(db_session):
    """The gateway clears maintenance_frozen_window_id on alerts an edited
    window no longer matches; the window itself is still active."""
    _frozen(db_session, "fp-released")
    last_alert = _last(db_session, "fp-released")
    last_alert.maintenance_frozen_window_id = None
    db_session.add(last_alert)
    db_session.commit()

    _catch_up(db_session)

    last_alert = _last(db_session, "fp-released")
    assert last_alert.alert_id == _latest(db_session, "fp-released").id
    assert last_alert.maintenance_pending_firing_count is None


def test_catch_up_closes_owned_session(db_session):
    tracking_session = MagicMock(wraps=db_session)
    with patch(
        "src.common.bl.maintenance_window_reveal_bl.get_session_sync",
        return_value=tracking_session,
    ), patch("src.common.bl.maintenance_window_reveal_bl.requests"):
        MaintenanceWindowRevealBl.catch_up_released_alerts(logger)
    tracking_session.close.assert_called_once()


def test_pipeline_non_matching_occurrence_clears_the_window(db_session):
    _window(db_session)

    _send("fp-pipeline-resolve", AlertStatus.FIRING, 0)
    _send("fp-pipeline-resolve", AlertStatus.RESOLVED, 1)

    db_session.expire_all()
    last_alert = get_last_alert_by_fingerprint(
        SINGLE_TENANT_UUID, "fp-pipeline-resolve", session=db_session
    )
    assert last_alert.maintenance_window_id is None
