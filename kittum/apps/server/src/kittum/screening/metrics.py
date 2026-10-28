"""The methods lead's view of a stage: progress, agreement, the audit gauge, and the M3 stop test.

Agreement uses the blind provisional decisions: a final decision may follow an AI reveal, and a
reviewer never sees the other's. Labels are include against exclude (`unclear` and insufficient
information count as include).
"""

from decimal import Decimal
from itertools import combinations
from typing import TYPE_CHECKING, Any, Literal

from kittum.protocol import ScreeningMode
from kittum.screening.types import AuditGauge, PairAgreement, StageMetrics, StopStatus
from kittum.stats import Stratum, cohen_kappa, elusion_estimate, pabak, stop_test

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from kittum.db import Tx
    from kittum.protocol import SearchTopic, StoppingPlan

AUDIT_CONFIDENCE = 0.95


def _label(outcome: str | None) -> str:
    return "exclude" if outcome == "exclude" else "include"


def _agreement(
    kind: Literal["human_human", "human_ai"],
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


def stage_metrics(
    tx: Tx, stage: dict[str, Any], topic: SearchTopic
) -> tuple[int, int, int, StageMetrics]:
    """(screened, remaining, open conflicts, metrics) over the stage's records."""
    stage_id = stage["id"]
    mode = ScreeningMode(stage["mode"])
    items = {
        r["item_ref"]: r
        for r in tx.execute(
            "select item_ref, kind, expected_outcome, audit from screening_item where stage_id = %s",
            (stage_id,),
        ).fetchall()
    }
    assignments = tx.execute(
        "select a.item_ref, a.reviewer_id, a.role, a.sort_key, a.provisional_decision_id,"
        " a.final_decision_id,"
        " p.outcome as p_outcome, p.reveal as p_reveal,"
        " f.outcome as f_outcome, f.reason_criterion_id as f_reason_criterion_id,"
        " f.evidence_anchor_ids as f_evidence_anchor_ids, f.comment as f_comment,"
        " f.criteria as f_criteria"
        " from screening_assignment a"
        " left join screening_decision p on p.id = a.provisional_decision_id"
        " left join screening_decision f on f.id = a.final_decision_id"
        " where a.stage_id = %s",
        (stage_id,),
    ).fetchall()
    readings = {
        r["item_ref"]: r
        for r in tx.execute(
            "select item_ref, excludes, rank from screening_ai_reading where stage_id = %s",
            (stage_id,),
        ).fetchall()
    }
    conflicts: dict[str, list[dict[str, Any]]] = {}
    for c in tx.execute(
        "select c.item_ref, c.state, c.kinds, j.outcome from screening_conflict c"
        " left join screening_adjudication j on j.conflict_id = c.id"
        " where c.stage_id = %s order by c.opened_at, c.id",
        (stage_id,),
    ).fetchall():
        conflicts.setdefault(c["item_ref"], []).append(c)

    by_item: dict[str, list[dict[str, Any]]] = {}
    for a in assignments:
        by_item.setdefault(a["item_ref"], []).append(a)
    records = [ref for ref, it in items.items() if it["kind"] == "record"]

    def screeners(ref: str) -> list[dict[str, Any]]:
        return [a for a in by_item.get(ref, []) if a["role"] == "screener"]

    def screened(ref: str) -> bool:
        return all(a["f_outcome"] is not None for a in screeners(ref))

    def outcome(ref: str) -> str | None:
        """The record's current outcome: adjudicated, in conflict, by consensus, or None."""
        history = [c for c in conflicts.get(ref, []) if c["state"] != "withdrawn"]
        if history and history[-1]["state"] == "open":
            return "conflict"
        if history:
            return history[-1]["outcome"]
        if not screened(ref):
            return None
        return "exclude" if all(a["f_outcome"] == "exclude" for a in screeners(ref)) else "include"

    def excluded_unseen(ref: str) -> bool:
        """Excluded on the screeners' path alone, without an adjudicator looking at it."""
        path_conflicts = [
            c
            for c in conflicts.get(ref, [])
            if c["state"] != "withdrawn" and set(c["kinds"]) - {"audit_miss"}
        ]
        return (
            screened(ref)
            and not path_conflicts
            and all(a["f_outcome"] == "exclude" for a in screeners(ref))
        )

    done = [r for r in records if screened(r)]
    open_conflicts = sum(
        1 for r in records if any(c["state"] == "open" for c in conflicts.get(r, []))
    )
    resolved = sum(1 for cs in conflicts.values() for c in cs if c["state"] == "resolved")

    # Agreement on the blind provisional decisions of records.
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
    for person in people:
        refs = [r for r in records if (person, r) in provisional and r in readings]
        agreement.append(
            _agreement(
                "human_ai",
                (person, "ai"),
                [_label(provisional[person, r]) for r in refs],
                ["exclude" if readings[r]["excludes"] else "include" for r in refs],
            )
        )

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
    unscreened = [r for r in records if not screened(r)] if mode == ScreeningMode.M3 else []
    population = [r for r in records if excluded_unseen(r)] + unscreened
    audited = [r for r in records if items[r]["audit"]]
    audit_final = {
        a["item_ref"]: a["f_outcome"]
        for a in assignments
        if a["role"] == "auditor" and a["f_outcome"] is not None and items[a["item_ref"]]["audit"]
    }
    sampled = [r for r in population if r in audit_final]
    relevant = [r for r in sampled if audit_final[r] != "exclude"]
    elusion = None
    if not population:
        elusion = elusion_estimate([], confidence=AUDIT_CONFIDENCE)
    elif sampled:
        elusion = elusion_estimate(
            [
                Stratum(
                    population=len(population), sampled=len(sampled), relevant_found=len(relevant)
                )
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
        audit_relevant_found=len(relevant),
        elusion=elusion,
    )

    stop = None
    if mode == ScreeningMode.M3 and topic.stopping is not None:
        stop = _stop_status(
            topic.stopping,
            [
                (r, readings[r]["rank"] if r in readings else 10**6, screeners(r)[0]["sort_key"])
                for r in records
            ],
            screened,
            outcome,
        )

    metrics = StageMetrics(
        mode=mode,
        reveal=stage["reveal"],
        conflicts_resolved=resolved,
        reveals_shown=len(reveals),
        revised_after_reveal=len(revised),
        agreement=tuple(agreement),
        audit=audit,
        stop_test=stop,
    )
    return len(done), len(records) - len(done), open_conflicts, metrics


def _stop_status(
    plan: StoppingPlan,
    ranked: list[tuple[str, int, str]],
    screened: Callable[[str], bool],
    outcome: Callable[[str], str | None],
) -> StopStatus:
    """The stop test over the fully screened prefix of the AI order (rank, then the shared seeded
    key). Records in conflict count as relevant, which can only delay the stop."""
    records = [r for r, _, _ in ranked]
    prefix: list[str] = []
    for ref, _, _ in sorted(ranked, key=lambda x: (x[1], x[2])):
        if not screened(ref):
            break
        prefix.append(ref)
    labels = [outcome(r) != "exclude" for r in prefix]
    target = float(Decimal(plan.target_recall_percent) / 100)
    alpha = float(1 - Decimal(plan.confidence_percent) / 100)
    test = stop_test(
        len(prefix),
        sum(labels),
        len(records) - len(prefix),
        target_recall=target,
        alpha=alpha,
        order=labels,
    )
    minimum_met = Decimal(len(prefix)) * 100 >= Decimal(plan.minimum_screened_percent) * len(
        records
    )
    return StopStatus(
        screened=len(prefix),
        relevant=sum(labels),
        remaining=len(records) - len(prefix),
        target_recall=target,
        alpha=alpha,
        p_value=test.p_value,
        minimum_screened_percent=plan.minimum_screened_percent,
        minimum_met=minimum_met,
        stop=test.stop and minimum_met,
        method=test.method,
    )


__all__ = ["stage_metrics"]
