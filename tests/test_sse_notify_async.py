"""
Async SSE notify offload + coalescing tests for the keep-workflows copy.

Mirror of keep-event-handler's tests/test_sse_notify_async.py (cross-repo
rule), adjusted to the workflows package layout. The workflows copy was fully
synchronous before SC-04; these tests prove it now offloads + coalesces like
event-handler.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import src.common.event_management.process_event_task as pet


@pytest.fixture
def notify_pool():
    """
    Yields a freshly-started SSE notify pool and tears it down deterministically.

    The notify pool in process_event_task is a module-level ThreadPoolExecutor
    of daemon threads. A test that submits work to it (and shuts it down) would
    otherwise leave a dead executor behind for the next test, or leak in-flight
    daemon threads across tests. This fixture rebuilds the pool before the test
    and drains + rebuilds it afterwards so each test starts from a clean,
    started worker and nothing leaks across the suite.
    """
    pet.rebuild_sse_pool()
    try:
        yield pet
    finally:
        pet.shutdown_sse_pool(wait=True)
        pet.rebuild_sse_pool()


def test_submit_notify_is_non_blocking_and_still_delivers(notify_pool):
    """
    SSE notifications must not block the event-processing thread.

    We patch the shared session's post() with a slow (0.5s) side effect. If
    _submit_notify were synchronous, three calls would take >= 1.5s. Because
    submission is offloaded to a background pool, the three submits must return
    in well under that time. After shutdown_sse_pool(wait=True) flushes the
    queue, all three notifications must have actually been delivered.
    """
    pet = notify_pool
    call_count = [0]

    def slow_post(*args, **kwargs):
        time.sleep(0.5)
        call_count[0] += 1
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    with patch.object(pet._sse_session, "post", side_effect=slow_post) as mock_post:
        start = time.monotonic()
        pet._submit_notify("http://localhost:8080", "t1", "poll-alerts", {"alerts": []})
        pet._submit_notify(
            "http://localhost:8080", "t1", "incident-change", {"incident_ids": ["1"]}
        )
        pet._submit_notify(
            "http://localhost:8080", "t1", "poll-presets", {"preset_names": ["p"]}
        )
        elapsed = time.monotonic() - start

        # Submission must be non-blocking: nowhere near 3 * 0.5s.
        assert elapsed < 0.1, f"submission blocked the caller for {elapsed:.3f}s"

        # Flush the background pool so pending notifications are delivered.
        pet.shutdown_sse_pool(wait=True)

        # All three notifications were actually sent.
        assert mock_post.call_count == 3
        assert call_count[0] == 3


def test_coalescing_collapses_duplicate_tenant_event_notifications(notify_pool):
    """
    With coalescing enabled and a single worker, while the first poll-alerts
    delivery is in flight, many further poll-alerts submits for the same
    (tenant, event) collapse to a single pending payload — so total deliveries
    are far fewer than submissions, and the LAST payload wins (refresh-style).
    """
    pet = notify_pool
    delivered = []
    release = threading.Event()
    first_in_flight = threading.Event()

    def post(url, json=None, timeout=None):
        # Hold the very first delivery so duplicates pile up behind it.
        if not first_in_flight.is_set():
            first_in_flight.set()
            release.wait(timeout=5)
        delivered.append(json["data"])
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    assert pet._SSE_COALESCE_ENABLED is True

    with patch.object(pet._sse_session, "post", side_effect=post):
        # First submit starts a worker that blocks in post().
        pet._submit_notify("http://localhost:8080", "t1", "poll-alerts", {"n": 0})
        assert first_in_flight.wait(timeout=5)

        # Many duplicate (t1, poll-alerts) submits while the worker is busy.
        for i in range(1, 51):
            pet._submit_notify("http://localhost:8080", "t1", "poll-alerts", {"n": i})

        # Release the in-flight delivery and drain.
        release.set()
        pet.shutdown_sse_pool(wait=True)

    # 51 submissions, but coalesced to at most 2 deliveries (the first in-flight
    # one + one collapsed delivery of the latest payload).
    assert len(delivered) <= 2
    assert len(delivered) >= 1
    # The collapsed delivery carried the LATEST payload (refresh-style).
    assert delivered[-1] == {"n": 50}


def test_distinct_keys_are_not_coalesced(notify_pool):
    """Different (tenant, event) keys are independent — each is delivered."""
    pet = notify_pool
    delivered = []

    def post(url, json=None, timeout=None):
        delivered.append((json.get("tenant_id"), json.get("event")))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    with patch.object(pet._sse_session, "post", side_effect=post):
        pet._submit_notify("http://localhost:8080", "t1", "poll-alerts", {})
        pet._submit_notify("http://localhost:8080", "t2", "poll-alerts", {})
        pet._submit_notify("http://localhost:8080", "t1", "poll-presets", {})
        pet.shutdown_sse_pool(wait=True)

    assert set(delivered) == {
        ("t1", "poll-alerts"),
        ("t2", "poll-alerts"),
        ("t1", "poll-presets"),
    }
