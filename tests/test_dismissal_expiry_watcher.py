"""Tests for the dismissal-expiry watcher.

Covers three regressions:
1. The deployed entrypoint (src.main:app) must start the watcher when WATCHER=true.
2. check_dismissal_expiry must close the session it creates (a leaked pooled
   connection per tick exhausts the pool and kills the watcher after ~15 min).
3. The Elastic reindex on expiry must build a valid AlertDto (Alert.id is a
   UUID; AlertDto.id expects str).
"""

import asyncio
import datetime
import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

from src.common.bl.dismissal_expiry_bl import DismissalExpiryBl
from src.common.core.dependencies import SINGLE_TENANT_UUID
from src.common.models.db.alert import Alert, AlertAudit, LastAlert

logger = logging.getLogger(__name__)

FINGERPRINT = "fp-dismissal-expiry"


def _insert_dismissed_alert(session, dismissed_until):
    alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="test",
        provider_id="test",
        name="cpu high",
        status="firing",
        severity="critical",
        source=["test"],
        fingerprint=FINGERPRINT,
        alert_hash="hash-1",
    )
    session.add(alert)
    session.commit()
    session.refresh(alert)

    last_alert = LastAlert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint=FINGERPRINT,
        alert_id=alert.id,
        timestamp=datetime.datetime.utcnow(),
        first_timestamp=datetime.datetime.utcnow(),
        status="suppressed",
        dismiss_mode="dismiss_until",
        dismissed_until=dismissed_until,
    )
    session.add(last_alert)
    session.commit()
    return alert, last_alert


def test_check_dismissal_expiry_closes_owned_session(db_session):
    """When the BL creates its own session it must close it, even on the
    no-expired-alerts early return."""
    tracking_session = MagicMock(wraps=db_session)
    with patch(
        "src.common.bl.dismissal_expiry_bl.get_session_sync",
        return_value=tracking_session,
    ):
        DismissalExpiryBl.check_dismissal_expiry(logger)
    tracking_session.close.assert_called_once()


def test_check_dismissal_expiry_does_not_close_caller_session(db_session):
    """A session passed in by the caller is the caller's to close."""
    tracking_session = MagicMock(wraps=db_session)
    DismissalExpiryBl.check_dismissal_expiry(logger, session=tracking_session)
    tracking_session.close.assert_not_called()


def test_expired_dismissal_clears_state_and_audits(db_session):
    past = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    _, last_alert = _insert_dismissed_alert(db_session, past)

    with patch("src.common.bl.dismissal_expiry_bl.ElasticClient"), patch(
        "src.common.bl.dismissal_expiry_bl.notify_sse"
    ):
        DismissalExpiryBl.check_dismissal_expiry(logger, session=db_session)

    db_session.refresh(last_alert)
    assert last_alert.status is None
    assert last_alert.dismiss_mode is None
    assert last_alert.dismissed_until is None

    audits = db_session.exec(
        select(AlertAudit).where(AlertAudit.fingerprint == FINGERPRINT)
    ).all()
    assert len(audits) == 1
    assert audits[0].user_id == "system"


def test_future_dismissal_left_untouched(db_session):
    future = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    _, last_alert = _insert_dismissed_alert(db_session, future)

    with patch("src.common.bl.dismissal_expiry_bl.ElasticClient"), patch(
        "src.common.bl.dismissal_expiry_bl.notify_sse"
    ):
        DismissalExpiryBl.check_dismissal_expiry(logger, session=db_session)

    db_session.refresh(last_alert)
    assert last_alert.status == "suppressed"
    assert last_alert.dismiss_mode == "dismiss_until"
    assert last_alert.dismissed_until is not None


def test_expiry_reindexes_alert_in_elastic_with_str_event_id(db_session):
    """Alert.id is a UUID; AlertDto.id (alias event_id) requires str. The DTO
    build must not raise, and index_alert must receive the converted DTO."""
    past = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    alert, _ = _insert_dismissed_alert(db_session, past)

    with patch("src.common.bl.dismissal_expiry_bl.ElasticClient") as elastic_cls, patch(
        "src.common.bl.dismissal_expiry_bl.notify_sse"
    ):
        DismissalExpiryBl.check_dismissal_expiry(logger, session=db_session)

    elastic_cls.return_value.index_alert.assert_called_once()
    dto = elastic_cls.return_value.index_alert.call_args[0][0]
    assert dto.id == str(alert.id)


def test_recover_strategy_closes_owned_session():
    """Mirror of the keep-event-handler test — recover_strategy must close the
    session it creates (same leak killed the watcher loop)."""
    from src.common.bl.maintenance_windows_bl import MaintenanceWindowsBl

    tracking_session = MagicMock()
    with patch(
        "src.common.bl.maintenance_windows_bl.get_session_sync",
        return_value=tracking_session,
    ), patch(
        "src.common.bl.maintenance_windows_bl.get_maintenance_windows_started",
        return_value=[],
    ), patch(
        "src.common.bl.maintenance_windows_bl.get_alerts_by_status",
        return_value=[],
    ):
        MaintenanceWindowsBl.recover_strategy(logger)
    tracking_session.close.assert_called_once()


def test_recover_strategy_does_not_close_caller_session():
    from src.common.bl.maintenance_windows_bl import MaintenanceWindowsBl

    tracking_session = MagicMock()
    with patch(
        "src.common.bl.maintenance_windows_bl.get_maintenance_windows_started",
        return_value=[],
    ), patch(
        "src.common.bl.maintenance_windows_bl.get_alerts_by_status",
        return_value=[],
    ):
        MaintenanceWindowsBl.recover_strategy(logger, session=tracking_session)
    tracking_session.close.assert_not_called()


@pytest.mark.asyncio
async def test_start_watcher_if_enabled_starts_loop(monkeypatch):
    """start_watcher_if_enabled (called by the src.main:app lifespan — the
    deployed entrypoint) must spawn the watcher loop when WATCHER=true and
    REDIS is off.

    Deliberately does NOT import src.main: importing it executes get_app()
    at module level (route-module reload, app construction), which perturbs
    global state for unrelated tests later in the suite.
    """
    import src.api.config as api_config
    import src.common.consts as consts
    from src.common.event_management import process_watcher_task

    started = asyncio.Event()

    async def fake_watcher(*args):
        started.set()

    monkeypatch.setattr(process_watcher_task, "async_process_watcher", fake_watcher)
    monkeypatch.setattr(api_config, "WATCHER", True)
    monkeypatch.setattr(consts, "REDIS", False)

    task = await process_watcher_task.start_watcher_if_enabled()

    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=2)
    await task


@pytest.mark.asyncio
async def test_start_watcher_if_enabled_skips_when_disabled(monkeypatch):
    import src.api.config as api_config
    import src.common.consts as consts
    from src.common.event_management import process_watcher_task

    watcher_mock = MagicMock()
    monkeypatch.setattr(process_watcher_task, "async_process_watcher", watcher_mock)
    monkeypatch.setattr(api_config, "WATCHER", False)
    monkeypatch.setattr(api_config, "MAINTENANCE_WINDOWS", False)
    monkeypatch.setattr(consts, "REDIS", False)

    task = await process_watcher_task.start_watcher_if_enabled()

    assert task is None
    watcher_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_watcher_loop_survives_tick_errors(monkeypatch):
    """An exception inside one tick (e.g. pool exhaustion) must not kill the
    loop — it should log and retry on the next interval."""
    import contextlib

    from src.common.event_management import process_watcher_task as pwt

    calls = {"count": 0}

    def boom(*args, **kwargs):
        calls["count"] += 1
        raise RuntimeError("tick failed")

    monkeypatch.setattr(
        pwt.MaintenanceWindowsBl, "recover_strategy", staticmethod(boom)
    )
    monkeypatch.setattr(pwt, "REDIS", False)
    monkeypatch.setattr(pwt, "WATCHER_LAPSED_TIME", 0)
    # The lock is not under test (and the real /tmp lock is shared across
    # processes/xdist workers) — replace it with a no-op context manager.
    monkeypatch.setattr(pwt, "FileLock", lambda *a, **kw: contextlib.nullcontext())

    task = asyncio.create_task(pwt.async_process_watcher())
    try:
        deadline = asyncio.get_event_loop().time() + 5
        while calls["count"] < 2:
            if task.done():
                # surface the loop's own exception as the failure
                task.result()
                pytest.fail("watcher loop exited without raising")
            if asyncio.get_event_loop().time() > deadline:
                pytest.fail(
                    f"watcher did not retry after error (ticks={calls['count']})"
                )
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
