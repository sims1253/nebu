"""Checks that run on each path's proposals before reconciliation (DESIGN.md, "Extraction").

Each check names itself on every proposal it looked at, passed or not, so a reviewer sees what was
checked as well as what failed. Only extracted values take part; a missing value is never zero.

- `exact_value`: an exact decimal string of the right kind, with the field's unit.
- `events_at_most_n`: events do not exceed the arm's N (analysed, else randomized).
- `arms_sum_to_total`: per-arm counts add up to the total.
- `ci_brackets_estimate`: lower bound <= estimate <= upper bound, and lower < upper.
- `percent_recomputes`: a percentage matches events / N to its printed precision. A mismatch often
  reveals an ITT and safety population swap.
- `ratio_log_symmetric`: a ratio sits in the middle of its CI on the log scale, within rounding.
"""

from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING

from kittum.extraction.model import Proposal, ValidatorResult
from kittum.extraction.numbers import decimal, well_formed
from kittum.extraction.schema import QUANTITIES, TOTAL_ARM

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kittum.extraction.model import SemanticKey
    from kittum.extraction.schema import ExtractionSchema, SchemaField

_COUNTS = ("n_randomized", "n_analysed", "events")
_SLACK = Decimal("0.05")  # share of the log CI half-width a ratio may sit off-centre


def _half_unit(value: str) -> Decimal:
    """Half a unit in the last printed decimal: "56.3" -> 0.05, "200" -> 0.5."""
    places = len(value.split(".", 1)[1]) if "." in value else 0
    return Decimal(5) / Decimal(10) ** (places + 1)


def _group(key: SemanticKey) -> tuple[str, ...]:
    return (key.subpopulation, key.analysis_set, key.data_cut, key.endpoint, key.timepoint)


def _population(key: SemanticKey) -> tuple[str, ...]:
    return (key.subpopulation, key.analysis_set, key.data_cut)


class _Table:
    """The extracted values of one path, by key."""

    def __init__(self, proposals: Iterable[Proposal]) -> None:
        self.values: dict[tuple, tuple[Proposal, Decimal]] = {}
        for p in proposals:
            number = decimal(p.value) if p.status == "extracted" else None
            if number is not None:
                self.values[(*_group(p.key), p.key.arm, p.key.quantity)] = (p, number)

    def get(
        self, key: SemanticKey, *, arm: str | None = None, quantity: str
    ) -> tuple[Proposal, Decimal] | None:
        return self.values.get((*_group(key), arm or key.arm, quantity))

    def n_for(self, key: SemanticKey) -> tuple[Proposal, Decimal] | None:
        """The arm's denominator: N analysed for this endpoint, else randomized, else the arm's
        N anywhere in the same population."""
        for quantity in ("n_analysed", "n_randomized"):
            found = self.get(key, quantity=quantity)
            if found is not None:
                return found
        for quantity in ("n_analysed", "n_randomized"):
            for (*rest, arm, q), found in self.values.items():
                if q == quantity and arm == key.arm and tuple(rest[:3]) == _population(key):
                    return found
        return None


def validate(
    proposals: Sequence[Proposal], fields: Sequence[SchemaField], schema: ExtractionSchema
) -> list[Proposal]:
    """The proposals with their validator results attached, in the same order."""
    kinds = {f.key: f for f in fields}
    results: dict[int, list[ValidatorResult]] = defaultdict(list)
    index = {id(p): i for i, p in enumerate(proposals)}
    table = _Table(proposals)

    def record(name: str, involved: Iterable[Proposal], problem: str | None) -> None:
        for p in involved:
            results[index[id(p)]].append(
                ValidatorResult(validator=name, passed=problem is None, message=problem)
            )

    for p in proposals:
        if p.status != "extracted":
            continue
        field = kinds.get(p.key)
        problem = None
        if field is not None:
            problem = well_formed(p.value, field.kind)
            if p.value is None:
                problem = "an extracted field has no value"
            elif problem is None and p.unit != field.spec.unit:
                problem = f"unit {p.unit!r} is not the field's unit {field.spec.unit!r}"
        if problem is None and not p.anchor_ids:
            problem = "an extracted value has no source anchor"
        record("exact_value", [p], problem)

    for (*_, arm, quantity), (p, events) in list(table.values.items()):
        if quantity == "events":
            n = table.n_for(p.key)
            if n is not None:
                problem = None if events <= n[1] else f"{events} events exceed N = {n[1]}"
                record("events_at_most_n", [p, n[0]], problem)
        if quantity == "percent":
            ev, n = table.get(p.key, quantity="events"), table.n_for(p.key)
            if ev is not None and n is not None and n[1] > 0:
                recomputed = ev[1] / n[1] * 100
                tolerance = _half_unit(p.value or "") + Decimal("1e-9")
                problem = None
                if abs(recomputed - events) > tolerance:
                    problem = (
                        f"{ev[1]} / {n[1]} is {recomputed:.2f}%, not {events}%: "
                        "check whether N and the events come from the same population"
                    )
                record("percent_recomputes", [p, ev[0], n[0]], problem)
        if arm == TOTAL_ARM and quantity in _COUNTS:
            parts = [table.get(p.key, arm=a, quantity=quantity) for a in schema.arm_ids()]
            if all(part is not None for part in parts):
                found = [part for part in parts if part is not None]
                total = sum((v for _, v in found), Decimal(0))
                problem = None if total == events else f"the arms add up to {total}, not {events}"
                record("arms_sum_to_total", [p, *(q for q, _ in found)], problem)
        spec = QUANTITIES.get(quantity)
        if spec is not None and spec.estimate:
            lower, upper = (
                table.get(p.key, quantity="ci_lower"),
                table.get(p.key, quantity="ci_upper"),
            )
            if lower is None or upper is None:
                continue
            estimate, lo, hi = events, lower[1], upper[1]
            problem = None
            if not lo < hi:
                problem = f"the CI ({lo}, {hi}) is empty or reversed"
            elif not lo <= estimate <= hi:
                problem = f"the CI ({lo}, {hi}) does not contain the estimate {estimate}"
            record("ci_brackets_estimate", [p, lower[0], upper[0]], problem)
            if spec.kind == "ratio" and problem is None and lo > 0:
                record(
                    "ratio_log_symmetric",
                    [p, lower[0], upper[0]],
                    _log_symmetric(p, lower[0], upper[0]),
                )

    return [
        p.model_copy(update={"validators": (*p.validators, *results[i])}) if results[i] else p
        for i, p in enumerate(proposals)
    ]


def _log_symmetric(estimate: Proposal, lower: Proposal, upper: Proposal) -> str | None:
    e, lo, hi = (Decimal(p.value or "0") for p in (estimate, lower, upper))
    centre = (lo.ln() + hi.ln()) / 2
    half_width = (hi.ln() - lo.ln()) / 2
    # Rounding each printed value moves its logarithm by at most half a unit over the value.
    rounding = (
        _half_unit(estimate.value or "") / e
        + (_half_unit(lower.value or "") / lo + _half_unit(upper.value or "") / hi) / 2
    )
    off = abs(e.ln() - centre)
    if off <= rounding + _SLACK * half_width:
        return None
    return (
        f"the ratio {e} is off the centre of its CI on the log scale by {off:.4f} "
        f"(half-width {half_width:.4f}): check the estimate and both bounds"
    )
