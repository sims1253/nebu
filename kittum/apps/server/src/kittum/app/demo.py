"""`kittum demo seed`: a complete review project to explore, demo, and test end to end.

The nicotine replacement example protocol (no profile), three searches replayed from recordings
(PubMed, an example CENTRAL export, and a citation search), the corpus, two CC BY trial PDFs as
full texts, and an open title and abstract stage with the AI judge's readings. Nothing calls the
network. The judge runs on a live model server (`--live`) or replays recorded answers
(`DemoBackend`), which the end-to-end test uses so that it is deterministic.

The recorded answers in `demo_data/judge_answers.json` are what `qwen3.5:4b` on Ollama answered
for each demo record (`scripts/record_demo_answers.py`), keyed by record title.
"""

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kittum.app.context import Judge, judge_config
from kittum.app.lifecycle import (
    add_member,
    attach_full_text,
    attach_sentinel_full_text,
    corpus_records,
    create_project,
    ingest_corpus,
    open_screening,
    publish_protocol,
)
from kittum.core import Id, Record, sha256_hex
from kittum.corpus import confirm
from kittum.gateway import HttpRequest, HttpResponse
from kittum.models import Completion, CompletionRequest, request_key
from kittum.runs import PostgresJobQueue, Worker
from kittum.screening import JUDGE_JOB, RecordFacts, ScreeningRecord, Sentinel, judge_handler
from kittum.search import PubMedSource, SearchPlan, import_file, run

if TYPE_CHECKING:
    from kittum.app.context import AppContext
    from kittum.app.lifecycle import MemberRole
    from kittum.models import ModelRegistry

DATA = Path(__file__).parent / "demo_data"
SERVER = Path(__file__).parents[3]
PROTOCOL = SERVER.parents[1] / "examples" / "protocols" / "nicotine-replacement.yaml"
PDFS = SERVER / "tests" / "documents" / "fixtures"

PUBMED_STRATEGY = (
    '("nicotine replacement"[tiab] OR "nicotine patch"[tiab] OR "nicotine gum"[tiab])'
    ' AND "smoking cessation"[tiab] AND randomized controlled trial[pt]'
    " AND 2024/01/01:2024/06/30[dp]"
)
TOPIC = "TRIALS"
SCOPE = "nrt-vs-control"
RECORDED_MODEL = "qwen3.5-4b-ollama"  # the model the recorded answers come from

PEOPLE: tuple[tuple[str, tuple[MemberRole, ...]], ...] = (
    ("Sam Okafor", ("reviewer",)),
    ("Jun Park", ("reviewer",)),
    ("Rita Nunes", ("auditor",)),
    ("Elena Weiss", ("adjudicator",)),
)
LEAD = "Morgan Lee"

# Full texts for records found by the citation search, by DOI (CC BY 4.0).
FULL_TEXTS = {
    "10.1186/s12916-026-04943-4": "gerber2026-lesotho-t2d-crct.pdf",
    "10.1186/s12916-026-05037-x": "li2026-vitamin-c-kidney-biopsy-rct.pdf",
}
# A full text with a known outcome, seeded into full-text queues: no smokers, no NRT.
FULL_TEXT_SENTINEL = (
    "Effect of Vitamin C on bleeding and pain after kidney biopsy: a randomized controlled trial",
    "li2026-vitamin-c-kidney-biopsy-rct.pdf",
)

SENTINELS = (
    Sentinel(
        record=ScreeningRecord(
            item_ref="sentinel:nrt-lozenge",
            title="Nicotine lozenge for smoking cessation: a randomised placebo-controlled trial",
            text="METHODS: 1,818 adult smokers were randomised to 2 mg or 4 mg nicotine lozenges or "
            "matching placebo for 12 weeks. RESULTS: Abstinence at 6 months was 17.9% with active "
            "lozenges and 8.6% with placebo.",
            facts=RecordFacts(year=2002, journal="Archives of Internal Medicine"),
        )
    ),
    Sentinel(
        record=ScreeningRecord(
            item_ref="sentinel:nrt-spray",
            title="Nicotine mouth spray for smoking cessation: a randomized trial",
            text="METHODS: We randomized 479 adults who smoked to nicotine mouth spray or placebo "
            "spray. RESULTS: Sustained abstinence at 24 weeks was 15.9% versus 6.9%.",
            facts=RecordFacts(year=2012, journal="European Respiratory Journal"),
        )
    ),
)


class ReplayGateway:
    """Answers PubMed's ESearch and EFetch with the recorded replies, whatever the session."""

    def fetch(self, source: str, request: HttpRequest, *, use_cache: bool = True) -> HttpResponse:  # noqa: ARG002
        name = "pubmed_esearch.xml" if "esearch.fcgi" in request.url else "pubmed_efetch.xml"
        body = (DATA / name).read_bytes()
        return HttpResponse(status=200, headers=(), body=body, body_hash=sha256_hex(body))


_TITLE = re.compile(r"^Title: (.*)$", re.MULTILINE)


class DemoBackend:
    """Replays the recorded answer for a record, found by its title in the request.

    A record without a recording gets `unclear` on every criterion, as a model that cannot judge
    it would answer.
    """

    model_identity = f"recorded:{RECORDED_MODEL}"

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        if answers is None:
            path = DATA / "judge_answers.json"
            answers = json.loads(path.read_text()) if path.exists() else {}
        self._answers = answers

    def complete(self, request: CompletionRequest) -> Completion:
        user = request.messages[-1].content
        found = _TITLE.search(user)
        title = found.group(1).strip() if found else ""
        stage = "full_text" if "Stage: full text" in user else "title_abstract"
        text = self._answers.get(f"{stage}|{title}")
        if text is None:
            criteria = list((request.json_schema or {}).get("properties", {}))
            text = json.dumps(
                {
                    c: {"quote": "", "rationale": "The record does not say.", "status": "unclear"}
                    for c in criteria
                }
            )
        return Completion(
            text=text,
            model_identity=self.model_identity,
            request_hash=request_key(request),
            raw_response_hash=sha256_hex(text.encode()),
        )


def demo_judge(registry: ModelRegistry) -> Judge:
    recipe = registry.get(RECORDED_MODEL)
    return Judge(
        config=judge_config(RECORDED_MODEL, label="demo-recorded"),
        recipe=recipe,
        backend=DemoBackend(),
    )


class DemoProject(Record):
    project_id: Id
    stage_id: Id
    people: dict[str, Id]
    records: int
    judge_config_id: str


def seed_demo(ctx: AppContext, *, name: str = "Nicotine replacement review (demo)") -> DemoProject:
    """Build the demo project and open title and abstract screening; the judge jobs are queued
    (`run_judge_jobs` or `kittum worker` runs them)."""
    ledger = ctx.require_ledger()
    with ctx.db.transaction() as tx:
        project_id, lead = create_project(tx, name=name, lead_name=LEAD, ledger=ledger)
        people = {LEAD: lead}
        for display_name, roles in PEOPLE:
            member = add_member(
                tx,
                project_id,
                display_name=display_name,
                roles=roles,
                added_by=lead,
                ledger=ledger,
            )
            people[display_name] = member.actor_id
        publish_protocol(
            tx,
            project_id,
            PROTOCOL.read_text(),
            approved_by=lead,
            ledger=ledger,
            reason="Demo protocol, approved by the methods lead.",
        )
        _searches(ctx, tx, project_id, lead)
        corpus = ingest_corpus(tx, project_id, actor_id=lead, ledger=ledger)
        # The lead confirms duplicates on an exact identifier before screening; study links stay
        # for the Studies screen.
        revision = corpus.identity_revision_id
        for proposal in corpus.proposals:
            exact = any(e.startswith("exact ") for e in proposal.evidence)
            if proposal.relation == "same_record" and exact and revision is not None:
                revision = confirm(tx, project_id, revision, proposal.id, lead, ledger=ledger).id
        records = corpus_records(tx, project_id)
        for record in records:
            pdf = FULL_TEXTS.get(record.facts.doi or "")
            if pdf:
                attach_full_text(
                    tx,
                    project_id,
                    record.item_ref,
                    (PDFS / pdf).read_bytes(),
                    actor_id=lead,
                    artifacts=ctx.artifacts,
                    ledger=ledger,
                    licence="CC BY 4.0",
                )
        title, pdf = FULL_TEXT_SENTINEL
        attach_sentinel_full_text(
            tx,
            project_id,
            title,
            (PDFS / pdf).read_bytes(),
            "exclude",
            actor_id=lead,
            artifacts=ctx.artifacts,
            ledger=ledger,
            licence="CC BY 4.0",
        )
        info = open_screening(
            tx, ctx, project_id, SCOPE, "title_abstract", actor_id=lead, sentinels=SENTINELS
        )
    assert ctx.judge is not None
    return DemoProject(
        project_id=project_id,
        stage_id=info.stage_id,
        people=people,
        records=len(records),
        judge_config_id=ctx.judge.config.id,
    )


def _searches(ctx: AppContext, tx: Any, project_id: Id, lead: Id) -> None:
    ledger = ctx.require_ledger()
    pubmed = PubMedSource(ReplayGateway(), ctx.artifacts, tool="kittum", clock=ctx.clock)
    run(
        tx,
        project_id,
        SearchPlan(
            topic_id=TOPIC,
            source="pubmed",
            platform="PubMed",
            strategy=PUBMED_STRATEGY,
            limits=("Publication date 1 January to 30 June 2024",),
        ),
        pubmed,
        ledger=ledger,
        actor_id=lead,
        artifacts=ctx.artifacts,
    )
    for plan, file in (
        (
            SearchPlan(
                topic_id=TOPIC,
                source="central",
                platform="Cochrane Library (Wiley)",
                strategy="#1 (nicotine NEAR/3 (replacement OR patch* OR gum OR lozenge*)):ti,ab,kw\n"
                "#2 (smok* NEAR/3 (cessation OR quit*)):ti,ab,kw\n#3 #1 AND #2",
            ),
            "central_export.ris",
        ),
        (
            SearchPlan(
                topic_id=TOPIC,
                source="citation_search",
                platform="Reference lists of included studies",
                strategy="Backward citation search of the included trials' reference lists",
            ),
            "citation_search.ris",
        ),
    ):
        import_file(
            tx,
            project_id,
            plan,
            (DATA / file).read_bytes(),
            "ris",
            ledger=ledger,
            actor_id=lead,
            artifacts=ctx.artifacts,
            clock=ctx.clock,
        )


def run_judge_jobs(ctx: AppContext, *, progress: Any = None) -> int:
    """Run judge jobs in this process until none is due. Returns how many ran."""
    judge = ctx.judge
    if judge is None:
        raise ValueError("no judge is configured")
    with ctx.db.transaction() as tx:
        actor_id = judge.actor(tx)
    runner = judge.runner(
        actor_id=actor_id, ledger=ctx.require_ledger(), store=ctx.artifacts, clock=ctx.clock
    )
    handler = judge_handler(ctx.db, ctx.screening(), runner, actor_id=actor_id)
    worker = Worker(ctx.db, PostgresJobQueue(ctx.clock), {JUDGE_JOB: handler}, name="demo-seed")
    ran = 0
    while worker.run_once():
        ran += 1
        if progress is not None:
            progress(ran)
    return ran


__all__ = [
    "PUBMED_STRATEGY",
    "SCOPE",
    "SENTINELS",
    "DemoBackend",
    "DemoProject",
    "ReplayGateway",
    "demo_judge",
    "run_judge_jobs",
    "seed_demo",
]
