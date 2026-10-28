"""Statistics for recall claims and agreement (DESIGN.md, "Statistics").

Pure functions over counts. No I/O, no randomness without an explicit seed.

Owner: WP-13. Shipped now: the signatures. Still to build: all of them, with scipy for exact
intervals and the hypergeometric test, and tests against published worked examples.
"""

from kittum.core import Record


class Interval(Record):
    estimate: float
    lower: float
    upper: float
    confidence: float
    method: str


class Stratum(Record):
    """A sampling stratum for the elusion audit."""

    population: int
    sampled: int
    relevant_found: int


def clopper_pearson(successes: int, trials: int, *, confidence: float = 0.95) -> Interval:
    raise NotImplementedError("WP-13")


def elusion_estimate(strata: tuple[Stratum, ...], *, confidence: float = 0.95) -> Interval:
    """Horvitz-Thompson estimate of relevant records among AI-excluded ones."""
    raise NotImplementedError("WP-13")


def stop_test(
    screened: int, relevant_found: int, remaining: int, *, target_recall: float, alpha: float
) -> bool:
    """Hypergeometric stopping test for prioritized screening (mode M3)."""
    raise NotImplementedError("WP-13")


def capture_recapture(counts: dict[frozenset[str], int]) -> Interval:
    """Estimate the total number of relevant records from overlapping sources."""
    raise NotImplementedError("WP-13")


def cohen_kappa(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    raise NotImplementedError("WP-13")


def pabak(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    raise NotImplementedError("WP-13")


__all__ = [
    "Interval",
    "Stratum",
    "capture_recapture",
    "clopper_pearson",
    "cohen_kappa",
    "elusion_estimate",
    "pabak",
    "stop_test",
]
