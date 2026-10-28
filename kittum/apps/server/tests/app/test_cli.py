"""A review set up from the command line, step by step, as the README tells a lead to do it."""

import json
from typing import TYPE_CHECKING, Any

import pytest

from kittum.app.demo import DATA, PROTOCOL
from kittum.cli import main

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Database

pytestmark = pytest.mark.postgres

SENTINELS = PROTOCOL.parents[1] / "sentinels" / "nicotine-replacement.yaml"


def test_a_lead_sets_up_screening_from_the_command_line(
    database: Database,  # noqa: ARG001 - skips without Postgres
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KITTUM_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("KITTUM_JUDGE_BACKEND", "demo")

    def kittum(*args: str, code: int = 0) -> Any:
        assert main(list(args)) == code
        out = capsys.readouterr()
        return json.loads(out.out) if code == 0 and out.out.startswith(("{", "[")) else out

    created = kittum("project", "create", "NRT from the shell", "--lead", "Morgan Lee")
    pid, lead = created["project_id"], created["lead"]
    for name, role in (
        ("Sam Okafor", "reviewer"),
        ("Jun Park", "reviewer"),
        ("Rita Nunes", "auditor"),
        ("Elena Weiss", "adjudicator"),
    ):
        kittum("project", "add-member", pid, name, "--role", role, "--by", lead)
    kittum("project", "publish-protocol", pid, str(PROTOCOL), "--by", lead)
    for source, file in (
        ("central", "central_export.ris"),
        ("citation_search", "citation_search.ris"),
    ):
        kittum(
            "project", "import-search", pid, str(DATA / file), "--by", lead,
            "--topic", "TRIALS", "--source", source, "--platform", source, "--strategy", "x",
        )  # fmt: skip
    assert kittum("project", "ingest", pid, "--by", lead)["occurrences"] == 10

    # The example protocol seeds 1% sentinels: without any, the stage is refused, with a reason.
    stage = ("project", "open-stage", pid, "nrt-vs-control", "title_abstract", "--by", lead)
    refused = kittum(*stage, code=1)
    assert "sentinel" in refused.err and "Traceback" not in refused.err
    opened = kittum(*stage, "--sentinels", str(SENTINELS))
    assert opened["mode"] == "M1" and len(opened["screeners"]) == 2

    verified = kittum("ledger", "verify", pid)
    assert verified.out.startswith(f"OK project {pid}")
