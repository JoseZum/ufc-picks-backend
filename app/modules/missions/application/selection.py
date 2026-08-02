"""Irreversible mission selection and mission-to-pick synchronization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import ceil

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.modules.missions.domain.catalog import MissionCatalog, MissionCatalogError
from app.modules.missions.domain.definitions import (
    AutoMissionDefinition,
    CardPropInput,
    CardPropMissionDefinition,
    CardPropTargetSource,
    ComboBuilderMissionDefinition,
    ComboLegTarget,
    PickField,
    TargetFighterMissionDefinition,
    TargetFightMissionDefinition,
    WinMethod,
    WinnerBinding,
)
from app.modules.missions.domain.enums import MissionAssignmentStatus, StringEnum
from app.modules.missions.domain.selections import (
    AutoMissionSelection,
    CanonicalPickPatch,
    CardPropMissionSelection,
    ComboBuilderMissionSelection,
    FighterCorner,
    SelectMissionCommand,
    TargetFighterMissionSelection,
    TargetFightMissionSelection,
)
from app.services.pick_lock_service import evaluate_bout_pick_lock


class MissionSelectionErrorCode(StringEnum):
    OFFER_NOT_FOUND = "OFFER_NOT_FOUND"
    ALREADY_SELECTED = "ALREADY_SELECTED"
    CARD_LOCKED = "CARD_LOCKED"
    STALE_CARD = "STALE_CARD"
    INVALID_SELECTION = "INVALID_SELECTION"
    PICK_LOCKED = "PICK_LOCKED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class MissionSelectionError(ValueError):
    def __init__(self, code: MissionSelectionErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MissionSelectionResult:
    assignment_id: str
    mission_id: str
    event_id: int
    slot: int
    status: str
    assignment_revision: int
    linked_pick_ids: tuple[str, ...]


@dataclass(frozen=True)
class _PickBinding:
    bout_id: int
    winner_corner: FighterCorner
    method: WinMethod | None = None
    round: int | None = None


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_name(value: str) -> str:
    return " ".join(value.lower().strip().split())


def _other_corner(corner: FighterCorner) -> FighterCorner:
    return FighterCorner.BLUE if corner == FighterCorner.RED else FighterCorner.RED


def _fighters(bout: dict) -> dict[FighterCorner, str]:
    sidecar_fighters = (bout.get("card_data_v1") or {}).get("fighters") or ()
    canonical = {
        FighterCorner(str(fighter.get("corner")).lower()): fighter.get("display_name")
        for fighter in sidecar_fighters
        if isinstance(fighter, dict)
        and str(fighter.get("corner", "")).lower() in {"red", "blue"}
        and isinstance(fighter.get("display_name"), str)
        and fighter["display_name"].strip()
    }
    if set(canonical) == {FighterCorner.RED, FighterCorner.BLUE}:
        return canonical

    fighters = bout.get("fighters") or {}
    names = {
        FighterCorner.RED: (fighters.get("red") or {}).get("fighter_name", ""),
        FighterCorner.BLUE: (fighters.get("blue") or {}).get("fighter_name", ""),
    }
    if not all(names.values()):
        raise MissionSelectionError(
            MissionSelectionErrorCode.INVALID_SELECTION,
            f"Bout {bout.get('id')} does not expose both canonical fighters",
        )
    return names


def _fighter_ids(bout: dict) -> dict[FighterCorner, str]:
    """Return stable CardData fighter IDs when the canonical sidecar has them."""

    sidecar_fighters = (bout.get("card_data_v1") or {}).get("fighters") or ()
    by_corner = {
        FighterCorner(str(fighter.get("corner")).lower()): fighter.get("fighter_id")
        for fighter in sidecar_fighters
        if isinstance(fighter, dict)
        and str(fighter.get("corner", "")).lower() in {"red", "blue"}
        and isinstance(fighter.get("fighter_id"), str)
        and fighter["fighter_id"].strip()
    }
    if set(by_corner) == {FighterCorner.RED, FighterCorner.BLUE}:
        return by_corner

    legacy_fighters = bout.get("fighters") or {}
    fallback = {
        corner: (legacy_fighters.get(corner.value.lower()) or {}).get("fighter_id")
        for corner in (FighterCorner.RED, FighterCorner.BLUE)
    }
    return {
        corner: fighter_id
        for corner, fighter_id in fallback.items()
        if isinstance(fighter_id, str) and fighter_id.strip()
    }


def _pick_method(method: WinMethod) -> str:
    return {
        WinMethod.KO_TKO: "KO/TKO",
        WinMethod.SUBMISSION: "SUB",
        WinMethod.DECISION: "DEC",
    }[method]


def _domain_method(method: str | None) -> WinMethod | None:
    return {
        "KO/TKO": WinMethod.KO_TKO,
        "SUB": WinMethod.SUBMISSION,
        "DEC": WinMethod.DECISION,
    }.get(method)


def _assignment_id(user_id: str, command: SelectMissionCommand) -> str:
    value = f"{user_id}\x1f{command.event_id}\x1f{command.slot}\x1f{command.offer_id}"
    return f"assignment_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _request_hash(user_id: str, command: SelectMissionCommand) -> str:
    payload = {"user_id": user_id, **command.model_dump(mode="json")}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _receipt_id(user_id: str, idempotency_key: str) -> str:
    value = f"{user_id}\x1f{idempotency_key}"
    return f"command_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _result_from_document(value: dict) -> MissionSelectionResult:
    return MissionSelectionResult(
        **{
            **value,
            "linked_pick_ids": tuple(value.get("linked_pick_ids", ())),
        }
    )


class MissionSelectionService:
    def __init__(
        self,
        db: AsyncDatabase,
        catalog: MissionCatalog,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self.db = db
        self.catalog = catalog
        self.clock = clock

    async def select(
        self,
        *,
        user_id: str,
        command: SelectMissionCommand,
    ) -> MissionSelectionResult:
        request_hash = _request_hash(user_id, command)
        receipt_id = _receipt_id(user_id, command.idempotency_key)

        async def callback(session: AsyncClientSession) -> MissionSelectionResult:
            existing_receipt = await self.db["mission_command_receipts"].find_one(
                {"_id": receipt_id},
                session=session,
            )
            if existing_receipt:
                if existing_receipt["request_hash"] != request_hash:
                    raise MissionSelectionError(
                        MissionSelectionErrorCode.IDEMPOTENCY_CONFLICT,
                        "Idempotency key was already used with another request",
                    )
                return _result_from_document(existing_receipt["result"])

            offer_set = await self.db["mission_offer_sets"].find_one(
                {
                    "_id": command.offer_set_id,
                    "user_id": user_id,
                    "event_id": command.event_id,
                },
                session=session,
            )
            if not offer_set:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.OFFER_NOT_FOUND,
                    "Mission offer set was not found for this user and event",
                )
            if offer_set.get("catalog_version") != self.catalog.version:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.OFFER_NOT_FOUND,
                    "Mission offer set uses an unavailable catalog version",
                )

            selected_offer = self._find_offer(offer_set, command)
            try:
                definition = self.catalog.get(selected_offer["mission_id"])
            except MissionCatalogError as exc:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.OFFER_NOT_FOUND,
                    "Mission definition is unavailable",
                ) from exc
            if command.selection.kind != definition.interaction:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "Selection type does not match the mission interaction",
                )

            existing_assignment = await self.db["mission_assignments"].find_one(
                {
                    "user_id": user_id,
                    "event_id": command.event_id,
                    "slot": command.slot,
                },
                session=session,
            )
            if existing_assignment:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.ALREADY_SELECTED,
                    "This mission slot already has an irreversible selection",
                )

            event = await self.db["events"].find_one(
                {"id": command.event_id},
                session=session,
            )
            if not event:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.STALE_CARD,
                    "Mission card no longer exists",
                )
            bouts_cursor = self.db["bouts"].find(
                {"event_id": command.event_id},
                session=session,
            )
            bouts = await bouts_cursor.to_list(length=None)
            control = await self.db["mission_card_controls"].find_one(
                {"event_id": command.event_id},
                session=session,
            )
            self._ensure_card_open(event, bouts, offer_set, control)
            bouts_by_id = {bout["id"]: bout for bout in bouts}

            resolved_selection, bindings = self._resolve_selection(
                definition,
                command,
                bouts_by_id,
                event,
            )
            eligibility_snapshot = self._freeze_eligibility(event, bouts_by_id)
            assignment_id = _assignment_id(user_id, command)
            now = self.clock()
            linked_pick_ids = await self._upsert_bound_picks(
                user_id=user_id,
                event_id=command.event_id,
                assignment_id=assignment_id,
                bindings=bindings,
                patches=command.pick_patches,
                bouts_by_id=bouts_by_id,
                now=now,
                session=session,
            )
            assignment = {
                "_id": assignment_id,
                "user_id": user_id,
                "event_id": command.event_id,
                "slot": command.slot,
                "offer_set_id": command.offer_set_id,
                "offer_id": command.offer_id,
                "mission_id": definition.mission_id,
                "catalog_version": definition.catalog_version,
                "card_revision": offer_set["card_revision"],
                "eligibility_snapshot": eligibility_snapshot,
                "definition_snapshot": definition.model_dump(mode="json"),
                "selection": resolved_selection,
                "status": MissionAssignmentStatus.ACTIVE.value,
                "xp": definition.xp,
                "progress": {},
                "linked_pick_ids": list(linked_pick_ids),
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            await self.db["mission_assignments"].insert_one(
                assignment,
                session=session,
            )
            result = MissionSelectionResult(
                assignment_id=assignment_id,
                mission_id=definition.mission_id,
                event_id=command.event_id,
                slot=command.slot,
                status=MissionAssignmentStatus.ACTIVE.value,
                assignment_revision=1,
                linked_pick_ids=linked_pick_ids,
            )
            await self.db["mission_command_receipts"].insert_one(
                {
                    "_id": receipt_id,
                    "actor_id": user_id,
                    "idempotency_key": command.idempotency_key,
                    "request_hash": request_hash,
                    "result": asdict(result),
                    "created_at": now,
                },
                session=session,
            )
            return result

        try:
            async with self.db.client.start_session() as session:
                return await session.with_transaction(callback)
        except DuplicateKeyError as exc:
            receipt = await self.db["mission_command_receipts"].find_one(
                {"_id": receipt_id}
            )
            if receipt and receipt.get("request_hash") == request_hash:
                return _result_from_document(receipt["result"])
            raise MissionSelectionError(
                MissionSelectionErrorCode.ALREADY_SELECTED,
                "This mission slot already has an irreversible selection",
            ) from exc

    @staticmethod
    def _find_offer(offer_set: dict, command: SelectMissionCommand) -> dict:
        slot = next(
            (
                candidate
                for candidate in offer_set.get("slots", [])
                if candidate.get("slot") == command.slot
            ),
            None,
        )
        offer = next(
            (
                candidate
                for candidate in (slot or {}).get("offers", [])
                if candidate.get("offer_id") == command.offer_id
            ),
            None,
        )
        if not offer:
            raise MissionSelectionError(
                MissionSelectionErrorCode.OFFER_NOT_FOUND,
                "Mission offer does not belong to the requested slot",
            )
        return offer

    def _ensure_card_open(
        self,
        event: dict,
        bouts: list[dict],
        offer_set: dict,
        control: dict | None,
    ) -> None:
        sidecar = event.get("card_data_v1") or {}
        # Legacy events carry no card_revision at all. Defaulting to 1 keeps this
        # in step with MissionReadService._card_facts; leaving it None made every
        # selection on such an event fail as STALE_CARD.
        current_revision = sidecar.get(
            "card_revision",
            sidecar.get("structure_revision", event.get("card_revision", 1)),
        )
        if current_revision != offer_set.get("card_revision"):
            raise MissionSelectionError(
                MissionSelectionErrorCode.STALE_CARD,
                "Card revision changed after these offers were generated",
            )
        if control and control.get("state") != "OPEN":
            raise MissionSelectionError(
                MissionSelectionErrorCode.CARD_LOCKED,
                "Card missions are closed by admin control",
            )
        if event.get("status") in {"completed", "cancelled"}:
            raise MissionSelectionError(
                MissionSelectionErrorCode.CARD_LOCKED,
                "Card missions are locked by event lifecycle",
            )
        if any(
            bool(bout.get("result") or (bout.get("card_data_v1") or {}).get("result"))
            for bout in bouts
        ):
            raise MissionSelectionError(
                MissionSelectionErrorCode.CARD_LOCKED,
                "Card missions locked when the first result was registered",
            )
        now = self.clock()
        for bout in bouts:
            if bout.get("status") == "cancelled":
                continue
            lock = evaluate_bout_pick_lock(event, bout, now=now)
            if lock.locked and lock.reason in {"admin_event", "section_time"}:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.CARD_LOCKED,
                    "Card missions are locked because card picks are closed",
                )

    def _resolve_selection(
        self,
        definition,
        command: SelectMissionCommand,
        bouts_by_id: dict[int, dict],
        event: dict,
    ) -> tuple[dict, tuple[_PickBinding, ...]]:
        selection = command.selection
        if isinstance(definition, AutoMissionDefinition):
            if not isinstance(selection, AutoMissionSelection):
                return self._invalid_type()
            return selection.model_dump(mode="json"), ()
        if isinstance(definition, TargetFighterMissionDefinition):
            if not isinstance(selection, TargetFighterMissionSelection):
                return self._invalid_type()
            return self._resolve_target_fighter(
                definition,
                selection,
                bouts_by_id,
                event,
            )
        if isinstance(definition, TargetFightMissionDefinition):
            if not isinstance(selection, TargetFightMissionSelection):
                return self._invalid_type()
            bout = self._selectable_bout(selection.bout_id, bouts_by_id, event)
            if (
                definition.selection.required_round is not None
                and definition.selection.required_round > self._scheduled_rounds(bout)
            ):
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "Mission round exceeds the selected bout schedule",
                )
            return {
                **selection.model_dump(mode="json"),
                "bout_ids": [selection.bout_id],
            }, ()
        if isinstance(definition, ComboBuilderMissionDefinition):
            if not isinstance(selection, ComboBuilderMissionSelection):
                return self._invalid_type()
            return self._resolve_combo(definition, selection, bouts_by_id, event)
        if isinstance(definition, CardPropMissionDefinition):
            if not isinstance(selection, CardPropMissionSelection):
                return self._invalid_type()
            return self._resolve_card_prop(
                definition,
                selection,
                bouts_by_id,
                event,
            ), ()
        return self._invalid_type()

    def _resolve_target_fighter(
        self,
        definition: TargetFighterMissionDefinition,
        selection: TargetFighterMissionSelection,
        bouts_by_id: dict[int, dict],
        event: dict,
    ) -> tuple[dict, tuple[_PickBinding, ...]]:
        bout = self._selectable_bout(selection.bout_id, bouts_by_id, event)
        names = _fighters(bout)
        if definition.selection.title_bouts_only and not self._is_title_bout(bout):
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "This mission requires a title bout",
            )
        method = selection.method
        if PickField.METHOD in definition.selection.bound_pick_fields:
            if method is None and len(definition.selection.allowed_methods) == 1:
                method = next(iter(definition.selection.allowed_methods))
            if method not in definition.selection.allowed_methods:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "Selected method is not allowed by this mission",
                )
        elif method is not None:
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "This mission does not bind a method",
            )
        round_ = selection.round
        if PickField.ROUND in definition.selection.bound_pick_fields:
            if round_ is None and len(definition.selection.allowed_rounds) == 1:
                round_ = definition.selection.allowed_rounds[0]
            if round_ not in definition.selection.allowed_rounds:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "Selected round is not allowed by this mission",
                )
        elif round_ is not None:
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "This mission does not bind a round",
            )
        winner_corner = (
            selection.corner
            if definition.selection.winner_binding == WinnerBinding.SELECTED_FIGHTER
            else _other_corner(selection.corner)
        )
        resolved = {
            **selection.model_dump(mode="json"),
            "selected_fighter_name": names[selection.corner],
            "canonical_winner_name": names[winner_corner],
            "method": method.value if method else None,
            "round": round_,
            "bout_ids": [selection.bout_id],
        }
        fighter_ids = _fighter_ids(bout)
        if selection.corner in fighter_ids:
            resolved["selected_fighter_id"] = fighter_ids[selection.corner]
        if winner_corner in fighter_ids:
            resolved["canonical_winner_id"] = fighter_ids[winner_corner]
        return resolved, (
            _PickBinding(
                bout_id=selection.bout_id,
                winner_corner=winner_corner,
                method=method,
                round=round_,
            ),
        )

    @staticmethod
    def _invalid_type():
        raise MissionSelectionError(
            MissionSelectionErrorCode.INVALID_SELECTION,
            "Selection type does not match the mission definition",
        )

    def _resolve_combo(
        self,
        definition: ComboBuilderMissionDefinition,
        selection: ComboBuilderMissionSelection,
        bouts_by_id: dict[int, dict],
        event: dict,
    ) -> tuple[dict, tuple[_PickBinding, ...]]:
        requested = {leg.key: leg for leg in selection.legs}
        expected = {leg.key: leg for leg in definition.selection.legs}
        if len(requested) != len(selection.legs) or requested.keys() != expected.keys():
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "Combo legs do not match the mission definition",
            )
        if definition.selection.distinct_bouts:
            bout_ids = [leg.bout_id for leg in selection.legs]
            if len(bout_ids) != len(set(bout_ids)):
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "Combo legs must target distinct bouts",
                )
        bindings: list[_PickBinding] = []
        resolved_legs: list[dict] = []
        selected_methods: list[WinMethod] = []
        for key, leg_definition in expected.items():
            selected = requested[key]
            bout = self._selectable_bout(selected.bout_id, bouts_by_id, event)
            resolved = selected.model_dump(mode="json")
            if leg_definition.target == ComboLegTarget.FIGHTER:
                if selected.corner is None:
                    raise MissionSelectionError(
                        MissionSelectionErrorCode.INVALID_SELECTION,
                        f"Combo leg {key} requires a fighter corner",
                    )
                if definition.selection.title_bouts_only and not self._is_title_bout(
                    bout
                ):
                    raise MissionSelectionError(
                        MissionSelectionErrorCode.INVALID_SELECTION,
                        f"Combo leg {key} requires a title bout",
                    )
                if leg_definition.method is not None:
                    if selected.method is not None:
                        raise MissionSelectionError(
                            MissionSelectionErrorCode.INVALID_SELECTION,
                            f"Combo leg {key} has a fixed method",
                        )
                    method = leg_definition.method
                elif leg_definition.allowed_methods:
                    if selected.method not in leg_definition.allowed_methods:
                        raise MissionSelectionError(
                            MissionSelectionErrorCode.INVALID_SELECTION,
                            f"Combo leg {key} requires an allowed method",
                        )
                    method = selected.method
                    assert method is not None
                    selected_methods.append(method)
                else:
                    if selected.method is not None:
                        raise MissionSelectionError(
                            MissionSelectionErrorCode.INVALID_SELECTION,
                            f"Combo leg {key} does not bind a method",
                        )
                    method = None
                if (
                    leg_definition.round is not None
                    and leg_definition.round > self._scheduled_rounds(bout)
                ):
                    raise MissionSelectionError(
                        MissionSelectionErrorCode.INVALID_SELECTION,
                        f"Round {leg_definition.round} exceeds the scheduled rounds "
                        f"for bout {selected.bout_id}",
                    )
                names = _fighters(bout)
                resolved["fighter_name"] = names[selected.corner]
                fighter_ids = _fighter_ids(bout)
                if selected.corner in fighter_ids:
                    resolved["fighter_id"] = fighter_ids[selected.corner]
                resolved["method"] = method.value if method else None
                resolved["round"] = leg_definition.round
                bindings.append(
                    _PickBinding(
                        bout_id=selected.bout_id,
                        winner_corner=selected.corner,
                        method=method,
                        round=leg_definition.round,
                    )
                )
            elif selected.corner is not None or selected.method is not None:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    f"Combo fight leg {key} cannot select a fighter or method",
                )
            resolved_legs.append(resolved)
        if definition.selection.distinct_methods and len(selected_methods) != len(
            set(selected_methods)
        ):
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "Combo legs must use distinct methods",
            )
        return {
            "kind": selection.kind.value,
            "legs": resolved_legs,
            "bout_ids": [leg["bout_id"] for leg in resolved_legs],
        }, tuple(bindings)

    def _resolve_card_prop(
        self,
        definition: CardPropMissionDefinition,
        selection: CardPropMissionSelection,
        bouts_by_id: dict[int, dict],
        event: dict,
    ) -> dict:
        spec = definition.selection
        eligible_count = self._eligible_bout_count(event, bouts_by_id)
        if spec.input == CardPropInput.ACCEPT:
            valid = selection.choice is None and selection.exact_count is None
        elif spec.input == CardPropInput.CHOICE:
            valid = selection.choice in spec.choices and selection.exact_count is None
        else:
            valid = (
                selection.choice is None
                and selection.exact_count is not None
                and selection.exact_count <= eligible_count
                and (spec.max_count is None or selection.exact_count <= spec.max_count)
            )
        if not valid:
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "Card-prop selection does not match the offered input",
            )
        resolved = {
            **selection.model_dump(mode="json"),
            "eligible_bout_count": eligible_count,
            "frozen_target": None,
            "frozen_max_count": None,
        }
        if spec.target_source == CardPropTargetSource.FROZEN_ELIGIBLE_RATIO:
            assert spec.frozen_ratio is not None
            resolved["frozen_target"] = ceil(eligible_count * spec.frozen_ratio)
        elif spec.target_source == CardPropTargetSource.SELECTED_EXACT_COUNT:
            resolved["frozen_max_count"] = eligible_count
        return resolved

    @staticmethod
    def _eligible_bout_count(event: dict, bouts_by_id: dict[int, dict]) -> int:
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
            for bout in bouts_by_id.values()
        )
        if fallback < 1:
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "Card has no canonical eligible bout count",
            )
        return fallback

    @staticmethod
    def _freeze_eligibility(event: dict, bouts_by_id: dict[int, dict]) -> dict:
        sidecar = event.get("card_data_v1") or {}
        current = sidecar.get("current_eligibility") or {}
        targets = []
        for raw in current.get("eligible_targets") or ():
            if not isinstance(raw, dict):
                continue
            bout_id = raw.get("bout_id")
            matchup_revision = raw.get("matchup_revision")
            if (
                isinstance(bout_id, int)
                and not isinstance(bout_id, bool)
                and bout_id in bouts_by_id
                and isinstance(matchup_revision, int)
                and not isinstance(matchup_revision, bool)
                and matchup_revision >= 1
            ):
                targets.append(
                    {"bout_id": bout_id, "matchup_revision": matchup_revision}
                )

        if not targets:
            for bout_id, bout in sorted(bouts_by_id.items()):
                if bout.get("status") in {"cancelled", "postponed", "replaced"}:
                    continue
                bout_sidecar = bout.get("card_data_v1") or {}
                revision = bout_sidecar.get("matchup_revision", 1)
                if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                    revision = 1
                targets.append(
                    {"bout_id": bout_id, "matchup_revision": revision}
                )

        serialized = json.dumps(targets, sort_keys=True, separators=(",", ":"))
        fallback_fingerprint = f"sha256:{hashlib.sha256(serialized.encode()).hexdigest()}"
        revision = current.get(
            "card_snapshot_revision",
            sidecar.get(
                "structure_revision",
                sidecar.get("card_revision", event.get("card_revision", 1)),
            ),
        )
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            revision = 1
        fingerprint = current.get("fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) < 8:
            fingerprint = fallback_fingerprint
        return {
            "eligibility_snapshot_id": current.get("eligibility_snapshot_id"),
            "eligibility_revision": revision,
            "fingerprint": fingerprint,
            "eligible_targets": targets,
            "denominator": len(targets),
        }

    def _selectable_bout(
        self,
        bout_id: int,
        bouts_by_id: dict[int, dict],
        event: dict,
    ) -> dict:
        bout = bouts_by_id.get(bout_id)
        if (
            not bout
            or bout.get("status") in {"cancelled", "completed"}
            or bout.get("result")
        ):
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                f"Bout {bout_id} is not selectable for this mission",
            )
        lock = evaluate_bout_pick_lock(event, bout, now=self.clock())
        if lock.locked:
            raise MissionSelectionError(
                MissionSelectionErrorCode.PICK_LOCKED,
                f"Bout {bout_id} picks are locked",
            )
        return bout

    @staticmethod
    def _is_title_bout(bout: dict) -> bool:
        sidecar = bout.get("card_data_v1") or {}
        return bool(sidecar.get("is_title_fight", bout.get("is_title_fight", False)))

    async def _upsert_bound_picks(
        self,
        *,
        user_id: str,
        event_id: int,
        assignment_id: str,
        bindings: tuple[_PickBinding, ...],
        patches: tuple[CanonicalPickPatch, ...],
        bouts_by_id: dict[int, dict],
        now: datetime,
        session: AsyncClientSession,
    ) -> tuple[str, ...]:
        patches_by_bout = {patch.bout_id: patch for patch in patches}
        bound_ids = {binding.bout_id for binding in bindings}
        if set(patches_by_bout) - bound_ids:
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                "Pick patches may only target mission-bound bouts",
            )
        if not bindings:
            if patches:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "This mission does not modify canonical picks",
                )
            return ()

        linked: list[str] = []
        for binding in bindings:
            bout = bouts_by_id[binding.bout_id]
            names = _fighters(bout)
            pick_id = f"{user_id}:{binding.bout_id}"
            existing = await self.db["picks"].find_one(
                {"_id": pick_id},
                session=session,
            )
            patch = patches_by_bout.get(binding.bout_id)
            if patch and patch.winner_corner not in {None, binding.winner_corner}:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "Pick winner conflicts with the mission-bound fighter",
                )
            method = binding.method
            if patch and patch.method is not None:
                if method is not None and patch.method != method:
                    raise MissionSelectionError(
                        MissionSelectionErrorCode.INVALID_SELECTION,
                        "Pick method conflicts with the mission-bound method",
                    )
                method = patch.method
            if method is None and existing:
                method = _domain_method(existing.get("picked_method"))
            if method is None:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "A complete canonical pick method is required",
                )

            round_ = binding.round
            if patch and patch.round is not None:
                if round_ is not None and patch.round != round_:
                    raise MissionSelectionError(
                        MissionSelectionErrorCode.INVALID_SELECTION,
                        "Pick round conflicts with the mission-bound round",
                    )
                round_ = patch.round
            if round_ is None and existing and method != WinMethod.DECISION:
                round_ = existing.get("picked_round")
            if method == WinMethod.DECISION:
                if patch and patch.round is not None:
                    raise MissionSelectionError(
                        MissionSelectionErrorCode.INVALID_SELECTION,
                        "Decision picks cannot include a round",
                    )
                round_ = None
            elif round_ is None:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    "A complete finish pick requires a round",
                )
            elif round_ is not None and round_ > self._scheduled_rounds(bout):
                raise MissionSelectionError(
                    MissionSelectionErrorCode.INVALID_SELECTION,
                    f"Round {round_} exceeds the scheduled rounds for bout "
                    f"{binding.bout_id}",
                )

            final_winner = names[binding.winner_corner]
            stable_ids = _fighter_ids(bout)
            final_method = _pick_method(method)
            locks = (existing or {}).get("mission_field_locks") or {}
            if locks.get("winner") and _normalize_name(
                existing.get("picked_fighter_name", "")
            ) != _normalize_name(final_winner):
                raise MissionSelectionError(
                    MissionSelectionErrorCode.PICK_LOCKED,
                    "Winner is bound by another active mission",
                )
            if locks.get("method") and existing.get("picked_method") != final_method:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.PICK_LOCKED,
                    "Method is bound by another active mission",
                )
            if locks.get("round") and existing.get("picked_round") != round_:
                raise MissionSelectionError(
                    MissionSelectionErrorCode.PICK_LOCKED,
                    "Round is bound by another active mission",
                )

            add_to_sets = {"mission_assignment_ids": assignment_id}
            add_to_sets["mission_field_locks.winner"] = assignment_id
            if binding.method is not None:
                add_to_sets["mission_field_locks.method"] = assignment_id
            if binding.round is not None:
                add_to_sets["mission_field_locks.round"] = assignment_id

            document = await self.db["picks"].find_one_and_update(
                {"_id": pick_id},
                {
                    "$set": {
                        "user_id": user_id,
                        "event_id": event_id,
                        "bout_id": binding.bout_id,
                        "picked_fighter_name": final_winner,
                        **(
                            {"picked_fighter_id": stable_ids[binding.winner_corner]}
                            if binding.winner_corner in stable_ids
                            else {}
                        ),
                        "picked_method": final_method,
                        "picked_round": round_,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                        "is_correct": None,
                        "points_awarded": 0,
                        "locked": False,
                    },
                    "$inc": {"revision": 1},
                    "$addToSet": add_to_sets,
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            linked.append(document["_id"])
        return tuple(linked)

    @staticmethod
    def _scheduled_rounds(bout: dict) -> int:
        sidecar = bout.get("card_data_v1") or {}
        value = sidecar.get(
            "scheduled_rounds",
            bout.get("rounds_scheduled", 5 if bout.get("is_main_event") else 3),
        )
        if isinstance(value, bool) or value not in {3, 5}:
            raise MissionSelectionError(
                MissionSelectionErrorCode.INVALID_SELECTION,
                f"Bout {bout.get('id')} has no valid scheduled-round authority",
            )
        return value
