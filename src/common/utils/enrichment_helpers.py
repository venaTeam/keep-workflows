import logging
from datetime import datetime, timezone
from typing import Optional

from opentelemetry import trace
from sqlmodel import Session

from sqlmodel import select

from src.common.core.db import existed_or_new_session
from src.common.models.alert import (
    AlertDto,
    AlertStatus,
    AlertWithIncidentLinkMetadataDto,
)
from src.common.models.db.alert import Alert, LastAlert, LastAlertToIncident

# Phase 2: user enrichment state lives on these typed LastAlert columns.
_LASTALERT_USER_COLUMNS = (
    "status",
    "dismiss_mode",
    "dismissed_until",
    "assignee",
    "note",
    "deleted",
)
# Phase 2: system tracking fields relocated from Alert to LastAlert.
_LASTALERT_TRACKING_COLUMNS = (
    "last_received",
    "firing_counter",
    "unresolved_counter",
    "started_at",
    "firing_start_time",
    "firing_start_time_since_last_resolved",
)
from src.common.models.incident import IncidentDto

tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)


def javascript_iso_format(last_received) -> str:
    """
    https://stackoverflow.com/a/63894149/12012756
    Accepts either an ISO-format string or a datetime (since the ORM column
    is now TIMESTAMPTZ and may bypass AlertDto's string-coercion validator).
    """
    if isinstance(last_received, datetime):
        dt = last_received
    else:
        dt = datetime.fromisoformat(last_received)
    # Normalize to UTC so the output is canonical "...Z" regardless of input TZ.
    # Postgres TIMESTAMPTZ returns datetimes in the session TZ (often the server's
    # local TZ, e.g. +03:00), which would otherwise emit "+03:00" suffix and break
    # comparisons against enrichments that store canonical UTC "Z" strings.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Phase 2: parse_and_enrich_deleted_and_assignees removed. `deleted` and
# `assignee` are now typed LastAlert columns (direct bool / str) instead of
# timestamp-keyed list/dict enrichments, so callers setattr() them straight
# from the LastAlert-sourced enrichments dict.


def calculated_start_firing_time(
    alert: AlertDto, previous_alert: AlertDto | list[AlertDto]
) -> str:
    """
    Calculate the start firing time of an alert based on the previous alert.

    Args:
        alert (AlertDto): The alert to calculate the start firing time for.
        previous_alert (AlertDto): The previous alert.

    Returns:
        str: The calculated start firing time.
    """
    # if the alert is not firing, there is no start firing time
    if alert.status != AlertStatus.FIRING.value:
        return None
    # if this is the first alert, the start firing time is the same as the last received time
    if not previous_alert:
        return alert.last_received
    elif isinstance(previous_alert, list):
        previous_alert = previous_alert[0]
    # else, if the previous alert was firing, the start firing time is the same as the previous alert
    if previous_alert.status == AlertStatus.FIRING.value:
        return previous_alert.firing_start_time
    # else, if the previous alert was resolved, the start firing time is the same as the last received time
    else:
        return alert.last_received


def calculate_firing_time_since_last_resolved(
    alert: AlertDto, previous_alert: AlertDto | list[AlertDto]
) -> int:
    """
    Calculate the firing counter of an alert based on the previous alert.
    """
    # if the alert is resolved, there is no firing time.
    if alert.status == AlertStatus.RESOLVED.value:
        return None
    else:
        # if there is previous alert, we need to check if it has firing time
        if previous_alert:
            if isinstance(previous_alert, list):
                previous_alert = previous_alert[0]
            if (
                previous_alert.status == AlertStatus.RESOLVED.value
                and alert.status == AlertStatus.FIRING.value
            ):
                return alert.last_received
            # if the previous alert has firing time since last resolved, we need to return it
            if previous_alert.firing_start_time_since_last_resolved:
                return previous_alert.firing_start_time_since_last_resolved
        else:
            # if there is no previous alert, we need to check if the alert is firing
            if alert.status == AlertStatus.FIRING.value:
                return alert.last_received
            else:
                return None


def calculated_firing_counter(
    alert: AlertDto, previous_alert: AlertDto | list[AlertDto]
) -> int:
    """
    Calculate the firing counter of an alert based on the previous alert.

    Args:
        alert (AlertDto): The alert to calculate the firing counter for.
        previous_alert (AlertDto): The previous alert.

    Returns:
        int: The calculated firing counter.
    """
    # if its an acknowledged alert, the firing counter is 0

    if alert.status == AlertStatus.ACKNOWLEDGED.value:
        return 0

    # if this is the first alert, the firing counter is 1
    if not previous_alert:
        return 1
    elif isinstance(previous_alert, list):
        previous_alert = previous_alert[0]

    if previous_alert.status == AlertStatus.ACKNOWLEDGED.value:
        return 1

    # else, increment counter if the previous alert was firing
    # NOTE: firing_counter -> 0 only if acknowledged
    return previous_alert.firing_counter + 1


def calculated_unresolved_counter(
    alert: AlertDto, previous_alert: AlertDto | list[AlertDto]
) -> int:
    """
    Calculate the unresolved counter of an alert based on the previous alert.

    Args:
        alert (AlertDto): The alert to calculate the unresolved counter for.
        previous_alert (AlertDto): The previous alert.

    Returns:
        int: The calculated unresolved counter.
    """
    # if it's a resolved alert, the unresolved counter is 0
    if alert.status == AlertStatus.RESOLVED.value:
        return 0

    # if this is the first alert, the unresolved counter is 1
    if not previous_alert:
        return 1
    elif isinstance(previous_alert, list):
        previous_alert = previous_alert[0]

    if previous_alert.status == AlertStatus.RESOLVED.value:
        return 1

    # else, increment counter if the previous alert was firing
    # NOTE: unresolved_counter -> 0 only if resolved
    return previous_alert.unresolved_counter + 1


def convert_db_alerts_to_dto_alerts(
    alerts: list[Alert | tuple[Alert, LastAlertToIncident]],
    with_incidents: bool = False,
    with_alert_instance_enrichment: bool = False,
    session: Optional[Session] = None,
) -> list[AlertDto | AlertWithIncidentLinkMetadataDto]:
    """
    Enriches the alerts with the enrichment data.

    Args:
        alerts (list[Alert]): The alerts to enrich.
        with_incidents (bool): enrich with incidents data

    Returns:
        list[AlertDto | AlertWithIncidentLinkMetadataDto]: The enriched alerts.
    """
    # Phase 2: `with_alert_instance_enrichment` no longer has a destination —
    # per-occurrence enrichment snapshots are dropped; occurrences carry only
    # provider data. The parameter is retained for signature compatibility.
    with existed_or_new_session(session) as session:
        alerts_dto = []

        # Batch-load the LastAlert rows for all (tenant, fingerprint) pairs in
        # one query so we can source user enrichment state + relocated tracking
        # fields from the typed columns instead of the removed alert_enrichment
        # relationship.
        #
        # MUST scope by tenant_id: fingerprints are not globally unique across
        # tenants, so a fp-only `IN (...)` lookup would leak another tenant's
        # enrichment state into the DTO (multi-tenant isolation bug).
        keys = set()
        for _object in alerts:
            _alert = _object if isinstance(_object, Alert) else _object[0]
            if _alert.fingerprint:
                keys.add((_alert.tenant_id, _alert.fingerprint))
        last_alerts_by_key = {}
        if keys:
            tenant_ids = {tid for (tid, _) in keys}
            fps = {fp for (_, fp) in keys}
            for la in session.exec(
                select(LastAlert)
                .where(LastAlert.tenant_id.in_(tenant_ids))
                .where(LastAlert.fingerprint.in_(fps))
            ).all():
                last_alerts_by_key[(la.tenant_id, la.fingerprint)] = la

        with tracer.start_as_current_span("alerts_enrichment"):
            # enrich the alerts with the enrichment data
            for _object in alerts:
                # We may have an Alert only or and Alert with an LastAlertToIncident
                if isinstance(_object, Alert):
                    alert, alert_to_incident = _object, None
                else:
                    alert, alert_to_incident = _object

                last_alert = last_alerts_by_key.get(
                    (alert.tenant_id, alert.fingerprint)
                )
                enrichments = {}
                if last_alert is not None:
                    for _col in _LASTALERT_USER_COLUMNS:
                        _val = getattr(last_alert, _col, None)
                        if _val is not None:
                            # dismissed_until is a TIMESTAMPTZ column; coerce to
                            # the legacy canonical ISO string so AlertDto / JSON
                            # serialization stay well-typed instead of carrying a
                            # raw datetime (mirrors keep-api-gateway).
                            if _col == "dismissed_until" and isinstance(
                                _val, datetime
                            ):
                                _val = (
                                    _val.astimezone(timezone.utc).strftime(
                                        "%Y-%m-%dT%H:%M:%S.%f"
                                    )[:-3]
                                    + "Z"
                                )
                            enrichments[_col] = _val
                    # Derived backward-compat flag.
                    enrichments["dismissed"] = last_alert.status == "suppressed"

                alert_payload = alert.dict()
                # Ensure ID is a string for AlertDto
                alert_payload["id"] = str(alert.id) if alert.id else None
                # source is a list in AlertDto but a string in Alert (SQLModel)
                if alert_payload.get("source") and isinstance(alert_payload["source"], str):
                    alert_payload["source"] = [alert_payload["source"]]

                # Layer the relocated tracking fields from LastAlert onto the
                # payload (Alert no longer carries them).
                if last_alert is not None:
                    for _col in _LASTALERT_TRACKING_COLUMNS:
                        _val = getattr(last_alert, _col, None)
                        if _val is not None:
                            alert_payload[_col] = _val

                alert_payload.update(enrichments)

                if with_incidents:
                    if alert._incidents:
                        alert_payload["incident"] = ",".join(
                            str(incident.id) for incident in alert._incidents
                        )
                        alert_payload["incident_dto"] = [
                            IncidentDto.from_db_incident(incident)
                            for incident in alert._incidents
                        ]
                try:
                    if alert_to_incident is not None:
                        alert_dto = AlertWithIncidentLinkMetadataDto.from_db_instance(
                            alert, alert_to_incident, payload=alert_payload
                        )
                    else:
                        alert_dto = AlertDto(**alert_payload)

                    # Phase 2: `deleted` and `assignee` are read directly from the
                    # typed LastAlert columns (already merged into alert_payload);
                    # the legacy timestamp-list parsing is no longer needed.

                except Exception:
                    # should never happen but just in case
                    logger.exception(
                        "Failed to parse alert",
                        extra={
                            "alert": alert,
                        },
                    )
                    continue

                alert_dto.id = str(alert.id)

                # if the alert is acknowledged, the firing counter is 0
                if alert_dto.status == AlertStatus.ACKNOWLEDGED.value:
                    alert_dto.firing_counter = 0

                # if the alert is resolved, the unresolved counter is 0
                if alert_dto.status == AlertStatus.RESOLVED.value:
                    alert_dto.unresolved_counter = 0

                # always update provider id and type to the new values
                alert_dto.provider_id = alert.provider_id
                alert_dto.provider_type = alert.provider_type
                alerts_dto.append(alert_dto)
    return alerts_dto
