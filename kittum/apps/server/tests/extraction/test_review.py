"""A run through the API and the worker, then human acceptance, the ledger, and the bundle."""

import json
import sqlite3
from uuid import UUID

from kittum.core import new_id
from kittum.extraction import PROPOSALS_RECORDED, RUN_REQUESTED, VALUE_ACCEPTED
from kittum.ledger import SigningKey, seal, verify_bundle
from kittum.models.testing import RecordedBackend
from tests.extraction.support import AGENT, FIXTURES, recorded, recording_path, schema

STUDY = UUID("019a3c10-0000-7000-8000-000000000001")
REPORT = UUID("019a3c10-0000-7000-8000-000000000002")


def _run(env, name, command_id=None):
    body = {
        "command_id": str(command_id or new_id()),
        "actor_id": str(env.requester),
        "layer_id": str(env.stored[name].layer.id),
        "study_id": str(STUDY),
        "report_id": str(REPORT),
        "extraction_schema": schema(name).model_dump(mode="json"),
        "agent": AGENT.model_dump(mode="json"),
        "report": {"study_label": name, "title": f"The {name} trial", "licence": "CC BY 4.0"},
    }
    return env.client.post(f"/api/projects/{env.project_id}/extraction/runs", json=body)


def _fields(env, run_id):
    response = env.client.get(f"/api/projects/{env.project_id}/extraction/runs/{run_id}/fields")
    assert response.status_code == 200
    return {
        (f["key"]["endpoint"], f["key"]["arm"], f["key"]["quantity"]): f for f in response.json()
    }


def _proposal(field, path):
    return next(p for p in field["proposals"] if p["path"] == path)


def _accept(env, field, path, viewed_ms=1500, command_id=None):
    return env.client.post(
        f"/api/projects/{env.project_id}/extraction/fields/{field['id']}/accept",
        json={
            "command_id": str(command_id or new_id()),
            "reviewer_id": str(env.reviewer),
            "path": path,
            "source_viewed_ms": viewed_ms,
        },
    )


def test_a_run_reconciles_both_paths_and_humans_accept_each_value(env, tmp_path):
    base = f"/api/projects/{env.project_id}/extraction"
    command_id = new_id()
    with env.client:
        requested = _run(env, "lesotho", command_id)
        assert requested.status_code == 202
        run = requested.json()
        assert run["status"] == "queued"
        assert (
            _run(env, "lesotho", command_id).json()["id"] == run["id"]
        )  # a retry, not a second run
        assert [e["run_id"] for e in env.events(RUN_REQUESTED)] == [run["id"]]

        # The job runs through the worker, against the completions qwen3.5:4b gave on Ollama.
        worker = env.worker(recorded("lesotho"))
        assert worker.run_once() is True
        assert worker.run_once() is False
        done = env.client.get(f"{base}/runs/{run['id']}").json()
        assert (done["status"], done["model_identity"]) == ("succeeded", "qwen3.5:4b")
        (recorded_event,) = env.events(PROPOSALS_RECORDED)
        assert recorded_event["statuses"] == {
            "janus": {"ambiguous": 2, "extracted": 3, "missing": 5},
            "llm": {"extracted": 2, "invalid": 6, "missing": 2},
        }
        assert env.artifacts.get(recorded_event["transcript_hash"])

        fields = _fields(env, run["id"])
        # The agent read 51 for the intervention arm; Table 1 prints 52 there and 51 for control.
        intervention = fields[("participants", "intervention", "n_analysed")]
        assert (
            _proposal(intervention, "janus")["value"],
            _proposal(intervention, "llm")["value"],
        ) == ("52", "51")
        assert (
            intervention["agreement"],
            intervention["gate"],
            intervention["second_extractor"],
        ) == (
            "disagree",
            "human_verify",
            True,
        )
        assert intervention["spec"]["policy"] == "critical"
        assert intervention["labels"] == {
            "endpoint": "Participants",
            "timepoint": "Baseline",
            "arm": "CHW-led care",
        }
        total = fields[("participants", "total", "n_analysed")]
        assert (total["agreement"], _proposal(total, "llm")["status"]) == ("janus_only", "invalid")
        mean = fields[("hba1c", "intervention", "mean")]
        assert (mean["agreement"], _proposal(mean, "llm")["value"]) == ("llm_only", "7.2")

        # The reviewer checks the cell and accepts the Janus value.
        accepted = _accept(env, intervention, "janus")
        assert accepted.status_code == 200, accepted.text
        acceptance = accepted.json()
        assert (acceptance["value"], acceptance["path"], acceptance["edited"]) == (
            "52",
            "janus",
            False,
        )
        assert acceptance["anchor_id"] == _proposal(intervention, "janus")["anchor_ids"][0]
        (event,) = env.events(VALUE_ACCEPTED)
        assert event["value"] == "52" and event["field_id"] == intervention["id"]
        assert (event["policy"], event["source_viewed_ms"], event["path"]) == (
            "critical",
            1500,
            "janus",
        )
        assert acceptance["event_seq"] == env.ledger.events(env.tx, env.project_id)[-1].seq
        assert _accept(env, intervention, "llm").status_code == 409  # already accepted

        # A fabricated quote and a value missing from its quote cannot be accepted as they stand.
        control = _accept(env, fields[("participants", "control", "n_analysed")], "llm")
        assert control.status_code == 409
        assert "invalid (the quote is not in the document)" in control.json()["detail"]
        deaths = _accept(env, fields[("deaths", "intervention", "events")], "llm")
        assert deaths.status_code == 409
        assert "the quote does not contain the value '51'" in deaths.json()["detail"]
        # Nor can a value whose source the reviewer did not look at.
        glance = _accept(
            env, fields[("participants", "control", "n_analysed")], "janus", viewed_ms=120
        )
        assert glance.status_code == 409 and "120 ms" in glance.json()["detail"]
        assert len(env.events(VALUE_ACCEPTED)) == 1
        first_cutoff = env.ledger.events(env.tx, env.project_id)[-1].seq

        # 7.2 is the baseline HbA1c; the reviewer selects the 12-month value in the text instead.
        text = env.stored["lesotho"].layer.text
        start = text.index("6.5% (SD 1.3) in the intervention")
        end = start + len("6.5% (SD 1.3) in the intervention")
        edit = {
            "command_id": str(new_id()),
            "reviewer_id": str(env.reviewer),
            "path": "llm",
            "source_viewed_ms": 2200,
            "value": "6.5",
            "start": start,
            "end": end,
            "quote": text[start:end],
        }
        url = f"{base}/fields/{mean['id']}/edit"
        wrong = env.client.post(url, json={**edit, "command_id": str(new_id()), "value": "6.4"})
        assert (
            wrong.status_code == 409
            and "does not contain the value '6.4'" in wrong.json()["detail"]
        )
        moved = env.client.post(url, json={**edit, "command_id": str(new_id()), "quote": "6.5%"})
        assert (
            moved.status_code == 409 and "not the layer text at the span" in moved.json()["detail"]
        )
        edited = env.client.post(url, json=edit)
        assert edited.status_code == 200, edited.text
        assert (edited.json()["value"], edited.json()["edited"], edited.json()["path"]) == (
            "6.5",
            True,
            "llm",
        )
        anchor = env.client.get(f"/api/anchors/{edited.json()['anchor_id']}").json()
        assert (anchor["start"], anchor["end"], {b["page"] for b in anchor["boxes"]}) == (
            start,
            end,
            {5},
        )

        # The accepted values are the assertion set reports use.
        values = env.client.get(f"{base}/assertions").json()
        assert sorted((a["key"]["endpoint"], a["key"]["arm"], a["value"]) for a in values) == [
            ("hba1c", "intervention", "6.5"),
            ("participants", "intervention", "52"),
        ]
        assert {a["document_hash"] for a in values} == {
            env.stored["lesotho"].document.artifact_hash
        }

    # The bundle holds the extraction tables as of its cutoff, with their anchors and artifacts.
    key = SigningKey.generate()
    early = seal(
        env.tx,
        env.project_id,
        first_cutoff,
        env.artifacts,
        tmp_path / "early",
        signer=key,
        actor_id=env.reviewer,
        ledger=env.ledger,
        clock=env.clock,
    )
    late_seq = env.ledger.events(env.tx, env.project_id)[-1].seq
    late = seal(
        env.tx,
        env.project_id,
        late_seq,
        env.artifacts,
        tmp_path / "late",
        signer=key,
        actor_id=env.reviewer,
        ledger=env.ledger,
        clock=env.clock,
    )
    for bundle, values in ((early, ["52"]), (late, ["52", "6.5"])):
        assert verify_bundle(bundle.path, trusted_key=key.verify_key).ok
        db = sqlite3.connect(bundle.path / "kittum.sqlite")
        assert sorted(v for (v,) in db.execute("select value from extraction_acceptance")) == values
        assert db.execute("select count(*) from extraction_field").fetchone() == (10,)
        anchors = {a for (a,) in db.execute("select id from extraction_anchor")}
        accepted_anchors = {a for (a,) in db.execute("select anchor_id from extraction_acceptance")}
        assert accepted_anchors <= anchors
        hashes = {
            h for (h,) in db.execute("select hash from artifact where exporter = 'extraction'")
        }
        assert (
            recorded_event["output_hash"] in hashes and recorded_event["transcript_hash"] in hashes
        )


def test_only_agreeing_validated_standard_fields_are_accepted_as_a_group(env):
    """Planted: the recorded answer, with the female-sex quotes as Table 1 prints them instead of
    the elided ones the model gave, so both paths agree on a standard field. Also planted: the
    agent reads the female count as the vitamin C arm's N, and the events from Table 3, so its
    200 events exceed its N of 146."""
    answers = json.loads(recording_path("vitamin_c").read_text())["answers"]
    final = next(k for k, v in answers.items() if json.loads(v)["tool"] == "final_result")
    answer = json.loads(answers[final])
    for value in answer["values"]:
        if value["field"] == "f12":
            value["quote"] = "Female sex, n (%) 146 (41.1)"
        if value["field"] == "f13":
            value["quote"] = "145 (40.8)"
        if value["field"] == "f1":
            value.update(quote="Female sex, n (%) 146 (41.1)", value="146")
        if value["field"] == "f4":
            value.update(quote="200 (56.3)", value="200")
    answers[final] = json.dumps(answer)
    base = f"/api/projects/{env.project_id}/extraction"
    with env.client:
        run = _run(env, "vitamin_c").json()
        assert env.worker(RecordedBackend(answers, model_identity="qwen3.5:4b")).run_once()
        fields = _fields(env, run["id"])
        female = [fields[("female_sex", arm, "events")] for arm in ("vitamin_c", "control")]
        assert [(f["spec"]["policy"], f["agreement"], f["gate"]) for f in female] == [
            ("standard", "agree", "group_accept")
        ] * 2
        rr = fields[("any_hematoma", "vitamin_c_vs_control", "risk_ratio")]
        assert (rr["spec"]["policy"], rr["agreement"], rr["gate"]) == (
            "critical",
            "agree",
            "human_verify",
        )

        def group(ids, viewed_ms=3000):
            return env.client.post(
                f"{base}/runs/{run['id']}/group-accept",
                json={
                    "command_id": str(new_id()),
                    "reviewer_id": str(env.reviewer),
                    "field_ids": [f["id"] for f in ids],
                    "list_viewed_ms": viewed_ms,
                },
            )

        # A critical field never joins a group, even when both paths agree: nothing is accepted.
        refused = group([*female, rr])
        assert refused.status_code == 409 and "(critical, human_verify)" in refused.json()["detail"]
        assert group(female, viewed_ms=100).status_code == 409
        assert env.events(VALUE_ACCEPTED) == []

        accepted = group(female)
        assert accepted.status_code == 200, accepted.text
        assert [(a["value"], a["group"], a["path"]) for a in accepted.json()] == [
            ("146", True, "janus"),
            ("145", True, "janus"),
        ]
        events = env.events(VALUE_ACCEPTED)
        assert [(e["value"], e["group"], e["source_viewed_ms"]) for e in events] == [
            ("146", True, 3000),
            ("145", True, 3000),
        ]
        # The critical risk ratio is accepted on its own, after viewing its source.
        assert _accept(env, rr, "llm").json()["value"] == "1.124"

        # Both paths read 200 events, but the agent's events exceed its own N: accepting its
        # proposal needs the reviewer to override the failed check, and the ledger records that.
        events_vc = fields[("any_hematoma", "vitamin_c", "events")]
        assert (events_vc["agreement"], _proposal(events_vc, "llm")["value"]) == ("agree", "200")
        failed = _accept(env, events_vc, "llm")
        assert failed.status_code == 409 and "events_at_most_n" in failed.json()["detail"]
        overridden = env.client.post(
            f"{base}/fields/{events_vc['id']}/accept",
            json={
                "command_id": str(new_id()),
                "reviewer_id": str(env.reviewer),
                "path": "llm",
                "source_viewed_ms": 2500,
                "override_failed_checks": True,
            },
        )
        assert overridden.status_code == 200, overridden.text
        assert "events_at_most_n" in env.events(VALUE_ACCEPTED)[-1]["failed_checks"]

        # "P < 0.001" does not print an exact p-value of 0.001.
        p_value = fields[("any_hematoma", "vitamin_c_vs_control", "p_value")]
        text = env.stored["vitamin_c"].layer.text
        printed = "P\u2009<\u20090.001"  # thin spaces, as typeset
        start = text.index(printed)
        exact = env.client.post(
            f"{base}/fields/{p_value['id']}/edit",
            json={
                "command_id": str(new_id()),
                "reviewer_id": str(env.reviewer),
                "path": "llm",
                "source_viewed_ms": 2500,
                "value": "0.001",
                "start": start,
                "end": start + len(printed),
                "quote": printed,
            },
        )
        assert exact.status_code == 409 and "does not contain the value" in exact.json()["detail"]

        view = env.client.get(f"{base}/runs/{run['id']}/review").json()
        assert view["report"] == {
            "id": str(REPORT),
            "study_id": str(STUDY),
            "study_label": "vitamin_c",
            "title": "The vitamin_c trial",
            "citation": "",
            "licence": "CC BY 4.0",
        }
        assert (
            len(view["layer"]["pages"]) == 10
            and view["layer"]["text"] == env.stored["vitamin_c"].layer.text
        )
        assert [f["accepted"] for f in view["fields"]].count(None) == len(view["fields"]) - 4
        anchors = {a["id"] for a in view["anchors"]}
        assert all(
            a in anchors for f in view["fields"] for p in f["proposals"] for a in p["anchor_ids"]
        )


def test_the_workbench_reads_a_schema_requests_a_janus_only_run_and_lists_it(env):
    """The workbench's path to a run: the schema file read by the server (a broken one is
    refused with the reason), no model server configured so the run takes the Janus path only,
    and the run list showing its report and how many of its fields are accepted."""
    base = f"/api/projects/{env.project_id}/extraction"
    with env.client:
        text = (FIXTURES / "lesotho_schema.yaml").read_text()
        broken = env.client.post(
            f"{base}/schemas/parse",
            json={"text": text.replace("schema_version: 1", "schema_version: [")},
        )
        assert broken.status_code == 422 and "line" in broken.json()["detail"][0]["msg"]
        unknown = env.client.post(f"{base}/schemas/parse", json={"text": text + "\nextra: 1\n"})
        assert unknown.status_code == 422 and "extra" in unknown.json()["detail"][0]["msg"]
        parsed = env.client.post(f"{base}/schemas/parse", json={"text": text})
        assert parsed.status_code == 200 and parsed.json() == schema("lesotho").model_dump(
            mode="json"
        )
        assert env.client.get(f"{base}/models").json() == []  # this context has no model server

        assert env.client.get(f"{base}/runs").json() == []
        body = {
            "command_id": str(new_id()),
            "actor_id": str(env.requester),
            "layer_id": str(env.stored["lesotho"].layer.id),
            "study_id": str(STUDY),
            "report_id": str(REPORT),
            "extraction_schema": parsed.json(),
            "report": {"study_label": "NCT05596773", "title": "CHW-led care in Lesotho"},
        }
        run = env.client.post(f"{base}/runs", json=body).json()
        (listed,) = env.client.get(f"{base}/runs").json()
        assert (listed["run"]["id"], listed["run"]["status"], listed["fields"]) == (
            run["id"],
            "queued",
            0,
        )
        assert listed["report"]["study_label"] == "NCT05596773"

        assert env.worker().run_once() is True
        (listed,) = env.client.get(f"{base}/runs").json()
        assert (listed["run"]["status"], listed["run"]["model_identity"]) == ("succeeded", None)
        assert (listed["fields"], listed["accepted"]) == (10, 0)
        fields = _fields(env, run["id"])
        assert {p["path"] for f in fields.values() for p in f["proposals"]} == {"janus"}
        intervention = fields[("participants", "intervention", "n_analysed")]
        assert _accept(env, intervention, "janus").json()["value"] == "52"
        (listed,) = env.client.get(f"{base}/runs").json()
        assert listed["accepted"] == 1
