"""
alertenrichment removal tests — keep-workflows.

Covers the typed-LastAlert-column enrichment path that replaces the
alertenrichment JSONB column:
  - enrich writes to typed columns
  - D1: enrich-before-first-alert is a no-op (no LastAlert row) but still audits
  - note-guard: empty incoming note does not erase an existing note
  - unknown enrichment key -> ValueError (422 at route)
  - dismissed <-> dismiss_mode translation
  - set_last_alert clearing on re-fire (status_disposable) and resolve
  - dismissal-expiry resets the LastAlert columns
  - provider read builds the enrichment dict from columns
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.common.core import db as db_module
from src.common.core.db import (
    LASTALERT_ENRICHMENT_COLUMNS,
    LASTALERT_TRACKING_COLUMNS,
    _enrich_entity,
    enrich_entity,
    get_enrichment_with_session,
    get_enrichments,
    set_last_alert,
)
from src.common.bl.dismissal_expiry_bl import DismissalExpiryBl
from src.common.core.dependencies import SINGLE_TENANT_UUID
from src.common.models.action_type import ActionType
from src.common.models.alert import AlertStatus
from src.common.models.db.alert import Alert, AlertAudit, LastAlert


def _make_alert(db_session, fingerprint, status=AlertStatus.FIRING.value, ts=None):
    """Create an Alert + LastAlert row directly for tests."""
    ts = ts or datetime.now(tz=timezone.utc)
    alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="mock",
        provider_id="mock",
        fingerprint=fingerprint,
        timestamp=ts,
        status=status,
        name="t",
        alert_hash="h",
    )
    db_session.add(alert)
    db_session.flush()
    last_alert = LastAlert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint=fingerprint,
        alert_id=alert.id,
        timestamp=ts,
        first_timestamp=ts,
        alert_hash="h",
    )
    db_session.add(last_alert)
    db_session.commit()
    return alert, last_alert


def test_enrichment_columns_match_model():
    assert LASTALERT_ENRICHMENT_COLUMNS == {
        "status", "status_disposable", "dismiss_mode", "dismissed_until", "assignee",
        "note", "deleted", "ticket_type", "ticket_url", "ticket_provider_id",
    }


def test_tracking_columns_match_model():
    assert LASTALERT_TRACKING_COLUMNS == {
        "last_received", "firing_counter", "unresolved_counter",
        "started_at", "firing_start_time", "firing_start_time_since_last_resolved",
    }


def test_enrich_writes_typed_columns(db_session):
    _make_alert(db_session, "fp-1")
    _enrich_entity(
        db_session,
        SINGLE_TENANT_UUID,
        "fp-1",
        {"assignee": "bob", "note": "hello"},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="assign+note",
    )
    la = db_session.query(LastAlert).filter_by(fingerprint="fp-1").one()
    assert la.assignee == "bob"
    assert la.note == "hello"
    # AlertAudit row created
    audits = db_session.query(AlertAudit).filter_by(fingerprint="fp-1").all()
    assert len(audits) == 1


def test_enrich_before_first_alert_is_noop_but_audits(db_session):
    # D1: no LastAlert row -> no column write, but AlertAudit still created.
    result = _enrich_entity(
        db_session,
        SINGLE_TENANT_UUID,
        "ghost-fp",
        {"assignee": "bob"},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="assign",
    )
    assert result is None
    assert db_session.query(LastAlert).filter_by(fingerprint="ghost-fp").first() is None
    audits = db_session.query(AlertAudit).filter_by(fingerprint="ghost-fp").all()
    assert len(audits) == 1


def test_note_guard_preserves_existing_note(db_session):
    _make_alert(db_session, "fp-note")
    enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-note",
        {"note": "keep me"},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="note",
        session=db_session,
    )
    # Empty incoming note must NOT erase the existing note.
    enrich_entity(
        SINGLE_TENANT_UUID,
        "fp-note",
        {"note": "   ", "assignee": "bob"},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="empty note",
        session=db_session,
    )
    la = db_session.query(LastAlert).filter_by(fingerprint="fp-note").one()
    assert la.note == "keep me"
    assert la.assignee == "bob"


def test_unknown_key_rejected(db_session):
    _make_alert(db_session, "fp-unknown")
    with pytest.raises(ValueError):
        _enrich_entity(
            db_session,
            SINGLE_TENANT_UUID,
            "fp-unknown",
            # `unknown_field` is intentionally NOT in LASTALERT_ENRICHMENT_COLUMNS.
            # (ticket_url is now allow-listed as a typed column.)
            {"unknown_field": "x"},
            action_type=ActionType.GENERIC_ENRICH,
            action_callee="alice",
            action_description="bad key",
        )


def test_dismissed_translation_permanent(db_session):
    _make_alert(db_session, "fp-dismiss")
    _enrich_entity(
        db_session,
        SINGLE_TENANT_UUID,
        "fp-dismiss",
        {"dismissed": True},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="dismiss",
    )
    la = db_session.query(LastAlert).filter_by(fingerprint="fp-dismiss").one()
    assert la.status == "suppressed"
    assert la.dismiss_mode == "permanent"


def test_dismissed_false_clears(db_session):
    _, la = _make_alert(db_session, "fp-undismiss")
    la.status = "suppressed"
    la.dismiss_mode = "permanent"
    db_session.add(la)
    db_session.commit()
    _enrich_entity(
        db_session,
        SINGLE_TENANT_UUID,
        "fp-undismiss",
        {"dismissed": False},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="undismiss",
    )
    la = db_session.query(LastAlert).filter_by(fingerprint="fp-undismiss").one()
    assert la.status is None
    assert la.dismiss_mode is None
    assert la.dismissed_until is None


def test_dismiss_until_translation(db_session):
    _make_alert(db_session, "fp-until")
    until = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    _enrich_entity(
        db_session,
        SINGLE_TENANT_UUID,
        "fp-until",
        {"dismissed": True, "dismiss_until": until},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="dismiss until",
    )
    la = db_session.query(LastAlert).filter_by(fingerprint="fp-until").one()
    assert la.status == "suppressed"
    assert la.dismiss_mode == "dismiss_until"
    assert la.dismissed_until is not None


def test_set_last_alert_status_disposable_clears_on_refire(db_session):
    alert, la = _make_alert(db_session, "fp-disp", status=AlertStatus.FIRING.value)
    la.status = "acknowledged"
    la.status_disposable = True
    db_session.add(la)
    db_session.commit()

    # New non-resolved occurrence
    new_ts = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    new_alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="mock",
        provider_id="mock",
        fingerprint="fp-disp",
        timestamp=new_ts,
        status=AlertStatus.FIRING.value,
        name="t",
        alert_hash="h2",
    )
    db_session.add(new_alert)
    db_session.commit()
    set_last_alert(SINGLE_TENANT_UUID, new_alert, session=db_session)

    la = db_session.query(LastAlert).filter_by(fingerprint="fp-disp").one()
    assert la.status is None
    assert la.status_disposable is False


def test_set_last_alert_permanent_dismiss_survives_resolve(db_session):
    alert, la = _make_alert(db_session, "fp-perm", status=AlertStatus.FIRING.value)
    la.status = "suppressed"
    la.dismiss_mode = "permanent"
    db_session.add(la)
    db_session.commit()

    new_ts = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    resolved_alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="mock",
        provider_id="mock",
        fingerprint="fp-perm",
        timestamp=new_ts,
        status=AlertStatus.RESOLVED.value,
        name="t",
        alert_hash="h2",
    )
    db_session.add(resolved_alert)
    db_session.commit()
    set_last_alert(SINGLE_TENANT_UUID, resolved_alert, session=db_session)

    la = db_session.query(LastAlert).filter_by(fingerprint="fp-perm").one()
    # permanent survives resolve
    assert la.status == "suppressed"
    assert la.dismiss_mode == "permanent"


def test_set_last_alert_until_resolved_clears_on_resolve(db_session):
    alert, la = _make_alert(db_session, "fp-ur", status=AlertStatus.FIRING.value)
    la.status = "suppressed"
    la.dismiss_mode = "until_resolved"
    db_session.add(la)
    db_session.commit()

    new_ts = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    resolved_alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="mock",
        provider_id="mock",
        fingerprint="fp-ur",
        timestamp=new_ts,
        status=AlertStatus.RESOLVED.value,
        name="t",
        alert_hash="h2",
    )
    db_session.add(resolved_alert)
    db_session.commit()
    set_last_alert(SINGLE_TENANT_UUID, resolved_alert, session=db_session)

    la = db_session.query(LastAlert).filter_by(fingerprint="fp-ur").one()
    assert la.status is None
    assert la.dismiss_mode is None


def test_set_last_alert_writes_tracking(db_session):
    alert, la = _make_alert(db_session, "fp-track", status=AlertStatus.FIRING.value)
    new_ts = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    new_alert = Alert(
        tenant_id=SINGLE_TENANT_UUID,
        provider_type="mock",
        provider_id="mock",
        fingerprint="fp-track",
        timestamp=new_ts,
        status=AlertStatus.FIRING.value,
        name="t",
        alert_hash="h2",
    )
    db_session.add(new_alert)
    db_session.commit()
    set_last_alert(
        SINGLE_TENANT_UUID,
        new_alert,
        session=db_session,
        tracking={"firing_counter": 3, "last_received": new_ts},
    )
    la = db_session.query(LastAlert).filter_by(fingerprint="fp-track").one()
    assert la.firing_counter == 3
    assert la.last_received is not None


def test_dismissal_expiry_resets_lastalert_columns(db_session, monkeypatch):
    # avoid elastic + sse side effects
    monkeypatch.setattr(
        "src.common.bl.dismissal_expiry_bl.ElasticClient",
        lambda *a, **k: type("E", (), {"index_alert": lambda self, dto: None})(),
    )
    monkeypatch.setattr(
        "src.common.bl.dismissal_expiry_bl.notify_sse", lambda *a, **k: None
    )

    _, la = _make_alert(db_session, "fp-expire", status=AlertStatus.FIRING.value)
    la.status = "suppressed"
    la.dismiss_mode = "dismiss_until"
    la.dismissed_until = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    db_session.add(la)
    db_session.commit()

    expired = DismissalExpiryBl.get_alerts_with_expired_dismissals(db_session)
    assert any(e.fingerprint == "fp-expire" for e in expired)

    DismissalExpiryBl.check_dismissal_expiry(logging.getLogger("t"), session=db_session)

    la = db_session.query(LastAlert).filter_by(fingerprint="fp-expire").one()
    assert la.status is None
    assert la.dismiss_mode is None
    assert la.dismissed_until is None


def test_get_enrichments_builds_dict_from_columns(db_session):
    _, la = _make_alert(db_session, "fp-read")
    la.assignee = "bob"
    la.note = "hi"
    la.status = "suppressed"
    db_session.add(la)
    db_session.commit()

    # get_enrichments opens its own Session against the patched engine.
    views = get_enrichments(SINGLE_TENANT_UUID, ["fp-read"])
    assert len(views) == 1
    enrichments = views[0].enrichments
    assert enrichments["assignee"] == "bob"
    assert enrichments["note"] == "hi"
    # internal flag not surfaced
    assert "status_disposable" not in enrichments

    # session-scoped read returns the same shape
    view = get_enrichment_with_session(db_session, SINGLE_TENANT_UUID, "fp-read")
    assert view.enrichments["assignee"] == "bob"


# ============================================================================
# Review-pass regressions (parity with keep-api-gateway fixes)
# ============================================================================


def test_dismissed_false_preserves_explicit_status(db_session):
    """Undismiss with an explicit caller status must NOT clobber the status.

    Regression for api-gateway commit f1b181c: the change-status modal moving
    suppressed -> acknowledged emits `{dismissed: False, status: "acknowledged"}`.
    `_translate_dismissed` must `setdefault("status", None)`, not assign None.
    """
    _, la = _make_alert(db_session, "fp-undismiss-status")
    la.status = "suppressed"
    la.dismiss_mode = "permanent"
    db_session.add(la)
    db_session.commit()

    _enrich_entity(
        db_session,
        SINGLE_TENANT_UUID,
        "fp-undismiss-status",
        {"dismissed": False, "status": "acknowledged"},
        action_type=ActionType.GENERIC_ENRICH,
        action_callee="alice",
        action_description="change-status from suppressed",
    )
    la = (
        db_session.query(LastAlert)
        .filter_by(fingerprint="fp-undismiss-status")
        .one()
    )
    assert la.status == "acknowledged"
    assert la.dismiss_mode is None
    assert la.dismissed_until is None


def test_strict_false_discards_unknown_keys(db_session):
    """System writes (mapping/extraction/workflow YAML) emit arbitrary keys.

    With strict=False they must be discarded with a warning instead of raising
    (parity with keep-api-gateway `normalize_enrichments(strict=False)`).
    """
    _make_alert(db_session, "fp-strict-false")
    # Should not raise: arbitrary keys are dropped, known keys still apply.
    result = _enrich_entity(
        db_session,
        SINGLE_TENANT_UUID,
        "fp-strict-false",
        {"ticket_url": "https://example", "assignee": "bob"},
        action_type=ActionType.MAPPING_RULE_ENRICH,
        action_callee="system",
        action_description="mapping enrich",
        strict=False,
    )
    assert result is not None
    la = (
        db_session.query(LastAlert)
        .filter_by(fingerprint="fp-strict-false")
        .one()
    )
    # known key applied, unknown key dropped
    assert la.assignee == "bob"


def test_convert_db_alerts_to_dto_filters_by_tenant(db_session):
    """convert_db_alerts_to_dto_alerts must scope the LastAlert lookup by tenant.

    Guards BOTH failure modes of dropping the `tenant_id.in_(...)` filter:
      1. same-fingerprint cross-tenant collision (tenant B owns "shared-fp"
         too), and
      2. a DIFFERENT-fingerprint tenant-B row that a fingerprint-only `IN (...)`
         query would over-fetch.
    If the tenant scoping is removed, the query becomes fp-only and tenant B's
    rows are pulled into the batch — this test asserts they never reach the DTO.
    """
    from src.common.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
    from src.common.models.db.tenant import Tenant

    OTHER_TENANT = "other-tenant-id"
    # tenant A: assignee=alice on the shared fingerprint.
    alert_a, la_a = _make_alert(db_session, "shared-fp")
    la_a.assignee = "alice"
    db_session.add(la_a)
    db_session.commit()

    # The test DB is sqlite with no FK enforcement; add the other tenant row so
    # the cross-tenant LastAlert rows can be inserted via the ORM.
    db_session.add(Tenant(id=OTHER_TENANT, name="other"))
    db_session.flush()

    def _add_other_tenant_lastalert(fp, assignee, hash_suffix):
        ts = datetime.now(tz=timezone.utc) + timedelta(seconds=1)
        alert_b = Alert(
            tenant_id=OTHER_TENANT,
            provider_type="mock",
            provider_id="mock",
            fingerprint=fp,
            timestamp=ts,
            status=AlertStatus.FIRING.value,
            name="t",
            alert_hash=hash_suffix,
        )
        db_session.add(alert_b)
        db_session.flush()
        db_session.add(
            LastAlert(
                tenant_id=OTHER_TENANT,
                fingerprint=fp,
                alert_id=alert_b.id,
                timestamp=ts,
                first_timestamp=ts,
                alert_hash=hash_suffix,
                assignee=assignee,
            )
        )

    # (1) tenant B, SAME fingerprint, different assignee.
    _add_other_tenant_lastalert("shared-fp", "bob", "h-other")
    # (2) tenant B, DIFFERENT fingerprint. A fp-only query would NOT fetch this
    #     (fp not in the batch's fps), but if scoping switched to tenant-only or
    #     dropped entirely this row could leak; assert it never surfaces.
    _add_other_tenant_lastalert("other-fp", "carol", "h-other2")
    db_session.commit()

    # Convert only tenant A's alert -> must pick up ONLY tenant A's "alice".
    dtos = convert_db_alerts_to_dto_alerts([alert_a], session=db_session)
    assert len(dtos) == 1
    assert dtos[0].assignee == "alice"
    assert dtos[0].assignee != "bob"
    assert dtos[0].assignee != "carol"


def test_dismissed_until_read_path_coerced_to_iso_string(db_session):
    """Read paths must emit `dismissed_until` as a canonical ISO string.

    The column is TIMESTAMPTZ; if a raw datetime leaks through, JSON
    serialization (SSE notify requests.post(json=...)) raises TypeError and
    Elastic gets a non-canonical timestamp. Both `_build_enrichments`
    (get_enrichments / get_enrichment_with_session) and the
    convert_db_alerts_to_dto_alerts copy path must coerce to
    "YYYY-MM-DDThh:mm:ss.mmmZ".
    """
    import re

    from src.common.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts

    ISO_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"

    alert, la = _make_alert(db_session, "fp-dismissed-until")
    la.status = "suppressed"
    la.dismiss_mode = "dismiss_until"
    la.dismissed_until = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    db_session.add(la)
    db_session.commit()

    # (a) _build_enrichments via session-scoped read.
    view = get_enrichment_with_session(
        db_session, SINGLE_TENANT_UUID, "fp-dismissed-until"
    )
    du = view.enrichments["dismissed_until"]
    assert isinstance(du, str), f"expected str, got {type(du)}"
    assert re.match(ISO_RE, du), du

    # (a') _build_enrichments via engine-scoped read.
    views = get_enrichments(SINGLE_TENANT_UUID, ["fp-dismissed-until"])
    du2 = views[0].enrichments["dismissed_until"]
    assert isinstance(du2, str), f"expected str, got {type(du2)}"
    assert re.match(ISO_RE, du2), du2

    # (b) convert_db_alerts_to_dto_alerts copy path.
    dtos = convert_db_alerts_to_dto_alerts([alert], session=db_session)
    assert len(dtos) == 1
    du3 = dtos[0].dismissed_until
    assert isinstance(du3, str), f"expected str, got {type(du3)}"
    assert re.match(ISO_RE, du3), du3
