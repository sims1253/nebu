"""Worked examples and seeded simulations for `kittum.stats` (WP-13).

The reference numbers for the stopping test come from the buscarpy implementation of Callaghan
and Müller-Hansen (2020), ``generate_dataset(random_seed=12345)`` followed by ``calculate_h0``
on the first 10,000 and 12,000 screened records. Buscarpy uses NumPy's PCG64 generator, so the
200 relevant positions are recorded here instead of making NumPy a direct dependency.
"""

import math
import random

import pytest
from scipy.stats import hypergeom

from kittum.stats import (
    Interval,
    Stratum,
    capture_recapture,
    clopper_pearson,
    cohen_kappa,
    conformal_threshold,
    elusion_estimate,
    fleiss_kappa,
    pabak,
    recall_needed,
    stop_test,
)

# Relevant record positions in buscarpy's generate_dataset(N=20000, prevalence=0.01, bias=10,
# random_seed=12345). All 200 fall within the first 7,776 records.
BUSCARPY_RELEVANT = (
    20,
    28,
    48,
    51,
    77,
    84,
    90,
    98,
    113,
    123,
    126,
    139,
    144,
    146,
    174,
    176,
    199,
    203,
    205,
    209,
    250,
    254,
    270,
    278,
    286,
    301,
    339,
    341,
    348,
    358,
    360,
    372,
    375,
    403,
    413,
    466,
    478,
    479,
    506,
    507,
    523,
    538,
    556,
    559,
    568,
    597,
    616,
    624,
    643,
    644,
    645,
    658,
    677,
    693,
    705,
    707,
    709,
    719,
    733,
    759,
    764,
    791,
    796,
    827,
    829,
    836,
    853,
    861,
    869,
    878,
    881,
    883,
    898,
    921,
    945,
    958,
    962,
    969,
    970,
    984,
    1010,
    1017,
    1069,
    1078,
    1090,
    1104,
    1125,
    1127,
    1138,
    1141,
    1153,
    1162,
    1163,
    1177,
    1204,
    1220,
    1257,
    1270,
    1281,
    1283,
    1284,
    1295,
    1325,
    1339,
    1367,
    1518,
    1574,
    1579,
    1593,
    1637,
    1681,
    1689,
    1798,
    1860,
    1953,
    1969,
    1981,
    2018,
    2052,
    2060,
    2122,
    2206,
    2217,
    2234,
    2275,
    2305,
    2309,
    2312,
    2322,
    2406,
    2421,
    2453,
    2474,
    2477,
    2507,
    2520,
    2540,
    2542,
    2582,
    2596,
    2608,
    2616,
    2619,
    2648,
    2680,
    2710,
    2790,
    2806,
    2892,
    2940,
    2948,
    3066,
    3070,
    3090,
    3106,
    3155,
    3164,
    3181,
    3251,
    3393,
    3394,
    3399,
    3456,
    3465,
    3483,
    3614,
    3627,
    3651,
    3695,
    3766,
    3853,
    3951,
    3997,
    4042,
    4098,
    4139,
    4195,
    4268,
    4302,
    4382,
    4440,
    4468,
    4491,
    4619,
    4701,
    4726,
    4776,
    4951,
    4954,
    5184,
    5304,
    5317,
    5533,
    5855,
    6128,
    6309,
    6347,
    6541,
    7152,
    7776,
)


def test_clopper_pearson_matches_the_exact_formula() -> None:
    ten_of_ten = clopper_pearson(10, 10)
    assert ten_of_ten.estimate == 1.0
    assert ten_of_ten.lower == pytest.approx(0.05 ** (1 / 10), rel=1e-12)
    assert ten_of_ten.lower == pytest.approx(0.741, abs=5e-4)
    assert ten_of_ten.upper == 1.0

    two_sided = clopper_pearson(10, 10, sided="two")
    assert two_sided.lower == pytest.approx(0.025 ** (1 / 10), rel=1e-12)
    assert two_sided.lower == pytest.approx(0.692, abs=5e-4)
    assert two_sided.upper == 1.0

    # Clopper and Pearson's own example: three successes in ten trials, two-sided 95%.
    three_of_ten = clopper_pearson(3, 10, sided="two")
    assert three_of_ten.lower == pytest.approx(0.0667395, abs=1e-6)
    assert three_of_ten.upper == pytest.approx(0.6524529, abs=1e-6)

    no_successes = clopper_pearson(0, 10)
    assert no_successes.lower == 0.0
    assert no_successes.upper == pytest.approx(1.0 - 0.05 ** (1 / 10), rel=1e-12)


def test_recall_needed_certifies_the_one_sided_975_target() -> None:
    assert recall_needed(0.975) == 119
    assert recall_needed(0.975, sided="two") == 146
    assert recall_needed(0.95) == 59

    # 118 records are one short, 119 exactly clear the one-sided 95% bound.
    assert clopper_pearson(118, 118).lower < 0.975
    assert clopper_pearson(119, 119).lower >= 0.975
    # On a two-sided interval the same target needs 146 records.
    assert clopper_pearson(145, 145, sided="two").lower < 0.975
    assert clopper_pearson(146, 146, sided="two").lower >= 0.975


def test_stop_test_reproduces_the_buscarpy_reference() -> None:
    relevant = set(BUSCARPY_RELEVANT)
    order = tuple(index in relevant for index in range(12_000))

    at_10_000 = stop_test(
        10_000, sum(order[:10_000]), 10_000, target_recall=0.95, alpha=0.05, order=order[:10_000]
    )
    assert at_10_000.p_value == pytest.approx(0.10980177863883214, rel=1e-10)
    assert not at_10_000.stop

    at_12_000 = stop_test(
        12_000, sum(order[:12_000]), 8_000, target_recall=0.95, alpha=0.05, order=order[:12_000]
    )
    assert at_12_000.p_value == pytest.approx(0.009418930806106517, rel=1e-10)
    assert at_12_000.stop

    # Without the order, the test can only use the whole screened sequence. That is a subset of
    # the suffix tests, so its p-value is never smaller and it does not stop early.
    count_only = stop_test(12_000, 200, 8_000, target_recall=0.95, alpha=0.05)
    assert count_only.p_value >= at_12_000.p_value
    assert not count_only.stop


def test_stop_test_false_stop_rate_stays_within_alpha() -> None:
    """Under random (unprioritised) order, a stop must rarely overstate recall."""

    rng = random.Random(20261028)
    total_records = 1_000
    true_relevant = 100
    target = 0.95
    alpha = 0.05
    stops = 0
    false_stops = 0

    for _ in range(500):
        labels = [True] * true_relevant + [False] * (total_records - true_relevant)
        rng.shuffle(labels)
        found = 0
        for screened in range(1, total_records + 1):
            found += labels[screened - 1]
            if screened % 50:
                continue
            decision = stop_test(
                screened,
                found,
                total_records - screened,
                target_recall=target,
                alpha=alpha,
                order=labels[:screened],
            )
            if decision.stop:
                stops += 1
                if found / true_relevant < target:
                    false_stops += 1
                break

    assert stops > 400, "the simulation should reach a stopping decision"
    assert false_stops / stops <= alpha


def test_elusion_estimate_covers_the_stratified_miss_total() -> None:
    worked = elusion_estimate(
        (
            Stratum(population=1_000, sampled=100, relevant_found=4),
            Stratum(population=500, sampled=50, relevant_found=1),
        )
    )
    assert worked.estimate == pytest.approx(50.0)
    assert worked.lower < worked.estimate < worked.upper

    census = elusion_estimate((Stratum(population=100, sampled=100, relevant_found=7),))
    assert census == Interval(
        estimate=7.0,
        lower=7.0,
        upper=7.0,
        confidence=0.95,
        method="stratified Horvitz-Thompson with simultaneous exact hypergeometric bounds",
    )

    rng = random.Random(5)
    specification = ((1_000, 400, 60), (500, 200, 20), (300, 300, 9), (2_000, 800, 80))
    truth = sum(relevant for _, _, relevant in specification)
    errors = []
    covered = 0
    for _ in range(300):
        strata = []
        for population, sample_size, relevant in specification:
            records = [True] * relevant + [False] * (population - relevant)
            drawn = rng.sample(range(population), sample_size)
            strata.append(
                Stratum(
                    population=population,
                    sampled=sample_size,
                    relevant_found=sum(records[index] for index in drawn),
                )
            )
        interval = elusion_estimate(tuple(strata))
        errors.append(abs(interval.estimate - truth))
        covered += interval.upper >= truth

    assert sum(errors) / len(errors) < 0.2 * truth
    assert covered / len(errors) >= 0.95


def test_elusion_upper_bound_inverts_the_hypergeometric() -> None:
    # Six relevant records in a sample of seven from 137. The binomial bound stops at
    # 135.9998 and excludes 136, but P(X <= 6 | N=137, K=136, n=7) = 7/137 > 0.05.
    interval = elusion_estimate((Stratum(population=137, sampled=7, relevant_found=6),))
    assert interval.upper == 136.0
    assert hypergeom.cdf(6, 137, 136, 7) > 0.05
    assert interval.lower < 136.0


def test_elusion_bounds_cover_every_small_population() -> None:
    """Exhaustive one-sided coverage of the hypergeometric inversion, without replacement."""

    alpha = 0.05
    for population in range(2, 17):
        for sampled in range(1, population):
            bounds = {
                found: elusion_estimate(
                    (Stratum(population=population, sampled=sampled, relevant_found=found),)
                )
                for found in range(0, sampled + 1)
            }
            for relevant in range(population + 1):
                upper_coverage = sum(
                    hypergeom.pmf(found, population, relevant, sampled)
                    for found in range(0, min(sampled, relevant) + 1)
                    if relevant <= bounds[found].upper
                )
                lower_coverage = sum(
                    hypergeom.pmf(found, population, relevant, sampled)
                    for found in range(0, min(sampled, relevant) + 1)
                    if relevant >= bounds[found].lower
                )
                assert upper_coverage >= 1.0 - alpha - 1e-9, (population, sampled, relevant)
                assert lower_coverage >= 1.0 - alpha - 1e-9, (population, sampled, relevant)


def test_capture_recapture_two_sources_recovers_the_population() -> None:
    # Chapman's worked shape: 100 in source one, 80 in source two, 40 in both.
    worked = capture_recapture(
        {
            frozenset({"human1"}): 60,
            frozenset({"human2"}): 40,
            frozenset({"human1", "human2"}): 40,
        }
    )
    assert worked.estimate == pytest.approx(101 * 81 / 41 - 1)
    assert worked.lower <= worked.estimate <= worked.upper

    rng = random.Random(11)
    population = 2_000
    first_probability = 0.5
    second_probability = 0.4
    errors = []
    covered = 0
    for _ in range(300):
        only_first = only_second = both = 0
        for _ in range(population):
            first = rng.random() < first_probability
            second = rng.random() < second_probability
            if first and second:
                both += 1
            elif first:
                only_first += 1
            elif second:
                only_second += 1
        interval = capture_recapture(
            {
                frozenset({"human1"}): only_first,
                frozenset({"human2"}): only_second,
                frozenset({"human1", "human2"}): both,
            }
        )
        errors.append(abs(interval.estimate - population) / population)
        covered += interval.lower <= population <= interval.upper

    assert sum(errors) / len(errors) < 0.05
    assert covered / len(errors) >= 0.90


def test_chapman_interval_handles_sparse_sources() -> None:
    # Two singleton sources that never overlap: no finite population is excluded, so the
    # upper end must be unbounded and must cover a population of 100.
    singleton = capture_recapture({frozenset({"a"}): 1, frozenset({"b"}): 1})
    assert singleton.estimate == 3.0
    assert singleton.lower == 2.0
    assert singleton.upper == math.inf

    # Complete overlap of two singletons used to give a zero-width interval [1, 1].
    overlap = capture_recapture({frozenset({"a", "b"}): 1})
    assert overlap.lower == 1.0
    assert overlap.upper > 1.0

    # At a true population of 100 the two intervals cover in 99% of draws; the old
    # log-normal interval covered in none of them.
    coverage = sum(
        hypergeom.pmf(overlap_count, 100, 1, 1)
        for overlap_count, interval in ((0, singleton), (1, overlap))
        if interval.lower <= 100 <= interval.upper
    )
    assert coverage >= 0.95


def test_chapman_interval_covers_sparse_populations() -> None:
    """Exhaustively check the exact overlap inversion on small, sparse tables."""

    alpha = 0.05
    for n1 in range(1, 8):
        for n2 in range(1, 8):
            intervals = {
                overlap: capture_recapture(
                    {
                        frozenset({"a"}): n1 - overlap,
                        frozenset({"b"}): n2 - overlap,
                        frozenset({"a", "b"}): overlap,
                    }
                )
                for overlap in range(0, min(n1, n2) + 1)
            }
            for population in range(max(n1, n2), 31):
                covered = sum(
                    hypergeom.pmf(overlap, population, n1, n2)
                    for overlap, interval in intervals.items()
                    if interval.lower <= population <= interval.upper
                )
                assert covered >= 1.0 - alpha - 1e-9, (n1, n2, population)


def test_capture_recapture_three_sources_recovers_the_population() -> None:
    """Simulate a closed population from a log-linear model and refit it."""

    def cell_probabilities(coefficients: tuple[float, ...]) -> list[float]:
        weights = []
        for i in (0, 1):
            for j in (0, 1):
                for k in (0, 1):
                    log_weight = (
                        coefficients[0]
                        + coefficients[1] * i
                        + coefficients[2] * j
                        + coefficients[3] * k
                        + coefficients[4] * i * j
                        + coefficients[5] * i * k
                        + coefficients[6] * j * k
                    )
                    weights.append(math.exp(log_weight))
        total = sum(weights)
        return [weight / total for weight in weights]

    true_coefficients = (
        math.log(0.05),
        0.8,
        0.5,
        0.3,
        0.6,
        0.4,
        0.5,
    )
    probabilities = cell_probabilities(true_coefficients)
    rng = random.Random(17)
    population = 20_000
    sources = ("human1", "human2", "ai")
    errors = []
    covered = 0
    for _ in range(40):
        cells = [0] * 8
        for _ in range(population):
            draw = rng.random()
            cumulative = 0.0
            for index, probability in enumerate(probabilities):
                cumulative += probability
                if draw < cumulative:
                    cells[index] += 1
                    break
        counts = {}
        for mask in range(1, 8):
            if cells[mask]:
                counts[
                    frozenset(sources[position] for position in range(3) if mask & (1 << position))
                ] = cells[mask]
        interval = capture_recapture(counts)
        errors.append(abs(interval.estimate - population) / population)
        covered += interval.lower <= population <= interval.upper

    assert sum(errors) / len(errors) < 0.02
    assert max(errors) < 0.05
    assert covered / len(errors) >= 0.85


def test_three_source_model_rejects_zero_cells() -> None:
    # All observable cells are 10 except the {a, b}-only cell. That forces a pairwise
    # interaction onto the boundary; the old code overflowed while forming the interval.
    counts = {
        frozenset({"a"}): 10,
        frozenset({"b"}): 10,
        frozenset({"c"}): 10,
        frozenset({"a", "b"}): 0,
        frozenset({"a", "c"}): 10,
        frozenset({"b", "c"}): 10,
        frozenset({"a", "b", "c"}): 10,
    }
    with pytest.raises(ValueError, match=r"\{a, b\}"):
        capture_recapture(counts)

    sources = ("a", "b", "c")
    for zero_mask in range(1, 8):
        sparse = {
            frozenset(
                source for position, source in enumerate(sources) if mask & (1 << position)
            ): 0 if mask == zero_mask else 10
            for mask in range(1, 8)
        }
        with pytest.raises(ValueError, match="not estimable"):
            capture_recapture(sparse)


def test_conformal_threshold_reaches_its_coverage() -> None:
    rng = random.Random(7)
    target = 0.9
    alpha = 0.05
    covered = 0
    replications = 400
    for _ in range(replications):
        scores = [rng.random() for _ in range(200)]
        chosen = conformal_threshold(scores, target_recall=target, alpha=alpha)
        # With standard-uniform scores the true recall above the threshold is 1 - threshold.
        covered += (1.0 - chosen.threshold) >= target
    assert covered / replications >= 1.0 - alpha

    # A stricter recall target lowers the threshold, never raises it.
    scores = [rng.random() for _ in range(500)]
    strict = conformal_threshold(scores, target_recall=0.975, alpha=0.05)
    loose = conformal_threshold(scores, target_recall=0.9, alpha=0.05)
    assert strict.threshold <= loose.threshold


def test_conformal_threshold_needs_enough_calibration() -> None:
    rng = random.Random(3)
    with pytest.raises(ValueError, match="at least 119"):
        conformal_threshold([rng.random() for _ in range(118)], target_recall=0.975, alpha=0.05)

    scores = [rng.random() for _ in range(119)]
    chosen = conformal_threshold(scores, target_recall=0.975, alpha=0.05)
    assert chosen.order_statistic == 1
    assert chosen.threshold == min(scores)


def test_cohen_kappa_matches_textbook_examples() -> None:
    # Cohen (1960) three-category table: [[44, 5, 1], [7, 20, 3], [2, 3, 8]].
    table = ((44, 5, 1), (7, 20, 3), (2, 3, 8))
    first: list[str] = []
    second: list[str] = []
    for row, counts in enumerate(table):
        for column, count in enumerate(counts):
            first.extend([str(row)] * count)
            second.extend([str(column)] * count)
    assert cohen_kappa(first, second) == pytest.approx(0.6096, abs=5e-4)
    assert pabak(first, second) == pytest.approx(0.5484, abs=5e-4)

    # The common two-by-two teaching table: 35 of 50 agree, chance agreement is one half.
    first = ["yes"] * 20 + ["yes"] * 5 + ["no"] * 10 + ["no"] * 15
    second = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    assert cohen_kappa(first, second) == pytest.approx(0.4)
    assert pabak(first, second) == pytest.approx(0.4)

    assert cohen_kappa(("include", "exclude"), ("include", "exclude")) == 1.0


def test_fleiss_kappa_matches_the_standard_worked_example() -> None:
    # Fleiss (1971) example reproduced on the Fleiss's kappa article: ten items, fourteen
    # runs, five categories, kappa = 0.210.
    table = (
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
    )
    ratings = [
        [str(category) for category, count in enumerate(row) for _ in range(count)] for row in table
    ]
    assert fleiss_kappa(ratings) == pytest.approx(0.210, abs=5e-4)

    assert (
        fleiss_kappa(
            (
                ("include", "include", "include"),
                ("exclude", "exclude", "exclude"),
            )
        )
        == 1.0
    )


def test_statistics_reject_impossible_inputs() -> None:
    with pytest.raises(ValueError):
        elusion_estimate((Stratum(population=10, sampled=11, relevant_found=0),))
    with pytest.raises(ValueError):
        capture_recapture({frozenset({"only-one-source"}): 5})
    with pytest.raises(ValueError):
        cohen_kappa(("a",), ("a", "b"))
    with pytest.raises(ValueError):
        fleiss_kappa((("include", "exclude"),))
    with pytest.raises(ValueError):
        stop_test(100, 101, 900, target_recall=0.95, alpha=0.05)
