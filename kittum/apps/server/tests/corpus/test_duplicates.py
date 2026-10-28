"""One review's searches on PubMed, Europe PMC, and ClinicalTrials.gov, from the HTTP API to the
corpus view: the same papers found twice become one report that names both searches, and a
registry entry becomes a report of the study its papers name. The searches replay real responses
through the gateway (tests/search/record_fixtures.py, `duplicates`)."""

import os
from collections import Counter
from typing import TYPE_CHECKING, Any

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
