"""One review's searches on PubMed, Europe PMC, and ClinicalTrials.gov, from the HTTP API to the
corpus view: the same papers found twice become one report that names both searches, and a
registry entry becomes a report of the study its papers name. The searches replay real responses
through the gateway (tests/search/record_fixtures.py, `duplicates`)."""

import os
import threading
from collections import Counter
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from tests.search.record_fixtures import (
    DUPLICATES_CONTACT,
    DUPLICATES_CTGOV,
    DUPLICATES_DIR,
    DUPLICATES_EUROPEPMC,
    DUPLICATES_PUBMED,
)

from kittum.api import AppContext, create_app
from kittum.app import create_project
from kittum.corpus import StaleRevision, confirm, confirm_exact
from kittum.db import Database
from kittum.gateway import RecordedTransport, SourceGateway
from kittum.ledger import PostgresLedger
from kittum.settings import Settings
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.postgres

SEARCHES = [
    ("pubmed", "PubMed", DUPLICATES_PUBMED),
    ("europepmc", "Europe PMC", DUPLICATES_EUROPEPMC),
    ("ctgov", "ClinicalTrials.gov", DUPLICATES_CTGOV),
]


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, str, str]]:
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url)
    artifacts = MemoryStore()
    context = AppContext(
        settings=Settings(
            database_url=url, artifact_dir=tmp_path, contact_email=DUPLICATES_CONTACT
        ),
        db=db,
        artifacts=artifacts,
        # Offline, as the workbench's stack runs: the recorded responses, a cache of its own.
        gateway=SourceGateway(
            database=db,
            transport=RecordedTransport(DUPLICATES_DIR, contact_email=DUPLICATES_CONTACT),
            artifacts=artifacts,
            buckets="memory",
            cache="memory",
        ),
        ledger=PostgresLedger(),
    )
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        project, lead = create_project(
            conn,  # ty: ignore[invalid-argument-type]
            name="Duplicates across databases",
            lead_name="Morgan Lee",
            ledger=PostgresLedger(),
        )
    with TestClient(create_app(context)) as client:
        yield client, str(project), str(lead)
    db.close()


def test_records_found_twice_become_one_report_and_a_registry_entry_joins_its_study(
    api: tuple[TestClient, str, str],
) -> None:
    client, project, lead = api
    base = f"/api/projects/{project}"
    as_lead = {"X-Kittum-Reviewer": lead}

    executions: dict[str, dict[str, Any]] = {}
    for source, platform, strategy in SEARCHES:
        plan = {"topic_id": "nrt", "source": source, "platform": platform, "strategy": strategy}
        resp = client.post(f"{base}/search/run", json={"plan": plan, "actor_id": lead})
        assert resp.status_code == 200, resp.text
        executions[source] = resp.json()

    # Europe PMC: every record of its count, with the IDs deduplication matches on, and its own
    # account of the query, synonyms off, for PRISMA-S.
    epmc = executions["europepmc"]
    assert (epmc["reported_count"], epmc["retrieved_count"]) == (4, 4)
    assert epmc["query_translation"] == f"{DUPLICATES_EUROPEPMC}\nsynonym=FALSE"
    ends = next(r for r in epmc["records"] if r["pmid"] == "38354139")
    assert ends["provider_record_id"] == "MED:38354139"
    assert ends["doi"] == "10.1056/nejmoa2308815"
    assert ends["registry_ids"] == ["NCT03589989"]  # from its abstract
    assert ends["abstract"].startswith("Background: Electronic nicotine-delivery systems")
    assert ends["authors"][0] == "Auer R"
    assert {r["pmcid"] for r in epmc["records"]} >= {"PMC11831237", "PMC10803114"}
    pubmed_pmcids = {r["pmid"]: r["pmcid"] for r in executions["pubmed"]["records"]}
    assert pubmed_pmcids["38482972"] == "PMC11831237"

    resp = client.post(f"{base}/corpus/ingest", json={"actor_id": lead})
    assert resp.status_code == 201, resp.text
    proposals = client.get(f"{base}/corpus/proposals", headers=as_lead).json()
    duplicates = [p for p in proposals if p["relation"] == "same_record"]
    study_links = [p for p in proposals if p["relation"] == "same_study"]
    # Each paper Europe PMC found is in PubMed's result: four duplicates on their DOI, which the
    # lead may confirm at once. Two papers name their ClinicalTrials.gov entry.
    assert len(duplicates) == 4
    assert all(p["exact"] and p["evidence"][0].startswith("Same DOI ") for p in duplicates)
    assert sorted(p["evidence"] for p in study_links) == [
        ["Same NCT ID NCT02564315"],
        ["Same NCT ID NCT03589989"],
    ]
    # The e-cigarette trial's paper is in both databases and names its trial in both.
    assert sorted(len(p["occurrence_ids"]) for p in study_links) == [2, 3]

    view = client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()
    resp = client.post(
        f"{base}/corpus/confirm-exact",
        json={"revision_id": view["revision"]["id"], "actor_id": lead},
    )
    assert resp.status_code == 200, resp.text
    again = client.post(
        f"{base}/corpus/confirm-exact", json={"revision_id": resp.json()["id"], "actor_id": lead}
    )
    assert again.status_code == 400
    assert "no open duplicate" in again.json()["detail"]
    # From the revision the page loaded before: refused, not applied over the newer links.
    stale = client.post(
        f"{base}/corpus/confirm-exact",
        json={"revision_id": view["revision"]["id"], "actor_id": lead},
    )
    assert stale.status_code == 409, stale.text
    assert "Reload" in stale.json()["detail"]

    revision = resp.json()["id"]
    for proposal in study_links:
        resp = client.post(
            f"{base}/corpus/confirm",
            json={"revision_id": revision, "proposal_id": proposal["id"], "actor_id": lead},
        )
        assert resp.status_code == 200, resp.text
        revision = resp.json()["id"]

    counts = client.get(f"{base}/corpus/revisions/{revision}/counts").json()
    assert counts == {
        "records_identified_per_source": {"pubmed": 10, "europepmc": 4, "ctgov": 2},
        "total_records_identified": 16,
        "duplicates_removed": 4,
        "unique_records": 12,
        "studies_identified": 10,
    }

    view = client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()
    assert view["revision"]["id"] == revision
    searches = {s["id"]: s for s in view["searches"]}
    assert [s["platform"] for s in view["searches"]] == [
        "PubMed",
        "Europe PMC",
        "ClinicalTrials.gov",
    ]
    by_title = {r["records"][0]["title"]: r for r in view["reports"]}
    paper = by_title["Electronic Nicotine-Delivery Systems for Smoking Cessation."]
    # One report, two records: one per search that found it.
    assert [r["source"] for r in paper["records"]] == ["pubmed", "europepmc"]
    assert [searches[s]["platform"] for s in paper["search_ids"]] == ["PubMed", "Europe PMC"]
    study = [r for r in view["reports"] if r["study_id"] == paper["study_id"]]
    registry_record = next(r for r in study if r["records"][0]["source"] == "ctgov")["records"][0]
    assert len(study) == 2
    assert {r["records"][0]["provider_record_id"] for r in study} == {"38354139", "NCT03589989"}
    assert sum(len(r["records"]) for r in view["reports"]) == 16

    # Why: each confirmed proposal's evidence, for the records it joined.
    links = view["links"]
    assert Counter((link["relation"], link["linked_by"]) for link in links) == {
        ("same_record", "confirmed"): 4,
        ("same_study", "confirmed"): 2,
    }
    ecig_occurrences = {r["occurrence_id"] for r in paper["records"]}
    ecig = next(
        link
        for link in links
        if link["relation"] == "same_record" and set(link["occurrence_ids"]) == ecig_occurrences
    )
    assert ecig["evidence"] == ["Same DOI 10.1056/nejmoa2308815"]
    trial = next(link for link in links if link["evidence"] == ["Same NCT ID NCT03589989"])
    assert set(trial["occurrence_ids"]) == ecig_occurrences | {registry_record["occurrence_id"]}

    # The lead takes the registry entry out of the study: its link no longer explains anything.
    resp = client.post(
        f"{base}/corpus/split",
        json={
            "revision_id": revision,
            "occurrence_ids": [registry_record["occurrence_id"]],
            "level": "study",
            "actor_id": lead,
            "reason": "Checking the registry entry",
        },
    )
    assert resp.status_code == 200, resp.text
    view = client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()
    assert not any(link["evidence"] == ["Same NCT ID NCT03589989"] for link in view["links"])

    # Merged back by hand: the link names the lead's reason.
    resp = client.post(
        f"{base}/corpus/merge",
        json={
            "revision_id": view["revision"]["id"],
            "occurrence_ids": [
                registry_record["occurrence_id"],
                paper["records"][0]["occurrence_id"],
            ],
            "relation": "same_study",
            "actor_id": lead,
            "reason": "The paper reports this trial",
        },
    )
    assert resp.status_code == 200, resp.text
    view = client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()
    by_hand = [link for link in view["links"] if link["linked_by"] == "merged_by_hand"]
    assert [(link["relation"], link["evidence"]) for link in by_hand] == [
        ("same_study", ["The paper reports this trial"])
    ]
    # The split undid the confirmed link for good: joined again, the records show only the
    # lead's reason, not the NCT ID the lead had taken back.
    assert [
        link["evidence"]
        for link in view["links"]
        if registry_record["occurrence_id"] in link["occurrence_ids"]
    ] == [["The paper reports this trial"]]


def test_two_bulk_confirmations_at_once_confirm_each_duplicate_once(
    api: tuple[TestClient, str, str],
) -> None:
    """The lead presses `a` in two tabs at once. The second waits for the first, then finds the
    revision it read is no longer the newest: every duplicate is confirmed once, in one chain."""
    client, project, lead = api
    base = f"/api/projects/{project}"
    for source, platform, strategy in SEARCHES[:2]:
        plan = {"topic_id": "nrt", "source": source, "platform": platform, "strategy": strategy}
        resp = client.post(f"{base}/search/run", json={"plan": plan, "actor_id": lead})
        assert resp.status_code == 200, resp.text
    assert client.post(f"{base}/corpus/ingest", json={"actor_id": lead}).status_code == 201
    as_lead = {"X-Kittum-Reviewer": lead}
    parent = client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()["revision"]
    db = Database(os.environ["KITTUM_DATABASE_URL"], max_size=4)
    ledger = PostgresLedger()
    first_done = threading.Event()
    outcomes: dict[str, object] = {}

    def second() -> None:
        try:
            with db.transaction() as tx:
                outcomes["second"] = confirm_exact(
                    tx, UUID(project), UUID(parent["id"]), UUID(lead), ledger=ledger
                )
        except StaleRevision as e:
            outcomes["second"] = e
        outcomes["second_after_first"] = first_done.is_set()

    with db.transaction() as tx:
        outcomes["first"] = confirm_exact(
            tx, UUID(project), UUID(parent["id"]), UUID(lead), ledger=ledger
        )
        racer = threading.Thread(target=second)
        racer.start()
        racer.join(timeout=1.0)  # it waits on the first, which has not committed
        assert racer.is_alive()
        first_done.set()
    racer.join(timeout=30)
    db.close()

    assert isinstance(outcomes["second"], StaleRevision)
    assert outcomes["second_after_first"] is True
    counts = client.get(
        f"{base}/corpus/revisions/{outcomes['first'].id}/counts"  # type: ignore[attr-defined]
    ).json()
    assert counts["duplicates_removed"] == 4
    proposals = client.get(f"{base}/corpus/proposals", headers=as_lead).json()
    assert sum(p["status"] == "confirmed" for p in proposals) == 4
    events = client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()
    assert events["revision"]["id"] == str(outcomes["first"].id)  # type: ignore[attr-defined]


class PausingLedger:
    """The Postgres ledger, pausing after it appends a confirmation until `resume` is set: the
    moment a single confirmation holds the ledger and has yet to mark its proposal confirmed."""

    def __init__(self) -> None:
        self.inner = PostgresLedger()
        self.appended = threading.Event()
        self.resume = threading.Event()

    def append(self, tx: Any, draft: Any) -> Any:
        event = self.inner.append(tx, draft)
        if draft.type == "corpus.confirmed":
            self.appended.set()
            self.resume.wait(timeout=30)
        return event


def test_a_bulk_and_a_single_confirmation_at_once_neither_deadlock_nor_confirm_twice(
    api: tuple[TestClient, str, str],
) -> None:
    """The lead confirms one duplicate with Enter while `a` confirms them all, in another tab.
    Both take the project's confirmation lock before any proposal or the ledger: the bulk one
    waits for the single one, then finds the revision it read stale. Nothing deadlocks, and the
    duplicate is confirmed once."""
    client, project, lead = api
    base = f"/api/projects/{project}"
    for source, platform, strategy in SEARCHES[:2]:
        plan = {"topic_id": "nrt", "source": source, "platform": platform, "strategy": strategy}
        resp = client.post(f"{base}/search/run", json={"plan": plan, "actor_id": lead})
        assert resp.status_code == 200, resp.text
    assert client.post(f"{base}/corpus/ingest", json={"actor_id": lead}).status_code == 201
    as_lead = {"X-Kittum-Reviewer": lead}
    parent = UUID(
        client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()["revision"]["id"]
    )
    # The proposal the bulk confirmation takes first.
    first = next(
        p for p in client.get(f"{base}/corpus/proposals", headers=as_lead).json() if p["exact"]
    )
    db = Database(os.environ["KITTUM_DATABASE_URL"], max_size=4)
    pausing = PausingLedger()
    outcomes: dict[str, object] = {}

    def run(name: str, work: Any) -> None:
        try:
            with db.transaction() as tx:
                outcomes[name] = work(tx)
        except Exception as e:  # any failure, a deadlock too, is the outcome
            outcomes[name] = e

    single = threading.Thread(
        target=run,
        args=(
            "single",
            lambda tx: confirm(
                tx, UUID(project), parent, UUID(first["id"]), UUID(lead), ledger=pausing
            ),
        ),
    )
    bulk = threading.Thread(
        target=run,
        args=(
            "bulk",
            lambda tx: confirm_exact(
                tx, UUID(project), parent, UUID(lead), ledger=PostgresLedger()
            ),
        ),
    )
    single.start()
    assert pausing.appended.wait(timeout=30)
    bulk.start()
    # Longer than Postgres's deadlock_timeout (1 s), so a deadlock would be found before the
    # single confirmation goes on to mark its proposal.
    bulk.join(timeout=2.0)
    pausing.resume.set()
    single.join(timeout=30)
    bulk.join(timeout=30)
    db.close()

    assert not isinstance(outcomes["single"], Exception), outcomes["single"]
    assert isinstance(outcomes["bulk"], StaleRevision), outcomes["bulk"]
    proposals = client.get(f"{base}/corpus/proposals", headers=as_lead).json()
    assert [p["id"] for p in proposals if p["status"] == "confirmed"] == [first["id"]]
    latest = client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()["revision"]
    counts = client.get(f"{base}/corpus/revisions/{latest['id']}/counts").json()
    assert counts["duplicates_removed"] == 1


def test_a_split_keeps_the_reasons_for_the_records_it_leaves_together(
    api: tuple[TestClient, str, str],
) -> None:
    """A split takes its own records out of each link and nothing else. A record split out of
    its report keeps its study, and the reason it joined it. Records a middle record joined into
    one study stay one study when it leaves, with both reasons that held them."""
    client, project, lead = api
    base = f"/api/projects/{project}"
    as_lead = {"X-Kittum-Reviewer": lead}
    for source, platform, strategy in SEARCHES:
        plan = {"topic_id": "nrt", "source": source, "platform": platform, "strategy": strategy}
        assert client.post(f"{base}/search/run", json={"plan": plan, "actor_id": lead}).is_success
    assert client.post(f"{base}/corpus/ingest", json={"actor_id": lead}).status_code == 201

    def view() -> dict[str, Any]:
        return client.get(f"{base}/corpus/revisions/latest", headers=as_lead).json()

    def post(path: str, **body: Any) -> None:
        body |= {"revision_id": view()["revision"]["id"], "actor_id": lead}
        resp = client.post(f"{base}/corpus/{path}", json=body)
        assert resp.status_code == 200, resp.text

    post("confirm-exact")
    records = {
        (r["source"], r["provider_record_id"]): r["occurrence_id"]
        for report in view()["reports"]
        for r in report["records"]
    }
    ecig_pubmed = records[("pubmed", "38354139")]
    ecig_epmc = records[("europepmc", "MED:38354139")]

    def reasons(occurrence: str) -> list[tuple[str, list[str], list[str]]]:
        """The links that name a record: relation, evidence, and the records they hold."""
        return sorted(
            (link["relation"], link["evidence"], sorted(link["occurrence_ids"]))
            for link in view()["links"]
            if occurrence in link["occurrence_ids"]
        )

    def study_of(occurrence: str) -> str:
        return next(
            r["study_id"]
            for r in view()["reports"]
            if occurrence in {x["occurrence_id"] for x in r["records"]}
        )

    # P1: the Europe PMC copy leaves the paper's report but not its study. The DOI that made it
    # a duplicate still explains why it is in the study.
    post("split", occurrence_ids=[ecig_epmc], level="report")
    assert study_of(ecig_epmc) == study_of(ecig_pubmed)
    assert reasons(ecig_epmc) == [
        ("same_study", ["Same DOI 10.1056/nejmoa2308815"], sorted([ecig_pubmed, ecig_epmc]))
    ]

    # P2: the paper and another trial's paper, each joined to the registry entry by a reason of
    # its own. The registry entry leaves; the two papers stay one study, and both reasons stay.
    registry = records[("ctgov", "NCT03589989")]
    other = records[("pubmed", "38482972")]
    post(
        "merge",
        occurrence_ids=[ecig_pubmed, registry],
        relation="same_study",
        reason="Same NCT ID NCT-A",
    )
    post(
        "merge", occurrence_ids=[registry, other], relation="same_study", reason="Same NCT ID NCT-B"
    )
    post("split", occurrence_ids=[registry], level="study")
    assert study_of(other) == study_of(ecig_pubmed) != study_of(registry)
    held = [
        (link["evidence"], set(link["occurrence_ids"]))
        for link in view()["links"]
        if link["linked_by"] == "merged_by_hand"
    ]
    assert sorted(e for e, _ in held) == [["Same NCT ID NCT-A"], ["Same NCT ID NCT-B"]]
    assert all({ecig_pubmed, other} <= ids and registry not in ids for _, ids in held)
    assert reasons(registry) == []
