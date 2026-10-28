"""One behaviour suite for the in-memory, SQLite, and Postgres ledgers."""

from typing import TYPE_CHECKING

import pytest

from kittum.ledger import (
    GENESIS_HASH,
    AuditEvent,
    LedgerIntegrityError,
    SigningKey,
    event_hash,
    payload_hash,
)
from tests.ledger.harness import PAYLOAD

if TYPE_CHECKING:
    from kittum.core import Id
    from tests.ledger.harness import Harness

KEY = SigningKey.generate()


def _forge_from(events: list[AuditEvent], seq: int, payload: dict) -> list[AuditEvent]:
    """Rewrite the event at `seq` and re-hash every later event, as a careful forger would."""
    out = events[: seq - 1]
    prev = out[-1].event_hash if out else GENESIS_HASH
    for e in events[seq - 1 :]:
        p = payload if e.seq == seq else e.payload
        p_hash = payload_hash(p)
        fields = e.model_dump(exclude={"payload", "payload_hash", "prev_hash", "event_hash"})
        h = event_hash(**fields, payload_hash=p_hash, prev_hash=prev)
        out.append(
            AuditEvent(**fields, payload=p, payload_hash=p_hash, prev_hash=prev, event_hash=h)
        )
        prev = h
    return out


def _replace_from(store: Harness, project: Id, seq: int, events: list[AuditEvent]) -> None:
    head = store.ledger.events(store.tx, project)[-1].seq
    for s in range(head, seq - 1, -1):
        store.delete(project, s)
    for e in events[seq - 1 :]:
        store.insert(e)


def test_appends_chain_per_project_and_read_back_exactly(store: Harness) -> None:
    project, actor = store.new_project()
    other, other_actor = store.new_project()
    store.append(project, actor, 3)
    store.append(other, other_actor, 1)

    events = store.ledger.events(store.tx, project)
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].prev_hash == GENESIS_HASH
    assert [e.prev_hash for e in events[1:]] == [e.event_hash for e in events[:-1]]
    # What comes back from the store hashes the same as what went in: time zone, microseconds,
    # umlauts, floats, and nulls survive the round trip.
    assert events[1].payload == {**PAYLOAD, "i": 1}
    assert events[1].recorded_at.microsecond == 123
    assert [e.seq for e in store.ledger.events(store.tx, project, after_seq=1)] == [2, 3]
    assert [e.seq for e in store.ledger.events(store.tx, project, through_seq=2)] == [1, 2]

    other_events = store.ledger.events(store.tx, other)
    assert [(e.seq, e.prev_hash) for e in other_events] == [(1, GENESIS_HASH)]
    report = store.ledger.verify(store.tx, project)
    assert report.ok and report.checked_through_seq == 3


def test_checkpoint_is_stored_and_verifies_a_growing_chain(store: Harness) -> None:
    project, actor = store.new_project()
    store.append(project, actor, 3)
    checkpoint = store.ledger.checkpoint(store.tx, project, KEY)
    assert (checkpoint.seq, checkpoint.key_id) == (3, KEY.key_id)
    assert checkpoint.head_hash == store.ledger.events(store.tx, project)[-1].event_hash

    store.append(project, actor, 2)
    assert store.ledger.checkpoints(store.tx, project) == [checkpoint]
    report = store.ledger.verify(store.tx, project, checkpoint=checkpoint, key=KEY.verify_key)
    assert report.ok and report.checked_through_seq == 5 and report.checkpoint_seq == 3


@pytest.mark.parametrize(
    ("tamper", "problem", "first_bad_seq"),
    [
        ("change_payload", "payload_mismatch", 2),
        ("delete_event", "seq_gap", 2),
        ("reorder_events", "prev_hash_mismatch", 2),
    ],
)
def test_verify_catches_tampering(
    store: Harness, tamper: str, problem: str, first_bad_seq: int
) -> None:
    project, actor = store.new_project()
    store.append(project, actor, 4)
    events = store.ledger.events(store.tx, project)

    if tamper == "change_payload":
        # The excluded record is quietly marked included; the stored hash stays.
        forged = events[1].model_copy(update={"payload": {**events[1].payload, "n": 120}})
        _replace_from(store, project, 2, [*events[:1], forged, *events[2:]])
    elif tamper == "delete_event":
        store.delete(project, 2)
    else:
        swapped = [
            events[0],
            events[2].model_copy(update={"seq": 2}),
            events[1].model_copy(update={"seq": 3}),
            events[3],
        ]
        _replace_from(store, project, 2, swapped)

    report = store.ledger.verify(store.tx, project)
    assert not report.ok
    assert (report.problem, report.first_bad_seq, report.checked_through_seq) == (
        problem,
        first_bad_seq,
        first_bad_seq - 1,
    )


def test_a_cut_tail_passes_the_chain_but_not_the_checkpoint(store: Harness) -> None:
    project, actor = store.new_project()
    store.append(project, actor, 5)
    trusted = store.ledger.checkpoint(store.tx, project, KEY)  # copied outside the database

    store.delete(project, 5)
    store.delete(project, 4)

    assert store.ledger.verify(store.tx, project).ok  # the chain alone cannot tell
    report = store.ledger.verify(store.tx, project, checkpoint=trusted, key=KEY.verify_key)
    assert not report.ok
    assert (report.problem, report.first_bad_seq) == ("tail_cut", 4)


def test_a_rewritten_chain_passes_the_chain_but_not_the_checkpoint(store: Harness) -> None:
    project, actor = store.new_project()
    store.append(project, actor, 5)
    trusted = store.ledger.checkpoint(store.tx, project, KEY)
    events = store.ledger.events(store.tx, project)

    _replace_from(store, project, 2, _forge_from(events, 2, {"decision": "exclude"}))

    assert store.ledger.verify(store.tx, project).ok
    report = store.ledger.verify(store.tx, project, checkpoint=trusted, key=KEY.verify_key)
    assert (report.ok, report.problem, report.first_bad_seq) == (False, "rewritten", 5)


def test_a_checkpoint_from_another_key_proves_nothing(store: Harness) -> None:
    project, actor = store.new_project()
    store.append(project, actor, 3)
    forger = SigningKey.generate()
    forged = store.ledger.checkpoint(store.tx, project, forger)

    report = store.ledger.verify(store.tx, project, checkpoint=forged, key=KEY.verify_key)
    assert report.problem == "untrusted_key"
    relabelled = forged.model_copy(update={"key_id": KEY.key_id})
    report = store.ledger.verify(store.tx, project, checkpoint=relabelled, key=KEY.verify_key)
    assert report.problem == "bad_signature"


def test_checkpoint_refuses_to_sign_a_tampered_chain(store: Harness) -> None:
    project, actor = store.new_project()
    store.append(project, actor, 3)
    store.delete(project, 2)

    with pytest.raises(LedgerIntegrityError) as caught:
        store.ledger.checkpoint(store.tx, project, KEY)
    assert caught.value.report.problem == "seq_gap"
    assert store.ledger.checkpoints(store.tx, project) == []
