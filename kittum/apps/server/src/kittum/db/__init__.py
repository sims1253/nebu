"""Database access: one pool, explicit transactions.

A module that changes state takes a `Tx` from its caller and writes its rows and its ledger event
through it, so both commit or neither does. Modules never open their own transactions.

    with database.transaction() as tx:
        rows = screening_store.record(tx, decision)
        ledger.append(tx, EventDraft(...))
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    from collections.abc import Generator

type Tx = Connection[dict[str, Any]]
"""A connection inside an open transaction. Rows come back as dicts."""


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._pool: ConnectionPool[Tx] = ConnectionPool(
            url,
            connection_class=Connection[dict[str, Any]],
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )

    @contextmanager
    def transaction(self) -> Generator[Tx]:
        with self._pool.connection() as conn, conn.transaction():
            yield conn

    def close(self) -> None:
        self._pool.close()


__all__ = ["Database", "Tx"]
