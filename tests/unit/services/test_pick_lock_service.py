from datetime import datetime, timedelta, timezone

from app.services.pick_lock_service import (
    as_utc_datetime,
    evaluate_bout_pick_lock,
    get_bout_automatic_lock_time,
)


NOW = datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)


def event(**updates):
    value = {
        "status": "scheduled",
        "picks_locked": False,
        "picks_lock_override": None,
        "section_lock_times_utc": {
            "early_prelim": datetime(2026, 8, 15, 21, 0),
            "prelim": datetime(2026, 8, 15, 23, 0),
            "main": datetime(2026, 8, 16, 1, 0),
        },
    }
    value.update(updates)
    return value


def bout(section="main", **updates):
    value = {
        "status": "scheduled",
        "result": None,
        "card_section": section,
        "picks_locked": False,
        "picks_lock_override": None,
    }
    value.update(updates)
    return value


def test_each_section_locks_at_its_own_start():
    early = evaluate_bout_pick_lock(event(), bout("early_prelim"), NOW)
    prelim = evaluate_bout_pick_lock(event(), bout("prelim"), NOW)
    main = evaluate_bout_pick_lock(event(), bout("main"), NOW)

    assert early.locked is True
    assert early.reason == "section_time"
    assert prelim.locked is False
    assert main.locked is False


def test_section_locks_at_exact_boundary():
    boundary = datetime(2026, 8, 15, 23, 0, tzinfo=timezone.utc)
    state = evaluate_bout_pick_lock(event(), bout("prelim"), boundary)
    assert state.locked is True
    assert state.reason == "section_time"


def test_full_event_admin_lock_closes_every_section():
    state = evaluate_bout_pick_lock(
        event(picks_lock_override="locked"),
        bout("main"),
        NOW,
    )
    assert state.locked is True
    assert state.reason == "admin_event"


def test_full_event_unlock_overrides_elapsed_time():
    state = evaluate_bout_pick_lock(
        event(picks_lock_override="unlocked"),
        bout("early_prelim"),
        NOW,
    )
    assert state.locked is False


def test_individual_lock_survives_full_event_unlock():
    state = evaluate_bout_pick_lock(
        event(picks_lock_override="unlocked"),
        bout("main", picks_lock_override="locked"),
        NOW,
    )
    assert state.locked is True
    assert state.reason == "admin_bout"


def test_individual_unlock_overrides_elapsed_section_time():
    state = evaluate_bout_pick_lock(
        event(),
        bout("early_prelim", picks_lock_override="unlocked"),
        NOW,
    )
    assert state.locked is False


def test_result_is_immutable_even_after_admin_unlock():
    state = evaluate_bout_pick_lock(
        event(picks_lock_override="unlocked"),
        bout(
            "main",
            picks_lock_override="unlocked",
            result={"winner": "red"},
        ),
        NOW,
    )
    assert state.locked is True
    assert state.reason == "result"


def test_cancelled_bout_is_always_locked():
    state = evaluate_bout_pick_lock(
        event(picks_lock_override="unlocked"),
        bout("main", status="cancelled"),
        NOW,
    )
    assert state.locked is True
    assert state.reason == "result"


def test_direct_bout_lock_time_has_priority_over_section():
    direct = NOW + timedelta(hours=2)
    value = bout("early_prelim", automatic_lock_time_utc=direct)
    assert get_bout_automatic_lock_time(event(), value) == direct
    assert evaluate_bout_pick_lock(event(), value, NOW).locked is False


def test_naive_mongo_datetime_is_treated_as_utc():
    value = as_utc_datetime(datetime(2026, 8, 15, 21, 0))
    assert value == datetime(2026, 8, 15, 21, 0, tzinfo=timezone.utc)


def test_iso_z_datetime_is_supported():
    value = as_utc_datetime("2026-08-15T21:00:00Z")
    assert value == datetime(2026, 8, 15, 21, 0, tzinfo=timezone.utc)
