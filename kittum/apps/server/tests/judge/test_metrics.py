"""Gold-data metrics on a small, hand-checked gold set and a published worked example."""

import pytest

from kittum.judge import fleiss_kappa, judge_metrics, pair_metrics

# Ten records, four relevant. Each judge maps a record to `Verdict.excludes`.
GOLD = {f"r{i}": i <= 4 for i in range(1, 11)}
JUDGE_A = {f"r{i}": i in {2, 5, 6, 7, 8} for i in range(1, 11)}  # misses r2
JUDGE_B = {f"r{i}": i in {2, 3, 5, 6, 9} for i in range(1, 11)}  # misses r2 and r3


def test_per_judge_recall_specificity_and_work_saved() -> None:
    a = judge_metrics(GOLD, JUDGE_A)
    assert (a.tp, a.fn, a.tn, a.fp) == (3, 1, 4, 2)
    assert a.recall == 0.75
    assert a.specificity == pytest.approx(4 / 6)
    assert a.work_saved == 0.5
    assert a.wss == pytest.approx(0.25)


def test_double_fault_rate_and_the_or_rule() -> None:
    m = pair_metrics(GOLD, JUDGE_A, JUDGE_B)
    assert m.b.recall == 0.5
    # Both judges miss r2, so pairing them cannot recover it.
    assert (m.positives, m.both_missed, m.double_fault_rate) == (4, 1, 0.25)
    # The OR rule excludes only what both exclude: r2, r5, r6.
    assert (m.or_rule.tp, m.or_rule.fn, m.or_rule.tn, m.or_rule.fp) == (3, 1, 2, 4)
    assert m.or_rule.recall == 0.75
    assert m.or_rule.work_saved == pytest.approx(0.3)
    assert m.agreement == pytest.approx(0.6)
    assert m.kappa == pytest.approx(0.2)
    assert m.or_rule_kappa == pytest.approx(0.04 / 0.54)


def test_judges_with_disjoint_misses_have_no_double_faults() -> None:
    b = {**JUDGE_B, "r2": False}
    m = pair_metrics(GOLD, JUDGE_A, b)
    assert m.double_fault_rate == 0.0
    assert m.or_rule.recall == 1.0


def test_metrics_refuse_missing_decisions_and_report_empty_rates_as_none() -> None:
    with pytest.raises(ValueError, match="no decision"):
        judge_metrics(GOLD, {"r1": False})
    none_relevant = judge_metrics({"r1": False}, {"r1": True})
    assert none_relevant.recall is None
    assert none_relevant.specificity == 1.0


def test_fleiss_kappa_matches_the_published_example() -> None:
    """Fleiss (1971) as worked on Wikipedia: 10 items, 14 raters, 5 categories, kappa 0.210."""
    counts = [
        (0, 0, 0, 0, 14),
        (0, 2, 6, 4, 2),
        (0, 0, 3, 5, 6),
        (0, 3, 9, 2, 0),
        (2, 2, 8, 1, 1),
        (7, 7, 0, 0, 0),
        (3, 2, 6, 3, 0),
        (2, 5, 3, 2, 2),
        (6, 5, 2, 1, 0),
        (0, 2, 2, 3, 7),
    ]
    ratings = [[cat for cat, n in enumerate(row) for _ in range(n)] for row in counts]
    assert fleiss_kappa(ratings) == pytest.approx(0.2099, abs=1e-4)
    stable = [["not_met"] * 3, ["met"] * 3, ["unclear", "unclear", "met"]]
    assert fleiss_kappa(stable) == pytest.approx(34 / 52)  # P = 7/9, Pe = 29/81
