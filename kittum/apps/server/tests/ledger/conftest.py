"""Fixtures for the ledger tests; the harnesses live in `tests.ledger.harness`."""

import os
from typing import TYPE_CHECKING

import pytest

from kittum.db import Database
from tests.ledger.harness import Harness, memory_harness, postgres_harness, sqlite_harness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(params=["memory", "sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    if request.param == "memory":
        yield from memory_harness()
    elif request.param == "sqlite":
        yield from sqlite_harness(tmp_path)
    else:
        yield from postgres_harness(request.getfixturevalue("pg"))


@pytest.fixture
def database() -> Iterator[Database]:
    """A pool on KITTUM_DATABASE_URL whose transactions commit. Use fresh project IDs per test."""
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url, max_size=4)
    try:
        yield db
    finally:
        db.close()
