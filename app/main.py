"""FastAPI surface for the iCalendar recurrence expansion service."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_serializer

from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc

app = FastAPI(
    title="iCalendar Recurrence Expansion API",
    version="1.0.0",
    summary="Expand DTSTART/RRULE/RDATE/EXDATE and RECURRENCE-ID overrides "
    "into concrete UTC instances within a query window.",
)


def _to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # naive input is assumed to be UTC
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_z(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ExpandRequest(BaseModel):
    ics: str = Field(..., description="iCalendar (RFC 5545) text to expand")
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )


class InstanceOut(BaseModel):
    uid: str
    recurrence_id: dt.datetime | None
    summary: str | None
    start: dt.datetime
    end: dt.datetime
    status: str

    @field_serializer("recurrence_id", "start", "end")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _iso_z(value)


class ExpandResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    count: int
    instances: list[InstanceOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/expand", response_model=ExpandResponse)
def expand(request: ExpandRequest) -> ExpandResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        instances = expand_calendar(request.ics, window_start, window_end)
    except ExpansionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ExpandResponse(
        window_start=window_start,
        window_end=window_end,
        count=len(instances),
        instances=[
            InstanceOut(
                uid=i.uid,
                recurrence_id=i.recurrence_id,
                summary=i.summary,
                start=i.start,
                end=i.end,
                status=i.status,
            )
            for i in instances
        ],
    )
