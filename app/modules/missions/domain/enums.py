"""Stable persisted vocabulary for the mission domain."""

from enum import Enum


class StringEnum(str, Enum):
    """Serialize domain enums as stable uppercase strings."""


class MissionDifficulty(StringEnum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class MissionInteractionType(StringEnum):
    AUTO = "AUTO"
    TARGET_FIGHTER = "TARGET_FIGHTER"
    TARGET_FIGHT = "TARGET_FIGHT"
    COMBO_BUILDER = "COMBO_BUILDER"
    CARD_PROP = "CARD_PROP"


class PickEffect(StringEnum):
    NONE = "NONE"
    UPSERT_ONE = "UPSERT_ONE"
    UPSERT_MANY = "UPSERT_MANY"


class MissionAssignmentStatus(StringEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    VOID = "VOID"


class CardMissionState(StringEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    VOID = "VOID"


class MonthlyConfigState(StringEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class MonthlyProgressStatus(StringEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    VOID = "VOID"


class CelebrationStatus(StringEnum):
    PENDING = "PENDING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CANCELLED = "CANCELLED"


class MissionTransitionReason(StringEnum):
    EVALUATION = "EVALUATION"
    RESULT_CORRECTION = "RESULT_CORRECTION"
    ADMIN_CLOSE = "ADMIN_CLOSE"
    ADMIN_REOPEN = "ADMIN_REOPEN"
    ADMIN_VOID = "ADMIN_VOID"
    PICKS_LOCKED = "PICKS_LOCKED"
    RESULTS_STARTED = "RESULTS_STARTED"
    MONTH_START = "MONTH_START"
    MONTH_CLOSE = "MONTH_CLOSE"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
