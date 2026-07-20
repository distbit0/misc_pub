from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest


MISC_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MISC_DIR / "time_allocation_popup.py"
SPEC = importlib.util.spec_from_file_location("time_allocation_popup", MODULE_PATH)
assert SPEC is not None
time_allocation_popup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["time_allocation_popup"] = time_allocation_popup
SPEC.loader.exec_module(time_allocation_popup)


LOCAL_TZ = timezone(timedelta(hours=7))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 2, hour, minute, tzinfo=LOCAL_TZ)


def google_event(
    event_id: str,
    activity: str,
    start: datetime,
    end: datetime,
    color_id: str | None = None,
) -> dict:
    event = {
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "summary": activity,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "updated": end.isoformat(),
        "description": "Created by time_allocation_popup.py",
    }
    if color_id is not None:
        event["colorId"] = color_id
    return event


def segment_tuples(segments):
    return [
        (
            segment.activity,
            segment.start.strftime("%H:%M"),
            segment.end.strftime("%H:%M"),
        )
        for segment in segments
    ]


def read_state(log_dir: Path) -> dict:
    return json.loads((log_dir / "state.json").read_text(encoding="utf-8"))


def read_outbox(log_dir: Path) -> dict:
    return json.loads(
        (log_dir / "google-event-outbox.json").read_text(encoding="utf-8")
    )


def write_calendar_state(log_dir: Path) -> None:
    (log_dir / "google-calendar-state.json").write_text(
        json.dumps({"calendar_id": "calendar-1"}),
        encoding="utf-8",
    )


class FakeGoogleError(Exception):
    def __init__(self, status: int) -> None:
        self.resp = type("Response", (), {"status": status})()


class FakeExecute:
    def __init__(self, callback) -> None:
        self.callback = callback

    def execute(self):
        return self.callback()


class FakeEventsResource:
    def __init__(self, service) -> None:
        self.service = service

    def insert(self, calendarId: str, body: dict):
        self.service.inserts.append((calendarId, body))
        action = self.service.insert_actions.pop(0) if self.service.insert_actions else None

        def execute():
            if isinstance(action, Exception):
                raise action
            return {"id": body["id"]}

        return FakeExecute(execute)

    def patch(self, calendarId: str, eventId: str, body: dict):
        self.service.patches.append((calendarId, eventId, body))
        action = self.service.patch_actions.pop(0) if self.service.patch_actions else None

        def execute():
            if isinstance(action, Exception):
                raise action
            return {"id": eventId}

        return FakeExecute(execute)

    def list(self, **kwargs):
        self.service.lists.append(kwargs)
        return FakeExecute(lambda: {"items": self.service.listed_events})


class FakeCalendarsResource:
    def __init__(self, service) -> None:
        self.service = service

    def insert(self, body: dict):
        self.service.calendar_inserts.append(body)
        return FakeExecute(lambda: {"id": "calendar-1", "summary": body["summary"]})


class FakeGoogleService:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, str, dict]] = []
        self.calendar_inserts: list[dict] = []
        self.lists: list[dict] = []
        self.insert_actions: list[Exception | None] = []
        self.patch_actions: list[Exception | None] = []
        self.listed_events: list[dict] = []

    def events(self) -> FakeEventsResource:
        return FakeEventsResource(self)

    def calendars(self) -> FakeCalendarsResource:
        return FakeCalendarsResource(self)


def test_activity_without_duration_fills_whole_period() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch", at(10), at(15))

    assert segment_tuples(segments) == [("brunch", "10:00", "15:00")]


def test_popup_header_uses_compact_record_fields() -> None:
    text = time_allocation_popup.popup_text(
        {
            "last_activity": "Clean room",
            "last_activity_end": at(20, 20).isoformat(),
            "last_activity_run_start": at(17, 50).isoformat(),
        },
        at(22),
    )

    assert "Clean room (1.7h ago) @ 8:20pm\nlen 2.5h" in text
    assert "last:" not in text
    assert 'font_desc="Sans 18"' in text
    assert "CSV: activity" in text
    assert "Last:" not in text
    assert "Last record:" not in text
    assert "Last record end:" not in text
    assert "Time since:" not in text
    assert "What have you done since" not in text


def test_popup_can_hide_help_without_changing_status_font() -> None:
    text = time_allocation_popup.popup_text(
        {
            "last_activity": "figure out what to do next for truesight",
            "last_activity_end": at(21, 40).isoformat(),
            "last_activity_run_start": at(21).isoformat(),
        },
        at(21, 46),
        show_help=False,
    )

    assert (
        "figure out what to do next for truesight (0.1h ago) @ 9:40pm\n"
        "len 0.7h"
    ) in text
    assert 'font_desc="Sans 18"' in text
    assert "CSV: activity" not in text
    assert "Examples:" not in text


def test_popup_hours_omit_redundant_decimal() -> None:
    text = time_allocation_popup.popup_text(
        {
            "last_activity": "work",
            "last_activity_end": at(20).isoformat(),
            "last_activity_run_start": at(19).isoformat(),
        },
        at(22),
        show_help=False,
    )

    assert "work (2h ago) @ 8pm\nlen 1h" in text


def test_activity_with_duration_leaves_remainder_unaccounted() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch,2", at(10), at(15))

    assert segment_tuples(segments) == [("brunch", "10:00", "12:00")]


def test_final_activity_gets_remaining_time_after_duration() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch,2,work", at(10), at(15))

    assert segment_tuples(segments) == [
        ("brunch", "10:00", "12:00"),
        ("work", "12:00", "15:00"),
    ]


def test_spaces_around_csv_tokens_are_ignored() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch, 2, work", at(10), at(15))

    assert segment_tuples(segments) == [
        ("brunch", "10:00", "12:00"),
        ("work", "12:00", "15:00"),
    ]


def test_overfull_all_duration_input_is_scaled_proportionally() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch,3,work,6", at(10), at(15))

    assert segment_tuples(segments) == [
        ("brunch", "10:00", "11:40"),
        ("work", "11:40", "15:00"),
    ]


def test_all_duration_input_under_period_leaves_remainder_unaccounted() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch,1,work,1", at(10), at(15))

    assert segment_tuples(segments) == [
        ("brunch", "10:00", "11:00"),
        ("work", "11:00", "12:00"),
    ]


def test_fractional_duration_can_start_with_decimal_point() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch,.2,work", at(10), at(10, 30))

    assert segment_tuples(segments) == [
        ("brunch", "10:00", "10:12"),
        ("work", "10:12", "10:30"),
    ]


def test_end_time_with_decimal_clock_derives_duration() -> None:
    segments = time_allocation_popup.parse_activity_segments("sleep,9.5am", at(8), at(10))

    assert segment_tuples(segments) == [("sleep", "08:00", "09:30")]


def test_end_time_with_colon_clock_derives_duration() -> None:
    segments = time_allocation_popup.parse_activity_segments("sleep,9:30am", at(8), at(10))

    assert segment_tuples(segments) == [("sleep", "08:00", "09:30")]


def test_end_time_can_cross_midnight() -> None:
    start = datetime(2026, 7, 2, 23, 30, tzinfo=LOCAL_TZ)
    end = datetime(2026, 7, 3, 10, 0, tzinfo=LOCAL_TZ)

    segments = time_allocation_popup.parse_activity_segments("sleep,9.5am", start, end)

    assert segments[0].start == start
    assert segments[0].end == datetime(2026, 7, 3, 9, 30, tzinfo=LOCAL_TZ)


def test_bare_decimal_still_means_duration() -> None:
    segments = time_allocation_popup.parse_activity_segments("sleep,9.5", at(8), at(20))

    assert segment_tuples(segments) == [("sleep", "08:00", "17:30")]


def test_end_time_after_period_is_rejected() -> None:
    with pytest.raises(ValueError, match="after the unaccounted period"):
        time_allocation_popup.parse_activity_segments("sleep,10am", at(8), at(9))


def test_mixed_duration_and_end_time_input() -> None:
    start = datetime(2026, 7, 2, 22, 0, tzinfo=LOCAL_TZ)
    end = datetime(2026, 7, 3, 10, 30, tzinfo=LOCAL_TZ)

    segments = time_allocation_popup.parse_activity_segments(
        "work,1.5,go to bed,0.5,sleep,9.5am,get ready,0.5",
        start,
        end,
    )

    assert segment_tuples(segments) == [
        ("work", "22:00", "23:30"),
        ("go to bed", "23:30", "00:00"),
        ("sleep", "00:00", "09:30"),
        ("get ready", "09:30", "10:00"),
    ]


def test_dot_repeats_last_activity() -> None:
    segments = time_allocation_popup.parse_activity_segments(
        ".,3,relax",
        at(10),
        at(15),
        last_activity="work",
    )

    assert segment_tuples(segments) == [
        ("work", "10:00", "13:00"),
        ("relax", "13:00", "15:00"),
    ]


def test_dot_without_duration_fills_period_with_last_activity() -> None:
    segments = time_allocation_popup.parse_activity_segments(
        ".",
        at(10),
        at(11),
        last_activity="work",
    )

    assert segment_tuples(segments) == [("work", "10:00", "11:00")]


def test_dot_needs_last_activity() -> None:
    with pytest.raises(ValueError, match="previous activity"):
        time_allocation_popup.parse_activity_segments(".,3,relax", at(10), at(15))


def test_duration_is_clipped_to_remaining_period() -> None:
    segments = time_allocation_popup.parse_activity_segments("brunch,2", at(10), at(11))

    assert segment_tuples(segments) == [("brunch", "10:00", "11:00")]


def test_activity_duration_pairs_reject_missing_duration() -> None:
    with pytest.raises(ValueError, match="Expected duration"):
        time_allocation_popup.parse_activity_segments("brunch,work", at(10), at(15))


def test_review_text_splits_segments_at_midnight() -> None:
    start = datetime(2026, 7, 2, 23, 30, tzinfo=LOCAL_TZ)
    end = datetime(2026, 7, 3, 0, 30, tzinfo=LOCAL_TZ)
    segment = time_allocation_popup.ActivitySegment("work", start, end)

    assert time_allocation_popup.segments_log_text([segment]) == (
        "2026-07-02 23:30-00:00  0.50h  work\n"
        "2026-07-03 00:00-00:30  0.50h  work\n"
    )


def test_calendar_drafts_merge_repeats_case_and_space_insensitive() -> None:
    drafts = time_allocation_popup.merge_calendar_event_drafts(
        [
            time_allocation_popup.ActivitySegment("Clean room", at(10), at(11)),
            time_allocation_popup.ActivitySegment("cleanroom", at(11), at(12)),
            time_allocation_popup.ActivitySegment("relax", at(12), at(13)),
        ]
    )

    assert [(draft.activity, draft.start, draft.end) for draft in drafts] == [
        ("Clean room", at(10), at(12)),
        ("relax", at(12), at(13)),
    ]


def test_google_event_id_uses_allowed_calendar_characters() -> None:
    event_id = time_allocation_popup.google_event_id_for("cleanroom", at(10), at(11))

    assert len(event_id) >= 5
    assert re.fullmatch(r"[a-v0-9]+", event_id)


def test_productive_activity_sets_google_event_color() -> None:
    draft = time_allocation_popup.CalendarEventDraft(
        activity="write tests!",
        activity_key=time_allocation_popup.activity_key("write tests!"),
        start=at(10),
        end=at(11),
    )

    body = time_allocation_popup.google_event_body(
        time_allocation_popup.calendar_event_record(draft)
    )

    assert body["colorId"] == time_allocation_popup.PRODUCTIVE_GOOGLE_EVENT_COLOR_ID


def test_unmarked_activity_uses_default_google_event_color() -> None:
    draft = time_allocation_popup.CalendarEventDraft(
        activity="lunch",
        activity_key=time_allocation_popup.activity_key("lunch"),
        start=at(10),
        end=at(11),
    )

    body = time_allocation_popup.google_event_body(
        time_allocation_popup.calendar_event_record(draft)
    )

    assert "colorId" not in body


def test_google_calendar_ics_mirror_contains_fetched_events(tmp_path: Path) -> None:
    event = google_event(
        "event-1",
        "write tests!",
        at(10),
        at(11),
        color_id=time_allocation_popup.PRODUCTIVE_GOOGLE_EVENT_COLOR_ID,
    )

    time_allocation_popup.write_google_calendar_ics(
        tmp_path,
        "calendar-1",
        [event],
        at(12),
    )

    text = (tmp_path / "time-allocation.ics").read_text(encoding="utf-8")
    assert text.startswith("BEGIN:VCALENDAR")
    assert "SUMMARY:write tests!" in text
    assert "X-GOOGLE-CALENDAR-COLOR-ID:10" in text
    assert "CATEGORIES:Productive" in text


def test_calendar_queue_has_no_gaps_between_merged_events(tmp_path: Path) -> None:
    segments = [
        time_allocation_popup.ActivitySegment("work", at(10), at(11)),
        time_allocation_popup.ActivitySegment("WORK", at(11), at(12)),
        time_allocation_popup.ActivitySegment("relax", at(12), at(13)),
    ]

    time_allocation_popup.queue_google_calendar_events(tmp_path, segments)

    events = read_outbox(tmp_path)["events"]
    assert len(events) == 2
    assert events[0]["start"] == at(10).isoformat()
    assert events[0]["end"] == events[1]["start"]
    assert events[1]["end"] == at(13).isoformat()


def test_google_sync_retries_queued_insert_after_failure(tmp_path: Path) -> None:
    write_calendar_state(tmp_path)
    segment = time_allocation_popup.ActivitySegment("work", at(10), at(11))
    time_allocation_popup.queue_google_calendar_events(tmp_path, [segment])
    failing_service = FakeGoogleService()
    failing_service.insert_actions.append(FakeGoogleError(500))

    with pytest.raises(FakeGoogleError):
        time_allocation_popup.sync_google_calendar_events(tmp_path, service=failing_service)

    assert read_outbox(tmp_path)["events"][0]["status"] == time_allocation_popup.PENDING_INSERT

    working_service = FakeGoogleService()
    synced_count = time_allocation_popup.sync_google_calendar_events(
        tmp_path,
        service=working_service,
    )

    assert synced_count == 1
    assert read_outbox(tmp_path)["events"] == []


def test_google_insert_conflict_is_treated_as_already_inserted(tmp_path: Path) -> None:
    write_calendar_state(tmp_path)
    segment = time_allocation_popup.ActivitySegment("work", at(10), at(11))
    time_allocation_popup.queue_google_calendar_events(tmp_path, [segment])
    service = FakeGoogleService()
    service.insert_actions.append(FakeGoogleError(409))

    synced_count = time_allocation_popup.sync_google_calendar_events(
        tmp_path,
        service=service,
    )

    assert synced_count == 1
    assert read_outbox(tmp_path)["events"] == []


def test_latest_google_event_is_extended_for_contiguous_repeat(tmp_path: Path) -> None:
    write_calendar_state(tmp_path)
    latest_event = google_event("event-1", "work", at(10), at(11))
    second_segment = time_allocation_popup.ActivitySegment("WORK", at(11), at(12))
    time_allocation_popup.queue_google_calendar_events(
        tmp_path,
        [second_segment],
        latest_synced_event=latest_event,
    )

    queued_event = read_outbox(tmp_path)["events"][0]
    assert len(read_outbox(tmp_path)["events"]) == 1
    assert queued_event["status"] == time_allocation_popup.PENDING_EXTEND
    assert queued_event["id"] == "event-1"
    assert queued_event["end"] == at(12).isoformat()

    patch_service = FakeGoogleService()
    synced_count = time_allocation_popup.sync_google_calendar_events(
        tmp_path,
        service=patch_service,
    )

    assert synced_count == 1
    assert len(patch_service.inserts) == 0
    assert len(patch_service.patches) == 1
    assert patch_service.patches[0][1] == "event-1"
    assert patch_service.patches[0][2]["end"]["dateTime"] == at(12).isoformat()
    assert read_outbox(tmp_path)["events"] == []


def test_save_state_continues_previous_run_case_and_space_insensitive(tmp_path: Path) -> None:
    previous_state = {
        "last_activity": "Clean room",
        "last_activity_end": at(11).isoformat(),
        "last_activity_run_start": at(10).isoformat(),
    }
    segments = [time_allocation_popup.ActivitySegment("cleanroom", at(11), at(12))]

    time_allocation_popup.save_state(tmp_path, segments, previous_state)

    assert read_state(tmp_path)["last_activity_run_start"] == at(10).isoformat()


def test_save_state_combines_adjacent_current_segments_case_and_space_insensitive(tmp_path: Path) -> None:
    previous_state = {
        "last_activity": "work",
        "last_activity_end": at(11).isoformat(),
        "last_activity_run_start": at(10).isoformat(),
    }
    segments = [
        time_allocation_popup.ActivitySegment("Clean room", at(11), at(12)),
        time_allocation_popup.ActivitySegment("cleanroom", at(12), at(13)),
    ]

    time_allocation_popup.save_state(tmp_path, segments, previous_state)

    assert read_state(tmp_path)["last_activity_run_start"] == at(11).isoformat()


def test_save_state_intervening_activity_resets_matching_previous_run(tmp_path: Path) -> None:
    previous_state = {
        "last_activity": "Clean room",
        "last_activity_end": at(11).isoformat(),
        "last_activity_run_start": at(10).isoformat(),
    }
    segments = [
        time_allocation_popup.ActivitySegment("work", at(11), at(12)),
        time_allocation_popup.ActivitySegment("cleanroom", at(12), at(13)),
    ]

    time_allocation_popup.save_state(tmp_path, segments, previous_state)

    assert read_state(tmp_path)["last_activity_run_start"] == at(12).isoformat()


def test_load_state_backfills_legacy_run_start_from_logs(tmp_path: Path) -> None:
    (tmp_path / "2026-07-02.txt").write_text(
        "17:00-17:30  0.50h  work\n"
        "17:30-19:00  1.50h  Clean room\n"
        "19:00-20:20  1.33h  cleanroom\n",
        encoding="utf-8",
    )
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "cursor_time": at(20, 20).isoformat(),
                "last_activity": "Clean room",
                "last_activity_end": at(20, 20).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    state = time_allocation_popup.load_state(tmp_path, at(21))

    assert state["last_activity_run_start"] == at(17, 30).isoformat()


def test_load_state_backfills_run_start_from_single_log_file(tmp_path: Path) -> None:
    (tmp_path / "time-allocation.txt").write_text(
        "2026-07-02 17:00-17:30  0.50h  work\n"
        "2026-07-02 17:30-19:00  1.50h  Clean room\n"
        "2026-07-02 19:00-20:20  1.33h  cleanroom\n",
        encoding="utf-8",
    )
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "cursor_time": at(20, 20).isoformat(),
                "last_activity": "Clean room",
                "last_activity_end": at(20, 20).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    state = time_allocation_popup.load_state(tmp_path, at(21))

    assert state["last_activity_run_start"] == at(17, 30).isoformat()


def test_runtime_state_ignores_future_calendar_events(tmp_path: Path) -> None:
    events = [
        google_event("past-1", "work", at(10), at(11)),
        google_event("future-1", "walk", at(23), at(23, 45)),
    ]

    state = time_allocation_popup.runtime_state_from_google_calendar(
        tmp_path,
        events,
        at(15),
    )

    assert state["cursor_time"] == at(11).isoformat()
    assert state["last_activity"] == "work"


@pytest.mark.parametrize(
    ("last_activity_end", "recent_only_after_hours", "expected_start", "is_recent_only"),
    [
        (at(4), 6, at(10, 30), True),
        (at(4), None, at(4), False),
        (at(5), 6, at(5), False),
    ],
)
def test_recent_only_threshold_controls_unaccounted_period(
    monkeypatch,
    tmp_path: Path,
    last_activity_end: datetime,
    recent_only_after_hours: float | None,
    expected_start: datetime,
    is_recent_only: bool,
) -> None:
    log_dir = tmp_path / time_allocation_popup.LOG_DIR_NAME
    log_dir.mkdir()
    previous_segment = time_allocation_popup.ActivitySegment(
        "sleep",
        last_activity_end - timedelta(hours=1),
        last_activity_end,
    )
    time_allocation_popup.save_state(
        log_dir,
        [previous_segment],
        time_allocation_popup.default_state(previous_segment.start),
    )
    prompts: list[str] = []

    def fake_ask_with_yad(
        message: str,
        entry_text: str = "",
        show_help_button: bool = False,
    ) -> time_allocation_popup.PopupResponse:
        prompts.append(message)
        return time_allocation_popup.PopupResponse(
            time_allocation_popup.LOG_ACTION,
            "work",
        )

    monkeypatch.setattr(time_allocation_popup, "ask_with_yad", fake_ask_with_yad)
    monkeypatch.setattr(
        time_allocation_popup,
        "refresh_google_calendar_snapshot",
        lambda *args, **kwargs: None,
    )

    status = time_allocation_popup.run(
        tmp_path,
        at(11),
        hide_help_by_default=True,
        recent_only_after_hours=recent_only_after_hours,
    )

    assert status == 0
    if is_recent_only:
        assert "<b>last 30m</b>" in prompts[0]
        assert "sleep" not in prompts[0]
        assert "ago" not in prompts[0]
        assert "len " not in prompts[0]
    else:
        assert "last 30m" not in prompts[0]
        assert "sleep" in prompts[0]
    queued_event = read_outbox(log_dir)["events"][0]
    assert queued_event["start"] == expected_start.isoformat()
    assert queued_event["end"] == at(11).isoformat()


def test_invalid_input_reopens_prompt_with_previous_text(monkeypatch, tmp_path: Path) -> None:
    prompts: list[tuple[str, str]] = []
    responses = iter(
        [
            time_allocation_popup.PopupResponse(
                time_allocation_popup.LOG_ACTION,
                "brunch,work",
            ),
            time_allocation_popup.PopupResponse(
                time_allocation_popup.LOG_ACTION,
                "brunch,1",
            ),
        ]
    )

    def fake_ask_with_yad(
        message: str,
        entry_text: str = "",
        show_help_button: bool = False,
    ) -> time_allocation_popup.PopupResponse:
        prompts.append((message, entry_text))
        return next(responses)

    monkeypatch.setattr(time_allocation_popup, "ask_with_yad", fake_ask_with_yad)

    status = time_allocation_popup.run(tmp_path, at(11))

    assert status == 0
    assert len(prompts) == 2
    assert prompts[0][1] == ""
    assert prompts[1][1] == "brunch,work"
    assert "Error:" in prompts[1][0]
    log_path = tmp_path / "time-allocation" / "time-allocation.txt"
    assert not log_path.exists()
    outbox_event = read_outbox(tmp_path / "time-allocation")["events"][0]
    assert outbox_event["activity"] == "brunch"
    assert outbox_event["status"] == time_allocation_popup.PENDING_INSERT


def test_review_reopens_prompt_with_preview_and_preserved_text(monkeypatch, tmp_path: Path) -> None:
    prompts: list[tuple[str, str]] = []
    responses = iter(
        [
            time_allocation_popup.PopupResponse(
                time_allocation_popup.REVIEW_ACTION,
                "brunch,1",
            ),
            time_allocation_popup.PopupResponse(
                time_allocation_popup.LOG_ACTION,
                "brunch,1",
            ),
        ]
    )

    def fake_ask_with_yad(
        message: str,
        entry_text: str = "",
        show_help_button: bool = False,
    ) -> time_allocation_popup.PopupResponse:
        prompts.append((message, entry_text))
        return next(responses)

    monkeypatch.setattr(time_allocation_popup, "ask_with_yad", fake_ask_with_yad)

    status = time_allocation_popup.run(tmp_path, at(11))

    assert status == 0
    assert len(prompts) == 2
    assert prompts[0][1] == ""
    assert prompts[1][1] == "brunch,1"
    assert "Would log:" in prompts[1][0]
    assert "2026-07-02 10:30-11:00  0.50h  brunch" in prompts[1][0]
    log_path = tmp_path / "time-allocation" / "time-allocation.txt"
    assert not log_path.exists()
    outbox_event = read_outbox(tmp_path / "time-allocation")["events"][0]
    assert outbox_event["activity"] == "brunch"
    assert outbox_event["status"] == time_allocation_popup.PENDING_INSERT


def test_hidden_help_button_reopens_prompt_with_help(monkeypatch, tmp_path: Path) -> None:
    prompts: list[tuple[str, str, bool]] = []
    responses = iter(
        [
            time_allocation_popup.PopupResponse(
                time_allocation_popup.HELP_ACTION,
                "brunch,1",
            ),
            time_allocation_popup.PopupResponse(
                time_allocation_popup.LOG_ACTION,
                "brunch,1",
            ),
        ]
    )

    def fake_ask_with_yad(
        message: str,
        entry_text: str = "",
        show_help_button: bool = False,
    ) -> time_allocation_popup.PopupResponse:
        prompts.append((message, entry_text, show_help_button))
        return next(responses)

    monkeypatch.setattr(time_allocation_popup, "ask_with_yad", fake_ask_with_yad)

    status = time_allocation_popup.run(
        tmp_path,
        at(11),
        hide_help_by_default=True,
    )

    assert status == 0
    assert len(prompts) == 2
    assert "CSV: activity" not in prompts[0][0]
    assert prompts[0][2] is True
    assert prompts[1][1] == "brunch,1"
    assert "CSV: activity" in prompts[1][0]
    assert prompts[1][2] is False


def test_review_does_not_write_if_user_cancels(monkeypatch, tmp_path: Path) -> None:
    responses = iter(
        [
            time_allocation_popup.PopupResponse(
                time_allocation_popup.REVIEW_ACTION,
                "brunch,1",
            ),
            None,
        ]
    )

    def fake_ask_with_yad(
        message: str,
        entry_text: str = "",
        show_help_button: bool = False,
    ) -> time_allocation_popup.PopupResponse | None:
        return next(responses)

    monkeypatch.setattr(time_allocation_popup, "ask_with_yad", fake_ask_with_yad)

    status = time_allocation_popup.run(tmp_path, at(11))

    assert status == 0
    assert not (tmp_path / "time-allocation" / "time-allocation.txt").exists()
    assert not (tmp_path / "time-allocation" / "google-event-outbox.json").exists()
