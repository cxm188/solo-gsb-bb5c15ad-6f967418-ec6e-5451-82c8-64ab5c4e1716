"""Key tests for the /availability endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//expand-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def named(name: str, ics: str) -> dict:
    return {"name": name, "ics": ics}


def avail(
    calendars,
    window_start="2026-01-05T00:00:00Z",  # Monday
    window_end="2026-01-06T00:00:00Z",
    timezone="UTC",
    working_hours=None,
    min_minutes=30,
):
    if working_hours is None:
        working_hours = {"mon": [{"start": "09:00", "end": "17:00"}]}
    return client.post(
        "/availability",
        json={
            "calendars": calendars,
            "window_start": window_start,
            "window_end": window_end,
            "timezone": timezone,
            "working_hours": working_hours,
            "min_minutes": min_minutes,
        },
    )


def slots(response) -> list[dict]:
    return response.json()["slots"]


# ---------------------------------------------------------------------------
# Common free slots: clip to working hours, merge busy, complement
# ---------------------------------------------------------------------------

def test_common_free_slots_between_two_calendars():
    alice = cal(event("UID:a1", "DTSTART:20260105T090000Z", "DTEND:20260105T103000Z"))
    bob = cal(event("UID:b1", "DTSTART:20260105T120000Z", "DTEND:20260105T130000Z"))
    resp = avail([named("alice", alice), named("bob", bob)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["timezone"] == "UTC"
    assert body["window_start"] == "2026-01-05T00:00:00Z"
    assert body["window_end"] == "2026-01-06T00:00:00Z"
    assert slots(resp) == [
        {"start": "2026-01-05T10:30:00Z", "end": "2026-01-05T12:00:00Z", "minutes": 90},
        {"start": "2026-01-05T13:00:00Z", "end": "2026-01-05T17:00:00Z", "minutes": 240},
    ]


def test_adjacent_busy_segments_merge():
    # Back-to-back meetings across calendars form one busy block.
    alice = cal(event("UID:a1", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"))
    bob = cal(event("UID:b1", "DTSTART:20260105T100000Z", "DTEND:20260105T110000Z"))
    resp = avail([named("alice", alice), named("bob", bob)])
    assert slots(resp) == [
        {"start": "2026-01-05T11:00:00Z", "end": "2026-01-05T17:00:00Z", "minutes": 360},
    ]


def test_busy_is_clipped_to_working_hours():
    alice = cal(event("UID:a1", "DTSTART:20260105T060000Z", "DTEND:20260105T100000Z"))
    bob = cal(event("UID:b1", "DTSTART:20260105T160000Z", "DTEND:20260105T230000Z"))
    resp = avail([named("alice", alice), named("bob", bob)])
    assert slots(resp) == [
        {"start": "2026-01-05T10:00:00Z", "end": "2026-01-05T16:00:00Z", "minutes": 360},
    ]


def test_cancelled_and_zero_duration_events_are_not_busy():
    alice = cal(event("UID:a1", "DTSTART:20260105T100000Z", "DTEND:20260105T100000Z"))
    bob = cal(event(
        "UID:b1",
        "DTSTART:20260105T110000Z",
        "DTEND:20260105T120000Z",
        "STATUS:CANCELLED",
    ))
    resp = avail([named("alice", alice), named("bob", bob)])
    assert slots(resp) == [
        {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T17:00:00Z", "minutes": 480},
    ]


def test_recurring_instances_and_cancelled_override():
    alice = cal(
        event(
            "UID:a1",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=2",  # Mon Jan 5 + Tue Jan 6
        ),
        event(
            "UID:a1",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T090000Z",
            "DTEND:20260106T100000Z",
            "STATUS:CANCELLED",
        ),
    )
    resp = avail(
        [named("alice", alice)],
        window_end="2026-01-07T00:00:00Z",
        working_hours={
            "mon": [{"start": "09:00", "end": "17:00"}],
            "tue": [{"start": "09:00", "end": "17:00"}],
        },
    )
    assert slots(resp) == [
        {"start": "2026-01-05T10:00:00Z", "end": "2026-01-05T17:00:00Z", "minutes": 420},
        {"start": "2026-01-06T09:00:00Z", "end": "2026-01-06T17:00:00Z", "minutes": 480},
    ]


def test_min_minutes_filters_short_slots():
    alice = cal(event("UID:a1", "DTSTART:20260105T090000Z", "DTEND:20260105T103000Z"))
    bob = cal(event("UID:b1", "DTSTART:20260105T120000Z", "DTEND:20260105T130000Z"))
    calendars = [named("alice", alice), named("bob", bob)]
    # The 90-minute slot meets a 90-minute minimum ...
    assert len(slots(avail(calendars, min_minutes=90))) == 2
    # ... but not a 91-minute one.
    resp = avail(calendars, min_minutes=91)
    assert slots(resp) == [
        {"start": "2026-01-05T13:00:00Z", "end": "2026-01-05T17:00:00Z", "minutes": 240},
    ]


# ---------------------------------------------------------------------------
# Working windows: per local date, multiple periods, DST
# ---------------------------------------------------------------------------

def test_multiple_periods_per_day():
    resp = avail(
        [named("alice", cal())],
        working_hours={
            "mon": [
                {"start": "09:00", "end": "12:00"},
                {"start": "13:00", "end": "18:00"},
            ]
        },
    )
    assert slots(resp) == [
        {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T12:00:00Z", "minutes": 180},
        {"start": "2026-01-05T13:00:00Z", "end": "2026-01-05T18:00:00Z", "minutes": 300},
    ]


def test_days_without_working_hours_have_no_slots():
    resp = avail(
        [named("alice", cal())],
        window_start="2026-01-03T00:00:00Z",  # Saturday
        window_end="2026-01-05T00:00:00Z",  # Monday 00:00
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert slots(resp) == []


def test_slots_are_clipped_to_the_query_window():
    resp = avail(
        [named("alice", cal())],
        window_start="2026-01-05T12:00:00Z",
        window_end="2026-01-05T15:00:00Z",
    )
    assert slots(resp) == [
        {"start": "2026-01-05T12:00:00Z", "end": "2026-01-05T15:00:00Z", "minutes": 180},
    ]


def test_naive_window_is_interpreted_as_utc():
    resp = avail(
        [named("alice", cal())],
        window_start="2026-01-05T12:00:00",  # no Z suffix
        window_end="2026-01-05T15:00:00",
    )
    assert resp.status_code == 200
    assert resp.json()["window_start"] == "2026-01-05T12:00:00Z"
    assert resp.json()["window_end"] == "2026-01-05T15:00:00Z"


def test_working_windows_follow_local_time_across_dst():
    # America/New_York springs forward 2026-03-08: EST (UTC-5) -> EDT (UTC-4).
    resp = avail(
        [named("alice", cal())],
        window_start="2026-03-06T00:00:00Z",
        window_end="2026-03-10T00:00:00Z",
        timezone="America/New_York",
        working_hours={
            "fri": [{"start": "09:00", "end": "17:00"}],
            "mon": [{"start": "09:00", "end": "17:00"}],
        },
    )
    assert resp.status_code == 200
    assert slots(resp) == [
        # Fri Mar 6, 09:00-17:00 EST
        {"start": "2026-03-06T14:00:00Z", "end": "2026-03-06T22:00:00Z", "minutes": 480},
        # Mon Mar 9, 09:00-17:00 EDT
        {"start": "2026-03-09T13:00:00Z", "end": "2026-03-09T21:00:00Z", "minutes": 480},
    ]


# ---------------------------------------------------------------------------
# Error handling: 422 with clear messages
# ---------------------------------------------------------------------------

def test_unknown_timezone_returns_422():
    resp = avail([named("alice", cal())], timezone="Mars/Olympus_Mons")
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]
    assert "Mars/Olympus_Mons" in resp.json()["detail"]


def test_inverted_working_period_returns_422():
    resp = avail(
        [named("alice", cal())],
        working_hours={"mon": [{"start": "17:00", "end": "09:00"}]},
    )
    assert resp.status_code == 422
    assert "inverted" in resp.json()["detail"]


def test_overnight_working_period_returns_422():
    resp = avail(
        [named("alice", cal())],
        working_hours={"mon": [{"start": "22:00", "end": "02:00"}]},
    )
    assert resp.status_code == 422
    assert "midnight" in resp.json()["detail"]


def test_non_positive_min_minutes_returns_422():
    for value in (0, -30):
        resp = avail([named("alice", cal())], min_minutes=value)
        assert resp.status_code == 422
        assert "min_minutes" in resp.json()["detail"]


def test_unknown_weekday_key_returns_422():
    resp = avail(
        [named("alice", cal())],
        working_hours={"funday": [{"start": "09:00", "end": "17:00"}]},
    )
    assert resp.status_code == 422
    assert "funday" in resp.json()["detail"]


def test_invalid_window_returns_422():
    resp = avail(
        [named("alice", cal())],
        window_start="2026-01-06T00:00:00Z",
        window_end="2026-01-05T00:00:00Z",
    )
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_invalid_ics_returns_422():
    resp = avail([named("alice", "this is not an iCalendar document")])
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]


def test_unknown_tzid_inside_calendar_returns_422():
    ics = cal(event(
        "UID:a1",
        "DTSTART;TZID=Mars/Olympus_Mons:20260105T090000",
        "DTEND;TZID=Mars/Olympus_Mons:20260105T100000",
    ))
    resp = avail([named("alice", ics)])
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_empty_calendars_list_returns_422():
    resp = avail([])
    assert resp.status_code == 422
