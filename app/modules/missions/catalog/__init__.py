"""Versioned, reviewed mission catalog assets."""

import json
from pathlib import Path
from types import MappingProxyType

from app.modules.missions.domain.catalog import (
    MissionCatalog,
    MissionCatalogError,
    load_mission_catalog_file,
)
from app.modules.missions.domain.monthly import (
    MonthlyMissionDefinition,
    validate_monthly_definition,
)

CARD_CATALOG_VERSION = "2026.08.01"
CARD_CATALOG_PATH = Path(__file__).with_name("card_catalog_2026_08_01.json")
MONTHLY_CATALOG_VERSION = "2026.08.01"
MONTHLY_CATALOG_PATH = Path(__file__).with_name("monthly_catalog_2026_08_01.json")


def load_card_catalog() -> MissionCatalog:
    catalog = load_mission_catalog_file(
        CARD_CATALOG_PATH,
        expected_version=CARD_CATALOG_VERSION,
    )
    from app.modules.missions.domain.exceptional import (  # noqa: PLC0415
        validate_catalog_evaluator_coverage,
    )

    validate_catalog_evaluator_coverage(catalog)
    return catalog


def load_monthly_catalog() -> MappingProxyType[str, MonthlyMissionDefinition]:
    """The 18 reviewed monthly templates, keyed by mission ID."""
    values = json.loads(MONTHLY_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(values, list) or not values:
        raise MissionCatalogError("Monthly catalog root must be a non-empty JSON array")

    definitions: dict[str, MonthlyMissionDefinition] = {}
    for index, value in enumerate(values):
        try:
            definition = validate_monthly_definition(value)
        except ValueError as exc:
            raise MissionCatalogError(
                f"Invalid monthly definition at index {index}: {exc}"
            ) from exc
        if definition.catalog_version != MONTHLY_CATALOG_VERSION:
            raise MissionCatalogError(
                f"Monthly mission {definition.mission_id} uses catalog version "
                f"{definition.catalog_version}; expected {MONTHLY_CATALOG_VERSION}"
            )
        if definition.mission_id in definitions:
            raise MissionCatalogError(
                f"Duplicate monthly mission id {definition.mission_id!r}"
            )
        definitions[definition.mission_id] = definition
    return MappingProxyType(definitions)


__all__ = [
    "CARD_CATALOG_PATH",
    "CARD_CATALOG_VERSION",
    "MONTHLY_CATALOG_PATH",
    "MONTHLY_CATALOG_VERSION",
    "load_card_catalog",
    "load_monthly_catalog",
]
