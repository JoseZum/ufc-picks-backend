"""Presentation-ready mission read models for the HTTP boundary.

Everything a surface renders is resolved here: progress copy, percent, lock and
VOID reasons, XP and eligibility. The client receives finished strings and never
recomputes a domain rule (D-ARCH-011, roadmap rule 8).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pymongo.asynchronous.database import AsyncDatabase

from app.modules.missions.application.card_streak import CardStreakService
from app.modules.missions.application.monthly_config import MonthlyConfigService
from app.modules.missions.application.progression import ProgressionService
from app.modules.missions.catalog import load_card_catalog, load_monthly_catalog
from app.modules.missions.contracts import (
    HomeMissionsResponse,
    MissionOfferView,
    MissionSlotView,
    MonthlyMissionView,
    ProfileMissionsResponse,
    SelectedMissionView,
    SelectionPartView,
    StreakCardView,
)
from app.modules.missions.domain.definitions import CardCapability
from app.modules.missions.domain.eligibility import FrozenCardFacts
from app.modules.missions.domain.enums import MonthlyConfigState
from app.modules.missions.domain.monthly import month_key_for
from app.modules.missions.domain.monthly_metrics import (
    MonthlyEvaluationContext,
    evaluate_monthly_metric,
    resolve_monthly_observation,
)
from app.modules.missions.domain.offers import (
    OfferGenerationError,
    generate_mission_offers,
    generated_offer_set_document,
)
from app.modules.missions.domain.streak import next_milestone

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _section(bout: dict) -> str:
    sidecar = bout.get("card_data_v1") or {}
    return str(sidecar.get("section") or bout.get("section") or "PRELIM").upper()


def _is_title(bout: dict) -> bool:
    sidecar = bout.get("card_data_v1") or {}
    if "is_title_fight" in sidecar:
        return bool(sidecar["is_title_fight"])
    return bool(bout.get("is_title_fight"))


_METHOD_DISPLAY = {
    "KO_TKO": "KO/TKO",
    "SUBMISSION": "Submission",
    "DECISION": "Decision",
}


def _display_method(value: object) -> str:
    """Ortografía de pantalla para un método; cualquier otra cosa pasa igual.

    El enum interno se escribe `KO_TKO`, y esa cadena estaba llegando tal cual
    a la tarjeta de misión. Es el mismo par de vocabularios que el frontend ya
    resuelve para los pickers, aplicado aquí porque esta frase la construye el
    backend entera.
    """
    text = str(value)
    return _METHOD_DISPLAY.get(text.upper(), text)


def _milestone_label(current: int) -> str:
    """The finished copy the Profile renders, e.g. "5 → +3 XP"."""
    length, bonus = next_milestone(current)
    return f"{length} → +{bonus} XP"


def _is_live(bout: dict) -> bool:
    sidecar = bout.get("card_data_v1") or {}
    lifecycle = str(sidecar.get("lifecycle") or "").upper()
    if lifecycle:
        return lifecycle in {"SCHEDULED", "COMPLETED"}
    return str(bout.get("status") or "scheduled").lower() not in {
        "cancelled",
        "postponed",
        "replaced",
    }


class MissionReadService:
    """Assembles Home and Profile payloads from persisted mission state."""

    def __init__(
        self,
        db: AsyncDatabase,
        *,
        offer_secret: bytes,
        clock: Clock = _utc_now,
    ) -> None:
        self.db = db
        self.clock = clock
        self.offer_secret = offer_secret
        self.catalog = load_card_catalog()
        self.monthly_catalog = load_monthly_catalog()
        self.progression = ProgressionService(db)
        self.streak = CardStreakService(db, clock=clock)
        self.monthly_config = MonthlyConfigService(
            db, catalog=self.monthly_catalog, clock=clock
        )

    # ------------------------------------------------------------------- home

    async def home(self, *, user_id: str, event_id: int) -> HomeMissionsResponse:
        event = await self.db["events"].find_one({"id": event_id})
        if not event:
            return HomeMissionsResponse(
                event_id=event_id,
                card_state="VOID",
                locked=True,
                lock_reason="CARD_NOT_FOUND",
            )

        bouts = await self.db["bouts"].find({"event_id": event_id}).to_list(length=None)
        live = [bout for bout in bouts if _is_live(bout)]
        facts = self._card_facts(event, live)
        control = await self.db["mission_card_controls"].find_one({"event_id": event_id})
        card_state = str((control or {}).get("state") or "OPEN").upper()

        locked, lock_reason = self._lock(event, card_state, live)
        streak = await self.streak.state_for(user_id)
        offer_set = await self._offer_set(user_id, facts)
        if offer_set is None:
            # A card too small to fill three slots still has to render — Home
            # shows the monthly mission and an explicit empty state, never a 500.
            return HomeMissionsResponse(
                event_id=event_id,
                card_state=card_state,
                card_revision=facts.card_revision,
                monthly=await self._monthly_view(user_id, event),
                slots=(),
                current_streak=streak.current,
                best_streak=streak.best,
                locked=True,
                lock_reason=lock_reason or "CARD_TOO_SMALL",
            )
        assignments = {
            int(document["slot"]): document
            async for document in self.db["mission_assignments"].find(
                {"user_id": user_id, "event_id": event_id}
            )
        }

        slots = []
        for slot in offer_set["slots"]:
            index = int(slot["slot"])
            assignment = assignments.get(index)
            slots.append(
                MissionSlotView(
                    slot=index,
                    selected=self.selected_view(assignment) if assignment else None,
                    options=(
                        ()
                        if assignment
                        else tuple(
                            self._offer_view(offer) for offer in slot["offers"]
                        )
                    ),
                )
            )

        return HomeMissionsResponse(
            event_id=event_id,
            card_state=card_state,
            offer_set_id=offer_set["_id"],
            card_revision=facts.card_revision,
            monthly=await self._monthly_view(user_id, event),
            slots=tuple(slots),
            current_streak=streak.current,
            best_streak=streak.best,
            locked=locked,
            lock_reason=lock_reason,
        )

    # ---------------------------------------------------------------- profile

    async def profile(self, *, user_id: str) -> ProfileMissionsResponse:
        projection, _ = await self.progression.compute(user_id)
        monthly = await self._monthly_view_for_now(user_id)
        streak = await self.streak.state_for(user_id)
        streak_cards = await self.streak.history_for(user_id, limit=20)

        assignments = (
            await self.db["mission_assignments"]
            .find({"user_id": user_id})
            .sort([("created_at", -1)])
            .to_list(length=200)
        )
        labels = await self._event_labels(
            {int(document["event_id"]) for document in assignments}
            | {int(row["event_id"]) for row in streak_cards}
        )
        active = tuple(
            self.selected_view(document, labels)
            for document in assignments
            if document.get("status") == "ACTIVE"
        )
        history = tuple(
            self.selected_view(document, labels)
            for document in assignments
            if document.get("status") != "ACTIVE"
        )
        celebrations = (
            await self.db["mission_celebrations"]
            .find({"user_id": user_id, "status": "PENDING"})
            .sort([("created_at", 1)])
            .to_list(length=20)
        )
        return ProfileMissionsResponse(
            lifetime_xp=projection.lifetime_xp,
            level=projection.level,
            title=projection.title.value,
            xp_into_level=projection.xp_into_level,
            xp_for_next_level=projection.xp_for_next_level,
            level_progress_pct=projection.level_progress_pct,
            next_title=projection.next_title.value if projection.next_title else None,
            next_title_level=projection.next_title_level,
            current_streak=streak.current,
            best_streak=streak.best,
            next_streak_milestone_label=_milestone_label(streak.current),
            streak_just_broke=streak.last_outcome == "BROKEN",
            monthly=monthly,
            active=active,
            history=history,
            streak_history=tuple(
                StreakCardView(
                    event_id=int(row["event_id"]),
                    event_label=labels.get(int(row["event_id"])),
                    outcome=row["outcome"],
                    picked=int(row["picked"]),
                    denominator=int(row["denominator"]),
                    coverage_percent=int(row["coverage_percent"]),
                    streak_after=int(row["current_after"]),
                    milestone=row.get("milestone"),
                    xp_earned=int(row.get("xp_awarded", 0)),
                )
                for row in streak_cards
                # NOT_ELIGIBLE cards are never recorded, but a defensive filter
                # keeps an unexpected row out of the contract instead of 500ing.
                if row["outcome"] in {"ADVANCED", "BROKEN", "UNCHANGED"}
            ),
            celebrations=tuple(
                {
                    "id": document["_id"],
                    "kind": document["kind"],
                    "presentation": document["presentation"],
                    "heading": document["heading"],
                    "message": document["message"],
                    "metadata": document.get("metadata") or {},
                }
                for document in celebrations
            ),
        )

    # ---------------------------------------------------------------- helpers

    def _card_facts(self, event: dict, bouts: list[dict]) -> FrozenCardFacts:
        sidecar = event.get("card_data_v1") or {}
        revision = int(
            sidecar.get(
                "card_revision",
                sidecar.get("structure_revision", event.get("card_revision", 1)),
            )
        )
        sections = [_section(bout) for bout in bouts]
        main = sum(1 for section in sections if section == "MAIN")
        prelim = sum(1 for section in sections if section in {"PRELIM", "EARLY_PRELIM"})
        titles = sum(1 for bout in bouts if _is_title(bout))

        capabilities = {CardCapability.CANONICAL_CARD, CardCapability.PICK_POINTS}
        if main or prelim:
            capabilities.add(CardCapability.SECTION_ORDER)
        if bouts:
            capabilities.add(CardCapability.MAIN_EVENT)
        if len(bouts) > 1:
            capabilities.add(CardCapability.CO_MAIN)
        if titles:
            capabilities.add(CardCapability.TITLE_BOUTS)
        capabilities.add(CardCapability.RESULT_METHOD)
        capabilities.add(CardCapability.RESULT_ROUND)
        capabilities.add(CardCapability.LEADERBOARD)

        return FrozenCardFacts(
            event_id=int(event["id"]),
            card_revision=revision,
            eligible_bouts=len(bouts),
            main_card_bouts=main,
            prelim_bouts=prelim,
            title_bouts=titles,
            capabilities=frozenset(capabilities),
        )

    async def _offer_set(self, user_id: str, facts: FrozenCardFacts) -> dict | None:
        """Draw once and persist, so a refresh can never reroll (D-PROD-003)."""
        existing = await self.db["mission_offer_sets"].find_one(
            {
                "user_id": user_id,
                "event_id": facts.event_id,
                "card_revision": facts.card_revision,
            }
        )
        if existing:
            return existing

        try:
            generated = generate_mission_offers(
                catalog=self.catalog,
                card=facts,
                user_id=user_id,
                secret=self.offer_secret,
            )
        except OfferGenerationError:
            return None
        document = generated_offer_set_document(generated, created_at=self.clock())
        await self.db["mission_offer_sets"].update_one(
            {"_id": document["_id"]},
            {"$setOnInsert": document},
            upsert=True,
        )
        return await self.db["mission_offer_sets"].find_one({"_id": document["_id"]})

    def _offer_view(self, offer: dict) -> MissionOfferView:
        definition = self.catalog.get(offer["mission_id"])
        return MissionOfferView(
            offer_id=offer["offer_id"],
            mission_id=definition.mission_id,
            name=definition.ui.name,
            description=definition.ui.description,
            difficulty=definition.difficulty.value,
            xp=definition.xp,
            interaction=definition.interaction.value,
            pick_effect=definition.pick_effect.value,
            selection_prompt=definition.ui.selection_prompt,
            selection_spec=(
                definition.selection.model_dump(mode="json")
                if definition.selection is not None
                else None
            ),
        )

    @staticmethod
    def _empty_monthly_progress(definition, config):
        """The reviewed progress copy for a month with no recorded events."""
        observation = evaluate_monthly_metric(
            definition.evaluation.metric,
            MonthlyEvaluationContext(
                month_key=config.month_key,
                user_id="preview",
                parameters=config.parameters,
                events=(),
            ),
        )
        return resolve_monthly_observation(
            definition=definition,
            observation=observation,
            parameters=config.parameters,
        ).progress

    async def _event_labels(self, event_ids: set[int]) -> dict[int, str]:
        """Human names for the cards a Profile row refers to."""
        if not event_ids:
            return {}
        events = (
            await self.db["events"]
            .find({"id": {"$in": sorted(event_ids)}}, {"id": 1, "name": 1})
            .to_list(length=None)
        )
        return {
            int(event["id"]): event["name"]
            for event in events
            if event.get("name")
        }

    def selected_view(
        self, assignment: dict, labels: dict[int, str] | None = None
    ) -> SelectedMissionView:
        definition = self.catalog.get(assignment["mission_id"])
        status = assignment.get("status", "ACTIVE")
        # The evaluator persists the whole MetricResolution under "progress", so
        # the rendered copy lives at progress.progress.{text,percent}. Reading a
        # flat progress_text here silently produced an empty string.
        resolution = assignment.get("progress") or {}
        rendered = resolution.get("progress") or {}
        observation = resolution.get("observation") or {}
        earned = definition.xp if status == "COMPLETED" else 0

        return SelectedMissionView(
            assignment_id=assignment["_id"],
            event_id=int(assignment["event_id"]),
            event_label=(labels or {}).get(int(assignment["event_id"])),
            slot=int(assignment["slot"]),
            offer_id=assignment.get("offer_id"),
            mission_id=definition.mission_id,
            name=definition.ui.name,
            description=definition.ui.description,
            difficulty=definition.difficulty.value,
            xp=definition.xp,
            xp_earned=earned,
            interaction=definition.interaction.value,
            status=status,
            progress_text=rendered.get("text") or "",
            progress_percent=int(rendered.get("percent") or 0),
            selection_summary=self._selection_summary(definition, assignment),
            selection_parts=self._selection_parts(assignment),
            selection=assignment.get("selection") or None,
            # Two writers, two shapes: the evaluator records the reason inside
            # the resolved observation, while an Admin VOID stamps it flat on
            # the assignment. Reading only one meant an Admin VOID showed no
            # reason at all.
            void_reason=(
                assignment.get("void_reason") or observation.get("void_reason")
            ),
        )

    def _selection_summary(self, definition, assignment: dict) -> str | None:
        """A short human line describing what the user actually locked in."""
        parts = self._selection_parts(assignment)
        if not parts:
            return None
        return " · ".join(
            f"{part.value} {part.detail}".strip() if part.detail else part.value
            for part in parts
        )

    def _selection_parts(self, assignment: dict) -> tuple[SelectionPartView, ...]:
        """Lo elegido, partido en piezas que la UI puede estilizar por separado.

        El método sale con la ortografía de display (`KO/TKO`), no con la del
        enum interno: la frase la lee un usuario, no otro servicio.
        """
        selection = assignment.get("selection") or {}
        legs = selection.get("legs") or ()
        parts: list[SelectionPartView] = []
        for leg in legs:
            value = leg.get("fighter_name") or leg.get("fighter_id") or leg.get("key")
            detail = " ".join(
                _display_method(value_)
                for value_ in (leg.get("method"), leg.get("outcome"))
                if value_
            )
            round_ = leg.get("round")
            if round_:
                detail = f"{detail} R{round_}".strip()
            parts.append(
                SelectionPartView(value=str(value), detail=detail or None)
            )
        if choice := selection.get("card_prop_choice"):
            parts.append(SelectionPartView(value=str(choice)))
        if (count := selection.get("card_prop_exact_count")) is not None:
            parts.append(SelectionPartView(value=f"exactly {count}"))
        return tuple(parts)

    async def _monthly_view_for_now(self, user_id: str) -> MonthlyMissionView | None:
        return await self._monthly_for_month(user_id, month_key_for(self.clock()))

    async def _monthly_view(self, user_id: str, event: dict) -> MonthlyMissionView | None:
        moment = (event.get("card_data_v1") or {}).get("official_date") or event.get(
            "date"
        )
        if moment is None:
            return None
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)

        return await self._monthly_for_month(user_id, month_key_for(moment))

    async def _monthly_for_month(
        self, user_id: str, month_key: str
    ) -> MonthlyMissionView | None:
        config = await self.monthly_config.get(month_key)
        if config is None or config.state == MonthlyConfigState.DRAFT:
            return None
        definition = self.monthly_catalog.get(config.mission_id)
        if definition is None:
            return None

        progress = await self.db["mission_monthly_progress"].find_one(
            {"user_id": user_id, "month_key": config.month_key}
        )
        return MonthlyMissionView(
            month_key=config.month_key,
            mission_id=definition.mission_id,
            name=definition.ui.name,
            description=definition.ui.description,
            xp=definition.xp,
            status=(progress or {}).get("status", "ACTIVE"),
            # A user who has not played yet still needs to see the goal. Resolving
            # an empty month through the real evaluator renders "0 / 15 winners"
            # from the reviewed template, rather than restating it here.
            progress_text=(
                (progress or {}).get("progress_text")
                or self._empty_monthly_progress(definition, config).text
            ),
            progress_percent=int((progress or {}).get("progress_percent", 0)),
        )

    @staticmethod
    def _lock(event: dict, card_state: str, bouts: list[dict]) -> tuple[bool, str | None]:
        """Distinguish why selection is closed: Admin, picks close or results."""
        if card_state != "OPEN":
            return True, "ADMIN_CLOSED"
        if any(
            bout.get("result") or (bout.get("card_data_v1") or {}).get("result")
            for bout in bouts
        ):
            return True, "RESULTS_STARTED"
        if str(event.get("status") or "").lower() in {"completed", "cancelled", "live"}:
            return True, "RESULTS_STARTED"
        if event.get("picks_locked"):
            return True, "PICKS_CLOSED"
        lock_at = event.get("picks_close_at") or event.get("lock_at")
        if lock_at is not None:
            if isinstance(lock_at, str):
                lock_at = datetime.fromisoformat(lock_at.replace("Z", "+00:00"))
            if lock_at.tzinfo is None:
                lock_at = lock_at.replace(tzinfo=UTC)
            if datetime.now(UTC) >= lock_at:
                return True, "PICKS_CLOSED"
        return False, None


__all__ = ["MissionReadService"]
