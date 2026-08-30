from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import CompilationError, DocumentEvidence
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource

RUNNING_HEADER = "Inspection notes - March"


def article_document() -> bytes:
    """Three pages. Page 0 is cover boilerplate; pages 1 and 2 carry the
    article under a running head and page-number folios. The overview section
    starts on page 1 and only finishes on page 2, so its span must cross the
    page boundary instead of truncating at it."""
    document = pymupdf.open()
    cover = document.new_page(width=612, height=792)
    cover.insert_text((48, 96), "Inspection report", fontsize=16)
    for index, line in enumerate(
        [
            "Prepared by the workshop team.",
            "This cover page carries distribution notes only.",
            "Not part of the article body.",
        ]
    ):
        cover.insert_text((48, 160 + index * 20), line, fontsize=10)
    first = document.new_page(width=612, height=792)
    first.insert_text((48, 40), RUNNING_HEADER, fontsize=9)
    first.insert_text((560, 740), "2", fontsize=9)
    first.insert_text((48, 130), "1. Overview", fontsize=12)
    for index, line in enumerate(
        [
            "The generator was inspected after the seasonal shutdown.",
            "Load tests ran twice and the second run repeated the first.",
            "The overview continues on the next page.",
        ]
    ):
        first.insert_text((48, 160 + index * 20), line, fontsize=10)
    second = document.new_page(width=612, height=792)
    second.insert_text((48, 40), RUNNING_HEADER, fontsize=9)
    second.insert_text((560, 740), "3", fontsize=9)
    second.insert_text((48, 130), "This sentence finishes the overview section.", fontsize=10)
    second.insert_text((48, 190), "2. Findings", fontsize=12)
    for index, line in enumerate(
        [
            "Wear on the primary bearing stays within tolerance.",
            "The cooling loop holds pressure across the whole run.",
        ]
    ):
        second.insert_text((48, 220 + index * 20), line, fontsize=10)
    second.insert_text((48, 300), "References", fontsize=12)
    second.insert_text((48, 330), "Anders, R. Field guide to workshop tools.", fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def span_field(
    key: str,
    labels: list[str],
    *,
    end_labels: list[str] | None = None,
    section_labels: list[str] | None = None,
    exclude_tables: bool | None = None,
) -> dict[str, Any]:
    locate: dict[str, Any] = {"strategy": "text_span", "labels": labels}
    if end_labels is not None:
        locate["end_labels"] = end_labels
    if section_labels is not None:
        locate["section_labels"] = section_labels
    if exclude_tables is not None:
        locate["exclude_tables"] = exclude_tables
    return {
        "key": key,
        "label": key,
        "reference": {"pointer": f"/fields/{key}", "required": False},
        "value": {"type": "text"},
        "locate": locate,
        "compare": {"operator": "exact", "normalizers": ["trim"]},
    }


def specification(fields: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Article span check",
            "sections": [{"key": "body", "label": "Body", "fields": fields}],
        }
    ).encode()


def read(document: bytes, document_id: str = "article") -> DocumentEvidence:
    with tempfile.TemporaryDirectory() as directory:
        document_path = Path(directory) / "article.pdf"
        document_path.write_bytes(document)
        return TextLayerDocumentReader().read(document_path, document_id)


def extract(document: bytes, fields: list[dict[str, Any]]) -> dict[str, Any]:
    plan = SpecificationCompiler().compile(
        specification(fields), ReferenceSource(b'{"fields": {}}', "reference.json")
    )
    extraction = SchemaExtractor().extract(plan, read(document))
    by_id = {field.id: field.key for field in plan.fields}
    return {by_id[item.field_id]: item for item in extraction.fields}


def test_span_crosses_a_page_boundary_without_truncation() -> None:
    extracted = extract(article_document(), [span_field("overview", ["1. Overview"])])
    overview = extracted["overview"]
    assert overview.status.value == "extracted", overview.explanation
    # Both the last line of page 1 and the first line of page 2 are present:
    # the page boundary is a space, never a cut.
    assert overview.raw_document_value == (
        "The generator was inspected after the seasonal shutdown. "
        "Load tests ran twice and the second run repeated the first. "
        "The overview continues on the next page. "
        "This sentence finishes the overview section."
    )


def test_running_heads_and_folios_stay_out_of_the_span() -> None:
    extracted = extract(article_document(), [span_field("overview", ["1. Overview"])])
    text = extracted["overview"].raw_document_value or ""
    assert RUNNING_HEADER not in text
    # The folios print "2" and "3"; no lone page number rides along.
    assert not [word for word in text.split() if word.isdigit()]


def test_cover_boilerplate_contributes_nothing() -> None:
    fields = [
        span_field("overview", ["1. Overview"]),
        span_field("findings", ["2. Findings"], end_labels=["References"]),
    ]
    for item in extract(article_document(), fields).values():
        assert item.status.value == "extracted", item.explanation
        assert "distribution notes" not in (item.raw_document_value or "")
        assert "Not part of the article body" not in (item.raw_document_value or "")
        assert "Prepared by the workshop team" not in (item.raw_document_value or "")


def test_end_labels_stop_the_span_before_references() -> None:
    extracted = extract(
        article_document(), [span_field("findings", ["2. Findings"], end_labels=["References"])]
    )
    findings = extracted["findings"]
    assert findings.status.value == "extracted", findings.explanation
    assert findings.raw_document_value == (
        "Wear on the primary bearing stays within tolerance. "
        "The cooling loop holds pressure across the whole run."
    )
    assert "Anders" not in findings.raw_document_value
    assert "Span ended before 'References'." in findings.explanation


def test_span_evidence_names_first_and_last_line() -> None:
    extracted = extract(
        article_document(),
        [
            span_field("overview", ["1. Overview"]),
            span_field("findings", ["2. Findings"], end_labels=["References"]),
        ],
    )
    overview, findings = extracted["overview"], extracted["findings"]
    # A span covering two pages names where it ends; a single-page span also
    # carries the bounding region over its lines.
    assert overview.location is not None
    assert (overview.location.page, overview.location.end_page) == (1, 2)
    assert overview.location.cell_bbox is None
    assert overview.row_context == "1. Overview"
    assert findings.location is not None
    assert findings.location.page == findings.location.end_page == 2
    assert findings.location.cell_bbox is not None
    assert findings.location.raw_text == findings.raw_document_value


def test_repeated_anchor_with_identical_spans_resolves() -> None:
    """A notes block printed the same way twice anchors one span per copy; the
    copies agree, so the span extracts instead of turning ambiguous."""
    document = pymupdf.open()
    for _page_index in range(2):
        page = document.new_page(width=612, height=792)
        page.insert_text((48, 300), "Handling notes", fontsize=12)
        page.insert_text((48, 330), "Always lift the unit with two people.", fontsize=10)
        page.insert_text((48, 350), "Gloves stay on while the motor runs.", fontsize=10)
        page.insert_text((48, 400), "End of notes", fontsize=12)
    content = document.tobytes()
    document.close()
    extracted = extract(
        content, [span_field("handling", ["Handling notes"], end_labels=["End of notes"])]
    )
    handling = extracted["handling"]
    assert handling.status.value == "extracted", handling.explanation
    assert handling.raw_document_value == (
        "Always lift the unit with two people. Gloves stay on while the motor runs."
    )
    assert "Tied anchors defined the same span." in handling.explanation


def test_repeated_anchor_with_different_spans_is_ambiguous() -> None:
    document = pymupdf.open()
    for lines in (
        ["Always lift the unit with two people.", "Gloves stay on while the motor runs."],
        ["The hoist must be rated for the full crate weight."],
    ):
        page = document.new_page(width=612, height=792)
        page.insert_text((48, 300), "Handling notes", fontsize=12)
        for index, line in enumerate(lines):
            page.insert_text((48, 330 + index * 20), line, fontsize=10)
        page.insert_text((48, 420), "End of notes", fontsize=12)
    content = document.tobytes()
    document.close()
    extracted = extract(
        content, [span_field("handling", ["Handling notes"], end_labels=["End of notes"])]
    )
    assert extracted["handling"].status.value == "ambiguous"


def test_section_labels_resolve_a_repeated_span_anchor() -> None:
    """The same anchor under two different headings: section_labels picks the
    right copy, exactly as it does for text_label."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 130), "1. Specifications", fontsize=12)
    page.insert_text((48, 170), "Summary", fontsize=10)
    page.insert_text((48, 200), "Cable ratings follow the regional table.", fontsize=10)
    page.insert_text((48, 260), "2. History", fontsize=12)
    page.insert_text((48, 300), "Summary", fontsize=10)
    page.insert_text((48, 330), "Earlier revisions removed the fuse block.", fontsize=10)
    content = document.tobytes()
    document.close()
    fields = [
        span_field("summary_spec", ["Summary"], section_labels=["1. Specifications"]),
        span_field("summary_hist", ["Summary"], section_labels=["2. History"]),
        span_field("summary_any", ["Summary"]),
    ]
    extracted = extract(content, fields)
    assert extracted["summary_spec"].status.value == "extracted"
    assert (
        extracted["summary_spec"].raw_document_value == "Cable ratings follow the regional table."
    )
    assert extracted["summary_hist"].status.value == "extracted"
    assert extracted["summary_hist"].raw_document_value == (
        "Earlier revisions removed the fuse block."
    )
    assert extracted["summary_any"].status.value == "ambiguous"


def test_missing_anchor_is_not_extracted() -> None:
    extracted = extract(article_document(), [span_field("glossary", ["Glossary"])])
    glossary = extracted["glossary"]
    assert glossary.status.value == "not_extracted"
    assert "anchor" in glossary.explanation


def test_anchor_without_following_content_is_not_extracted() -> None:
    """A heading immediately followed by the next heading anchors an empty
    span; that is reported loudly, not handed on as an empty value."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 130), "1. Overview", fontsize=12)
    page.insert_text((48, 190), "2. Findings", fontsize=12)
    page.insert_text((48, 220), "Wear stays within tolerance.", fontsize=10)
    content = document.tobytes()
    document.close()
    extracted = extract(content, [span_field("overview", ["1. Overview"])])
    overview = extracted["overview"]
    assert overview.status.value == "not_extracted"
    assert "no text before the span ends" in overview.explanation


def test_span_matches_end_to_end() -> None:
    fields = [
        span_field("overview", ["1. Overview"]),
        span_field("findings", ["2. Findings"], end_labels=["References"]),
    ]
    plan = SpecificationCompiler().compile(
        specification(fields), ReferenceSource(b'{"fields": {}}', "reference.json")
    )
    extraction = SchemaExtractor().extract(plan, read(article_document()))
    comparisons = ComparisonEngine().compare(plan, extraction)
    assert len(comparisons) == 2
    # The stub reference is absent by design, so both fields skip comparison
    # after extracting.
    assert all(item.status.value == "not_compared" for item in comparisons)
    assert all(item.extraction_status.value == "extracted" for item in comparisons)


def test_column_labels_and_value_pattern_are_rejected_for_text_span() -> None:
    for key, value in (("column_labels", ["Value"]), ("value_pattern", r"^\d+$")):
        raw = json.loads(specification([span_field("overview", ["1. Overview"])]))
        raw["sections"][0]["fields"][0]["locate"][key] = value
        with pytest.raises(CompilationError) as caught:
            SpecificationCompiler().compile(
                json.dumps(raw).encode(), ReferenceSource(b'{"fields": {}}', "reference.json")
            )
        assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_end_labels_are_rejected_outside_text_span() -> None:
    raw = json.loads(specification([span_field("overview", ["1. Overview"])]))
    raw["sections"][0]["fields"][0]["locate"]["strategy"] = "table_label"
    raw["sections"][0]["fields"][0]["locate"]["end_labels"] = ["References"]
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            json.dumps(raw).encode(), ReferenceSource(b'{"fields": {}}', "reference.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_exclude_tables_is_rejected_outside_text_span() -> None:
    for strategy in ("table_label", "text_label"):
        raw = json.loads(specification([span_field("overview", ["1. Overview"])]))
        raw["sections"][0]["fields"][0]["locate"]["strategy"] = strategy
        raw["sections"][0]["fields"][0]["locate"]["exclude_tables"] = False
        with pytest.raises(CompilationError) as caught:
            SpecificationCompiler().compile(
                json.dumps(raw).encode(), ReferenceSource(b'{"fields": {}}', "reference.json")
            )
        assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_exclude_tables_is_optional_and_defaults_to_true() -> None:
    plan = SpecificationCompiler().compile(
        specification(
            [
                span_field("defaulted", ["1. Overview"]),
                span_field("opted_out", ["2. Findings"], exclude_tables=False),
                span_field("opted_in", ["References"], exclude_tables=True),
            ]
        ),
        ReferenceSource(b'{"fields": {}}', "reference.json"),
    )
    by_key = {field.key: field for field in plan.fields}
    assert by_key["defaulted"].locate.exclude_tables is True
    assert by_key["opted_out"].locate.exclude_tables is False
    assert by_key["opted_in"].locate.exclude_tables is True


def ruled_table_between_caption_and_prose() -> bytes:
    """One page, top to bottom: a findings heading, a table caption, a ruled
    2x2 table (header row plus one data row, rules drawn so the layout
    detector finds it geometrically), the paragraph AFTER the table, and a
    closing heading. The paragraph after the table is unreachable as an
    anchor of its own — every prose line here stays short, but in real
    journals each such line exceeds the 64-char anchor cap, which is exactly
    why the caption must anchor and the table must drop out."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 100), "1. Findings", fontsize=12)
    page.insert_text((48, 140), "Table 1. Measured gains", fontsize=10)
    table_top, row_height, x0, x1 = 180.0, 40.0, 48.0, 400.0
    for index in range(3):
        y = table_top + index * row_height
        page.draw_line(pymupdf.Point(x0, y), pymupdf.Point(x1, y), color=(0.6, 0.6, 0.6), width=0.7)
    for x in (x0, 220.0, x1):
        page.draw_line(
            pymupdf.Point(x, table_top),
            pymupdf.Point(x, table_top + 2 * row_height),
            color=(0.6, 0.6, 0.6),
            width=0.7,
        )
    for row, (label, value) in enumerate((("Metric", "Value"), ("Gain", "0.26"))):
        page.insert_text((x0 + 8, table_top + row * row_height + 25), label, fontsize=10)
        page.insert_text((228.0, table_top + row * row_height + 25), value, fontsize=10)
    page.insert_text((48, 340), "The paragraph after the table reports the outcome.", fontsize=10)
    page.insert_text((48, 400), "2. Closing", fontsize=12)
    content = document.tobytes()
    document.close()
    return content


def test_caption_anchored_span_excludes_the_ruled_table() -> None:
    """The cold-run trap: anchoring on the caption swallowed the whole table
    as prose. With exclude_tables (the default) the caption anchors, the
    table's lines drop out, and the span yields the post-table paragraph."""
    extracted = extract(
        ruled_table_between_caption_and_prose(),
        [span_field("caption", ["Table 1. Measured gains"], end_labels=["2. Closing"])],
    )
    caption = extracted["caption"]
    assert caption.status.value == "extracted", caption.explanation
    assert caption.raw_document_value == ("The paragraph after the table reports the outcome.")
    # The table's numbers and headers are gone; the exclusion is noted.
    assert "0.26" not in caption.raw_document_value
    assert "Metric" not in caption.raw_document_value
    assert "Excluded" in caption.explanation and "ruled-table line(s)" in caption.explanation


def test_exclude_tables_false_keeps_the_table_lines() -> None:
    """The escape hatch: exclude_tables: false restores the pre-exclusion
    behaviour, table cells joined into the prose."""
    extracted = extract(
        ruled_table_between_caption_and_prose(),
        [
            span_field(
                "caption",
                ["Table 1. Measured gains"],
                end_labels=["2. Closing"],
                exclude_tables=False,
            )
        ],
    )
    caption = extracted["caption"]
    assert caption.status.value == "extracted", caption.explanation
    assert "Metric Value Gain 0.26" in caption.raw_document_value
    assert "The paragraph after the table reports the outcome." in caption.raw_document_value
    assert "Excluded" not in caption.explanation


def test_span_emptied_by_exclusion_is_not_extracted() -> None:
    """An anchor whose only following lines are the table's: exclusion leaves
    no prose, which is reported loudly instead of handing back an empty
    value."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 100), "Table 1. Measured gains", fontsize=10)
    table_top, row_height, x0, x1 = 180.0, 40.0, 48.0, 400.0
    for index in range(3):
        y = table_top + index * row_height
        page.draw_line(pymupdf.Point(x0, y), pymupdf.Point(x1, y), color=(0.6, 0.6, 0.6), width=0.7)
    for x in (x0, 220.0, x1):
        page.draw_line(
            pymupdf.Point(x, table_top),
            pymupdf.Point(x, table_top + 2 * row_height),
            color=(0.6, 0.6, 0.6),
            width=0.7,
        )
    for row, (label, value) in enumerate((("Metric", "Value"), ("Gain", "0.26"))):
        page.insert_text((x0 + 8, table_top + row * row_height + 25), label, fontsize=10)
        page.insert_text((228.0, table_top + row * row_height + 25), value, fontsize=10)
    page.insert_text((48, 400), "2. Closing", fontsize=12)
    content = document.tobytes()
    document.close()
    extracted = extract(
        content, [span_field("caption", ["Table 1. Measured gains"], end_labels=["2. Closing"])]
    )
    caption = extracted["caption"]
    assert caption.status.value == "not_extracted"
    assert "excluded" in caption.explanation and "no prose remains" in caption.explanation


def evidence_with_table_zone(*, bordered: bool | None, bbox: Any) -> DocumentEvidence:
    """Synthetic single-page evidence: three prose lines, the middle one
    inside a table's rectangle, plus one label:value line the text strategy
    can pair. The table's provenance and rectangle are exactly what the
    caller passes, so exclusion is tested against the zone machinery alone."""

    def line(text: str, y: float) -> Any:
        from janus.comparison.content import BoundingBox, TextElement

        return TextElement(
            text=text,
            confidence=1,
            element_type="line",
            bbox=BoundingBox(x=48, y=y, width=400, height=10, page=0),
        )

    from janus.comparison.content import PageContent, TableStructure

    table = TableStructure(
        rows=1, columns=1, cells=[], header_row=False, bbox=bbox, bordered=bordered
    )
    page = PageContent(
        page_number=0,
        width=612,
        height=792,
        text_elements=[
            line("1. Section", 100),
            line("Inside the zone.", 200),
            line("After it.", 300),
        ],
        tables=[table],
    )
    return DocumentEvidence(
        document_id="document", document_hash="hash", reader_name="test", pages=[page]
    )


def test_only_geometrically_ruled_tables_exclude_span_lines() -> None:
    """A ruled table's zone excludes the lines inside it; a borderless
    inference over the same lines keeps them (prose misread as a grid must
    not be swallowed) and says so; a table with no provenance recorded
    (older evidence) behaves like an inference."""
    from janus.comparison.content import BoundingBox

    zone = BoundingBox(x=40, y=190, width=420, height=30, page=0)
    plan = SpecificationCompiler().compile(
        specification([span_field("section", ["1. Section"])]),
        ReferenceSource(b'{"fields": {}}', "reference.json"),
    )
    extractor = SchemaExtractor()
    ruled = extractor.extract(plan, evidence_with_table_zone(bordered=True, bbox=zone)).fields[0]
    assert ruled.status.value == "extracted", ruled.explanation
    assert "Inside the zone." not in ruled.raw_document_value
    assert ruled.raw_document_value == "After it."
    assert "Excluded 1 ruled-table line(s)." in ruled.explanation

    inferred = extractor.extract(plan, evidence_with_table_zone(bordered=False, bbox=zone)).fields[
        0
    ]
    assert inferred.status.value == "extracted", inferred.explanation
    assert "Inside the zone." in inferred.raw_document_value
    assert "no reliably ruled borders" in inferred.explanation

    legacy = extractor.extract(plan, evidence_with_table_zone(bordered=None, bbox=zone)).fields[0]
    assert legacy.status.value == "extracted", legacy.explanation
    assert "Inside the zone." in legacy.raw_document_value
    assert "no reliably ruled borders" in legacy.explanation


def test_table_without_any_rectangle_keeps_its_lines_with_a_note() -> None:
    """A rebuilt borderless table with neither a table bbox nor cell boxes
    cannot be located at all: the lines stay and the explanation notes it."""
    plan = SpecificationCompiler().compile(
        specification([span_field("section", ["1. Section"])]),
        ReferenceSource(b'{"fields": {}}', "reference.json"),
    )
    extraction = (
        SchemaExtractor()
        .extract(plan, evidence_with_table_zone(bordered=False, bbox=None))
        .fields[0]
    )
    assert extraction.status.value == "extracted", extraction.explanation
    assert extraction.raw_document_value == "Inside the zone. After it."
    assert "no reliably ruled borders" in extraction.explanation


def test_ambiguous_anchor_tie_names_the_rival() -> None:
    """A genuine tie — the same anchor printed twice defining different
    spans — reports ambiguous WITH the rival named: its text and its page,
    in the explanation and in the artifact."""
    document = pymupdf.open()
    for _page_index, lines in enumerate(
        (
            ["Always lift the unit with two people."],
            ["The hoist must be rated for the full crate weight."],
        )
    ):
        page = document.new_page(width=612, height=792)
        page.insert_text((48, 300), "Handling notes", fontsize=12)
        for index, line in enumerate(lines):
            page.insert_text((48, 330 + index * 20), line, fontsize=10)
        page.insert_text((48, 420), "End of notes", fontsize=12)
    content = document.tobytes()
    document.close()
    extracted = extract(
        content, [span_field("handling", ["Handling notes"], end_labels=["End of notes"])]
    )
    handling = extracted["handling"]
    assert handling.status.value == "ambiguous"
    # The rival is named: its text and its page (the runner-up sits on page 1;
    # the winning line is on page 0).
    assert "runner-up 'Handling notes' at page 1, line" in handling.explanation
    assert handling.runner_up_score is not None
    assert handling.runner_up_text == "Handling notes"
    assert handling.runner_up_location is not None
    assert handling.runner_up_location.page == 1


def test_subset_rival_anchor_tie_resolves_by_complete_token_set() -> None:
    """The wrapped-header trap from the cold run: a shorter line whose tokens
    all sit inside the alias ('Summary' against 'Summary of Findings') ties
    at 1.0 on token-set similarity. The complete-token-set tiebreak resolves
    toward the line that carries every alias token."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 100), "Summary", fontsize=12)
    page.insert_text((48, 130), "Alpha content line.", fontsize=10)
    page.insert_text((48, 180), "Summary of Findings", fontsize=12)
    page.insert_text((48, 210), "Beta content line.", fontsize=10)
    page.insert_text((48, 260), "Closing", fontsize=12)
    content = document.tobytes()
    document.close()
    extracted = extract(
        content, [span_field("findings", ["Summary of Findings"], end_labels=["Closing"])]
    )
    findings = extracted["findings"]
    assert findings.status.value == "extracted", findings.explanation
    assert findings.raw_document_value == "Beta content line."
    assert findings.row_context == "Summary of Findings"


def test_unresolvable_subset_tie_stays_ambiguous_with_the_rival_named() -> None:
    """When the tiebreak cannot separate the tied lines — the rival carries
    the alias just as completely as the winner — the field stays ambiguous,
    now with the rival named instead of bare tied scores."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((48, 100), "Notes", fontsize=12)
    page.insert_text((48, 130), "Alpha content line.", fontsize=10)
    page.insert_text((48, 200), "Handling notes", fontsize=12)
    page.insert_text((48, 230), "Beta content line.", fontsize=10)
    page.insert_text((48, 280), "Closing", fontsize=12)
    content = document.tobytes()
    document.close()
    extracted = extract(content, [span_field("notes", ["Notes"], end_labels=["Closing"])])
    notes = extracted["notes"]
    assert notes.status.value == "ambiguous"
    # 'Notes' is a complete token set of BOTH lines (the rival 'Handling
    # notes' carries it as a superset), so completeness cannot resolve.
    assert "runner-up" in notes.explanation
    assert "page 0, line" in notes.explanation
    assert notes.runner_up_text is not None
