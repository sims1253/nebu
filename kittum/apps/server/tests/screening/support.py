"""A realistic screening world: the nicotine replacement example protocol, a recorded PubMed search
and a CENTRAL export, the corpus, and the AI judge on recorded completions.

The PubMed search is the recorded lumacaftor search of the search tests, so its records are
off-topic for this review, as a broad search's often are. The judge's answers are what a model
says about them; the judge's filter decides what stands. On these records it never confirms an
exclusion (no quote negates the population criterion), so the AI includes them, and M1 sends
records both humans exclude to adjudication. One CENTRAL record states that its patients did not
smoke, and that exclusion is verified.
"""

import functools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from kittum.core import SystemClock, new_id
from kittum.corpus import ingest
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.judge import (
    PROMPT_VERSION,
    JudgeConfig,
    ScreeningItem,
    applicable,
    build_request,
    judge_many,
)
from kittum.ledger import PostgresLedger
from kittum.models import ModelRegistry
from kittum.models.testing import RecordedBackend
from kittum.protocol import compile, publish, save_draft
from kittum.runs import PostgresJobQueue, Worker
from kittum.screening import (
    JUDGE_JOB,
    DecisionTiming,
    QueueItem,
    RecordFacts,
    Screening,
    ScreeningRecord,
    Sentinel,
    StageInfo,
    StageSpec,
    criteria_for,
    judge_handler,
    topic_for,
)
from kittum.search import PubMedSource, SearchPlan, SourceRecord, import_file, run
from kittum.search.testing import FakeSource
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database, Tx
    from kittum.protocol import Criterion, Stage, StoredProtocol
    from kittum.search import SearchExecution

HERE = Path(__file__).parent
EXAMPLE = HERE.parents[3] / "examples" / "protocols" / "nicotine-replacement.yaml"
SEARCH_FIXTURES = HERE.parent / "search" / "fixtures"
SCOPE = "nrt-vs-control"
TOPIC = "TRIALS"
STAGE = "title_abstract"
CONFIG = JudgeConfig(
    id="nrt-ta-qwen-v1", model_id="qwen3.8-27b-bf16", prompt_version=PROMPT_VERSION
)
RECIPE = ModelRegistry.load().get(CONFIG.model_id)
START = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
JUDGE_MARK = "(judge)"  # every recorded rationale ends with it, so tests can find AI output


def protocol_source(mode: str = "M1") -> dict[str, Any]:
    """The example protocol, with the topic switched to M2 or M3 when asked (a new version with
    a rationale, as a mode change needs)."""
    source = yaml.safe_load(EXAMPLE.read_text())
    topic = source["search_topics"][0]
    if mode != "M1":
        topic["mode"] = mode
        topic["mode_rationale"] = {"en": f"Screened in {mode} for the screening tests."}
        topic["audit"] = {**topic["audit"], "random_audit_percent": "10"}
    if mode == "M2":
        topic["validation_pack"] = "kittum-validation-pack/2026-10"
    if mode == "M3":
        topic["audit"]["random_audit_percent"] = "2"
        topic["stopping"] = {
            "method": "buscar",
            "target_recall_percent": "80",
            "confidence_percent": "95",
            "minimum_screened_percent": "50",
        }
    return source


@dataclass
class People:
    project_id: Id
    lead: Id
    a: Id
    b: Id
    adjudicator: Id
    auditor: Id
    model: Id


def seed_people(tx: Tx) -> People:
    project = tx.execute(
        "insert into project (name, policy_version) values ('NRT review', 'p1') returning id"
    ).fetchone()
    assert project is not None

    def actor(ref: str, kind: str) -> Id:
        row = tx.execute(
            "insert into actor (identity_ref, kind) values (%s, %s) returning id",
            (f"{ref}:{new_id()}", kind),
        ).fetchone()
        assert row is not None
        return row["id"]

    return People(
        project_id=project["id"],
        lead=actor("lead@example.org", "human"),
        a=actor("reviewer-a@example.org", "human"),
        b=actor("reviewer-b@example.org", "human"),
        adjudicator=actor("adjudicator@example.org", "human"),
        auditor=actor("auditor@example.org", "human"),
        model=actor("model:qwen3.8-27b-bf16", "model"),
    )


def publish_protocol(tx: Tx, people: People, ledger: PostgresLedger, mode: str) -> StoredProtocol:
    draft = save_draft(tx, people.project_id, compile(protocol_source(mode)))
    return publish(tx, people.project_id, draft.id, people.lead, ledger=ledger)


def pubmed_search(
    tx: Tx, people: People, ledger: PostgresLedger, artifacts: MemoryStore
) -> SearchExecution:
    """The recorded PubMed search of the search tests, replayed through the gateway fake."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    transport = FakeTransport(
        {
            f"{base}esearch.fcgi?db=pubmed&term=lumacaftor%5Bti%5D%20AND%20trial%5Btiab%5D"
            "&usehistory=y&retmode=xml&tool=kittum": (
                SEARCH_FIXTURES / "pubmed_esearch.xml"
            ).read_bytes(),
            f"{base}efetch.fcgi?db=pubmed&query_key=1&WebEnv=MCID_6ae14800045d3e9e3004e3fd"
            "&retstart=0&retmax=24&retmode=xml&tool=kittum": (
                SEARCH_FIXTURES / "pubmed_efetch.xml"
            ).read_bytes(),
        }
    )
    source = PubMedSource(PassThroughGateway(transport), artifacts, tool="kittum")
    plan = SearchPlan(
        topic_id=TOPIC,
        source="pubmed",
        platform="PubMed",
        strategy="lumacaftor[ti] AND trial[tiab]",
    )
    return run(
        tx,
        people.project_id,
        plan,
        source,
        ledger=ledger,
        actor_id=people.lead,
        artifacts=artifacts,
    )


def central_import(
    tx: Tx, people: People, ledger: PostgresLedger, artifacts: MemoryStore
) -> SearchExecution:
    plan = SearchPlan(
        topic_id=TOPIC,
        source="central",
        platform="Cochrane Library (Wiley)",
        strategy="(nicotine NEAR/3 replacement):ti,ab,kw",
    )
    return import_file(
        tx,
        people.project_id,
        plan,
        (HERE / "fixtures" / "nrt_central.ris").read_bytes(),
        "central",
        ledger=ledger,
        actor_id=people.lead,
        artifacts=artifacts,
    )


def screening_records(
    tx: Tx, people: People, executions: list[SearchExecution], ledger: PostgresLedger
) -> list[ScreeningRecord]:
    """One screening record per report of the corpus's first identity revision."""
    revision = ingest(tx, people.project_id, executions, actor_id=people.lead, ledger=ledger)
    links = tx.execute(
        "select occurrence_id, report_id from identity_link where revision_id = %s",
        (revision.identity_revision_id,),
    ).fetchall()
    occurrences = {r.occurrence_id: r for e in executions for r in e.records}
    by_report: dict[Id, list[SourceRecord]] = {}
    for link in links:
        by_report.setdefault(link["report_id"], []).append(occurrences[link["occurrence_id"]])
    records = []
    for report_id, recs in by_report.items():
        best = max(recs, key=lambda r: len(r.abstract or ""))
        records.append(
            ScreeningRecord(
                item_ref=str(report_id),
                title=best.title,
                text=best.abstract or "",
                facts=RecordFacts(
                    pmid=best.pmid,
                    doi=best.doi,
                    journal=best.journal,
                    year=best.year,
                    publication_types=best.publication_types,
                    registry_ids=best.registry_ids,
                ),
            )
        )
    return sorted(records, key=lambda r: r.title)


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


def answer(**criteria: tuple[str, str, str]) -> str:
    return json.dumps(
        {
            cid: {"quote": quote, "rationale": f"{rationale} {JUDGE_MARK}", "status": status}
            for cid, (status, quote, rationale) in criteria.items()
        }
    )


UNCLEAR = answer(
    POP=("unclear", "", "The population is not described."),
    INT=("unclear", "", "No intervention is named."),
    DESIGN=("unclear", "", "The design is not stated."),
)

# What the model answers for the CENTRAL records, by title prefix.
CENTRAL_ANSWERS = {
    "Nicotine patch versus placebo": answer(
        POP=("met", "adults aged 18 years or older who smoked", "Adult smokers."),
        INT=("met", "16-hour nicotine patch", "One arm receives the nicotine patch."),
        DESIGN=("met", "double-blind, randomised, placebo-controlled trial", "An RCT."),
    ),
    "Nicotine gum for smoking": answer(
        POP=("met", "adult inpatients who smoked tobacco daily", "Adult smokers."),
        INT=("met", "4 mg nicotine gum", "One arm receives nicotine gum."),
        DESIGN=("met", "We randomized 280 adult inpatients", "A randomised trial."),
    ),
    "Combination nicotine replacement": answer(
        POP=("met", "adult smokers who wanted to quit", "Adult smokers."),
        INT=("met", "nicotine patch plus a 2 mg nicotine lozenge", "NRT in both arms."),
        DESIGN=("met", "open-label randomised controlled trial", "An RCT."),
    ),
    "Transdermal nicotine for active ulcerative colitis": answer(
        POP=("not_met", "did not smoke tobacco", "The patients were non-smokers."),
        INT=("met", "transdermal nicotine patches", "One arm receives nicotine patches."),
        DESIGN=("met", "randomly assigned", "A randomised trial."),
    ),
    "Varenicline versus placebo": answer(
        POP=("met", "adult smokers with stable depression", "Adult smokers."),
        INT=("not_met", "varenicline 1 mg twice daily or placebo", "No arm receives NRT."),
        DESIGN=("met", "randomized, double-blind trial", "An RCT."),
    ),
    "Use of nicotine replacement therapy": answer(
        POP=("met", "adult smokers who made a quit attempt", "Adult smokers."),
        INT=("met", "nicotine replacement therapy bought over the counter", "NRT use."),
        DESIGN=("not_met", "a prospective cohort study", "A cohort study, not a trial."),
    ),
}


def ai_answer(record: ScreeningRecord) -> str:
    """The recorded completion for a record."""
    for prefix, text in CENTRAL_ANSWERS.items():
        if record.title.startswith(prefix):
            return text
    source = f"{record.title}\n\n{record.text}"
    design = next(
        (
            q
            for q in ("Randomized Controlled Trial", "randomised, placebo-controlled", "randomized")
            if q in source
        ),
        None,
    )
    return answer(
        POP=(
            "not_met",
            "cystic fibrosis",
            "The patients have cystic fibrosis; smoking is not studied.",
        ),
        INT=("unclear", "", "No nicotine replacement is mentioned."),
        DESIGN=("met", design, "The study is randomised.")
        if design
        else ("unclear", "", "No design is stated."),
    )


def unclear_answer(criteria: tuple[Criterion, ...]) -> str:
    return answer(**{c.id: ("unclear", "", "The record does not say.") for c in criteria})


def recorded_backend(
    records: list[ScreeningRecord],
    criteria: tuple[Criterion, ...],
    *,
    stage: Stage = STAGE,
    answers: dict[str, str] | None = None,
) -> RecordedBackend:
    """Recorded completions keyed by the exact request the judge sends for each record."""
    table = {}
    for r in records:
        item = ScreeningItem(item_ref=r.item_ref, stage=stage, title=r.title, text=r.text)
        request = build_request(item, applicable(criteria, SCOPE, item), CONFIG)
        found = (answers or {}).get(r.item_ref)
        table[RecordedBackend.key(request)] = found or (
            ai_answer(r) if stage == STAGE else unclear_answer(criteria)
        )
    return RecordedBackend(
        table, default=unclear_answer(criteria), model_identity="Qwen/Qwen3.8-27B"
    )


# Simulated records for M3 and the audit, through a fake source and the corpus like any search.

RELEVANT = "Nicotine patch for smoking cessation, trial"
NON_SMOKERS = "Transdermal nicotine in ulcerative colitis, trial"
OFF_TOPIC = "Exercise counselling after cardiac surgery, study"


def simulated(kind: str, i: int) -> SourceRecord:
    title, text = {
        RELEVANT: (
            f"{RELEVANT} {i}: a randomised controlled trial in adult smokers",
            f"METHODS: We randomised {200 + i} adults who smoked tobacco to nicotine patches or "
            "placebo patches for 12 weeks. RESULTS: Validated abstinence at 6 months was higher "
            "with nicotine patches.",
        ),
        NON_SMOKERS: (
            f"{NON_SMOKERS} {i}",
            f"METHODS: We enrolled {40 + i} adults with ulcerative colitis who did not smoke "
            "tobacco. They were randomly assigned to nicotine or placebo patches. RESULTS: "
            "Remission rates did not differ.",
        ),
        OFF_TOPIC: (
            f"{OFF_TOPIC} {i}",
            f"METHODS: {100 + i} patients were followed after cardiac surgery. RESULTS: Exercise "
            "capacity improved.",
        ),
    }[kind]
    return SourceRecord(
        occurrence_id=new_id(),
        source="simulated",
        provider_record_id=f"{kind[:10]}-{i}",
        ordinal=0,
        title=title,
        abstract=text,
        year=2020,
        raw_hash="0" * 64,
    )


SIMULATED_ANSWERS = {
    RELEVANT: answer(
        POP=("met", "adults who smoked tobacco", "Adult smokers."),
        INT=("met", "nicotine patches", "One arm receives nicotine patches."),
        DESIGN=("met", "We randomised", "A randomised trial."),
    ),
    NON_SMOKERS: answer(
        POP=("not_met", "did not smoke tobacco", "The patients did not smoke."),
        INT=("met", "nicotine or placebo patches", "One arm receives nicotine patches."),
        DESIGN=("met", "randomly assigned", "A randomised trial."),
    ),
}


def simulated_records(
    tx: Tx, people: People, ledger: PostgresLedger, artifacts: MemoryStore, counts: dict[str, int]
) -> list[ScreeningRecord]:
    source = FakeSource(
        artifacts,
        "simulated",
        records=tuple(simulated(kind, i) for kind, n in counts.items() for i in range(1, n + 1)),
    )
    plan = SearchPlan(topic_id=TOPIC, source="simulated", platform="Simulated", strategy="all")
    execution = run(
        tx,
        people.project_id,
        plan,
        source,
        ledger=ledger,
        actor_id=people.lead,
        artifacts=artifacts,
    )
    return screening_records(tx, people, [execution], ledger)


def simulated_answers(records: list[ScreeningRecord]) -> dict[str, str]:
    return {
        r.item_ref: text
        for r in records
        for prefix, text in SIMULATED_ANSWERS.items()
        if r.title.startswith(prefix)
    }


@dataclass
class World:
    db: Database
    ledger: PostgresLedger
    screening: Screening
    people: People
    protocol: StoredProtocol
    records: list[ScreeningRecord]
    backend: RecordedBackend | None = None
    artifacts: MemoryStore = field(default_factory=MemoryStore)

    @property
    def project_id(self) -> Id:
        return self.people.project_id

    def criteria(self, stage: Stage = STAGE) -> tuple[Criterion, ...]:
        topic = topic_for(self.protocol.version.protocol, SCOPE, None)
        return criteria_for(topic, SCOPE, stage)

    def open(
        self,
        records: list[ScreeningRecord] | None = None,
        *,
        stage: Stage = STAGE,
        screeners: tuple[Id, ...] | None = None,
        auditor: Id | None = None,
        reveal: str = "on_disagreement",
        backend: RecordedBackend | None = None,
    ) -> StageInfo:
        """Open the stage, enqueue the judge jobs, and run them."""
        records = self.records if records is None else records
        with self.db.transaction() as tx:
            info = self.screening.open_stage(
                tx,
                command_id=new_id(),
                project_id=self.project_id,
                actor_id=self.people.lead,
                spec=StageSpec(
                    protocol_version_id=self.protocol.id,
                    scope_id=SCOPE,
                    stage=stage,
                    records=tuple(records),
                    screeners=screeners or (self.people.a, self.people.b),
                    auditor_id=auditor,
                    sentinels=SENTINELS,
                    reveal=reveal,  # type: ignore[arg-type]
                    judge_config_id=CONFIG.id,
                ),
            )
            self.screening.request_judging(
                tx,
                command_id=new_id(),
                project_id=self.project_id,
                actor_id=self.people.lead,
                stage_id=info.stage_id,
            )
        sentinels = [s.record for s in SENTINELS]
        self.backend = backend or recorded_backend(
            [*records, *sentinels], self.criteria(stage), stage=stage
        )
        self.run_judge_jobs(self.backend)
        return info

    def screening_queue(self, reviewer: Id) -> list[QueueItem]:
        with self.db.transaction() as tx:
            return self.screening.queue(tx, self.project_id, reviewer, SCOPE, STAGE, limit=500)

    def record(self, title_prefix: str) -> ScreeningRecord:
        (found,) = [r for r in self.records if r.title.startswith(title_prefix)]
        return found

    def runner(self, backend: RecordedBackend) -> Any:
        return functools.partial(
            judge_many,
            actor_id=self.people.model,
            config=CONFIG,
            recipe=RECIPE,
            backend=backend,
            ledger=self.ledger,
            store=self.artifacts,
            clock=SystemClock(),
        )

    def run_judge_jobs(self, backend: RecordedBackend) -> int:
        """Run judge jobs until none is due; returns how many ran."""
        handler = judge_handler(
            self.db, self.screening, self.runner(backend), actor_id=self.people.model
        )
        worker = Worker(self.db, PostgresJobQueue(), {JUDGE_JOB: handler}, name=f"judge-{new_id()}")
        ran = 0
        while worker.run_once():
            ran += 1
        return ran


def build_world(db: Database, mode: str = "M1", *, simulate: dict[str, int] | None = None) -> World:
    """Commit a project, its published protocol, its searches, and its corpus: the recorded
    PubMed search and CENTRAL export, or simulated records in the given numbers."""
    ledger = PostgresLedger()
    artifacts = MemoryStore()
    with db.transaction() as tx:
        people = seed_people(tx)
        stored = publish_protocol(tx, people, ledger, mode)
        if simulate is None:
            executions = [
                pubmed_search(tx, people, ledger, artifacts),
                central_import(tx, people, ledger, artifacts),
            ]
            records = screening_records(tx, people, executions, ledger)
        else:
            records = simulated_records(tx, people, ledger, artifacts, simulate)
    return World(
        db, ledger, Screening.postgres(ledger), people, stored, records, artifacts=artifacts
    )


def timing(seconds: float, *, revealed: bool = False) -> DecisionTiming:
    shown = START + timedelta(seconds=seconds)
    return DecisionTiming(
        shown_at=shown,
        first_key_at=shown + timedelta(seconds=4),
        decided_at=shown + timedelta(seconds=12),
        reveal_shown_at=shown + timedelta(seconds=13) if revealed else None,
    )
