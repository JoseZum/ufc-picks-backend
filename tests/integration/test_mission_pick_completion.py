"""Selecting a pick-coupled mission on a bout the user never picked.

Twenty-one of the eighty-five card missions rewrite the user's canonical picks.
A canonical pick is only valid when it is complete — winner, method, and a
round for anything that is not a decision — so a mission that binds a winner
but leaves the method to the user cannot be written on a bout with no prior
pick unless the user supplies the rest at selection time.

The domain already models that: `SelectMissionCommand.pick_patches` carries the
missing fields. These tests drive the real selection service over the real
reviewed catalog to record which missions actually depend on it, so the number
is measured rather than inferred from reading the catalog. Reading the catalog
gave 6, then 7, then 9; driving it gives 14.
"""

from datetime import UTC, datetime

import pytest

from app.modules.missions.application import (
    MissionSelectionError,
    MissionSelectionService,
)
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.domain import CanonicalPickPatch, SelectMissionCommand
from app.modules.missions.domain.selections import (
    ComboBuilderMissionSelection,
    ComboLegSelection,
    TargetFighterMissionSelection,
)
from app.modules.missions.indexes import apply_mission_indexes

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
EVENT_ID = 7311
OFFER_SET_ID = "offer_set_ccccccccccccccc1"
BOUT_IDS = (7311001, 7311002, 7311003, 7311004)


def event_document() -> dict:
    return {
        "_id": f"event-{EVENT_ID}",
        "id": EVENT_ID,
        "name": "Pick Completion Card",
        "status": "scheduled",
        "card_data_v1": {
            "card_revision": 3,
            "current_eligibility": {"denominator": len(BOUT_IDS)},
        },
    }


def bout_document(index: int) -> dict:
    bout_id = BOUT_IDS[index]
    return {
        "_id": f"bout-{bout_id}",
        "id": bout_id,
        "event_id": EVENT_ID,
        "status": "scheduled",
        "rounds_scheduled": 5,
        "is_title_fight": True,
        "card_section": "main",
        "card_order": index + 1,
        "fighters": {
            "red": {"fighter_name": f"Red {index}"},
            "blue": {"fighter_name": f"Blue {index}"},
        },
        "result": None,
    }


def offer_set_document(offer_id: str, mission_id: str) -> dict:
    return {
        "_id": OFFER_SET_ID,
        "user_id": "jose",
        "event_id": EVENT_ID,
        "card_revision": 3,
        "catalog_version": "2026.08.01",
        "slots": [{"slot": 1, "offers": [{"offer_id": offer_id, "mission_id": mission_id}]}],
        "created_at": NOW,
    }


@pytest.fixture
async def mission_db(test_db):
    await apply_mission_indexes(test_db)
    await test_db["events"].delete_many({"id": EVENT_ID})
    await test_db["bouts"].delete_many({"event_id": EVENT_ID})
    await test_db["events"].insert_one(event_document())
    await test_db["bouts"].insert_many(
        [bout_document(index) for index in range(len(BOUT_IDS))]
    )
    return test_db


def catalog_service(db) -> MissionSelectionService:
    return MissionSelectionService(db, load_card_catalog(), clock=lambda: NOW)


def pick_coupled_definitions():
    """Every reviewed mission that rewrites canonical picks."""
    return [
        definition
        for definition in load_card_catalog()
        if str(getattr(definition, "pick_effect", "NONE")) != "NONE"
        and definition.interaction in ("TARGET_FIGHTER", "COMBO_BUILDER")
    ]


METHOD_ORDER = ("KO_TKO", "SUBMISSION", "DECISION")


def ordered_methods(methods):
    """Canonical order. The wire type is a frozenset and iterates at random."""
    return sorted(methods or (), key=lambda m: METHOD_ORDER.index(m.value))


def selection_for(definition):
    """Build what the drawer would send: a bout and a corner, nothing invented.

    Whatever the catalog fixes is echoed back; whatever it leaves open is left
    open, which is exactly the payload a user who has made no picks produces.
    """
    spec = definition.selection
    if definition.interaction == "TARGET_FIGHTER":
        # The drawer collects a method or a round whenever the catalog binds
        # that field, so anything with options comes back filled in. Only a
        # field the catalog never mentions arrives empty.
        # `.value`, not `str()`: these are enum members and str() renders the
        # member, not "METHOD".
        bound = {f.value for f in getattr(spec, "bound_pick_fields", ()) or ()}
        # `allowed_methods` is a frozenset: its order changes between runs, so
        # an unsorted [0] makes this test pass or fail at random. The drawer
        # sorts into the same canonical order for exactly this reason.
        methods = ordered_methods(getattr(spec, "allowed_methods", ()))
        rounds = sorted(getattr(spec, "allowed_rounds", ()) or ())
        return TargetFighterMissionSelection(
            kind="TARGET_FIGHTER",
            bout_id=BOUT_IDS[0],
            corner="red",
            method=methods[0] if ("METHOD" in bound and methods) else None,
            round=rounds[0] if ("ROUND" in bound and rounds) else None,
        )

    legs = []
    used_methods: set[str] = set()
    for index, leg in enumerate(spec.legs):
        # A leg the catalog pinned carries NO method in the payload — echoing
        # it back is "Combo leg has a fixed method". Only a leg that OFFERS
        # methods is answered, and `distinct_methods` needs a different one
        # on each leg.
        options = [
            option
            for option in ordered_methods(getattr(leg, "allowed_methods", ()))
            if option.value not in used_methods
        ]
        method = options[0] if (leg.method is None and options) else None
        if method is not None:
            used_methods.add(method.value)
        legs.append(
            ComboLegSelection(
                key=leg.key,
                bout_id=BOUT_IDS[index % len(BOUT_IDS)],
                corner=None if leg.target.value == "FIGHT" else "red",
                method=method,
            )
        )
    return ComboBuilderMissionSelection(kind="COMBO_BUILDER", legs=tuple(legs))


def command_for(definition, offer_id: str, *, patches=()):
    return SelectMissionCommand(
        event_id=EVENT_ID,
        slot=1,
        offer_set_id=OFFER_SET_ID,
        offer_id=offer_id,
        idempotency_key=f"complete-{definition.mission_id}"[:128].replace("/", "-"),
        selection=selection_for(definition),
        pick_patches=patches,
    )


def offer_id_for(index: int) -> str:
    return f"offer_{index:016x}"


async def attempt(db, definition, index, *, patches=()):
    await db["mission_offer_sets"].delete_many({"_id": OFFER_SET_ID})
    await db["mission_assignments"].delete_many({"user_id": "jose"})
    await db["mission_command_receipts"].delete_many({})
    # Each attempt is a separate user story. Without this the pick the previous
    # mission wrote stays bound and the next one is refused for the wrong
    # reason — "Winner is bound by another active mission" rather than the
    # incompleteness this file is about.
    await db["picks"].delete_many({"user_id": "jose"})
    offer_id = offer_id_for(index)
    await db["mission_offer_sets"].insert_one(
        offer_set_document(offer_id, definition.mission_id)
    )
    try:
        await catalog_service(db).select(
            user_id="jose", command=command_for(definition, offer_id, patches=patches)
        )
    except MissionSelectionError as error:
        return str(error)
    return None


@pytest.mark.asyncio
async def test_pick_coupled_missions_needing_a_completed_pick_are_a_known_set(
    mission_db,
):
    """Measure, do not guess, which missions a fresh user cannot select.

    A user with no picks on the card is the ordinary case on a new card, so
    every mission listed here is unselectable in the product until the drawer
    can collect the missing fields.
    """
    definitions = pick_coupled_definitions()
    assert len(definitions) == 21, "reviewed catalog: 21 missions rewrite canonical picks"

    blocked = {}
    for index, definition in enumerate(definitions):
        failure = await attempt(mission_db, definition, index)
        if failure:
            blocked[definition.mission_id] = failure

    incomplete = {
        mission_id: reason
        for mission_id, reason in blocked.items()
        if "complete" in reason.lower()
    }
    # Fourteen of twenty-one. Two are EASY, which is what makes this matter:
    # they are the tier a brand-new user is offered first, and until the drawer
    # can collect the missing fields none of these can be selected at all.
    assert set(incomplete) == {
        "CARD-V2-E-001",  # BANKER LOCK
        "CARD-V2-E-002",  # FADE LOCK
        "CARD-V2-H-001",  # SUBMISSION LOCK
        "CARD-V2-H-007",  # METHOD CYCLE
        "CARD-V2-H-008",  # KO HAT TRICK
        "CARD-V2-H-010",  # ROUND LADDER
        "CARD-V2-H-015",  # CHAMPIONSHIP SCRIPT
        "CARD-V2-M-002",  # KO LOCK
        "CARD-V2-M-011",  # WINNER DOUBLE
        "CARD-V2-M-013",  # METHOD PAIR
        "CARD-V3-H-002",  # SUBMISSION HAT TRICK
        "CARD-V3-H-005",  # KO / SUB DOUBLE
        "CARD-V3-H-015",  # DOUBLE GOLD SCRIPT
        "CARD-V3-M-006",  # KO / DEC SPLIT
    }, sorted(incomplete.items())


@pytest.mark.asyncio
async def test_a_supplied_pick_patch_completes_the_missing_fields(mission_db):
    """The domain already accepts the fix; only the HTTP route omits it."""
    definition = next(
        d for d in pick_coupled_definitions() if d.mission_id == "CARD-V2-E-001"
    )

    without = await attempt(mission_db, definition, 90)
    assert without is not None and "complete" in without.lower()

    with_patch = await attempt(
        mission_db,
        definition,
        91,
        patches=(
            CanonicalPickPatch(bout_id=BOUT_IDS[0], method="KO_TKO", round=2),
        ),
    )
    assert with_patch is None, with_patch

    pick = await mission_db["picks"].find_one({"_id": "jose:%d" % BOUT_IDS[0]})
    assert pick is not None, "the mission must have written the canonical pick"
    assert pick["picked_fighter_name"] == "Red 0"


@pytest.mark.asyncio
async def test_every_pick_coupled_mission_is_selectable_once_the_gaps_are_filled(
    mission_db,
):
    """The completion step has to unblock all six, not just the one clicked.

    The patches here are what the drawer produces: a method for a bout the
    mission left open, a round for a finish it fixed, and nothing at all for a
    field the mission already pinned down.
    """
    for index, definition in enumerate(pick_coupled_definitions()):
        selection = selection_for(definition)

        # Read what the user ended up choosing, exactly as `pickGapsFor` does
        # in the drawer. Reading the definition instead re-derives a method the
        # user already answered and the server rejects the contradiction.
        if definition.interaction == "TARGET_FIGHTER":
            bound = [(selection.bout_id, selection.method, selection.round)]
        else:
            # The effective method is the leg's own choice OR the one the
            # catalog fixed; the round can also be fixed per leg.
            specs = {leg.key: leg for leg in definition.selection.legs}
            bound = []
            for leg in selection.legs:
                if leg.corner is None:
                    continue
                spec = specs.get(leg.key)
                bound.append(
                    (
                        leg.bout_id,
                        leg.method or (spec.method if spec else None),
                        spec.round if spec else None,
                    )
                )

        patches = []
        for bout_id, method, round_ in bound:
            # Send only what is still missing. Restating a method the mission
            # fixed is a conflict, not a redundancy.
            fields = {}
            if method is None:
                fields["method"] = "KO_TKO"
            # A round the catalog already fixed must not be restated either.
            effective = method.value if method is not None else "KO_TKO"
            if effective != "DECISION" and round_ is None:
                fields["round"] = 2
            if fields:
                patches.append(CanonicalPickPatch(bout_id=bout_id, **fields))

        failure = await attempt(
            mission_db, definition, 200 + index, patches=tuple(patches)
        )
        assert failure is None, f"{definition.mission_id}: {failure}"


# ----------------------------------------------------- reachable over HTTP


@pytest.mark.asyncio
async def test_the_select_route_carries_pick_completion_fields(
    client, auth_headers, test_db, sample_event_data
):
    """The capability is worthless if the only caller — the browser — cannot use it.

    This drives the real route rather than the service, because the gap that
    made six missions unselectable was not in the domain: it was a request
    model that silently dropped the field.
    """
    from app.modules.missions.contracts import SelectMissionRequest

    assert "pick_patches" in SelectMissionRequest.model_fields

    request = SelectMissionRequest(
        event_id=1,
        slot=1,
        offer_id="offer_aaaaaaaaaaaaaaaa",
        idempotency_key="route-patch-key",
        selection={"bout_id": 5, "corner": "red"},
        pick_patches=[{"bout_id": 5, "method": "KO_TKO", "round": 2}],
    )
    assert request.pick_patches == [{"bout_id": 5, "method": "KO_TKO", "round": 2}]

    # And the command the route builds from it still validates in the domain.
    patch = CanonicalPickPatch(**request.pick_patches[0])
    assert patch.bout_id == 5 and patch.round == 2


@pytest.mark.asyncio
async def test_a_malformed_patch_is_a_422_not_a_crash(client, auth_headers):
    response = await client.post(
        "/missions/select",
        headers=auth_headers,
        json={
            "event_id": 999321,
            "slot": 1,
            "offer_id": "offer_aaaaaaaaaaaaaaaa",
            "idempotency_key": "malformed-patch-key",
            "pick_patches": [{"bout_id": -1, "method": "TELEPORT"}],
        },
    )
    assert response.status_code < 500, response.text
