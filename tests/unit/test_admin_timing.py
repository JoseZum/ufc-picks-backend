from datetime import datetime

from app.controllers.admin_controller import build_event_timing_updates


def sample_event():
    return {
        "card_start_time_utc": datetime(2026, 8, 15, 21),
        "picks_lock_time_utc": datetime(2026, 8, 15, 21),
        "section_start_times_utc": {
            "early_prelim": datetime(2026, 8, 15, 21),
            "prelim": datetime(2026, 8, 15, 23),
            "main": datetime(2026, 8, 16, 1),
        },
        "section_lock_times_utc": {
            "early_prelim": datetime(2026, 8, 15, 21),
            "prelim": datetime(2026, 8, 15, 23),
            "main": datetime(2026, 8, 16, 1),
        },
    }


def test_moving_card_start_shifts_all_starts_and_locks():
    updates = build_event_timing_updates(
        sample_event(),
        datetime(2026, 8, 15, 22),
        None,
    )
    assert updates["card_start_time_utc"] == datetime(2026, 8, 15, 22)
    assert updates["picks_lock_time_utc"] == datetime(2026, 8, 15, 22)
    assert updates["section_start_times_utc"]["main"] == datetime(
        2026, 8, 16, 2
    )
    assert updates["section_lock_times_utc"]["prelim"] == datetime(
        2026, 8, 16, 0
    )
    assert updates["timing_source"] == "admin"


def test_moving_only_pick_lock_preserves_broadcast_schedule():
    updates = build_event_timing_updates(
        sample_event(),
        None,
        datetime(2026, 8, 15, 20, 30),
    )
    assert "section_start_times_utc" not in updates
    assert updates["section_lock_times_utc"] == {
        "early_prelim": datetime(2026, 8, 15, 20, 30),
        "prelim": datetime(2026, 8, 15, 22, 30),
        "main": datetime(2026, 8, 16, 0, 30),
    }


def test_explicit_start_and_lock_apply_both_offsets():
    updates = build_event_timing_updates(
        sample_event(),
        datetime(2026, 8, 15, 22),
        datetime(2026, 8, 15, 21, 45),
    )
    assert updates["section_start_times_utc"]["early_prelim"] == datetime(
        2026, 8, 15, 22
    )
    assert updates["section_lock_times_utc"]["early_prelim"] == datetime(
        2026, 8, 15, 21, 45
    )
    assert updates["section_lock_times_utc"]["main"] == datetime(
        2026, 8, 16, 1, 45
    )


def test_et_compatibility_fields_honor_daylight_saving():
    updates = build_event_timing_updates(
        sample_event(),
        datetime(2026, 8, 15, 21),
        None,
    )
    assert updates["start_time_et"] == "17:00"
    assert updates["date"] == datetime(2026, 8, 15)


def test_empty_request_produces_no_update():
    assert build_event_timing_updates(sample_event(), None, None) == {}
