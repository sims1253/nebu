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
from kittum.report.extracted import as_csv, as_json
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
        f"Every search the ledger recorded up to event {review.cutoff_seq}, with the fields "
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
            f"- Run by {r['run_by']} from {r['started_at']} to {r['ended_at']}, ledger event "
            f"{r['event_seq']}",
            f"- Limits in the strategy: {', '.join(r['limits']) or 'None'}",
            f"- Limits in the protocol: {r['protocol_limits'] or 'None'}",
            f"- Study filter: {r['study_filter'] or 'None'}",
            f"- Records the source reported: {r['reported_count']}",
            f"- Records retrieved: {r['retrieved_count']}",
            *(
                [
                    f"- Records Kittum could not read: {len(r['skipped_record_ids'])} "
                    f"({', '.join(r['skipped_record_ids'])})"
                ]
                if r["skipped_record_ids"]
                else []
            ),
            f"- RIS file of the records: `{r['ris_file']}`",
            f"- Raw responses (SHA-256): {', '.join(f'`{h}`' for h in r['raw_artifact_hashes'])}",
            "",
            "The strategy as run:",
            "",
            "```text",
            r["strategy"],
            "```",
            "",
        ]
        if r["query_translation"]:
            lines += ["The platform's translation of the query:", "", "```text"]
            lines += [r["query_translation"], "```", ""]
    if unsearched:
        lines += ["## Planned sources with no recorded search", ""]
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
                    [t.id, dev.source_id, " ".join(dev.justification.values())]
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
                "extra_body": recipe.get("extra_body") or {},
                "reported_model_identities": list(j.model_identities),
                "endpoints": list(j.endpoints),
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
                        "agreed": a.agreed,
                        "table": {
                            "include_include": a.include_include,
                            "include_exclude": a.include_exclude,
                            "exclude_include": a.exclude_include,
                            "exclude_exclude": a.exclude_exclude,
                        },
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
                "audit": s.audit.model_dump(mode="json"),
            }
        )
    assistant = {
        "requests": [
            {
                "purpose": r.get("purpose"),
                "outcome": r.get("outcome"),
                "part": r.get("part"),
                "revision_id": r.get("revision_id"),
                "recipe_id": r.get("recipe_id"),
                "model_identity": r.get("model_identity"),
                "serving": r.get("serving"),
                "endpoint": r.get("endpoint"),
                "prompt_version": r.get("prompt_version"),
                "prompt_hash": r.get("prompt_hash"),
                "attempts": len(r.get("attempts", [])),
                "rejected_attempts": sum(1 for a in r.get("attempts", []) if a.get("issues")),
                "raw_response_hashes": [
                    a["raw_response_hash"]
                    for a in r.get("attempts", [])
                    if a.get("raw_response_hash")
                ],
            }
            for r in review.assist_requests
        ],
        "ai_parts": [dict(p) for p in review.protocol_ai_parts],
    }
    return {
        "project": review.project_name,
        "cutoff_seq": review.cutoff_seq,
        "protocol_hash": review.protocol.version.content_hash if review.protocol else None,
        "protocol_assistant": assistant,
        "models": models,
        "prompts": [card.model_dump(mode="json") for card in review.prompts],
        "topics": topics,
        "stages": stages,
        "verifier_limits": list(VERIFIER_LIMITS),
        "reproducibility_tier": "replay",
    }


STAGE_NAMES = {"title_abstract": "Title and abstract", "full_text": "Full text"}

AI_READING = {
    "human_human": "",
    "human_ai": "the AI's own reading",
    "human_ai_verified": "the AI's checked reading",
}
"""How the AI's side of a pair is named: the model's reading before Kittum checks its quotes,
or after."""


def _number(value: float | None) -> str:
    return "not defined" if value is None else f"{value:.2f}"


def rater_names(agreement: dict[str, Any]) -> tuple[str, str]:
    first, second = agreement["raters"]
    return first, AI_READING[agreement["kind"]] or second


def agreement_sentence(agreement: dict[str, Any]) -> str:
    """ "Reviewer 1 and the AI's own reading agreed on 18 of 33 records. ..." """
    first, second = rater_names(agreement)
    n, agreed = agreement["items"], agreement["agreed"]
    if n == 0:
        return f"{first} and {second} have no record in common yet."
    share = round(100 * agreed / n)
    return (
        f"{first} and {second} agreed on {agreed} of {n} records ({share}%). "
        f"Cohen's kappa is {_number(agreement['kappa'])} and PABAK is "
        f"{_number(agreement['pabak'])}. Both cover all {n} records, agreed or not."
    )


def agreement_table(agreement: dict[str, Any]) -> str:
    first, second = rater_names(agreement)
    t = agreement["table"]
    return md_table(
        ["", f"{second}: include", f"{second}: exclude"],
        [
            [f"{first}: include", t["include_include"], t["include_exclude"]],
            [f"{first}: exclude", t["exclude_include"], t["exclude_exclude"]],
        ],
    )


def _model(model: dict[str, Any]) -> str:
    served = ", ".join(model["reported_model_identities"]) or model["serving"]
    if model["endpoints"]:
        served += f" ({model['serving']} at {', '.join(model['endpoints'])})"
    return served


def _assistant_model(entry: dict[str, Any]) -> str:
    served = entry.get("model_identity") or entry.get("recipe_id") or "not recorded"
    if entry.get("endpoint"):
        served += f" ({entry.get('serving')} at {entry['endpoint']})"
    return f"{entry.get('recipe_id')}: {served}"


def _fenced(text: str, language: str = "text") -> list[str]:
    """A code block that the text cannot close, however many backticks it holds."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", text, fence, ""]


ROLE_NAMES = {"judge": "AI judge", "assistant": "Protocol assistant", "extraction": "Extraction"}


def prompt_section(card: dict[str, Any]) -> list[str]:
    """One AI role's prompts and model recipe, as section 6 of the disclosure shows them."""
    model = card["model"]
    lines = [f"### {ROLE_NAMES.get(card['role'], card['role'])}: {card['label']}", ""]
    facts = [
        ("Recipe", card["recipe_id"]),
        ("Model", model.get("repository") or model.get("model_as_sent")),
        ("Revision", model.get("revision") or "Not recorded for this role"),
        ("Weights SHA-256", model.get("weights_hash") and f"`{model['weights_hash']}`"),
        ("Licence", model.get("weights_licence")),
        ("Serving", model.get("serving")),
        ("Endpoint", ", ".join(model.get("endpoints") or []) or None),
        (
            "Model as reported by the server",
            ", ".join(model.get("reported_model_identities") or []),
        ),
        ("Prompt version", card["prompt_version"]),
        ("Prompt template SHA-256", card["prompt_hash"] and f"`{card['prompt_hash']}`"),
        ("Calls through this cutoff", card["calls"]),
    ]
    lines += [f"- {label}: {value}" for label, value in facts if value not in (None, "")]
    lines.append("")
    if card["request_sha256"] is None:
        lines += [
            "Kittum has no stored request for this role, so it cannot show the prompts.",
            "",
        ]
        return lines
    where = (
        "the request body sent to the model server"
        if card["request_form"] == "http_body"
        else "the request as Kittum built it. The backend stored no request body"
    )
    lines += [f"Read from {where}, SHA-256 `{card['request_sha256']}`.", ""]
    settings = dict(card["request_settings"])
    extra = model.get("extra_body") or {}
    if extra:
        lines += ["Server options from the recipe (`extra_body`):", ""]
        lines += _fenced(json.dumps(extra, indent=2, sort_keys=True), "json")
    if settings:
        lines += ["Request settings as sent:", ""]
        lines += _fenced(json.dumps(settings, indent=2, sort_keys=True), "json")
    if card["system_prompt"] is not None:
        lines += ["#### System prompt", "", *_fenced(card["system_prompt"])]
    if card["user_prompt_template"] is not None:
        lines += [
            "#### User prompt template",
            "",
            "Kittum fills in the parts in braces for each record.",
            "",
            *_fenced(card["user_prompt_template"]),
        ]
    if card["first_user_prompt"] is not None:
        lines += ["#### User prompt of the first call, as sent", ""]
        lines += _fenced(card["first_user_prompt"])
    if card["json_schema"] is not None:
        lines += ["#### JSON schema the answer must follow", ""]
        lines += _fenced(json.dumps(card["json_schema"], indent=2), "json")
    return lines


def render_disclosure(review: Review, scope_id: str | None) -> list[RenderedFile]:
    del scope_id
    d = disclosure(review)
    stages = d["stages"]
    lines = [
        f"# AI-use disclosure: {d['project']}",
        "",
        "Kittum wrote this page from the disclosure record it keeps with the review "
        "(`ai_disclosure.json`). The sections follow the RAISE recommendations for reporting AI "
        "use in evidence synthesis. The authors of RAISE have not reviewed this page. It covers "
        f"the ledger up to event {d['cutoff_seq']}.",
        "",
        "## 1. AI tools and versions",
        "",
        md_table(
            ["Judge configuration", "Model", "Revision", "Weights SHA-256", "Licence", "Served by"],
            [
                [
                    m["judge_config_id"],
                    m["repository"],
                    m["revision"],
                    f"`{m['weights_hash']}`",
                    m["weights_licence"],
                    _model(m),
                ]
                for m in d["models"]
            ],
        ),
        "Section 6 shows every prompt in full.",
        "",
        "## 2. What the AI did and what people decided",
        "",
        "People screen every record. The AI never excludes a record. Each reviewer decides "
        "before Kittum shows them any AI output. The stage's reveal setting says whether Kittum "
        "then shows the AI's reading. When two people disagree, an adjudicator decides. When "
        "only the AI disagrees, the stage's auditor screens the record once more, without "
        "seeing the other decisions. In mode M1 that happens when both people excluded a record "
        "and the AI found no criterion that fails. In mode M2 it happens when the AI disagrees "
        "with the one person who screened. If the auditor disagrees with the screeners, an "
        "adjudicator decides.",
        "",
        md_table(
            ["Topic", "Mode", "Why this mode", "Validation pack"],
            [
                [
                    t["topic_id"],
                    t["mode"],
                    " ".join((t["mode_rationale"] or {}).values())
                    or "Two people screen every record (the default).",
                    t["validation_pack"] or "None",
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
                "Read by the AI",
                "AI proposed to exclude",
                "Excluded by people",
            ],
            [
                [
                    s["topic_id"],
                    s["scope_id"],
                    STAGE_NAMES.get(s["stage"], s["stage"]),
                    s["mode"],
                    s["reveal"].replace("_", " "),
                    s["records"],
                    s["records_read_by_ai"],
                    s["ai_proposed_exclusions"],
                    s["excluded"],
                ]
                for s in stages
            ],
        ),
        "### Writing the protocol",
        "",
        "A model can draft the protocol or suggest changes to one part of it. A lead accepts or "
        "rejects each change, and only a lead can publish. Kittum checks each answer and sends "
        "the problems back to the model. Every request to the assistant:",
        "",
        md_table(
            ["Purpose", "Part", "Outcome", "Model", "Attempts", "Sent back", "Prompt"],
            [
                [
                    r["purpose"],
                    f"`{r['part']}`" if r["part"] else "Whole protocol",
                    r["outcome"],
                    _assistant_model(r),
                    r["attempts"],
                    r["rejected_attempts"],
                    f"{r['prompt_version']} `{(r['prompt_hash'] or '')[:16]}`",
                ]
                for r in d["protocol_assistant"]["requests"]
            ],
        )
        if d["protocol_assistant"]["requests"]
        else "Nobody used the protocol assistant.\n",
        "Fields of the published protocol that a model wrote:",
        "",
        md_table(
            ["Field", "Written in revision", "Accepted in revision", "Model"],
            [
                [
                    f"`{p['path']}`",
                    p["revision_id"],
                    p["accepted_in"] or "At publication",
                    _assistant_model(p),
                ]
                for p in d["protocol_assistant"]["ai_parts"]
            ],
        )
        if d["protocol_assistant"]["ai_parts"]
        else "People wrote every field of the published protocol.\n",
        "## 3. Agreement in this review",
        "",
        "Kittum compares the decisions each person made before any AI output was shown. A "
        "decision counts as include or exclude. Unclear and insufficient information count as "
        "include. The AI's own reading excludes a record when the model answered not met on any "
        "criterion. The AI's checked reading excludes only when Kittum also found the model's "
        "quote in the record. Cohen's kappa and PABAK cover every record both sides decided, "
        "not only the records where they disagreed.",
        "",
    ]
    for s in stages:
        for a in s["agreement"]:
            first, second = rater_names(a)
            lines += [
                f"### {STAGE_NAMES.get(s['stage'], s['stage'])}, scope {s['scope_id']}: "
                f"{first} and {second}",
                "",
                agreement_sentence(a),
                "",
                agreement_table(a),
            ]
    lines += [
        "## 4. Reveals, adjudication, and the audit",
        "",
        md_table(
            [
                "Scope",
                "Stage",
                "AI readings shown",
                "Decisions changed after one",
                "Conflicts adjudicated",
                "Sentinels screened right",
                "Audit samples screened",
                "Relevant records the audit found",
                "Missed relevant records, estimate (upper bound)",
            ],
            [
                [
                    s["scope_id"],
                    STAGE_NAMES.get(s["stage"], s["stage"]),
                    s["reveals_shown"],
                    s["revised_after_reveal"],
                    s["conflicts_resolved"],
                    f"{s['audit']['sentinels_correct']} of {s['audit']['sentinels_decided']}",
                    f"{s['audit']['audit_decided']} of {s['audit']['audit_samples']}",
                    s["audit"]["audit_relevant_found"],
                    _elusion(s["audit"]["elusion"]),
                ]
                for s in stages
            ],
        ),
        "Extra screens when only the AI disagreed. The auditor screens the record again. Upheld "
        "means the auditor agreed with the screeners, so their decision stands. Otherwise an "
        "adjudicator decided.",
        "",
        md_table(
            ["Scope", "Stage", "Sent", "Upheld", "Sent to adjudication", "Waiting"],
            [
                [
                    s["scope_id"],
                    STAGE_NAMES.get(s["stage"], s["stage"]),
                    s["rescreens"]["triggered"],
                    s["rescreens"]["upheld"],
                    s["rescreens"]["adjudicated"],
                    s["rescreens"]["pending"],
                ]
                for s in stages
            ],
        ),
        "## 5. Known limits and reproducibility",
        "",
        "The AI's reading proposes an exclusion only if Kittum finds the model's quote word for "
        "word in the record and the quote passes a structural filter. The filter does not "
        "check meaning. It cannot catch these cases:",
        "",
        *[f"- {limit}" for limit in d["verifier_limits"]],
        "",
        "Reproducibility tier: replay. Kittum stores the exact request and response of every "
        "model call, and the handover bundle lists each by SHA-256. Running a model again may "
        "give a different answer.",
        "",
        "## 6. Prompts",
        "",
        "Each AI role this review used, with the prompts read from the first request it sent. "
        "The Audit screen shows the exact request and response of every call.",
        "",
    ]
    if not d["prompts"]:
        lines += ["No AI role has sent a request yet.", ""]
    for card in d["prompts"]:
        lines += prompt_section(card)
    return [json_file("ai_disclosure.json", d), text_file("ai_disclosure.md", "\n".join(lines))]


def _elusion(interval: dict[str, Any] | None) -> str:
    if interval is None:
        return "No estimate yet"
    return f"{interval.get('estimate')} (at most {interval.get('upper')})"


# The accepted extraction values.


def render_extraction(review: Review, scope_id: str | None) -> list[RenderedFile]:
    del scope_id  # values belong to studies, which the scopes share
    return [
        RenderedFile(
            path="extracted_values.csv",
            media_type="text/csv; charset=utf-8",
            data=as_csv(review.extracted),
        ),
        json_file("extracted_values.json", as_json(review.extracted, review.cutoff_seq)),
    ]


CORE_RENDERERS: dict[str, Renderer] = {
    "prisma": render_prisma,
    "ris": render_ris,
    "search_report": render_search_report,
    "ai_disclosure": render_disclosure,
    "extraction": render_extraction,
}
