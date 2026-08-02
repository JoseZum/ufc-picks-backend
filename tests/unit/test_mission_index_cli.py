from argparse import Namespace

import pytest

from app.modules.missions.indexes import mission_index_plan_id
from scripts.mission_indexes import is_local_mongo_uri, require_mutation_confirmation


def confirmation_args(**overrides) -> Namespace:
    values = {
        "confirm_database": "ufc_picks_test",
        "confirm_plan_id": mission_index_plan_id(),
        "allow_nonlocal": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_local_uri_detection_rejects_remote_hosts():
    assert is_local_mongo_uri("mongodb://127.0.0.1:27017") is True
    assert is_local_mongo_uri("mongodb://localhost:27017") is True
    assert is_local_mongo_uri("mongodb://db.example.com:27017") is False


def test_mutation_requires_exact_database_and_plan():
    with pytest.raises(SystemExit):
        require_mutation_confirmation(
            confirmation_args(confirm_database="wrong"),
            "mongodb://localhost:27017",
            "ufc_picks_test",
        )
    with pytest.raises(SystemExit):
        require_mutation_confirmation(
            confirmation_args(confirm_plan_id="stale"),
            "mongodb://localhost:27017",
            "ufc_picks_test",
        )


def test_nonlocal_mutation_requires_extra_flag():
    with pytest.raises(SystemExit):
        require_mutation_confirmation(
            confirmation_args(),
            "mongodb://db.example.com:27017",
            "ufc_picks_test",
        )

    require_mutation_confirmation(
        confirmation_args(allow_nonlocal=True),
        "mongodb://db.example.com:27017",
        "ufc_picks_test",
    )
