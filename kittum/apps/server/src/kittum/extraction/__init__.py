"""Extraction of study data through two independent paths (DESIGN.md, "Extraction").

The Janus path reads tables and labelled values deterministically. The LLM path is a bounded
agent with read-only tools that returns values with verbatim quotes. Both paths produce
`Proposal`s keyed by a full `SemanticKey`; validators run on both; reconciliation compares them.

Owner: WP-11. Shipped now: the types. Still to build: the Janus adapter (layer to Janus
`DocumentEvidence`, specification per endpoint schema), the PydanticAI agent, validators, and
reconciliation.
"""

from typing import TYPE_CHECKING, Literal

from kittum.core import Id, Record

if TYPE_CHECKING:
    from kittum.documents import Layer

type Policy = Literal["critical", "standard", "descriptive"]
type ProposalStatus = Literal["extracted", "missing", "ambiguous", "invalid", "error"]


class SemanticKey(Record):
    study_id: Id
    report_id: Id
    subpopulation: str
    analysis_set: str
    data_cut: str
    arm: str
    endpoint: str
    timepoint: str
    quantity: str  # e.g. "n_randomized", "events", "hazard_ratio", "ci_lower"


class FieldSpec(Record):
    quantity: str
    unit: str | None
    policy: Policy
    description: str


class ValidatorResult(Record):
    validator: str
    passed: bool
    message: str | None = None


class Proposal(Record):
    key: SemanticKey
    value: str | None  # exact decimal as text; empty is not zero
    unit: str | None
    status: ProposalStatus
    path: Literal["janus", "llm"]
    anchor_ids: tuple[Id, ...]
    validators: tuple[ValidatorResult, ...] = ()


class ProposalSet(Record):
    layer_id: Id
    proposals: tuple[Proposal, ...]


def extract(layer: Layer, fields: tuple[FieldSpec, ...]) -> ProposalSet:
    raise NotImplementedError("WP-11")


__all__ = [
    "FieldSpec",
    "Policy",
    "Proposal",
    "ProposalSet",
    "ProposalStatus",
    "SemanticKey",
    "ValidatorResult",
    "extract",
]
