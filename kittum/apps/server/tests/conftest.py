import os
from typing import TYPE_CHECKING

import psycopg
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def pg() -> Iterator[psycopg.Connection]:
    """A connection to the migrated test database, rolled back after the test.

    Start one with `docker compose -f infra/compose.yaml up -d postgres` and
    `docker compose -f infra/compose.yaml run --rm migrate`, then set KITTUM_DATABASE_URL.
    """
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    with psycopg.connect(url) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
