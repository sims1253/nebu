"""The ledger in a SQLite file: the handover bundle's `kittum.sqlite`, and a test store.

It owns its connection. To make appends commit or roll back with your own writes, open
`transaction()` and pass the `tx` it yields: appends and checkpoints join it, and your writes
go through the same connection. Outside one, each append is its own `begin immediate`
transaction. Either way, appends from any process serialize.

    with ledger.transaction() as tx:
        ledger.append(tx, draft)
        tx.execute("insert into decision values (?, ?)", (record, outcome))
"""

# The store's methods take the interface's `tx` and use their own connection, which is the
# caller's `tx` inside `transaction()`.
# ruff: noqa: ARG002

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from kittum.core import canonical_json
from kittum.ledger.model import AuditEvent, Checkpoint
from kittum.ledger.store import ChainStore

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Iterator
    from pathlib import Path

    from kittum.core import Clock, Id
    from kittum.db import Tx

SCHEMA = """
create table if not exists audit_event (
    project_id text not null,
    seq integer not null check (seq > 0),
    event_id text not null unique,
    type text not null,
    schema_version integer not null,
    actor_id text not null,
    recorded_at text not null,
    payload text not null,
    payload_hash text not null,
    prev_hash text not null,
    event_hash text not null unique,
    primary key (project_id, seq)
);

create trigger if not exists audit_event_no_update before update on audit_event
begin select raise(abort, 'audit_event is append-only'); end;

create trigger if not exists audit_event_no_delete before delete on audit_event
begin select raise(abort, 'audit_event is append-only'); end;

create table if not exists checkpoint (
    id text primary key,
    project_id text not null,
    seq integer not null,
    head_hash text not null,
    signature text not null,
    key_id text not null,
    tsa_token_hash text,
    created_at text not null,
    foreign key (project_id, seq) references audit_event (project_id, seq)
);
"""

NO_TX = cast("Tx", None)
"""Pass this where the interface wants a `Tx`; SQLite uses its own connection."""

_COLUMNS = (
    "project_id, seq, event_id, type, schema_version, actor_id, recorded_at, payload,"
    " payload_hash, prev_hash, event_hash"
)
_CHECKPOINT_COLUMNS = (
    "id, project_id, seq, head_hash, signature, key_id, tsa_token_hash, created_at"
)


def _event(row: sqlite3.Row) -> AuditEvent:
    data: dict[str, Any] = dict(row)
    data["payload"] = json.loads(data["payload"])
    data["recorded_at"] = datetime.fromisoformat(data["recorded_at"])
    return AuditEvent.model_validate(data)


class SqliteLedger(ChainStore):
    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        super().__init__(clock)
        conn.row_factory = sqlite3.Row
        conn.autocommit = True
        conn.execute("pragma foreign_keys = on")
        self._conn = conn

    @classmethod
    def open(cls, path: Path | str, *, clock: Clock | None = None) -> SqliteLedger:
        """Open or create a ledger file with its schema."""
        conn = sqlite3.connect(path, autocommit=True)
        conn.executescript(SCHEMA)
        return cls(conn, clock)

    @classmethod
    def open_readonly(cls, path: Path) -> SqliteLedger:
        if not path.is_file():
            raise FileNotFoundError(path)
        return cls(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True))

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self) -> Generator[Tx]:
        """A transaction the caller owns. It commits on exit and rolls back on an exception."""
        if self._conn.in_transaction:
            raise RuntimeError("a transaction is already open on this ledger")
        with self._atomic(NO_TX):
            yield cast("Tx", self._conn)

    def close(self) -> None:
        self._conn.close()

    def restore(self, events: Iterable[AuditEvent], checkpoints: Iterable[Checkpoint]) -> None:
        """Copy events and checkpoints verbatim, as sealing a bundle does. Nothing is re-hashed."""
        with self._atomic(NO_TX):
            for event in events:
                self._insert_event(NO_TX, event)
            for checkpoint in checkpoints:
                self._insert_checkpoint(NO_TX, checkpoint)

    @contextmanager
    def _atomic(self, tx: Tx) -> Generator[None]:
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("begin immediate")
        try:
            yield
        except BaseException:
            self._conn.execute("rollback")
            raise
        self._conn.execute("commit")

    def _lock_head(self, tx: Tx, project_id: Id) -> AuditEvent | None:
        row = self._conn.execute(
            f"select {_COLUMNS} from audit_event where project_id = ? order by seq desc limit 1",
            (str(project_id),),
        ).fetchone()
        return _event(row) if row else None

    def _insert_event(self, tx: Tx, event: AuditEvent) -> None:
        self._conn.execute(
            f"insert into audit_event ({_COLUMNS}) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event.project_id),
                event.seq,
                str(event.event_id),
                event.type,
                event.schema_version,
                str(event.actor_id),
                event.recorded_at.isoformat(),
                canonical_json(event.payload).decode(),
                event.payload_hash,
                event.prev_hash,
                event.event_hash,
            ),
        )

    def _iter_events(
        self, tx: Tx, project_id: Id, after_seq: int, through_seq: int | None
    ) -> Iterator[AuditEvent]:
        rows = self._conn.execute(
            f"select {_COLUMNS} from audit_event"
            " where project_id = ? and seq > ? and (? is null or seq <= ?) order by seq",
            (str(project_id), after_seq, through_seq, through_seq),
        )
        for row in rows:
            yield _event(row)

    def _insert_checkpoint(self, tx: Tx, checkpoint: Checkpoint) -> None:
        self._conn.execute(
            f"insert into checkpoint ({_CHECKPOINT_COLUMNS}) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(checkpoint.id),
                str(checkpoint.project_id),
                checkpoint.seq,
                checkpoint.head_hash,
                checkpoint.signature,
                checkpoint.key_id,
                checkpoint.tsa_token_hash,
                checkpoint.created_at.isoformat(),
            ),
        )

    def checkpoints(self, tx: Tx, project_id: Id) -> list[Checkpoint]:
        rows = self._conn.execute(
            f"select {_CHECKPOINT_COLUMNS} from checkpoint where project_id = ?"
            " order by seq, created_at, id",
            (str(project_id),),
        ).fetchall()
        return [Checkpoint.model_validate(dict(r)) for r in rows]

    def project_ids(self) -> list[Id]:
        rows = self._conn.execute("select distinct project_id from audit_event").fetchall()
        return [uuid.UUID(r[0]) for r in rows]
