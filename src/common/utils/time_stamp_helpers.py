import json
from typing import Optional

from fastapi import HTTPException, Query

from src.common.models.time_stamp import TimeStampFilter


def get_time_stamp_filter(time_stamp: Optional[str] = Query(None)) -> TimeStampFilter:
    if time_stamp:
        try:
            # Parse the JSON string
            time_stamp_dict = json.loads(time_stamp)
            # Return the TimeStampFilter object, Pydantic will map 'from' -> lower_timestamp and 'to' -> upper_timestamp
            return TimeStampFilter(**time_stamp_dict)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid time_stamp format")
    return TimeStampFilter()
