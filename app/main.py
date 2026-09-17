"""FastAPI surface for the iCalendar expansion and availability service."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_serializer, model_validator

from .availability import (
    WEEKDAY_NAMES,
    AvailabilityError,
    LocalRange,
    build_working_periods,
    busy_intervals,
    common_free_slots,
)
from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc

app = FastAPI(
    title="iCalendar Recurrence Expansion and Availability API",
    version="1.1.0",
    summary="Expand DTSTART/RRULE/RDATE/EXDATE and RECURRENCE-ID overrides "
    "into concrete UTC instances, and intersect named calendars with "
    "local working hours to find common free time.",
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


# ---------------------------------------------------------------------------
# POST /availability
# ---------------------------------------------------------------------------


class WorkRange(BaseModel):
    """One local working period within a day (wall-clock times, no zone)."""

    model_config = {"extra": "forbid"}

    start: dt.time = Field(
        ..., description="Local start time, e.g. '09:00' (no timezone offset)"
    )
    end: dt.time = Field(
        ..., description="Local end time, same local date as start"
    )

    @model_validator(mode="after")
    def _validate_range(self) -> "WorkRange":
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("working hours must be naive local times (no offset)")
        if self.start >= self.end:
            raise ValueError(
                "working period end must be after start on the same local date "
                "(overnight ranges are not supported)"
            )
        return self


class WorkingHours(BaseModel):
    """Per-weekday working periods; a missing weekday means no work that day."""

    model_config = {"extra": "forbid"}

    monday: list[WorkRange] = Field(default_factory=list)
    tuesday: list[WorkRange] = Field(default_factory=list)
    wednesday: list[WorkRange] = Field(default_factory=list)
    thursday: list[WorkRange] = Field(default_factory=list)
    friday: list[WorkRange] = Field(default_factory=list)
    saturday: list[WorkRange] = Field(default_factory=list)
    sunday: list[WorkRange] = Field(default_factory=list)


class AvailabilityRequest(BaseModel):
    calendars: dict[str, str] = Field(
        default_factory=dict,
        description="Named iCalendar texts; free time must avoid events in all "
        "of them. May be empty.",
    )
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )
    timezone: str = Field(
        ..., description="IANA timezone for the working hours, e.g. 'Europe/Berlin'"
    )
    working_hours: WorkingHours
    minimum_minutes: int = Field(
        ..., gt=0, description="Only free slots at least this long are returned"
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

    ranges_by_weekday: dict[int, list[LocalRange]] = {
        idx: [LocalRange(start=r.start, end=r.end) for r in getattr(request.working_hours, name)]
        for idx, name in enumerate(WEEKDAY_NAMES)
    }

    try:
        working = build_working_periods(
            window_start, window_end, request.timezone, ranges_by_weekday
        )
        busy = busy_intervals(
            request.calendars, window_start, window_end, working
        )
        slots = common_free_slots(
            busy, working, dt.timedelta(minutes=request.minimum_minutes)
        )
    except AvailabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return AvailabilityResponse(
        window_start=window_start,
        window_end=window_end,
        timezone=request.timezone,
        count=len(slots),
        slots=[
            FreeSlotOut(
                start=start,
                end=end,
                minutes=int((end - start).total_seconds() // 60),
            )
            for start, end in slots
        ],
    )
