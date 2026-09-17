"""Common-availability computation across multiple named calendars.

Busy instances (non-CANCELLED, positive duration) produced by the
expander are clipped to the configured local working windows and merged
as half-open intervals [start, end) — touching segments coalesce. The
complement inside the working windows gives the common free slots.

Working windows are generated per local date and then converted to UTC,
so DST transitions shift the UTC bounds of a window while the local
wall-clock hours stay fixed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .expander import ExpansionError, expand_calendar

UTC = dt.timezone.utc

# Accepted working_hours keys -> datetime.weekday() numbers.
WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


@dataclass
class FreeSlot:
    start: dt.datetime  # aware UTC
    end: dt.datetime  # aware UTC

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def common_availability(
    calendars: list[tuple[str, str]],
    window_start: dt.datetime,
    window_end: dt.datetime,
    timezone: str,
    working_hours: dict[str, list[tuple[dt.time, dt.time]]],
    min_minutes: int,
) -> list[FreeSlot]:
    """Common free slots inside the working windows of [window_start, window_end).

    ``calendars`` is a list of (name, ics_text) pairs; ``working_hours``
    maps a weekday key (mon..sun) to local (start, end) periods. Only
    slots at least ``min_minutes`` long are returned, sorted by start.
    """
    if min_minutes <= 0:
        raise ExpansionError("min_minutes must be a positive integer")
    tz = _parse_timezone(timezone)
    periods = _validate_working_hours(working_hours)

    windows = _working_windows(window_start, window_end, tz, periods)
    if not windows:
        return []
    busy = _merged_busy(calendars, window_start, window_end, windows)

    minimum = dt.timedelta(minutes=min_minutes)
    return [
        FreeSlot(start, end)
        for start, end in _complement(windows, busy)
        if end - start >= minimum
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _parse_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ExpansionError(f"unknown timezone: {name!r}") from exc


def _validate_working_hours(
    working_hours: dict[str, list[tuple[dt.time, dt.time]]]
) -> dict[int, list[tuple[dt.time, dt.time]]]:
    """Map weekday keys to weekday numbers; reject inverted/overnight periods."""
    periods: dict[int, list[tuple[dt.time, dt.time]]] = {}
    for key, day_periods in working_hours.items():
        weekday = WEEKDAYS.get(key.strip().lower())
        if weekday is None:
            raise ExpansionError(
                f"unknown weekday {key!r}: expected one of " + ", ".join(WEEKDAYS)
            )
        for start, end in day_periods:
            if start >= end:
                raise ExpansionError(
                    f"working period {start.strftime('%H:%M')}-"
                    f"{end.strftime('%H:%M')} on {key!r} is inverted or crosses "
                    "midnight: start must be before end within the same local day"
                )
        periods.setdefault(weekday, []).extend(day_periods)
    return periods


# ---------------------------------------------------------------------------
# Working windows, busy intervals, complement
# ---------------------------------------------------------------------------

def _working_windows(
    window_start: dt.datetime,
    window_end: dt.datetime,
    tz: ZoneInfo,
    periods: dict[int, list[tuple[dt.time, dt.time]]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """UTC working windows overlapping [window_start, window_end), merged.

    Each period is anchored to its local date and converted to UTC, so a
    DST transition moves the UTC bounds, not the local wall-clock hours.
    """
    if not periods:
        return []
    # Pad by one local day on each side; the overlap check below discards
    # windows that do not actually intersect the query window.
    first = (window_start.astimezone(tz) - dt.timedelta(days=1)).date()
    last = (window_end.astimezone(tz) + dt.timedelta(days=1)).date()
    one_day = dt.timedelta(days=1)
    windows: list[tuple[dt.datetime, dt.datetime]] = []
    day = first
    while day <= last:
        for start_t, end_t in periods.get(day.weekday(), ()):
            start = dt.datetime.combine(day, start_t, tzinfo=tz).astimezone(UTC)
            end = dt.datetime.combine(day, end_t, tzinfo=tz).astimezone(UTC)
            if start < window_end and end > window_start:
                windows.append((max(start, window_start), min(end, window_end)))
        day += one_day
    return _merge(windows)


def _merged_busy(
    calendars: list[tuple[str, str]],
    window_start: dt.datetime,
    window_end: dt.datetime,
    windows: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """All busy instances clipped to the working windows, merged."""
    busy: list[tuple[dt.datetime, dt.datetime]] = []
    for _name, ics_text in calendars:
        for instance in expand_calendar(ics_text, window_start, window_end):
            if instance.status == "CANCELLED" or instance.end <= instance.start:
                continue
            for win_start, win_end in windows:
                start = max(instance.start, win_start)
                end = min(instance.end, win_end)
                if start < end:
                    busy.append((start, end))
    return _merge(busy)


def _complement(
    windows: list[tuple[dt.datetime, dt.datetime]],
    busy: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Sorted half-open intervals covered by ``windows`` but not ``busy``."""
    free: list[tuple[dt.datetime, dt.datetime]] = []
    for win_start, win_end in windows:
        cursor = win_start
        for busy_start, busy_end in busy:
            if busy_end <= cursor:
                continue
            if busy_start >= win_end:
                break
            if busy_start > cursor:
                free.append((cursor, busy_start))
            cursor = busy_end
            if cursor >= win_end:
                break
        if cursor < win_end:
            free.append((cursor, win_end))
    return free


def _merge(
    intervals: list[tuple[dt.datetime, dt.datetime]]
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Merge half-open [start, end) intervals; touching ends coalesce."""
    merged: list[list[dt.datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]
