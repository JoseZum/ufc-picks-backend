"""Inspect, apply or roll back the mission index manifest.

Dry-run inspection is the default. Mutations require the exact database name
and content-derived plan id printed by a preceding dry run.
"""

# ruff: noqa: E402 -- direct script execution must add the project root first.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic_settings import BaseSettings, SettingsConfigDict
from pymongo import AsyncMongoClient
from pymongo.uri_parser import parse_uri

from app.modules.missions.indexes import (
    apply_mission_indexes,
    inspect_mission_indexes,
    mission_index_plan_id,
    rollback_mission_indexes,
)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class MissionMongoSettings(BaseSettings):
    """Load only the two settings needed by this migration."""

    mongodb_uri: str
    mongodb_db_name: str = "ufc_picks"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def is_local_mongo_uri(uri: str) -> bool:
    parsed = parse_uri(uri)
    hosts = {host.lower() for host, _port in parsed["nodelist"]}
    return bool(hosts) and hosts.issubset(LOCAL_HOSTS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-plan-id")
    parser.add_argument(
        "--allow-nonlocal",
        action="store_true",
        help="Permit an explicitly confirmed non-loopback target.",
    )
    return parser


def require_mutation_confirmation(args, uri: str, database_name: str) -> None:
    if args.confirm_database != database_name:
        raise SystemExit("Mutation requires --confirm-database with the exact target name.")
    if args.confirm_plan_id != mission_index_plan_id():
        raise SystemExit("Mutation requires --confirm-plan-id from the current dry run.")
    if not is_local_mongo_uri(uri) and not args.allow_nonlocal:
        raise SystemExit("Non-local mutation also requires --allow-nonlocal.")


async def run(args: argparse.Namespace) -> int:
    settings = MissionMongoSettings()
    if args.apply or args.rollback:
        require_mutation_confirmation(
            args,
            settings.mongodb_uri,
            settings.mongodb_db_name,
        )

    client = AsyncMongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5_000)
    try:
        db = client[settings.mongodb_db_name]
        if args.apply:
            report = await apply_mission_indexes(db)
            action = "apply"
        elif args.rollback:
            report = await rollback_mission_indexes(db)
            action = "rollback"
        else:
            report = await inspect_mission_indexes(db)
            action = "dry-run"
        print(json.dumps({"action": action, **report.to_dict()}, indent=2))
        return 0 if report.can_apply else 1
    finally:
        await client.close()


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
