"""Public fight-card visibility rules for terminal canonical bouts."""

import pytest

from app.repositories.bout_repository import BoutRepository


class _Cursor:
    def sort(self, _ordering):
        return self

    async def to_list(self, *, length):
        assert length is None
        return []


class _Collection:
    def __init__(self):
        self.query = None

    def find(self, query):
        self.query = query
        return _Cursor()


class _Database:
    def __init__(self):
        self.collection = _Collection()

    def __getitem__(self, name):
        assert name == "bouts"
        return self.collection


@pytest.mark.asyncio
async def test_default_event_read_excludes_non_current_lifecycles():
    db = _Database()

    await BoutRepository(db).get_by_event(144513)

    assert db.collection.query == {
        "event_id": 144513,
        "status": {"$nin": ["cancelled", "postponed", "replaced"]},
    }


@pytest.mark.asyncio
async def test_explicit_status_read_remains_available_for_internal_history():
    db = _Database()

    await BoutRepository(db).get_by_event(144513, status="replaced")

    assert db.collection.query == {"event_id": 144513, "status": "replaced"}
