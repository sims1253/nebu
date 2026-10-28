"""The accepted extraction values leave Kittum as data: CSV and JSON, one row per study and field.

The Lesotho trial is extracted through the API and the worker, its LLM path served over HTTP as a
live model server would. A reviewer accepts the Janus value of the intervention arm's analysed
participants and edits the 12-month HbA1c from a span on page 5. The lead renders the
`extraction` target and downloads both files: each row names the study, the report, the field,
the value and unit, the path that produced it, the page and quote it came from, and who accepted
it when. The sealed bundle carries the same files and every LLM call's request and response,
and `kittum verify` checks the files against the ledger's acceptances: a bundle whose values
file says something the ledger does not fails, even with every hash and the signature intact.
"""

import csv
import io
import json
from typing import TYPE_CHECKING

import pytest
import yaml

from kittum.cli import main
from kittum.ledger import SigningKey
from kittum.models import ModelRegistry
from kittum.protocol import compile, publish, save_draft
from kittum.report import CORE_RENDERERS, RenderedFile, seal_bundle
from tests.extraction.support import RECIPE_ID, recorded
from tests.extraction.test_review import _accept, _fields, _run
from tests.report.support import EXAMPLES, OverHttp

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from kittum.report import Review
    from tests.extraction.conftest import Env

pytestmark = pytest.mark.postgres

VALUES = "reports/extraction/extracted_values"


def test_accepted_values_download_as_csv_and_json_and_verify_against_the_ledger(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = yaml.safe_load((EXAMPLES / "nicotine-replacement.yaml").read_text())
    draft = save_draft(env.tx, env.project_id, compile(source))
    publish(env.tx, env.project_id, draft.id, env.requester, ledger=env.ledger)
    recipe = ModelRegistry.load().get(RECIPE_ID)
    server = OverHttp(recorded("lesotho"), recipe, env.artifacts, reasoning="Table 1 first.")
    base = f"/api/projects/{env.project_id}"
    lead = {"X-Kittum-Reviewer": str(env.requester)}

    with env.client:
        run = _run(env, "lesotho")
        worker = env.worker(server)
        assert worker.run_once() is True
        fields = _fields(env, run["id"])
        intervention = fields[("participants", "intervention", "n_analysed")]
        assert _accept(env, intervention, "janus").status_code == 200
        mean = fields[("hba1c", "intervention", "mean")]
        text = env.stored["lesotho"].layer.text
        quote = "6.5% (SD 1.3) in the intervention"
        start = text.index(quote)
        edited = env.client.post(
            f"{base}/extraction/fields/{mean['id']}/edit",
            json={
                "command_id": "019a3c10-0000-7000-8000-0000000000e1",
                "reviewer_id": str(env.reviewer),
                "path": "llm",
                "source_viewed_ms": 2200,
                "value": "6.5",
                "start": start,
                "end": start + len(quote),
                "quote": quote,
            },
        )
        assert edited.status_code == 200, edited.text

        # The lead renders the values and downloads both files.
        rendered = env.client.post(
            f"{base}/reports/targets/extraction/render", json={"actor_id": str(env.requester)}
        )
        assert rendered.status_code == 200, rendered.text
        files = {
            f["path"]: env.client.get(
                f"{base}/reports/artifacts/{f['sha256']}", headers=lead
            ).content
            for f in rendered.json()["artifact"]["files"]
        }

        # The LLM path's calls open from the ledger like the judge's, request and response.
        page = env.client.get(
            f"{base}/audit/events",
            params={"type": "extraction.proposals_recorded"},
            headers=lead,
        ).json()
        (proposals,) = page["events"]
        calls = proposals["model_calls"]
        assert len(calls) == len(server.seen) > 0
        assert all(c["request"]["form"] == "http_body" and c["request"]["stored"] for c in calls)
        first = env.client.get(
            f"{base}/audit/artifacts/{calls[0]['request']['sha256']}", headers=lead
        )
        assert first.content == server.seen[0].content
        assert json.loads(first.content)["reasoning_effort"] == "none"

    rows = list(csv.DictReader(io.StringIO(files["extracted_values.csv"].decode())))
    data = json.loads(files["extracted_values.json"])
    assert [v["acceptance_id"] for v in data["values"]] == [r["acceptance_id"] for r in rows]
    by_quantity = {(r["endpoint"], r["quantity"]): r for r in rows}
    janus = by_quantity[("Participants", "n_analysed")]
    assert (janus["study"], janus["report"], janus["arm"]) == (
        "lesotho",
        "The lesotho trial",
        "CHW-led care",
    )
    assert (janus["value"], janus["produced_by"], janus["accepted_by"]) == (
        "52",
        "janus",
        "Reviewer",
    )
    assert janus["unit"] == "participants" and janus["pages"] and janus["quote"]
    assert janus["accepted_at"].startswith("2026-10-29T09:00:00")
    hba1c = next(r for r in rows if r["quantity"] == "mean")
    assert (hba1c["value"], hba1c["produced_by"], hba1c["pages"], hba1c["quote"]) == (
        "6.5",
        "manual_edit",
        "5",
        quote,
    )
    assert data["values"][rows.index(hba1c)]["proposal_path"] == "llm"

    # The sealed bundle carries the same files, and `kittum verify` checks them.
    key = SigningKey.generate()
    sealed = seal_bundle(
        env.tx,
        env.project_id,
        env.artifacts,
        tmp_path / "bundle",
        signer=key,
        actor_id=env.requester,
        ledger=env.ledger,
        clock=env.clock,
    )
    assert (sealed.path / f"{VALUES}.csv").read_bytes() == files["extracted_values.csv"]
    bundled = json.loads((sealed.path / f"{VALUES}.json").read_text())
    assert bundled["values"] == data["values"]
    assert bundled["cutoff_seq"] == sealed.cutoff_seq > data["cutoff_seq"]
    for c in calls:
        for part in (c["request"], c["response"]):
            assert (sealed.path / "artifacts" / part["sha256"]).is_file()
    assert main(["verify", str(sealed.path)]) == 0
    capsys.readouterr()

    # Every forged file below is sealed into a bundle whose hashes and signature all check out;
    # `kittum verify` must still catch that the file disagrees with the ledger.
    honest = CORE_RENDERERS["extraction"]
    forgeries = 0

    def verify_forged(forge: Callable[[str, bytes], bytes]) -> str:
        nonlocal forgeries
        forgeries += 1

        def forged(review: Review, scope_id: str | None) -> list[RenderedFile]:
            return [
                f.model_copy(update={"data": forge(f.path, f.data)})
                for f in honest(review, scope_id)
            ]

        monkeypatch.setitem(CORE_RENDERERS, "extraction", forged)
        bundle = seal_bundle(
            env.tx,
            env.project_id,
            env.artifacts,
            tmp_path / f"forged-{forgeries}",
            signer=key,
            actor_id=env.requester,
            ledger=env.ledger,
            clock=env.clock,
        )
        assert main(["verify", str(bundle.path)]) == 1
        return capsys.readouterr().out

    # A value that says 53 where the ledger says 52.
    out = verify_forged(lambda _, data: data.replace(b'"value": "52"', b'"value": "53"'))
    assert f"FAIL {VALUES}.json: " in out
    assert "has value '53', ledger event" in out and "has '52'" in out
    assert f"FAIL {VALUES}.csv: " in out

    # An empty file that claims to show the ledger before anything was accepted.
    out = verify_forged(
        lambda path, data: (
            b'{"cutoff_seq": 0, "values": []}\n'
            if path.endswith(".json")
            else data.split(b"\n")[0] + b"\n"
        )
    )
    assert "says it shows the ledger at event 0, but the bundle's ledger ends at event" in out
    assert out.count("leaves out the value") == 2

    # The right value, unit, anchor, and reviewer, claimed for another study and quantity, the
    # same in both files.
    def other_study(_: str, data: bytes) -> bytes:
        return data.replace(b"lesotho", b"vitamin_c").replace(b"n_analysed", b"n_randomized")

    out = verify_forged(other_study)
    assert "has study 'vitamin_c', ledger event" in out
    assert "has quantity 'n_randomized', ledger event" in out
    assert f"{VALUES}.csv: " not in out  # the CSV says what the JSON says

    # A CSV that differs from the JSON in a column other than value and unit.
    out = verify_forged(
        lambda path, data: (
            data.replace(b",Reviewer,", b",Somebody,") if path.endswith(".csv") else data
        )
    )
    assert f"FAIL {VALUES}.csv: accepted_by of " in out
    assert f"FAIL {VALUES}.json" not in out
