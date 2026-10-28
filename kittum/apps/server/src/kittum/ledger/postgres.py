"""The live ledger on the foundation tables `audit_event` and `checkpoint`."""

from contextlib import nullcontext
from datetime import UTC
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from kittum.ledger.model import AuditEvent, Checkpoint
from kittum.ledger.store import ChainStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from kittum.core import Id
    from kittum.db import Tx

# The first key of the two-key advisory lock, so ledger locks never meet other modules' locks.
LOCK_NAMESPACE = 0x6B6C6467  # "kldg"
_BATCH = 1000

_COLUMNS = (
    "project_id, seq, event_id, type, schema_version, actor_id, recorded_at, payload,"
    " payload_hash, prev_hash, event_hash"
)


def _event(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent.model_validate({**row, "recorded_at": row["recorded_at"].astimezone(UTC)})


def _checkpoint(row: dict[str, Any]) -> Checkpoint:
    return Checkpoint.model_validate(
        {
            **row,
            "signature": bytes(row["signature"]).hex(),
            "created_at": row["created_at"].astimezone(UTC),
        }
    )


class PostgresLedger(ChainStore):
    """Appends run in the caller's transaction and serialize per project on an advisory lock.

    The lock is held until the caller commits, so keep transactions that append short.
    """

    def _atomic(self, tx: Tx) -> AbstractContextManager[None]:  # noqa: ARG002
        return nullcontext()

    def _lock_head(self, tx: Tx, project_id: Id) -> AuditEvent | None:
        tx.execute(
            "select pg_advisory_xact_lock(%s, hashtext(%s::text))", (LOCK_NAMESPACE, project_id)
        )
        row = tx.execute(
            f"select {_COLUMNS} from audit_event where project_id = %s order by seq desc limit 1",
            (project_id,),
        ).fetchone()
        return _event(row) if row else None

    def _insert_event(self, tx: Tx, event: AuditEvent) -> None:
        tx.execute(
            f"insert into audit_event ({_COLUMNS}) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                event.project_id,
                event.seq,
                event.event_id,
                event.type,
                event.schema_version,
                event.actor_id,
                event.recorded_at,
                Jsonb(event.payload),
                event.payload_hash,
                event.prev_hash,
                event.event_hash,
            ),
        )

    def _iter_events(
        self, tx: Tx, project_id: Id, after_seq: int, through_seq: int | None
    ) -> Iterator[AuditEvent]:
        last = after_seq
        while True:
            rows = tx.execute(
                f"select {_COLUMNS} from audit_event"
                " where project_id = %s and seq > %s and (%s::bigint is null or seq <= %s)"
                " order by seq limit %s",
                (project_id, last, through_seq, through_seq, _BATCH),
            ).fetchall()
            for row in rows:
                yield _event(row)
            if len(rows) < _BATCH:
                return
            last = rows[-1]["seq"]

    def _insert_checkpoint(self, tx: Tx, checkpoint: Checkpoint) -> None:
        tx.execute(
            "insert into checkpoint"
            " (id, project_id, seq, head_hash, signature, key_id, tsa_token_hash, created_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                checkpoint.id,
                checkpoint.project_id,
                checkpoint.seq,
                checkpoint.head_hash,
                bytes.fromhex(checkpoint.signature),
                checkpoint.key_id,
                checkpoint.tsa_token_hash,
                checkpoint.created_at,
            ),
        )

    def checkpoints(self, tx: Tx, project_id: Id) -> list[Checkpoint]:
        rows = tx.execute(
            "select id, project_id, seq, head_hash, signature, key_id, tsa_token_hash, created_at"
            " from checkpoint where project_id = %s order by seq, created_at, id",
            (project_id,),
        ).fetchall()
        return [_checkpoint(r) for r in rows]
