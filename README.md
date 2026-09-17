# iCalendar Recurrence Expansion API

A local HTTP API that expands iCalendar (RFC 5545) recurring events into
concrete UTC instances inside a query window, and finds common free slots
across multiple calendars.

- Expands `DTSTART` + `RRULE` + `RDATE` − `EXDATE`
- Merges overrides by `UID` + `RECURRENCE-ID`: an override can **reschedule**
  an instance (judged against the window by its *actual* new start/end; the
  original slot is never kept) or **cancel** it via `STATUS:CANCELLED`
- Interprets `TZID` as IANA timezones with correct DST handling
- Computes **common availability** across any number of named calendars,
  clipped to per-weekday local working hours that follow DST
- Results sorted by UTC start time, then `UID` (stable)
- Clear `422` errors for invalid rules, unknown timezones, or bad input

Stack: Python · FastAPI · icalendar · python-dateutil · zoneinfo

## Quick start (Docker Compose)

```bash
docker compose up --build
```

The API listens on **http://localhost:8000** (interactive docs at `/docs`,
health check at `/health`).

## API

### `POST /expand`

Request body:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

An instance is returned when it **overlaps** `[window_start, window_end)`
(a zero-duration event must start inside the window). Each instance carries
`uid`, `recurrence_id` (UTC, `null` for non-recurring events), `summary`,
`start`, `end`, and `status`; all timestamps are UTC (`Z`).

### Example

```bash
curl -s http://localhost:8000/expand \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART;TZID=America/New_York:20260301T090000\r\nDTEND;TZID=America/New_York:20260301T100000\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID=America/New_York:20260308T090000\r\nSUMMARY:Weekly sync\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nRECURRENCE-ID;TZID=America/New_York:20260315T090000\r\nDTSTART;TZID=America/New_York:20260316T110000\r\nDTEND;TZID=America/New_York:20260316T120000\r\nSUMMARY:Weekly sync (moved)\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-02-25T00:00:00Z",
    "window_end": "2026-04-01T00:00:00Z"
  }'
```

Response — note the DST shift (14:00Z → 13:00Z), the `EXDATE`-removed
March 8 occurrence, and the March 15 instance replaced by its override at
the new time (the original slot is gone):

```json
{
  "window_start": "2026-02-25T00:00:00Z",
  "window_end": "2026-04-01T00:00:00Z",
  "count": 2,
  "instances": [
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-01T14:00:00Z",
      "summary": "Weekly sync",
      "start": "2026-03-01T14:00:00Z",
      "end": "2026-03-01T15:00:00Z",
      "status": "CONFIRMED"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-15T13:00:00Z",
      "summary": "Weekly sync (moved)",
      "start": "2026-03-16T15:00:00Z",
      "end": "2026-03-16T16:00:00Z",
      "status": "CONFIRMED"
    }
  ]
}
```

### `POST /availability`

Finds the common free slots across several named calendars, inside the
configured local working hours.

Request body:

| Field           | Type   | Description                                                        |
| --------------- | ------ | ------------------------------------------------------------------ |
| `calendars`     | array  | `{name, ics}` objects (at least one)                               |
| `window_start`  | string | ISO 8601 timestamp, converted to UTC (naive = UTC)                 |
| `window_end`    | string | ISO 8601 timestamp, converted to UTC (naive = UTC)                 |
| `timezone`      | string | IANA timezone for the working hours, e.g. `Europe/Berlin`          |
| `working_hours` | object | Weekday key (`mon`…`sun`) → list of `{start, end}` local `HH:MM` periods; unlisted days are non-working |
| `min_minutes`   | number | Minimum slot length in minutes (must be positive)                  |

Every calendar is expanded with the same logic as `/expand`. All
non-`CANCELLED` instances with positive duration are clipped to the working
windows and merged as half-open intervals `[start, end)` — adjacent busy
segments coalesce — and the complement inside the working windows gives the
common free slots. Working windows are generated **per local date** and then
converted to UTC, so a DST transition shifts the UTC bounds of a window
while the local wall-clock hours stay fixed. Only slots of at least
`min_minutes` are returned, sorted by start; each slot carries `start`,
`end` (UTC, `Z` format) and `minutes`.

### Example

```bash
curl -s http://localhost:8000/availability \
  -H 'Content-Type: application/json' \
  -d '{
    "calendars": [
      {"name": "alice", "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:alice-1\r\nDTSTART;TZID=Europe/Berlin:20260105T090000\r\nDTEND;TZID=Europe/Berlin:20260105T100000\r\nSUMMARY:Alice standup\r\nEND:VEVENT\r\nEND:VCALENDAR"},
      {"name": "bob", "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:bob-1\r\nDTSTART;TZID=Europe/Berlin:20260105T120000\r\nDTEND;TZID=Europe/Berlin:20260105T130000\r\nSUMMARY:Bob focus block\r\nEND:VEVENT\r\nEND:VCALENDAR"}
    ],
    "window_start": "2026-01-05T00:00:00Z",
    "window_end": "2026-01-06T00:00:00Z",
    "timezone": "Europe/Berlin",
    "working_hours": {"mon": [{"start": "09:00", "end": "17:00"}]},
    "min_minutes": 30
  }'
```

Response — the working window is 09:00–17:00 Berlin time (08:00–16:00 UTC,
CET in January); Alice's standup and Bob's focus block are clipped to it and
merged, leaving two common free slots:

```json
{
  "window_start": "2026-01-05T00:00:00Z",
  "window_end": "2026-01-06T00:00:00Z",
  "timezone": "Europe/Berlin",
  "count": 2,
  "slots": [
    {
      "start": "2026-01-05T09:00:00Z",
      "end": "2026-01-05T11:00:00Z",
      "minutes": 120
    },
    {
      "start": "2026-01-05T12:00:00Z",
      "end": "2026-01-05T16:00:00Z",
      "minutes": 240
    }
  ]
}
```

### Errors (`422 Unprocessable Entity`)

Returned with a descriptive `detail` message for: unparsable iCalendar
text, invalid `RRULE` (e.g. `FREQ=BOGUS`), unknown `TZID`
(non-IANA name), missing `DTSTART`/`UID`, `RECURRENCE-ID` with
`RANGE=THISANDFUTURE` (unsupported), `window_start >= window_end`, and
expansions exceeding 10 000 occurrences per UID within the window.

`/availability` additionally returns `422` for: an unknown `timezone`
(non-IANA name), an unknown weekday key in `working_hours`, a working
period that is inverted or crosses midnight (`start` must be before `end`
within the same local day), a non-positive `min_minutes`, and an empty
`calendars` list.

## Semantics and limitations

- **Window overlap**: an instance is included when
  `start < window_end and end > window_start`; zero-duration instances when
  `window_start <= start < window_end`.
- **Overrides** replace the occurrence identified by `RECURRENCE-ID` even
  when the original falls outside the expansion window (e.g. moved into the
  window from outside). A `STATUS:CANCELLED` override removes the instance.
  An override without `DTEND`/`DURATION` inherits the master's duration.
- **Floating times** (no `Z`, no `TZID`) are interpreted as UTC.
- **All-day events** (`DATE`-valued `DTSTART`) start at local midnight and
  last one day when no `DTEND` is given.
- `RDATE`/`EXDATE` support `DATE` and `DATE-TIME` values (not `PERIOD`);
  `RECURRENCE-ID` with `RANGE=THISANDFUTURE` is not supported.
- A master event with `STATUS:CANCELLED` suppresses its whole series.
- **Availability**: busy = all non-`CANCELLED` instances with positive
  duration, clipped to the working windows and merged as `[start, end)`
  (touching segments merge). Free slots are the complement of busy inside
  the working windows; slots shorter than `min_minutes` are dropped.
  Working windows come from `working_hours` anchored to each local date in
  `timezone` — e.g. 09:00–17:00 in `America/New_York` is 14:00–22:00 UTC
  before the 2026-03-08 spring-forward and 13:00–21:00 UTC after it.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v          # run the test suite
uvicorn app.main:app --reload       # run locally on port 8000
```

## Project layout

```
app/main.py         FastAPI app: POST /expand, POST /availability, GET /health
app/expander.py     recurrence expansion, override merging, TZ/DST handling
app/availability.py common free slots: clip to working hours, merge, complement
tests/              key tests (RRULE/RDATE/EXDATE, overrides, DST, availability, 422s)
Dockerfile          python:3.12-slim image running uvicorn on port 8000
docker-compose.yml  single-command startup
```
