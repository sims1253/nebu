"""Interview-style schema-authoring benchmark harness.

Evaluates generated comparison specifications against natural-language task
descriptions (benchmarks/schema-authoring/*.task.md). Three stages, runnable
separately so agents and language models can be mixed:

  write  — an LM turns a task description + grammar docs + document text into
           a specification (and, where the task asks for it, reference data).
  score  — deterministic: does the specification compile, how many fields
           extract cleanly, how do comparisons land. No LM involved.
  judge  — fuzzy: an LM (or an agent, via --judge-mode agent) reads the task
           and the extracted values and verdicts whether the requested
           information was captured cleanly.

Document families (families.json) extend all three stages: one task applied to
several documents of the same type, so a single specification is written once
and then compiled, scored, and judged against every member. Family results are
additive — score.json gains a "families" key only when a round actually holds a
family specification, so rounds without families are unaffected.

Run from apps/server:

    uv run --no-sync python scripts/schema_authoring_bench.py score ../..
    uv run --no-sync python scripts/schema_authoring_bench.py write --round r1 --slug invoice
    uv run --no-sync python scripts/schema_authoring_bench.py judge --round r1

The LM is any OpenAI-compatible endpoint, configured with JANUS_LM_API_KEY,
JANUS_LM_BASE_URL (default: the z.ai coding endpoint), and JANUS_LM_MODEL
(default glm-5.3-flash; a reasoning model — budgets are generous on purpose).
Writer and judge outputs land in benchmarks/schema-authoring/runs/<round>/ and
are gitignored: they are evaluation artifacts, not product code.

The prompt used by `write` is deliberately plain. When a prompt-optimization
pass (DSPy/GEPA or similar) is wanted, the collected (task, document,
specification, scores, verdicts) tuples in runs/ are its trainset; swap the
`_lm_complete` call for the optimized program and keep this harness as the
scorer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

JANUS_ROOT = Path(__file__).resolve().parents[3]
BENCH_DIR = JANUS_ROOT / "benchmarks" / "schema-authoring"

DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_MODEL = "glm-5.3-flash"
MAX_TOKENS = 16_000
RETRY_ATTEMPTS = 4


def _lm_complete(system: str, user: str) -> str:
    api_key = os.environ.get("JANUS_LM_API_KEY", "")
    base_url = os.environ.get("JANUS_LM_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("JANUS_LM_MODEL", DEFAULT_MODEL)
    if not api_key:
        raise SystemExit(
            "JANUS_LM_API_KEY is not set; export it (the writer/judge need a model)."
        )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
    }
    # Structured outputs do not need the reasoning budget; JANUS_LM_THINKING=off
    # makes long-document calls return in seconds instead of many minutes.
    if os.environ.get("JANUS_LM_THINKING", "").lower() in {"off", "0", "false", "disabled"}:
        body["thinking"] = {"type": "disabled"}
    payload = json.dumps(body).encode()
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                f"{base_url.rstrip('/')}/chat/completions",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=900) as response:
                body = json.loads(response.read())
            content = body["choices"][0]["message"]["content"]
            if not content.strip():
                raise ValueError("empty completion content (reasoning consumed the budget?)")
            return content
        except (OSError, ValueError, KeyError) as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            print(f"LM attempt {attempt} failed ({exc}); retrying", file=sys.stderr)
            time.sleep(15 * attempt)
    raise AssertionError("unreachable")


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response.

    Tolerates the two common deviations: a fenced block, and a bare
    specification object returned without the {"specification": ...} wrapper.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"no JSON object found in response:\n{text[:500]}")


def _families() -> dict[str, dict]:
    """Families from families.json: one task applied to several same-type
    documents (the generality requirement — one specification must work on
    every member). Paths inside the manifest are relative to BENCH_DIR.
    """
    manifest = BENCH_DIR / "families.json"
    if not manifest.exists():
        return {}
    return json.loads(manifest.read_text())


def _family_tasks() -> set[str]:
    return {
        family.get("task", f"{name}.task.md") for name, family in _families().items()
    }


def _family_members(family: dict) -> list[dict]:
    members = []
    for entry in family.get("members", []):
        document = BENCH_DIR / entry["document"]
        reference = BENCH_DIR / entry["reference"] if entry.get("reference") else None
        members.append(
            {
                "id": document.stem,
                "document": document,
                "reference": reference if reference is not None and reference.exists() else None,
            }
        )
    return members


def _cases() -> list[dict]:
    # A family task describes several documents at once; it is scored through
    # the family path, never as a single-document case.
    family_tasks = _family_tasks()
    cases = []
    for task_file in sorted(BENCH_DIR.glob("*.task.md")):
        if task_file.name in family_tasks:
            continue
        slug = task_file.name.removesuffix(".task.md")
        cases.append({"slug": slug, "task": task_file.read_text(), "task_file": task_file})
    return cases


def _document_for(slug: str) -> Path | None:
    patterns = [
        JANUS_ROOT / "examples" / slug / "document.pdf",
        JANUS_ROOT / "examples" / slug,
        JANUS_ROOT / "challenges" / f"{slug}.pdf",
    ]
    # Benchmark corpus cases keep their document under corpus/<slug>*/.
    corpus = BENCH_DIR / "corpus"
    if corpus.is_dir():
        for directory in sorted(corpus.glob(f"{slug}*")):
            if directory.is_dir():
                patterns.extend(sorted(directory.glob("*.pdf")))
    for pattern in patterns:
        if pattern.exists():
            return pattern
    return None


def _document_text(path: Path, max_chars: int = 18_000) -> str:
    import pymupdf

    with pymupdf.open(path) as document:
        pages = []
        for index, page in enumerate(document):
            text = page.get_text().strip()
            if text:
                pages.append(f"--- page {index} ---\n{text}")
    joined = "\n\n".join(pages)
    return joined[:max_chars] + ("\n...[truncated]" if len(joined) > max_chars else "")


# --- write -------------------------------------------------------------------


def cmd_write(round_name: str, slug: str | None) -> None:
    system = (
        "You write comparison specifications for the Janus document-comparison "
        "tool. Output ONLY a JSON object with keys \"specification\" (the "
        "comparison specification) and, when the task asks you to create "
        "reference data, \"reference\" (the reference dataset). Follow the "
        "grammar documentation exactly."
    )
    grammar_paths = [
        JANUS_ROOT / "skills" / "schema-authoring" / "SKILL.md",
        JANUS_ROOT / "docs" / "comparison-specification-v1.md",
        JANUS_ROOT / "docs" / "locators.md",
        JANUS_ROOT / "docs" / "repeated-rows.md",
    ]
    grammar_docs = "\n\n".join(path.read_text() for path in grammar_paths if path.exists())
    schema_text = (JANUS_ROOT / "packages" / "contracts" / "schema" /
                   "comparison-specification.v1.json").read_text()

    for case in _cases():
        if slug and case["slug"] != slug:
            continue
        document = _document_for(case["slug"])
        if document is None or not document.is_file():
            print(f"{case['slug']}: no document found, skipping")
            continue
        reference_text = ""
        for pattern in (
            JANUS_ROOT / "examples" / case["slug"] / "reference.json",
            JANUS_ROOT / "examples" / case["slug"] / "reference.csv",
        ):
            if pattern.exists():
                reference_text = (
                    f"# Reference data ({pattern.name}, provided by the user — "
                    "pointers must resolve against THIS structure)\n\n"
                    + pattern.read_text()[:4000]
                )
                break
        user = (
            f"# Grammar documentation\n\n{grammar_docs}\n\n"
            f"# JSON schema\n\n{schema_text}\n\n"
            f"# Task (from the user, in their words)\n\n{case['task']}\n\n"
            f"# Document text ({document.name})\n\n{_document_text(document)}\n\n"
            f"{reference_text}\n\n"
            "Write the specification now. Read the document text carefully to "
            "pick labels that actually appear in it. If reference data was "
            "provided above, use it as-is: do NOT include a reference in your "
            "output."
        )
        try:
            response = _extract_json(_lm_complete(system, user))
            if "specification" not in response and "sections" in response:
                # Bare specification without the documented wrapper.
                response = {"specification": response}
        except Exception as exc:  # one bad case must not kill the round
            print(f"{case['slug']}: WRITE FAILED — {exc}", file=sys.stderr)
            continue
        out_dir = BENCH_DIR / "runs" / round_name / case["slug"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "specification.json").write_text(
            json.dumps(response["specification"], indent=2) + "\n"
        )
        if "reference" in response and response["reference"] is not None:
            (out_dir / "reference.json").write_text(
                json.dumps(response["reference"], indent=2) + "\n"
            )
        print(f"{case['slug']}: wrote specification")

    for name, family in _families().items():
        if slug and slug != name:
            continue
        members = _family_members(family)
        if not members:
            continue
        first = members[0]
        provided = next((m["reference"] for m in members if m["reference"]), None)
        reference_text = ""
        if provided is not None:
            reference_text = (
                f"# Reference data ({provided.name}, provided by the user for the "
                "first member — every member of this family ships an equivalent "
                "keyed reference; pointers must resolve against THIS structure)\n\n"
                + provided.read_text()[:4000]
            )
        user = (
            f"# Grammar documentation\n\n{grammar_docs}\n\n"
            f"# JSON schema\n\n{schema_text}\n\n"
            f"# Task (from the user, in their words)\n\n"
            f"{(BENCH_DIR / family.get('task', f'{name}.task.md')).read_text()}\n\n"
            f"# Document text ({first['document'].name} — the first of "
            f"{len(members)} documents this ONE specification must cover)\n\n"
            f"{_document_text(first['document'])}\n\n"
            f"{reference_text}\n\n"
            "Write the specification now. This is a document FAMILY: one "
            "specification, authored once, must run unchanged against every "
            "member — the documents share a type but drift in layout, labels, "
            "and column headers between members. Do not tune labels, headers, "
            "or pointers so narrowly to the document above that they break on "
            "the other members."
        )
        try:
            response = _extract_json(_lm_complete(system, user))
            if "specification" not in response and "sections" in response:
                response = {"specification": response}
        except Exception as exc:  # one bad family must not kill the round
            print(f"{name}: WRITE FAILED — {exc}", file=sys.stderr)
            continue
        out_dir = BENCH_DIR / "runs" / round_name / name
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "specification.json").write_text(
            json.dumps(response["specification"], indent=2) + "\n"
        )
        if "reference" in response and response["reference"] is not None:
            (out_dir / "reference.json").write_text(
                json.dumps(response["reference"], indent=2) + "\n"
            )
        print(f"{name}: wrote family specification ({len(members)} members)")


# --- score -------------------------------------------------------------------


def _score_case(case: dict, run_dir: Path) -> dict:
    from janus.comparison.compiler import SpecificationCompiler
    from janus.comparison.engine import ComparisonEngine
    from janus.comparison.extractor import SchemaExtractor
    from janus.comparison.reader import TextLayerDocumentReader
    from janus.comparison.reference import ReferenceSource

    result = {"slug": case["slug"], "compiles": False, "diagnostics": [], "fields": 0}
    spec_path = run_dir / case["slug"] / "specification.json"
    if not spec_path.exists():
        result["error"] = "no generated specification"
        return result

    document = _document_for(case["slug"])
    if document is None or not document.is_file():
        result["error"] = "document not found"
        return result

    reference = None
    generated_reference = run_dir / case["slug"] / "reference.json"
    if generated_reference.exists():
        reference = ReferenceSource.from_path(generated_reference)
    else:
        for candidate in (
            JANUS_ROOT / "examples" / case["slug"] / "reference.json",
            JANUS_ROOT / "examples" / case["slug"] / "reference.csv",
        ):
            if candidate.exists():
                reference = ReferenceSource.from_path(candidate)
                break

    try:
        plan = SpecificationCompiler().compile(spec_path.read_bytes(), reference)
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
    evidence = TextLayerDocumentReader().read(document, case["slug"])
    extraction = SchemaExtractor().extract(plan, evidence)
    statuses: dict[str, int] = {}
    for field in extraction.fields:
        statuses[field.status.value] = statuses.get(field.status.value, 0) + 1
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


def _score_family(name: str, family: dict, run_dir: Path) -> dict | None:
    """Run the one generated family specification against every member.

    A member's reference comes from the manifest when one is listed (that is
    the provided, trusted dataset); the writer-created reference under runs/
    is only a fallback for members the manifest leaves bare. Returns None
    when the round holds no specification for this family.
    """
    from janus.comparison.compiler import SpecificationCompiler
    from janus.comparison.engine import ComparisonEngine
    from janus.comparison.extractor import SchemaExtractor
    from janus.comparison.reader import TextLayerDocumentReader
    from janus.comparison.reference import ReferenceSource

    spec_path = run_dir / name / "specification.json"
    if not spec_path.exists():
        return None

    result = {"slug": name, "members": []}
    for member in _family_members(family):
        member_result: dict = {
            "id": member["id"],
            "document": str(member["document"].relative_to(BENCH_DIR)),
            "compiles": False,
            "diagnostics": [],
            "fields": 0,
        }
        reference = None
        if member["reference"] is not None:
            reference = ReferenceSource.from_path(member["reference"])
        else:
            generated = run_dir / name / "reference.json"
            if generated.exists():
                reference = ReferenceSource.from_path(generated)
        try:
            plan = SpecificationCompiler().compile(spec_path.read_bytes(), reference)
        except Exception as exc:
            diagnostics = getattr(exc, "diagnostics", None)
            member_result["diagnostics"] = (
                [
                    {"code": d.code, "path": d.specification_path, "message": d.message}
                    for d in diagnostics
                ]
                if diagnostics
                else [{"code": type(exc).__name__, "path": "/", "message": str(exc)}]
            )
            result["members"].append(member_result)
            continue

        member_result["compiles"] = True
        member_result["fields"] = len(plan.fields)
        evidence = TextLayerDocumentReader().read(
            member["document"], f"{name}-{member['id']}"
        )
        extraction = SchemaExtractor().extract(plan, evidence)
        statuses: dict[str, int] = {}
        for field in extraction.fields:
            statuses[field.status.value] = statuses.get(field.status.value, 0) + 1
        member_result["extraction"] = statuses

        comparisons = ComparisonEngine().compare(plan, extraction)
        comparison_statuses: dict[str, int] = {}
        for item in comparisons:
            comparison_statuses[item.status.value] = (
                comparison_statuses.get(item.status.value, 0) + 1
            )
        member_result["comparison"] = comparison_statuses
        member_result["extracted_values"] = [
            {
                "field": item.field_key,
                "label": item.field_label,
                "document_value": item.document_value,
                "status": item.status.value,
            }
            for item in comparisons
        ]
        result["members"].append(member_result)

    ratios = []
    for member_result in result["members"]:
        extracted = member_result.get("extraction", {}).get("extracted", 0)
        ratio = extracted / member_result["fields"] if member_result["fields"] else 0.0
        member_result["extracted_ratio"] = ratio
        ratios.append(ratio)
    # worst_member: the weakest member's extracted/fields — the family is only
    # as general as its worst member. overfit_gap: spread between the best and
    # worst member — a spec tuned to one member scores near 1.0 there and low
    # elsewhere, which is exactly the overfit this benchmark exists to catch.
    result["worst_member"] = min(ratios) if ratios else 0.0
    result["overfit_gap"] = max(ratios) - min(ratios) if ratios else 0.0
    return result


def cmd_score(round_name: str | None) -> None:
    rounds = sorted(p.name for p in (BENCH_DIR / "runs").iterdir()) if round_name is None else [round_name]
    for name in rounds:
        run_dir = BENCH_DIR / "runs" / name
        results = [_score_case(case, run_dir) for case in _cases()]
        report = {"round": name, "cases": results}
        family_results = [
            scored
            for family_name, family in _families().items()
            if (scored := _score_family(family_name, family, run_dir)) is not None
        ]
        if family_results:
            report["families"] = family_results
        (run_dir / "score.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"== round {name} ==")
        for case in results:
            if not case.get("compiles"):
                print(f"  {case['slug']}: FAIL — {case.get('error') or case['diagnostics'][:1]}")
                continue
            extraction = case.get("extraction", {})
            comparison = case.get("comparison", {})
            extracted = extraction.get("extracted", 0)
            print(
                f"  {case['slug']}: {case['fields']} fields, "
                f"{extracted} extracted, {extraction.get('ambiguous', 0)} ambiguous, "
                f"{extraction.get('not_extracted', 0)} not-extracted, "
                f"comparison {comparison}"
            )
        for family_result in family_results:
            print(
                f"  [family] {family_result['slug']}: "
                f"worst_member={family_result['worst_member']:.3f} "
                f"overfit_gap={family_result['overfit_gap']:.3f}"
            )
            for member in family_result["members"]:
                if not member.get("compiles"):
                    print(
                        f"    {member['id']}: FAIL — {member['diagnostics'][:1]}"
                    )
                    continue
                extraction = member.get("extraction", {})
                comparison = member.get("comparison", {})
                extracted = extraction.get("extracted", 0)
                print(
                    f"    {member['id']}: {member['fields']} fields, "
                    f"{extracted} extracted, {extraction.get('ambiguous', 0)} ambiguous, "
                    f"{extraction.get('not_extracted', 0)} not-extracted, "
                    f"comparison {comparison}"
                )


# --- judge -------------------------------------------------------------------


def _family_task_text(slug: str) -> str:
    family = _families().get(slug, {})
    task_file = BENCH_DIR / family.get("task", f"{slug}.task.md")
    return task_file.read_text()


def _aggregate_family_verdicts(member_verdicts: dict[str, dict]) -> dict:
    """Roll per-member verdicts into one family verdict (notes aggregated)."""
    member_scores = [
        verdict.get("score")
        for verdict in member_verdicts.values()
        if isinstance(verdict.get("score"), (int, float))
    ]
    missing = [
        f"{member_id}: {item}"
        for member_id, verdict in member_verdicts.items()
        for item in verdict.get("missing", [])
    ]
    dirty = [
        f"{member_id}: {item}"
        for member_id, verdict in member_verdicts.items()
        for item in verdict.get("dirty", [])
    ]
    notes = " | ".join(
        f"{member_id}: {verdict.get('notes', '')}"
        for member_id, verdict in member_verdicts.items()
    )
    return {
        "covered": all(verdict.get("covered") for verdict in member_verdicts.values()),
        "missing": missing,
        "dirty": dirty,
        "score": sum(member_scores) / len(member_scores) if member_scores else None,
        "notes": notes,
    }


def cmd_judge(round_name: str, mode: str) -> None:
    run_dir = BENCH_DIR / "runs" / round_name
    score_path = run_dir / "score.json"
    if not score_path.exists():
        raise SystemExit("run `score` first; the judge reads score.json")
    scores = json.loads(score_path.read_text())

    if mode == "agent":
        requests_dir = run_dir / "judge-requests"
        requests_dir.mkdir(exist_ok=True)
        for case in scores["cases"]:
            if not case.get("compiles"):
                continue
            payload = {
                "task": next(c["task"] for c in _cases() if c["slug"] == case["slug"]),
                "extracted_values": case["extracted_values"],
            }
            (requests_dir / f"{case['slug']}.md").write_text(
                "# Judge request\n\n## Task\n\n"
                + payload["task"]
                + "\n\n## Extracted values\n\n```json\n"
                + json.dumps(payload["extracted_values"], indent=2)
                + "\n```\n\n"
                "Write your verdict as JSON to judge-responses/<slug>.json with keys: "
                "covered (bool), missing (list of str), dirty (list of str), "
                "score (0.0-1.0), notes (str).\n"
            )
        for family in scores.get("families", []):
            task = _family_task_text(family["slug"])
            for member in family["members"]:
                if not member.get("compiles") or not member.get("extracted_values"):
                    continue
                stem = f"{family['slug']}__{member['id']}"
                (requests_dir / f"{stem}.md").write_text(
                    "# Judge request (document family)\n\n"
                    f"Family: {family['slug']} — member document: {member['id']} "
                    "(one specification is judged across every member)\n\n"
                    "## Task\n\n"
                    + task
                    + "\n\n## Extracted values\n\n```json\n"
                    + json.dumps(member["extracted_values"], indent=2)
                    + "\n```\n\n"
                    f"Write your verdict as JSON to judge-responses/{stem}.json with keys: "
                    "covered (bool), missing (list of str), dirty (list of str), "
                    "score (0.0-1.0), notes (str).\n"
                )
        print(f"wrote judge requests to {requests_dir}")
        # Aggregate family member responses when an agent has already answered.
        responses_dir = run_dir / "judge-responses"
        family_aggregates = {}
        for family in scores.get("families", []):
            member_verdicts = {}
            for member in family["members"]:
                response_path = responses_dir / f"{family['slug']}__{member['id']}.json"
                if response_path.exists():
                    try:
                        member_verdicts[member["id"]] = json.loads(response_path.read_text())
                    except json.JSONDecodeError:
                        continue
            if member_verdicts:
                family_aggregates[family["slug"]] = {
                    "members": member_verdicts,
                    "aggregate": _aggregate_family_verdicts(member_verdicts),
                }
        if family_aggregates:
            (run_dir / "judge-families.json").write_text(
                json.dumps(family_aggregates, indent=2) + "\n"
            )
            print(f"aggregated family judge responses into {run_dir / 'judge-families.json'}")
        return

    system = (
        "You judge structured extraction results against a natural-language "
        "task. The values do not need to match any particular reference "
        "exactly; the standard is: every piece of information the task asked "
        "for is present, correctly attributed, and cleanly structured for "
        "downstream use. Respond with ONLY a JSON object: "
        '{"covered": bool, "missing": [str], "dirty": [str], '
        '"score": 0.0-1.0, "notes": str}.'
    )
    verdicts = {}
    for case in scores["cases"]:
        if not case.get("compiles"):
            continue
        task = next(c["task"] for c in _cases() if c["slug"] == case["slug"])
        user = (
            f"# Task\n\n{task}\n\n# Extracted values\n\n"
            + json.dumps(case["extracted_values"], indent=2)
        )
        verdicts[case["slug"]] = _extract_json(_lm_complete(system, user))
        (run_dir / "judge.json").write_text(json.dumps(verdicts, indent=2) + "\n")
        verdict = verdicts[case["slug"]]
        print(f"  {case['slug']}: score={verdict.get('score')} covered={verdict.get('covered')}")
        for note in verdict.get("missing", [])[:3]:
            print(f"    missing: {note}")

    for family in scores.get("families", []):
        task = _family_task_text(family["slug"])
        member_verdicts = {}
        for member in family["members"]:
            if not member.get("compiles") or not member.get("extracted_values"):
                continue
            stem = f"{family['slug']}::{member['id']}"
            user = (
                f"# Task (one specification is expected to cover every member of "
                f"this family)\n\n{task}\n\n# Member document\n\n{member['id']}\n\n"
                f"# Extracted values\n\n"
                + json.dumps(member["extracted_values"], indent=2)
            )
            verdicts[stem] = _extract_json(_lm_complete(system, user))
            (run_dir / "judge.json").write_text(json.dumps(verdicts, indent=2) + "\n")
            member_verdicts[member["id"]] = verdicts[stem]
            verdict = verdicts[stem]
            print(f"  {stem}: score={verdict.get('score')} covered={verdict.get('covered')}")
            for note in verdict.get("missing", [])[:3]:
                print(f"    missing: {note}")
        if member_verdicts:
            verdicts[f"{family['slug']}::family"] = _aggregate_family_verdicts(member_verdicts)
            (run_dir / "judge.json").write_text(json.dumps(verdicts, indent=2) + "\n")
            aggregate = verdicts[f"{family['slug']}::family"]
            print(
                f"  {family['slug']} (family aggregate): score={aggregate['score']} "
                f"covered={aggregate['covered']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    write_parser = sub.add_parser("write")
    write_parser.add_argument("--round", required=True)
    write_parser.add_argument("--slug")
    score_parser = sub.add_parser("score")
    score_parser.add_argument("--round")
    judge_parser = sub.add_parser("judge")
    judge_parser.add_argument("--round", required=True)
    judge_parser.add_argument("--judge-mode", choices=["lm", "agent"], default="lm")
    args = parser.parse_args()

    if args.command == "write":
        cmd_write(args.round, args.slug)
    elif args.command == "score":
        cmd_score(args.round)
    elif args.command == "judge":
        cmd_judge(args.round, args.judge_mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
