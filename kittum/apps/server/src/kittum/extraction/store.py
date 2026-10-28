"""Extraction runs, their proposals, and human acceptances, through `kittum.runs.Commands`.

Every write is a command with a pure decider:

- `request_run` (`extraction.request_run`): records the run and its `extraction.run_requested`
  event, and enqueues the `extraction.run` job.
- the job's publish step (`extraction.record_proposals`): saves the anchors, fields, and both
  paths' proposals with an `extraction.proposals_recorded` event, in the transaction that
  completes the job.
- `accept`, `edit_and_accept`, and `group_accept` (`extraction.accept`, `extraction.group_accept`):
  one `extraction.value_accepted` event and acceptance row per value.

Accepted values are the assertion set reports use (`assertions`).
"""

from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb
from pydantic import Field

from kittum.core import (
    Id,
    NotFoundError,
    Record,
    Sha256,
    Span,
    canonical_json,
    content_hash,
    new_id,
)
from kittum.documents import (
    Anchor,
    Document,
    get_anchor,
    get_document,
    get_layer,
    make_anchor,
    save_anchor,
)
from kittum.extraction.janus_path import JANUS_VERSION
from kittum.extraction.llm_path import AgentConfig
from kittum.extraction.model import (
    Acceptance,
    Agreement,
    Assertion,
    FieldSpec,
    Gate,
    Path,
    Proposal,
    SemanticKey,
    ValidatorResult,
)
from kittum.extraction.numbers import printed_in, well_formed
from kittum.extraction.schema import ExtractionSchema, Kind
from kittum.ledger import EventDraft
from kittum.runs import Command, CommandRejected, Decision, JobSpec

if TYPE_CHECKING:
    from kittum.core import Clock
    from kittum.db import Tx
    from kittum.extraction.extract import Extraction
    from kittum.ledger import AuditEvent
    from kittum.runs import Commands

RUN_REQUESTED = "extraction.run_requested"
PROPOSALS_RECORDED = "extraction.proposals_recorded"
VALUE_ACCEPTED = "extraction.value_accepted"
JOB_KIND = "extraction.run"
MIN_SOURCE_VIEW_MS = 400  # the workbench's own floor before Enter accepts

type RunStatus = (
    str  # "queued", "running", "succeeded", "failed", "dead": the job's, until it succeeds
)


class ReportInfo(Record):
    """What the review screen shows about the report; the corpus owns the real record."""

    study_label: str = ""
    title: str = ""
    citation: str = ""
    licence: str = ""


class RunRequest(Record):
    command_id: Id
    project_id: Id
    actor_id: Id
    layer_id: Id
    study_id: Id
    report_id: Id
    extraction_schema: ExtractionSchema
    agent: AgentConfig | None = None
    report: ReportInfo = ReportInfo()


class Edit(Record):
    """A reviewer's own value and the span it came from: code points into the layer text."""

    value: str | None
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str


class AcceptRequest(Record):
    command_id: Id
    project_id: Id
    field_id: Id
    reviewer_id: Id
    path: Path
    source_viewed_ms: int = Field(ge=0)
    edit: Edit | None = None
    # The reviewer confirmed the value against the source although a validator failed on it.
    override_failed_checks: bool = False


class GroupAcceptRequest(Record):
    command_id: Id
    project_id: Id
    run_id: Id
    field_ids: tuple[Id, ...] = Field(min_length=1)
    reviewer_id: Id
    list_viewed_ms: int = Field(ge=0)


class ExtractionRun(Record):
    id: Id
    project_id: Id
    layer_id: Id
    document_id: Id
    study_id: Id
    report_id: Id
    status: RunStatus
    schema_hash: Sha256
    model_identity: str | None
    janus_version: str | None
    created_at: datetime
    completed_at: datetime | None


class RunListing(Record):
    """A run as the run list shows it: its report and how far review has come."""

    run: ExtractionRun
    report: ReportInfo
    fields: int
    accepted: int


class FieldLabels(Record):
    endpoint: str
    timepoint: str
    arm: str


class ReviewField(Record):
    """One field to review: both paths' proposals for one semantic key, and the gate."""

    id: Id
    spec: FieldSpec
    key: SemanticKey
    proposals: list[Proposal]
    accepted: Path | None
    labels: FieldLabels
    agreement: Agreement
    gate: Gate
    second_extractor: bool
    recheck: bool
    acceptance: Acceptance | None


class PageSize(Record):
    page: int
    width: float
    height: float


class ReviewLayer(Record):
    """A layer without its words: the viewer loads words per page."""

    id: Id
    document_id: Id
    recipe_hash: Sha256
    output_hash: Sha256
    text: str
    pages: list[PageSize]


class ReportSummary(Record):
    id: Id
    study_id: Id
    study_label: str
    title: str
    citation: str
    licence: str


class ExtractionReview(Record):
    run: ExtractionRun
    report: ReportSummary
    document: Document
    layer: ReviewLayer
    anchors: list[Anchor]
    fields: list[ReviewField]


def _key_hash(key: SemanticKey) -> str:
    return content_hash(key, domain="kittum.semantic_key.v1")


def _payload(record: Record) -> dict[str, Any]:
    return record.model_dump(mode="json")


# Requesting a run


def _decide_run(command: Command, document_id: Id) -> Decision[dict[str, Any]]:
    run_id = command.payload["run_id"]
    request = RunRequest.model_validate(
        {k: v for k, v in command.payload.items() if k not in ("run_id", "publish_command_id")}
    )
    return Decision(
        events=(
            EventDraft(
                project_id=command.project_id,
                type=RUN_REQUESTED,
                schema_version=1,
                actor_id=command.actor_id,
                payload={
                    "run_id": run_id,
                    "layer_id": str(request.layer_id),
                    "document_id": str(document_id),
                    "study_id": str(request.study_id),
                    "report_id": str(request.report_id),
                    "schema_hash": request.extraction_schema.hash(),
                    "model_id": request.agent.model_id if request.agent else None,
                },
            ),
        ),
        jobs=(
            JobSpec(
                project_id=command.project_id,
                kind=JOB_KIND,
                stage_key=f"{JOB_KIND}:{run_id}",
                payload={"run_id": run_id},
            ),
        ),
        changes={"document_id": document_id},
    )


def request_run(tx: Tx, commands: Commands, request: RunRequest, *, clock: Clock) -> ExtractionRun:
    """Record a run and enqueue its job. A retried command returns the same run."""
    payload = {
        **_payload(request),
        "run_id": str(new_id()),
        "publish_command_id": str(new_id()),
    }
    command = Command(
        command_id=request.command_id,
        project_id=request.project_id,
        type="extraction.request_run",
        actor_id=request.actor_id,
        payload=payload,
    )

    def load(tx: Tx, _command: Command) -> Id:
        row = tx.execute(
            "select document_id from layer where id = %s", (request.layer_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"layer {request.layer_id}")
        return row["document_id"]

    def apply(tx: Tx, decision: Decision[dict[str, Any]], events: tuple[AuditEvent, ...]) -> None:
        assert decision.changes is not None
        tx.execute(
            "insert into extraction_run (id, project_id, command_id, layer_id, document_id,"
            " study_id, report_id, report, schema, schema_hash, agent, status, requested_by,"
            " requested_seq, publish_command_id, created_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s)",
            (
                payload["run_id"],
                request.project_id,
                request.command_id,
                request.layer_id,
                decision.changes["document_id"],
                request.study_id,
                request.report_id,
                Jsonb(_payload(request.report)),
                Jsonb(_payload(request.extraction_schema)),
                request.extraction_schema.hash(),
                Jsonb(_payload(request.agent)) if request.agent else None,
                request.actor_id,
                events[0].seq,
                payload["publish_command_id"],
                clock.now(),
            ),
        )

    commands.execute(tx, command, _decide_run, load=load, apply=apply)
    return get_run_by_command(tx, request.command_id)


def _run(row: dict[str, Any]) -> ExtractionRun:
    status = row["status"]
    if status == "queued" and row.get("job_status"):
        status = row["job_status"]
    return ExtractionRun(
        id=row["id"],
        project_id=row["project_id"],
        layer_id=row["layer_id"],
        document_id=row["document_id"],
        study_id=row["study_id"],
        report_id=row["report_id"],
        status=status,
        schema_hash=row["schema_hash"],
        model_identity=row["model_identity"],
        janus_version=row["janus_version"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


_RUN_SELECT = (
    "select r.*, j.status as job_status from extraction_run r"
    " left join job j on j.stage_key = 'extraction.run:' || r.id::text"
)


def get_run(tx: Tx, project_id: Id, run_id: Id) -> ExtractionRun:
    row = tx.execute(
        f"{_RUN_SELECT} where r.id = %s and r.project_id = %s", (run_id, project_id)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"extraction run {run_id}")
    return _run(row)


def list_runs(tx: Tx, project_id: Id) -> list[RunListing]:
    """The project's runs, newest first, with their field and acceptance counts."""
    rows = tx.execute(
        "select r.*, j.status as job_status,"
        " (select count(*) from extraction_field f where f.run_id = r.id) as n_fields,"
        " (select count(*) from extraction_acceptance a where a.run_id = r.id) as n_accepted"
        " from extraction_run r"
        " left join job j on j.stage_key = 'extraction.run:' || r.id::text"
        " where r.project_id = %s order by r.created_at desc, r.id desc",
        (project_id,),
    ).fetchall()
    return [
        RunListing(
            run=_run(row),
            report=ReportInfo.model_validate(row["report"]),
            fields=row["n_fields"],
            accepted=row["n_accepted"],
        )
        for row in rows
    ]


def run_of_field(tx: Tx, project_id: Id, field_id: Id) -> ExtractionRun:
    row = tx.execute(
        "select run_id from extraction_field where id = %s and project_id = %s",
        (field_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"extraction field {field_id}")
    return get_run(tx, project_id, row["run_id"])


def get_run_by_command(tx: Tx, command_id: Id) -> ExtractionRun:
    row = tx.execute(f"{_RUN_SELECT} where r.command_id = %s", (command_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"extraction run for command {command_id}")
    return _run(row)


def run_inputs(tx: Tx, run_id: Id) -> dict[str, Any]:
    """What the job needs: the run row, with its schema and agent config parsed."""
    row = tx.execute("select * from extraction_run where id = %s", (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"extraction run {run_id}")
    return {
        **row,
        "schema": ExtractionSchema.model_validate(row["schema"]),
        "agent": AgentConfig.model_validate(row["agent"]) if row["agent"] else None,
    }


# Publishing a run's proposals


def _counts(extraction: Extraction) -> dict[str, Any]:
    by_path: dict[str, Counter[str]] = {"janus": Counter(), "llm": Counter()}
    failed = 0
    for p in extraction.proposals.proposals:
        by_path[p.path][p.status] += 1
        failed += sum(not v.passed for v in p.validators)
    return {
        "fields": len(extraction.fields),
        "statuses": {path: dict(sorted(c.items())) for path, c in by_path.items() if c},
        "agreement": dict(sorted(Counter(r.agreement for r in extraction.reconciliations).items())),
        "validator_failures": failed,
    }


def _run_status(tx: Tx, run_id: Id) -> str:
    row = tx.execute(
        "select status from extraction_run where id = %s for update", (run_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"extraction run {run_id}")
    return row["status"]


def record_proposals(
    tx: Tx,
    commands: Commands,
    run_id: Id,
    extraction: Extraction,
    *,
    output_hash: str,
    transcript_hash: str | None,
    clock: Clock,
) -> None:
    """Save a finished run's anchors, fields, and proposals, once. Called from the job's publish
    step, so it commits with the job or not at all."""
    run = run_inputs(tx, run_id)
    transcript = extraction.transcript
    model_identity = transcript.model_identity if transcript else None
    command = Command(
        command_id=run["publish_command_id"],
        project_id=run["project_id"],
        type="extraction.record_proposals",
        actor_id=run["requested_by"],
        payload={"run_id": str(run_id), "output_hash": output_hash},
    )

    def decide(command: Command, status: str) -> Decision[None]:
        if status != "queued":
            raise CommandRejected(f"extraction run {run_id} is already {status}")
        return Decision(
            events=(
                EventDraft(
                    project_id=command.project_id,
                    type=PROPOSALS_RECORDED,
                    schema_version=1,
                    actor_id=command.actor_id,
                    payload={
                        "run_id": str(run_id),
                        "layer_id": str(run["layer_id"]),
                        "output_hash": output_hash,
                        "transcript_hash": transcript_hash,
                        "model_identity": model_identity,
                        "serving": transcript.serving if transcript else None,
                        "endpoint": transcript.endpoint if transcript else None,
                        "janus_version": JANUS_VERSION,
                        **_counts(extraction),
                    },
                ),
            )
        )

    def apply(tx: Tx, _decision: Decision[None], events: tuple[AuditEvent, ...]) -> None:
        seq = events[0].seq
        for anchor in extraction.anchors.values():
            save_anchor(tx, anchor)
        by_key = {(p.key, p.path): p for p in extraction.proposals.proposals}
        for ordinal, (field, rec) in enumerate(
            zip(extraction.fields, extraction.reconciliations, strict=True)
        ):
            field_id = new_id()
            tx.execute(
                "insert into extraction_field (id, run_id, project_id, ordinal, key, key_hash, spec,"
                " kind, labels, agreement, gate, second_extractor, recheck, recorded_seq)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    field_id,
                    run_id,
                    run["project_id"],
                    ordinal,
                    Jsonb(_payload(field.key)),
                    _key_hash(field.key),
                    Jsonb(_payload(field.spec)),
                    field.kind,
                    Jsonb(
                        {
                            "endpoint": field.endpoint_label,
                            "timepoint": field.timepoint_label,
                            "arm": field.arm_label,
                        }
                    ),
                    rec.agreement,
                    rec.gate,
                    rec.second_extractor,
                    rec.recheck,
                    seq,
                ),
            )
            for path in ("janus", "llm"):
                p = by_key.get((field.key, path))
                if p is None:
                    continue
                tx.execute(
                    "insert into extraction_proposal (id, field_id, run_id, path, value, unit, status,"
                    " message, quote, anchor_ids, validators, recorded_seq)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        new_id(),
                        field_id,
                        run_id,
                        path,
                        p.value,
                        p.unit,
                        p.status,
                        p.message,
                        p.quote,
                        list(p.anchor_ids),
                        Jsonb([_payload(v) for v in p.validators]),
                        seq,
                    ),
                )
        tx.execute(
            "update extraction_run set status = 'succeeded', janus_version = %s, model_identity = %s,"
            " transcript_hash = %s, output_hash = %s, recorded_seq = %s, completed_at = %s"
            " where id = %s",
            (JANUS_VERSION, model_identity, transcript_hash, output_hash, seq, clock.now(), run_id),
        )

    commands.execute(
        tx,
        command,
        decide,
        load=lambda tx, _c: _run_status(tx, run_id),
        apply=apply,
    )


# Reading fields


def _proposal(key: SemanticKey, row: dict[str, Any]) -> Proposal:
    return Proposal(
        key=key,
        value=row["value"],
        unit=row["unit"],
        status=row["status"],
        path=row["path"],
        anchor_ids=tuple(row["anchor_ids"]),
        validators=tuple(ValidatorResult.model_validate(v) for v in row["validators"]),
        message=row["message"],
        quote=row["quote"],
    )


def _acceptance(row: dict[str, Any], key: SemanticKey) -> Acceptance:
    return Acceptance(
        id=row["id"],
        field_id=row["field_id"],
        run_id=row["run_id"],
        key=key,
        path=row["path"],
        edited=row["edited"],
        group=row["grouped"],
        value=row["value"],
        unit=row["unit"],
        anchor_id=row["anchor_id"],
        reviewer_id=row["reviewer_id"],
        source_viewed_ms=row["source_viewed_ms"],
        accepted_at=row["accepted_at"],
        recheck=row["recheck"],
        event_seq=row["event_seq"],
    )


def list_fields(tx: Tx, project_id: Id, run_id: Id) -> list[ReviewField]:
    """The run's fields in schema order, each with both paths' proposals and any acceptance."""
    rows = tx.execute(
        "select * from extraction_field where run_id = %s and project_id = %s order by ordinal",
        (run_id, project_id),
    ).fetchall()
    proposals: dict[Id, list[dict[str, Any]]] = {}
    for p in tx.execute(
        "select * from extraction_proposal where run_id = %s order by path", (run_id,)
    ).fetchall():
        proposals.setdefault(p["field_id"], []).append(p)
    accepted = {
        a["field_id"]: a
        for a in tx.execute(
            "select * from extraction_acceptance where run_id = %s", (run_id,)
        ).fetchall()
    }
    out = []
    for row in rows:
        key = SemanticKey.model_validate(row["key"])
        acceptance = _acceptance(accepted[row["id"]], key) if row["id"] in accepted else None
        out.append(
            ReviewField(
                id=row["id"],
                spec=FieldSpec.model_validate(row["spec"]),
                key=key,
                proposals=[_proposal(key, p) for p in proposals.get(row["id"], [])],
                accepted=acceptance.path if acceptance else None,
                labels=FieldLabels.model_validate(row["labels"]),
                agreement=row["agreement"],
                gate=row["gate"],
                second_extractor=row["second_extractor"],
                recheck=row["recheck"],
                acceptance=acceptance,
            )
        )
    return out


def review(tx: Tx, project_id: Id, run_id: Id, *, artifacts: Any) -> ExtractionReview:
    """Everything the extraction screen shows for one run."""
    run = get_run(tx, project_id, run_id)
    row = tx.execute("select report from extraction_run where id = %s", (run_id,)).fetchone()
    assert row is not None
    report = ReportInfo.model_validate(row["report"])
    layer = get_layer(tx, run.layer_id, artifacts=artifacts)
    fields = list_fields(tx, project_id, run_id)
    anchor_ids = sorted(
        {a for f in fields for p in f.proposals for a in p.anchor_ids}
        | {f.acceptance.anchor_id for f in fields if f.acceptance}
    )
    return ExtractionReview(
        run=run,
        report=ReportSummary(id=run.report_id, study_id=run.study_id, **_payload(report)),
        document=get_document(tx, run.document_id),
        layer=ReviewLayer(
            id=layer.id,
            document_id=layer.document_id,
            recipe_hash=layer.recipe_hash,
            output_hash=layer.output_hash,
            text=layer.text,
            pages=[PageSize(page=p.number, width=p.width, height=p.height) for p in layer.pages],
        ),
        anchors=[get_anchor(tx, a) for a in anchor_ids],
        fields=fields,
    )


# Accepting values


class _Field(Record):
    id: Id
    run_id: Id
    project_id: Id
    key: SemanticKey
    key_hash: Sha256
    spec: FieldSpec
    kind: Kind
    gate: Gate
    recheck: bool
    accepted: bool
    proposals: dict[str, Proposal]


def _load_field(tx: Tx, project_id: Id, field_id: Id) -> _Field:
    row = tx.execute(
        "select f.*, exists (select 1 from extraction_acceptance a where a.field_id = f.id) as accepted"
        " from extraction_field f where f.id = %s and f.project_id = %s for update",
        (field_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"extraction field {field_id}")
    key = SemanticKey.model_validate(row["key"])
    proposals = {
        p["path"]: _proposal(key, p)
        for p in tx.execute(
            "select * from extraction_proposal where field_id = %s", (field_id,)
        ).fetchall()
    }
    return _Field(
        id=row["id"],
        run_id=row["run_id"],
        project_id=row["project_id"],
        key=key,
        key_hash=row["key_hash"],
        spec=FieldSpec.model_validate(row["spec"]),
        kind=row["kind"],
        gate=row["gate"],
        recheck=row["recheck"],
        accepted=row["accepted"],
        proposals=proposals,
    )


class _Accepted(Record):
    """One value a decider accepted, for `apply` to write."""

    field: _Field
    path: Path
    edited: bool
    group: bool
    value: str | None
    unit: str | None
    anchor_id: Id
    new_anchor: Anchor | None = None  # an edit's span, saved with the acceptance
    failed_checks: tuple[str, ...] = ()  # validators that failed on the accepted proposal


def _event(command: Command, a: _Accepted, viewed_ms: int) -> EventDraft:
    return EventDraft(
        project_id=command.project_id,
        type=VALUE_ACCEPTED,
        schema_version=1,
        actor_id=command.actor_id,
        payload={
            "run_id": str(a.field.run_id),
            "field_id": str(a.field.id),
            "key": _payload(a.field.key),
            "key_hash": a.field.key_hash,
            "policy": a.field.spec.policy,
            "path": a.path,
            "edited": a.edited,
            "group": a.group,
            "value": a.value,
            "unit": a.unit,
            "anchor_id": str(a.anchor_id),
            "source_viewed_ms": viewed_ms,
            "recheck": a.field.recheck and a.group,
            "failed_checks": list(a.failed_checks),
        },
    )


def _accepted_proposal(field: _Field, path: Path, *, override_failed_checks: bool) -> _Accepted:
    proposal = field.proposals.get(path)
    if proposal is None:
        raise CommandRejected(f"field {field.id} has no {path} proposal")
    if proposal.status != "extracted" or len(proposal.anchor_ids) != 1:
        raise CommandRejected(
            f"the {path} proposal is {proposal.status}"
            + (f" ({proposal.message})" if proposal.message else "")
            + "; edit it with a source span instead"
        )
    failed = tuple(dict.fromkeys(v.validator for v in proposal.validators if not v.passed))
    if failed and not override_failed_checks:
        raise CommandRejected(
            f"the {path} proposal failed {', '.join(failed)}; confirm it against the source and"
            " override, or edit it"
        )
    return _Accepted(
        field=field,
        path=path,
        edited=False,
        group=False,
        value=proposal.value,
        unit=proposal.unit,
        anchor_id=proposal.anchor_ids[0],
        failed_checks=failed,
    )


def _write_acceptances(
    tx: Tx,
    accepted: list[_Accepted],
    events: tuple[AuditEvent, ...],
    reviewer: Id,
    viewed_ms: int,
    clock: Clock,
) -> None:
    for a, event in zip(accepted, events, strict=True):
        if a.new_anchor is not None:
            save_anchor(tx, a.new_anchor)
        tx.execute(
            "insert into extraction_acceptance (id, field_id, run_id, project_id, key_hash, path,"
            " edited, grouped, value, unit, anchor_id, reviewer_id, source_viewed_ms, recheck,"
            " accepted_at, event_seq)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                new_id(),
                a.field.id,
                a.field.run_id,
                a.field.project_id,
                a.field.key_hash,
                a.path,
                a.edited,
                a.group,
                a.value,
                a.unit,
                a.anchor_id,
                reviewer,
                viewed_ms,
                a.field.recheck and a.group,
                clock.now(),
                event.seq,
            ),
        )


def accept(
    tx: Tx, commands: Commands, request: AcceptRequest, *, artifacts: Any, clock: Clock
) -> Acceptance:
    """Accept one field after the reviewer viewed its source: a path's value as it stands, or with
    `edit`, the reviewer's own value and span. Raises `CommandRejected` with the reason if the
    value cannot be accepted (not extracted, no single anchor, already accepted, source not viewed,
    a failed validator the reviewer did not override, or an edit whose span does not hold its
    value)."""
    command = Command(
        command_id=request.command_id,
        project_id=request.project_id,
        type="extraction.accept",
        actor_id=request.reviewer_id,
        payload=_payload(request),
    )

    def load(tx: Tx, _command: Command) -> tuple[_Field, Anchor | None, str | None]:
        field = _load_field(tx, request.project_id, request.field_id)
        if request.edit is None:
            return field, None, None
        run = tx.execute(
            "select layer_id from extraction_run where id = %s", (field.run_id,)
        ).fetchone()
        assert run is not None
        layer = get_layer(tx, run["layer_id"], artifacts=artifacts)
        edit = request.edit
        if edit.end > len(layer.text) or edit.start >= edit.end:
            return field, None, f"the span {edit.start}..{edit.end} is outside the layer text"
        printed = layer.text[edit.start : edit.end]
        if printed != edit.quote:
            return field, None, "the quote is not the layer text at the span"
        return field, make_anchor(layer, Span(edit.start, edit.end)), None

    def decide(
        command: Command, state: tuple[_Field, Anchor | None, str | None]
    ) -> Decision[list[_Accepted]]:
        field, anchor, problem = state
        if field.accepted:
            raise CommandRejected(f"field {field.id} is already accepted")
        if request.source_viewed_ms < MIN_SOURCE_VIEW_MS:
            raise CommandRejected(
                f"the source was on screen for {request.source_viewed_ms} ms; view it first"
            )
        if request.edit is None:
            accepted = _accepted_proposal(
                field, request.path, override_failed_checks=request.override_failed_checks
            )
        else:
            if problem is not None or anchor is None:
                raise CommandRejected(problem or "an edit needs a source span")
            value = request.edit.value
            if value is not None:
                printed = printed_in(value, field.kind, anchor.quote.exact)
                if printed is None:
                    raise CommandRejected(f"the span does not contain the value {value!r}")
                problem = well_formed(printed, field.kind)
                if problem is not None:
                    raise CommandRejected(problem)
                value = printed
            accepted = _Accepted(
                field=field,
                path=request.path,
                edited=True,
                group=False,
                value=value,
                unit=field.spec.unit if value is not None else None,
                anchor_id=anchor.id,
                new_anchor=anchor,
            )
        return Decision(
            events=(_event(command, accepted, request.source_viewed_ms),), changes=[accepted]
        )

    def apply(tx: Tx, decision: Decision[list[_Accepted]], events: tuple[AuditEvent, ...]) -> None:
        assert decision.changes is not None
        _write_acceptances(
            tx, decision.changes, events, request.reviewer_id, request.source_viewed_ms, clock
        )

    commands.execute(tx, command, decide, load=load, apply=apply)
    return _acceptance_of(tx, request.field_id)


def group_accept(
    tx: Tx, commands: Commands, request: GroupAcceptRequest, *, clock: Clock
) -> list[Acceptance]:
    """Accept agreeing, validated `standard` fields together after the reviewer viewed the list.

    Every field must have the `group_accept` gate: a `critical` field never does, so asking for
    one rejects the whole group and accepts nothing.
    """
    command = Command(
        command_id=request.command_id,
        project_id=request.project_id,
        type="extraction.group_accept",
        actor_id=request.reviewer_id,
        payload=_payload(request),
    )

    def load(tx: Tx, _command: Command) -> list[_Field]:
        return [_load_field(tx, request.project_id, f) for f in request.field_ids]

    def decide(command: Command, fields: list[_Field]) -> Decision[list[_Accepted]]:
        if len(set(request.field_ids)) != len(request.field_ids):
            raise CommandRejected("a field is listed twice")
        if request.list_viewed_ms < MIN_SOURCE_VIEW_MS:
            raise CommandRejected(
                f"the list was on screen for {request.list_viewed_ms} ms; view it first"
            )
        accepted = []
        for field in fields:
            if field.run_id != request.run_id:
                raise CommandRejected(f"field {field.id} is not in run {request.run_id}")
            if field.gate != "group_accept":
                raise CommandRejected(
                    f"field {field.id} ({field.spec.policy}, {field.gate}) cannot be accepted as a"
                    " group; verify it on its own against the source"
                )
            if field.accepted:
                raise CommandRejected(f"field {field.id} is already accepted")
            # Both paths agree; the Janus anchor is the table cell.
            accepted.append(
                _accepted_proposal(field, "janus", override_failed_checks=False).model_copy(
                    update={"group": True}
                )
            )
        return Decision(
            events=tuple(_event(command, a, request.list_viewed_ms) for a in accepted),
            changes=accepted,
        )

    def apply(tx: Tx, decision: Decision[list[_Accepted]], events: tuple[AuditEvent, ...]) -> None:
        assert decision.changes is not None
        _write_acceptances(
            tx, decision.changes, events, request.reviewer_id, request.list_viewed_ms, clock
        )

    commands.execute(tx, command, decide, load=load, apply=apply)
    return [_acceptance_of(tx, f) for f in request.field_ids]


def _acceptance_of(tx: Tx, field_id: Id) -> Acceptance:
    row = tx.execute(
        "select a.*, f.key from extraction_acceptance a join extraction_field f on f.id = a.field_id"
        " where a.field_id = %s",
        (field_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"acceptance of field {field_id}")
    return _acceptance(row, SemanticKey.model_validate(row["key"]))


def assertions(tx: Tx, project_id: Id) -> list[Assertion]:
    """The accepted values of a project: per semantic key, the latest acceptance in ledger order."""
    rows = tx.execute(
        "select distinct on (a.key_hash) a.*, f.key, f.spec, r.layer_id, d.artifact_hash"
        " from extraction_acceptance a"
        " join extraction_field f on f.id = a.field_id"
        " join extraction_run r on r.id = a.run_id"
        " join document d on d.id = r.document_id"
        " where a.project_id = %s order by a.key_hash, a.event_seq desc",
        (project_id,),
    ).fetchall()
    out = [
        Assertion(
            key=SemanticKey.model_validate(r["key"]),
            value=r["value"],
            unit=r["unit"],
            policy=FieldSpec.model_validate(r["spec"]).policy,
            anchor_id=r["anchor_id"],
            acceptance_id=r["id"],
            run_id=r["run_id"],
            layer_id=r["layer_id"],
            document_hash=r["artifact_hash"].strip(),
        )
        for r in rows
    ]
    return sorted(out, key=lambda a: canonical_json(a.key))
