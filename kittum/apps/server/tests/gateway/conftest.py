"""A gateway database for each test: its attempts, cache, budgets, cooldowns, and rate-limit
buckets emptied before and after, so no test sees another's state (a leftover cooldown would
make a frozen-clock test wait forever)."""

import os
from typing import TYPE_CHECKING

import pytest
from psycopg import sql

from kittum.db import Database

if TYPE_CHECKING:
    from collections.abc import Iterator


def _reset(database: Database) -> None:
    with database.transaction() as tx:
        tx.execute("truncate gateway_attempt, gateway_cache, gateway_budget, gateway_cooldown")
        tables = tx.execute(
            "select tablename from pg_tables"
            " where schemaname = 'public' and tablename like 'ratelimit___gateway%'"
        ).fetchall()
        for row in tables:
            tx.execute(sql.SQL("drop table if exists {}").format(sql.Identifier(row["tablename"])))


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url)
    _reset(db)
    try:
        yield db
    finally:
        _reset(db)
        db.close()
