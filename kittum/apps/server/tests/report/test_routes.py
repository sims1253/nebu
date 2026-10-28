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
    capsys: pytest.CaptureFixture[str],
) -> None:
    world, seqs = nrt
    base = f"/api/projects/{world.project_id}/reports"

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
        download = client.get(f"{base}/artifacts/{f['sha256']}")
        assert download.status_code == 200
        assert sha256_hex(download.content) == f["sha256"]
        assert download.headers["content-type"].startswith(f["media_type"].split(";")[0])
    index = client.get(f"{base}/artifacts/{rendered['sha256']}").json()
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
    assert client.get(f"{base}/artifacts/{raw}").status_code == 404
    other = f"/api/projects/{capivasertib.project_id}/reports/artifacts/{rendered['sha256']}"
    assert client.get(other).status_code == 404
    assert (
        client.post(
            f"{base}/targets/gba_amnog.anhang_4c/render", json={"actor_id": str(world.people.lead)}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"{base}/targets/nothing/render", json={"actor_id": str(world.people.lead)}
        ).status_code
        == 400
    )

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
