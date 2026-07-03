#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import html
import json
from pathlib import Path
import re
import subprocess
import sys

DEFAULT_LOOKBACK = timedelta(minutes=30)
POPUP_TIMEOUT_SECONDS = 4 * 60
POPUP_FONT = "Sans 18"
NOTES_DIR = Path.home() / "notes"
LOG_DIR_NAME = "time-allocation"
LOG_FILE_NAME = "time-allocation.txt"
STATE_FILE_NAME = "state.json"


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


def local_now() -> datetime:
    return datetime.now().astimezone()


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


def append_segments(log_dir: Path, segments: list[ActivitySegment]) -> None:
    log_path = log_dir / LOG_FILE_NAME
    with log_path.open("a", encoding="utf-8") as log_file:
        for segment in segments:
            for day_segment in split_segment_by_day(segment):
                log_file.write(log_line(day_segment) + "\n")


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
    if (
        parsed_last_activity_end is not None
        and isinstance(last_activity_run_start, str)
        and last_activity_run_start.strip()
    ):
        run_start = parse_iso_datetime(
            last_activity_run_start,
            "last_activity_run_start",
        )
        duration_text = f" ({format_display_hours(parsed_last_activity_end - run_start)})"
    else:
        duration_text = ""

    status_line = html.escape(
        f"Last: {last_activity}{duration_text} @ {last_activity_end_text} "
        f"({format_display_hours(now - period_start)} ago)"
    )
    error_block = (
        f'<span foreground="red"><b>Error:</b> {html.escape(error_message)}</span>\n\n'
        if error_message
        else ""
    )
    help_text = html.escape(
        "CSV: activity | activity,hours | activity,end time | . repeats last activity\n"
        "Examples: brunch | brunch, 2 | sleep, 9.5am | ., 3, relax\n"
        "Auto-closes after 4 minutes."
    )
    return (
        f'{error_block}<span font_desc="{POPUP_FONT}"><b>{status_line}</b></span>'
        f'\n\n<span font_desc="{POPUP_FONT}">{help_text}</span>'
    )


def ask_with_yad(message: str, entry_text: str = "") -> str | None:
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
        "--button=Log:0",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def run(notes_dir: Path, now: datetime) -> int:
    log_dir = notes_dir / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(log_dir, now)
    period_start = parse_iso_datetime(state["cursor_time"], "cursor_time")
    if period_start >= now:
        print("No unaccounted time yet.", file=sys.stderr)
        return 0

    entry_text = ""
    error_message = None
    while True:
        raw_input = ask_with_yad(
            popup_text(state, period_start, now, error_message),
            entry_text,
        )
        if raw_input is None or not raw_input.strip():
            return 0
        entry_text = raw_input
        last_activity = state.get("last_activity")
        try:
            segments = parse_activity_segments(
                raw_input,
                period_start,
                now,
                str(last_activity) if isinstance(last_activity, str) else None,
            )
            break
        except ValueError as error:
            error_message = f"{error} No time was logged."

    append_segments(log_dir, segments)
    save_state(log_dir, segments, state)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-dir", type=Path, default=NOTES_DIR)
    args = parser.parse_args()
    raise SystemExit(run(args.notes_dir, local_now()))


if __name__ == "__main__":
    main()
