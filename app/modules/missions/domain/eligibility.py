"""Elegibilidad de la card congelada y políticas de solapamiento de ofertas."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from app.modules.missions.domain.definitions import (
    CardCapability,
    MissionDefinition,
)
from app.modules.missions.domain.enums import StringEnum


class EligibilityFailureCode(StringEnum):
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    INSUFFICIENT_ELIGIBLE_BOUTS = "INSUFFICIENT_ELIGIBLE_BOUTS"
    INSUFFICIENT_MAIN_CARD_BOUTS = "INSUFFICIENT_MAIN_CARD_BOUTS"
    INSUFFICIENT_PRELIM_BOUTS = "INSUFFICIENT_PRELIM_BOUTS"
    INSUFFICIENT_TITLE_BOUTS = "INSUFFICIENT_TITLE_BOUTS"


@dataclass(frozen=True)
class FrozenCardFacts:
    event_id: int
    card_revision: int
    eligible_bouts: int
    main_card_bouts: int
    prelim_bouts: int
    title_bouts: int
    capabilities: frozenset[CardCapability]

    def __post_init__(self) -> None:
        counts = (
            self.card_revision,
            self.eligible_bouts,
            self.main_card_bouts,
            self.prelim_bouts,
            self.title_bouts,
        )
        if self.event_id <= 0 or any(value < 0 for value in counts):
            raise ValueError("card facts require a positive event id and non-negative counts")
        if self.main_card_bouts > self.eligible_bouts:
            raise ValueError("main-card bouts cannot exceed eligible bouts")
        if self.prelim_bouts > self.eligible_bouts:
            raise ValueError("prelim bouts cannot exceed eligible bouts")
        if self.title_bouts > self.eligible_bouts:
            raise ValueError("title bouts cannot exceed eligible bouts")

    @property
    def offer_fingerprint(self) -> str:
        """Todo lo que decide QUÉ misiones puede ofrecer esta card.

        Excluye `card_revision` a propósito: usarla como key redibujaría
        las misiones por un cambio cosmético, rompiendo INT-001.
        """
        payload = "\x1f".join(
            (
                str(self.event_id),
                str(self.eligible_bouts),
                str(self.main_card_bouts),
                str(self.prelim_bouts),
                str(self.title_bouts),
                ",".join(sorted(capability.value for capability in self.capabilities)),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def bout_is_live(bout: dict) -> bool:
    """Un bout que la card sigue contando: no cancelado, pospuesto ni reemplazado."""
    sidecar = bout.get("card_data_v1") or {}
    lifecycle = str(sidecar.get("lifecycle") or "").upper()
    if lifecycle:
        return lifecycle in {"SCHEDULED", "COMPLETED"}
    return str(bout.get("status") or "scheduled").lower() not in {
        "cancelled",
        "postponed",
        "replaced",
    }


def bout_section(bout: dict) -> str:
    sidecar = bout.get("card_data_v1") or {}
    return str(sidecar.get("section") or bout.get("section") or "PRELIM").upper()


def bout_is_title(bout: dict) -> bool:
    sidecar = bout.get("card_data_v1") or {}
    if "is_title_fight" in sidecar:
        return bool(sidecar["is_title_fight"])
    return bool(bout.get("is_title_fight"))


def card_revision_of(event: dict) -> int:
    """Los eventos legacy no traen revisión; 1 mantiene a todos sincronizados."""
    sidecar = event.get("card_data_v1") or {}
    return int(
        sidecar.get(
            "card_revision",
            sidecar.get("structure_revision", event.get("card_revision", 1)),
        )
    )


def frozen_card_facts(event: dict, bouts: Iterable[dict]) -> FrozenCardFacts:
    """La card tal como la ve la capa de ofertas.

    Compartida y no duplicada: el read model y selección la derivan igual,
    si contaran distinto se le decía al usuario que su card cambió sin ser cierto.
    """
    live = [bout for bout in bouts if bout_is_live(bout)]
    sections = [bout_section(bout) for bout in live]
    main = sum(1 for section in sections if section == "MAIN")
    prelim = sum(1 for section in sections if section in {"PRELIM", "EARLY_PRELIM"})
    titles = sum(1 for bout in live if bout_is_title(bout))

    capabilities = {CardCapability.CANONICAL_CARD, CardCapability.PICK_POINTS}
    if main or prelim:
        capabilities.add(CardCapability.SECTION_ORDER)
    if live:
        capabilities.add(CardCapability.MAIN_EVENT)
    if len(live) > 1:
        capabilities.add(CardCapability.CO_MAIN)
    if titles:
        capabilities.add(CardCapability.TITLE_BOUTS)
    capabilities.add(CardCapability.RESULT_METHOD)
    capabilities.add(CardCapability.RESULT_ROUND)
    capabilities.add(CardCapability.LEADERBOARD)

    return FrozenCardFacts(
        event_id=int(event["id"]),
        card_revision=card_revision_of(event),
        eligible_bouts=len(live),
        main_card_bouts=main,
        prelim_bouts=prelim,
        title_bouts=titles,
        capabilities=frozenset(capabilities),
    )


def canonical_eligible_bout_count(
    event: dict, bouts: Iterable[dict]
) -> int | None:
    """El denominador contra el que se mide todo target de card prop.

    Selección y oferta lo leen del mismo lugar para no prometer un número
    distinto del guardado. `None` si la card aún no tiene conteo canónico.
    """
    sidecar = event.get("card_data_v1") or {}
    eligibility = sidecar.get("current_eligibility") or {}
    value = eligibility.get(
        "denominator",
        sidecar.get("mission_eligible_bout_count"),
    )
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    fallback = sum(
        bout.get("status") not in {"cancelled", "postponed", "replaced"}
        for bout in bouts
    )
    return fallback if fallback >= 1 else None


@dataclass(frozen=True)
class EligibilityFailure:
    code: EligibilityFailureCode
    field: str
    required: int | str
    actual: int | str


@dataclass(frozen=True)
class EligibilityDecision:
    mission_id: str
    card_revision: int
    failures: tuple[EligibilityFailure, ...]

    @property
    def eligible(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class OverlapDecision:
    left_mission_id: str
    right_mission_id: str
    shared_tags: frozenset[str]
    conflicts: bool


@dataclass(frozen=True)
class MissionOverlapPolicy:
    """Dos ofertas chocan si comparten más tags de los que el presupuesto permite."""

    max_shared_tags: int = 1

    def __post_init__(self) -> None:
        if self.max_shared_tags < 0:
            raise ValueError("max_shared_tags cannot be negative")

    def compare(
        self,
        left: MissionDefinition,
        right: MissionDefinition,
    ) -> OverlapDecision:
        shared = left.overlap_tags & right.overlap_tags
        return OverlapDecision(
            left_mission_id=left.mission_id,
            right_mission_id=right.mission_id,
            shared_tags=shared,
            conflicts=(
                left.mission_id == right.mission_id
                or len(shared) > self.max_shared_tags
            ),
        )


def evaluate_definition_eligibility(
    definition: MissionDefinition,
    card: FrozenCardFacts,
) -> EligibilityDecision:
    requirement = definition.eligibility
    failures: list[EligibilityFailure] = []

    for capability in sorted(
        requirement.capabilities - card.capabilities,
        key=lambda value: value.value,
    ):
        failures.append(
            EligibilityFailure(
                code=EligibilityFailureCode.MISSING_CAPABILITY,
                field="capabilities",
                required=capability.value,
                actual="MISSING",
            )
        )

    count_rules = (
        (
            "eligible_bouts",
            requirement.min_eligible_bouts,
            card.eligible_bouts,
            EligibilityFailureCode.INSUFFICIENT_ELIGIBLE_BOUTS,
        ),
        (
            "main_card_bouts",
            requirement.min_main_card_bouts,
            card.main_card_bouts,
            EligibilityFailureCode.INSUFFICIENT_MAIN_CARD_BOUTS,
        ),
        (
            "prelim_bouts",
            requirement.min_prelim_bouts,
            card.prelim_bouts,
            EligibilityFailureCode.INSUFFICIENT_PRELIM_BOUTS,
        ),
        (
            "title_bouts",
            requirement.min_title_bouts,
            card.title_bouts,
            EligibilityFailureCode.INSUFFICIENT_TITLE_BOUTS,
        ),
    )
    for field, required, actual, code in count_rules:
        if actual < required:
            failures.append(
                EligibilityFailure(
                    code=code,
                    field=field,
                    required=required,
                    actual=actual,
                )
            )

    return EligibilityDecision(
        mission_id=definition.mission_id,
        card_revision=card.card_revision,
        failures=tuple(failures),
    )


def eligible_definitions(
    definitions: tuple[MissionDefinition, ...],
    card: FrozenCardFacts,
) -> tuple[MissionDefinition, ...]:
    return tuple(
        definition
        for definition in definitions
        if evaluate_definition_eligibility(definition, card).eligible
    )
