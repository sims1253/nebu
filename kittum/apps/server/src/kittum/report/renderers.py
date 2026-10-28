"""The core report targets, rendered from a `Review`.

Each renderer returns files with paths relative to the target; `render` stores them by hash and
`seal_bundle` puts them under `reports/<target>/`.
"""

import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from kittum.core import Id, Record
from kittum.judge import VERIFIER_LIMITS
from kittum.report.prisma import flows, svg
from kittum.report.ris_format import ris_dumps, ris_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from kittum.report.review import ItemResult, Review, SearchRun, StageResult
    from kittum.search import SourceRecord


class RenderedFile(Record):
    path: str
    media_type: str
    data: bytes


type Renderer = Callable[[Review, str | None], Sequence[RenderedFile]]
"""(review, scope_id or None for every scope) -> files. Profiles register these."""

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text: str) -> str:
    return _UNSAFE.sub("_", text).strip("_") or "_"


def json_file(path: str, value: Any) -> RenderedFile:
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n"
    return RenderedFile(path=path, media_type="application/json", data=data.encode())


def text_file(path: str, text: str, media_type: str = "text/markdown") -> RenderedFile:
    return RenderedFile(path=path, media_type=f"{media_type}; charset=utf-8", data=text.encode())


def md_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(value: Any) -> str:
        return str("" if value is None else value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


# The PRISMA flow.


def render_prisma(review: Review, scope_id: str | None) -> list[RenderedFile]:
    files = []
    for f in flows(review, scope_id):
        name = safe_name(f"{f.topic_id}-{f.scope_id}")
        title = f"PRISMA 2020 flow: {f.topic_id}, scope {f.scope_id}"
        files.append(json_file(f"{name}.json", f.model_dump(mode="json")))
        files.append(
            RenderedFile(
                path=f"{name}.svg", media_type="image/svg+xml", data=svg(f, title).encode()
            )
        )
    return files


# RIS files.


def representative(review: Review) -> dict[Id, SourceRecord]:
    """One record per report: the occurrence with the longest abstract, then the earliest."""
    if review.identity is None:
        return {}
    by_occurrence = review.records_by_occurrence()
    best: dict[Id, tuple[tuple[int, int, int], SourceRecord]] = {}
    for link in review.identity.links:
        found = by_occurrence.get(link.occurrence_id)
        if found is None:
            continue
        run, record = found
        key = (-len(record.abstract or ""), run.event_seq, record.ordinal)
        if link.report_id not in best or key < best[link.report_id][0]:
            best[link.report_id] = (key, record)
    return {report: record for report, (_, record) in best.items()}


def _registry_reports(review: Review) -> set[Id]:
    """Reports whose every occurrence comes from a registry search."""
    if review.identity is None:
        return set()
    kinds: dict[Id, set[str]] = defaultdict(set)
    by_occurrence = review.records_by_occurrence()
    for link in review.identity.links:
        if (found := by_occurrence.get(link.occurrence_id)) is not None:
            kinds[link.report_id].add(found[0].source_kind)
    return {r for r, k in kinds.items() if k == {"registry"}}


def item_entries(
    review: Review, items: Sequence[ItemResult], note: Callable[[ItemResult], list[str]]
) -> list[dict[str, Any]]:
    records = representative(review)
    study_of = review.identity.study_of() if review.identity else {}
    registry = _registry_reports(review)
    entries = []
    for item in items:
        report_id = _report_id(item.item_ref)
        record = records.get(report_id) if report_id else None
        if record is None:
            continue
        entries.append(
            ris_entry(
                record,
                registry=report_id in registry,
                report_id=report_id,
                study_id=study_of.get(report_id) if report_id else None,
                notes=note(item),
            )
        )
    return entries


def _report_id(ref: str) -> Id | None:
    from uuid import UUID

    try:
        return UUID(ref)
    except ValueError:
        return None


def search_file_name(n: int, run: SearchRun) -> str:
    day = run.execution.started_at.date().isoformat()
    return (
        f"{n:02d}_{safe_name(run.source.id if run.source else run.execution.plan.source)}_{day}.ris"
    )


def render_ris(review: Review, scope_id: str | None) -> list[RenderedFile]:
    files: list[RenderedFile] = []
    report_of = review.identity.report_of() if review.identity else {}
    study_of = review.identity.study_of() if review.identity else {}
    if scope_id is None:
        for n, run in enumerate(review.searches, start=1):
            registry = run.source_kind == "registry"
            entries = [
                ris_entry(
                    r,
                    registry=registry,
                    report_id=report_of.get(r.occurrence_id),
                    study_id=study_of.get(report_of.get(r.occurrence_id)),  # type: ignore[arg-type]
                )
                for r in sorted(run.execution.records, key=lambda r: r.ordinal)
            ]
            files.append(_ris_file(f"searches/{search_file_name(n, run)}", entries))
        sources: dict[Id, list[str]] = defaultdict(list)
        by_occurrence = review.records_by_occurrence()
        for link in review.identity.links if review.identity else ():
            if (found := by_occurrence.get(link.occurrence_id)) is not None:
                run, record = found
                sources[link.report_id].append(
                    f"Found in {run.source.name if run.source else run.execution.plan.platform}"
                    f" as {record.provider_record_id}"
                )
        registry_reports = _registry_reports(review)
        entries = [
            ris_entry(
                record,
                registry=report in registry_reports,
                report_id=report,
                study_id=study_of.get(report),
                notes=sorted(sources[report]),
            )
            for report, record in sorted(representative(review).items(), key=lambda x: str(x[0]))
        ]
        files.append(_ris_file("deduplicated.ris", entries))
    for topic_id, scope in review.scopes():
        if scope_id not in (None, scope):
            continue
        base = safe_name(f"{topic_id}-{scope}")
        ta = review.stage(scope, "title_abstract", topic_id)
        ft = review.stage(scope, "full_text", topic_id)
        files.append(
            _ris_file(
                f"{base}/included.ris",
                item_entries(review, _with(ft, "include"), lambda _: ["Included"]),
            )
        )
        files.append(
            _ris_file(
                f"{base}/excluded_title_abstract.ris",
                item_entries(
                    review,
                    _with(ta, "exclude"),
                    lambda i: [
                        f"Excluded at title and abstract: {', '.join(i.reason_criterion_ids)}"
                    ],
                ),
            )
        )
        files.append(
            _ris_file(
                f"{base}/excluded_full_text.ris",
                item_entries(
                    review,
                    _with(ft, "exclude"),
                    lambda i: [f"Excluded at full text: {', '.join(i.reason_criterion_ids)}"],
                ),
            )
        )
    return files


def _with(stage: StageResult | None, outcome: str) -> list[ItemResult]:
    return [i for i in stage.records if i.outcome == outcome] if stage else []


def _ris_file(path: str, entries: list[dict[str, Any]]) -> RenderedFile:
    return RenderedFile(
        path=path, media_type="application/x-research-info-systems", data=ris_dumps(entries)
    )


# The search report, with the fields PRISMA-S asks for.


def actor_name(review: Review, actor_id: Id | None) -> str:
    actor = next((a for a in review.actors if a.id == actor_id), None)
    return actor.identity_ref if actor else str(actor_id)


def search_rows(review: Review) -> list[dict[str, Any]]:
    rows = []
    for n, run in enumerate(review.searches, start=1):
        e, plan = run.execution, run.execution.plan
        topic = next((t for t in review.document.search_topics if t.id == plan.topic_id), None)
        rows.append(
            {
                "number": n,
                "execution_id": str(e.id),
                "topic_id": plan.topic_id,
                "source": plan.source,
                "protocol_source_id": run.source.id if run.source else None,
                "database": run.source.name if run.source else plan.platform,
                "source_kind": run.source_kind,
                "platform": plan.platform,
                "search_type": e.kind,
                "started_at": e.started_at.isoformat(),
                "ended_at": e.ended_at.isoformat(),
                "strategy": plan.strategy,
                "limits": list(plan.limits),
                "protocol_limits": topic.source_plan.limits if topic else None,
                "study_filter": run.source.study_filter if run.source else None,
                "query_translation": e.query_translation,
                "reported_count": e.reported_count,
                "retrieved_count": e.retrieved_count,
                "skipped_record_ids": list(e.skipped_record_ids),
                "raw_artifact_hashes": list(e.raw_artifact_hashes),
                "run_by": actor_name(review, run.actor_id),
                "event_seq": run.event_seq,
                "ris_file": f"ris/searches/{search_file_name(n, run)}",
            }
        )
    return rows


def render_search_report(review: Review, scope_id: str | None) -> list[RenderedFile]:
    del scope_id  # searches belong to topics, not scopes
    rows = search_rows(review)
    searched = {(r["topic_id"], r["protocol_source_id"]) for r in rows}
    unsearched = [
        {"topic_id": t.id, "source_id": s.id, "name": s.name, "kind": s.kind}
        for t in review.document.search_topics
        for s in t.source_plan.sources
        if (t.id, s.id) not in searched
    ]
    deviations = [
        {"topic_id": t.id, "source_id": d.source_id, "justification": dict(d.justification)}
        for t in review.document.search_topics
        for d in t.source_plan.deviations
    ]
    data = {
        "project": review.project_name,
        "cutoff_seq": review.cutoff_seq,
        "searches": rows,
        "planned_sources_not_searched": unsearched,
        "source_deviations": deviations,
    }
    lines = [
        f"# Search report: {review.project_name}",
        "",
        f"Every search recorded in the ledger through event {review.cutoff_seq}, with the fields "
        "PRISMA-S asks for.",
        "",
        md_table(
            ["#", "Topic", "Database", "Platform", "Date", "Reported", "Retrieved"],
            [
                [
                    r["number"],
                    r["topic_id"],
                    r["database"],
                    r["platform"],
                    r["started_at"][:10],
                    r["reported_count"],
                    r["retrieved_count"],
                ]
                for r in rows
            ],
        ),
    ]
    for r in rows:
        lines += [
            f"## {r['number']}. {r['database']} ({r['platform']}), topic {r['topic_id']}",
            "",
            f"- Source: {r['source']} ({r['source_kind']})",
            f"- Search type: {r['search_type'].replace('_', ' ')}",
            f"- Run: {r['started_at']} to {r['ended_at']} by {r['run_by']} (ledger event "
            f"{r['event_seq']})",
            f"- Limits: {'; '.join(r['limits']) or 'none in the strategy'}"
            + (f"; protocol: {r['protocol_limits']}" if r["protocol_limits"] else ""),
            f"- Study filter: {r['study_filter'] or 'none'}",
            f"- Records reported by the source: {r['reported_count']}; retrieved: "
            f"{r['retrieved_count']}"
            + (
                f"; not mapped: {len(r['skipped_record_ids'])} ({', '.join(r['skipped_record_ids'])})"
                if r["skipped_record_ids"]
                else ""
            ),
            f"- Records as exported: `{r['ris_file']}`",
            f"- Raw responses (SHA-256): {', '.join(f'`{h}`' for h in r['raw_artifact_hashes'])}",
            "",
            "Strategy as run:",
            "",
            "```text",
            r["strategy"],
            "```",
            "",
        ]
        if r["query_translation"]:
            lines += ["Query translation reported by the platform:", "", "```text"]
            lines += [r["query_translation"], "```", ""]
    if unsearched:
        lines += ["## Planned sources without a recorded search", ""]
        lines.append(
            md_table(
                ["Topic", "Source", "Name", "Kind"],
                [[u["topic_id"], u["source_id"], u["name"], u["kind"]] for u in unsearched],
            )
        )
    if deviations:
        lines += ["## Deviations from the source plan", ""]
        lines.append(
            md_table(
                ["Topic", "Source", "Justification"],
                [
                    [t.id, dev.source_id, "; ".join(dev.justification.values())]
                    for t in review.document.search_topics
                    for dev in t.source_plan.deviations
                ],
            )
        )
    return [json_file("search_report.json", data), text_file("search_report.md", "\n".join(lines))]


# The AI-use disclosure.


def _pseudonyms(review: Review) -> dict[str, str]:
    people = sorted(
        {str(p) for s in review.stages for p in (*s.screeners, s.auditor_id) if p is not None}
    )
    return {p: f"Reviewer {n}" for n, p in enumerate(people, start=1)} | {"ai": "AI"}


def disclosure(review: Review) -> dict[str, Any]:
    """The disclosure record: stored data only, no template."""
    names = _pseudonyms(review)
    protocol = review.document
    models = []
    for j in review.judges:
        recipe = j.recipe
        models.append(
            {
                "judge_config_id": j.id,
                "recipe_id": j.model_recipe_id,
                "family": recipe.get("family"),
                "repository": recipe.get("repository"),
                "upstream_repository": recipe.get("upstream_repository"),
                "revision": recipe.get("revision"),
                "weights_hash": j.weights_hash,
                "weights_licence": recipe.get("weights_licence"),
                "licence_url": recipe.get("licence_url"),
                "serving": recipe.get("serving"),
                "reasoning": recipe.get("reasoning"),
                "reported_model_identities": list(j.model_identities),
                "verdicts": j.verdicts,
                "decoding": j.decoding,
                "prompt_version": j.prompt_version,
                "prompt_hash": j.prompt_hash,
                "schema_hash": j.schema_hash,
            }
        )
    topics = [
        {
            "topic_id": t.id,
            "mode": t.mode.value,
            "mode_rationale": dict(t.mode_rationale) if t.mode_rationale else None,
            "validation_pack": t.validation_pack,
            "audit_plan": t.audit.model_dump(mode="json"),
            "stopping": t.stopping.model_dump(mode="json") if t.stopping else None,
        }
        for t in protocol.search_topics
    ]
    stages = []
    for s in review.stages:
        records = s.records
        excluded = [i for i in records if i.outcome == "exclude"]
        stages.append(
            {
                "stage_id": str(s.id),
                "topic_id": s.topic_id,
                "scope_id": s.scope_id,
                "stage": s.stage,
                "mode": s.mode.value,
                "reveal": s.reveal,
                "judge_config_id": s.judge_config_id,
                "records": len(records),
                "records_read_by_ai": sum(1 for i in records if i.ai_excludes is not None),
                "ai_proposed_exclusions": sum(1 for i in records if i.ai_excludes),
                "agreement": [
                    {
                        "kind": a.kind,
                        "raters": [names.get(r, r) for r in a.raters],
                        "actor_ids": list(a.raters),
                        "items": a.items,
                        "kappa": a.kappa,
                        "pabak": a.pabak,
                    }
                    for a in s.agreement
                ],
                "reveals_shown": s.reveals_shown,
                "revised_after_reveal": s.revised_after_reveal,
                "conflicts_resolved": s.conflicts_resolved,
                "rescreens": s.rescreens.model_dump(mode="json"),
                "rescreen_cases": [
                    {
                        "item_ref": i.item_ref,
                        "reason": i.rescreen.reason,
                        "auditor": names.get(
                            str(i.rescreen.auditor_id), str(i.rescreen.auditor_id)
                        ),
                        "auditor_id": str(i.rescreen.auditor_id),
                        "event_id": str(i.rescreen.event_id),
                        "result": "pending"
                        if i.rescreen_upheld is None
                        else ("upheld" if i.rescreen_upheld else "adjudicated"),
                        "outcome": i.outcome,
                    }
                    for i in records
                    if i.rescreen is not None
                ],
                "excluded": len(excluded),
                "excluded_with_ai_proposal": sum(1 for i in excluded if i.ai_excludes),
                "excluded_by_ai_alone": 0,
                "audit": s.audit.model_dump(mode="json"),
            }
        )
    return {
        "project": review.project_name,
        "cutoff_seq": review.cutoff_seq,
        "protocol_hash": review.protocol.version.content_hash if review.protocol else None,
        "models": models,
        "topics": topics,
        "stages": stages,
        "verifier_limits": list(VERIFIER_LIMITS),
        "reproducibility_tier": "replay",
    }


_READING = {"human_ai": " (model's own reading)", "human_ai_verified": " (verified reading)"}


def _fmt(value: float | None) -> str:
    return "not defined" if value is None else f"{value:.2f}"


def render_disclosure(review: Review, scope_id: str | None) -> list[RenderedFile]:
    del scope_id
    d = disclosure(review)
    lines = [
        f"# AI-use disclosure: {d['project']}",
        "",
        "This page is one rendering of the disclosure record that Kittum stores with the review "
        "(`ai_disclosure.json`), arranged along the RAISE recommendations for reporting AI use in "
        "evidence synthesis. It is a view of the data, not the standard, and not an endorsement "
        f"by RAISE's authors. It covers the ledger through event {d['cutoff_seq']}.",
        "",
        "## 1. AI tools and versions",
        "",
        md_table(
            ["Judge config", "Model", "Revision", "Weights SHA-256", "Licence", "Served as"],
            [
                [
                    m["judge_config_id"],
                    m["repository"],
                    m["revision"],
                    f"`{m['weights_hash']}`",
                    m["weights_licence"],
                    ", ".join(m["reported_model_identities"]) or m["serving"],
                ]
                for m in d["models"]
            ],
        ),
        "Prompts and output schemas, by hash:",
        "",
        md_table(
            ["Judge config", "Prompt version", "Prompt template SHA-256", "Schema SHA-256"],
            [
                [
                    m["judge_config_id"],
                    m["prompt_version"],
                    f"`{m['prompt_hash']}`",
                    f"`{m['schema_hash']}`",
                ]
                for m in d["models"]
            ],
        ),
        "## 2. Role of the AI and human oversight",
        "",
        "No screening mode lets the AI exclude a record that no human saw. A reviewer decides "
        "before any AI output is shown, and the AI's reading is revealed only as the stage's "
        "reveal setting allows. Adjudication holds only disagreements between humans. When only "
        "the AI disagrees (in M1, the model saw no failing criterion in a record both humans "
        "excluded; in M2, the AI disagrees with the lone human), the stage's auditor screens the "
        "record once more, blind; if the auditor disagrees with the screeners, an adjudicator "
        "decides.",
        "",
        md_table(
            ["Topic", "Mode", "Reason for the mode", "Validation pack"],
            [
                [
                    t["topic_id"],
                    t["mode"],
                    "; ".join((t["mode_rationale"] or {}).values()) or "default (dual human)",
                    t["validation_pack"] or "none",
                ]
                for t in d["topics"]
            ],
        ),
        md_table(
            [
                "Topic",
                "Scope",
                "Stage",
                "Mode",
                "Reveal",
                "Records",
                "AI proposed exclusion",
                "Excluded",
                "Excluded by AI alone",
            ],
            [
                [
                    s["topic_id"],
                    s["scope_id"],
                    s["stage"],
                    s["mode"],
                    s["reveal"],
                    s["records"],
                    s["ai_proposed_exclusions"],
                    s["excluded"],
                    s["excluded_by_ai_alone"],
                ]
                for s in d["stages"]
            ],
        ),
        "## 3. Performance in this review",
        "",
        "Agreement on include against exclude, from the blind decisions made before any reveal "
        "(Cohen's kappa and PABAK):",
        "",
        md_table(
            ["Scope", "Stage", "Pair", "Items", "Kappa", "PABAK"],
            [
                [
                    s["scope_id"],
                    s["stage"],
                    " and ".join(a["raters"]) + _READING.get(a["kind"], ""),
                    a["items"],
                    _fmt(a["kappa"]),
                    _fmt(a["pabak"]),
                ]
                for s in d["stages"]
                for a in s["agreement"]
            ],
        ),
        "Reveals, adjudication, and the audit:",
        "",
        md_table(
            [
                "Scope",
                "Stage",
                "Reveals shown",
                "Revised after reveal",
                "Adjudicated",
                "Sentinels correct",
                "Audit samples decided",
                "Misses found",
                "Elusion (upper bound)",
            ],
            [
                [
                    s["scope_id"],
                    s["stage"],
                    s["reveals_shown"],
                    s["revised_after_reveal"],
                    s["conflicts_resolved"],
                    f"{s['audit']['sentinels_correct']} of {s['audit']['sentinels_decided']}",
                    f"{s['audit']['audit_decided']} of {s['audit']['audit_samples']}",
                    s["audit"]["audit_relevant_found"],
                    _elusion(s["audit"]["elusion"]),
                ]
                for s in d["stages"]
            ],
        ),
        "Extra screens the AI triggered: records only the AI disagreed on, screened once more, "
        "blind, by the stage's auditor. Upheld means the auditor agreed with the screeners and "
        "their decision stands; otherwise an adjudicator decided.",
        "",
        md_table(
            ["Scope", "Stage", "Triggered", "Upheld", "Sent to adjudication", "Pending"],
            [
                [
                    s["scope_id"],
                    s["stage"],
                    s["rescreens"]["triggered"],
                    s["rescreens"]["upheld"],
                    s["rescreens"]["adjudicated"],
                    s["rescreens"]["pending"],
                ]
                for s in d["stages"]
            ],
        ),
        "## 4. Known limitations",
        "",
        "An AI exclusion counts only if its quote is found verbatim in the record and passes a "
        "conservative structural filter. The filter is not a semantic verifier. By design it "
        "cannot catch:",
        "",
        *[f"- {limit}" for limit in d["verifier_limits"]],
        "",
        "## 5. Reproducibility",
        "",
        "Tier: replay. Every AI reading derives from a stored raw output, identified by hash in "
        "the handover bundle; re-running a model is not claimed to reproduce it bit for bit.",
        "",
    ]
    return [json_file("ai_disclosure.json", d), text_file("ai_disclosure.md", "\n".join(lines))]


def _elusion(interval: dict[str, Any] | None) -> str:
    if interval is None:
        return "no estimate"
    return f"{interval.get('estimate')} (≤ {interval.get('upper')})"


CORE_RENDERERS: dict[str, Renderer] = {
    "prisma": render_prisma,
    "ris": render_ris,
    "search_report": render_search_report,
    "ai_disclosure": render_disclosure,
}
