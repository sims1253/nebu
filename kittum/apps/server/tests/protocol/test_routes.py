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


def test_validate_reports_the_hash_or_the_issues() -> None:
    client = TestClient(create_app())
    for name in (AMNOG, "nicotine-replacement"):
        ok = client.post("/api/protocol/validate", json=example_source(name))
        assert ok.status_code == 200
        assert ok.json() == {"ok": True, "content_hash": _hash(name), "issues": []}

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
    assert denied.status_code == 409

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
