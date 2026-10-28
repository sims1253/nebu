"""Seal a handover bundle, then check it offline with `kittum verify`, as a client would."""

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from kittum.cli import main
from kittum.core import Id, new_id, sha256_hex
from kittum.ledger import BundleWriter, SigningKey, seal
from kittum.ledger.bundle import MANIFEST_DOMAIN
from kittum.ledger.keys import document_message
from kittum.storage import FilesystemStore
from tests.ledger.harness import memory_harness

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Tx
    from tests.ledger.harness import Harness

KEY = SigningKey.generate()
TRIAL_PDF = b"%PDF-1.7\n% KEYNOTE-189 primary publication\n"
CSR_PDF = b"%PDF-1.7\n% clinical study report, confidential\n"
REPORT = b"# Screening\n\n1 record included.\n"


@dataclass
class ScreeningExporter:
    """Stands in for a module that adds its own rows and artifacts to every bundle."""

    pdf: str
    csr: str
    name: str = "screening"
    seen: list[tuple[Id, int]] = field(default_factory=list)

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:  # noqa: ARG002
        self.seen.append((project_id, cutoff_seq))
        bundle.table("screening_decision", ["id", "record", "outcome", "decided_at", "evidence"])
        bundle.insert(
            "screening_decision",
            [
                {
                    "id": new_id(),
                    "record": "PMID:29658856",
                    "outcome": "include",
                    "decided_at": datetime(2026, 10, 28, 10, 15, tzinfo=UTC),
                    "evidence": {"page": 3, "quote": "overall survival"},
                }
            ],
        )
        bundle.artifact(self.pdf, media_type="application/pdf")
        bundle.artifact(
            self.csr, media_type="application/pdf", withheld_reason="CSR under confidentiality"
        )
        bundle.file("reports/screening.md", REPORT)
        for bad in ("../outside.md", "/abs.md", "reports//x.md", "artifacts/x", "manifest.json"):
            with pytest.raises(ValueError):
                bundle.file(bad, REPORT)


def _seal(store: Harness, tmp_path: Path) -> tuple[Path, Id, ScreeningExporter]:
    project, actor = store.new_project()
    store.append(project, actor, 4)
    artifacts = FilesystemStore(tmp_path / "store")
    exporter = ScreeningExporter(pdf=artifacts.put(TRIAL_PDF), csr=artifacts.put(CSR_PDF))
    store.append(project, actor, 1, kind="test.after_cutoff")

    out = tmp_path / "bundle"
    bundle = seal(
        store.tx,
        project,
        4,
        artifacts,
        out,
        signer=KEY,
        actor_id=actor,
        ledger=store.ledger,
        exporters=[exporter],
        clock=store.clock,
    )
    sealed = store.ledger.events(store.tx, project, after_seq=5)
    assert [(e.type, e.payload["manifest_hash"]) for e in sealed] == [
        ("ledger.bundle_sealed", bundle.manifest_hash)
    ]
    assert exporter.seen == [(project, 4)]
    return out, project, exporter


def _public_key(tmp_path: Path, key: SigningKey = KEY) -> Path:
    path = tmp_path / f"{key.key_id[:8]}.pub.pem"
    path.write_bytes(key.verify_key.to_pem())
    return path


def test_sealed_bundle_verifies_offline(
    store: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, project, exporter = _seal(store, tmp_path)

    assert main(["verify", str(out), "--public-key", str(_public_key(tmp_path))]) == 0
    printed = capsys.readouterr().out
    assert f"key {KEY.key_id} (trusted)" in printed
    assert "OK" in printed and "through seq 4" in printed

    # The bundle holds the chain to the cutoff, the module's rows, and the permitted artifacts.
    db = sqlite3.connect(out / "kittum.sqlite")
    assert db.execute("select max(seq) from audit_event").fetchone() == (4,)
    assert db.execute("select record, evidence from screening_decision").fetchall() == [
        ("PMID:29658856", '{"page":3,"quote":"overall survival"}')
    ]
    assert sorted(db.execute("select hash, included, withheld_reason from artifact")) == sorted(
        [(exporter.pdf, 1, None), (exporter.csr, 0, "CSR under confidentiality")]
    )
    db.close()
    assert sorted(p.name for p in (out / "artifacts").iterdir()) == [exporter.pdf]
    manifest = json.loads((out / "manifest.json").read_text())
    assert (manifest["project_id"], manifest["checkpoint"]["seq"]) == (str(project), 4)
    assert (out / "reports" / "screening.md").read_bytes() == REPORT
    assert manifest["files"]["reports/screening.md"] == sha256_hex(REPORT)

    # An exporter's file is checked like every other: a changed report fails.
    (out / "reports" / "screening.md").write_bytes(REPORT.replace(b"1 record", b"2 records"))
    assert main(["verify", str(out)]) == 1
    assert "reports/screening.md does not match its hash" in capsys.readouterr().out
    (out / "reports" / "screening.md").write_bytes(REPORT)

    # Without the sender's key the bundle still checks out, but the tool says so.
    assert main(["verify", str(out)]) == 0
    assert "NOT PINNED" in capsys.readouterr().out


def _rewrite_manifest(bundle: Path, change: Any) -> None:
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest, indent=2))


def _rehash(bundle: Path, rel: str) -> None:
    def update(m: dict[str, Any]) -> None:
        m["files"][rel] = sha256_hex((bundle / rel).read_bytes())

    _rewrite_manifest(bundle, update)


def _edit_ledger(bundle: Path, sql: str) -> None:
    db = sqlite3.connect(bundle / "kittum.sqlite")
    db.execute("drop trigger audit_event_no_update")
    db.execute("drop trigger audit_event_no_delete")
    db.execute(sql)
    db.commit()
    db.close()
    _rehash(bundle, "kittum.sqlite")


def _change_artifact(bundle: Path, exporter: ScreeningExporter) -> None:
    path = bundle / "artifacts" / exporter.pdf
    path.write_bytes(path.read_bytes().replace(b"KEYNOTE-189", b"KEYNOTE-407"))


def _change_payload(bundle: Path, _: ScreeningExporter) -> None:
    _edit_ledger(
        bundle,
        "update audit_event set payload = replace(payload, '\"n\":119', '\"n\":120') where seq = 2",
    )


def _cut_tail(bundle: Path, _: ScreeningExporter) -> None:
    _edit_ledger(bundle, "delete from audit_event where seq = 4")


def _resign(bundle: Path, _: ScreeningExporter) -> None:
    forger = SigningKey.generate()

    def update(m: dict[str, Any]) -> None:
        m["public_key"] = forger.verify_key.raw.hex()
        m.pop("signature")
        m["signature"] = forger.sign(document_message(m, domain=MANIFEST_DOMAIN))

    _rewrite_manifest(bundle, update)


def _smuggle(bundle: Path, _: ScreeningExporter) -> None:
    (bundle / "artifacts" / "notes.txt").write_text("not part of the handover")


def _link_directory(bundle: Path, _: ScreeningExporter) -> None:
    outside = bundle.parent / "outside"
    outside.mkdir()
    (outside / "evidence.pdf").write_bytes(b"%PDF-1.7\n% unlisted evidence\n")
    (bundle / "extra").symlink_to(outside, target_is_directory=True)


def _dangling_link(bundle: Path, _: ScreeningExporter) -> None:
    (bundle / "artifacts" / "gone").symlink_to(bundle.parent / "does-not-exist")


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        pytest.param(_change_artifact, "does not match its hash", id="changed artifact"),
        pytest.param(
            _change_payload, "the manifest signature does not verify", id="changed sqlite file"
        ),
        pytest.param(
            _change_payload,
            "ledger seq 2: the payload does not match its hash",
            id="changed payload",
        ),
        pytest.param(_cut_tail, "ledger seq 4: the chain ends at seq 3", id="cut tail"),
        pytest.param(_resign, "not the trusted key", id="re-signed by another key"),
        pytest.param(
            _smuggle, "artifacts/notes.txt is not listed in the manifest", id="smuggled file"
        ),
        pytest.param(_link_directory, "extra is a symlink", id="directory symlink"),
        pytest.param(_dangling_link, "artifacts/gone is a symlink", id="dangling symlink"),
    ],
)
def test_tampered_bundle_fails_offline_verification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tamper: Any, expected: str
) -> None:
    store = next(memory_harness())
    out, _, exporter = _seal(store, tmp_path)
    public_key = _public_key(tmp_path)
    assert main(["verify", str(out), "--public-key", str(public_key)]) == 0
    capsys.readouterr()

    copy = tmp_path / "tampered"
    shutil.copytree(out, copy)
    tamper(copy, exporter)

    assert main(["verify", str(copy), "--public-key", str(public_key)]) == 1
    printed = capsys.readouterr().out
    assert expected in printed
    assert "FAILED" in printed
