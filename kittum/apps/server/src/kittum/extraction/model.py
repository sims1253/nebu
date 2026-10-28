"""The values `extraction` hands to other modules.

A value is identified by its full `SemanticKey`, never by a table position or a JSON pointer.
Values are exact decimal strings with an explicit unit; `None` means not reported, never zero.
"""

from datetime import datetime
from typing import Literal

from kittum.core import Id, Record, Sha256

type Policy = Literal["critical", "standard", "descriptive"]
type ProposalStatus = Literal["extracted", "missing", "ambiguous", "invalid", "error"]
type Path = Literal["janus", "llm"]
type Agreement = Literal["agree", "disagree", "janus_only", "llm_only", "neither"]
type Gate = Literal["human_verify", "group_accept", "human_review"]
"""What a field needs before it is accepted (DESIGN.md, "Extraction"):

- `human_verify`: a human checks the value against the highlighted source, one field at a time.
- `group_accept`: a `standard` field whose paths agree and validate; a human may accept it with
  others after viewing the list.
- `human_review`: a `descriptive` field; one human reviews the value and its span.
"""


class SemanticKey(Record):
    study_id: Id
    report_id: Id
    subpopulation: str
    analysis_set: str
    data_cut: str
    arm: str  # an arm ID, a comparison ID, or "total"
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
    path: Path
    anchor_ids: tuple[Id, ...]
    validators: tuple[ValidatorResult, ...] = ()
    message: str | None = None  # why a value is missing, ambiguous, invalid, or an error
    quote: str | None = None  # the LLM path's verbatim quote, or the Janus cell text


class ProposalSet(Record):
    layer_id: Id
    proposals: tuple[Proposal, ...]


class Reconciliation(Record):
    """Both paths' proposals for one key, compared, with the policy's gate."""

    key: SemanticKey
    spec: FieldSpec
    agreement: Agreement
    gate: Gate
    second_extractor: bool  # a critical field whose paths disagree needs an independent human
    recheck: bool  # a standard field drawn for the 10% re-check queue if accepted as a group


class Acceptance(Record):
    """A human's acceptance of one field: the value, its source span, and how it was reached."""

    id: Id
    field_id: Id
    run_id: Id
    key: SemanticKey
    path: Path  # the path whose proposal the reviewer accepted or edited
    edited: bool
    group: bool  # accepted with other agreeing, validated standard fields
    value: str | None
    unit: str | None
    anchor_id: Id
    reviewer_id: Id
    source_viewed_ms: int
    accepted_at: datetime
    recheck: bool
    event_seq: int


class Assertion(Record):
    """An accepted value as reports use it: the latest acceptance per semantic key."""

    key: SemanticKey
    value: str | None
    unit: str | None
    policy: Policy
    anchor_id: Id
    acceptance_id: Id
    run_id: Id
    layer_id: Id
    document_hash: Sha256
