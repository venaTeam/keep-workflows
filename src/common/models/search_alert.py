from pydantic import BaseModel, Extra, Field, validator

from src.common.models.db.preset import PresetSearchQuery


class SearchAlertsRequest(BaseModel):
    query: PresetSearchQuery = Field(..., alias="query")
    timeframe: int = Field(..., alias="timeframe")

    @validator("query")
    def validate_search_query(cls, value):
        if value.timeframe < 0:
            raise ValueError("Timeframe must be greater than or equal to 0.")
        return value

    class Config:
        extra = Extra.allow
