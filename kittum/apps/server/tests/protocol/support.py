"""Shared helpers for the protocol tests.

Tests use the real `kittum.ledger.PostgresLedger` for publish flows against Postgres.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from kittum.core import Id, new_id

if TYPE_CHECKING:
    from kittum.db import Tx

EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "protocols"


def example_text(name: str = "nicotine-replacement") -> str:
    return (EXAMPLES / f"{name}.yaml").read_text()


def example_source(name: str = "nicotine-replacement") -> dict[str, Any]:
    """The example as JSON data: YAML dates become ISO strings, as in a request body."""
    return json.loads(json.dumps(yaml.safe_load(example_text(name)), default=str))


def seed_project(tx: Tx) -> tuple[Id, Id, Id]:
    """A project, a human approver, and a system actor."""
    project = tx.execute(
        "insert into project (name, policy_version) values ('example review', 'p1') returning id"
    ).fetchone()
    human = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'human') returning id",
        (f"reviewer-{new_id()}",),
    ).fetchone()
    system = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'system') returning id",
        (f"system-{new_id()}",),
    ).fetchone()
    assert project and human and system
    return project["id"], human["id"], system["id"]
