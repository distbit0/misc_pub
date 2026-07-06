#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys

DEFAULT_LOOKBACK = timedelta(minutes=30)
POPUP_TIMEOUT_SECONDS = 4 * 60
POPUP_FONT = "Sans 18"
STATUS_FONT = POPUP_FONT
NOTES_DIR = Path.home() / "notes"
LOG_DIR_NAME = "time-allocation"
LOG_FILE_NAME = "time-allocation.txt"
STATE_FILE_NAME = "state.json"
GOOGLE_CALENDAR_STATE_FILE_NAME = "google-calendar-state.json"
GOOGLE_EVENT_OUTBOX_FILE_NAME = "google-event-outbox.json"
GOOGLE_TOKEN_FILE_NAME = "google-token.json"
GOOGLE_CALENDAR_NAME = "Time Allocation"
GOOGLE_CALENDAR_TIME_ZONE = "Asia/Ho_Chi_Minh"
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
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


def load_state(log_dir: Path, now: datetime) -> dict[str, object]:
    state_path = log_dir / STATE_FILE_NAME
    if not state_path.exists():
        cursor_time = now - DEFAULT_LOOKBACK
        return {
            "cursor_time": cursor_time.isoformat(),
            "last_activity": "",
            "last_activity_end": "",
            "last_activity_run_start": "",
        }

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
    write_json_object(google_event_outbox_path(log_dir), outbox)


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


def find_event_record(
    events: list[dict[str, object]],
    event_id: object,
) -> dict[str, object] | None:
    if not isinstance(event_id, str) or not event_id:
        return None
    return next(
        (event for event in events if event.get("id") == event_id),
        None,
    )


def queue_google_calendar_events(log_dir: Path, segments: list[ActivitySegment]) -> None:
    drafts = merge_calendar_event_drafts(segments)
    if not drafts:
        return

    outbox = load_google_event_outbox(log_dir)
    events = outbox_events(outbox)
    latest_event = find_event_record(events, outbox.get("latest_event_id"))

    if latest_event is not None:
        latest_key = latest_event.get("activity_key")
        latest_end = parse_iso_datetime(latest_event.get("end"), "outbox event end")
        first_draft = drafts[0]
        if latest_key == first_draft.activity_key and latest_end == first_draft.start:
            latest_event["end"] = first_draft.end.isoformat()
            if latest_event.get("status") == INSERTED:
                latest_event["status"] = PENDING_EXTEND
            outbox["latest_event_id"] = latest_event["id"]
            drafts = drafts[1:]

    for draft in drafts:
        record = calendar_event_record(draft)
        events.append(record)
        outbox["latest_event_id"] = record["id"]

    save_google_event_outbox(log_dir, outbox)


def google_event_body(record: dict[str, object]) -> dict[str, object]:
    return {
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


def google_event_end_patch(record: dict[str, object]) -> dict[str, object]:
    return {
        "end": {
            "dateTime": str(record["end"]),
            "timeZone": GOOGLE_CALENDAR_TIME_ZONE,
        }
    }


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


def best_effort_sync_google_calendar(log_dir: Path) -> None:
    try:
        sync_google_calendar_events(log_dir)
    except Exception as error:
        print(
            f"Warning: Google Calendar sync failed; queued events will retry later: {error}",
            file=sys.stderr,
        )


def setup_google_calendar(notes_dir: Path, credentials_path: Path) -> int:
    log_dir = notes_dir / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
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
    print(f"Google Calendar configured: {calendar_id}")
    print(f"Synced queued events: {synced_count}")
    return 0


def append_segments(log_dir: Path, segments: list[ActivitySegment]) -> None:
    log_path = log_dir / LOG_FILE_NAME
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(segments_log_text(segments))


def format_clock_time(value: datetime) -> str:
    hour = value.strftime("%I").lstrip("0") or "12"
    suffix = value.strftime("%p").lower()
    if value.minute == 0:
        return f"{hour}{suffix}"
    return f"{hour}:{value.minute:02d}{suffix}"


def format_display_hours(duration: timedelta) -> str:
    hours = max(0, duration.total_seconds()) / 3600
    return f"{hours:.1f}h"


def popup_text(
    state: dict[str, object],
    period_start: datetime,
    now: datetime,
    error_message: str | None = None,
    review_text: str | None = None,
    show_help: bool = True,
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
    duration_line = None
    if (
        parsed_last_activity_end is not None
        and isinstance(last_activity_run_start, str)
        and last_activity_run_start.strip()
    ):
        run_start = parse_iso_datetime(
            last_activity_run_start,
            "last_activity_run_start",
        )
        duration_line = f"{format_display_hours(parsed_last_activity_end - run_start)} len"

    status_lines = ["last:", last_activity, ""]
    if duration_line is not None:
        status_lines.append(duration_line)
    status_lines.extend(
        [
            f"{last_activity_end_text} end",
            f"{format_display_hours(now - period_start)} ago",
        ]
    )
    status_text = html.escape("\n".join(status_lines))
    error_block = (
        f'<span foreground="red"><b>Error:</b> {html.escape(error_message)}</span>\n\n'
        if error_message
        else ""
    )
    review_block = (
        f'\n\n<span font_desc="Monospace 18"><b>Would append:</b>\n'
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
        "--width=980",
        f"--fontname={POPUP_FONT}",
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
) -> int:
    log_dir = notes_dir / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(log_dir, now)
    period_start = parse_iso_datetime(state["cursor_time"], "cursor_time")
    if period_start >= now:
        print("No unaccounted time yet.", file=sys.stderr)
        return 0

    entry_text = ""
    error_message = None
    review_text = None
    help_visible = not hide_help_by_default
    while True:
        response = ask_with_yad(
            popup_text(
                state,
                period_start,
                now,
                error_message,
                review_text,
                show_help=help_visible,
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

    append_segments(log_dir, segments)
    save_state(log_dir, segments, state)
    try:
        queue_google_calendar_events(log_dir, segments)
        best_effort_sync_google_calendar(log_dir)
    except Exception as error:
        print(
            f"Warning: Google Calendar queueing failed after local log save: {error}",
            file=sys.stderr,
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-dir", type=Path, default=NOTES_DIR)
    parser.add_argument("--setup-google-calendar", action="store_true")
    parser.add_argument("--sync-google-calendar", action="store_true")
    parser.add_argument("--google-credentials", type=Path)
    parser.add_argument("--hide-help-by-default", action="store_true")
    args = parser.parse_args()

    if args.setup_google_calendar:
        if args.google_credentials is None:
            parser.error("--setup-google-calendar requires --google-credentials")
        raise SystemExit(setup_google_calendar(args.notes_dir, args.google_credentials))

    if args.sync_google_calendar:
        log_dir = args.notes_dir / LOG_DIR_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        synced_count = sync_google_calendar_events(log_dir, raise_on_unavailable=True)
        print(f"Synced queued events: {synced_count}")
        raise SystemExit(0)

    raise SystemExit(run(args.notes_dir, local_now(), args.hide_help_by_default))


if __name__ == "__main__":
    main()
