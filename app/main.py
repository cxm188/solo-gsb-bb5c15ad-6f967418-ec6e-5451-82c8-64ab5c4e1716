"""FastAPI surface for the iCalendar recurrence expansion service."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_serializer

from .availability import common_availability
from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc

app = FastAPI(
    title="iCalendar Recurrence Expansion API",
    version="1.1.0",
    summary="Expand DTSTART/RRULE/RDATE/EXDATE and RECURRENCE-ID overrides "
    "into concrete UTC instances within a query window, and find common "
    "free slots across multiple calendars.",
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


class NamedCalendar(BaseModel):
    name: str = Field(..., description="Calendar label (for identification)")
    ics: str = Field(..., description="iCalendar (RFC 5545) text for this calendar")


class WorkPeriod(BaseModel):
    start: dt.time = Field(..., description="Local period start (HH:MM)")
    end: dt.time = Field(
        ..., description="Local period end (HH:MM); after start, same local day"
    )


class AvailabilityRequest(BaseModel):
    calendars: list[NamedCalendar] = Field(
        ..., min_length=1, description="Named calendars to find common free time for"
    )
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )
    timezone: str = Field(
        ..., description="IANA timezone for the working hours, e.g. Europe/Berlin"
    )
    working_hours: dict[str, list[WorkPeriod]] = Field(
        ...,
        description="Local working periods per weekday (mon, tue, ..., sun); "
        "days not listed are non-working",
    )
    min_minutes: int = Field(
        ..., description="Minimum slot length in minutes (must be positive)"
    )


class FreeSlotOut(BaseModel):
    start: dt.datetime
    end: dt.datetime
    minutes: int

    @field_serializer("start", "end")
    def _serialize_dt(self, value: dt.datetime) -> str:
        return _iso_z(value)


class AvailabilityResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    timezone: str
    count: int
    slots: list[FreeSlotOut]

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


@app.post("/availability", response_model=AvailabilityResponse)
def availability(request: AvailabilityRequest) -> AvailabilityResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        slots = common_availability(
            calendars=[(c.name, c.ics) for c in request.calendars],
            window_start=window_start,
            window_end=window_end,
            timezone=request.timezone,
            working_hours={
                day: [(p.start, p.end) for p in periods]
                for day, periods in request.working_hours.items()
            },
            min_minutes=request.min_minutes,
        )
    except ExpansionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AvailabilityResponse(
        window_start=window_start,
        window_end=window_end,
        timezone=request.timezone,
        count=len(slots),
        slots=[
            FreeSlotOut(start=s.start, end=s.end, minutes=s.minutes) for s in slots
        ],
    )
