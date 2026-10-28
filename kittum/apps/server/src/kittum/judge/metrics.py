"""Judge performance on gold data (DESIGN.md, "Evaluation"; docs/document-tools.md).

A record is positive when the gold label says it is relevant. A judge includes a record unless
its verdict excludes it, so a miss is a relevant record the judge excludes. Rates whose
denominator is zero are None, not zero.
"""

from collections import Counter
from typing import TYPE_CHECKING

from kittum.core import Record

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping, Sequence


class JudgeMetrics(Record):
    n: int
    tp: int  # relevant, included
    fn: int  # relevant, excluded: a miss
    tn: int  # irrelevant, excluded
    fp: int  # irrelevant, included
    recall: float | None
    specificity: float | None
    work_saved: float | None  # share of records the judge excludes, so no human reads them
    wss: float | None  # work saved over sampling: work_saved - (1 - recall)


class PairMetrics(Record):
    a: JudgeMetrics
    b: JudgeMetrics
    or_rule: JudgeMetrics  # include when either judge includes
    positives: int
    both_missed: int
    double_fault_rate: float | None  # both_missed / positives
    agreement: float | None  # share of records where the judges decide alike
    kappa: float | None  # Cohen's kappa between the judges
    or_rule_kappa: float | None  # Cohen's kappa between the OR rule and the gold labels


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _check(gold: Mapping[str, bool], excludes: Mapping[str, bool]) -> None:
    missing = gold.keys() - excludes.keys()
    if missing:
        raise ValueError(f"no decision for {len(missing)} gold records, e.g. {min(missing)}")


def judge_metrics(gold: Mapping[str, bool], excludes: Mapping[str, bool]) -> JudgeMetrics:
    """`gold` maps item refs to relevance; `excludes` maps them to `Verdict.excludes`."""
    _check(gold, excludes)
    c = Counter((relevant, excludes[ref]) for ref, relevant in gold.items())
    tp, fn, tn, fp = c[True, False], c[True, True], c[False, True], c[False, False]
    n = len(gold)
    recall = _ratio(tp, tp + fn)
    work_saved = _ratio(tn + fn, n)
    return JudgeMetrics(
        n=n,
        tp=tp,
        fn=fn,
        tn=tn,
        fp=fp,
        recall=recall,
        specificity=_ratio(tn, tn + fp),
        work_saved=work_saved,
        wss=None if recall is None or work_saved is None else work_saved - (1 - recall),
    )


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float | None:
    if len(a) != len(b):
        raise ValueError("ratings differ in length")
    n = len(a)
    if not n:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in ca.keys() | cb.keys()) / (n * n)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def pair_metrics(
    gold: Mapping[str, bool], a: Mapping[str, bool], b: Mapping[str, bool]
) -> PairMetrics:
    """Two judges on the same gold set, and the OR rule that excludes only when both do."""
    _check(gold, a)
    _check(gold, b)
    refs = sorted(gold)
    or_excludes = {ref: a[ref] and b[ref] for ref in refs}
    positives = [ref for ref in refs if gold[ref]]
    both_missed = sum(a[ref] and b[ref] for ref in positives)
    return PairMetrics(
        a=judge_metrics(gold, a),
        b=judge_metrics(gold, b),
        or_rule=judge_metrics(gold, or_excludes),
        positives=len(positives),
        both_missed=both_missed,
        double_fault_rate=_ratio(both_missed, len(positives)),
        agreement=_ratio(sum(a[ref] == b[ref] for ref in refs), len(refs)),
        kappa=cohen_kappa([a[r] for r in refs], [b[r] for r in refs]),
        or_rule_kappa=cohen_kappa([not or_excludes[r] for r in refs], [gold[r] for r in refs]),
    )


def fleiss_kappa(ratings: Sequence[Sequence[Hashable]]) -> float | None:
    """Agreement of several runs: one row per item, one rating per run (run-to-run kappa)."""
    if not ratings:
        return None
    m = len(ratings[0])
    if m < 2 or any(len(row) != m for row in ratings):
        raise ValueError("every item needs the same number of ratings, at least two")
    counts = [Counter(row) for row in ratings]
    n = len(ratings)
    p_bar = sum((sum(v * v for v in c.values()) - m) / (m * (m - 1)) for c in counts) / n
    totals: Counter[Hashable] = Counter()
    for c in counts:
        totals.update(c)
    p_e = sum((v / (n * m)) ** 2 for v in totals.values())
    return None if p_e == 1 else (p_bar - p_e) / (1 - p_e)
