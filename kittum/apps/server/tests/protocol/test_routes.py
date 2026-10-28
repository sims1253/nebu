import os
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from kittum.api import AppContext, create_app
from kittum.core import new_id
from kittum.db import Database
from kittum.ledger import PostgresLedger
from kittum.profiles import load_profiles
from kittum.protocol import PUBLISHED_EVENT, compile
from kittum.settings import Settings
from kittum.storage import FilesystemStore
from tests.protocol.support import example_source, example_text, seed_project

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

AMNOG = "capivasertib"


def _hash(name: str) -> str:
    load_profiles()
    return compile(example_text(name)).content_hash


def test_a_pubmed_strategy_may_use_every_field_tag_pubmed_lists() -> None:
    """Tags from PubMed's "Search Field Descriptions and Tags", short and long, proximity
    included: a strategy with them compiles. A tag PubMed does not list is a warning beside the
    strategy, as PubMed itself only warns; unbalanced syntax is a problem."""
    client = TestClient(create_app())
    source = example_source("nicotine-replacement")
    medline = source["search_topics"][0]["source_plan"]["sources"][1]
    assert medline["id"] == "medline"
    medline["strategy"] = (
        '(nicotine[mh] OR "nicotine patch"[tiab] OR NRT[Title/Abstract] OR "patch quit"[tiab:~3]'
        ' OR "Nicotinic Agonists"[pa] OR "Smoking Cessation"[majr] OR therapy[sh:noexp])'
        " AND (2024[epdat] OR 2023[ppdat] OR 2024/01/01[edat] OR 2024[Date - Entry]"
        " OR 2024[mhda] OR 2024[crdt] OR 2024[lr] OR 2020:2024[dp])"
        " AND (10.1000/xyz[aid] OR funding[cois] OR smith john[fir] OR e123[lid]"
        ' OR 0372354[jid] OR hughes j[ps] OR "randomized controlled trial"[Publication Type])'
        " AND english[la] AND medline[sb] AND humans[filter]"
        # PubMed's own Clinical Queries use these unexpanded and subheading forms.
        ' AND (review[pt:noexp] OR "clinical trial"[Publication Type:noexp]'
        " OR diagnosis[Subheading:noexp] OR therapy[subheading] OR therapy[MeSH Subheading:noexp]"
        ' OR "Smoking Cessation"[MeSH Major Topic:noexp] OR review[ptyp:noexp])'
    )
    report = client.post("/api/protocol/validate", json=source).json()
    assert (report["ok"], report["issues"], report["warnings"]) == (True, [], [])

    medline["strategy"] += " AND addiction[jt]"
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is True
    assert report["warnings"] == [
        {
            "path": "search_topics[0].source_plan.sources[1].strategy",
            "message": "[jt] is not a PubMed field tag; PubMed would search it as text",
        }
    ]
    medline["strategy"] = "(" + medline["strategy"]
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is False
    assert report["issues"][0]["message"] == "1 opening parenthesis is not closed"


def test_a_europe_pmc_strategy_is_checked_like_a_pubmed_one() -> None:
    """A source on Europe PMC uses Europe PMC's fields, in any case, and its date ranges; a field
    Europe PMC does not know is a warning, since Europe PMC finds nothing for it without saying
    so; unbalanced syntax is a problem."""
    client = TestClient(create_app())
    source = example_source("nicotine-replacement")
    sources = source["search_topics"][0]["source_plan"]["sources"]
    sources.append(
        {
            "id": "europepmc",
            "kind": "bibliographic",
            "name": "Europe PMC",
            "platform": "Europe PMC",
            "strategy": (
                '(TITLE_ABS:"nicotine replacement" OR title:NRT OR MESH:"Smoking Cessation")'
                ' AND PUB_TYPE:"Randomized Controlled Trial" AND (SRC:MED OR SRC:PPR)'
                " AND FIRST_PDATE:[2024-01-01 TO 2024-03-31] AND LANG:eng"
                # A colon inside a phrase or after a number is no field.
                ' AND NOT TITLE:"Erratum: correction" AND NOT covid-19:x'
            ),
        }
    )
    at = f"search_topics[0].source_plan.sources[{len(sources) - 1}].strategy"
    report = client.post("/api/protocol/validate", json=source).json()
    assert (report["ok"], report["issues"], report["warnings"]) == (True, [], [])

    sources[-1]["strategy"] += " AND JOURNAL_TITLE:Addiction AND Tiab:nicotine"
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is True
    assert report["warnings"] == [
        {
            "path": at,
            "message": "Europe PMC has no field JOURNAL_TITLE. It finds no records for a search"
            " in it.",
        },
        {
            "path": at,
            "message": "Europe PMC has no field Tiab. It finds no records for a search in it.",
        },
    ]
    sources[-1]["strategy"] = "(" + sources[-1]["strategy"]
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is False
    assert report["issues"] == [{"path": at, "message": "1 opening parenthesis is not closed"}]


def test_validate_reports_the_hash_or_the_issues() -> None:
    client = TestClient(create_app())
    for name in (AMNOG, "nicotine-replacement"):
        ok = client.post("/api/protocol/validate", json=example_source(name))
        assert ok.status_code == 200
        assert ok.json() == {
            "ok": True,
            "content_hash": _hash(name),
            "issues": [],
            "warnings": [],
        }

    broken = example_source(AMNOG)
    broken["search_topics"][0]["freshness"]["max_age_months"] = 6
    report = client.post("/api/protocol/validate", json=broken).json()
    assert report["ok"] is False
    assert report["issues"] == [
        {
            "path": "search_topics[0].freshness.max_age_months",
            "message": "Modul 4 allows searches at most 3 months old at submission, got 6",
        }
    ]


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    """An app on the test database. Rows commit, so each test makes its own project."""
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        project, approver, system = seed_project(conn)
    context = AppContext(
        settings=Settings(database_url=url, artifact_dir=tmp_path),
        db=Database(url, max_size=2),
        artifacts=FilesystemStore(tmp_path),
        ledger=PostgresLedger(),
    )
    with TestClient(create_app(context)) as client:
        yield client, {"project": project, "approver": approver, "system": system, "url": url}
    context.db.close()


@pytest.mark.postgres
def test_api_saves_and_publishes_the_example(api: tuple[TestClient, dict[str, Any]]) -> None:
    client, ids = api
    base = f"/api/projects/{ids['project']}/protocol-versions"

    created = client.post(base, json=example_source(AMNOG))
    assert created.status_code == 201
    draft = created.json()
    assert draft["status"] == "draft"
    assert draft["version"]["content_hash"] == _hash(AMNOG)
    assert client.post(base, json=example_source(AMNOG)).json()["id"] == draft["id"]

    denied = client.post(f"{base}/{draft['id']}/publish", json={"approved_by": str(ids["system"])})
    assert denied.status_code == 403  # not the project's lead

    published = client.post(
        f"{base}/{draft['id']}/publish", json={"approved_by": str(ids["approver"])}
    )
    assert published.status_code == 200
    body = published.json()
    assert body["status"] == "published"
    assert body["approved_by"] == str(ids["approver"])

    again = client.post(f"{base}/{draft['id']}/publish", json={"approved_by": str(ids["approver"])})
    assert again.status_code == 409

    listed = client.get(base).json()
    assert [(v["id"], v["status"], v["version_label"]) for v in listed] == [
        (draft["id"], "published", "1.0")
    ]
    fetched = client.get(f"{base}/{draft['id']}").json()
    assert fetched == body
    protocol = fetched["version"]["protocol"]
    assert protocol["profile"] == "gba_amnog"
    assert [q["id"] for q in protocol["questions"]] == ["a1", "a2", "b1", "b2"]
    assert protocol["questions"][2]["extensions"]["zvt"]["nach_aerztlicher_massgabe"] is True
    assert client.get(f"{base}/{new_id()}").status_code == 404

    with psycopg.connect(ids["url"], row_factory=dict_row) as conn:
        event = conn.execute(
            "select type, actor_id, payload from audit_event where event_id = %s",
            (body["approved_event_id"],),
        ).fetchone()
    assert event is not None
    assert (event["type"], str(event["actor_id"])) == (PUBLISHED_EVENT, str(ids["approver"]))
    assert event["payload"]["content_hash"] == draft["version"]["content_hash"]


@pytest.mark.postgres
def test_api_rejects_an_invalid_draft(api: tuple[TestClient, dict[str, Any]]) -> None:
    client, ids = api
    broken = example_source(AMNOG)
    broken["questions"][0]["comparators"] = []
    response = client.post(f"/api/projects/{ids['project']}/protocol-versions", json=broken)
    assert response.status_code == 422
    (issue,) = response.json()["detail"]["issues"]
    assert issue == {
        "path": "questions[0].comparators",
        "message": "the zVT options are the comparators; list at least one",
    }
    assert client.get(f"/api/projects/{ids['project']}/protocol-versions").json() == []


MALFORMED = {
    "questions is not a list": (
        lambda s: s.__setitem__("questions", 1),
        "questions",
        "Input should be a valid tuple, got 1",
    ),
    "default language is a list": (
        lambda s: s.__setitem__("default_language", ["de"]),
        "default_language",
        "Input should be a valid string",
    ),
    "a topic is not a mapping": (
        lambda s: s["search_topics"].append(7),
        "search_topics[1]",
        "Input should be a valid dictionary or instance of SearchTopic",
    ),
}


@pytest.mark.parametrize("case", MALFORMED.keys())
def test_validate_reports_malformed_sources_as_issues(case: str) -> None:
    edit, path, message = MALFORMED[case]
    source = example_source()
    edit(source)
    response = TestClient(create_app()).post("/api/protocol/validate", json=source)
    assert response.status_code == 200
    report = response.json()
    assert report["ok"] is False
    assert any(i["path"] == path and message in i["message"] for i in report["issues"]), report


@pytest.mark.postgres
def test_api_rejects_a_malformed_draft_with_422(api: tuple[TestClient, dict[str, Any]]) -> None:
    client, ids = api
    source = example_source()
    source["questions"] = 1
    source["default_language"] = ["de"]
    response = client.post(f"/api/projects/{ids['project']}/protocol-versions", json=source)
    assert response.status_code == 422
    paths = {i["path"] for i in response.json()["detail"]["issues"]}
    assert {"questions", "default_language"} <= paths
