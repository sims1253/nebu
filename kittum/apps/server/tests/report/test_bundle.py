"""The handover bundle: seal the nicotine replacement review with every report, verify it offline
with `kittum verify`, and trace numbers in its reports to the decisions, events, and people in
its `kittum.sqlite`, with nothing but the bundle at hand."""

import json
import sqlite3
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import pytest

from kittum.cli import main
from kittum.core import new_id
from kittum.ledger import SigningKey
from kittum.report import RightsProfile, load_review, render_files, seal_bundle
from kittum.screening import StageSpec
from tests.report.support import NRT_SCOPE
from tests.screening.support import SENTINELS

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.ledger import Bundle
    from tests.report.support import NrtSeqs, ReviewWorld

pytestmark = pytest.mark.postgres

KEY = SigningKey.generate()
FLOW = f"reports/prisma/TRIALS-{NRT_SCOPE}.json"


@pytest.fixture(scope="module")
def sealed(nrt: tuple[ReviewWorld, NrtSeqs], tmp_path_factory: pytest.TempPathFactory) -> Bundle:
    world, seqs = nrt
    out = tmp_path_factory.mktemp("bundle") / "nrt"
    with world.db.transaction() as tx:
        return seal_bundle(
            tx,
            world.project_id,
            world.artifacts,
            out,
            signer=KEY,
            actor_id=world.people.lead,
            cutoff_seq=seqs.full_text,
            rights=RightsProfile(
                id="client-contract-7",
                withhold_sources={"embase": "Embase export licence: no redistribution"},
            ),
            ledger=world.ledger,
        )


def _db(bundle: Bundle) -> sqlite3.Connection:
    db = sqlite3.connect(bundle.path / "kittum.sqlite")
    db.row_factory = sqlite3.Row
    return db


def test_the_bundle_verifies_offline_and_carries_every_report(
    nrt: tuple[ReviewWorld, NrtSeqs],
    sealed: Bundle,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, seqs = nrt
    key = tmp_path / "sender.pub.pem"
    key.write_bytes(KEY.verify_key.to_pem())
    assert main(["verify", str(sealed.path), "--public-key", str(key)]) == 0
    assert f"through seq {seqs.full_text}" in capsys.readouterr().out

    # The reports are the targets rendered at the cutoff, byte for byte.
    with world.db.transaction() as tx:
        review = load_review(tx, world.project_id, cutoff_seq=seqs.full_text, store=world.artifacts)
    for target in ("prisma", "ris", "search_report", "ai_disclosure", "extraction"):
        for f in render_files(review, target):
            assert (sealed.path / "reports" / target / f.path).read_bytes() == f.data
    manifest = json.loads((sealed.path / "manifest.json").read_text())
    assert FLOW in manifest["files"]
    assert not any(p.startswith("reports/dossier/") for p in manifest["files"])  # no profile

    # Every module's rows are there, as of the cutoff.
    db = _db(sealed)
    tables = {r[0] for r in db.execute("select name from sqlite_master where type = 'table'")}
    assert {
        "protocol_version",
        "search_execution",
        "source_occurrence",
        "corpus_revision",
        "proposal",
        "identity_revision",
        "identity_link",
        "report",
        "study",
        "judge_config",
        "verdict",
        "screening_stage",
        "screening_item",
        "screening_assignment",
        "screening_decision",
        "screening_undo",
        "screening_ai_reading",
        "screening_conflict",
        "screening_adjudication",
        "project",
        "actor",
        "report_artifact",
    } <= tables
    assert db.execute("select count(*) from source_occurrence").fetchone()[0] == 35
    assert db.execute("select count(*) from screening_undo").fetchone()[0] == 1
    assert db.execute("select max(seq) from audit_event").fetchone()[0] == seqs.full_text

    # Restricted artifacts appear as hashes with a reason; the model outputs travel.
    embase = json.loads(
        db.execute(
            "select raw_artifact_hashes from search_execution where source = 'embase'"
        ).fetchone()[0]
    )
    (row,) = db.execute(
        "select included, withheld_reason from artifact where hash = ?", (embase[0],)
    ).fetchall()
    assert (row["included"], row["withheld_reason"]) == (
        0,
        "client-contract-7: Embase export licence: no redistribution",
    )
    assert not (sealed.path / "artifacts" / embase[0]).exists()
    outputs = [r[0] for r in db.execute("select raw_output_hash from verdict")]
    assert outputs
    for h in outputs:
        assert (sealed.path / "artifacts" / h).is_file()
        assert db.execute("select included from artifact where hash = ?", (h,)).fetchone()[0] == 1

    # A changed number in a report is caught.
    flow = sealed.path / FLOW
    original = flow.read_bytes()
    flow.write_bytes(original.replace(b'"records_screened": 33', b'"records_screened": 34'))
    assert main(["verify", str(sealed.path)]) == 1
    assert f"{FLOW} does not match its hash" in capsys.readouterr().out
    flow.write_bytes(original)


def _final_outcomes(db: sqlite3.Connection, stage_id: str) -> dict[str, list[dict[str, Any]]]:
    """Replay the bundle's decisions and undos: each item's standing screener decisions."""
    seq = {r["event_id"]: r["seq"] for r in db.execute("select event_id, seq from audit_event")}
    rows = [
        ("decision", dict(r))
        for r in db.execute(
            "select d.*, a.item_ref, a.reviewer_id, a.role from screening_decision d"
            " join screening_assignment a on a.id = d.assignment_id where a.stage_id = ?",
            (stage_id,),
        )
    ] + [
        ("undo", dict(r))
        for r in db.execute(
            "select u.*, a.item_ref from screening_undo u"
            " join screening_assignment a on a.id = u.assignment_id where a.stage_id = ?",
            (stage_id,),
        )
    ]
    standing: dict[str, dict[str, Any]] = {}
    for kind, r in sorted(rows, key=lambda x: seq[x[1]["event_id"]]):
        if kind == "undo":
            standing.pop(r["assignment_id"], None)
        elif r["phase"] == "final" or r["reveal"] is None:
            standing[r["assignment_id"]] = r
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in standing.values():
        by_item[r["item_ref"]].append(r)
    return by_item


def test_every_count_traces_to_decisions_events_and_people(sealed: Bundle) -> None:
    """DESIGN.md's acceptance test, on the bundle alone: pick numbers in an exported table and
    trace each to the decisions, ledger events, and human actors behind it."""
    flow = json.loads((sealed.path / FLOW).read_text())
    db = _db(sealed)
    events = {r["event_id"]: dict(r) for r in db.execute("select * from audit_event")}
    actors = {r["id"]: dict(r) for r in db.execute("select * from actor")}

    def human(event_id: str) -> dict[str, Any]:
        actor = actors[events[event_id]["actor_id"]]
        assert actor["kind"] == "human", actor
        return actor

    def stage(name: str) -> str:
        return db.execute(
            "select id from screening_stage where scope_id = ? and stage = ?", (NRT_SCOPE, name)
        ).fetchone()[0]

    # "Records excluded: 27" at title and abstract.
    ta = _final_outcomes(db, stage("title_abstract"))
    adjudicated = {
        r["item_ref"]: dict(r)
        for r in db.execute(
            "select c.item_ref, j.* from screening_adjudication j join screening_conflict c"
            " on c.id = j.conflict_id where c.stage_id = ?",
            (stage("title_abstract"),),
        )
    }
    excluded = flow["members"]["records_excluded"]
    assert len(excluded) == flow["records_excluded"] == 27
    people: set[str] = set()
    for ref in excluded:
        if ref in adjudicated:
            j = adjudicated[ref]
            assert j["outcome"] == "exclude"
            people.add(human(j["event_id"])["identity_ref"])
        else:
            decisions = [d for d in ta[ref] if d["role"] == "screener"]
            assert len(decisions) == 2 and {d["outcome"] for d in decisions} == {"exclude"}
            for d in decisions:
                assert events[d["event_id"]]["type"] == "screening.decided"
                assert human(d["event_id"])["id"] == d["reviewer_id"]
                people.add(actors[d["reviewer_id"]]["identity_ref"])
    assert {p.split(":")[0] for p in people} == {
        "reviewer-a@example.org",
        "reviewer-b@example.org",
        "adjudicator@example.org",
    }

    # "Excluded after an extra screen the AI triggered: 2": each has its rescreen event, and a
    # human auditor screened it; one upheld the screeners, the other went to the adjudicator.
    rescreened = flow["members"]["title_abstract:excluded_after_ai_rescreen"]
    assert len(rescreened) == flow["ai_title_abstract"]["excluded_after_ai_rescreen"] == 2
    triggers = {
        json.loads(e["payload"])["item_ref"]: e
        for e in events.values()
        if e["type"] == "screening.ai_disagreement_rescreen"
        and json.loads(e["payload"])["stage_id"] == stage("title_abstract")
    }
    results = set()
    for ref in rescreened:
        payload = json.loads(triggers[ref]["payload"])
        assert payload["reason"] == "ai_include"
        (audit,) = [d for d in ta[ref] if d["role"] == "auditor"]
        assert human(audit["event_id"])["id"] == audit["reviewer_id"] == payload["auditor_id"]
        assert actors[audit["reviewer_id"]]["identity_ref"].startswith("auditor@example.org")
        results.add((audit["outcome"], ref in adjudicated))
    assert results == {("exclude", False), ("include", True)}

    # "Reports excluded at full text, COMP-CONTROL: 1": the adjudicator adopted that reason.
    (ref,) = flow["members"]["reason:COMP-CONTROL"]
    j = db.execute(
        "select j.* from screening_adjudication j join screening_conflict c on c.id = j.conflict_id"
        " where c.stage_id = ? and c.item_ref = ?",
        (stage("full_text"), ref),
    ).fetchone()
    assert (j["outcome"], j["reason_criterion_id"]) == ("exclude", "COMP-CONTROL")
    assert human(j["event_id"])["identity_ref"].startswith("adjudicator@example.org")
    payload = json.loads(events[j["event_id"]]["payload"])
    assert payload["item_ref"] == ref and payload["reason_criterion_id"] == "COMP-CONTROL"

    # "Studies included: 2": the included reports map to two studies in the frozen revision,
    # one of them by the lead's confirmed link.
    revision = flow["identity_revision_id"]
    studies = {
        r["study_id"]
        for r in db.execute(
            "select distinct study_id from identity_link where revision_id = ? and report_id in"
            f" ({','.join('?' * len(flow['members']['reports_included']))})",
            (revision, *flow["members"]["reports_included"]),
        )
    }
    assert len(studies) == flow["studies_included"] == 2
    revision_event = db.execute(
        "select event_id from identity_revision where id = ?", (revision,)
    ).fetchone()[0]
    assert events[revision_event]["type"] == "corpus.confirmed"
    assert human(revision_event)["identity_ref"].startswith("lead@example.org")

    # "Records identified: 35": every occurrence belongs to a search the lead ran.
    by_search = {
        r["source"]: r["n"]
        for r in db.execute(
            "select e.source, count(*) as n from source_occurrence o join search_execution e"
            " on e.id = o.execution_id group by e.source"
        )
    }
    assert sum(by_search.values()) == flow["records_identified"] == 35
    for e in events.values():
        if e["type"] == "search.executed":
            assert human(e["event_id"])["identity_ref"].startswith("lead@example.org")


def test_a_bundle_at_an_old_cutoff_names_no_one_from_a_later_stage(
    nrt: tuple[ReviewWorld, NrtSeqs], tmp_path: Path
) -> None:
    """Runs last: it opens a stage on the shared review."""
    world, seqs = nrt

    def actors(name: str) -> set[str]:
        with world.db.transaction() as tx:
            bundle = seal_bundle(
                tx,
                world.project_id,
                world.artifacts,
                tmp_path / name,
                signer=KEY,
                actor_id=world.people.lead,
                cutoff_seq=seqs.full_text,
                ledger=world.ledger,
            )
        return {r["identity_ref"] for r in _db(bundle).execute("select identity_ref from actor")}

    before = actors("before")
    # Later, the second question's screening opens with people who had no part before.
    with world.db.transaction() as tx:
        newcomers = [
            tx.execute(
                "insert into actor (identity_ref, kind) values (%s, 'human') returning id",
                (f"{role}@example.org:{new_id()}",),
            ).fetchone()["id"]  # type: ignore[index]
            for role in ("screener-c", "screener-d", "auditor-2", "adjudicator-2")
        ]
        world.screening.open_stage(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            actor_id=world.people.lead,
            spec=StageSpec(
                protocol_version_id=world.protocol.id,
                scope_id="combination-vs-single",
                topic_id="TRIALS",
                stage="title_abstract",
                records=tuple(world.records),
                screeners=(newcomers[0], newcomers[1]),
                auditor_id=newcomers[2],
                adjudicators=(newcomers[3],),
                sentinels=SENTINELS,
                judge_config_id=world.config.id,
            ),
        )
    after = actors("after")
    assert after == before
    assert not any(
        ref.split(":")[0].endswith(("-c@example.org", "-d@example.org")) for ref in after
    )
    assert not any(ref.startswith(("auditor-2", "adjudicator-2")) for ref in after)
