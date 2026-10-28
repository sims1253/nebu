"""Typographic minus signs are numeric syntax, while evidence stays as printed."""

import hashlib
import json
import subprocess
import sys

import pymupdf
import pytest

from janus.comparison.extractor import extract_component
from janus.comparison.models import ValueRule
from janus.extraction import ExtractionSpecification, extract
from janus.extraction.models import Scalar
from janus.extraction.parsing import parse_value

MINUS_SIGNS = ["\u2212", "\ufe63", "\uff0d"]


@pytest.mark.parametrize("sign", MINUS_SIGNS)
@pytest.mark.parametrize(
    ("text", "options", "expected"),
    [
        ("{sign}0.040", {}, "-0.040"),
        ("{sign}42", {"type": "integer"}, -42),
        ("{sign}12.5%", {"type": "percentage"}, "-12.5"),
        ("{sign}0.000", {}, "-0.000"),
        ("{sign}1,275.50", {"group_separator": ","}, "-1275.50"),
        (
            "{sign}1.275,50",
            {"decimal_separator": ",", "group_separator": "."},
            "-1275.50",
        ),
        (
            "Dose{sign} {sign}0.040 mg",
            {"strip_prefix": "Dose{sign}", "strip_suffix": "mg"},
            "-0.040",
        ),
    ],
)
def test_unicode_minus_in_declared_number_formats(sign, text, options, expected):
    options = {k: v.format(sign=sign) if isinstance(v, str) else v for k, v in options.items()}
    rule = Scalar.model_validate({"type": "decimal", **options})
    assert parse_value(text.format(sign=sign), rule) == expected


@pytest.mark.parametrize("sign", MINUS_SIGNS)
@pytest.mark.parametrize("text", ["1,234", "1,23.00", "12.3.4", "NaN", "Infinity", "-0.040"])
def test_unicode_minus_does_not_relax_numeric_validation(sign, text):
    with pytest.raises(ValueError):
        parse_value(sign + text, Scalar(type="decimal"))


@pytest.mark.parametrize("sign", MINUS_SIGNS)
def test_nonnumeric_fields_keep_their_existing_syntax(sign):
    text = f"Dose{sign}0.040; \ufb01; \u2460"
    assert parse_value(text, Scalar(type="text")) == text
    with pytest.raises(ValueError, match="ISO date"):
        parse_value(f"2026{sign}10{sign}28", Scalar(type="date"))
    with pytest.raises(ValueError, match="true/false"):
        parse_value(sign + "1", Scalar(type="boolean"))


@pytest.mark.parametrize("sign", MINUS_SIGNS)
@pytest.mark.parametrize(
    ("shape", "text", "component", "expected"),
    [
        ("count_percentage", "{sign}12 (34%)", "count", "-12"),
        ("mean_standard_deviation", "{sign}0.040 (0.025)", "mean", "-0.040"),
        ("estimate_interval", "{sign}0.040 ({sign}0.187, 0.107)", "estimate", "-0.040"),
        ("estimate_interval", "{sign}0.040 ({sign}0.187, 0.107)", "lower", "-0.187"),
        ("estimate_interval", "{sign}0.040 ({sign}0.187, 0.107)", "upper", "0.107"),
        ("estimate_interval", "{sign}0.040 (0.025) [{sign}0.187, 0.107]", "lower", "-0.187"),
        ("estimate_interval", "{sign}0,040 ({sign}0,187; 0,107)", "lower", "-0,187"),
        ("range", "{sign}2\u2013{sign}1", "minimum", "-2"),
        ("range", "{sign}2\u2014{sign}1", "maximum", "-1"),
        ("range", "{sign}2-{sign}1", "maximum", "-1"),
    ],
)
def test_compound_numeric_components_accept_unicode_minus(sign, shape, text, component, expected):
    rule = ValueRule(type="decimal", source_shape=shape, component=component)
    assert extract_component(rule, text.format(sign=sign)) == (expected, None)


@pytest.mark.parametrize("sign", MINUS_SIGNS)
def test_compound_annotations_and_text_components_stay_as_printed(sign):
    raw = f"{sign}0.040 ({sign}0.187, 0.107); P<0.001; BF\u2265{sign}100"
    for component, expected in (("p_value", "<0.001"), ("bayes_factor", f"\u2265{sign}100")):
        rule = Scalar(type="text", source_shape="estimate_interval", component=component)
        assert extract_component(rule, raw) == (expected, None)
        assert parse_value(raw, rule) == expected
    rule = Scalar(type="decimal", source_shape="estimate_interval", component="p_value")
    with pytest.raises(ValueError):
        parse_value(raw, rule)
    rule = Scalar(type="text", source_shape="estimate_interval", component="estimate")
    assert parse_value(raw, rule) == sign + "0.040"


@pytest.mark.parametrize("dash", ["\u2013", "\u2014"])
def test_range_dashes_are_not_negative_signs(dash):
    with pytest.raises(ValueError):
        parse_value(dash + "0.040", Scalar(type="decimal"))


@pytest.mark.parametrize("sign", MINUS_SIGNS)
def test_unicode_minus_is_not_a_range_separator(sign):
    rule = ValueRule(type="decimal", source_shape="range", component="minimum")
    value, error = extract_component(rule, f"1 {sign} 2")
    assert value is None
    assert error


@pytest.mark.parametrize("sign", MINUS_SIGNS)
def test_public_pdf_extraction_and_cli_keep_exact_values_and_source_text(tmp_path, sign):
    pdf = tmp_path / "trial.pdf"
    raw = f"{sign}0.040 ({sign}0.187, 0.107)"
    text = f"Arm{sign}A"
    with pymupdf.open() as document:
        page = document.new_page()
        writer = pymupdf.TextWriter(page.rect)
        font = pymupdf.Font("cjk")
        for x in (50, 170, 500):
            page.draw_line((x, 50), (x, 130))
        for y in (50, 90, 130):
            page.draw_line((50, y), (500, y))
        for position, line in (
            ((60, 75), "Measure"),
            ((180, 75), "Value"),
            ((60, 115), "Effect"),
            ((180, 115), raw),
            ((50, 180), f"Change: {sign}0.040"),
            ((50, 220), f"Arm: {text}"),
        ):
            writer.append(position, line, font=font, fontsize=11)
        writer.write_text(page)
        assert raw in page.get_text()
        document.save(pdf)
    fields = {
        component: {
            "type": "decimal",
            "source_shape": "estimate_interval",
            "component": component,
            "locate": {"strategy": "table_label", "labels": ["Effect"], "column_labels": ["Value"]},
        }
        for component in ("estimate", "lower", "upper")
    }
    fields["change"] = {
        "type": "decimal",
        "locate": {"strategy": "text_label", "labels": ["Change"]},
    }
    fields["arm"] = {"type": "text", "locate": {"strategy": "text_label", "labels": ["Arm"]}}
    fields["effects"] = {
        "type": "records",
        "columns": {
            "label": {"type": "text", "labels": ["Measure"]},
            "value": {
                "type": "decimal",
                "source_shape": "estimate_interval",
                "component": "estimate",
                "labels": ["Value"],
            },
        },
    }
    specification = ExtractionSpecification.model_validate(
        {"name": "Synthetic trial", "fields": fields}
    )
    result = extract(pdf, specification)
    expected = {
        "estimate": "-0.040",
        "lower": "-0.187",
        "upper": "0.107",
        "change": "-0.040",
        "arm": text,
        "effects": [{"label": "Effect", "value": "-0.040"}],
    }
    assert result.data == expected
    assert result.document_hash == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert all(field.status == "extracted" for field in result.fields)
    for field in result.fields:
        assert field.source is not None
        assert field.source.cell_bbox is not None
        assert field.source.raw_text == {
            "/change": sign + "0.040",
            "/arm": text,
            "/effects/0/label": "Effect",
        }.get(field.path, raw)
    spec_path = tmp_path / "specification.json"
    spec_path.write_text(specification.model_dump_json(), encoding="utf-8")
    cli = subprocess.run(
        [sys.executable, "-m", "janus.cli", "extract", str(pdf), str(spec_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr
    saved = json.loads(cli.stdout)
    assert saved["data"] == expected
    assert saved["fields"] == result.model_dump(mode="json")["fields"]
    assert saved["document_hash"] == result.document_hash
