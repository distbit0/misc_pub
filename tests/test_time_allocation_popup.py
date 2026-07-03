from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
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
        at(20, 20),
        at(22),
    )

    assert "Last: Clean room (2.5h) @ 8:20pm (1.7h ago)" in text
    assert "Last record:" not in text
    assert "Last record end:" not in text
    assert "Time since:" not in text
    assert "What have you done since" not in text


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


def test_daily_log_splits_segments_at_midnight(tmp_path: Path) -> None:
    start = datetime(2026, 7, 2, 23, 30, tzinfo=LOCAL_TZ)
    end = datetime(2026, 7, 3, 0, 30, tzinfo=LOCAL_TZ)
    segment = time_allocation_popup.ActivitySegment("work", start, end)

    time_allocation_popup.append_segments(tmp_path, [segment])

    assert (
        tmp_path / "time-allocation.txt"
    ).read_text(encoding="utf-8") == (
        "2026-07-02 23:30-00:00  0.50h  work\n"
        "2026-07-03 00:00-00:30  0.50h  work\n"
    )
    assert not (tmp_path / "2026-07-02.txt").exists()
    assert not (tmp_path / "2026-07-03.txt").exists()


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
    time_allocation_popup.append_segments(
        tmp_path,
        [
            time_allocation_popup.ActivitySegment("work", at(17), at(17, 30)),
            time_allocation_popup.ActivitySegment("Clean room", at(17, 30), at(19)),
            time_allocation_popup.ActivitySegment("cleanroom", at(19), at(20, 20)),
        ],
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


def test_invalid_input_reopens_prompt_with_previous_text(monkeypatch, tmp_path: Path) -> None:
    prompts: list[tuple[str, str]] = []
    responses = iter(["brunch,work", "brunch,1"])

    def fake_ask_with_yad(message: str, entry_text: str = "") -> str:
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
    assert log_path.read_text(encoding="utf-8") == "2026-07-02 10:30-11:00  0.50h  brunch\n"
