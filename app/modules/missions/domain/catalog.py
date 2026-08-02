"""Immutable versioned mission catalog loading."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from app.modules.missions.domain.definitions import (
    MissionDefinition,
    validate_mission_definition,
)
from app.modules.missions.domain.enums import (
    MissionDifficulty,
    MissionInteractionType,
)


class MissionCatalogError(ValueError):
    """A catalog cannot be used safely as versioned mission content."""


class MissionCatalog(Sequence[MissionDefinition]):
    def __init__(
        self,
        *,
        version: str,
        definitions: Iterable[MissionDefinition],
    ) -> None:
        frozen = tuple(definitions)
        self.version = version
        self._definitions = frozen
        self._by_id = MappingProxyType(
            {definition.mission_id: definition for definition in frozen}
        )

    def __len__(self) -> int:
        return len(self._definitions)

    def __getitem__(self, index):
        return self._definitions[index]

    def __iter__(self) -> Iterator[MissionDefinition]:
        return iter(self._definitions)

    def get(self, mission_id: str) -> MissionDefinition:
        try:
            return self._by_id[mission_id]
        except KeyError as exc:
            raise MissionCatalogError(
                f"Unknown mission id {mission_id!r} in catalog {self.version}"
            ) from exc

    def by_difficulty(
        self,
        difficulty: MissionDifficulty,
    ) -> tuple[MissionDefinition, ...]:
        return tuple(
            definition
            for definition in self
            if definition.difficulty == difficulty
        )

    def by_interaction(
        self,
        interaction: MissionInteractionType,
    ) -> tuple[MissionDefinition, ...]:
        return tuple(
            definition
            for definition in self
            if definition.interaction == interaction
        )


def load_mission_catalog(
    values: object,
    *,
    expected_version: str,
) -> MissionCatalog:
    if not isinstance(values, list) or not values:
        raise MissionCatalogError("Mission catalog root must be a non-empty JSON array")

    definitions: list[MissionDefinition] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(values):
        try:
            definition = validate_mission_definition(value)
        except ValidationError as exc:
            raise MissionCatalogError(
                f"Invalid mission definition at index {index}: {exc}"
            ) from exc
        if definition.catalog_version != expected_version:
            raise MissionCatalogError(
                f"Mission {definition.mission_id} uses catalog version "
                f"{definition.catalog_version}; expected {expected_version}"
            )
        if definition.mission_id in seen_ids:
            raise MissionCatalogError(
                f"Duplicate mission id {definition.mission_id!r}"
            )
        seen_ids.add(definition.mission_id)
        definitions.append(definition)

    return MissionCatalog(version=expected_version, definitions=definitions)


def load_mission_catalog_file(
    path: str | Path,
    *,
    expected_version: str,
) -> MissionCatalog:
    catalog_path = Path(path)
    try:
        values: Any = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MissionCatalogError(
            f"Cannot load mission catalog file {catalog_path.name!r}: {exc}"
        ) from exc
    return load_mission_catalog(values, expected_version=expected_version)
