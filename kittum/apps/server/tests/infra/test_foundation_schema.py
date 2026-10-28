from typing import TYPE_CHECKING

import psycopg
import pytest

if TYPE_CHECKING:
    from kittum.db import Tx

pytestmark = pytest.mark.postgres


def _seed_event(pg: Tx) -> tuple[object, int]:
    project = pg.execute(
        "insert into project (name, policy_version) values ('t', 'p1') returning id"
    ).fetchone()
    actor = pg.execute(
        "insert into actor (identity_ref, kind) values (gen_random_uuid()::text, 'system') "
        "returning id"
    ).fetchone()
    assert project and actor
    project_id, actor_id = project["id"], actor["id"]
    pg.execute(
        "insert into audit_event (project_id, seq, event_id, type, schema_version, actor_id,"
        " recorded_at, payload, payload_hash, prev_hash, event_hash)"
        " values (%s, 1, uuidv7(), 't', 1, %s, now(), '{}', repeat('a', 64), repeat('0', 64),"
        " encode(sha256(gen_random_uuid()::text::bytea), 'hex'))",
        (project_id, actor_id),
    )
    return project_id, 1


def test_ids_default_to_uuidv7(pg: Tx) -> None:
    row = pg.execute("select uuid_extract_version(uuidv7()) as v").fetchone()
    assert row == {"v": 7}


def test_audit_events_cannot_be_updated(pg: Tx) -> None:
    project, seq = _seed_event(pg)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        pg.execute(
            "update audit_event set type = 'x' where project_id = %s and seq = %s", (project, seq)
        )


def test_audit_events_cannot_be_deleted(pg: Tx) -> None:
    project, seq = _seed_event(pg)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        pg.execute("delete from audit_event where project_id = %s and seq = %s", (project, seq))
