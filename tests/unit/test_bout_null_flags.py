"""A single null flag must not take the whole card down.

Production answered `GET /events/143855/bouts` with a 500 because one bout
document had `is_title_fight: null`. The repository parses the card in one
list comprehension, so that one fight cost every fight on the card.
"""

import pytest

from app.models.bout import Bout

NULLABLE_FLAGS = [
    ("is_title_fight", False),
    ("is_bmf_title_fight", False),
    ("is_main_event", False),
    ("is_co_main_event", False),
    ("picks_locked", False),
    ("status", "scheduled"),
]


def _doc(**overrides):
    return {"id": 1, "event_id": 143855, **overrides}


@pytest.mark.parametrize("field,expected", NULLABLE_FLAGS)
def test_stored_null_reads_as_the_default(field, expected):
    bout = Bout(**_doc(**{field: None}))

    assert getattr(bout, field) == expected


def test_every_flag_null_at_once_still_parses():
    """The failing document is unlikely to have exactly one bad field."""
    bout = Bout(**_doc(**{field: None for field, _ in NULLABLE_FLAGS}))

    assert bout.is_title_fight is False
    assert bout.status == "scheduled"


def test_a_real_value_still_wins_over_the_default():
    """The tolerance must not flatten a flag that was actually set."""
    bout = Bout(**_doc(is_title_fight=True, status="completed"))

    assert bout.is_title_fight is True
    assert bout.status == "completed"


def test_absent_key_behaves_the_same_as_a_null_one():
    absent = Bout(**_doc())
    explicit_null = Bout(**_doc(is_title_fight=None))

    assert absent.is_title_fight == explicit_null.is_title_fight
