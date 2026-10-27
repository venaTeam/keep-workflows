from typing import Any

from pydantic import BaseModel

from src.common.models.alert import AlertDto, AlertWithIncidentLinkMetadataDto
from src.common.models.db.enrichment_event import EnrichmentEvent
from src.common.models.db.workflow import *  # pylint: disable=unused-wildcard-importfrom typing import Optional
from src.common.models.incident import IncidentDto
from src.common.models.workflow import WorkflowDTO, WorkflowExecutionDTO


class PaginatedResultsDto(BaseModel):
    limit: int = 25
    offset: int = 0
    count: int
    items: list[Any]


class IncidentsPaginatedResultsDto(PaginatedResultsDto):
    items: list[IncidentDto]


class AlertPaginatedResultsDto(PaginatedResultsDto):
    items: list[AlertDto]


class EnrichmentEventPaginatedResultsDto(PaginatedResultsDto):
    items: list[EnrichmentEvent]


class AlertWithIncidentLinkMetadataPaginatedResultsDto(PaginatedResultsDto):
    items: list[AlertWithIncidentLinkMetadataDto]


class WorkflowExecutionsPaginatedResultsDto(PaginatedResultsDto):
    items: list[WorkflowExecutionDTO]
    passCount: int = 0
    avgDuration: float = 0.0
    workflow: Optional[WorkflowDTO] = None
    failCount: int = 0
