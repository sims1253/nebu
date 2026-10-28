import os
from typing import TYPE_CHECKING

import pytest

from kittum.db.testing import rollback_tx

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kittum.db import Tx


@pytest.fixture
def pg() -> Iterator[Tx]:
    """A migrated test database connection, rolled back after the test.

    Start one with `docker compose -f infra/compose.yaml up -d --wait postgres` and
    `docker compose -f infra/compose.yaml run --rm migrate`, then set KITTUM_DATABASE_URL.
    """
    if not os.environ.get("KITTUM_DATABASE_URL"):
        pytest.skip("KITTUM_DATABASE_URL is not set")
    with rollback_tx() as tx:
        yield tx
