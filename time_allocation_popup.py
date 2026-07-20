#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

DEFAULT_LOOKBACK = timedelta(minutes=30)
POPUP_TIMEOUT_SECONDS = 4 * 60
POPUP_FONT = "Sans 18"
STATUS_FONT = POPUP_FONT
POPUP_CSS = """
* {
    background-color: #000000;
    background-image: none;
    border-color: #00ff00;
    box-shadow: none;
    caret-color: #00ff00;
    color: #00ff00;
    outline-color: #00ff00;
    text-shadow: none;
}
*:disabled {
    opacity: 1;
}
button:active, button:checked, button:focus, button:hover,
entry selection, progressbar progress {
    background-color: #00ff00;
    color: #000000;
}
""".strip()
NOTES_DIR = Path.home() / "notes"
LOG_DIR_NAME = "time-allocation"
LOG_FILE_NAME = "time-allocation.txt"
STATE_FILE_NAME = "state.json"
GOOGLE_CALENDAR_STATE_FILE_NAME = "google-calendar-state.json"
GOOGLE_EVENT_OUTBOX_FILE_NAME = "google-event-outbox.json"
GOOGLE_TOKEN_FILE_NAME = "google-token.json"
GOOGLE_CALENDAR_ICS_FILE_NAME = "time-allocation.ics"
GOOGLE_CALENDAR_NAME = "Time Allocation"
GOOGLE_CALENDAR_TIME_ZONE = "Asia/Ho_Chi_Minh"
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
PRODUCTIVE_MARKER = "!"
PRODUCTIVE_GOOGLE_EVENT_COLOR_ID = "10"
PENDING_INSERT = "pending_insert"
PENDING_EXTEND = "pending_extend"
INSERTED = "inserted"
LOG_ACTION = "log"
REVIEW_ACTION = "review"
HELP_ACTION = "help"


@dataclass(frozen=True)
class ActivitySegment:
    activity: str
    start: datetime
    end: datetime

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class ActivityRequest:
    activity: str
    hours: float | None
    end_time: time | None


@dataclass(frozen=True)
class LoggedActivity:
    activity: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PopupResponse:
    action: str
    text: str


@dataclass(frozen=True)
class CalendarEventDraft:
    activity: str
    activity_key: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class GoogleCalendarSnapshot:
    service: object
    calendar_id: str
    events: list[dict[str, object]]


class GoogleCalendarUnavailable(RuntimeError):
    pass


def local_now() -> datetime:
    return datetime.now().astimezone()


def read_json_object(path: Path, default: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return dict(default)
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def write_json_object(path: Path, data: dict[str, object], private: bool = False) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if private:
        os.chmod(path, 0o600)


def parse_iso_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"State field {field_name} is missing or empty.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def parse_hours(value: str) -> float:
    stripped = value.strip()
    if stripped.startswith("."):
        stripped = f"0{stripped}"
    try:
        hours = float(stripped)
    except ValueError as error:
        raise ValueError(f"Expected duration in hours, got: {value!r}") from error
    if hours <= 0:
        raise ValueError(f"Duration must be positive, got: {value!r}")
    return hours


def parse_clock_end_time(value: str) -> time | None:
    stripped = re.sub(r"\s+", "", value).casefold()
    match = re.fullmatch(r"(\d{1,2})(?:(:|\.)(\d{1,2}))?(am|pm)", stripped)
    if match is None:
        return None

    hour = int(match.group(1))
    separator = match.group(2)
    minute_text = match.group(3)
    suffix = match.group(4)
    if not 1 <= hour <= 12:
        raise ValueError(f"Clock time hour must be 1-12, got: {value!r}")

    if minute_text is None:
        minute = 0
    elif separator == ":":
        minute = int(minute_text)
    else:
        fraction = float(f"0.{minute_text}")
        minute = round(fraction * 60)

    if not 0 <= minute < 60:
        raise ValueError(f"Clock time minute must be 0-59, got: {value!r}")

    if suffix == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return time(hour, minute)


def parse_request_value(value: str) -> tuple[float | None, time | None]:
    clock_end_time = parse_clock_end_time(value)
    if clock_end_time is not None:
        return None, clock_end_time
    return parse_hours(value), None


def csv_tokens(raw_input: str) -> list[str]:
    try:
        rows = list(csv.reader([raw_input], skipinitialspace=True))
    except csv.Error as error:
        raise ValueError(f"Input is not valid CSV: {error}") from error
    if len(rows) != 1:
        raise ValueError("Input must contain one CSV row.")
    tokens = [token.strip() for token in rows[0]]
    if not tokens or all(token == "" for token in tokens):
        raise ValueError("Input cannot be empty.")
    return tokens


def normalize_activity_name(activity: str, last_activity: str | None) -> str:
    if not activity:
        raise ValueError("Activity names cannot be empty.")
    if activity != ".":
        return activity
    previous_activity = (last_activity or "").strip()
    if not previous_activity:
        raise ValueError("'.' needs a previous activity, but none is recorded.")
    return previous_activity


def activity_key(activity: str) -> str:
    return "".join(activity.split()).casefold()


def is_productive_activity(activity: str) -> bool:
    return PRODUCTIVE_MARKER in activity


def google_event_id_for(activity_key_value: str, start: datetime, end: datetime) -> str:
    event_fingerprint = "|".join(
        [
            activity_key_value,
            start.isoformat(),
            end.isoformat(),
        ]
    )
    return f"ta{hashlib.sha256(event_fingerprint.encode('utf-8')).hexdigest()[:40]}"


def google_error_status(error: Exception) -> int | None:
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def parse_activity_requests(
    raw_input: str,
    last_activity: str | None = None,
) -> list[ActivityRequest]:
    tokens = csv_tokens(raw_input)
    requests: list[ActivityRequest] = []
    token_index = 0

    while token_index < len(tokens):
        activity = normalize_activity_name(tokens[token_index], last_activity)
        token_index += 1

        hours = None
        end_time = None
        if token_index < len(tokens):
            hours, end_time = parse_request_value(tokens[token_index])
            token_index += 1
        requests.append(ActivityRequest(activity=activity, hours=hours, end_time=end_time))

    return requests


def resolve_end_time(cursor: datetime, end_time: time) -> datetime:
    candidate = datetime.combine(cursor.date(), end_time, tzinfo=cursor.tzinfo)
    if candidate <= cursor:
        candidate += timedelta(days=1)
    return candidate


def segment_requests(
    requests: list[ActivityRequest],
    period_start: datetime,
    period_end: datetime,
) -> list[ActivitySegment]:
    if period_end <= period_start:
        raise ValueError("There is no unaccounted time to log.")
    if not requests:
        raise ValueError("Input cannot be empty.")

    period_hours = (period_end - period_start).total_seconds() / 3600
    all_hours_specified = all(
        request.hours is not None and request.end_time is None
        for request in requests
    )
    scale = 1.0
    if all_hours_specified:
        requested_hours = sum(request.hours or 0 for request in requests)
        if requested_hours > period_hours:
            scale = period_hours / requested_hours

    cursor = period_start
    segments: list[ActivitySegment] = []

    for request_index, request in enumerate(requests):
        if cursor >= period_end:
            break

        if request.end_time is not None:
            segment_end = resolve_end_time(cursor, request.end_time)
            if segment_end > period_end:
                raise ValueError(
                    f"End time for {request.activity!r} is after the unaccounted period."
                )
        elif request.hours is None:
            segment_end = period_end
        else:
            requested_end = cursor + timedelta(hours=request.hours * scale)
            last_scaled_segment = (
                all_hours_specified
                and scale < 1.0
                and request_index == len(requests) - 1
            )
            segment_end = period_end if last_scaled_segment else min(requested_end, period_end)

        if segment_end <= cursor:
            raise ValueError(f"Activity has no time to log: {request.activity}")
        segments.append(
            ActivitySegment(activity=request.activity, start=cursor, end=segment_end)
        )
        cursor = segment_end

    return segments


def parse_activity_segments(
    raw_input: str,
    period_start: datetime,
    period_end: datetime,
    last_activity: str | None = None,
) -> list[ActivitySegment]:
    return segment_requests(
        parse_activity_requests(raw_input, last_activity),
        period_start,
        period_end,
    )


def default_state(now: datetime) -> dict[str, object]:
    cursor_time = now - DEFAULT_LOOKBACK
    return {
        "cursor_time": cursor_time.isoformat(),
        "last_activity": "",
        "last_activity_end": "",
        "last_activity_run_start": "",
    }


def load_state(log_dir: Path, now: datetime) -> dict[str, object]:
    state_path = log_dir / STATE_FILE_NAME
    if not state_path.exists():
        return default_state(now)

    with state_path.open("r", encoding="utf-8") as state_file:
        state = json.load(state_file)
    if not isinstance(state, dict):
        raise ValueError(f"State file must contain an object: {state_path}")
    return backfill_last_activity_run_start(log_dir, state)


def same_log_minute(left: datetime, right: datetime) -> bool:
    return left.replace(second=0, microsecond=0) == right.replace(
        second=0,
        microsecond=0,
    )


def parse_log_segment(log_path: Path, line: str, tzinfo) -> LoggedActivity:
    time_range, _hours_text, activity = line.rstrip("\n").split(None, 2)
    start_text, end_text = time_range.split("-", 1)
    log_date = datetime.strptime(log_path.stem, "%Y-%m-%d").date()
    start_time = datetime.strptime(start_text, "%H:%M").time()
    end_time = datetime.strptime(end_text, "%H:%M").time()
    start = datetime.combine(log_date, start_time, tzinfo=tzinfo)
    end = datetime.combine(log_date, end_time, tzinfo=tzinfo)
    if end <= start:
        end += timedelta(days=1)
    return LoggedActivity(activity=activity, start=start, end=end)


def parse_single_file_log_segment(line: str, tzinfo) -> LoggedActivity:
    date_text, time_range, _hours_text, activity = line.rstrip("\n").split(None, 3)
    start_text, end_text = time_range.split("-", 1)
    log_date = datetime.strptime(date_text, "%Y-%m-%d").date()
    start_time = datetime.strptime(start_text, "%H:%M").time()
    end_time = datetime.strptime(end_text, "%H:%M").time()
    start = datetime.combine(log_date, start_time, tzinfo=tzinfo)
    end = datetime.combine(log_date, end_time, tzinfo=tzinfo)
    if end <= start:
        end += timedelta(days=1)
    return LoggedActivity(activity=activity, start=start, end=end)


def read_logged_activity_file(log_path: Path, tzinfo) -> list[LoggedActivity]:
    logged_activities: list[LoggedActivity] = []
    for line_number, line in enumerate(
        log_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            if log_path.name == LOG_FILE_NAME:
                logged_activities.append(parse_single_file_log_segment(line, tzinfo))
            else:
                logged_activities.append(parse_log_segment(log_path, line, tzinfo))
        except ValueError as error:
            print(
                f"Warning: skipping malformed log line {log_path}:{line_number}: {error}",
                file=sys.stderr,
            )
    return logged_activities


def read_logged_activities(log_dir: Path, tzinfo) -> list[LoggedActivity]:
    logged_activities: list[LoggedActivity] = []
    single_log_path = log_dir / LOG_FILE_NAME
    if single_log_path.exists():
        logged_activities.extend(read_logged_activity_file(single_log_path, tzinfo))
    for log_path in sorted(log_dir.glob("*.txt")):
        if log_path.name == LOG_FILE_NAME:
            continue
        logged_activities.extend(read_logged_activity_file(log_path, tzinfo))
    return sorted(logged_activities, key=lambda activity: activity.start)


def find_run_start_in_logs(
    logged_activities: list[LoggedActivity],
    last_activity: str,
    last_activity_end: datetime,
) -> datetime | None:
    target_key = activity_key(last_activity)
    matching_index = None
    for activity_index, logged_activity in enumerate(logged_activities):
        if activity_key(logged_activity.activity) != target_key:
            continue
        if same_log_minute(logged_activity.end, last_activity_end):
            matching_index = activity_index

    if matching_index is None:
        return None

    run_start = logged_activities[matching_index].start
    for previous_activity in reversed(logged_activities[:matching_index]):
        if previous_activity.end != run_start:
            break
        if activity_key(previous_activity.activity) != target_key:
            break
        run_start = previous_activity.start
    return run_start


def backfill_last_activity_run_start(
    log_dir: Path,
    state: dict[str, object],
) -> dict[str, object]:
    if str(state.get("last_activity_run_start") or "").strip():
        return state

    last_activity = str(state.get("last_activity") or "").strip()
    last_activity_end_text = state.get("last_activity_end")
    if not last_activity or not isinstance(last_activity_end_text, str):
        return state
    if not last_activity_end_text.strip():
        return state

    last_activity_end = parse_iso_datetime(last_activity_end_text, "last_activity_end")
    run_start = find_run_start_in_logs(
        read_logged_activities(log_dir, last_activity_end.tzinfo),
        last_activity,
        last_activity_end,
    )
    if run_start is None:
        print(
            "Warning: could not backfill last activity run duration from time logs.",
            file=sys.stderr,
        )
        return state

    updated_state = dict(state)
    updated_state["last_activity_run_start"] = run_start.isoformat()
    return updated_state


def current_run_start(
    segments: list[ActivitySegment],
    previous_state: dict[str, object],
) -> datetime:
    final_segment = segments[-1]
    final_key = activity_key(final_segment.activity)
    run_start = final_segment.start

    for segment in reversed(segments[:-1]):
        if segment.end != run_start:
            break
        if activity_key(segment.activity) != final_key:
            break
        run_start = segment.start

    previous_activity = previous_state.get("last_activity")
    previous_activity_end = previous_state.get("last_activity_end")
    previous_run_start = previous_state.get("last_activity_run_start")
    if (
        run_start == segments[0].start
        and isinstance(previous_activity, str)
        and activity_key(previous_activity) == final_key
        and isinstance(previous_activity_end, str)
        and isinstance(previous_run_start, str)
        and previous_run_start.strip()
    ):
        parsed_previous_end = parse_iso_datetime(
            previous_activity_end,
            "last_activity_end",
        )
        if parsed_previous_end == run_start:
            return parse_iso_datetime(
                previous_run_start,
                "last_activity_run_start",
            )

    return run_start


def save_state(
    log_dir: Path,
    segments: list[ActivitySegment],
    previous_state: dict[str, object],
) -> None:
    segment = segments[-1]
    state_path = log_dir / STATE_FILE_NAME
    state_path.write_text(
        json.dumps(
            {
                "cursor_time": segment.end.isoformat(),
                "last_activity": segment.activity,
                "last_activity_end": segment.end.isoformat(),
                "last_activity_run_start": current_run_start(
                    segments,
                    previous_state,
                ).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def split_segment_by_day(segment: ActivitySegment) -> list[ActivitySegment]:
    pieces: list[ActivitySegment] = []
    cursor = segment.start
    while cursor.date() < segment.end.date():
        midnight = datetime.combine(
            cursor.date() + timedelta(days=1),
            time.min,
            tzinfo=cursor.tzinfo,
        )
        pieces.append(ActivitySegment(segment.activity, cursor, midnight))
        cursor = midnight
    pieces.append(ActivitySegment(segment.activity, cursor, segment.end))
    return pieces


def log_line(segment: ActivitySegment) -> str:
    return (
        f"{segment.start.date().isoformat()} "
        f"{segment.start.strftime('%H:%M')}-{segment.end.strftime('%H:%M')}"
        f"  {segment.hours:.2f}h  {segment.activity}"
    )


def segment_log_lines(segments: list[ActivitySegment]) -> list[str]:
    lines: list[str] = []
    for segment in segments:
        for day_segment in split_segment_by_day(segment):
            lines.append(log_line(day_segment))
    return lines


def segments_log_text(segments: list[ActivitySegment]) -> str:
    return "".join(f"{line}\n" for line in segment_log_lines(segments))


def merge_calendar_event_drafts(segments: list[ActivitySegment]) -> list[CalendarEventDraft]:
    drafts: list[CalendarEventDraft] = []
    for segment in segments:
        key = activity_key(segment.activity)
        if drafts and drafts[-1].activity_key == key and drafts[-1].end == segment.start:
            previous = drafts[-1]
            drafts[-1] = CalendarEventDraft(
                activity=previous.activity,
                activity_key=previous.activity_key,
                start=previous.start,
                end=segment.end,
            )
            continue
        drafts.append(
            CalendarEventDraft(
                activity=segment.activity,
                activity_key=key,
                start=segment.start,
                end=segment.end,
            )
        )
    return drafts


def google_calendar_state_path(log_dir: Path) -> Path:
    return log_dir / GOOGLE_CALENDAR_STATE_FILE_NAME


def google_event_outbox_path(log_dir: Path) -> Path:
    return log_dir / GOOGLE_EVENT_OUTBOX_FILE_NAME


def google_token_path(log_dir: Path) -> Path:
    return log_dir / GOOGLE_TOKEN_FILE_NAME


def google_calendar_ics_path(log_dir: Path) -> Path:
    return log_dir / GOOGLE_CALENDAR_ICS_FILE_NAME


def load_google_calendar_state(log_dir: Path) -> dict[str, object]:
    return read_json_object(google_calendar_state_path(log_dir), {})


def save_google_calendar_state(log_dir: Path, state: dict[str, object]) -> None:
    write_json_object(google_calendar_state_path(log_dir), state)


def load_google_event_outbox(log_dir: Path) -> dict[str, object]:
    outbox = read_json_object(
        google_event_outbox_path(log_dir),
        {"version": 1, "latest_event_id": "", "events": []},
    )
    events = outbox.get("events")
    if not isinstance(events, list):
        raise ValueError(f"Outbox events must be a list: {google_event_outbox_path(log_dir)}")
    return outbox


def save_google_event_outbox(log_dir: Path, outbox: dict[str, object]) -> None:
    pending_events = [
        event
        for event in outbox_events(outbox)
        if event.get("status") != INSERTED
    ]
    pending_outbox = dict(outbox)
    pending_outbox["events"] = pending_events
    pending_outbox["latest_event_id"] = (
        pending_events[-1].get("id") if pending_events else ""
    )
    write_json_object(google_event_outbox_path(log_dir), pending_outbox)


def outbox_events(outbox: dict[str, object]) -> list[dict[str, object]]:
    events = outbox.get("events")
    if not isinstance(events, list):
        raise ValueError("Outbox events must be a list.")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Each outbox event must be an object.")
    return events


def calendar_event_record(draft: CalendarEventDraft) -> dict[str, object]:
    return {
        "id": google_event_id_for(draft.activity_key, draft.start, draft.end),
        "activity": draft.activity,
        "activity_key": draft.activity_key,
        "start": draft.start.isoformat(),
        "end": draft.end.isoformat(),
        "synced_end": "",
        "status": PENDING_INSERT,
    }


def calendar_extension_record(
    event_id: str,
    draft: CalendarEventDraft,
) -> dict[str, object]:
    return {
        "id": event_id,
        "activity": draft.activity,
        "activity_key": draft.activity_key,
        "start": draft.start.isoformat(),
        "end": draft.end.isoformat(),
        "synced_end": draft.start.isoformat(),
        "status": PENDING_EXTEND,
    }


def latest_pending_event_record(
    events: list[dict[str, object]],
) -> dict[str, object] | None:
    pending_events = [
        event
        for event in events
        if event.get("status") in {PENDING_INSERT, PENDING_EXTEND}
    ]
    if not pending_events:
        return None
    return max(
        pending_events,
        key=lambda event: parse_iso_datetime(event.get("end"), "outbox event end"),
    )


def queue_google_calendar_events(
    log_dir: Path,
    segments: list[ActivitySegment],
    latest_synced_event: dict[str, object] | None = None,
) -> None:
    drafts = merge_calendar_event_drafts(segments)
    if not drafts:
        return

    outbox = load_google_event_outbox(log_dir)
    events = outbox_events(outbox)
    latest_event = latest_pending_event_record(events)

    if latest_event is not None:
        latest_key = latest_event.get("activity_key")
        latest_end = parse_iso_datetime(latest_event.get("end"), "outbox event end")
        first_draft = drafts[0]
        if latest_key == first_draft.activity_key and latest_end == first_draft.start:
            latest_event["end"] = first_draft.end.isoformat()
            outbox["latest_event_id"] = latest_event["id"]
            drafts = drafts[1:]

    if drafts and latest_synced_event is not None:
        first_draft = drafts[0]
        event_id = latest_synced_event.get("id")
        event_summary = google_event_activity(latest_synced_event)
        event_end = google_event_datetime(latest_synced_event, "end")
        if (
            isinstance(event_id, str)
            and event_summary is not None
            and event_end == first_draft.start
            and activity_key(event_summary) == first_draft.activity_key
        ):
            extension_record = calendar_extension_record(event_id, first_draft)
            events.append(extension_record)
            outbox["latest_event_id"] = extension_record["id"]
            drafts = drafts[1:]

    for draft in drafts:
        record = calendar_event_record(draft)
        events.append(record)
        outbox["latest_event_id"] = record["id"]

    save_google_event_outbox(log_dir, outbox)


def google_event_body(record: dict[str, object]) -> dict[str, object]:
    body = {
        "id": str(record["id"]),
        "summary": str(record["activity"]),
        "start": {
            "dateTime": str(record["start"]),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
        "end": {
            "dateTime": str(record["end"]),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        },
        "description": "Created by time_allocation_popup.py",
    }
    if is_productive_activity(str(record["activity"])):
        body["colorId"] = PRODUCTIVE_GOOGLE_EVENT_COLOR_ID
    return body


def google_event_end_patch(record: dict[str, object]) -> dict[str, object]:
    return {
        "end": {
            "dateTime": str(record["end"]),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        }
    }


def google_calendar_events(service, calendar_id: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    page_token = None
    while True:
        request = service.events().list(
            calendarId=calendar_id,
            singleEvents=True,
            orderBy="startTime",
            showDeleted=False,
            maxResults=2500,
            pageToken=page_token,
        )
        response = request.execute()
        items = response.get("items", [])
        if not isinstance(items, list):
            raise ValueError("Google Calendar events response did not include a list.")
        for item in items:
            if isinstance(item, dict):
                events.append(item)
        page_token = response.get("nextPageToken")
        if not page_token:
            return events


def google_event_datetime(event: dict[str, object], field_name: str) -> datetime | None:
    raw_time = event.get(field_name)
    if not isinstance(raw_time, dict):
        return None
    date_time = raw_time.get("dateTime")
    if not isinstance(date_time, str) or not date_time.strip():
        return None
    return parse_iso_datetime(date_time, f"Google Calendar event {field_name}")


def google_event_activity(event: dict[str, object]) -> str | None:
    summary = event.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    return summary


def google_event_logged_activity(event: dict[str, object]) -> LoggedActivity | None:
    activity = google_event_activity(event)
    start = google_event_datetime(event, "start")
    end = google_event_datetime(event, "end")
    if activity is None or start is None or end is None or end <= start:
        return None
    return LoggedActivity(activity=activity, start=start, end=end)


def google_event_end(event: dict[str, object]) -> datetime | None:
    return google_event_datetime(event, "end")


def latest_google_event(events: list[dict[str, object]]) -> dict[str, object] | None:
    timed_events = [
        event
        for event in events
        if google_event_activity(event) is not None and google_event_end(event) is not None
    ]
    return max(timed_events, key=lambda event: google_event_end(event) or datetime.min) if timed_events else None


def outbox_logged_activity(record: dict[str, object]) -> LoggedActivity:
    return LoggedActivity(
        activity=str(record["activity"]),
        start=parse_iso_datetime(record.get("start"), "outbox event start"),
        end=parse_iso_datetime(record.get("end"), "outbox event end"),
    )


def logged_activities_from_google_events(
    events: list[dict[str, object]],
) -> list[LoggedActivity]:
    logged_activities: list[LoggedActivity] = []
    for event in events:
        logged_activity = google_event_logged_activity(event)
        if logged_activity is not None:
            logged_activities.append(logged_activity)
    return logged_activities


def logged_activities_from_pending_outbox(log_dir: Path) -> list[LoggedActivity]:
    return [
        outbox_logged_activity(record)
        for record in outbox_events(load_google_event_outbox(log_dir))
        if record.get("status") in {PENDING_INSERT, PENDING_EXTEND}
    ]


def state_from_logged_activities(
    logged_activities: list[LoggedActivity],
    now: datetime,
) -> dict[str, object]:
    completed_activities = [
        logged_activity
        for logged_activity in logged_activities
        if logged_activity.end <= now
    ]
    if not completed_activities:
        return default_state(now)

    activities = sorted(completed_activities, key=lambda activity: (activity.end, activity.start))
    latest_activity = activities[-1]
    latest_key = activity_key(latest_activity.activity)
    run_start = latest_activity.start
    for previous_activity in reversed(activities[:-1]):
        if previous_activity.end != run_start:
            break
        if activity_key(previous_activity.activity) != latest_key:
            break
        run_start = previous_activity.start

    return {
        "cursor_time": latest_activity.end.isoformat(),
        "last_activity": latest_activity.activity,
        "last_activity_end": latest_activity.end.isoformat(),
        "last_activity_run_start": run_start.isoformat(),
    }


def runtime_state_from_google_calendar(
    log_dir: Path,
    events: list[dict[str, object]],
    now: datetime,
) -> dict[str, object]:
    return state_from_logged_activities(
        logged_activities_from_google_events(events)
        + logged_activities_from_pending_outbox(log_dir),
        now,
    )


def escape_ics_text(value: object) -> str:
    text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold_ics_line(line: str) -> list[str]:
    if len(line) <= 75:
        return [line]
    folded_lines = [line[:75]]
    cursor = 75
    while cursor < len(line):
        folded_lines.append(f" {line[cursor:cursor + 74]}")
        cursor += 74
    return folded_lines


def format_ics_datetime(value: datetime) -> str:
    local_value = value.astimezone(ZoneInfo(GOOGLE_CALENDAR_TIME_ZONE))
    return local_value.strftime("%Y%m%dT%H%M%S")


def format_ics_utc_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def google_event_ics_lines(
    event: dict[str, object],
    generated_at: datetime,
) -> list[str]:
    event_id = event.get("id")
    activity = google_event_activity(event)
    start = google_event_datetime(event, "start")
    end = google_event_datetime(event, "end")
    if not isinstance(event_id, str) or activity is None or start is None or end is None:
        return []

    updated_text = event.get("updated")
    updated = (
        parse_iso_datetime(updated_text, "Google Calendar event updated")
        if isinstance(updated_text, str) and updated_text.strip()
        else generated_at
    )
    lines = [
        "BEGIN:VEVENT",
        f"UID:{escape_ics_text(event.get('iCalUID') or event_id)}",
        f"DTSTAMP:{format_ics_utc_datetime(updated)}",
        f"DTSTART;TZID={GOOGLE_CALENDAR_TIME_ZONE}:{format_ics_datetime(start)}",
        f"DTEND;TZID={GOOGLE_CALENDAR_TIME_ZONE}:{format_ics_datetime(end)}",
        f"SUMMARY:{escape_ics_text(activity)}",
    ]
    description = event.get("description")
    if isinstance(description, str) and description:
        lines.append(f"DESCRIPTION:{escape_ics_text(description)}")
    color_id = event.get("colorId")
    if isinstance(color_id, str) and color_id:
        lines.append(f"X-GOOGLE-CALENDAR-COLOR-ID:{escape_ics_text(color_id)}")
    if color_id == PRODUCTIVE_GOOGLE_EVENT_COLOR_ID:
        lines.append("CATEGORIES:Productive")
    lines.append("END:VEVENT")
    return lines


def google_calendar_ics_text(
    calendar_id: str,
    events: list[dict[str, object]],
    generated_at: datetime,
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//time_allocation_popup.py//EN",
        f"X-WR-CALNAME:{escape_ics_text(GOOGLE_CALENDAR_NAME)}",
        f"X-WR-TIMEZONE:{GOOGLE_CALENDAR_TIME_ZONE}",
        f"X-GOOGLE-CALENDAR-ID:{escape_ics_text(calendar_id)}",
    ]
    for event in events:
        lines.extend(google_event_ics_lines(event, generated_at))
    lines.append("END:VCALENDAR")

    folded_lines: list[str] = []
    for line in lines:
        folded_lines.extend(fold_ics_line(line))
    return "\r\n".join(folded_lines) + "\r\n"


def write_google_calendar_ics(
    log_dir: Path,
    calendar_id: str,
    events: list[dict[str, object]],
    generated_at: datetime,
) -> None:
    google_calendar_ics_path(log_dir).write_text(
        google_calendar_ics_text(calendar_id, events, generated_at),
        encoding="utf-8",
    )


def refresh_google_calendar_ics(
    log_dir: Path,
    service,
    calendar_id: str,
    now: datetime,
) -> list[dict[str, object]]:
    events = google_calendar_events(service, calendar_id)
    write_google_calendar_ics(log_dir, calendar_id, events, now)
    return events


def save_google_credentials(token_path: Path, credentials) -> None:
    token_path.write_text(credentials.to_json() + "\n", encoding="utf-8")
    os.chmod(token_path, 0o600)


def google_calendar_service(
    log_dir: Path,
    credentials_path: Path | None = None,
    interactive: bool = False,
):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = google_token_path(log_dir)
    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            GOOGLE_CALENDAR_SCOPES,
        )

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_google_credentials(token_path, credentials)

    if not credentials or not credentials.valid:
        if not interactive or credentials_path is None:
            raise GoogleCalendarUnavailable(
                "Google Calendar is not configured. Run setup with "
                "--setup-google-calendar --google-credentials /path/to/credentials.json."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path),
            GOOGLE_CALENDAR_SCOPES,
        )
        credentials = flow.run_local_server(port=0)
        save_google_credentials(token_path, credentials)

    return build("calendar", "v3", credentials=credentials)


def ensure_google_calendar(
    log_dir: Path,
    service,
    calendar_name: str = GOOGLE_CALENDAR_NAME,
) -> str:
    state = load_google_calendar_state(log_dir)
    calendar_id = state.get("calendar_id")
    if isinstance(calendar_id, str) and calendar_id:
        return calendar_id

    calendar = (
        service.calendars()
        .insert(body={"summary": calendar_name, "timeZone": GOOGLE_CALENDAR_TIME_ZONE})
        .execute()
    )
    calendar_id = calendar.get("id")
    if not isinstance(calendar_id, str) or not calendar_id:
        raise ValueError("Google Calendar creation response did not include an id.")

    save_google_calendar_state(
        log_dir,
        {
            "calendar_id": calendar_id,
            "summary": calendar.get("summary", calendar_name),
            "time_zone": GOOGLE_CALENDAR_TIME_ZONE,
        },
    )
    return calendar_id


def configured_google_calendar_id(log_dir: Path) -> str:
    state = load_google_calendar_state(log_dir)
    calendar_id = state.get("calendar_id")
    if not isinstance(calendar_id, str) or not calendar_id:
        raise GoogleCalendarUnavailable(
            "Google Calendar has no calendar_id yet. Run --setup-google-calendar first."
        )
    return calendar_id


def refresh_google_calendar_snapshot(
    log_dir: Path,
    now: datetime,
    service=None,
    raise_on_unavailable: bool = False,
) -> GoogleCalendarSnapshot | None:
    try:
        calendar_id = configured_google_calendar_id(log_dir)
        active_service = service or google_calendar_service(log_dir)
        sync_google_calendar_events(
            log_dir,
            service=active_service,
            raise_on_unavailable=True,
        )
        events = refresh_google_calendar_ics(log_dir, active_service, calendar_id, now)
        return GoogleCalendarSnapshot(
            service=active_service,
            calendar_id=calendar_id,
            events=events,
        )
    except GoogleCalendarUnavailable:
        if raise_on_unavailable:
            raise
        print(
            "Warning: Google Calendar is unavailable; using pending local queue/state only.",
            file=sys.stderr,
        )
        return None
    except Exception as error:
        if raise_on_unavailable:
            raise
        print(
            f"Warning: Google Calendar refresh failed; using pending local queue/state only: {error}",
            file=sys.stderr,
        )
        return None


def insert_google_event(service, calendar_id: str, record: dict[str, object]) -> bool:
    try:
        (
            service.events()
            .insert(calendarId=calendar_id, body=google_event_body(record))
            .execute()
        )
        return True
    except Exception as error:
        if google_error_status(error) == 409:
            return True
        raise


def extend_google_event(service, calendar_id: str, record: dict[str, object]) -> bool:
    try:
        (
            service.events()
            .patch(
                calendarId=calendar_id,
                eventId=str(record["id"]),
                body=google_event_end_patch(record),
            )
            .execute()
        )
        return True
    except Exception as error:
        if google_error_status(error) == 404:
            return False
        raise


def queue_missing_extension_as_new_event(
    events: list[dict[str, object]],
    record: dict[str, object],
) -> dict[str, object] | None:
    synced_end_text = record.get("synced_end")
    if not isinstance(synced_end_text, str) or not synced_end_text:
        synced_end_text = str(record["start"])
    extension_start = parse_iso_datetime(synced_end_text, "synced_end")
    extension_end = parse_iso_datetime(record["end"], "outbox event end")
    if extension_start >= extension_end:
        record["status"] = INSERTED
        record["end"] = synced_end_text
        return None

    record["end"] = synced_end_text
    record["status"] = INSERTED
    new_record = calendar_event_record(
        CalendarEventDraft(
            activity=str(record["activity"]),
            activity_key=str(record["activity_key"]),
            start=extension_start,
            end=extension_end,
        )
    )
    events.append(new_record)
    return new_record


def sync_google_calendar_events(
    log_dir: Path,
    service=None,
    raise_on_unavailable: bool = False,
) -> int:
    state = load_google_calendar_state(log_dir)
    calendar_id = state.get("calendar_id")
    if not isinstance(calendar_id, str) or not calendar_id:
        message = (
            "Google Calendar has no calendar_id yet. Run "
            "--setup-google-calendar first."
        )
        if raise_on_unavailable:
            raise GoogleCalendarUnavailable(message)
        print(f"Warning: {message}", file=sys.stderr)
        return 0

    if service is None:
        try:
            service = google_calendar_service(log_dir)
        except GoogleCalendarUnavailable:
            if raise_on_unavailable:
                raise
            print(
                "Warning: Google Calendar is not configured; queued events will retry later.",
                file=sys.stderr,
            )
            return 0

    outbox = load_google_event_outbox(log_dir)
    events = outbox_events(outbox)
    pending_events = [
        event
        for event in events
        if event.get("status") != INSERTED
    ]
    if len(pending_events) != len(events):
        outbox["events"] = pending_events
        events = pending_events
        save_google_event_outbox(log_dir, outbox)
    synced_count = 0

    event_index = 0
    while event_index < len(events):
        record = events[event_index]
        status = record.get("status")
        try:
            if status == PENDING_INSERT:
                insert_google_event(service, calendar_id, record)
                record["status"] = INSERTED
                record["synced_end"] = record["end"]
                synced_count += 1
                save_google_event_outbox(log_dir, outbox)
            elif status == PENDING_EXTEND:
                if extend_google_event(service, calendar_id, record):
                    record["status"] = INSERTED
                    record["synced_end"] = record["end"]
                    synced_count += 1
                    save_google_event_outbox(log_dir, outbox)
                else:
                    new_record = queue_missing_extension_as_new_event(events, record)
                    if new_record is not None:
                        outbox["latest_event_id"] = new_record["id"]
                    save_google_event_outbox(log_dir, outbox)
                    continue
        except Exception:
            save_google_event_outbox(log_dir, outbox)
            raise
        event_index += 1

    return synced_count


def setup_google_calendar(notes_dir: Path, credentials_path: Path) -> int:
    log_dir = notes_dir / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    now = local_now()
    service = google_calendar_service(
        log_dir,
        credentials_path=credentials_path,
        interactive=True,
    )
    calendar_id = ensure_google_calendar(log_dir, service)
    synced_count = sync_google_calendar_events(
        log_dir,
        service=service,
        raise_on_unavailable=True,
    )
    refresh_google_calendar_ics(log_dir, service, calendar_id, now)
    print(f"Google Calendar configured: {calendar_id}")
    print(f"Synced queued events: {synced_count}")
    print(f"ICS mirror: {google_calendar_ics_path(log_dir)}")
    return 0


def format_clock_time(value: datetime) -> str:
    hour = value.strftime("%I").lstrip("0") or "12"
    suffix = value.strftime("%p").lower()
    if value.minute == 0:
        return f"{hour}{suffix}"
    return f"{hour}:{value.minute:02d}{suffix}"


def format_display_hours(duration: timedelta) -> str:
    hours = max(0, duration.total_seconds()) / 3600
    return f"{hours:.1f}".removesuffix(".0") + "h"


def popup_text(
    state: dict[str, object],
    now: datetime,
    error_message: str | None = None,
    review_text: str | None = None,
    show_help: bool = True,
    recent_only: bool = False,
) -> str:
    last_activity = str(state.get("last_activity") or "none")
    last_activity_end = state.get("last_activity_end")
    if isinstance(last_activity_end, str) and last_activity_end.strip():
        parsed_last_activity_end = parse_iso_datetime(
            last_activity_end,
            "last_activity_end",
        )
        last_activity_end_text = format_clock_time(parsed_last_activity_end)
    else:
        parsed_last_activity_end = None
        last_activity_end_text = "none"

    last_activity_run_start = state.get("last_activity_run_start")
    last_activity_duration = None
    if (
        parsed_last_activity_end is not None
        and isinstance(last_activity_run_start, str)
        and last_activity_run_start.strip()
    ):
        run_start = parse_iso_datetime(
            last_activity_run_start,
            "last_activity_run_start",
        )
        last_activity_duration = format_display_hours(parsed_last_activity_end - run_start)

    last_activity_line = last_activity
    if parsed_last_activity_end is not None:
        time_since_last_activity = format_display_hours(now - parsed_last_activity_end)
        last_activity_line = (
            f"{last_activity} ({time_since_last_activity} ago) @ {last_activity_end_text}"
        )

    status_lines = [last_activity_line]
    if last_activity_duration is not None:
        status_lines.append(f"len {last_activity_duration}")
    if recent_only:
        status_lines.append("Only the last 30m needs logging.")
    status_text = html.escape("\n".join(status_lines))
    error_block = (
        f'<span><b>Error:</b> {html.escape(error_message)}</span>\n\n'
        if error_message
        else ""
    )
    review_block = (
        f'\n\n<span font_desc="Monospace 18"><b>Would log:</b>\n'
        f"{html.escape(review_text.rstrip())}</span>"
        if review_text
        else ""
    )
    help_text = html.escape(
        "CSV: activity | activity,hours | activity,end time | . repeats last activity\n"
        "Examples: brunch | brunch, 2 | sleep, 9.5am | ., 3, relax\n"
        "Auto-closes after 4 minutes."
    )
    help_block = f'\n\n<span font_desc="{POPUP_FONT}">{help_text}</span>' if show_help else ""
    return (
        f'{error_block}<span font_desc="{STATUS_FONT}"><b>{status_text}</b></span>'
        f"{help_block}{review_block}"
    )


def ask_with_yad(
    message: str,
    entry_text: str = "",
    show_help_button: bool = False,
) -> PopupResponse | None:
    command = [
        "yad",
        "--entry",
        "--title=Time allocation",
        "--undecorated",
        f"--fontname={POPUP_FONT}",
        f"--css={POPUP_CSS}",
        "--text",
        message,
        "--entry-text",
        entry_text,
        f"--timeout={POPUP_TIMEOUT_SECONDS}",
        "--timeout-indicator=bottom",
        "--button=Cancel:1",
    ]
    if show_help_button:
        command.append("--button=Help:3")
    command.extend(["--button=Review:2", "--button=Log:0"])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode == 0:
        return PopupResponse(action=LOG_ACTION, text=completed.stdout.strip())
    if completed.returncode == 2:
        return PopupResponse(action=REVIEW_ACTION, text=completed.stdout.strip())
    if completed.returncode == 3:
        return PopupResponse(action=HELP_ACTION, text=completed.stdout.strip())
    return None


def run(
    notes_dir: Path,
    now: datetime,
    hide_help_by_default: bool = False,
    recent_only_after_hours: float | None = None,
) -> int:
    log_dir = notes_dir / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    snapshot = refresh_google_calendar_snapshot(log_dir, now)
    if snapshot is None:
        pending_activities = logged_activities_from_pending_outbox(log_dir)
        state = (
            state_from_logged_activities(pending_activities, now)
            if pending_activities
            else load_state(log_dir, now)
        )
        calendar_events: list[dict[str, object]] = []
    else:
        state = runtime_state_from_google_calendar(log_dir, snapshot.events, now)
        calendar_events = snapshot.events
    period_start = parse_iso_datetime(state["cursor_time"], "cursor_time")
    if period_start >= now:
        print("No unaccounted time yet.", file=sys.stderr)
        return 0
    recent_only = (
        recent_only_after_hours is not None
        and now - period_start > timedelta(hours=recent_only_after_hours)
    )
    if recent_only:
        period_start = max(period_start, now - DEFAULT_LOOKBACK)

    entry_text = ""
    error_message = None
    review_text = None
    help_visible = not hide_help_by_default
    while True:
        response = ask_with_yad(
            popup_text(
                state,
                now,
                error_message=error_message,
                review_text=review_text,
                show_help=help_visible,
                recent_only=recent_only,
            ),
            entry_text,
            show_help_button=hide_help_by_default and not help_visible,
        )
        if response is None:
            return 0
        raw_input = response.text
        entry_text = raw_input
        if response.action == HELP_ACTION:
            help_visible = True
            continue
        if not raw_input.strip():
            return 0
        last_activity = state.get("last_activity")
        try:
            segments = parse_activity_segments(
                raw_input,
                period_start,
                now,
                str(last_activity) if isinstance(last_activity, str) else None,
            )
        except ValueError as error:
            error_message = f"{error} No time was logged."
            review_text = None
            continue

        if response.action == REVIEW_ACTION:
            error_message = None
            review_text = segments_log_text(segments)
            continue

        break

    try:
        queue_google_calendar_events(
            log_dir,
            segments,
            latest_synced_event=latest_google_event(calendar_events),
        )
    except Exception as error:
        print(
            f"Error: Google Calendar queueing failed; no time was logged: {error}",
            file=sys.stderr,
        )
        return 1

    save_state(log_dir, segments, state)
    refresh_google_calendar_snapshot(
        log_dir,
        now,
        service=snapshot.service if snapshot is not None else None,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-dir", type=Path, default=NOTES_DIR)
    parser.add_argument("--setup-google-calendar", action="store_true")
    parser.add_argument("--sync-google-calendar", action="store_true")
    parser.add_argument("--google-credentials", type=Path)
    parser.add_argument("--hide-help-by-default", action="store_true")
    parser.add_argument(
        "--recent-only-after-hours",
        type=parse_hours,
        metavar="HOURS",
        help="only ask about the last 30 minutes when unaccounted time exceeds HOURS",
    )
    args = parser.parse_args()

    if args.setup_google_calendar:
        if args.google_credentials is None:
            parser.error("--setup-google-calendar requires --google-credentials")
        raise SystemExit(setup_google_calendar(args.notes_dir, args.google_credentials))

    if args.sync_google_calendar:
        log_dir = args.notes_dir / LOG_DIR_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        service = google_calendar_service(log_dir)
        calendar_id = configured_google_calendar_id(log_dir)
        synced_count = sync_google_calendar_events(
            log_dir,
            service=service,
            raise_on_unavailable=True,
        )
        refresh_google_calendar_ics(log_dir, service, calendar_id, local_now())
        print(f"Synced queued events: {synced_count}")
        print(f"ICS mirror: {google_calendar_ics_path(log_dir)}")
        raise SystemExit(0)

    raise SystemExit(
        run(
            args.notes_dir,
            local_now(),
            hide_help_by_default=args.hide_help_by_default,
            recent_only_after_hours=args.recent_only_after_hours,
        )
    )


if __name__ == "__main__":
    main()
