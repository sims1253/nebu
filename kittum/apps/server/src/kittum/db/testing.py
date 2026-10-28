"""Test helpers for code that takes a `Tx`."""

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import psycopg
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from collections.abc import Generator

    from kittum.db import Tx


class NullTx:
    """A transaction for in-memory fakes that never touch SQL."""

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an in-memory fake should not run SQL")


def null_tx() -> Tx:
    """A `NullTx` typed as `Tx`, for passing to fakes."""
    return cast("Tx", NullTx())


@contextmanager
def rollback_tx() -> Generator[Tx]:
    """A connection to KITTUM_DATABASE_URL whose work is rolled back afterwards.

    Tests use it through the `pg` fixture, which skips when no database is configured.
    """
    url = os.environ["KITTUM_DATABASE_URL"]
    with psycopg.Connection[dict[str, Any]].connect(url, row_factory=dict_row) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
