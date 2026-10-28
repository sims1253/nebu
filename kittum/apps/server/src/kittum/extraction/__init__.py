"""Extraction of study data through two independent paths (DESIGN.md, "Extraction").

An `ExtractionSchema` (`load_schema`) lists arms, comparisons, and endpoints with their timepoints
and quantities; `schema.fields(study_id, report_id)` expands it to one `SchemaField` per semantic
key, each with a verification policy. `extract` then runs:

- **the Janus path**: the layer as Janus `DocumentEvidence`, one Janus specification for the
  schema, `janus.extraction` locators, typed decimal parsing, and Janus field statuses, each
  extracted value anchored on its layer cell or line with `documents.make_anchor`;
- **the LLM path**: a bounded PydanticAI agent on any `LlmBackend` with read-only tools (search,
  read a page, open a table), a call and a time budget, and a verifier that anchors each quote
  with `documents.quote_spans` and checks the value is printed in it;
- **validators** on each path, and **reconciliation** per key with the policy's gate.

Runs are durable jobs. `request_run` records a run and enqueues `JOB_KIND`; `run_handler` is the
worker's handler, and publishes the proposals with the job. A human then accepts each field
(`accept`, with an optional `Edit` and its span) or a group of agreeing, validated standard
fields (`group_accept`); every acceptance is a ledger event through `kittum.runs.Commands`.
`assertions` returns the accepted values that reports use. The extraction tables join every
handover bundle through a registered exporter.

    schema = load_schema(yaml_text)
    with db.transaction() as tx:
        run = request_run(tx, commands, RunRequest(...), clock=clock)
    worker = Worker(db, queue, {JOB_KIND: run_handler(...)}, name="w1")

Owner: WP-11.
"""

from kittum.extraction.bundle import ExtractionExporter
from kittum.extraction.extract import Extraction, extract
from kittum.extraction.jobs import run_handler
from kittum.extraction.llm_path import PROMPT_VERSION, AgentConfig, BudgetExhausted, Transcript
from kittum.extraction.llm_path import run as run_agent
from kittum.extraction.model import (
    Acceptance,
    Agreement,
    Assertion,
    FieldSpec,
    Gate,
    Path,
    Policy,
    Proposal,
    ProposalSet,
    ProposalStatus,
    Reconciliation,
    SemanticKey,
    ValidatorResult,
)
from kittum.extraction.reconcile import reconcile, reconcile_all
from kittum.extraction.schema import (
    QUANTITIES,
    Arm,
    Comparison,
    Endpoint,
    ExtractionSchema,
    Locate,
    QuantitySpec,
    SchemaError,
    SchemaField,
    Timepoint,
    check_against_outcomes,
    load_schema,
)
from kittum.extraction.store import (
    JOB_KIND,
    MIN_SOURCE_VIEW_MS,
    PROPOSALS_RECORDED,
    RUN_REQUESTED,
    VALUE_ACCEPTED,
    AcceptRequest,
    Edit,
    ExtractionReview,
    ExtractionRun,
    FieldLabels,
    GroupAcceptRequest,
    ReportInfo,
    ReportSummary,
    ReviewField,
    ReviewLayer,
    RunListing,
    RunRequest,
    accept,
    assertions,
    get_run,
    group_accept,
    list_fields,
    list_runs,
    request_run,
    review,
    run_of_field,
)
from kittum.extraction.validators import validate

__all__ = [
    "JOB_KIND",
    "MIN_SOURCE_VIEW_MS",
    "PROMPT_VERSION",
    "PROPOSALS_RECORDED",
    "QUANTITIES",
    "RUN_REQUESTED",
    "VALUE_ACCEPTED",
    "AcceptRequest",
    "Acceptance",
    "AgentConfig",
    "Agreement",
    "Arm",
    "Assertion",
    "BudgetExhausted",
    "Comparison",
    "Edit",
    "Endpoint",
    "Extraction",
    "ExtractionExporter",
    "ExtractionReview",
    "ExtractionRun",
    "ExtractionSchema",
    "FieldLabels",
    "FieldSpec",
    "Gate",
    "GroupAcceptRequest",
    "Locate",
    "Path",
    "Policy",
    "Proposal",
    "ProposalSet",
    "ProposalStatus",
    "QuantitySpec",
    "Reconciliation",
    "ReportInfo",
    "ReportSummary",
    "ReviewField",
    "ReviewLayer",
    "RunListing",
    "RunRequest",
    "SchemaError",
    "SchemaField",
    "SemanticKey",
    "Timepoint",
    "Transcript",
    "ValidatorResult",
    "accept",
    "assertions",
    "check_against_outcomes",
    "extract",
    "get_run",
    "group_accept",
    "list_fields",
    "list_runs",
    "load_schema",
    "reconcile",
    "reconcile_all",
    "request_run",
    "review",
    "run_agent",
    "run_handler",
    "run_of_field",
    "validate",
]
