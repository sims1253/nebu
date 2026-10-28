"""Both paths over the real trial PDFs: Janus on the table cells, the agent on recorded answers."""

import json
from datetime import timedelta

import pymupdf
import pytest

from kittum.core import FixedClock, new_id
from kittum.documents import derive, ingest, resolve
from kittum.extraction import AgentConfig, SchemaError, extract, load_schema, run_agent
from kittum.models.testing import RecordedBackend
from tests.documents.conftest import squash, text_under
from tests.extraction.conftest import _PARSER
from tests.extraction.support import AGENT, RECIPE_ID, recorded, recording_path, schema


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

    # An answer that arrives after the deadline is discarded, even a final one.
    answers = json.loads(recording_path("vitamin_c").read_text())["answers"]
    final = next(v for v in answers.values() if json.loads(v)["tool"] == "final_result")

    class Late(Slow):
        def complete(self, request):
            self.requests.append(request)
            clock.advance(timedelta(minutes=20))
            return RecordedBackend(default=final).complete(request)

    late = Late()
    config = AgentConfig(model_id=RECIPE_ID, max_calls=10, max_seconds=600)
    proposals, anchors, transcript = run_agent(stored.layer, fields, late, config, clock)
    assert len(late.requests) == 1 and not anchors
    assert {(p.status, p.message) for p in proposals} == {
        ("error", "used the time budget of 600 s")
    }


_TEXT_LABEL = """
schema_version: 1
name: Text labels
analysis_set: itt
arms:
  - {id: intervention, label: Intervention}
endpoints:
  - id: hba1c
    label: HbA1c
    timepoints: [{id: m12, label: 12 months}]
    locate: {strategy: text_label, rows: [Mean HbA1c]}
    quantities:
      - quantity: mean
        arms: [intervention]
"""


def test_a_text_label_value_on_its_own_line_is_anchored_on_that_line(env):
    """Planted: a two-column page where "Mean HbA1c:" ends a left-column line and its value
    "6.7" is a separate line on the same baseline. The anchor must print the value, not the label.
    A schema that names an arm twice is refused up front, not when its fields are stored."""
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    left = ["Follow-up lasted", "twelve months.", "Mean HbA1c:"]
    right = ["values improved in both", "arms of the trial.", "6.7"]
    for i, (a, b) in enumerate(zip(left, right, strict=True)):
        page.insert_text((60, 100 + i * 14), a, fontsize=10)
        page.insert_text((200, 100 + i * 14), b, fontsize=10)
    document = ingest(env.tx, env.artifacts, pdf.tobytes())
    layer = derive(env.tx, document, _PARSER, artifacts=env.artifacts)
    assert "Mean HbA1c:\n6.7" in layer.text  # two lines, as the bug needs
    schema = load_schema(_TEXT_LABEL)
    result = extract(layer, document.artifact_hash, schema, schema.fields(new_id(), new_id()))
    (proposal,) = result.proposals.proposals
    assert (proposal.status, proposal.value) == ("extracted", "6.7")
    (anchor_id,) = proposal.anchor_ids
    assert result.anchors[str(anchor_id)].quote.exact == "6.7"

    with pytest.raises(SchemaError, match="mean names intervention twice"):
        load_schema(
            _TEXT_LABEL.replace("arms: [intervention]", "arms: [intervention, intervention]")
        )


_PLANTED_TABLE = """
schema_version: 1
name: Planted outcomes table
analysis_set: itt
arms:
  - {id: vitamin_c, label: Vitamin C, columns: [Vitamin C group]}
  - {id: control, label: Control, columns: [Control group]}
comparisons:
  - id: vitamin_c_vs_control
    label: Vitamin C versus control
    intervention: vitamin_c
    comparator: control
    columns: [Effect size (95% CI)]
endpoints:
  - id: transfusion
    label: Transfusion
    timepoints: [{id: h24, label: 24 h}]
    locate: {rows: ["Transfusion required, n (%)"]}
    quantities:
      - quantity: events
        arms: [vitamin_c, control]
        locate: {cell: count_percentage.count}
      - quantity: percent
        arms: [vitamin_c]
        locate: {cell: count_percentage.percentage}
      - quantity: risk_difference
        unit: percentage points
        comparisons: [vitamin_c_vs_control]
        locate: {cell: estimate_interval.estimate}
  - id: egfr
    label: Change in eGFR
    unit: mL/min
    timepoints: [{id: h24, label: 24 h}]
    locate: {rows: ["Change in eGFR, mL/min"]}
    quantities:
      - quantity: mean_difference
        comparisons: [vitamin_c_vs_control]
        locate: {cell: estimate_interval.estimate}
      - quantity: ci_lower
        comparisons: [vitamin_c_vs_control]
        locate: {cell: estimate_interval.lower}
"""


def test_janus_reads_typeset_minus_signs_bare_zero_counts_and_not_reported_cells(env):
    """Planted: a ruled table as journals set it. A zero count without its percentage, "NR" for
    an effect that was not estimated, and a difference printed with U+2212 minus signs."""
    minus = "\N{MINUS SIGN}"
    rows = [
        ["Outcome", "Vitamin C group", "Control group", "Effect size (95% CI)"],
        ["Transfusion required, n (%)", "0", "2 (0.6)", "NR"],
        ["Change in eGFR, mL/min", "1.2 (0.4)", "1.3 (0.5)", f"{minus}0.040 ({minus}0.187, 0.107)"],
    ]
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    xs = [40, 190, 290, 390, 560]
    writer = pymupdf.TextWriter(page.rect)
    for r, row in enumerate(rows):
        y = 100 + r * 20
        for c, text in enumerate(row):
            writer.append((xs[c] + 3, y + 14), text, font=pymupdf.Font("helv"), fontsize=8)
    writer.write_text(page)
    for r in range(len(rows) + 1):
        page.draw_line((xs[0], 100 + r * 20), (xs[-1], 100 + r * 20))
    for x in xs:
        page.draw_line((x, 100), (x, 100 + len(rows) * 20))
    document = ingest(env.tx, env.artifacts, pdf.tobytes())
    layer = derive(env.tx, document, _PARSER, artifacts=env.artifacts)
    schema = load_schema(_PLANTED_TABLE)
    result = extract(layer, document.artifact_hash, schema, schema.fields(new_id(), new_id()))
    janus = {(p.key.endpoint, p.key.arm, p.key.quantity): p for p in result.proposals.proposals}
    summary = {key: (p.status, p.value) for key, p in janus.items() if p.path == "janus"}
    assert summary == {
        ("transfusion", "vitamin_c", "events"): ("extracted", "0"),
        ("transfusion", "control", "events"): ("extracted", "2"),
        # The percentage is not printed: never invented.
        ("transfusion", "vitamin_c", "percent"): ("invalid", None),
        ("transfusion", "vitamin_c_vs_control", "risk_difference"): ("missing", None),
        ("egfr", "vitamin_c_vs_control", "mean_difference"): ("extracted", "-0.040"),
        ("egfr", "vitamin_c_vs_control", "ci_lower"): ("extracted", "-0.187"),
    }
    not_reported = janus[("transfusion", "vitamin_c_vs_control", "risk_difference")]
    assert (not_reported.message, not_reported.quote) == ("the cell prints 'NR'", "NR")
    for key, printed in (
        (("transfusion", "vitamin_c", "events"), "0"),
        (("egfr", "vitamin_c_vs_control", "mean_difference"), f"{minus}0.040"),
        (("egfr", "vitamin_c_vs_control", "ci_lower"), f"{minus}0.187"),
    ):
        (anchor_id,) = janus[key].anchor_ids
        assert result.anchors[str(anchor_id)].quote.exact == printed, key
