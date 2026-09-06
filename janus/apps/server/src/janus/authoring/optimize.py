"""Optimize a schema writer against local benchmark cases with DSPy/GEPA.

Run: python -m janus.authoring.optimize --round opt1 --cases invoice --writer predict
Outputs go to benchmarks/schema-authoring/runs/<round>-opt/. Model settings
use JANUS_LM_API_KEY, JANUS_LM_BASE_URL, JANUS_LM_MODEL, and JANUS_LM_THINKING."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import TYPE_CHECKING

import dspy

from janus.authoring.metric import (
    JANUS_ROOT,
    BenchmarkTarget,
    _harness,
    build_metric,
    provided_reference_for,
)
from janus.authoring.writer import FlexSchemaWriter, PredictSchemaWriter

if TYPE_CHECKING:
    from pathlib import Path

_THINKING_DISABLED = os.environ.get("JANUS_LM_THINKING", "").lower() in {
    "off",
    "0",
    "false",
    "disabled",
}


def _parallel_support() -> tuple[bool, dict | None]:
    """Use parallel-proposal strategies when the installed GEPA exposes them."""
    try:
        from gepa.strategies.proposal_sampling import PxNSampling
        from gepa.strategies.proposal_selection import AllImprovements
    except ImportError:
        return False, None
    return True, {
        "sampling_strategy": PxNSampling(p=2, n=2),
        "selection_strategy": AllImprovements(),
    }


PARALLEL_AVAILABLE, PARALLEL_GEPA_KWARGS = _parallel_support()


def _build_lm(temperature: float, max_tokens: int) -> dspy.LM:
    api_key = os.environ.get("JANUS_LM_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "JANUS_LM_API_KEY is not set; the optimizer needs a model "
            "(same variables as scripts/schema_authoring_bench.py)."
        )
    base_url = os.environ.get("JANUS_LM_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    model = os.environ.get("JANUS_LM_MODEL", "glm-5.3-flash")
    kwargs: dict = {
        "api_base": base_url,
        "api_key": api_key,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if _THINKING_DISABLED:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return dspy.LM(f"openai/{model}", **kwargs)


def _grammar_docs(harness) -> str:
    grammar_paths = [
        harness.JANUS_ROOT / "skills" / "schema-authoring" / "SKILL.md",
        harness.JANUS_ROOT / "docs" / "comparison-specification-v1.md",
        harness.JANUS_ROOT / "docs" / "locators.md",
        harness.JANUS_ROOT / "docs" / "repeated-rows.md",
    ]
    docs = "\n\n".join(path.read_text() for path in grammar_paths if path.exists())
    schema_text = (
        harness.JANUS_ROOT
        / "packages"
        / "contracts"
        / "schema"
        / "comparison-specification.v1.json"
    ).read_text()
    return f"# Grammar documentation\n\n{docs}\n\n# JSON schema\n\n{schema_text}"


def _reference_text(path: Path | None) -> str:
    if path is None:
        return ""
    return (
        f"# Reference data ({path.name}, provided by the user — "
        "pointers must resolve against THIS structure)\n\n" + path.read_text()[:4000]
    )


def _build_targets(
    names: list[str], *, strict: bool
) -> tuple[list[BenchmarkTarget], list[str], list[str]]:
    """Build targets, reporting problems and skipped cases.

    Explicitly requested missing documents are problems; default discovery skips them."""
    harness = _harness()
    grammar_docs = _grammar_docs(harness)
    families = harness._families()
    case_slugs = [case["slug"] for case in harness._cases()]

    targets: list[BenchmarkTarget] = []
    problems: list[str] = []
    skipped: list[str] = []
    for name in names:
        if name in families:
            family = families[name]
            members = harness._family_members(family)
            if not members:
                (problems if strict else skipped).append(f"{name} (empty family)")
                continue
            task = (harness.BENCH_DIR / family.get("task", f"{name}.task.md")).read_text()
            documents = tuple(member["document"] for member in members)
            provided = tuple(member["reference"] for member in members)
            first_provided = next((p for p in provided if p is not None), None)
            targets.append(
                BenchmarkTarget(
                    slug=name,
                    task=task,
                    is_family=True,
                    documents=documents,
                    provided_references=provided,
                    example_inputs={
                        "task": task,
                        "grammar_docs": grammar_docs,
                        "document_text": harness._document_text(members[0]["document"]),
                        "reference_text": _reference_text(first_provided),
                        "document_path": str(documents[0].relative_to(JANUS_ROOT)),
                    },
                )
            )
        elif name in case_slugs:
            task = next(c["task"] for c in harness._cases() if c["slug"] == name)
            document = harness._document_for(name)
            if document is None or not document.is_file():
                (problems if strict else skipped).append(
                    f"{name} (no document the harness can resolve)"
                )
                continue
            provided = provided_reference_for(document)
            targets.append(
                BenchmarkTarget(
                    slug=name,
                    task=task,
                    documents=(document,),
                    provided_references=(provided,),
                    example_inputs={
                        "task": task,
                        "grammar_docs": grammar_docs,
                        "document_text": harness._document_text(document),
                        "reference_text": _reference_text(provided),
                        "document_path": str(document.relative_to(JANUS_ROOT)),
                    },
                )
            )
        else:
            problems.append(name)
    return targets, problems, skipped


def _trainset(targets: list[BenchmarkTarget]) -> list[dspy.Example]:
    return [
        dspy.Example(slug=target.slug, **target.example_inputs).with_inputs(
            "task", "grammar_docs", "document_text", "reference_text", "document_path"
        )
        for target in targets
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--round", required=True, help="run name; artifacts land in runs/<round>-opt/"
    )
    parser.add_argument(
        "--cases",
        default="",
        help="comma-separated case slugs and/or family names (default: all cases)",
    )
    parser.add_argument(
        "--writer",
        choices=["predict", "flex"],
        default="predict",
        help="predict = stable dspy.Predict baseline; flex = dspy.Flex with the "
        "validate_specification tool (needs Deno; experimental)",
    )
    parser.add_argument("--max-metric-calls", type=int, default=24, help="GEPA evaluation budget")
    parser.add_argument(
        "--parallel",
        action="store_true",
        help=(
            "parallel proposals: PxNSampling(p=2, n=2) + AllImprovements with 8 evaluation threads"
            if PARALLEL_AVAILABLE
            else "parallel proposals: NOT available in the installed gepa; degrades to "
            "plain dspy.GEPA"
        ),
    )
    args = parser.parse_args(argv)

    harness = _harness()
    requested = [name.strip() for name in args.cases.split(",") if name.strip()]
    default_all = not requested
    if default_all:
        requested = [case["slug"] for case in harness._cases()]
    targets, problems, skipped = _build_targets(requested, strict=not default_all)
    if problems:
        known = [case["slug"] for case in harness._cases()] + list(harness._families())
        raise SystemExit(
            f"unknown or unusable case(s): {', '.join(problems)}; known: {', '.join(known)}"
        )
    if skipped:
        print(f"skipping (harness cannot resolve their documents): {', '.join(skipped)}")
    if not targets:
        raise SystemExit("no usable cases to optimize")
    trainset = _trainset(targets)
    print(f"trainset: {len(trainset)} example(s) — {', '.join(t.slug for t in targets)}")

    if args.writer == "flex":
        try:
            writer = FlexSchemaWriter()
        except RuntimeError as exc:
            print(f"cannot use --writer flex: {exc}", file=sys.stderr)
            return 2
    else:
        writer = PredictSchemaWriter()

    lm = _build_lm(temperature=0.2, max_tokens=16_000)
    dspy.configure(lm=lm)
    if hasattr(writer.generate, "set_lm"):
        writer.generate.set_lm(lm)
    reflection_lm = _build_lm(temperature=1.0, max_tokens=8_000)

    metrics = {target.slug: build_metric(target) for target in targets}
    trajectory: list[dict] = []

    def metric(gold, pred, trace=None, pred_name=None, pred_trace=None, program_trace=None):
        slug = getattr(gold, "slug", "")
        scorer = metrics.get(slug)
        if scorer is None:
            return dspy.Prediction(score=0.0, feedback=f"unknown case {slug!r}")
        outcome = scorer(gold, pred, trace, pred_name, pred_trace, program_trace)
        trajectory.append({"case": slug, "score": outcome.score})
        print(f"[metric] {slug}: score={outcome.score:.3f}")
        return outcome

    optimizer_kwargs: dict = {
        "metric": metric,
        "reflection_lm": reflection_lm,
        "max_metric_calls": args.max_metric_calls,
        "track_stats": True,
    }
    parallel_note = "not requested"
    if args.parallel:
        if PARALLEL_AVAILABLE and PARALLEL_GEPA_KWARGS is not None:
            optimizer_kwargs["num_threads"] = 8
            optimizer_kwargs["gepa_kwargs"] = PARALLEL_GEPA_KWARGS
            parallel_note = "PxNSampling(p=2, n=2) + AllImprovements, num_threads=8"
        else:
            parallel_note = "requested but unavailable in installed gepa; ran plain dspy.GEPA"

    print(
        f"optimizing ({args.writer} writer, max_metric_calls={args.max_metric_calls}, "
        f"parallel: {parallel_note})"
    )
    optimizer = dspy.GEPA(**optimizer_kwargs)
    compiled = optimizer.compile(writer, trainset=trainset)

    out_dir = harness.BENCH_DIR / "runs" / f"{args.round}-opt"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "program-state.json").write_text(
        json.dumps(compiled.dump_state(), indent=2, default=str) + "\n"
    )

    detailed = getattr(compiled, "detailed_results", None)
    summary = {
        "round": args.round,
        "writer": args.writer,
        "cases": [target.slug for target in targets],
        "model": os.environ.get("JANUS_LM_MODEL", "glm-5.3-flash"),
        "max_metric_calls": args.max_metric_calls,
        "parallel": parallel_note,
        "thinking_disabled": _THINKING_DISABLED,
        "metric_calls": trajectory,
        "val_aggregate_scores": getattr(detailed, "val_aggregate_scores", None),
        "discovery_eval_counts": getattr(detailed, "discovery_eval_counts", None),
        "objective_pareto_front": getattr(detailed, "objective_pareto_front", None),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    print(f"\nsaved {out_dir / 'program-state.json'} and {out_dir / 'summary.json'}")
    if trajectory:
        scores = [call["score"] for call in trajectory]
        print(
            f"metric trajectory ({len(scores)} calls): "
            + ", ".join(f"{score:.3f}" for score in scores)
        )
        print(f"best metric call: {max(scores):.3f}")
    val_scores = getattr(detailed, "val_aggregate_scores", None) or []
    if val_scores:
        print("per-candidate validation scores: " + ", ".join(f"{s:.3f}" for s in val_scores))
    return 0


if __name__ == "__main__":
    sys.exit(main())
