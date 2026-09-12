"""Initialize and verify the local single-node MongoDB replica set.

This script only targets the loopback test instance declared in
``compose.test.yml``. A replica set is required for mission transactions.
"""

from __future__ import annotations

import time

from pymongo import MongoClient
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

TEST_MONGO_URI = "mongodb://127.0.0.1:27017"
REPLICA_SET_NAME = "rs0"
REPLICA_MEMBER = "localhost:27017"
READY_TIMEOUT_SECONDS = 20


def main() -> int:
    client = MongoClient(
        TEST_MONGO_URI,
        directConnection=True,
        serverSelectionTimeoutMS=2_000,
    )
    try:
        client.admin.command("ping")
    except ServerSelectionTimeoutError as exc:
        raise SystemExit(
            "Local test MongoDB is unavailable. Start it with: "
            "docker compose -f compose.test.yml up -d --wait"
        ) from exc

    try:
        client.admin.command("replSetGetStatus")
    except OperationFailure as exc:
        if exc.code != 94:  # NotYetInitialized
            raise
        client.admin.command(
            {
                "replSetInitiate": {
                    "_id": REPLICA_SET_NAME,
                    "members": [{"_id": 0, "host": REPLICA_MEMBER}],
                }
            }
        )

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        hello = client.admin.command("hello")
        if (
            hello.get("setName") == REPLICA_SET_NAME
            and hello.get("isWritablePrimary") is True
        ):
            client.close()
            print("Local MongoDB replica set rs0 is writable and ready.")
            return 0
        time.sleep(0.25)

    client.close()
    raise SystemExit("Local MongoDB replica set did not become writable in time.")


if __name__ == "__main__":
    raise SystemExit(main())
