"""A committing database and the two reviews, built once per module: every test reads them at
fixed ledger positions, so the renders and seals that tests add later do not change what
earlier positions show."""

import os
from typing import TYPE_CHECKING

import pytest

from kittum.db import Database
from tests.extraction.conftest import env  # noqa: F401 - extraction's project, for test_extracted
from tests.report.support import capivasertib_review, nrt_review

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.report.support import NrtSeqs, ReviewWorld


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url, max_size=4)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def nrt(database: Database) -> tuple[ReviewWorld, NrtSeqs]:
    return nrt_review(database)


@pytest.fixture(scope="module")
def capivasertib(database: Database) -> ReviewWorld:
    return capivasertib_review(database)
