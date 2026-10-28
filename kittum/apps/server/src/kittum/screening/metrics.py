"""The methods lead's view of a stage: progress, agreement, the audit gauge, the extra screens
of AI-only disagreements, and the M3 stop test.

Agreement uses the blind provisional decisions: a final decision may follow an AI reveal, and a
reviewer never sees the other's. Labels are include against exclude (`unclear` and insufficient
information count as include). A record sent to an extra screen counts as screened once the
auditor has decided it.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from typing import TYPE_CHECKING, Any, Literal

from kittum.protocol import ScreeningMode
from kittum.screening.store import reading_of
from kittum.screening.types import (
    AuditGauge,
    AuditStratum,
    PairAgreement,
    RescreenGauge,
    StageMetrics,
    StopStatus,
)
from kittum.stats import Stratum, cohen_kappa, elusion_estimate, pabak, stop_test

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kittum.db import Tx
    from kittum.protocol import SearchTopic, StoppingPlan
    from kittum.screening.rules import Reading

AUDIT_CONFIDENCE = 0.95

type RecordOutcome = Literal["include", "exclude", "conflict"] | None
"""A record's current outcome: by consensus or adjudication, in conflict, or not yet screened."""


def _label(outcome: str | None) -> str:
    return "exclude" if outcome == "exclude" else "include"


def _agreement(
    kind: Literal["human_human", "human_ai", "human_ai_verified"],
    raters: tuple[str, str],
    a: Sequence[str],
    b: Sequence[str],
) -> PairAgreement:
    kappa = None
    if a:
        try:
            kappa = cohen_kappa(a, b)
        except ValueError:
            kappa = None  # one label throughout: kappa is undefined
    return PairAgreement(
        kind=kind,
        raters=raters,
        items=len(a),
        kappa=kappa,
        pabak=pabak(a, b) if a else None,
    )


@dataclass
class StageData:
    """Every row of a stage that the lead's view reads."""

    mode: ScreeningMode
    items: dict[str, dict[str, Any]]
    assignments: list[dict[str, Any]]
    readings: dict[str, dict[str, Any]]
    conflicts: dict[str, list[dict[str, Any]]]
    by_item: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for a in self.assignments:
            self.by_item.setdefault(a["item_ref"], []).append(a)

    @classmethod
    def load(cls, tx: Tx, stage: dict[str, Any]) -> StageData:
        stage_id = stage["id"]
        items = {
            r["item_ref"]: r
            for r in tx.execute(
                "select item_ref, kind, expected_outcome, audit, rescreen from screening_item"
                " where stage_id = %s",
                (stage_id,),
            ).fetchall()
        }
        assignments = tx.execute(
            "select a.item_ref, a.reviewer_id, a.role, a.sort_key, a.provisional_decision_id,"
            " a.final_decision_id, p.outcome as p_outcome, p.reveal as p_reveal,"
            " f.outcome as f_outcome"
            " from screening_assignment a"
            " left join screening_decision p on p.id = a.provisional_decision_id"
            " left join screening_decision f on f.id = a.final_decision_id"
            " where a.stage_id = %s",
            (stage_id,),
        ).fetchall()
        readings = {
            r["item_ref"]: r
            for r in tx.execute(
                "select item_ref, excludes, rank, criteria, flags from screening_ai_reading"
                " where stage_id = %s",
                (stage_id,),
            ).fetchall()
        }
        conflicts: dict[str, list[dict[str, Any]]] = {}
        for c in tx.execute(
            "select c.item_ref, c.state, c.kinds, j.outcome"
            " from screening_conflict c left join screening_adjudication j on j.conflict_id = c.id"
            " where c.stage_id = %s order by c.opened_at, c.id",
            (stage_id,),
        ).fetchall():
            conflicts.setdefault(c["item_ref"], []).append(c)
        return cls(ScreeningMode(stage["mode"]), items, assignments, readings, conflicts)

    @property
    def records(self) -> list[str]:
        return [ref for ref, it in self.items.items() if it["kind"] == "record"]

    def screeners(self, ref: str) -> list[dict[str, Any]]:
        return [a for a in self.by_item.get(ref, []) if a["role"] == "screener"]

    def auditor(self, ref: str) -> dict[str, Any] | None:
        return next((a for a in self.by_item.get(ref, []) if a["role"] == "auditor"), None)

    def screened(self, ref: str) -> bool:
        """Every screener decided, and the auditor too if the record went to an extra screen."""
        if any(a["f_outcome"] is None for a in self.screeners(ref)):
            return False
        auditor = self.auditor(ref)
        return not self.items[ref]["rescreen"] or (
            auditor is not None and auditor["f_outcome"] is not None
        )

    def genuine(self, ref: str) -> list[dict[str, Any]]:
        """The record's conflicts that were not withdrawn."""
        return [c for c in self.conflicts.get(ref, []) if c["state"] != "withdrawn"]

    def outcome(self, ref: str) -> RecordOutcome:
        history = self.genuine(ref)
        if history and history[-1]["state"] == "open":
            return "conflict"
        if history:
            return history[-1]["outcome"]
        if not self.screened(ref):
            return None
        finals = [a["f_outcome"] for a in self.screeners(ref)]
        return "exclude" if all(f == "exclude" for f in finals) else "include"

    def reading(self, ref: str) -> Reading | None:
        row = self.readings.get(ref)
        return reading_of(row) if row else None


def record_outcomes(tx: Tx, stage: dict[str, Any]) -> dict[str, RecordOutcome]:
    data = StageData.load(tx, stage)
    return {ref: data.outcome(ref) for ref in data.records}


def stage_metrics(
    tx: Tx, stage: dict[str, Any], topic: SearchTopic
) -> tuple[int, int, int, StageMetrics]:
    """(screened, remaining, open conflicts, metrics) over the stage's records."""
    data = StageData.load(tx, stage)
    items, assignments, records = data.items, data.assignments, data.records

    def excluded_unseen(ref: str) -> bool:
        """Excluded on the screeners' path alone. An adjudication the audit itself raised keeps the
        record in the frame, so an audit miss still counts against the bound."""
        path = [c for c in data.genuine(ref) if set(c["kinds"]) - {"audit_disagreement"}]
        finals = [a["f_outcome"] for a in data.screeners(ref)]
        return data.screened(ref) and not path and all(f == "exclude" for f in finals)

    done = [r for r in records if data.screened(r)]
    open_conflicts = sum(1 for r in records if any(c["state"] == "open" for c in data.genuine(r)))
    resolved = sum(1 for r in records for c in data.genuine(r) if c["state"] == "resolved")

    # Agreement on the blind provisional decisions of records. Against the AI twice: its own
    # reading (the headline) and the reading the judge's filter verified.
    people = sorted({str(a["reviewer_id"]) for a in assignments if a["role"] == "screener"})
    provisional = {
        (str(a["reviewer_id"]), a["item_ref"]): a["p_outcome"]
        for a in assignments
        if a["role"] == "screener"
        and a["p_outcome"] is not None
        and items[a["item_ref"]]["kind"] == "record"
    }
    agreement: list[PairAgreement] = []
    for left, right in combinations(people, 2):
        refs = [r for r in records if (left, r) in provisional and (right, r) in provisional]
        agreement.append(
            _agreement(
                "human_human",
                (left, right),
                [_label(provisional[left, r]) for r in refs],
                [_label(provisional[right, r]) for r in refs],
            )
        )
    readings = {r: reading for r in records if (reading := data.reading(r)) is not None}
    for person in people:
        refs = [r for r in records if (person, r) in provisional and r in readings]
        human = [_label(provisional[person, r]) for r in refs]
        own = ["include" if readings[r].includes_by_own_reading else "exclude" for r in refs]
        verified = ["exclude" if readings[r].excludes else "include" for r in refs]
        agreement.append(_agreement("human_ai", (person, "ai"), human, own))
        agreement.append(_agreement("human_ai_verified", (person, "ai"), human, verified))

    reveals = [a for a in assignments if a["p_reveal"] is not None]
    revised = [
        a
        for a in reveals
        if a["final_decision_id"] not in (None, a["provisional_decision_id"])
        and a["f_outcome"] != a["p_outcome"]
    ]

    # The audit gauge: sentinels, and the elusion estimate from the random audit sample.
    sentinel_assignments = [a for a in assignments if items[a["item_ref"]]["kind"] == "sentinel"]
    sentinels_decided = [a for a in sentinel_assignments if a["f_outcome"] is not None]
    correct = [
        a
        for a in sentinels_decided
        if _label(a["f_outcome"]) == items[a["item_ref"]]["expected_outcome"]
    ]
    unscreened = (
        [r for r in records if not data.screened(r)] if data.mode == ScreeningMode.M3 else []
    )
    population = [r for r in records if excluded_unseen(r)] + unscreened
    audited = [r for r in records if items[r]["audit"]]
    audit_final = {
        a["item_ref"]: a["f_outcome"]
        for a in assignments
        if a["role"] == "auditor" and a["f_outcome"] is not None and items[a["item_ref"]]["audit"]
    }
    # One stratum per verified AI reading: records the AI verifiably excluded, and the rest.
    strata = []
    for ai_excludes in (True, False):
        members = [r for r in population if (r in readings and readings[r].excludes) == ai_excludes]
        sampled = [r for r in members if r in audit_final]
        strata.append(
            AuditStratum(
                ai_excludes=ai_excludes,
                population=len(members),
                sampled=len(sampled),
                relevant_found=sum(audit_final[r] != "exclude" for r in sampled),
            )
        )
    filled = [s for s in strata if s.population]
    elusion = None
    if all(s.sampled for s in filled):  # a stratum without a decided sample has no estimate
        elusion = elusion_estimate(
            [
                Stratum(population=s.population, sampled=s.sampled, relevant_found=s.relevant_found)
                for s in filled
            ],
            confidence=AUDIT_CONFIDENCE,
        )
    audit = AuditGauge(
        sentinels=len(sentinel_assignments),
        sentinels_decided=len(sentinels_decided),
        sentinels_correct=len(correct),
        audit_samples=len(audited),
        audit_decided=sum(1 for r in audited if r in audit_final),
        excluded_population=len(population),
        audit_relevant_found=sum(s.relevant_found for s in strata),
        strata=tuple(strata),
        elusion=elusion,
    )

    rescreened = [r for r in records if items[r]["rescreen"]]
    rescreen_finals = {
        r: auditor["f_outcome"]
        for r in rescreened
        if (auditor := data.auditor(r)) is not None and auditor["f_outcome"] is not None
    }
    upheld = sum(
        all(_label(rescreen_finals[r]) == _label(a["f_outcome"]) for a in data.screeners(r))
        for r in rescreen_finals
    )
    rescreens = RescreenGauge(
        triggered=len(rescreened),
        pending=len(rescreened) - len(rescreen_finals),
        upheld=upheld,
        adjudicated=len(rescreen_finals) - upheld,
    )

    stop = None
    if data.mode == ScreeningMode.M3 and topic.stopping is not None:
        order = sorted(
            records,
            key=lambda r: (
                data.readings[r]["rank"] if r in data.readings else 10**6,
                data.screeners(r)[0]["sort_key"],
            ),
        )
        stop = stop_status(topic.stopping, [data.outcome(r) for r in order])

    metrics = StageMetrics(
        mode=data.mode,
        reveal=stage["reveal"],
        conflicts_resolved=resolved,
        reveals_shown=len(reveals),
        revised_after_reveal=len(revised),
        agreement=tuple(agreement),
        audit=audit,
        rescreens=rescreens,
        stop_test=stop,
    )
    return len(done), len(records) - len(done), open_conflicts, metrics


def stop_status(plan: StoppingPlan, outcomes: Sequence[RecordOutcome]) -> StopStatus:
    """The M3 stop test over the outcomes of every record in AI order.

    It tests the fully screened prefix (up to the first record not yet screened). A record in
    conflict counts as relevant in the p-value, but an unresolved conflict anywhere in the prefix
    withholds certification: its adjudication may turn it into an exclusion, which can only make
    the test less likely to pass, so the verdict is `pending_conflicts` until they are resolved.
    """
    prefix: list[RecordOutcome] = []
    for outcome in outcomes:
        if outcome is None:
            break
        prefix.append(outcome)
    labels = [o != "exclude" for o in prefix]
    pending = sum(o == "conflict" for o in prefix)
    target = float(Decimal(plan.target_recall_percent) / 100)
    alpha = float(1 - Decimal(plan.confidence_percent) / 100)
    test = stop_test(
        len(prefix),
        sum(labels),
        len(outcomes) - len(prefix),
        target_recall=target,
        alpha=alpha,
        order=labels,
    )
    minimum_met = Decimal(len(prefix)) * 100 >= Decimal(plan.minimum_screened_percent) * len(
        outcomes
    )
    verdict: Literal["stop", "continue", "pending_conflicts"] = (
        "pending_conflicts" if pending else "stop" if test.stop and minimum_met else "continue"
    )
    return StopStatus(
        screened=len(prefix),
        relevant=sum(labels),
        remaining=len(outcomes) - len(prefix),
        target_recall=target,
        alpha=alpha,
        p_value=test.p_value,
        minimum_screened_percent=plan.minimum_screened_percent,
        minimum_met=minimum_met,
        pending_conflicts=pending,
        verdict=verdict,
        stop=verdict == "stop",
        method=test.method,
    )


__all__ = ["RecordOutcome", "StageData", "record_outcomes", "stage_metrics", "stop_status"]
