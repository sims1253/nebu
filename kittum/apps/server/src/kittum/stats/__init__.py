"""Statistics for recall claims and agreement (DESIGN.md, "Statistics").

Pure functions over counts. No I/O, no randomness without an explicit seed.

Owner: WP-13. Every interval is exact or deliberately conservative, never a Wald interval.
The methods and their sources are named in each docstring so a reviewer can check the claim
without reading the implementation.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import TYPE_CHECKING

from scipy.linalg import inv
from scipy.optimize import minimize
from scipy.stats import beta, binom, hypergeom, norm

from kittum.core import Record

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Literal

    Sided = Literal["one", "two"]

__all__ = [
    "Interval",
    "StopDecision",
    "Stratum",
    "Threshold",
    "capture_recapture",
    "clopper_pearson",
    "cohen_kappa",
    "conformal_threshold",
    "elusion_estimate",
    "fleiss_kappa",
    "pabak",
    "recall_needed",
    "stop_test",
]


class Interval(Record):
    """A point estimate with an interval."""

    estimate: float
    lower: float
    upper: float
    confidence: float
    method: str


class Stratum(Record):
    """A sampling stratum for the elusion audit.

    ``population`` is every AI-excluded record in the stratum, ``sampled`` the number drawn
    without replacement for the audit, and ``relevant_found`` how many of those the human
    audit found relevant.
    """

    population: int
    sampled: int
    relevant_found: int


class StopDecision(Record):
    """The outcome of the prioritized-screening stopping test."""

    stop: bool
    p_value: float
    target_recall: float
    alpha: float
    method: str


class Threshold(Record):
    """A conformal score threshold and the order statistic it came from."""

    threshold: float
    target_recall: float
    alpha: float
    calibration_size: int
    order_statistic: int
    method: str


def _check_proportion(value: float, name: str) -> None:
    if not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be strictly between 0 and 1, got {value}")


def _check_sided(sided: str) -> None:
    if sided not in ("one", "two"):
        raise ValueError(f"sided must be 'one' or 'two', got {sided!r}")


def _check_counts(successes: int, trials: int) -> None:
    if trials < 1:
        raise ValueError(f"trials must be at least 1, got {trials}")
    if not 0 <= successes <= trials:
        raise ValueError(f"successes must be between 0 and trials, got {successes} of {trials}")


def _exact_bounds(successes: int, trials: int, *, tail: float) -> tuple[float, float]:
    """Clopper-Pearson bounds with ``tail`` probability in each open end.

    ``tail`` is the one-sided tail probability. A two-sided interval at confidence
    ``1 - alpha`` uses ``tail = alpha / 2``; a one-sided lower bound uses ``tail = alpha``.
    """

    lower = 0.0 if successes == 0 else float(beta.ppf(tail, successes, trials - successes + 1))
    upper = (
        1.0
        if successes == trials
        else float(beta.ppf(1.0 - tail, successes + 1, trials - successes))
    )
    return lower, upper


def clopper_pearson(
    successes: int, trials: int, *, confidence: float = 0.95, sided: Sided = "one"
) -> Interval:
    """Exact Clopper-Pearson binomial interval (Clopper and Pearson 1934, *Biometrika* 26).

    ``sided="one"`` is the default convention in Kittum: each endpoint is a one-sided 95%
    bound. For 10 of 10 successes the lower bound is ``0.05 ** (1/10) = 0.741``; for 0 of 10
    the upper bound is ``1 - 0.05 ** (1/10) = 0.259``. Read the endpoint the claim needs; the
    two endpoints are not a single 95% two-sided interval. ``sided="two"`` gives the
    equal-tailed interval, whose lower bound for 10 of 10 is ``0.025 ** (1/10) = 0.692``, the
    figure quoted in DESIGN.md and the WP-13 acceptance note.
    """

    _check_proportion(confidence, "confidence")
    _check_sided(sided)
    _check_counts(successes, trials)

    tail = 1.0 - confidence if sided == "one" else (1.0 - confidence) / 2.0
    lower, upper = _exact_bounds(successes, trials, tail=tail)
    return Interval(
        estimate=successes / trials,
        lower=lower,
        upper=upper,
        confidence=confidence,
        method=f"Clopper-Pearson exact, {sided}-sided",
    )


def recall_needed(target_recall: float, *, confidence: float = 0.95, sided: Sided = "one") -> int:
    """Relevant records with zero misses that certify recall of at least ``target_recall``.

    With no misses the exact lower bound is ``alpha ** (1/n)``, where ``alpha`` is the tail
    probability (Clopper and Pearson 1934). For a one-sided 95% bound
    ``alpha = 1 - confidence``; for a two-sided interval ``alpha = (1 - confidence) / 2``.
    The smallest ``n`` is ``ceil(log(alpha) / log(target_recall))``. A target of 0.975 at
    one-sided 95% confidence needs 119 records (``0.975 ** 119 = 0.049 < 0.05``); a two-sided
    95% interval needs 146.
    """

    _check_proportion(target_recall, "target_recall")
    _check_proportion(confidence, "confidence")
    _check_sided(sided)
    alpha = 1.0 - confidence if sided == "one" else (1.0 - confidence) / 2.0
    return max(1, math.ceil(math.log(alpha) / math.log(target_recall)))


def _hypergeometric_bounds(
    population: int, sampled: int, found: int, alpha: float
) -> tuple[int, int]:
    """One-sided exact bounds for a relevant count in a finite population.

    Sampling is without replacement, so the sample count is hypergeometric, not binomial. The
    lower bound is the smallest population count ``K`` whose upper tail
    ``P(X >= found | N, K, n)`` exceeds ``alpha``; the upper bound is the largest ``K`` whose
    lower tail ``P(X <= found | N, K, n)`` exceeds ``alpha``. Both are integral and are the
    finite-population analogue of inverting the Clopper-Pearson tails (Clopper and Pearson
    1934).
    """

    def upper_tail(relevant: int) -> float:
        return float(hypergeom.sf(found - 1, population, relevant, sampled))

    def lower_tail(relevant: int) -> float:
        return float(hypergeom.cdf(found, population, relevant, sampled))

    low, high = found, population
    while low < high:
        middle = (low + high) // 2
        if upper_tail(middle) > alpha:
            high = middle
        else:
            low = middle + 1
    lower_bound = low

    if lower_tail(population) > alpha:
        upper_bound = population
    else:
        low, high = found, population
        while low < high:
            middle = (low + high) // 2
            if lower_tail(middle) <= alpha:
                high = middle
            else:
                low = middle + 1
        upper_bound = low - 1

    return lower_bound, upper_bound


def elusion_estimate(strata: Sequence[Stratum], *, confidence: float = 0.95) -> Interval:
    """Horvitz-Thompson estimate of relevant records among AI-excluded ones.

    Each stratum is a simple random sample without replacement from a known population. The
    point estimate is the stratified Horvitz-Thompson total
    ``sum(N_h * y_h / n_h)`` (Horvitz and Thompson 1952, *JASA* 47). Bounds are simultaneous
    one-sided exact bounds from inverting each stratum's hypergeometric distribution at
    ``1 - alpha/H``, so a union bound over the ``H`` strata covers each side of the total with
    probability at least ``1 - alpha``. The upper bound is the one that certifies how many
    relevant records the AI could have missed. This is deliberately conservative, as
    DESIGN.md requires; a census stratum (``sampled == population``) is exact. A binomial
    bound would be wrong here because the audit draws records without replacement.
    """

    _check_proportion(confidence, "confidence")
    if not strata:
        return Interval(
            estimate=0.0,
            lower=0.0,
            upper=0.0,
            confidence=confidence,
            method="stratified Horvitz-Thompson with simultaneous exact hypergeometric bounds",
        )

    alpha = 1.0 - confidence
    tail = alpha / len(strata)
    estimate = 0.0
    lower = 0.0
    upper = 0.0
    for stratum in strata:
        n_h = stratum.population
        n = stratum.sampled
        y = stratum.relevant_found
        if n_h < 1:
            if n != 0 or y != 0:
                raise ValueError("a stratum with no population cannot have a sample")
            continue
        if n < 1:
            raise ValueError(f"stratum of {n_h} records has no audit sample")
        if n > n_h:
            raise ValueError(f"sampled {n} records from a stratum of {n_h}")
        if not 0 <= y <= n:
            raise ValueError(f"relevant_found {y} is not between 0 and sampled {n}")

        estimate += n_h * y / n
        if n == n_h:
            lower += y
            upper += y
        else:
            low_k, high_k = _hypergeometric_bounds(n_h, n, y, tail)
            lower += low_k
            upper += high_k

    return Interval(
        estimate=estimate,
        lower=lower,
        upper=upper,
        confidence=confidence,
        method="stratified Horvitz-Thompson with simultaneous exact hypergeometric bounds",
    )


def _hypergeometric_stop_p(
    labels: Sequence[bool], total_records: int, target_recall: float
) -> float:
    """Callaghan and Müller-Hansen's p-score for one ordered screening sequence.

    This follows the reference implementation (buscarR/buscarpy ``calculate_h0``): for every
    suffix of the screened sequence, ask how likely that suffix is if the population held just
    enough relevant records to miss the target, and take the smallest p-value.
    """

    screened = len(labels)
    relevant_seen = sum(labels)
    # If every remaining record were irrelevant, recall is still at or above target.
    if relevant_seen / total_records >= target_recall:
        return 0.0

    suffix_relevant: list[int] = []
    suffix_sizes: list[int] = []
    populations: list[int] = []
    relevant_in_urn: list[int] = []
    counts = 0
    for suffix in range(1, screened + 1):
        counts += labels[-suffix]
        relevant_before = relevant_seen - counts
        # Smallest total relevant count that would miss the target, minus those already seen.
        urn = math.floor(relevant_seen / target_recall - relevant_before + 1)
        population = total_records - (screened - suffix)
        if 0 <= urn <= population and suffix <= population:
            suffix_relevant.append(counts)
            suffix_sizes.append(suffix)
            populations.append(population)
            relevant_in_urn.append(urn)
    if not suffix_relevant:
        return 1.0
    probabilities = hypergeom.cdf(suffix_relevant, populations, relevant_in_urn, suffix_sizes)
    return float(min(probabilities))


def stop_test(
    screened: int,
    relevant_found: int,
    remaining: int,
    *,
    target_recall: float,
    alpha: float,
    order: Sequence[bool] | None = None,
) -> StopDecision:
    """Hypergeometric stopping test for prioritized screening (mode M3).

    Callaghan and Müller-Hansen (2020), "Statistical stopping criteria for automated screening
    in systematic reviews", *Systematic Reviews* 9:273. The null hypothesis is that the true
    recall is below ``target_recall``; a p-value below ``alpha`` rejects it, so screening may
    stop and recall at or above the target may be claimed at level ``alpha``.

    Pass ``order`` with the inclusive/exclusive decision for each screened record, in screening
    order, to reproduce the reference implementation exactly: it takes the smallest
    hypergeometric p-value over every suffix of the sequence. Without ``order`` the test uses
    the whole screened sequence only. That p-value is never smaller than the reference's
    (which minimises over suffixes), so the count-only form is conservative and stops no
    earlier.
    """

    _check_proportion(target_recall, "target_recall")
    _check_proportion(alpha, "alpha")
    if screened < 0 or remaining < 0:
        raise ValueError("screened and remaining must not be negative")
    if not 0 <= relevant_found <= screened:
        raise ValueError(f"relevant_found {relevant_found} is not between 0 and {screened}")

    total = screened + remaining
    if total < 1:
        raise ValueError("there are no records to screen")
    if remaining == 0:
        # Nothing is unscreened, so recall is one by construction.
        return StopDecision(
            stop=True,
            p_value=0.0,
            target_recall=target_recall,
            alpha=alpha,
            method="hypergeometric stopping test (all records screened)",
        )

    if order is not None:
        labels = tuple(bool(value) for value in order)
        if len(labels) != screened:
            raise ValueError(f"order has {len(labels)} records, screened is {screened}")
        if sum(labels) != relevant_found:
            raise ValueError(
                f"order contains {sum(labels)} relevant records, relevant_found is {relevant_found}"
            )
        p_value = _hypergeometric_stop_p(labels, total, target_recall)
        method = "Callaghan-Muller-Hansen hypergeometric, minimum over screening suffixes"
    else:
        if relevant_found / total >= target_recall:
            p_value = 0.0
        else:
            relevant_in_urn = math.floor(relevant_found / target_recall) + 1
            p_value = float(hypergeom.cdf(relevant_found, total, relevant_in_urn, screened))
        method = "Callaghan-Muller-Hansen hypergeometric, whole screened sequence"

    return StopDecision(
        stop=p_value < alpha,
        p_value=p_value,
        target_recall=target_recall,
        alpha=alpha,
        method=method,
    )


def _chapman_interval(n1: int, n2: int, overlap: int, confidence: float) -> Interval:
    """Chapman's two-source estimator with an exact hypergeometric interval.

    Conditional on the source sizes, the overlap is ``M ~ Hypergeometric(N, n1, n2)``. The
    confidence set for the population ``N`` is every value that is not rejected by either tail
    at ``alpha / 2``: ``P(M <= m) > alpha / 2`` and ``P(M >= m) > alpha / 2``. This is the
    finite-population analogue of the Clopper-Pearson interval and is valid for sparse counts,
    where a normal or log-normal approximation has no coverage. If ``m = 0`` the upper end is
    unbounded, because no finite population is excluded by never observing an overlap.
    """

    estimate = (n1 + 1) * (n2 + 1) / (overlap + 1) - 1
    tail = (1.0 - confidence) / 2.0
    minimum = max(n1, n2, n1 + n2 - overlap)

    def lower_tail(population: int) -> float:
        return float(hypergeom.cdf(overlap, population, n1, n2))

    def upper_tail(population: int) -> float:
        if overlap == 0:
            return 1.0
        return float(hypergeom.sf(overlap - 1, population, n1, n2))

    if overlap == 0:
        cap = max(minimum, math.ceil(n1 * n2 / (1.0 - tail)) + 2)
    else:
        # Markov's inequality on the overlap bounds the last population that can be compatible.
        cap = max(minimum, math.ceil(n1 * n2 / (tail * overlap)) + 2)

    if lower_tail(cap) <= tail:
        raise ValueError(
            f"the observed overlap {overlap} is not compatible with any population size "
            f"at confidence {confidence}"
        )

    low, high = minimum, cap
    while low < high:
        middle = (low + high) // 2
        if lower_tail(middle) > tail:
            high = middle
        else:
            low = middle + 1
    lower_bound = low

    if overlap == 0:
        upper_bound = math.inf
    else:
        if upper_tail(minimum) <= tail:
            raise ValueError(
                f"the observed overlap {overlap} is not compatible with any population size "
                f"at confidence {confidence}"
            )
        low, high = minimum, cap
        while low < high:
            middle = (low + high) // 2
            if upper_tail(middle) <= tail:
                high = middle
            else:
                low = middle + 1
        upper_bound = low - 1
        if lower_bound > upper_bound:
            raise ValueError(
                f"the observed overlap {overlap} is not compatible with any population size "
                f"at confidence {confidence}"
            )

    return Interval(
        estimate=float(estimate),
        lower=float(lower_bound),
        upper=float(upper_bound),
        confidence=confidence,
        method="Chapman two-source capture-recapture with exact hypergeometric interval",
    )


def _loglinear_three_source(
    counts: Sequence[int], sources: Sequence[str], confidence: float
) -> Interval:
    """Poisson log-linear model for three sources without a three-way interaction.

    The seven observable cells identify the seven parameters (intercept, three main effects,
    three pairwise interactions), so the unobserved cell is predicted. Fienberg (1972), "The
    multiple recapture census for closed populations and incomplete 2^k contingency tables",
    *Biometrika* 59. The interval uses the observed information for the intercept on the log
    scale, then maps the unobserved cell back to a population total.
    """

    zero_cells = []
    for mask in range(1, 8):
        if counts[mask] == 0:
            present = [
                sources[position]
                for position in range(len(sources))
                if mask & (1 << (len(sources) - 1 - position))
            ]
            zero_cells.append("{" + ", ".join(present) + "}")
    if zero_cells:
        raise ValueError(
            "three-source log-linear model is not estimable: zero observed cell(s) "
            + ", ".join(zero_cells)
            + "; their pairwise interactions are on the boundary"
        )

    # Cells are (i, j, k) in {0,1}^3, the all-zero cell being the unobserved one.
    design: list[list[float]] = []
    observed: list[int] = []
    for i in (0, 1):
        for j in (0, 1):
            for k in (0, 1):
                if i == j == k == 0:
                    continue
                design.append(
                    [1.0, float(i), float(j), float(k), float(i * j), float(i * k), float(j * k)]
                )
                observed.append(counts[(i << 2) | (j << 1) | k])

    def negative_log_likelihood(beta: Sequence[float]) -> float:
        total = 0.0
        for row, count in zip(design, observed, strict=True):
            linear = sum(x * b for x, b in zip(row, beta, strict=True))
            total += math.exp(linear) - count * linear
        return total

    def gradient(beta: Sequence[float]) -> list[float]:
        result = [0.0] * len(beta)
        for row, count in zip(design, observed, strict=True):
            linear = sum(x * b for x, b in zip(row, beta, strict=True))
            fitted = math.exp(linear)
            for index, x in enumerate(row):
                result[index] += x * (fitted - count)
        return result

    fitted = minimize(
        negative_log_likelihood,
        [0.0] * len(design[0]),
        jac=gradient,
        method="L-BFGS-B",
        bounds=[(-50.0, 50.0)] * len(design[0]),
        options={"ftol": 1e-15, "gtol": 1e-12, "maxiter": 10_000},
    )
    if not fitted.success:
        raise ValueError(f"three-source log-linear fit did not converge: {fitted.message}")
    beta_hat = [float(value) for value in fitted.x]
    if not all(math.isfinite(value) for value in beta_hat):
        raise ValueError("three-source log-linear model is not estimable: a parameter diverged")
    if max(abs(value) for value in beta_hat) >= 49.0:
        raise ValueError(
            "three-source log-linear model is not estimable: a parameter reached the search "
            "bound, usually from a zero or near-zero cell"
        )
    means = [math.exp(sum(x * b for x, b in zip(row, beta_hat, strict=True))) for row in design]
    information = [
        [
            sum(x[a] * x[b] * mean for x, mean in zip(design, means, strict=True))
            for b in range(len(beta_hat))
        ]
        for a in range(len(beta_hat))
    ]
    try:
        covariance = inv(information)
    except Exception as error:
        raise ValueError(
            "three-source log-linear model is not estimable; the sources need pairwise overlap"
        ) from error
    intercept_variance = float(covariance[0][0])
    if not math.isfinite(intercept_variance) or intercept_variance < 0:
        raise ValueError(
            "three-source log-linear model is not estimable; the sources need pairwise overlap"
        )

    observed_total = sum(observed)
    unseen = math.exp(beta_hat[0])
    z = float(norm.ppf(1.0 - (1.0 - confidence) / 2.0))
    se = math.sqrt(intercept_variance)
    if not math.isfinite(se) or beta_hat[0] + z * se > 700.0:
        raise ValueError(
            "three-source log-linear model is not estimable: the predicted unseen cell diverges"
        )
    return Interval(
        estimate=float(observed_total + unseen),
        lower=float(observed_total + math.exp(beta_hat[0] - z * se)),
        upper=float(observed_total + math.exp(beta_hat[0] + z * se)),
        confidence=confidence,
        method=(
            "Poisson log-linear capture-recapture without three-way interaction "
            f"(sources: {', '.join(sources)})"
        ),
    )


def capture_recapture(
    counts: Mapping[frozenset[str], int], *, confidence: float = 0.95
) -> Interval:
    """Estimate the total number of relevant records from overlapping sources.

    Two sources use Chapman's bias-corrected estimator (Chapman 1951, *University of
    California Publications in Statistics* 1) with an exact interval obtained by inverting the
    hypergeometric distribution of the overlap; the upper end is unbounded when the observed
    overlap is zero. Three sources fit a Poisson log-linear model with main effects and pairwise
    interactions but no three-way interaction (Fienberg 1972, *Biometrika* 59), which predicts
    the records no source found. The point estimate is the estimated population total, including
    records all sources caught.
    """

    _check_proportion(confidence, "confidence")
    if not counts:
        raise ValueError("capture-recapture needs at least one observed capture cell")
    for cell, count in counts.items():
        if count < 0:
            raise ValueError(f"capture count for {sorted(cell)} is negative")
        if not cell:
            raise ValueError("the empty capture cell cannot be observed")

    sources = sorted({source for cell in counts for source in cell})
    if len(sources) == 2:
        first, second = sources
        n1 = sum(count for cell, count in counts.items() if first in cell)
        n2 = sum(count for cell, count in counts.items() if second in cell)
        overlap = counts.get(frozenset((first, second)), 0)
        return _chapman_interval(n1, n2, overlap, confidence)

    if len(sources) == 3:
        index = {source: position for position, source in enumerate(sources)}
        n = len(sources)
        cells = [0] * (1 << n)
        for cell, count in counts.items():
            mask = 0
            for source in cell:
                mask |= 1 << (n - 1 - index[source])
            cells[mask] += count
        return _loglinear_three_source(cells, sources, confidence)

    raise ValueError(f"capture-recapture supports two or three sources, found {len(sources)}")


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's kappa for two raters (Cohen 1960, *Educational and Psychological Measurement* 20).

    ``a`` and ``b`` hold one label per item in the same order.
    """

    if len(a) != len(b):
        raise ValueError(f"raters labelled {len(a)} and {len(b)} items")
    if not a:
        raise ValueError("kappa needs at least one item")
    total = len(a)
    observed = sum(left == right for left, right in zip(a, b, strict=True)) / total
    left_counts = Counter(a)
    right_counts = Counter(b)
    expected = sum(
        (left_counts[label] / total) * (right_counts[label] / total)
        for label in set(left_counts) | set(right_counts)
    )
    if math.isclose(expected, 1.0):
        raise ValueError("kappa is undefined when both raters use one label for every item")
    return (observed - expected) / (1.0 - expected)


def pabak(a: Sequence[str], b: Sequence[str]) -> float:
    """Prevalence-adjusted bias-adjusted kappa, ``2 * P(observed) - 1``.

    Byrt, Bishop and Carlin (1993), "Bias, prevalence and kappa", *Journal of Clinical
    Epidemiology* 46. PABAK removes the chance-agreement term that makes kappa fall when one
    label dominates, so it reports raw agreement on the same -1 to 1 scale.
    """

    if len(a) != len(b):
        raise ValueError(f"raters labelled {len(a)} and {len(b)} items")
    if not a:
        raise ValueError("PABAK needs at least one item")
    observed = sum(left == right for left, right in zip(a, b, strict=True)) / len(a)
    return 2.0 * observed - 1.0


def fleiss_kappa(ratings: Sequence[Sequence[str]]) -> float:
    """Fleiss' kappa for a fixed number of three or more runs (Fleiss 1971, *Psychological
    Bulletin* 76).

    ``ratings[i]`` holds the label every run gave item ``i``. Use it for run-to-run agreement
    over three or more judge or model runs, where Cohen's kappa only handles two.
    """

    if not ratings:
        raise ValueError("Fleiss' kappa needs at least one item")
    runs = len(ratings[0])
    if runs < 3:
        raise ValueError("Fleiss' kappa needs at least three ratings per item")
    if any(len(row) != runs for row in ratings):
        raise ValueError("every item must have the same number of ratings")

    items = len(ratings)
    observed_per_item = []
    totals: Counter[str] = Counter()
    for row in ratings:
        row_counts = Counter(row)
        totals.update(row_counts)
        pairs = sum(count * (count - 1) for count in row_counts.values())
        observed_per_item.append(pairs / (runs * (runs - 1)))
    observed = sum(observed_per_item) / items
    expected = sum((count / (items * runs)) ** 2 for count in totals.values())
    if math.isclose(expected, 1.0):
        raise ValueError("Fleiss' kappa is undefined when every run uses one label")
    return (observed - expected) / (1.0 - expected)


def conformal_threshold(
    scores: Sequence[float], *, target_recall: float, alpha: float
) -> Threshold:
    """Split-conformal score threshold that keeps recall at or above ``target_recall``.

    ``scores`` are the calibration scores of known-relevant records, ordered however; a higher
    score means the record is screened earlier. Taking the ``k``-th smallest score, where
    ``k`` is the largest order statistic with ``P(Binomial(n, 1 - target_recall) <= k - 1) <=
    alpha``, gives a distribution-free lower confidence bound on the target quantile (Vovk,
    Gammerman and Shafer 2005, *Algorithmic Learning in a Random World*; Angelopoulos and
    Bates 2021, "A gentle introduction to conformal prediction", arXiv:2107.07511). With
    probability at least ``1 - alpha`` over the calibration draw, a new relevant record scores
    at or above the threshold with probability at least ``target_recall``.

    With no misses in calibration (``k = 1``) the guarantee reduces to
    ``target_recall ** n <= alpha``, so ``recall_needed`` gives the calibration size.
    """

    _check_proportion(target_recall, "target_recall")
    _check_proportion(alpha, "alpha")
    if not scores:
        raise ValueError("conformal calibration needs at least one score")
    if any(math.isnan(score) for score in scores):
        raise ValueError("calibration scores must not be NaN")

    calibration_size = len(scores)
    low_tail = 1.0 - target_recall
    # Largest m with P(Binomial(n, low_tail) <= m) <= alpha; the threshold follows it.
    candidate = int(binom.ppf(alpha, calibration_size, low_tail))
    if candidate < 0:
        below = -1
    elif float(binom.cdf(candidate, calibration_size, low_tail)) <= alpha:
        below = candidate
    else:
        below = candidate - 1
    if below < 0:
        needed = recall_needed(target_recall, confidence=1.0 - alpha)
        raise ValueError(
            f"{calibration_size} calibration scores cannot certify recall {target_recall} "
            f"at confidence {1.0 - alpha}; at least {needed} are needed"
        )
    order_statistic = below + 1
    threshold = sorted(float(score) for score in scores)[order_statistic - 1]
    return Threshold(
        threshold=threshold,
        target_recall=target_recall,
        alpha=alpha,
        calibration_size=calibration_size,
        order_statistic=order_statistic,
        method="split-conformal order statistic with binomial coverage guarantee",
    )
