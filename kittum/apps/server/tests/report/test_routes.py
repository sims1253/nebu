"""Reports over HTTP: the targets a project offers, a render and its downloads, and a sealed
bundle that verifies."""

import sqlite3
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.cli import main
from kittum.core import sha256_hex
from kittum.ledger import SigningKey
from kittum.settings import Settings
from tests.report.support import NRT_SCOPE

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.report.support import NrtSeqs, ReviewWorld

pytestmark = pytest.mark.postgres

PEM = Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
KEY = SigningKey.from_pem(PEM)


@pytest.fixture
def client(nrt: tuple[ReviewWorld, NrtSeqs], tmp_path: Path) -> Iterator[TestClient]:
    world, _ = nrt
    key = tmp_path / "checkpoint.pem"
    key.write_bytes(PEM)
    settings = Settings(
        database_url="postgresql://unused",
        artifact_dir=tmp_path / "artifacts",
        checkpoint_key_path=key,
    )
    context = AppContext(
        settings=settings, db=world.db, artifacts=world.artifacts, ledger=world.ledger
    )
    with TestClient(create_app(context)) as client:
        yield client


def test_targets_render_download_and_seal_over_http(
    nrt: tuple[ReviewWorld, NrtSeqs],
    capivasertib: ReviewWorld,
    client: TestClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, seqs = nrt
    base = f"/api/projects/{world.project_id}/reports"
    as_lead = {"X-Kittum-Reviewer": str(world.people.lead)}

    # A project without a profile offers the core targets; a G-BA dossier adds its own.
    listed = client.get(f"{base}/targets").json()
    assert [t["id"] for t in listed] == [
        "prisma",
        "ris",
        "search_report",
        "ai_disclosure",
        "bundle",
    ]
    assert [t["id"] for t in listed if not t["renderable"]] == ["bundle"]
    dossier = client.get(f"/api/projects/{capivasertib.project_id}/reports/targets").json()
    assert {t["id"] for t in dossier if t["kind"] == "profile"} == {
        "gba_amnog.anhang_4a",
        "gba_amnog.anhang_4b",
        "gba_amnog.anhang_4c",
        "gba_amnog.anhang_4d",
        "gba_amnog.justification_4235",
        "gba_amnog.anlage_ii_1",
    }

    # Render the flow at a cutoff; every file and the index download by hash.
    response = client.post(
        f"{base}/targets/prisma/render",
        json={
            "actor_id": str(world.people.lead),
            "scope_id": NRT_SCOPE,
            "cutoff_seq": seqs.full_text,
        },
    )
    assert response.status_code == 200, response.text
    rendered = response.json()
    assert rendered["artifact"]["cutoff_seq"] == seqs.full_text
    for f in rendered["artifact"]["files"]:
        download = client.get(f"{base}/artifacts/{f['sha256']}", headers=as_lead)
        assert download.status_code == 200
        assert sha256_hex(download.content) == f["sha256"]
        assert download.headers["content-type"].startswith(f["media_type"].split(";")[0])
    index = client.get(f"{base}/artifacts/{rendered['sha256']}", headers=as_lead).json()
    assert index == rendered["artifact"]
    with world.db.transaction() as tx:
        (event,) = [
            e for e in world.ledger.events(tx, world.project_id) if e.type == "report.rendered"
        ]
    assert event.payload["index_hash"] == rendered["sha256"]
    assert event.actor_id == world.people.lead

    # Only what was rendered for this project downloads; other artifacts stay out of reach.
    raw = world.executions[0].raw_artifact_hashes[0]
    assert world.artifacts.exists(raw)
    assert client.get(f"{base}/artifacts/{raw}", headers=as_lead).status_code == 404
    other = f"/api/projects/{capivasertib.project_id}/reports/artifacts/{rendered['sha256']}"
    other_lead = {"X-Kittum-Reviewer": str(capivasertib.people.lead)}
    assert client.get(other, headers=other_lead).status_code == 404

    # Reports show every reviewer's decisions and the AI's output: a screener, the auditor, and
    # the (blinded) adjudicator may not render, download, or seal them, and nothing is recorded.
    people = world.people
    for who in (people.a, people.auditor, people.adjudicator, people.model):
        actor = {"actor_id": str(who)}
        for method, path, kwargs in (
            ("POST", "/targets/ai_disclosure/render", {"json": actor}),
            ("POST", "/targets/ris/render", {"json": {**actor, "scope_id": NRT_SCOPE}}),
            (
                "GET",
                f"/artifacts/{rendered['sha256']}",
                {"headers": {"X-Kittum-Reviewer": str(who)}},
            ),
            ("POST", "/bundle", {"json": actor}),
        ):
            refused = client.request(method, f"{base}{path}", **kwargs)
            assert refused.status_code == 403, (who, path, refused.text)
    assert not (bundles := tmp_path / "bundles").exists() or not any(bundles.iterdir())

    # Requests the review cannot answer say why, and record nothing.
    lead = str(world.people.lead)
    for target, body, reason in (
        ("gba_amnog.anhang_4c", {}, "needs a protocol with profile 'gba_amnog'"),
        ("nothing", {}, "unknown report target"),
        ("bundle", {}, "sealed with seal_bundle"),
        ("prisma", {"cutoff_seq": 10**9}, "outside the ledger"),
    ):
        refused = client.post(f"{base}/targets/{target}/render", json={"actor_id": lead, **body})
        assert refused.status_code == 400 and reason in refused.json()["detail"], refused.text
    with world.db.transaction() as tx:
        rendered_events = [
            e for e in world.ledger.events(tx, world.project_id) if e.type == "report.rendered"
        ]
    assert len(rendered_events) == 1

    # Seal at the head: the bundle includes the render just recorded, and verifies.
    sealed = client.post(f"{base}/bundle", json={"actor_id": str(world.people.lead)})
    assert sealed.status_code == 200, sealed.text
    bundle = sealed.json()
    assert bundle["key_id"] == KEY.key_id
    assert main(["verify", bundle["path"]]) == 0
    assert "OK" in capsys.readouterr().out
    db = sqlite3.connect(f"{bundle['path']}/kittum.sqlite")
    assert db.execute("select index_hash from report_artifact").fetchall() == [
        (rendered["sha256"],)
    ]
