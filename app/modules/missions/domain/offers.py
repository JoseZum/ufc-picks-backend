"""Deterministic personalized card-offer generation."""

from __future__ import annotations

import hashlib
import hmac
import itertools
from dataclasses import dataclass
from datetime import datetime

from app.modules.missions.domain.catalog import MissionCatalog
from app.modules.missions.domain.definitions import MissionDefinition
from app.modules.missions.domain.eligibility import (
    FrozenCardFacts,
    MissionOverlapPolicy,
    eligible_definitions,
)
from app.modules.missions.domain.enums import MissionDifficulty

OFFER_DIFFICULTY_ORDER = (
    MissionDifficulty.EASY,
    MissionDifficulty.MEDIUM,
    MissionDifficulty.HARD,
)
OFFER_SLOT_COUNT = 3


class OfferGenerationError(ValueError):
    """The eligible catalog cannot produce a safe three-slot offer set."""


@dataclass(frozen=True)
class MissionOffer:
    offer_id: str
    slot: int
    definition: MissionDefinition


@dataclass(frozen=True)
class MissionOfferSlot:
    slot: int
    offers: tuple[MissionOffer, MissionOffer, MissionOffer]

    def __post_init__(self) -> None:
        if self.slot not in range(1, OFFER_SLOT_COUNT + 1):
            raise ValueError("offer slot must be 1, 2 or 3")
        if tuple(offer.definition.difficulty for offer in self.offers) != OFFER_DIFFICULTY_ORDER:
            raise ValueError("each slot must contain EASY, MEDIUM and HARD in order")
        if any(offer.slot != self.slot for offer in self.offers):
            raise ValueError("offer slot numbers must match their container")


@dataclass(frozen=True)
class GeneratedMissionOfferSet:
    offer_set_id: str
    user_id: str
    event_id: int
    card_revision: int
    catalog_version: str
    slots: tuple[MissionOfferSlot, MissionOfferSlot, MissionOfferSlot]

    def __post_init__(self) -> None:
        if tuple(slot.slot for slot in self.slots) != (1, 2, 3):
            raise ValueError("offer set must contain slots 1, 2 and 3 in order")
        mission_ids = [
            offer.definition.mission_id
            for slot in self.slots
            for offer in slot.offers
        ]
        if len(mission_ids) != len(set(mission_ids)):
            raise ValueError("an offer set cannot repeat a mission definition")


def generated_offer_set_document(
    offer_set: GeneratedMissionOfferSet,
    *,
    created_at: datetime,
) -> dict:
    """Serialize a first draw without mutable definition or progress data."""

    return {
        "_id": offer_set.offer_set_id,
        "user_id": offer_set.user_id,
        "event_id": offer_set.event_id,
        "card_revision": offer_set.card_revision,
        "catalog_version": offer_set.catalog_version,
        "slots": [
            {
                "slot": slot.slot,
                "offers": [
                    {
                        "offer_id": offer.offer_id,
                        "mission_id": offer.definition.mission_id,
                    }
                    for offer in slot.offers
                ],
            }
            for slot in offer_set.slots
        ],
        "created_at": created_at,
    }


def _digest(secret: bytes, *parts: object) -> bytes:
    message = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).digest()


def _opaque_id(prefix: str, secret: bytes, *parts: object) -> str:
    return f"{prefix}_{_digest(secret, *parts).hex()[:24]}"


def _slot_candidates(
    *,
    slot: int,
    pools: dict[MissionDifficulty, tuple[MissionDefinition, ...]],
    used_ids: set[str],
    overlap_policy: MissionOverlapPolicy,
    secret: bytes,
    context: tuple[object, ...],
) -> tuple[MissionDefinition, MissionDefinition, MissionDefinition]:
    valid: list[
        tuple[
            bytes,
            tuple[MissionDefinition, MissionDefinition, MissionDefinition],
        ]
    ] = []
    for triple in itertools.product(
        pools[MissionDifficulty.EASY],
        pools[MissionDifficulty.MEDIUM],
        pools[MissionDifficulty.HARD],
    ):
        if any(definition.mission_id in used_ids for definition in triple):
            continue
        if any(
            overlap_policy.compare(left, right).conflicts
            for left, right in itertools.combinations(triple, 2)
        ):
            continue
        mission_ids = tuple(definition.mission_id for definition in triple)
        valid.append(
            (
                _digest(secret, *context, "slot", slot, *mission_ids),
                triple,
            )
        )

    if not valid:
        raise OfferGenerationError(
            f"No overlap-safe EASY/MEDIUM/HARD combination exists for slot {slot}"
        )
    valid.sort(key=lambda candidate: (candidate[0], *(item.mission_id for item in candidate[1])))
    return valid[0][1]


def generate_mission_offers(
    *,
    catalog: MissionCatalog,
    card: FrozenCardFacts,
    user_id: str,
    secret: bytes,
    overlap_policy: MissionOverlapPolicy | None = None,
) -> GeneratedMissionOfferSet:
    if not user_id.strip():
        raise OfferGenerationError("user_id cannot be empty")
    if len(secret) < 32:
        raise OfferGenerationError("offer-generation secret must contain at least 32 bytes")

    policy = overlap_policy or MissionOverlapPolicy()
    eligible = eligible_definitions(tuple(catalog), card)
    pools = {
        difficulty: tuple(
            definition
            for definition in eligible
            if definition.difficulty == difficulty
        )
        for difficulty in OFFER_DIFFICULTY_ORDER
    }
    for difficulty, definitions in pools.items():
        if len(definitions) < OFFER_SLOT_COUNT:
            raise OfferGenerationError(
                f"Card {card.event_id} has only {len(definitions)} eligible "
                f"{difficulty.value} missions; {OFFER_SLOT_COUNT} are required"
            )

    context: tuple[object, ...] = (
        "mission-offers-v1",
        catalog.version,
        user_id,
        card.event_id,
        card.card_revision,
    )
    used_ids: set[str] = set()
    slots: list[MissionOfferSlot] = []
    for slot_number in range(1, OFFER_SLOT_COUNT + 1):
        definitions = _slot_candidates(
            slot=slot_number,
            pools=pools,
            used_ids=used_ids,
            overlap_policy=policy,
            secret=secret,
            context=context,
        )
        used_ids.update(definition.mission_id for definition in definitions)
        offers = tuple(
            MissionOffer(
                offer_id=_opaque_id(
                    "offer",
                    secret,
                    *context,
                    slot_number,
                    definition.difficulty.value,
                    definition.mission_id,
                ),
                slot=slot_number,
                definition=definition,
            )
            for definition in definitions
        )
        slots.append(MissionOfferSlot(slot=slot_number, offers=offers))

    slot_tuple = tuple(slots)
    return GeneratedMissionOfferSet(
        offer_set_id=_opaque_id("offer_set", secret, *context),
        user_id=user_id,
        event_id=card.event_id,
        card_revision=card.card_revision,
        catalog_version=catalog.version,
        slots=slot_tuple,
    )
