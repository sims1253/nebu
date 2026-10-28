"""The extraction schema: what to extract from a report, and where Janus should look for it.

A schema lists arms, comparisons, and endpoints. Each endpoint has timepoints and quantities;
each quantity applies to arms (counts, means) or to comparisons (effect estimates, CI bounds,
p-values). Expanding the schema gives one `SchemaField` per semantic key, with its `FieldSpec`
and an optional Janus locator. Locator hints (`rows`, `sections`, `columns`, `cell`) may be set on
an endpoint, a timepoint, or a quantity; the most specific one wins.

    schema = load_schema(yaml_text)
    fields = schema.fields(study_id, report_id)

The quantity catalogue below fixes each quantity's kind, unit, and default policy. A schema may
raise a policy but never lower a `critical` one.
"""

from typing import Literal

import yaml
from pydantic import Field, model_validator

from kittum.core import Id, KittumError, Record, content_hash
from kittum.extraction.model import FieldSpec, Policy, SemanticKey

type Kind = Literal["count", "decimal", "percentage", "ratio", "p_value", "text"]
type Role = Literal["outcome", "baseline", "descriptive"]
type CellShape = Literal[
    "count_percentage.count",
    "count_percentage.percentage",
    "estimate_interval.estimate",
    "estimate_interval.lower",
    "estimate_interval.upper",
    "mean_standard_deviation.mean",
    "mean_standard_deviation.standard_deviation",
]

TOTAL_ARM = "total"


class Quantity(Record):
    kind: Kind
    unit: str | None  # None: the schema must give the unit (a mean, a difference)
    description: str
    estimate: bool = False  # an effect estimate that CI bounds belong to
    per: Literal["arm", "comparison", "either"] = "arm"


QUANTITIES: dict[str, Quantity] = {
    "n_randomized": Quantity(kind="count", unit="participants", description="Randomized"),
    "n_analysed": Quantity(kind="count", unit="participants", description="Analysed"),
    "events": Quantity(kind="count", unit="participants", description="Participants with event"),
    "percent": Quantity(kind="percentage", unit="%", description="Participants with event, %"),
    "mean": Quantity(kind="decimal", unit=None, description="Mean"),
    "sd": Quantity(kind="decimal", unit=None, description="Standard deviation"),
    "median": Quantity(kind="decimal", unit=None, description="Median"),
    "follow_up": Quantity(kind="decimal", unit=None, description="Follow-up", per="either"),
    "risk_ratio": Quantity(
        kind="ratio", unit="ratio", description="Risk ratio", estimate=True, per="comparison"
    ),
    "odds_ratio": Quantity(
        kind="ratio", unit="ratio", description="Odds ratio", estimate=True, per="comparison"
    ),
    "hazard_ratio": Quantity(
        kind="ratio", unit="ratio", description="Hazard ratio", estimate=True, per="comparison"
    ),
    "mean_difference": Quantity(
        kind="decimal", unit=None, description="Mean difference", estimate=True, per="comparison"
    ),
    "risk_difference": Quantity(
        kind="decimal", unit=None, description="Risk difference", estimate=True, per="comparison"
    ),
    "smd": Quantity(
        kind="decimal",
        unit="SD",
        description="Standardized mean difference",
        estimate=True,
        per="comparison",
    ),
    "ci_lower": Quantity(
        kind="decimal", unit=None, description="Lower 95% CI bound", per="comparison"
    ),
    "ci_upper": Quantity(
        kind="decimal", unit=None, description="Upper 95% CI bound", per="comparison"
    ),
    "p_value": Quantity(kind="p_value", unit=None, description="p-value", per="comparison"),
    "description": Quantity(kind="text", unit=None, description="Description", per="either"),
}

# DESIGN.md, "Extraction": reported results are critical, baseline data standard, prose
# descriptive.
_ROLE_POLICY: dict[Role, Policy] = {
    "outcome": "critical",
    "baseline": "standard",
    "descriptive": "descriptive",
}
_RANK: dict[Policy, int] = {"descriptive": 0, "standard": 1, "critical": 2}
# Counts of participants are critical wherever they appear: they are the denominators.
_ALWAYS_CRITICAL = frozenset({"n_randomized", "n_analysed"})


class SchemaError(KittumError):
    pass


class Locate(Record):
    """Janus locator hints. `rows` are row labels (or text labels); `columns` column headers."""

    strategy: Literal["table_label", "text_label"] = "table_label"
    rows: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    cell: CellShape | None = None

    def over(self, base: Locate | None) -> Locate:
        """These hints, falling back to `base` for any left unset."""
        if base is None:
            return self
        return Locate(
            strategy=self.strategy if "strategy" in self.model_fields_set else base.strategy,
            rows=self.rows or base.rows,
            sections=self.sections or base.sections,
            columns=self.columns or base.columns,
            cell=self.cell or base.cell,
        )


class Arm(Record):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    columns: tuple[str, ...] = ()  # column headers the arm is printed under


class Comparison(Record):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    intervention: str
    comparator: str
    columns: tuple[str, ...] = ()


class QuantitySpec(Record):
    quantity: str
    arms: tuple[str, ...] = ()
    comparisons: tuple[str, ...] = ()
    unit: str | None = None
    policy: Policy | None = None
    description: str | None = None
    locate: Locate | None = None


class Timepoint(Record):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    label: str
    locate: Locate | None = None


class Endpoint(Record):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    role: Role = "outcome"
    unit: str | None = None  # the unit of means and differences on this endpoint
    timepoints: tuple[Timepoint, ...] = Field(min_length=1)
    quantities: tuple[QuantitySpec, ...] = Field(min_length=1)
    locate: Locate | None = None


class SchemaField(Record):
    """One field to extract: its key, its spec, its labels, and where Janus looks for it."""

    key: SemanticKey
    spec: FieldSpec
    kind: Kind
    endpoint_label: str
    timepoint_label: str
    arm_label: str
    locate: Locate | None  # None: the Janus path has no locator for this field

    @property
    def label(self) -> str:
        return f"{self.endpoint_label}, {self.timepoint_label}, {self.arm_label}: {self.spec.description}"


class ExtractionSchema(Record):
    schema_version: Literal[1] = 1
    name: str
    subpopulation: str = "all"
    analysis_set: str = "itt"
    data_cut: str = "primary"
    total_columns: tuple[str, ...] = ("Total",)  # column headers of the all-arms total
    arms: tuple[Arm, ...] = Field(min_length=1)
    comparisons: tuple[Comparison, ...] = ()
    endpoints: tuple[Endpoint, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> ExtractionSchema:
        arms = {a.id for a in self.arms}
        if TOTAL_ARM in arms:
            raise ValueError(f"arm ID {TOTAL_ARM!r} is reserved for the total of all arms")
        comparisons = {c.id for c in self.comparisons}
        if len(arms) != len(self.arms) or len(comparisons) != len(self.comparisons):
            raise ValueError("arm and comparison IDs must be unique")
        if arms & comparisons:
            raise ValueError(
                f"IDs used for both an arm and a comparison: {sorted(arms & comparisons)}"
            )
        for c in self.comparisons:
            if {c.intervention, c.comparator} - arms:
                raise ValueError(f"comparison {c.id} names an unknown arm")
        if len({e.id for e in self.endpoints}) != len(self.endpoints):
            raise ValueError("endpoint IDs must be unique")
        for e in self.endpoints:
            if len({t.id for t in e.timepoints}) != len(e.timepoints):
                raise ValueError(f"endpoint {e.id}: timepoint IDs must be unique")
            estimates: dict[str, list[str]] = {}
            targets: set[tuple[str, str]] = set()
            for q in e.quantities:
                spec = QUANTITIES.get(q.quantity)
                if spec is None:
                    raise ValueError(f"endpoint {e.id}: unknown quantity {q.quantity!r}")
                if not q.arms and not q.comparisons:
                    raise ValueError(f"endpoint {e.id}: {q.quantity} names no arm or comparison")
                if set(q.arms) - arms - {TOTAL_ARM}:
                    raise ValueError(f"endpoint {e.id}: {q.quantity} names an unknown arm")
                if set(q.comparisons) - comparisons:
                    raise ValueError(f"endpoint {e.id}: {q.quantity} names an unknown comparison")
                for target in (*q.arms, *q.comparisons):
                    if (q.quantity, target) in targets:
                        raise ValueError(f"endpoint {e.id}: {q.quantity} names {target} twice")
                    targets.add((q.quantity, target))
                if spec.per == "arm" and q.comparisons:
                    raise ValueError(f"endpoint {e.id}: {q.quantity} is per arm, not comparison")
                if spec.per == "comparison" and q.arms:
                    raise ValueError(f"endpoint {e.id}: {q.quantity} is per comparison, not arm")
                if spec.estimate:
                    for c in q.comparisons:
                        estimates.setdefault(c, []).append(q.quantity)
                if q.policy is not None and _RANK[q.policy] < _RANK[self._policy(e, q)]:
                    raise ValueError(
                        f"endpoint {e.id}: {q.quantity} may not lower its policy below "
                        f"{self._policy(e, q)}"
                    )
            for c, names in estimates.items():
                if len(names) > 1:
                    # CI bounds and p-values are keyed like their estimate, so one per comparison.
                    raise ValueError(
                        f"endpoint {e.id}: comparison {c} has several estimates ({', '.join(names)})"
                    )
        return self

    @staticmethod
    def _policy(endpoint: Endpoint, q: QuantitySpec) -> Policy:
        spec = QUANTITIES[q.quantity]
        if q.quantity in _ALWAYS_CRITICAL:
            return "critical"
        if spec.kind == "text":
            return "descriptive"
        return _ROLE_POLICY[endpoint.role]

    def hash(self) -> str:
        return content_hash(self, domain="kittum.extraction_schema.v1")

    def arm_ids(self) -> tuple[str, ...]:
        return tuple(a.id for a in self.arms)

    def comparison(self, id: str) -> Comparison | None:
        return next((c for c in self.comparisons if c.id == id), None)

    def fields(self, study_id: Id, report_id: Id) -> tuple[SchemaField, ...]:
        """Every field of the schema, in schema order."""
        arms = {a.id: a for a in self.arms}
        comparisons = {c.id: c for c in self.comparisons}
        out: list[SchemaField] = []
        for e in self.endpoints:
            estimate_unit = next(
                (self._unit(e, q) for q in e.quantities if QUANTITIES[q.quantity].estimate), None
            )
            for t in e.timepoints:
                for q in e.quantities:
                    spec = QUANTITIES[q.quantity]
                    unit = q.unit or (
                        estimate_unit if q.quantity in ("ci_lower", "ci_upper") else None
                    )
                    unit = unit or self._unit(e, q)
                    policy = q.policy or self._policy(e, q)
                    for target in (*q.arms, *q.comparisons):
                        if target in arms:
                            arm_label, columns = arms[target].label, arms[target].columns
                        elif target in comparisons:
                            arm_label = comparisons[target].label
                            columns = comparisons[target].columns
                        else:
                            arm_label, columns = "Total", self.total_columns
                        locate = (q.locate or Locate()).over(t.locate).over(e.locate)
                        if not locate.columns and columns:
                            locate = locate.model_copy(update={"columns": columns})
                        out.append(
                            SchemaField(
                                key=SemanticKey(
                                    study_id=study_id,
                                    report_id=report_id,
                                    subpopulation=self.subpopulation,
                                    analysis_set=self.analysis_set,
                                    data_cut=self.data_cut,
                                    arm=target,
                                    endpoint=e.id,
                                    timepoint=t.id,
                                    quantity=q.quantity,
                                ),
                                spec=FieldSpec(
                                    quantity=q.quantity,
                                    unit=unit,
                                    policy=policy,
                                    description=q.description or spec.description,
                                ),
                                kind=spec.kind,
                                endpoint_label=e.label,
                                timepoint_label=t.label,
                                arm_label=arm_label,
                                locate=locate if locate.rows else None,
                            )
                        )
        return tuple(out)

    @staticmethod
    def _unit(endpoint: Endpoint, q: QuantitySpec) -> str | None:
        spec = QUANTITIES[q.quantity]
        if q.unit is not None:
            return q.unit
        if spec.unit is not None:
            return spec.unit
        return endpoint.unit


def load_schema(source: str | dict) -> ExtractionSchema:
    """Parse and check a schema from YAML or JSON text, or from a mapping."""
    try:
        data = yaml.safe_load(source) if isinstance(source, str) else source
        return ExtractionSchema.model_validate(data)
    except (yaml.YAMLError, ValueError) as e:
        raise SchemaError(str(e)) from e


def check_against_outcomes(schema: ExtractionSchema, outcome_ids: set[str]) -> None:
    """Every outcome endpoint must be one of the protocol's outcomes."""
    unknown = sorted(
        e.id for e in schema.endpoints if e.role == "outcome" and e.id not in outcome_ids
    )
    if unknown:
        raise SchemaError(f"endpoints not among the protocol's outcomes: {', '.join(unknown)}")
