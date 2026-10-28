"""The audit trail over HTTP, on a review screened, adjudicated, and extracted through the API.

The review: the demo's citation search and a hand search that finds the Lesotho trial again, so the
lead confirms a duplicate. At title and abstract reviewer A includes the trial, reviewer B excludes
it, and the adjudicator includes it. The lead uploads the trial's PDF. At full text A excludes it
blind, is shown the AI's reading (the model saw no failing criterion), and revises to include; B
and the auditor (whose full-text audit sample it is) include it. The lead requests a Janus extraction and a third reviewer accepts a value from Table 1.

The lead traces the trial from the searches to the accepted value. Then someone with the database
superuser's rights edits the ledger, step by step as an attacker covering their tracks would, and
the audit check names each edit with the check it fails, while a bundle sealed before the edits
still verifies offline and no new bundle can be sealed. Runs on a rolled-back connection, with the
recorded judge and the Janus path in a worker on the same connection.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from kittum.api import AppContext, create_app
from kittum.app import job_handlers
from kittum.app.demo import DATA, PDFS, PROTOCOL, demo_judge
from kittum.cli import main
from kittum.core import new_id
from kittum.ledger import PostgresLedger, event_hash, payload_hash
from kittum.models import ModelRegistry
from kittum.runs import PostgresJobQueue, PostgresReceipts, Worker
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.documents.conftest import savepoint_db
from tests.extraction.support import schema

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Tx

pytestmark = pytest.mark.postgres

SENTINELS = PROTOCOL.parents[1] / "sentinels" / "nicotine-replacement.yaml"
LESOTHO = PDFS / "gerber2026-lesotho-t2d-crct.pdf"
VITAMIN_C = PDFS / "li2026-vitamin-c-kidney-biopsy-rct.pdf"
TRIAL = "Community health worker-led"
SCOPE = "nrt-vs-control"
PEM = Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)


class Review:
    """A review screened through the API, and the client that did it."""

    def __init__(self, pg: Tx, tmp_path: Path) -> None:
        key = tmp_path / "checkpoint.pem"
        key.write_bytes(PEM)
        queue = PostgresJobQueue()
        self.ctx = AppContext(
            settings=Settings(
                database_url="postgresql://unused",
                artifact_dir=tmp_path / "artifacts",
                checkpoint_key_path=key,
            ),
            db=savepoint_db(pg),
            artifacts=MemoryStore(),
            ledger=PostgresLedger(),
            judge=demo_judge(ModelRegistry.load()),
            queue=queue,
            receipts=PostgresReceipts(),
        )
        self.client = TestClient(create_app(self.ctx))
        self.worker = Worker(
            self.ctx.db, queue, job_handlers(self.ctx), name="audit", lease=timedelta(minutes=30)
        )
        self.people: dict[str, str] = {}
        self.pid = ""
        self.trial = ""

    def ok(self, response: Any, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    def work(self) -> None:
        while self.worker.run_once():
            pass

    def as_(self, person: str) -> dict[str, str]:
        return {"X-Kittum-Reviewer": self.people[person]}

    def audit(self, path: str, person: str = "Morgan", **params: Any) -> Any:
        return self.ok(
            self.client.get(
                f"/api/projects/{self.pid}/audit/{path}", params=params, headers=self.as_(person)
            )
        )

    def screen(self, person: str, stage: str, decide: Any) -> int:
        """Decide every item in the person's queue; `decide` returns the blind decision and, if
        the AI is revealed, the final one (None keeps the blind one)."""
        params = {"scope_id": SCOPE, "stage": stage, "limit": 50}
        base = f"/api/projects/{self.pid}/screening"
        items = self.ok(self.client.get(f"{base}/queue", params=params, headers=self.as_(person)))
        for item in items:
            now = datetime.now(UTC)
            timing = {
                "shown_at": now.isoformat(),
                "first_key_at": now.isoformat(),
                "decided_at": (now + timedelta(seconds=9)).isoformat(),
                "reveal_shown_at": None,
            }
            blind, revised = decide(item, self.as_(person))
            path = f"{base}/assignments/{item['assignment_id']}/decide"
            body = {"decision": blind, "timing": timing, "expected_etag": item["etag"]}
            result = self.ok(
                self.client.post(
                    path,
                    json=body | {"command_id": str(new_id()), "phase": "provisional"},
                    headers=self.as_(person),
                )
            )
            if result["phase"] == "provisional":  # the AI read it otherwise
                shown = (now + timedelta(seconds=10)).isoformat()
                final = body | {
                    "decision": revised or blind,
                    "expected_etag": result["etag"],
                    "timing": timing | {"reveal_shown_at": shown, "decided_at": shown},
                }
                self.ok(
                    self.client.post(
                        path,
                        json=final | {"command_id": str(new_id()), "phase": "final"},
                        headers=self.as_(person),
                    )
                )
        return len(items)

    def full_text_include(self, item: Any, headers: Any) -> dict[str, Any]:
        """Every criterion met, on the first protocol-term mention adopted as evidence."""
        source = self.ok(
            self.client.get(
                f"/api/projects/{self.pid}/screening/assignments/{item['assignment_id']}/full-text",
                headers=headers,
            )
        )
        mention = next(a for c in source["mentions"] for a in c["mentions"])
        anchor = self.ok(
            self.client.post(
                f"/api/layers/{source['layer_id']}/anchors",
                json={"quote": mention["quote"]["exact"], "start": mention["start"]},
            ),
            201,
        )
        return {
            "outcome": "include",
            "reason_criterion_id": None,
            "evidence_anchor_ids": [anchor["id"]],
            "comment": None,
            "criteria": [
                {
                    "criterion_id": c["criterion_id"],
                    "status": "met",
                    "evidence_anchor_ids": [anchor["id"]],
                }
                for c in source["mentions"]
            ],
        }


def include() -> dict[str, Any]:
    return {
        "outcome": "include",
        "reason_criterion_id": None,
        "evidence_anchor_ids": [],
        "comment": None,
        "criteria": [],
    }


def exclude(criterion: str) -> dict[str, Any]:
    return include() | {"outcome": "exclude", "reason_criterion_id": criterion}


def screened_review(pg: Tx, tmp_path: Path) -> Review:
    r = Review(pg, tmp_path)
    ok, client = r.ok, r.client
    project = ok(client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan"}), 201)
    r.pid = pid = project["id"]
    lead = project["members"][0]["actor_id"]
    r.people["Morgan"] = lead
    for name, role in (
        ("Sam", "reviewer"),
        ("Jun", "reviewer"),
        ("Rita", "auditor"),
        ("Elena", "adjudicator"),
        ("Ola", "reviewer"),  # screens nothing; accepts extracted values
    ):
        body = {"display_name": name, "roles": [role], "added_by": lead}
        r.people[name] = ok(client.post(f"/api/projects/{pid}/members", json=body), 201)["actor_id"]
    source = ok(client.post("/api/protocol/parse", json={"text": PROTOCOL.read_text()}))["source"]
    draft = ok(client.post(f"/api/projects/{pid}/protocol-versions", json=source), 201)
    ok(
        client.post(
            f"/api/projects/{pid}/protocol-versions/{draft['id']}/publish",
            json={"approved_by": lead},
        )
    )
    citations = (DATA / "citation_search.ris").read_text()
    # A hand search of a journal's issue finds the trial's article a second time.
    hand = citations[: citations.index("ER  -") + len("ER  -")] + "\n"
    for plan, data in (
        (
            {
                "topic_id": "TRIALS",
                "source": "citation_search",
                "platform": "Lists",
                "strategy": "x",
            },
            citations,
        ),
        (
            {
                "topic_id": "TRIALS",
                "source": "handsearch",
                "platform": "BMC Medicine, 2026 issues",
                "strategy": "Hand search of the 2026 issues",
            },
            hand,
        ),
    ):
        body = {"plan": plan, "data": data, "format": "ris", "actor_id": lead}
        ok(client.post(f"/api/projects/{pid}/search/import", json=body))
    corpus = ok(client.post(f"/api/projects/{pid}/corpus/ingest", json={"actor_id": lead}), 201)
    (duplicate,) = [p for p in corpus["proposals"] if p["relation"] == "same_record"]
    ok(
        client.post(
            f"/api/projects/{pid}/corpus/confirm",
            json={
                "revision_id": corpus["identity_revision_id"],
                "proposal_id": duplicate["id"],
                "actor_id": lead,
            },
        )
    )
    sentinels = ok(
        client.post(
            f"/api/projects/{pid}/screening/sentinels/parse", json={"text": SENTINELS.read_text()}
        )
    )
    stage = {"scope_id": SCOPE, "stage": "title_abstract", "actor_id": lead}
    ok(
        client.post(f"/api/projects/{pid}/screening/stages", json=stage | {"sentinels": sentinels}),
        201,
    )
    r.work()

    # Title and abstract: A includes the trial, B excludes it on the population, and the
    # adjudicator includes it.
    def b(item: Any, _: Any) -> tuple[Any, Any]:
        return (exclude("POP"), None) if item["title"].startswith(TRIAL) else (include(), None)

    assert r.screen("Sam", "title_abstract", lambda *_: (include(), None)) == 4  # and a sentinel
    assert r.screen("Jun", "title_abstract", b) == 4
    r.screen("Rita", "title_abstract", lambda *_: (include(), None))
    params = {"scope_id": SCOPE, "stage": "title_abstract"}
    (conflict,) = ok(
        client.get(
            f"/api/projects/{pid}/screening/adjudications", params=params, headers=r.as_("Elena")
        )
    )
    assert conflict["title"].startswith(TRIAL)
    now = datetime.now(UTC).isoformat()
    ok(
        client.post(
            f"/api/projects/{pid}/screening/adjudications/{conflict['conflict_id']}/decide",
            json={
                "command_id": str(new_id()),
                "expected_etag": conflict["etag"],
                "adopted": None,
                "decision": include(),
                "timing": {"shown_at": now, "first_key_at": now, "decided_at": now},
            },
            headers=r.as_("Elena"),
        )
    )

    # Full text: the lead uploads the trial; both reviewers and the auditor include it.
    reports = ok(client.get(f"/api/projects/{pid}/full-texts", headers=r.as_("Morgan")))
    trial = next(t for t in reports if t["title"].startswith(TRIAL))
    r.trial = trial["item_ref"]
    text = ok(
        client.post(
            f"/api/projects/{pid}/full-texts/{trial['item_ref']}",
            params={"actor_id": lead, "licence": "CC BY 4.0"},
            content=LESOTHO.read_bytes(),
            headers={"Content-Type": "application/pdf"},
        ),
        201,
    )
    ok(
        client.post(
            f"/api/projects/{pid}/full-text-sentinels",
            params={"actor_id": lead, "title": "Vitamin C", "expected_outcome": "exclude"},
            content=VITAMIN_C.read_bytes(),
            headers={"Content-Type": "application/pdf"},
        ),
        201,
    )
    ft = {"scope_id": SCOPE, "stage": "full_text", "actor_id": lead}
    ok(client.post(f"/api/projects/{pid}/screening/stages", json=ft), 201)
    r.work()

    # Full text: A excludes the trial on the intervention, sees the AI's reading, and revises.
    def a(item: Any, headers: Any) -> tuple[Any, Any]:
        included = r.full_text_include(item, headers)
        if not item["title"].startswith(TRIAL):
            return included, None
        criteria = [
            c | {"status": "not_met"} if c["criterion_id"] == "INT" else c
            for c in included["criteria"]
        ]
        blind = included | {
            "outcome": "exclude",
            "reason_criterion_id": "INT",
            "criteria": criteria,
        }
        return blind, included

    assert r.screen("Sam", "full_text", a) >= 1
    for person in ("Jun", "Rita"):
        assert r.screen(person, "full_text", lambda i, h: (r.full_text_include(i, h), None)) >= 1

    # Extraction on the Janus path; a third reviewer accepts the participants from Table 1.
    run = ok(
        client.post(
            f"/api/projects/{pid}/extraction/runs",
            json={
                "command_id": str(new_id()),
                "actor_id": lead,
                "layer_id": text["layer_id"],
                "study_id": trial["study_id"],
                "report_id": trial["report_id"],
                "extraction_schema": schema("lesotho").model_dump(mode="json"),
                "report": {"study_label": "NCT05596773", "title": trial["title"]},
            },
        ),
        202,
    )
    r.work()
    fields = ok(
        client.get(
            f"/api/projects/{pid}/extraction/runs/{run['id']}/fields", headers=r.as_("Morgan")
        )
    )
    participants = next(f for f in fields if f["key"]["endpoint"] == "participants")
    accepted = ok(
        client.post(
            f"/api/projects/{pid}/extraction/fields/{participants['id']}/accept",
            json={
                "command_id": str(new_id()),
                "path": "janus",
                "source_viewed_ms": 900,
                "reviewer_id": r.people["Ola"],
            },
        )
    )
    assert accepted["value"] == "52"
    return r


def test_the_lead_traces_a_record_from_its_searches_to_an_accepted_value(
    pg: Tx, tmp_path: Path
) -> None:
    r = screened_review(pg, tmp_path)
    people = {v: k for k, v in r.people.items()}

    # From the PRISMA count: the trial is among the reports included at full text.
    now = r.audit("as-of")
    (flow,) = [f for f in now["flows"] if f["scope_id"] == SCOPE]
    assert flow["members"]["reports_included"] == [r.trial]
    assert now["items"][r.trial]["title"].startswith(TRIAL)
    trace = r.audit(f"records/{now['items'][r.trial]['trace_ref']}/trace")
    events = trace["events"]
    assert trace["report_id"] == r.trial and trace["title"].startswith(TRIAL)

    # Found twice, each search with the raw response the record was read from.
    assert sorted(o["source"] for o in trace["occurrences"]) == ["citation_search", "handsearch"]
    searches = [e for e in events if e["type"] == "search.executed"]
    assert {e["seq"] for e in searches} == {o["search_seq"] for o in trace["occurrences"]}
    for o in trace["occurrences"]:
        (search,) = [e for e in searches if e["seq"] == o["search_seq"]]
        (own,) = [a for a in search["artifacts"] if a["label"].startswith("Raw response this")]
        assert own["sha256"] == o["raw_hash"] and own["stored"]

    # Every stage in order, by the people who did it.
    def first(kind: str) -> int:
        return next(i for i, e in enumerate(events) if e["kind"] == kind)

    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert (
        first("search")
        < first("identity")
        < first("stage")
        < first("ai")
        < first("decision")
        < first("adjudication")
        < first("full_text")
        < first("extraction")
    )
    (duplicate,) = [e for e in events if e["type"] == "corpus.confirmed"]
    assert duplicate["actor_id"] == r.people["Morgan"] and duplicate["kind"] == "identity"

    def decisions(stage: str) -> list[tuple[str, str, str]]:
        return [
            (people[e["actor_id"]], e["payload"]["phase"], e["payload"]["outcome"])
            for e in events
            if e["type"] == "screening.decided" and e["stage"] == stage
        ]

    ta = decisions("title_abstract")
    assert ("Sam", "provisional", "include") in ta and ("Jun", "provisional", "exclude") in ta
    ft = decisions("full_text")
    blind = ft.index(("Sam", "provisional", "exclude"))
    assert ft[blind + 1 :].count(("Sam", "final", "include")) == 1
    assert {who for who, _, _ in ft} == {"Sam", "Jun", "Rita"}
    (rita,) = [
        e for e in events if e["stage"] == "full_text" and people.get(e["actor_id"]) == "Rita"
    ]
    assert rita["summary"].endswith("(the auditor's audit sample)")
    (adjudicated,) = [e for e in events if e["kind"] == "adjudication"]
    assert people[adjudicated["actor_id"]] == "Elena"
    assert adjudicated["payload"]["outcome"] == "include"
    assert adjudicated["seq"] > max(
        e["seq"] for e in events if e["stage"] == "title_abstract" and e["kind"] == "decision"
    )

    # The AI read the full text before anyone decided, and A saw its reading only after deciding.
    (verdict,) = [
        e for e in events if e["type"] == "judge.verdict_recorded" and e["stage"] == "full_text"
    ]
    (reveal,) = trace["reveals"]
    assert reveal["reviewer"] == "Sam" and reveal["stage"] == "full_text"
    assert verdict["seq"] < reveal["decision_seq"] < reveal["final_seq"]
    assert reveal["shown_at"] > reveal["decided_at"] and reveal["kept"] is False
    (blind,) = [e for e in events if e["seq"] == reveal["decision_seq"]]
    assert people[blind["actor_id"]] == "Sam" and blind["payload"]["phase"] == "provisional"
    assert any(f["label"] == "AI reading shown after this decision" for f in blind["facts"])
    assert not any(e["payload"].get("reveal") for e in events if people.get(e["actor_id"]) == "Jun")

    # The full text and the accepted value, with the PDF and the anchor they rest on.
    (upload,) = [e for e in events if e["type"] == "project.full_text_attached"]
    assert people[upload["actor_id"]] == "Morgan"
    assert {"label": "Licence", "value": "CC BY 4.0"} in upload["facts"]
    (pdf,) = upload["artifacts"]
    served = r.client.get(
        f"/api/projects/{r.pid}/audit/artifacts/{pdf['sha256']}", headers=r.as_("Morgan")
    )
    assert served.status_code == 200 and served.content == LESOTHO.read_bytes()
    assert served.headers["content-type"] == "application/pdf"
    (value,) = [e for e in events if e["type"] == "extraction.value_accepted"]
    assert people[value["actor_id"]] == "Ola" and value["payload"]["value"] == "52"
    facts = {f["label"]: f["value"] for f in value["facts"]}
    assert facts["Anchor"] == value["payload"]["anchor_id"] and "52" in facts["Quote"]
    assert int(facts["Page"]) >= 1

    # Each event sits in the chain: its prev hash is the hash of the event before it.
    for e in events[:: max(1, len(events) // 6)]:
        (before,) = r.audit("events", after_seq=e["seq"] - 2, limit=1)["events"]
        assert before["seq"] == e["seq"] - 1 and before["event_hash"] == e["prev_hash"]
    assert [e["seq"] for e in r.audit("events", ref=r.trial, limit=500)["events"]] == [
        e["seq"] for e in events
    ]
    mine = r.audit("events", type="screening.", actor_id=r.people["Sam"], limit=500)["events"]
    assert mine and {(e["actor_id"], e["type"][:10]) for e in mine} == {
        (r.people["Sam"], "screening.")
    }

    # Nobody but the lead sees any of it: not the screeners, the auditor, or the adjudicator.
    for person in ("Sam", "Jun", "Rita", "Elena", "Ola"):
        for path in (
            f"records/{r.trial}/trace",
            "events",
            "verify",
            "checkpoints",
            "milestones",
            "as-of",
            f"artifacts/{pdf['sha256']}",
        ):
            refused = r.client.get(f"/api/projects/{r.pid}/audit/{path}", headers=r.as_(person))
            assert refused.status_code == 403, (person, path)
            assert r.trial not in refused.text and "exclude" not in refused.text
        signed = r.client.post(
            f"/api/projects/{r.pid}/audit/checkpoints", json={"actor_id": r.people[person]}
        )
        assert signed.status_code == 403, person


def _set(pg: Tx, seq: int, pid: str, **columns: Any) -> None:
    """Edit a ledger row as the database superuser, with the append-only trigger out of the way."""
    pg.execute("set local session_replication_role = replica")
    assignments = ", ".join(f"{c} = %s" for c in columns)
    pg.execute(
        f"update audit_event set {assignments} where project_id = %s and seq = %s",
        (*(Jsonb(v) if c == "payload" else v for c, v in columns.items()), pid, seq),
    )
    pg.execute("set local session_replication_role = origin")


def test_a_tampered_decision_is_pinpointed_and_a_bundle_sealed_before_still_verifies(
    pg: Tx, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    r = screened_review(pg, tmp_path)
    base = f"/api/projects/{r.pid}"
    lead = {"actor_id": r.people["Morgan"]}
    assert r.audit("verify")["ok"]
    signed = r.ok(r.client.post(f"{base}/audit/checkpoints", json=lead), 201)
    sealed = r.ok(r.client.post(f"{base}/reports/bundle", json=lead))
    checkpoint = signed["checkpoint"]
    assert sealed["cutoff_seq"] == checkpoint["seq"]
    assert main(["verify", sealed["path"]]) == 0

    # B's blind exclusion of the trial at title and abstract becomes an inclusion.
    trace = r.audit(f"records/{r.trial}/trace")
    (target,) = [
        e
        for e in trace["events"]
        if e["type"] == "screening.decided"
        and e["actor_id"] == r.people["Jun"]
        and e["stage"] == "title_abstract"
    ]
    seq = target["seq"]
    forged = target["payload"] | {"outcome": "include", "reason_criterion_id": None}
    _set(pg, seq, r.pid, payload=forged)

    def findings() -> list[tuple[int, str, str]]:
        audit = r.audit("verify")
        assert not audit["ok"]
        return [(f["seq"], f["check"], f["problem"]) for f in audit["findings"]]

    # 1. The payload alone: its hash no longer matches. Nothing else is touched.
    audit = r.audit("verify")
    (finding,) = audit["findings"]
    assert finding == {
        "seq": seq,
        "event_id": target["event_id"],
        "type": "screening.decided",
        "check": "payload_hash",
        "problem": "payload_mismatch",
        "stored": target["payload_hash"],
        "expected": payload_hash(forged),
        "reason": finding["reason"],
    }
    assert audit["verified_through_seq"] == seq - 1
    assert audit["head_seq"] == audit["events"] > seq
    (status,) = audit["checkpoints"]
    assert status["signature"] == "valid" and status["head_matches"] is True
    summary = r.ok(r.client.get(f"{base}/ledger/verify"))["verify"]
    assert (summary["first_bad_seq"], summary["problem"]) == (seq, "payload_mismatch")

    # The bundle sealed before the edit still verifies: it holds its own copy of the chain. A new
    # bundle cannot be sealed over a chain that does not verify.
    assert main(["verify", sealed["path"]]) == 0
    refused = r.client.post(f"{base}/reports/bundle", json=lead)
    assert refused.status_code == 409 and f"seq {seq}" in refused.json()["detail"]
    capsys.readouterr()

    # 2. The payload hash recomputed to match: the event hash no longer does.
    _set(pg, seq, r.pid, payload_hash=payload_hash(forged))
    assert findings() == [(seq, "event_hash", "event_hash_mismatch")]

    # 3. The event hash recomputed too: the next event's link to it breaks.
    with r.ctx.db.transaction() as tx:
        events = PostgresLedger().events(tx, r.pid)
    rehashed = events[seq - 1].model_copy(update={"payload": forged})

    def rehash(e: Any, prev: str) -> str:
        return event_hash(
            project_id=e.project_id,
            seq=e.seq,
            event_id=e.event_id,
            type=e.type,
            schema_version=e.schema_version,
            actor_id=e.actor_id,
            recorded_at=e.recorded_at,
            payload_hash=payload_hash(e.payload),
            prev_hash=prev,
        )

    new = rehash(rehashed, events[seq - 2].event_hash)
    _set(pg, seq, r.pid, event_hash=new)
    assert findings() == [(seq + 1, "prev_hash", "prev_hash_mismatch")]

    # 4. Every later event re-linked and re-hashed: the chain is consistent again, and only the
    # signed checkpoint, whose copy lives outside the database, shows it was rewritten.
    prev = new
    for e in events[seq:]:
        prev_new = rehash(e, prev)
        _set(pg, e.seq, r.pid, prev_hash=prev, event_hash=prev_new)
        prev = prev_new
    audit = r.audit("verify")
    assert [(f["seq"], f["check"], f["problem"]) for f in audit["findings"]] == [
        (checkpoint["seq"], "checkpoint", "rewritten")
    ]
    (status,) = audit["checkpoints"]
    assert status["signature"] == "valid" and status["head_matches"] is False
    (rewritten,) = audit["findings"]
    assert (
        rewritten["expected"] == checkpoint["head_hash"]
        and rewritten["stored"] != (checkpoint["head_hash"])
    )

    # 5. An event deleted before the edit: its seq is missing.
    pg.execute("set local session_replication_role = replica")
    pg.execute("delete from audit_event where project_id = %s and seq = %s", (r.pid, seq - 1))
    pg.execute("set local session_replication_role = origin")
    assert findings() == [
        (seq - 1, "seq", "seq_gap"),
        (checkpoint["seq"], "checkpoint", "rewritten"),
    ]
    assert main(["verify", sealed["path"]]) == 0
