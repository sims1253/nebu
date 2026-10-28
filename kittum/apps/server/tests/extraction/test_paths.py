"""Both paths over the real trial PDFs: Janus on the table cells, the agent on recorded answers."""

import json
from datetime import timedelta

from kittum.core import FixedClock, new_id
from kittum.documents import resolve
from kittum.extraction import AgentConfig, extract, run_agent
from kittum.models.testing import RecordedBackend
from tests.documents.conftest import squash, text_under
from tests.extraction.support import AGENT, RECIPE_ID, recorded, schema


def _by(proposals, path="janus"):
    return {(p.key.endpoint, p.key.arm, p.key.quantity): p for p in proposals if p.path == path}


def test_janus_anchors_each_value_on_its_table_cell(env):
    """Every Janus value is anchored on the cell it came from; PDFium reads the value under the
    anchor's box; statuses Janus could not resolve stay visible, never zero."""
    expected = {
        "vitamin_c": {
            # Table 3, primary outcomes, "Any hematoma, n (%)": 200 (56.3), 178 (50.1), RR 1.124.
            ("any_hematoma", "vitamin_c", "events"): ("200", "p6t1", 4, 1),
            ("any_hematoma", "control", "events"): ("178", "p6t1", 4, 2),
            ("any_hematoma", "vitamin_c", "percent"): ("56.3", "p6t1", 4, 1),
            ("any_hematoma", "control", "percent"): ("50.1", "p6t1", 4, 2),
            ("any_hematoma", "vitamin_c_vs_control", "risk_ratio"): ("1.124", "p6t1", 4, 3),
            ("any_hematoma", "vitamin_c_vs_control", "ci_lower"): ("0.978", "p6t1", 4, 3),
            ("any_hematoma", "vitamin_c_vs_control", "ci_upper"): ("1.290", "p6t1", 4, 3),
            ("any_hematoma", "vitamin_c_vs_control", "p_value"): ("0.098", "p6t1", 4, 4),
            # Table 1, baseline: "Female sex, n (%)".
            ("female_sex", "vitamin_c", "events"): ("146", "p5t0", 3, 1),
            ("female_sex", "control", "events"): ("145", "p5t0", 3, 2),
        },
        "lesotho": {
            # Table 1, "Individual-level, participants", primary analysis set under a merged header.
            ("participants", "intervention", "n_analysed"): ("52", "p7t0", 6, 2),
            ("participants", "control", "n_analysed"): ("51", "p7t0", 6, 1),
            ("participants", "total", "n_analysed"): ("103", "p7t0", 6, 3),
        },
    }
    for name, values in expected.items():
        stored = env.stored[name]
        s = schema(name)
        result = extract(
            stored.layer, stored.document.artifact_hash, s, s.fields(new_id(), new_id())
        )
        janus = _by(result.proposals.proposals)
        assert {k for k, p in janus.items() if p.status == "extracted"} == set(values)
        for key, (value, table, row, col) in values.items():
            p = janus[key]
            assert (p.value, p.validators and all(v.passed for v in p.validators)) == (value, True)
            (anchor_id,) = p.anchor_ids
            anchor = result.anchors[str(anchor_id)]
            assert [(c.table_id, c.row, c.col) for c in anchor.cells] == [(table, row, col)]
            assert anchor.quote.exact == value
            (box,) = anchor.boxes
            assert value in text_under(stored.pdf, box), key
            assert resolve(anchor, stored.layer).status == "exact"

    s = schema("lesotho")
    stored = env.stored["lesotho"]
    janus = _by(
        extract(stored.layer, "0" * 64, s, s.fields(new_id(), new_id())).proposals.proposals
    )
    # Table 2 prints "Deaths, n (%)" under both analysis sets: Janus says so instead of guessing.
    deaths = janus[("deaths", "intervention", "events")]
    assert deaths.status == "ambiguous" and deaths.value is None and not deaths.anchor_ids
    assert "Deaths, n (%)" in (deaths.message or "")
    # The primary endpoint is reported in the text only, so the schema gives Janus no locator.
    assert janus[("hba1c", "intervention", "mean")].status == "missing"


def test_agent_quotes_are_anchored_and_fabrications_rejected(env):
    """Recorded qwen3.5:4b answers: values whose quotes are in the PDF are anchored there; a
    quote that is not in the document, or that does not contain its value, counts for nothing."""
    stored = env.stored["vitamin_c"]
    fields = schema("vitamin_c").fields(new_id(), new_id())
    clock = FixedClock(env.clock.now())
    proposals, anchors, transcript = run_agent(
        stored.layer, fields, recorded("vitamin_c"), AGENT, clock
    )
    llm = _by(proposals, "llm")
    assert transcript.model_identity == "qwen3.5:4b" and len(transcript.calls) == 4
    assert [t["tool"] for t in transcript.tools] == ["read_page"] * 3

    rr = llm[("any_hematoma", "vitamin_c_vs_control", "risk_ratio")]
    assert (rr.status, rr.value, rr.quote) == (
        "extracted",
        "1.124",
        "(RR, 1.124; 95% CI, 0.978\u20131.290; P = 0.098)",
    )
    (anchor_id,) = rr.anchor_ids
    anchor = anchors[str(anchor_id)]
    # The model never gave offsets; the verifier found the sentence on page 5, the page it read.
    assert {b.page for b in anchor.boxes} == {5}
    assert "1.124" in squash(" ".join(text_under(stored.pdf, b) for b in anchor.boxes))
    assert squash(anchor.quote.exact).replace(" ", "") == squash(rr.quote or "").replace(" ", "")

    # The model put the percentage where the count belongs: not a count.
    events = llm[("any_hematoma", "vitamin_c", "events")]
    assert (events.status, events.value, events.anchor_ids) == ("invalid", None, ())
    assert events.message == "'56.3' is not a count"
    # "Female sex, n (%) ... Vitamin C ... 146 (41.1)": an elided quote is a fabricated one.
    female = llm[("female_sex", "vitamin_c", "events")]
    assert (female.status, female.anchor_ids) == ("invalid", ())
    assert female.message == "the quote is not in the document"
    # "N = 355" is printed twelve times: it exists but cannot anchor on its own.
    n = llm[("randomization", "vitamin_c", "n_randomized")]
    assert (n.status, n.value, len(n.anchor_ids)) == ("ambiguous", "355", 5)

    stored = env.stored["lesotho"]
    fields = schema("lesotho").fields(new_id(), new_id())
    proposals, _, _ = run_agent(stored.layer, fields, recorded("lesotho"), AGENT, clock)
    llm = _by(proposals, "llm")
    # "Individual-level, participants n = 132" joins cells that are not adjacent in Table 1.
    control = llm[("participants", "control", "n_analysed")]
    assert (control.status, control.value, control.quote) == (
        "invalid",
        None,
        "Individual-level, participants n = 132",
    )
    assert control.message == "the quote is not in the document"
    # A real passage that does not print the value given for it.
    deaths = llm[("deaths", "intervention", "events")]
    assert (deaths.status, deaths.value, deaths.anchor_ids) == ("invalid", None, ())
    assert deaths.message == "the quote does not contain the value '51'"
    assert deaths.quote and deaths.quote.startswith("Missing data: HbA1c: 18")


def test_agent_stops_at_its_call_and_time_budgets(env):
    """A model that never answers usably uses up its calls; a slow one runs out of time. Either
    way every field is an error, and the last allowed call may only answer."""
    stored = env.stored["vitamin_c"]
    fields = schema("vitamin_c").fields(new_id(), new_id())
    garbage = RecordedBackend(default="I think the answer is in Table 3.")
    config = AgentConfig(model_id=RECIPE_ID, max_calls=2)
    proposals, anchors, transcript = run_agent(stored.layer, fields, garbage, config, env.clock)
    assert len(garbage.requests) == 2 and not anchors
    assert {(p.status, p.message) for p in proposals} == {("error", "used all 2 model calls")}
    last = garbage.requests[-1]
    assert last.json_schema is not None
    assert last.json_schema["properties"]["tool"]["enum"] == ["final_result"]
    assert last.messages[-1].content.startswith("This is your last call")
    assert "Invalid JSON" in last.messages[-2].content  # PydanticAI's retry feedback

    clock = FixedClock(env.clock.now())

    class Slow:
        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            clock.advance(timedelta(minutes=20))
            tool = {"tool": "search", "query": "hematoma", "page": 0, "table": "", "values": []}
            return RecordedBackend(default=json.dumps(tool)).complete(request)

    config = AgentConfig(model_id=RECIPE_ID, max_calls=10, max_seconds=3000)
    slow = Slow()
    proposals, _, transcript = run_agent(stored.layer, fields, slow, config, clock)
    assert len(slow.requests) == 3  # 60 minutes used of 50
    assert transcript.stopped == "used the time budget of 3000 s"
    assert {p.status for p in proposals} == {"error"}
    # The searches ran on the layer; their results reached the model fenced as data.
    result = slow.requests[1].messages[-1].content
    assert result.startswith("Result of search:\n<document>\n[page 1] ")
    assert result.endswith("\n</document>") and "perirenal hematoma" in result
