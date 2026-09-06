"""Score schema-writer output through the comparison pipeline.

The benchmark harness supplies local case and document discovery. Scores
measure extraction and agreement, not completeness against the requested task."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import dspy

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource

# src/janus/authoring/metric.py -> <repo>/apps/server/src/janus/authoring/metric.py
JANUS_ROOT = Path(__file__).resolve().parents[5]
HARNESS_PATH = JANUS_ROOT / "apps" / "server" / "scripts" / "schema_authoring_bench.py"

_harness_module: Any = None


def _harness() -> Any:
    """Load and cache the benchmark harness from its source-tree path."""
    global _harness_module
    if _harness_module is None:
        spec = importlib.util.spec_from_file_location("janus_schema_authoring_bench", HARNESS_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load benchmark harness from {HARNESS_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _harness_module = module
    return _harness_module


def provided_reference_for(document: Path) -> Path | None:
    """The trusted reference shipped beside a document, if one exists.

    Mirrors the harness's lookup order: a family member's manifest reference
    first, then ``examples/<slug>/reference.{json,csv}``.
    """
    harness = _harness()
    for family in harness._families().values():
        for member in harness._family_members(family):
            if member["document"] == document and member["reference"] is not None:
                return member["reference"]
    for candidate in (
        document.parent / "reference.json",
        document.parent / "reference.csv",
    ):
        if candidate.exists():
            return candidate
    return None


def evaluate_specification(
    document: Path,
    specification_json: str,
    reference_json: str = "",
    provided_reference: Path | None = None,
) -> dict:
    """Evaluate a candidate with writer-authored reference data when supplied,
    otherwise the provided reference. Return extraction and comparison counts."""
    result: dict[str, Any] = {
        "document": document.name,
        "compiles": False,
        "diagnostics": [],
        "fields": 0,
        "sections": 0,
        "extraction": {},
        "comparison": {},
        "extracted_values": [],
    }
    reference: ReferenceSource | None = None
    if reference_json.strip():
        reference = ReferenceSource(
            content=reference_json.strip().encode(), filename="reference.json"
        )
    elif provided_reference is not None:
        reference = ReferenceSource.from_path(provided_reference)
    if reference is None:
        result["diagnostics"] = [
            {
                "code": "MISSING_REFERENCE_DATA",
                "path": "/",
                "message": "no reference data: the writer produced none and the document ships none",
            }
        ]
        return result

    try:
        plan = SpecificationCompiler().compile(specification_json.strip().encode(), reference)
    except Exception as exc:  # CompilationError carries structured diagnostics
        diagnostics = getattr(exc, "diagnostics", None)
        result["diagnostics"] = (
            [
                {"code": d.code, "path": d.specification_path, "message": d.message}
                for d in diagnostics
            ]
            if diagnostics
            else [{"code": type(exc).__name__, "path": "/", "message": str(exc)}]
        )
        return result

    result["compiles"] = True
    result["fields"] = len(plan.fields)
    result["sections"] = len({f.section_key for f in plan.fields})
    evidence = TextLayerDocumentReader().read(document, document.stem)
    extraction = SchemaExtractor().extract(plan, evidence)
    statuses: dict[str, int] = {}
    for extracted_field in extraction.fields:
        statuses[extracted_field.status.value] = statuses.get(extracted_field.status.value, 0) + 1
    result["extraction"] = statuses

    comparisons = ComparisonEngine().compare(plan, extraction)
    comparison_statuses: dict[str, int] = {}
    for item in comparisons:
        comparison_statuses[item.status.value] = comparison_statuses.get(item.status.value, 0) + 1
    result["comparison"] = comparison_statuses
    result["extracted_values"] = [
        {
            "field": item.field_key,
            "label": item.field_label,
            "document_value": item.document_value,
            "status": item.status.value,
        }
        for item in comparisons
    ]
    return result


# --- scoring -----------------------------------------------------------------


def score_result(result: dict) -> float:
    """score = 0.5 * extracted_ratio + 0.3 * match_ratio + 0.2 * structure_bonus.

    extracted_ratio is the harness's coverage measure (extracted / fields);
    match_ratio is match / all comparisons; the judge-free structure bonus
    averages three booleans (fields > 0, sections used, no diagnostics), which
    coincide for a compiling plan — so the term is effectively a flat 0.2 for
    any compiling specification with at least one field, 0 otherwise.
    """
    if not result.get("compiles"):
        return 0.0
    fields = result.get("fields", 0)
    extraction = result.get("extraction", {})
    extracted_ratio = extraction.get("extracted", 0) / fields if fields else 0.0
    comparison = result.get("comparison", {})
    total = sum(comparison.values())
    match_ratio = comparison.get("match", 0) / total if total else 0.0
    structure_bonus = (
        (fields > 0) + (result.get("sections", 0) > 0) + (not result.get("diagnostics"))
    ) / 3
    return max(0.0, min(1.0, 0.5 * extracted_ratio + 0.3 * match_ratio + 0.2 * structure_bonus))


def _hints(result: dict) -> list[str]:
    """Reflection hints derived from the actual diagnostics and statuses."""
    hints: list[str] = []
    for diagnostic in result.get("diagnostics", [])[:3]:
        code = diagnostic.get("code", "")
        if code == "REFERENCE_POINTER_NOT_FOUND":
            hints.append(
                f"reference pointer {diagnostic.get('message', '')!r} does not resolve — "
                "align reference pointers with the dataset you author (or the provided one)"
            )
        elif code in {
            "INVALID_SPECIFICATION_SCHEMA",
            "INVALID_SPECIFICATION",
            "INVALID_SPECIFICATION_JSON",
        }:
            hints.append(
                f"grammar violation at {diagnostic.get('path', '/')}: {diagnostic.get('message', '')}"
            )
        else:
            hints.append(f"{code}: {diagnostic.get('message', '')}")
    extraction = result.get("extraction", {})
    if extraction.get("ambiguous"):
        hints.append(
            f"{extraction['ambiguous']} field(s) ambiguous: likely identically-labeled rows "
            "needing locate.section_labels (name the group), or the same label across several "
            "columns needing locate.column_labels (name the column header)"
        )
    if extraction.get("not_extracted"):
        hints.append(
            f"{extraction['not_extracted']} field(s) not extracted: quote label forms that "
            "actually appear in the document text, add aliases for wording drift, or lower "
            "locate.minimum_confidence"
        )
    comparison = result.get("comparison", {})
    if comparison.get("mismatch"):
        hints.append(
            f"{comparison['mismatch']} mismatch(es): check compare.operator and normalizer "
            "order (currency_symbol before numeric_punctuation, percent_symbol for %), and "
            "absolute/relative tolerances for rounded figures"
        )
    if result.get("compiles") and not result.get("fields"):
        hints.append("the plan has zero fields — every section needs at least one field")
    if not hints:
        hints.append("clean run — consider covering more of the task's requested information")
    return hints


def _feedback_text(result: dict, label: str) -> str:
    extraction = result.get("extraction", {})
    comparison = result.get("comparison", {})
    head = (
        f"{label}: {'compiles' if result.get('compiles') else 'DOES NOT COMPILE'}, "
        f"{result.get('fields', 0)} fields, {extraction.get('extracted', 0)} extracted, "
        f"{extraction.get('ambiguous', 0)} ambiguous, "
        f"{extraction.get('not_extracted', 0)} not-extracted, comparison {comparison}"
    )
    return head + ". Hints: " + "; ".join(_hints(result))


def _family_score(member_results: list[dict], label: str) -> tuple[float, str]:
    """Family score: half mean, half worst member, minus 0.5 * overfit_gap."""
    member_scores = [score_result(member) for member in member_results]
    worst_member = min(member_scores)
    overfit_gap = max(member_scores) - min(member_scores)
    mean = sum(member_scores) / len(member_scores) if member_scores else 0.0
    score = max(0.0, min(1.0, 0.5 * mean + 0.5 * worst_member - 0.5 * overfit_gap))
    lines = [
        _feedback_text(member, f"{label}[{member.get('document', '?')}]")
        for member in member_results
    ]
    summary = (
        f"family {label}: worst_member={worst_member:.3f} overfit_gap={overfit_gap:.3f} "
        "(a spec tuned to one member scores high there and low elsewhere — keep labels "
        "general enough for every member)"
    )
    return score, "\n".join([*lines, summary])


# --- targets -----------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkTarget:
    """One optimizable unit: a single-document case or a document family."""

    slug: str
    task: str
    is_family: bool = False
    documents: tuple[Path, ...] = ()
    provided_references: tuple[Path | None, ...] = ()
    example_inputs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.documents:
            raise ValueError(f"target {self.slug!r} has no documents")
        if len(self.documents) != len(self.provided_references):
            raise ValueError(f"target {self.slug!r}: documents and provided_references must align")


def build_metric(target: BenchmarkTarget):
    """Return a DSPy score and diagnostic feedback for one case or family."""

    def metric(
        gold: dspy.Example,  # noqa: ARG001 (protocol slot; the target is closed over)
        pred: dspy.Prediction,
        trace: list | None = None,  # noqa: ARG001 (protocol slot)
        pred_name: str | None = None,  # noqa: ARG001 (protocol slot)
        pred_trace: list | None = None,  # noqa: ARG001 (protocol slot)
        program_trace: list | None = None,  # noqa: ARG001 (protocol slot)
    ) -> dspy.Prediction:
        specification = getattr(pred, "specification_json", None)
        if not specification or not str(specification).strip():
            return dspy.Prediction(
                score=0.0,
                feedback=f"{target.slug}: the writer returned no specification JSON",
            )
        reference_json = str(getattr(pred, "reference_json", "") or "")
        if target.is_family:
            member_results = [
                evaluate_specification(document, str(specification), reference_json, provided)
                for document, provided in zip(
                    target.documents, target.provided_references, strict=True
                )
            ]
            score, feedback = _family_score(member_results, target.slug)
        else:
            document, provided = target.documents[0], target.provided_references[0]
            result = evaluate_specification(document, str(specification), reference_json, provided)
            score, feedback = score_result(result), _feedback_text(result, target.slug)
        return dspy.Prediction(score=score, feedback=feedback)

    metric.__name__ = f"authoring_metric_{target.slug.replace('-', '_')}"
    metric.__qualname__ = metric.__name__
    return metric


def summarize_tool_result(result: dict) -> str:
    """Compact JSON for the Flex validate_specification tool."""
    extraction = result.get("extraction", {})
    comparison = result.get("comparison", {})
    problems: list[str] = [
        f"{d.get('code', '')} at {d.get('path', '/')}: {d.get('message', '')}"
        for d in result.get("diagnostics", [])[:4]
    ]
    for value in result.get("extracted_values", []):
        if value["status"] != "match" and len(problems) < 8:
            problems.append(
                f"{value['status']}: {value['label']!r} -> {str(value['document_value'])[:60]!r}"
            )
    return json.dumps(
        {
            "compiles": result.get("compiles", False),
            "diagnostics": result.get("diagnostics", [])[:4],
            "fields": result.get("fields", 0),
            "extracted": extraction.get("extracted", 0),
            "ambiguous": extraction.get("ambiguous", 0),
            "not_extracted": extraction.get("not_extracted", 0),
            "match": comparison.get("match", 0),
            "mismatch": comparison.get("mismatch", 0),
            "sample_problems": problems,
        }
    )
