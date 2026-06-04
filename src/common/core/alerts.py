import datetime
import json
import logging
import os
from typing import Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, text

from src.common.core.cel_to_sql.ast_nodes import DataType
from src.common.core.cel_to_sql.properties_metadata import (
    FieldMappingConfiguration,
    PropertiesMetadata,
    PropertyMetadataInfo,
)
from src.common.core.cel_to_sql.sql_providers.get_cel_to_sql_provider_for_dialect import (
    get_cel_to_sql_provider,
)
from src.common.core import db

# This import is required to create the tables
from src.common.core.facets import get_facet_options, get_facets
from src.common.models.alert import AlertSeverity, AlertStatus
from src.common.models.db.alert import (
    Alert,
    AlertField,
    Incident,
    LastAlert,
    LastAlertToIncident,
)
from src.common.models.db.facet import FacetType
from src.common.models.db.incident import IncidentStatus
from src.common.models.facet import FacetDto, FacetOptionDto, FacetOptionsQueryDto
from src.common.models.query import QueryDto, SortOptionsDto

logger = logging.getLogger(__name__)

alerts_hard_limit = int(os.environ.get("KEEP_LAST_ALERTS_LIMIT", 50000))

alert_field_configurations = [
    FieldMappingConfiguration(
        map_from_pattern="id", map_to="lastalert.alert_id", data_type=DataType.UUID
    ),
    FieldMappingConfiguration(
        map_from_pattern="source",
        map_to="alert.provider_type",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="providerId",
        map_to="alert.provider_id",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="providerType",
        map_to="alert.provider_type",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="timestamp",
        map_to="lastalert.timestamp",
        data_type=DataType.DATETIME,
    ),
    FieldMappingConfiguration(
        map_from_pattern="fingerprint",
        map_to="lastalert.fingerprint",
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="started_at",
        map_to="lastalert.first_timestamp",
        data_type=DataType.DATETIME,
    ),
    FieldMappingConfiguration(
        map_from_pattern="incident.id",
        map_to=[
            "incident.id",
        ],
        data_type=DataType.UUID,
    ),
    FieldMappingConfiguration(
        map_from_pattern="incident.is_visible",
        map_to=[
            "incident.is_visible",
        ],
        data_type=DataType.BOOLEAN,
    ),
    FieldMappingConfiguration(
        map_from_pattern="incident.name",
        map_to=[
            "incident.user_generated_name",
            "incident.ai_generated_name",
        ],
        data_type=DataType.STRING,
    ),
]

_INFRA_COLUMNS = {
    "id", "tenant_id", "timestamp", "provider_type", "provider_id",
    "fingerprint", "alert_hash"
}

# Retained for backward-compat with src/common/core/incidents.py, which imports
# these to build its own (incident-scoped) alert field configurations. The
# incident query path is out of scope for the alertenrichment removal and keeps its existing behavior
# (still reads alertenrichment JSONB).
_SPECIAL_FIELDS = {
    "severity": {
        "data_type": DataType.STRING,
        "enum_values": [
            severity.value
            for severity in sorted(
                [severity for _, severity in enumerate(AlertSeverity)],
                key=lambda s: s.order,
            )
        ],
    },
    "status": {
        "data_type": DataType.STRING,
        "enum_values": list(reversed([item.value for _, item in enumerate(AlertStatus)])),
    },
    "last_received": {"data_type": DataType.DATETIME},
    "dismissed": {"data_type": DataType.BOOLEAN},
    "firing_counter": {"data_type": DataType.INTEGER},
    "unresolved_counter": {"data_type": DataType.INTEGER},
}

# === strict schema (mirrors keep-api-gateway/src/repositories/alerts.py) ===
# User-enrichment state + relocated tracking fields now live as typed columns on
# LastAlert (no more alertenrichment JSONB extraction for ALERTS). These are
# mapped explicitly here and EXCLUDED from the generic Alert-column loop below.
#   - status: user override (lastalert.status) coalesced with the provider value
#     (alert.status).
#   - severity: immutable provider value on alert.
#   - assignee/note/dismiss_mode/dismissed_until/deleted: typed lastalert columns.
#   - dismissed: derived boolean (lastalert.status == 'suppressed').
#   - tracking fields (last_received/firing_counter/...): relocated to lastalert.
_STRICT_SCHEMA_FIELD_CONFIGS = [
    FieldMappingConfiguration(
        map_from_pattern="status",
        map_to=["lastalert.status", "alert.status"],
        data_type=DataType.STRING,
        enum_values=list(
            reversed([item.value for _, item in enumerate(AlertStatus)])
        ),
    ),
    FieldMappingConfiguration(
        map_from_pattern="severity",
        map_to=["alert.severity"],
        data_type=DataType.STRING,
        enum_values=[
            severity.value
            for severity in sorted(
                [severity for _, severity in enumerate(AlertSeverity)],
                key=lambda s: s.order,
            )
        ],
    ),
    FieldMappingConfiguration(
        map_from_pattern="assignee",
        map_to=["lastalert.assignee"],
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="note",
        map_to=["lastalert.note"],
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="dismiss_mode",
        map_to=["lastalert.dismiss_mode"],
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="dismissed_until",
        map_to=["lastalert.dismissed_until"],
        data_type=DataType.DATETIME,
    ),
    FieldMappingConfiguration(
        map_from_pattern="deleted",
        map_to=["lastalert.deleted"],
        data_type=DataType.BOOLEAN,
    ),
    FieldMappingConfiguration(
        map_from_pattern="dismissed",
        map_to=[
            "CASE WHEN lastalert.status = 'suppressed' THEN 'true' ELSE 'false' END"
        ],
        data_type=DataType.BOOLEAN,
    ),
    # relocated system-tracking fields
    FieldMappingConfiguration(
        map_from_pattern="last_received",
        map_to=["lastalert.last_received"],
        data_type=DataType.DATETIME,
    ),
    FieldMappingConfiguration(
        map_from_pattern="firing_counter",
        map_to=["lastalert.firing_counter"],
        data_type=DataType.INTEGER,
    ),
    FieldMappingConfiguration(
        map_from_pattern="unresolved_counter",
        map_to=["lastalert.unresolved_counter"],
        data_type=DataType.INTEGER,
    ),
    FieldMappingConfiguration(
        map_from_pattern="firing_start_time",
        map_to=["lastalert.firing_start_time"],
        data_type=DataType.STRING,
    ),
    FieldMappingConfiguration(
        map_from_pattern="firing_start_time_since_last_resolved",
        map_to=["lastalert.firing_start_time_since_last_resolved"],
        data_type=DataType.STRING,
    ),
]
alert_field_configurations.extend(_STRICT_SCHEMA_FIELD_CONFIGS)

# Fields handled explicitly above — skip them in the generic loop so we
# don't shadow the LastAlert-backed mappings with a plain alert.* mapping. Note
# `started_at` stays mapped to lastalert.first_timestamp (declared at the top) and
# must not be overridden by the relocated lastalert.started_at string column.
_STRICT_SCHEMA_HANDLED_FIELDS = {cfg.map_from_pattern for cfg in _STRICT_SCHEMA_FIELD_CONFIGS}
_STRICT_SCHEMA_HANDLED_FIELDS.add("started_at")

for col in Alert.__table__.columns:
    if col.name in _INFRA_COLUMNS:
        continue
    if col.name in _STRICT_SCHEMA_HANDLED_FIELDS:
        continue
    alert_field_configurations.append(
        FieldMappingConfiguration(
            map_from_pattern=col.name,
            map_to=[
                f"alert.{col.name}",
            ],
            data_type=DataType.STRING,
        )
    )

# Strict schema — the catch-all `*` → alertenrichment JSON mapping is
# removed. Alert user-state lives on typed LastAlert columns; arbitrary unknown
# fields are no longer routed to the (no-longer-written) JSONB column.

# Copies the same configuration as above, but adds the "alert." prefix to each entry in map_from_pattern.
# This allows users to write queries using dictionary-style field access, like:
#   alert['some_attribute'] == 'value'
field_configurations_with_alert_prefix = []
for item in alert_field_configurations:
    field_configurations_with_alert_prefix.append(
        FieldMappingConfiguration(
            map_from_pattern=f"alert.{item.map_from_pattern}",
            map_to=item.map_to,
            data_type=item.data_type,
            enum_values=item.enum_values,
        )
    )
alert_field_configurations = (
    field_configurations_with_alert_prefix + alert_field_configurations
)

properties_metadata = PropertiesMetadata(alert_field_configurations)

static_facets = [
    FacetDto(
        id="f8a91ac7-4916-4ad0-9b46-a5ddb85bfbb8",
        property_path="severity",
        name="Severity",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="5dd1519c-6277-4109-ad95-c19d2f4f15e3",
        property_path="status",
        name="Status",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="461bef05-fc20-4363-b427-9d26fe064e7f",
        property_path="source",
        name="Source",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="6afa12d7-21df-4694-8566-fd56d5ee2266",
        property_path="incident.name",
        name="Incident",
        is_static=True,
        type=FacetType.str,
    ),
    FacetDto(
        id="77b8a6d4-3b8d-4b6a-9f8e-2c8e4b8f8e4c",
        property_path="dismissed",
        name="Dismissed",
        is_static=True,
        type=FacetType.str,
    ),
]
static_facets_dict = {facet.id: facet for facet in static_facets}


def get_threeshold_query(tenant_id: str):
    return func.coalesce(
        select(LastAlert.timestamp)
        .select_from(LastAlert)
        .where(LastAlert.tenant_id == tenant_id)
        .order_by(LastAlert.timestamp.desc())
        .limit(1)
        .offset(alerts_hard_limit - 1)
        .scalar_subquery(),
        datetime.datetime.min,
    )


def __build_query_for_filtering(
    tenant_id: str,
    select_args: list,
    cel=None,
    limit=None,
    fetch_alerts_data=True,
    fetch_incidents=False,
    force_fetch=False,
):
    fetch_incidents = fetch_incidents or (cel and "incident." in cel)
    cel_to_sql_instance = get_cel_to_sql_provider(properties_metadata)
    sql_filter = None
    involved_fields = []

    if cel:
        cel_to_sql_result = cel_to_sql_instance.convert_to_sql_str_v2(cel)
        sql_filter = cel_to_sql_result.sql
        involved_fields = cel_to_sql_result.involved_fields
        fetch_incidents = next(
            (
                True
                for field in involved_fields
                if field.field_name.startswith("incident.")
            ),
            False,
        )

    sql_query = select(*select_args).select_from(LastAlert)

    if fetch_alerts_data or force_fetch:
        # No more alertenrichment JOIN — user state lives on LastAlert
        # typed columns (already the FROM table here).
        sql_query = sql_query.join(
            Alert,
            and_(
                Alert.id == LastAlert.alert_id, Alert.tenant_id == LastAlert.tenant_id
            ),
        )

    if fetch_incidents or force_fetch:
        # Fingerprint with active incidents subquery, i.e  in Firing status
        firing_subq = (
            select(LastAlert.fingerprint)
            .join(
                LastAlertToIncident,
                LastAlert.fingerprint == LastAlertToIncident.fingerprint,
            )
            .join(Incident, LastAlertToIncident.incident_id == Incident.id)
            .where(Incident.status == IncidentStatus.FIRING.value)
            .distinct()
        ).subquery()

        sql_query = sql_query.outerjoin(
            LastAlertToIncident,
            and_(
                LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                LastAlert.fingerprint == LastAlertToIncident.fingerprint,
            ),
        ).outerjoin(
            Incident,
            and_(
                LastAlertToIncident.tenant_id == Incident.tenant_id,
                LastAlertToIncident.incident_id == Incident.id,
                LastAlert.fingerprint.in_(select(firing_subq.c.fingerprint)),
            ),
        )

    sql_query = sql_query.filter(LastAlert.tenant_id == tenant_id).filter(
        LastAlert.timestamp >= get_threeshold_query(tenant_id)
    )
    involved_fields = []

    if sql_filter:
        sql_query = sql_query.where(text(sql_filter))
    return {
        "query": sql_query,
        "involved_fields": involved_fields,
        "fetch_incidents": fetch_incidents,
    }


def build_total_alerts_query(tenant_id, query: QueryDto):
    fetch_incidents = query.cel and "incident." in query.cel
    fetch_alerts_data = query.cel is not None or query.cel != ""

    count_funct = (
        func.count(func.distinct(LastAlert.alert_id))
        if fetch_incidents
        else func.count(1)
    )
    built_query_result = __build_query_for_filtering(
        tenant_id=tenant_id,
        cel=query.cel,
        select_args=[count_funct],
        limit=query.limit,
        fetch_alerts_data=fetch_alerts_data,
    )

    return built_query_result["query"]


def build_alerts_query(tenant_id, query: QueryDto):
    cel_to_sql_instance = get_cel_to_sql_provider(properties_metadata)
    sort_by_exp = cel_to_sql_instance.get_order_by_expression(
        [
            (sort_option.sort_by, sort_option.sort_dir)
            for sort_option in query.sort_options
        ]
    )
    distinct_columns = [
        text(cel_to_sql_instance.get_field_expression(sort_option.sort_by))
        for sort_option in query.sort_options
    ]

    built_query_result = __build_query_for_filtering(
        tenant_id,
        select_args=[
            Alert,
            LastAlert,
            LastAlert.first_timestamp.label("started_at"),
        ]
        + distinct_columns,
        cel=query.cel,
    )
    sql_query = built_query_result["query"]
    fetch_incidents = built_query_result["fetch_incidents"]
    sql_query = sql_query.order_by(text(sort_by_exp))

    if fetch_incidents:
        sql_query = sql_query.distinct(*(distinct_columns + [Alert.id]))

    if query.limit is not None:
        sql_query = sql_query.limit(query.limit)

    if query.offset is not None:
        sql_query = sql_query.offset(query.offset)

    return sql_query


def query_last_alerts(tenant_id, query: QueryDto) -> Tuple[list[Alert], int]:
    query_with_defaults = query.copy()

    # Shahar: this happens when the frontend query builder fails to build a query
    if query_with_defaults.cel == "1 == 1":
        logger.warning("Failed to build query for alerts")
        query_with_defaults.cel = ""
    if query_with_defaults.limit is None:
        query_with_defaults.limit = 1000
    if query_with_defaults.offset is None:
        query_with_defaults.offset = 0
    if query_with_defaults.sort_by is not None:
        query_with_defaults.sort_options = [
            SortOptionsDto(
                sort_by=query_with_defaults.sort_by,
                sort_dir=query_with_defaults.sort_dir,
            )
        ]
    if not query_with_defaults.sort_options:
        query_with_defaults.sort_options = [
            SortOptionsDto(sort_by="timestamp", sort_dir="desc")
        ]

    with Session(db.engine) as session:
        try:
            total_count_query = build_total_alerts_query(
                tenant_id=tenant_id, query=query_with_defaults
            )
            total_count = session.exec(total_count_query).one()[0]

            if not query_with_defaults.limit:
                return [], total_count

            if query_with_defaults.offset >= alerts_hard_limit:
                return [], total_count

            if (
                query_with_defaults.offset + query_with_defaults.limit
                > alerts_hard_limit
            ):
                query_with_defaults.limit = (
                    alerts_hard_limit - query_with_defaults.offset
                )

            data_query = build_alerts_query(tenant_id, query_with_defaults)
            alerts_with_start = session.execute(data_query).all()
        except OperationalError as e:
            logger.warning(
                f"Failed to query alerts for query object '{json.dumps(query_with_defaults.dict(exclude_unset=True))}': {e}"
            )
            return [], 0

        # Process results based on dialect
        # alert_data = (Alert, LastAlert, started_at). The CEL field
        # config + query builders now map alert user-state (status/assignee/note/
        # dismiss_mode/dismissed_until/deleted) and the relocated tracking fields
        # to typed LastAlert columns — the alertenrichment JOIN and the catch-all
        # `*` JSON mapping have been removed (mirrors keep-api-gateway). The DTO
        # builder (convert_db_alerts_to_dto_alerts) re-fetches the LastAlert row
        # by fingerprint, so we only carry the Alert forward here.
        alerts = []
        for alert_data in alerts_with_start:
            alert: Alert = alert_data[0]
            alerts.append(alert)

        return alerts, total_count


def get_alert_facets_data(
    tenant_id: str,
    facet_options_query: FacetOptionsQueryDto,
) -> dict[str, list[FacetOptionDto]]:
    if facet_options_query and facet_options_query.facet_queries:
        facets = get_alert_facets(tenant_id, facet_options_query.facet_queries.keys())
    else:
        facets = static_facets

    def base_query_factory(
        facet_property_path: str,
        involved_fields: PropertyMetadataInfo,
        select_statement,
    ):
        fetch_incidents = "incident." in facet_property_path or next(
            (True for item in involved_fields if "incident." in item.field_name),
            False,
        )
        return __build_query_for_filtering(
            tenant_id=tenant_id,
            select_args=select_statement,
            force_fetch=False,
            fetch_incidents=fetch_incidents,
        )["query"]

    return get_facet_options(
        base_query_factory=base_query_factory,
        entity_id_column=LastAlert.alert_id,
        facets=facets,
        facet_options_query=facet_options_query,
        properties_metadata=properties_metadata,
    )


def get_alert_facets(
    tenant_id: str, facet_ids_to_load: list[str] = None
) -> list[FacetDto]:
    not_static_facet_ids = []
    facets = []

    if not facet_ids_to_load:
        return static_facets + get_facets(tenant_id, "alert")

    if facet_ids_to_load:
        for facet_id in facet_ids_to_load:
            if facet_id not in static_facets_dict:
                not_static_facet_ids.append(facet_id)
                continue

            facets.append(static_facets_dict[facet_id])

    if not_static_facet_ids:
        facets += get_facets(tenant_id, "alert", not_static_facet_ids)

    return facets


def get_alert_potential_facet_fields(tenant_id: str) -> list[str]:
    with Session(db.engine) as session:
        query = (
            select(AlertField.field_name)
            .select_from(AlertField)
            .where(AlertField.tenant_id == tenant_id)
            .distinct(AlertField.field_name)
        )
        result = session.exec(query).all()
        return [row[0] for row in result]
