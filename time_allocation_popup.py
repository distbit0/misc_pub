#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import html
import json
from pathlib import Path
import subprocess
import sys

DEFAULT_LOOKBACK = timedelta(minutes=30)
POPUP_TIMEOUT_SECONDS = 4 * 60
POPUP_FONT = "Sans 18"
NOTES_DIR = Path.home() / "notes"
LOG_DIR_NAME = "time-allocation"
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
        if token_index < len(tokens):
            hours = parse_hours(tokens[token_index])
            token_index += 1
        requests.append(ActivityRequest(activity=activity, hours=hours))

    return requests


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
    all_hours_specified = all(request.hours is not None for request in requests)
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

        if request.hours is None:
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
        }

    with state_path.open("r", encoding="utf-8") as state_file:
        state = json.load(state_file)
    if not isinstance(state, dict):
        raise ValueError(f"State file must contain an object: {state_path}")
    return state


def save_state(log_dir: Path, segment: ActivitySegment) -> None:
    state_path = log_dir / STATE_FILE_NAME
    state_path.write_text(
        json.dumps(
            {
                "cursor_time": segment.end.isoformat(),
                "last_activity": segment.activity,
                "last_activity_end": segment.end.isoformat(),
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
        f"{segment.start.strftime('%H:%M')}-{segment.end.strftime('%H:%M')}"
        f"  {segment.hours:.2f}h  {segment.activity}"
    )


def append_segments(log_dir: Path, segments: list[ActivitySegment]) -> None:
    for segment in segments:
        for day_segment in split_segment_by_day(segment):
            log_path = log_dir / f"{day_segment.start.date().isoformat()}.txt"
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(log_line(day_segment) + "\n")


def format_clock_time(value: datetime) -> str:
    hour = value.strftime("%I").lstrip("0") or "12"
    suffix = value.strftime("%p").lower()
    if value.minute == 0:
        return f"{hour}{suffix}"
    return f"{hour}:{value.minute:02d}{suffix}"


def format_elapsed_hours(duration: timedelta) -> str:
    hours = max(0, duration.total_seconds()) / 3600
    formatted_hours = f"{hours:.2f}".rstrip("0").rstrip(".")
    return f"{formatted_hours}h"


def popup_text(
    state: dict[str, object],
    period_start: datetime,
    now: datetime,
    error_message: str | None = None,
) -> str:
    last_activity = str(state.get("last_activity") or "none")
    last_activity_end = state.get("last_activity_end")
    if isinstance(last_activity_end, str) and last_activity_end.strip():
        last_activity_end_text = format_clock_time(
            parse_iso_datetime(last_activity_end, "last_activity_end")
        )
    else:
        last_activity_end_text = "none"

    status_line = html.escape(
        f"Last: {last_activity} @ {last_activity_end_text} "
        f"({format_elapsed_hours(now - period_start)} ago)"
    )
    error_block = (
        f'<span foreground="red"><b>Error:</b> {html.escape(error_message)}</span>\n\n'
        if error_message
        else ""
    )
    help_text = html.escape(
        "CSV: activity | activity,hours | activity,hours,next activity | . repeats last activity\n"
        "Examples: brunch | brunch, 2 | brunch, .2, work | ., 3, relax\n"
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
    save_state(log_dir, segments[-1])
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-dir", type=Path, default=NOTES_DIR)
    args = parser.parse_args()
    raise SystemExit(run(args.notes_dir, local_now()))


if __name__ == "__main__":
    main()
