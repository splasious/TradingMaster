from datetime import datetime

from pydantic import BaseModel


class IndicatorSpecOut(BaseModel):
    code: str
    name: str
    category: str
    output_fields: list[str]
    default_params: dict[str, float]
    overlay: bool


class IndicatorPoint(BaseModel):
    ts: datetime
    values: dict[str, float | None]
