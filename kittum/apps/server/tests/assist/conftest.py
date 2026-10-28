"""A committing database, for the command line, which opens connections of its own. Every test
builds its own project, so tests never see each other's rows."""

import os
from typing import TYPE_CHECKING

import pytest

from kittum.db import Database

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url, max_size=4)
    try:
        yield db
    finally:
        db.close()
