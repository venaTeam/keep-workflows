"""Tests for incident & alert suppression derivation and real delete semantics.

Pins the 4 gaps closed in keep-workflows:
  - Groundwork: DismissMode, is_dismiss_active, suppressed_if_dismiss_active_sql
  - Gap 1: Dismiss no longer writes status; get_effective_status derives suppression
  - Gap 2: Alert CEL status & dismissed resolution
  - Gap 3: Incident CEL status resolution
  - Gap 4: IncidentStatus enum changes & real delete_incident_by_id
"""

import datetime
from datetime import timedelta, timezone
from uuid import uuid4

from sqlmodel import select

from src.common.core.alerts import (
    _STRICT_SCHEMA_FIELD_CONFIGS,
    alert_field_configurations,
)
from src.common.core.cel_to_sql.properties_metadata import PropertiesMetadata
from src.common.core.cel_to_sql.sql_providers.get_cel_to_sql_provider_for_dialect import (
    get_cel_to_sql_provider_for_dialect,
)
from src.common.core.db import (
    _LastAlertEnrichmentView,
    _translate_dismissed,
    delete_incident_by_id,
)
from src.common.core.dependencies import SINGLE_TENANT_UUID
from src.common.core.incidents import incident_field_configurations
from src.common.models.db.alert import (
    Alert,
    AlertAudit,
    AlertToIncident,
    CommentMention,
    IncidentEnrichment,
    LastAlert,
    LastAlertToIncident,
)
from src.common.models.db.helpers import (
    DismissMode,
    is_dismiss_active,
    suppressed_if_dismiss_active_sql,
)
from src.common.models.db.incident import (
    Incident,
    IncidentDismissMode,
    IncidentStatus,
)


def _future(hours=2) -> datetime.datetime:
    return datetime.datetime.now(timezone.utc) + timedelta(hours=hours)


def _past(hours=2) -> datetime.datetime:
    return datetime.datetime.now(timezone.utc) - timedelta(hours=hours)


# ============================================================================
# Groundwork: Helpers & Model Methods
# ============================================================================


def test_dismiss_mode_enum():
    assert DismissMode.PERMANENT.value == "permanent"
    assert DismissMode.DISMISS_UNTIL.value == "dismiss_until"


def test_is_dismiss_active_permanent():
    assert is_dismiss_active("permanent", None) is True
    assert is_dismiss_active("permanent", _past()) is True


def test_is_dismiss_active_until():
    assert is_dismiss_active("dismiss_until", _future()) is True
    assert is_dismiss_active("dismiss_until", _past()) is False
    assert is_dismiss_active("dismiss_until", None) is False


def test_suppressed_if_dismiss_active_sql():
    sql = suppressed_if_dismiss_active_sql("lastalert")
    assert "lastalert.dismiss_mode = 'permanent'" in sql
    assert "lastalert.dismiss_mode = 'dismiss_until'" in sql
    assert "lastalert.dismissed_until > CURRENT_TIMESTAMP" in sql


def test_last_alert_effective_status():
    la = LastAlert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint="fp1",
        alert_id=uuid4(),
        timestamp=datetime.datetime.now(timezone.utc),
        first_timestamp=datetime.datetime.now(timezone.utc),
        status=None,
        dismiss_mode="permanent",
    )
    assert la.is_dismiss_active() is True
    assert la.get_effective_status() == "suppressed"

    # With expired dismissal, reverts to underlying status
    la.dismiss_mode = "dismiss_until"
    la.dismissed_until = _past()
    la.status = "firing"
    assert la.is_dismiss_active() is False
    assert la.get_effective_status() == "firing"


def test_incident_effective_status():
    inc = Incident(
        tenant_id=SINGLE_TENANT_UUID,
        status=IncidentStatus.FIRING.value,
        dismiss_mode=IncidentDismissMode.PERMANENT.value,
    )
    assert inc.is_dismiss_active() is True
    assert inc.get_effective_status() == "suppressed"

    # Expired dismissal
    inc.dismiss_mode = IncidentDismissMode.DISMISS_UNTIL.value
    inc.dismissed_until = _past()
    assert inc.is_dismiss_active() is False
    assert inc.get_effective_status() == "firing"


# ============================================================================
# Gap 1: Stop writing status on dismissal
# ============================================================================


def test_translate_dismissed_does_not_write_status():
    # dismissed: True writes dismiss_mode, not status
    res = _translate_dismissed({"dismissed": True})
    assert res.get("dismiss_mode") == "permanent"
    assert "status" not in res

    # dismissed: True with timestamp
    ts = _future()
    res = _translate_dismissed({"dismissed": True, "dismissed_until": ts})
    assert res.get("dismiss_mode") == "dismiss_until"
    assert res.get("dismissed_until") == ts
    assert "status" not in res

    # dismissed: False clears dismiss columns without clobbering status override
    res = _translate_dismissed({"dismissed": False})
    assert res.get("dismiss_mode") is None
    assert res.get("dismissed_until") is None
    assert "status" not in res

    # explicit status preserved
    res = _translate_dismissed({"dismissed": False, "status": "acknowledged"})
    assert res.get("status") == "acknowledged"
    assert res.get("dismiss_mode") is None


def test_last_alert_enrichment_view_derives_status():
    la = LastAlert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint="fp-view",
        alert_id=uuid4(),
        timestamp=datetime.datetime.now(timezone.utc),
        first_timestamp=datetime.datetime.now(timezone.utc),
        status=None,
        dismiss_mode="permanent",
    )
    view = _LastAlertEnrichmentView(SINGLE_TENANT_UUID, "fp-view", la)
    assert view.enrichments.get("status") == "suppressed"

    # When expired, no suppressed status in enrichments
    la.dismiss_mode = "dismiss_until"
    la.dismissed_until = _past()
    view = _LastAlertEnrichmentView(SINGLE_TENANT_UUID, "fp-view", la)
    assert "status" not in view.enrichments


# ============================================================================
# Gap 2: Alert CEL status and dismissed mappings
# ============================================================================


def test_alert_cel_mappings():
    # Find status configuration in _STRICT_SCHEMA_FIELD_CONFIGS
    status_cfg = next(c for c in _STRICT_SCHEMA_FIELD_CONFIGS if c.map_from_pattern == "status")
    # First entry in map_to must be the suppressed CASE
    assert "lastalert.dismiss_mode = 'permanent'" in status_cfg.map_to[0]
    assert status_cfg.map_to[1] == "lastalert.status"
    assert status_cfg.map_to[2] == "alert.status"

    # Find dismissed configuration
    dismissed_cfg = next(c for c in _STRICT_SCHEMA_FIELD_CONFIGS if c.map_from_pattern == "dismissed")
    assert "lastalert.status = 'suppressed'" not in dismissed_cfg.map_to[0]
    assert "lastalert.dismiss_mode = 'permanent'" in dismissed_cfg.map_to[0]
    assert "lastalert.dismissed_until > CURRENT_TIMESTAMP" in dismissed_cfg.map_to[0]


def test_alert_cel_compilation_to_sql():
    provider = get_cel_to_sql_provider_for_dialect(
        "postgresql", PropertiesMetadata(alert_field_configurations)
    )
    # CEL query for status == 'suppressed'
    sql = provider.convert_to_sql_str("status == 'suppressed'")
    assert "lastalert.dismiss_mode = 'permanent'" in sql
    assert "lastalert.dismiss_mode = 'dismiss_until'" in sql

    # CEL query for dismissed == true
    sql = provider.convert_to_sql_str("dismissed == true")
    assert "lastalert.dismiss_mode = 'permanent'" in sql
    assert "lastalert.dismiss_mode = 'dismiss_until'" in sql


# ============================================================================
# Gap 3: Incident CEL status mapping
# ============================================================================


def test_incident_cel_mapping():
    status_cfg = next(c for c in incident_field_configurations if c.map_from_pattern == "status")
    # Dropped JSON(incidentenrichment.enrichments).*
    assert not any("incidentenrichment" in str(m) for m in status_cfg.map_to)
    assert "incident.dismiss_mode = 'permanent'" in status_cfg.map_to[0]
    assert status_cfg.map_to[1] == "incident.status"


def test_incident_cel_compilation_to_sql():
    provider = get_cel_to_sql_provider_for_dialect(
        "postgresql", PropertiesMetadata(incident_field_configurations)
    )
    sql = provider.convert_to_sql_str("status == 'firing'")
    assert "COALESCE" in sql
    assert "incident.dismiss_mode = 'permanent'" in sql
    assert "incident.status) = 'firing'" in sql


# ============================================================================
# Gap 4: IncidentStatus enum and real delete_incident_by_id
# ============================================================================


def test_incident_status_enum_members():
    assert not hasattr(IncidentStatus, "DELETED")
    assert "deleted" not in [s.value for s in IncidentStatus]
    assert IncidentStatus.SUPPRESSED.value == "suppressed"
    assert IncidentStatus.get_closed(return_values=True) == ["resolved", "merged"]


def test_delete_incident_by_id_removes_rows_and_dependents(db_session):
    tenant_id = SINGLE_TENANT_UUID
    incident_id = uuid4()
    sibling_id = uuid4()

    # Create target incident
    inc = Incident(
        id=incident_id,
        tenant_id=tenant_id,
        user_generated_name="target-inc",
        user_summary="",
        generated_summary="",
        status=IncidentStatus.FIRING.value,
        running_number=1001,
    )
    db_session.add(inc)
    db_session.flush()

    # Create sibling incident that references target
    sibling = Incident(
        id=sibling_id,
        tenant_id=tenant_id,
        user_generated_name="sibling-inc",
        user_summary="",
        generated_summary="",
        status=IncidentStatus.FIRING.value,
        merged_into_incident_id=incident_id,
        same_incident_in_the_past_id=incident_id,
        running_number=1002,
    )
    db_session.add(sibling)
    db_session.flush()

    # Create alert and link
    alert = Alert(
        id=uuid4(),
        tenant_id=tenant_id,
        provider_type="mock",
        provider_id="mock",
        fingerprint="fp-del",
        timestamp=datetime.datetime.now(timezone.utc),
        name="test",
        alert_hash="h",
    )
    db_session.add(alert)
    db_session.flush()

    link1 = LastAlertToIncident(
        tenant_id=tenant_id,
        fingerprint="fp-del",
        incident_id=incident_id,
    )
    link2 = AlertToIncident(
        tenant_id=tenant_id,
        alert_id=alert.id,
        incident_id=incident_id,
    )
    db_session.add(link1)
    db_session.add(link2)

    # Create IncidentEnrichment
    enrichment = IncidentEnrichment(
        tenant_id=tenant_id,
        incident_id=incident_id,
        enrichments={"note": "something"},
    )
    db_session.add(enrichment)

    # Create AlertAudit for the incident (keyed by incident UUID string in fingerprint)
    audit = AlertAudit(
        tenant_id=tenant_id,
        fingerprint=str(incident_id),
        user_id="tester",
        action="GENERIC_ENRICH",
        description="testing",
        timestamp=datetime.datetime.now(timezone.utc),
    )
    db_session.add(audit)
    db_session.flush()

    audit_id = audit.id
    mention = CommentMention(
        tenant_id=tenant_id,
        comment_id=audit_id,
        mentioned_user_id="user1",
    )
    db_session.add(mention)
    db_session.commit()

    # Now delete the incident
    success = delete_incident_by_id(tenant_id, incident_id, session=db_session)
    assert success is True

    # Assert incident row is gone for real (not status='deleted')
    deleted_inc = db_session.exec(
        select(Incident).where(Incident.tenant_id == tenant_id, Incident.id == incident_id)
    ).first()
    assert deleted_inc is None

    # Assert links and enrichment are deleted
    assert (
        db_session.exec(
            select(LastAlertToIncident).where(
                LastAlertToIncident.tenant_id == tenant_id,
                LastAlertToIncident.incident_id == incident_id,
            )
        ).first()
        is None
    )
    assert (
        db_session.exec(
            select(AlertToIncident).where(
                AlertToIncident.tenant_id == tenant_id,
                AlertToIncident.incident_id == incident_id,
            )
        ).first()
        is None
    )
    assert (
        db_session.exec(
            select(IncidentEnrichment).where(
                IncidentEnrichment.tenant_id == tenant_id,
                IncidentEnrichment.incident_id == incident_id,
            )
        ).first()
        is None
    )
    assert (
        db_session.exec(
            select(AlertAudit).where(
                AlertAudit.tenant_id == tenant_id,
                AlertAudit.fingerprint == str(incident_id),
            )
        ).first()
        is None
    )
    assert (
        db_session.exec(
            select(CommentMention).where(
                CommentMention.tenant_id == tenant_id,
                CommentMention.comment_id == audit_id,
            )
        ).first()
        is None
    )

    # Sibling references were nulled
    db_session.refresh(sibling)
    assert sibling.merged_into_incident_id is None
    assert sibling.same_incident_in_the_past_id is None

    # Non-existent incident returns False
    assert delete_incident_by_id(tenant_id, uuid4(), session=db_session) is False
