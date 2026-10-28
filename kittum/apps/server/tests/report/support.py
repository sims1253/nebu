"""Two small, complete reviews to report on, both screened in M1 by two humans with the AI judge
as third reader, through title and abstract and full text. An auditor screens the random audit
sample and the blind extra screens of AI-only disagreements; an adjudicator resolves the human
disagreements.

`nrt_review`: the nicotine replacement example (no profile). The recorded PubMed search, the
CENTRAL export of the screening tests, and an Embase export with two duplicates and a follow-up
report of a CENTRAL trial; a human confirms every link proposal, so the PRISMA flow has
duplicates and a study with two reports.

`capivasertib_review`: the capivasertib example under the `gba_amnog` profile. MEDLINE and
CENTRAL exports and a CTIS registry export; registry entries are excluded at title and abstract,
and one report at full text, which Anhang 4-C and 4-D list.

The judge runs on recorded completions keyed by the exact request. Every decision goes through
`Screening`, so the ledger holds every event a real review would.
"""

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from kittum.core import SystemClock, new_id
from kittum.corpus import confirm, ingest, list_proposals, merge
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
from kittum.profiles import load_profiles
from kittum.protocol import compile, publish, save_draft
from kittum.runs import PostgresJobQueue, Worker
from kittum.screening import (
    JUDGE_JOB,
    CriterionDecision,
    Decision,
    RecordFacts,
    Screening,
    ScreeningRecord,
    Sentinel,
    StageSpec,
    criteria_for,
    judge_handler,
    topic_for,
)
from kittum.search import SearchPlan, import_file
from kittum.storage import MemoryStore
from tests.screening.support import (
    SENTINELS,
    ai_answer,
    answer,
    central_import,
    pubmed_search,
    timing,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.core import Id
    from kittum.db import Database, Tx
    from kittum.protocol import Criterion, Stage, StoredProtocol
    from kittum.screening import AdjudicationItem, QueueItem, StageInfo
    from kittum.search import SearchExecution

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
EXAMPLES = HERE.parents[3] / "examples" / "protocols"
RECIPE = ModelRegistry.load().get("qwen3.8-27b-bf16")
ANCHOR = new_id()  # an evidence anchor; opaque to screening until documents exist


@dataclass
class People:
    project_id: Id
    lead: Id
    a: Id
    b: Id
    adjudicator: Id
    auditor: Id
    model: Id


def seed_people(tx: Tx, name: str) -> People:
    project = tx.execute(
        "insert into project (name, policy_version) values (%s, 'p1') returning id", (name,)
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


@dataclass
class ReviewWorld:
    db: Database
    ledger: PostgresLedger
    screening: Screening
    people: People
    protocol: StoredProtocol
    topic: str
    scope: str
    config: JudgeConfig
    artifacts: MemoryStore = field(default_factory=MemoryStore)
    executions: list[SearchExecution] = field(default_factory=list)
    records: list[ScreeningRecord] = field(default_factory=list)
    stages: dict[str, StageInfo] = field(default_factory=dict)
    sentinels: tuple[Sentinel, ...] = SENTINELS

    @property
    def project_id(self) -> Id:
        return self.people.project_id

    def criteria(self, stage: Stage) -> tuple[Criterion, ...]:
        topic = topic_for(self.protocol.version.protocol, self.scope, self.topic)
        return criteria_for(topic, self.scope, stage)

    def record(self, prefix: str) -> ScreeningRecord:
        (found,) = [r for r in self.records if r.title.startswith(prefix)]
        return found

    def head(self) -> int:
        with self.db.transaction() as tx:
            return self.ledger.events(tx, self.project_id)[-1].seq

    # Searches and the corpus

    def import_file(
        self, tx: Tx, source: str, platform: str, strategy: str, path: Path, fmt: str
    ) -> None:
        plan = SearchPlan(topic_id=self.topic, source=source, platform=platform, strategy=strategy)
        self.executions.append(
            import_file(
                tx,
                self.project_id,
                plan,
                path.read_bytes(),
                fmt,
                ledger=self.ledger,
                actor_id=self.people.lead,
                artifacts=self.artifacts,
            )
        )

    def build_corpus(self, tx: Tx, *, confirm_all: bool) -> Id:
        """Ingest the searches, let the lead confirm every proposal, and build one screening
        record per report of the resulting identity revision."""
        revision = ingest(
            tx, self.project_id, self.executions, actor_id=self.people.lead, ledger=self.ledger
        ).identity_revision_id
        if confirm_all:
            for proposal in list_proposals(tx, self.project_id, status="pending"):
                revision = confirm(
                    tx, self.project_id, revision, proposal.id, self.people.lead, ledger=self.ledger
                ).id
        self.records = _records_of(tx, self, revision)
        return revision

    # Screening

    def open(
        self,
        stage: Stage,
        records: list[ScreeningRecord],
        answers: Callable[[ScreeningRecord, tuple[Criterion, ...]], str],
    ) -> StageInfo:
        """Open the stage, run the judge on recorded completions, and keep the stage."""
        criteria = self.criteria(stage)
        with self.db.transaction() as tx:
            info = self.screening.open_stage(
                tx,
                command_id=new_id(),
                project_id=self.project_id,
                actor_id=self.people.lead,
                spec=StageSpec(
                    protocol_version_id=self.protocol.id,
                    scope_id=self.scope,
                    topic_id=self.topic,
                    stage=stage,
                    records=tuple(records),
                    screeners=(self.people.a, self.people.b),
                    auditor_id=self.people.auditor,
                    adjudicators=(self.people.adjudicator,),
                    sentinels=self.sentinels,
                    judge_config_id=self.config.id,
                ),
            )
            self.screening.request_judging(
                tx,
                command_id=new_id(),
                project_id=self.project_id,
                actor_id=self.people.lead,
                stage_id=info.stage_id,
            )
        table = {}
        for r in [*records, *(s.record for s in self.sentinels)]:
            item = ScreeningItem(item_ref=r.item_ref, stage=stage, title=r.title, text=r.text)
            request = build_request(item, applicable(criteria, self.scope, item), self.config)
            table[RecordedBackend.key(request)] = answers(r, criteria)
        backend = RecordedBackend(table, model_identity="Qwen/Qwen3.8-27B")
        runner = functools.partial(
            judge_many,
            actor_id=self.people.model,
            config=self.config,
            recipe=RECIPE,
            backend=backend,
            ledger=self.ledger,
            store=self.artifacts,
            clock=SystemClock(),
        )
        handler = judge_handler(self.db, self.screening, runner, actor_id=self.people.model)
        worker = Worker(self.db, PostgresJobQueue(), {JUDGE_JOB: handler}, name=f"judge-{new_id()}")
        while worker.run_once():
            pass
        self.stages[stage] = info
        return info

    def queue(self, reviewer: Id, stage: Stage) -> list[QueueItem]:
        with self.db.transaction() as tx:
            return self.screening.queue(
                tx, self.project_id, reviewer, self.scope, stage, limit=500, topic_id=self.topic
            )

    def decide(
        self,
        reviewer: Id,
        stage: Stage,
        prefix: str,
        decision: Decision,
        final: Decision | None = None,
    ) -> bool:
        """Decide one item; if the AI is revealed, keep the decision or revise to `final`.
        Returns whether a reveal was shown."""
        (item,) = [i for i in self.queue(reviewer, stage) if i.title.startswith(prefix)]
        with self.db.transaction() as tx:
            result = self.screening.decide(
                tx,
                command_id=new_id(),
                project_id=self.project_id,
                reviewer_id=reviewer,
                assignment_id=item.assignment_id,
                decision=decision,
                phase="provisional",
                expected_etag=item.etag,
                timing=timing(0, revealed=False),
            )
            if result.phase == "provisional":
                self.screening.decide(
                    tx,
                    command_id=new_id(),
                    project_id=self.project_id,
                    reviewer_id=reviewer,
                    assignment_id=item.assignment_id,
                    decision=final or decision,
                    phase="final",
                    expected_etag=result.etag,
                    timing=timing(20, revealed=True),
                )
                return True
        return False

    def decide_rest(self, reviewer: Id, stage: Stage, decision: Decision | None = None) -> int:
        """Decide every item still in the reviewer's queue the same way (sentinels: include)."""
        n = 0
        for item in self.queue(reviewer, stage):
            sentinel = any(item.title == s.record.title for s in self.sentinels)
            assert sentinel or decision is not None, item.title
            chosen = (
                include(stage, self.criteria(stage)) if sentinel or decision is None else decision
            )
            self.decide(reviewer, stage, item.title, chosen)
            n += 1
        return n

    def audit(self, stage: Stage, overrides: dict[str, Decision] | None = None) -> int:
        """The auditor screens every item in their queue: the random audit sample and the blind
        extra screens AI-only disagreements sent. They agree with the screeners' standing
        decision unless `overrides` names the record by title prefix. Returns how many."""
        criteria = self.criteria(stage)
        items = self.queue(self.people.auditor, stage)
        for item in items:
            chosen = next(
                (d for prefix, d in (overrides or {}).items() if item.title.startswith(prefix)),
                None,
            )
            if chosen is None:
                with self.db.transaction() as tx:
                    rows = tx.execute(
                        "select d.outcome, d.reason_criterion_id from screening_assignment a"
                        " join screening_assignment me on me.stage_id = a.stage_id"
                        " and me.item_ref = a.item_ref"
                        " join screening_decision d on d.id = a.final_decision_id"
                        " where me.id = %s and a.role = 'screener' order by a.id",
                        (item.assignment_id,),
                    ).fetchall()
                excluded = [r for r in rows if r["outcome"] == "exclude"]
                chosen = (
                    exclude(stage, criteria, excluded[0]["reason_criterion_id"])
                    if rows and len(excluded) == len(rows)
                    else include(stage, criteria)
                )
            self.decide(self.people.auditor, stage, item.title, chosen)
        return len(items)

    def adjudicate(
        self, stage: Stage, choose: Callable[[AdjudicationItem], tuple[str | None, Decision | None]]
    ) -> int:
        """Resolve every conflict in the adjudicator's queue; returns how many."""
        with self.db.transaction() as tx:
            items = self.screening.adjudication_queue(
                tx, self.project_id, self.people.adjudicator, self.scope, stage, topic_id=self.topic
            )
        for item in items:
            adopted, own = choose(item)
            with self.db.transaction() as tx:
                self.screening.adjudicate(
                    tx,
                    command_id=new_id(),
                    project_id=self.project_id,
                    adjudicator_id=self.people.adjudicator,
                    conflict_id=item.conflict_id,
                    expected_etag=item.etag,
                    adopted=adopted,
                    decision=own,
                    timing=timing(40),
                )
        return len(items)


def exclude(stage: Stage, criteria: tuple[Criterion, ...], reason: str) -> Decision:
    if stage == "title_abstract":
        return Decision(outcome="exclude", reason_criterion_id=reason)
    return Decision(
        outcome="exclude",
        reason_criterion_id=reason,
        criteria=tuple(
            CriterionDecision(
                criterion_id=c.id,
                status="not_met" if c.id == reason else "met",
                evidence_anchor_ids=(ANCHOR,),
            )
            for c in criteria
        ),
    )


def include(stage: Stage, criteria: tuple[Criterion, ...]) -> Decision:
    if stage == "title_abstract":
        return Decision(outcome="include")
    return Decision(
        outcome="include",
        criteria=tuple(
            CriterionDecision(criterion_id=c.id, status="met", evidence_anchor_ids=(ANCHOR,))
            for c in criteria
        ),
    )


def unclear(_: ScreeningRecord, criteria: tuple[Criterion, ...]) -> str:
    return answer(**{c.id: ("unclear", "", "The record does not say.") for c in criteria})


def build(
    db: Database, name: str, source: dict[str, Any], *, topic: str, scope: str, config_id: str
) -> ReviewWorld:
    load_profiles()
    ledger = PostgresLedger()
    with db.transaction() as tx:
        people = seed_people(tx, name)
        draft = save_draft(tx, people.project_id, compile(source))
        stored = publish(tx, people.project_id, draft.id, people.lead, ledger=ledger)
    config = JudgeConfig(id=config_id, model_id=RECIPE.id, prompt_version=PROMPT_VERSION)
    return ReviewWorld(db, ledger, Screening.postgres(ledger), people, stored, topic, scope, config)


# The nicotine replacement review.

NRT_SCOPE = "nrt-vs-control"
NRT_TITLES = {
    "patch": "Nicotine patch versus placebo",
    "gum": "Nicotine gum for smoking",
    "combination": "Combination nicotine replacement",
    "colitis": "Transdermal nicotine for active ulcerative colitis",
    "varenicline": "Varenicline versus placebo",
    "cohort": "Use of nicotine replacement therapy",
    "followup": "Long-term abstinence after a nicotine patch",
    "inhaler": "Nicotine inhaler versus placebo",
    "spray": "Nicotine mouth spray for smoking cessation: a 12-week",
    "glpg": "GLPG2737 in lumacaftor",
    "predictors": "Rate and predictors of prescription",
}


def _nrt_answers(record: ScreeningRecord, _: tuple[Criterion, ...]) -> str:
    met = answer(
        POP=("met", "adult smokers", "Adult smokers."),
        INT=("met", "nicotine", "One arm receives NRT."),
        DESIGN=("met", "randomised", "A randomised trial."),
    )
    if record.title.startswith(
        (NRT_TITLES["followup"], NRT_TITLES["inhaler"], NRT_TITLES["spray"])
    ):
        return met
    return ai_answer(record)


@dataclass
class NrtSeqs:
    """Ledger positions the tests render at."""

    corpus: int = 0
    title_abstract: int = 0
    inhaler_excluded: int = 0
    rescreens_open: int = 0
    full_text: int = 0


def nrt_review(db: Database) -> tuple[ReviewWorld, NrtSeqs]:
    source = yaml.safe_load((EXAMPLES / "nicotine-replacement.yaml").read_text())
    world = build(
        db, "NRT review", source, topic="TRIALS", scope=NRT_SCOPE, config_id=f"nrt-{new_id()}"
    )
    seqs = NrtSeqs()
    with db.transaction() as tx:
        world.executions.append(pubmed_search(tx, world.people, world.ledger, world.artifacts))  # type: ignore[arg-type]
        world.executions.append(central_import(tx, world.people, world.ledger, world.artifacts))  # type: ignore[arg-type]
        world.import_file(
            tx,
            "embase",
            "Embase (embase.com)",
            "'nicotine replacement therapy'/exp AND 'smoking cessation'/exp AND random*:ti,ab",
            FIXTURES / "nrt_embase.ris",
            "embase",
        )
        world.build_corpus(tx, confirm_all=True)
    seqs.corpus = world.head()

    ta: Stage = "title_abstract"
    world.open(ta, world.records, _nrt_answers)
    a, b = world.people.a, world.people.b
    c = world.criteria(ta)
    t = NRT_TITLES
    for key in ("patch", "gum", "combination", "followup", "spray"):
        world.decide(a, ta, t[key], include(ta, c))
        world.decide(b, ta, t[key], include(ta, c))
    # A includes the colitis trial, sees the AI's verified exclusion, and revises.
    assert world.decide(a, ta, t["colitis"], include(ta, c), final=exclude(ta, c, "POP"))
    world.decide(b, ta, t["colitis"], exclude(ta, c, "POP"))
    world.decide(a, ta, t["varenicline"], exclude(ta, c, "INT"))
    world.decide(b, ta, t["varenicline"], include(ta, c))
    world.decide(a, ta, t["cohort"], exclude(ta, c, "DESIGN"))
    world.decide(b, ta, t["cohort"], exclude(ta, c, "DESIGN"))
    # B first excludes the inhaler trial, then takes it back and includes it.
    world.decide(a, ta, t["inhaler"], include(ta, c))
    world.decide(b, ta, t["inhaler"], exclude(ta, c, "INT"))
    with db.transaction() as tx:
        (link,) = tx.execute(
            "select a.id, a.etag from screening_assignment a join screening_item i"
            " on i.stage_id = a.stage_id and i.item_ref = a.item_ref"
            " where a.stage_id = %s and a.reviewer_id = %s and i.title like %s",
            (world.stages[ta].stage_id, b, t["inhaler"] + "%"),
        ).fetchall()
    # The disagreement is open now; the undo withdraws it.
    seqs.inhaler_excluded = world.head()
    with db.transaction() as tx:
        world.screening.undo(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            reviewer_id=b,
            assignment_id=link["id"],
            expected_etag=link["etag"],
        )
    world.decide(b, ta, t["inhaler"], include(ta, c))
    # Everything else is the off-topic PubMed search: both exclude on the population.
    world.decide_rest(a, ta, exclude(ta, c, "POP"))
    world.decide_rest(b, ta, exclude(ta, c, "POP"))
    # The model saw nothing wrong with two PubMed records both humans excluded: the auditor screens
    # them again, blind. They uphold one exclusion and include the other, a human disagreement.
    seqs.rescreens_open = world.head()
    world.audit(ta, overrides={t["predictors"]: include(ta, c)})
    # Adjudication: the varenicline disagreement, and the auditor's disagreement.
    resolved = world.adjudicate(
        ta,
        lambda _: (None, exclude(ta, c, "INT" if _.title.startswith(t["varenicline"]) else "POP")),
    )
    assert resolved == 2
    seqs.title_abstract = world.head()

    # Full text: the inhaler trial's report could not be obtained.
    ft: Stage = "full_text"
    retrieved = [world.record(t[k]) for k in ("patch", "gum", "combination", "followup", "spray")]
    world.open(ft, retrieved, unclear)
    c = world.criteria(ft)
    for key in ("patch", "gum", "followup"):
        world.decide(a, ft, t[key], include(ft, c))
        world.decide(b, ft, t[key], include(ft, c))
    world.decide(a, ft, t["combination"], exclude(ft, c, "COMP-CONTROL"))
    world.decide(b, ft, t["combination"], exclude(ft, c, "FOLLOWUP"))
    world.decide(a, ft, t["spray"], exclude(ft, c, "FOLLOWUP"))
    world.decide(b, ft, t["spray"], exclude(ft, c, "FOLLOWUP"))
    world.decide_rest(a, ft)
    world.decide_rest(b, ft)
    # The AI read every full text as unclear, so both double exclusions get an extra screen; the
    # auditor upholds them.
    world.audit(ft)

    def choose(item: AdjudicationItem) -> tuple[str | None, Decision | None]:
        column = next(col for col in item.columns if col.reason_criterion_id == "COMP-CONTROL")
        return column.label, None

    # Only the screeners' different reasons for the combination trial need an adjudicator.
    assert world.adjudicate(ft, choose) == 1
    seqs.full_text = world.head()
    return world, seqs


# The capivasertib dossier.

CAPI_SCOPE = "a1"
CAPI_TITLES = {
    "turner": "Capivasertib in Hormone Receptor-Positive",
    "jones": "Fulvestrant plus capivasertib versus placebo after relapse or progression on an aromatase inhibitor in metastatic, oestrogen receptor-positive breast cancer (FAKTION): a multicentre",
    "howell": "Fulvestrant plus capivasertib versus placebo after relapse or progression on an aromatase inhibitor in metastatic, oestrogen receptor-positive, HER2-negative",
    "pakt": "Capivasertib Plus Paclitaxel",
    "smyth": "Capivasertib, an AKT Kinase Inhibitor",
    "ctis_capitello": "A Phase III Double-blind Randomised Study",
    "ctis_tnbc": "A randomised trial of capivasertib plus paclitaxel",
    "ctis_prostate": "Capivasertib with docetaxel",
}


CAPI_SENTINELS = (
    Sentinel(
        record=ScreeningRecord(
            item_ref="sentinel:capi-rct",
            title="Capivasertib plus fulvestrant in AKT pathway-altered advanced breast cancer: a "
            "randomised, placebo-controlled trial",
            text="METHODS: Adults with ER-positive, HER2-negative advanced breast cancer that "
            "progressed after endocrine therapy were randomised to capivasertib plus fulvestrant "
            "or placebo plus fulvestrant.",
        )
    ),
)


def capivasertib_review(db: Database) -> ReviewWorld:
    source = yaml.safe_load((EXAMPLES / "capivasertib.yaml").read_text())
    world = build(
        db,
        "Capivasertib Modul 4 A",
        source,
        topic="RCT",
        scope=CAPI_SCOPE,
        config_id=f"capi-{new_id()}",
    )
    world.sentinels = CAPI_SENTINELS
    with db.transaction() as tx:
        world.import_file(
            tx,
            "medline",
            "PubMed",
            "#1 capivasertib[tiab] OR AZD5363[tiab]\n#2 fulvestrant[tiab]\n"
            "#3 randomized controlled trial[pt] OR randomized[tiab] OR placebo[tiab]\n#4 #1 AND #2 AND #3",
            FIXTURES / "capivasertib_medline.ris",
            "ris",
        )
        world.import_file(
            tx,
            "central",
            "Cochrane Library (Wiley)",
            "#1 (capivasertib OR AZD5363):ti,ab,kw\n#2 fulvestrant:ti,ab,kw\n#3 #1 AND #2",
            FIXTURES / "capivasertib_central.ris",
            "central",
        )
        world.import_file(
            tx,
            "ctis",
            "CTIS public search",
            "capivasertib",
            FIXTURES / "capivasertib_ctis.csv",
            "ctis",
        )
        revision = world.build_corpus(tx, confirm_all=False)
        # The lead merges the reports that share a DOI, and nothing else.
        rows = tx.execute(
            "select occurrence_id, doi from source_occurrence"
            " where project_id = %s and doi is not null order by occurrence_id",
            (world.project_id,),
        ).fetchall()
        by_doi: dict[str, list[Id]] = {}
        for row in rows:
            by_doi.setdefault(row["doi"].lower(), []).append(row["occurrence_id"])
        for ids in by_doi.values():
            if len(ids) > 1:
                revision = merge(
                    tx,
                    world.project_id,
                    revision,
                    ids,
                    "same_record",
                    world.people.lead,
                    reason="same DOI",
                    ledger=world.ledger,
                ).id
        world.records = _records_of(tx, world, revision)

    ta: Stage = "title_abstract"
    world.open(ta, world.records, unclear)
    a, b = world.people.a, world.people.b
    c = world.criteria(ta)
    t = CAPI_TITLES
    for key in ("turner", "jones", "howell", "ctis_capitello"):
        world.decide(a, ta, t[key], include(ta, c))
        world.decide(b, ta, t[key], include(ta, c))
    for who in (a, b):
        world.decide(who, ta, t["pakt"], exclude(ta, c, "RCT-E2"))
        world.decide(who, ta, t["ctis_tnbc"], exclude(ta, c, "RCT-E2"))
        world.decide(who, ta, t["ctis_prostate"], exclude(ta, c, "RCT-E1"))
    world.decide(a, ta, t["smyth"], exclude(ta, c, "RCT-E5"))
    world.decide(b, ta, t["smyth"], include(ta, c))
    world.decide_rest(a, ta)
    world.decide_rest(b, ta)
    # The AI read every criterion as unclear, so the auditor screens every double exclusion again
    # and upholds it; only the Smyth disagreement goes to the adjudicator.
    world.audit(ta)
    assert world.adjudicate(ta, lambda _: (None, exclude(ta, c, "RCT-E5"))) == 1

    ft: Stage = "full_text"
    world.open(
        ft, [world.record(t[k]) for k in ("turner", "jones", "howell", "ctis_capitello")], unclear
    )
    c = world.criteria(ft)
    for key in ("turner", "howell", "ctis_capitello"):
        world.decide(a, ft, t[key], include(ft, c))
        world.decide(b, ft, t[key], include(ft, c))
    # The 2020 FAKTION report has no results for the PIK3CA/AKT1/PTEN-altered subgroup.
    world.decide(a, ft, t["jones"], exclude(ft, c, "RCT-E1-ALT"))
    world.decide(b, ft, t["jones"], exclude(ft, c, "RCT-E1-ALT"))
    world.decide_rest(a, ft)
    world.decide_rest(b, ft)
    world.audit(ft)
    assert world.adjudicate(ft, lambda _: ("A", None)) == 0
    return world


def _records_of(tx: Tx, world: ReviewWorld, revision: Id) -> list[ScreeningRecord]:
    """One screening record per report of the revision, from its longest abstract."""
    links = tx.execute(
        "select occurrence_id, report_id from identity_link where revision_id = %s", (revision,)
    ).fetchall()
    occurrences = {r.occurrence_id: r for e in world.executions for r in e.records}
    by_report: dict[Id, list[Any]] = {}
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
                    pmid=best.pmid, doi=best.doi, journal=best.journal, year=best.year
                ),
            )
        )
    return sorted(records, key=lambda r: r.title)
