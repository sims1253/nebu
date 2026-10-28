"""The project lifecycle, as service functions the API and the CLI share.

    project, lead = create_project(tx, name=..., lead_name=..., ledger=...)
    add_member(tx, project, display_name="...", roles=("reviewer",), added_by=lead, ledger=...)
    publish_protocol(tx, project, source, approved_by=lead, ledger=...)
    # searches: kittum.search.run / import_file
    ingest_corpus(tx, project, actor_id=lead, ledger=...)
    attach_full_text(tx, project, item_ref, pdf, ...)       # for full-text screening
    open_screening(tx, ctx, project, scope_id, "title_abstract", actor_id=lead)

The workbench drives the same steps over HTTP; `parse_source` and `parse_sentinels` read the
YAML or JSON files the CLI reads from disk, and `retrieval` lists what the full-text upload
queue shows the methods lead.

Each function writes its rows and its ledger event through the caller's transaction. Screening
records come from the corpus: one per report of the newest identity revision at title and
abstract; at full text, one per record included at title and abstract that has a full text.
"""

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import ValidationError

from kittum.core import Id, KittumError, NotFoundError, Record, new_id, sha256_hex
from kittum.corpus import (
    CorpusRevision,
    PrismaCounts,
    get_counts,
    ingest,
    latest_revision,
    reports,
)
from kittum.documents import PyMuPDFParser, derive, get_document, get_layer
from kittum.documents import ingest as ingest_pdf
from kittum.ledger import EventDraft
from kittum.people import Member, MemberRole, is_lead, members, record_roles
from kittum.protocol import StoredProtocol, compile, latest_published, publish, save_draft
from kittum.screening import (
    NotAssigned,
    RecordFacts,
    ScopeStatus,
    ScreeningRecord,
    Sentinel,
    StageInfo,
    StageSpec,
    sees_lead_data,
    topic_for,
    undecided_items,
)
from kittum.search import list_executions

if TYPE_CHECKING:
    from kittum.app.context import AppContext
    from kittum.db import Tx
    from kittum.documents import Acquisition, Document
    from kittum.ledger import Ledger
    from kittum.protocol import Stage
    from kittum.screening import RevealSetting, Screening
    from kittum.storage import ArtifactStore

PROJECT_CREATED = "project.created"
MEMBER_ADDED = "project.member_added"
FULL_TEXT_ATTACHED = "project.full_text_attached"
EXAMPLES = Path(__file__).parents[5] / "examples"
"""kittum/examples in a checkout: example protocols and sentinel files."""


class LifecycleError(KittumError):
    """A lifecycle step does not fit the project's state, e.g. opening a stage before the
    protocol is published."""


class NotLead(KittumError):
    """The caller is not the project's methods lead. Reported as forbidden."""


class ProtocolRef(Record):
    version_id: Id
    version_label: str
    content_hash: str
    profile: str | None


class ProjectSummary(Record):
    id: Id
    name: str
    created_at: datetime
    title: str | None
    """The published protocol's title, in its default language."""
    protocol: ProtocolRef | None
    members: tuple[Member, ...]
    stages: tuple[StageInfo, ...]


class FullText(Record):
    item_ref: str
    document_id: Id
    layer_id: Id
    page_count: int | None
    licence: str | None


class ExampleProtocol(Record):
    id: str
    title: str
    text: str
    """The YAML source, as `kittum project publish-protocol` takes it."""


class RetrievalItem(Record):
    """A report of the corpus and its full text, for the methods lead's upload queue. `outcomes`
    holds the report's current outcome per screened scope and stage (`include`, `exclude`,
    `conflict`, or None while it is being screened)."""

    item_ref: str
    report_id: Id
    study_id: Id
    title: str
    year: int | None
    journal: str | None
    doi: str | None
    registry_ids: tuple[str, ...]
    outcomes: dict[str, dict[str, str | None]]
    full_text: FullText | None


class StageProgress(Record):
    """One stage as the viewer may see it. `outcomes` counts records by their current outcome
    (`include`, `exclude`, `conflict`, `pending`) and is only filled in for the methods lead."""

    stage: StageInfo
    status: ScopeStatus
    outcomes: dict[str, int] | None


class Progress(Record):
    identity_revision_id: Id | None
    identification: PrismaCounts | None
    searches: int
    full_texts: int
    stages: tuple[StageProgress, ...]


def _event(project_id: Id, type: str, actor_id: Id, payload: dict[str, Any]) -> EventDraft:
    return EventDraft(
        project_id=project_id, type=type, schema_version=1, actor_id=actor_id, payload=payload
    )


def _person(tx: Tx, display_name: str) -> Id:
    """A new human actor. Identity references stay opaque until the API has authentication."""
    row = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'human') returning id",
        (f"person:{new_id()}:{display_name}",),
    ).fetchone()
    assert row is not None
    return row["id"]


# People


def create_project(tx: Tx, *, name: str, lead_name: str, ledger: Ledger) -> tuple[Id, Id]:
    """A project with its methods lead. Returns the project ID and the lead's actor ID."""
    row = tx.execute(
        "insert into project (name, policy_version) values (%s, 'kittum.v1') returning id", (name,)
    ).fetchone()
    assert row is not None
    project_id: Id = row["id"]
    lead = _person(tx, lead_name)
    event = ledger.append(
        tx, _event(project_id, PROJECT_CREATED, lead, {"name": name, "lead": str(lead)})
    )
    _add_roles(tx, project_id, lead, lead_name, ("lead",), event.event_id, ledger, lead)
    return project_id, lead


def add_member(
    tx: Tx,
    project_id: Id,
    *,
    display_name: str,
    roles: tuple[MemberRole, ...],
    added_by: Id,
    ledger: Ledger,
    actor_id: Id | None = None,
) -> Member:
    """Add a person (or new roles of an existing member) to the project."""
    if not roles:
        raise LifecycleError("a member needs at least one role")
    if actor_id is None:
        actor_id = _person(tx, display_name)
    _add_roles(tx, project_id, actor_id, display_name, roles, None, ledger, added_by)
    (member,) = [m for m in members(tx, project_id) if m.actor_id == actor_id]
    return member


def _add_roles(
    tx: Tx,
    project_id: Id,
    actor_id: Id,
    display_name: str,
    roles: tuple[MemberRole, ...],
    event_id: Id | None,
    ledger: Ledger,
    added_by: Id,
) -> None:
    if event_id is None:
        payload = {"actor_id": str(actor_id), "display_name": display_name, "roles": list(roles)}
        event_id = ledger.append(tx, _event(project_id, MEMBER_ADDED, added_by, payload)).event_id
    record_roles(tx, project_id, actor_id, display_name, roles, event_id=event_id)


def _with_role(people: list[Member], role: MemberRole) -> list[Id]:
    return [m.actor_id for m in people if role in m.roles]


# Projects


def list_projects(tx: Tx, screening: Screening) -> list[ProjectSummary]:
    rows = tx.execute("select id from project order by created_at desc").fetchall()
    return [project_summary(tx, r["id"], screening) for r in rows]


def project_summary(tx: Tx, project_id: Id, screening: Screening) -> ProjectSummary:
    row = tx.execute("select id, name, created_at from project where id = %s", (project_id,))
    project = row.fetchone()
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    stored = latest_published(tx, project_id)
    protocol = stored.version.protocol if stored else None
    return ProjectSummary(
        id=project["id"],
        name=project["name"],
        created_at=project["created_at"],
        title=protocol.title[protocol.default_language] if protocol else None,
        protocol=ProtocolRef(
            version_id=stored.id,
            version_label=protocol.version_label,
            content_hash=stored.version.content_hash,
            profile=protocol.profile,
        )
        if stored and protocol
        else None,
        members=tuple(members(tx, project_id)),
        stages=tuple(screening.stages(tx, project_id)),
    )


# Protocol


def publish_protocol(
    tx: Tx,
    project_id: Id,
    source: dict[str, Any] | str,
    *,
    approved_by: Id,
    ledger: Ledger,
    reason: str | None = None,
) -> StoredProtocol:
    """Compile a protocol source (YAML text or a mapping), save it as a draft, and publish it
    with the approver's recorded approval. Raises `kittum.protocol.ProtocolError` if it does not
    compile."""
    data = yaml.safe_load(source) if isinstance(source, str) else source
    draft = save_draft(tx, project_id, compile(data))
    return publish(tx, project_id, draft.id, approved_by, ledger=ledger, reason=reason)


def parse_source(text: str, what: str = "the file") -> Any:
    """A YAML or JSON document (JSON is YAML) as data. Raises `LifecycleError` naming the line and
    column of a syntax error."""
    try:
        return yaml.safe_load(text)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise LifecycleError(f"{what} is not valid YAML or JSON: {where}{e.problem}") from None
    except yaml.YAMLError as e:
        raise LifecycleError(f"{what} is not valid YAML or JSON: {e}") from None


def example_protocols() -> list[ExampleProtocol]:
    """The example protocols of a checkout, to start a review from; none in an installed
    package without them."""
    out = []
    for path in sorted((EXAMPLES / "protocols").glob("*.yaml")):
        text = path.read_text()
        data = yaml.safe_load(text)
        title = data.get("title", {}) if isinstance(data, dict) else {}
        language = data.get("default_language", "en") if isinstance(data, dict) else "en"
        label = title.get(language) if isinstance(title, dict) else None
        out.append(ExampleProtocol(id=path.stem, title=str(label or path.stem), text=text))
    return out


def protocol_source(tx: Tx, project_id: Id) -> str:
    """The published protocol as YAML, to edit into its next version. It compiles to the same
    content hash."""
    protocol = _published(tx, project_id).version.protocol
    return yaml.safe_dump(
        protocol.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )


def _published(tx: Tx, project_id: Id) -> StoredProtocol:
    stored = latest_published(tx, project_id)
    if stored is None:
        raise LifecycleError("publish the protocol first")
    return stored


# Corpus


def ingest_corpus(tx: Tx, project_id: Id, *, actor_id: Id, ledger: Ledger) -> CorpusRevision:
    """Deduplicate every search of the project into the first identity revision.

    Once a corpus exists, later searches (update searches) need the corpus diff, which is not
    built yet, so a second ingest is refused.
    """
    if latest_revision(tx, project_id) is not None:
        raise LifecycleError("the corpus is already ingested; update searches are not wired yet")
    executions = list(reversed(list_executions(tx, project_id)))
    if not executions:
        raise LifecycleError("run or import a search first")
    return ingest(tx, project_id, executions, actor_id=actor_id, ledger=ledger)


def corpus_records(tx: Tx, project_id: Id) -> list[ScreeningRecord]:
    """One screening record per report of the newest identity revision, in search order.

    A report found by several searches shows the occurrence with the longest abstract, with the
    registry IDs and publication types of all of them.
    """
    revision = latest_revision(tx, project_id)
    if revision is None:
        raise LifecycleError("ingest the corpus first")
    out = []
    for report in reports(tx, project_id, revision.id):
        best = max(report.records, key=lambda r: len(r.abstract or ""))
        out.append(
            ScreeningRecord(
                item_ref=str(report.report_id),
                title=best.title,
                text=best.abstract or "",
                facts=RecordFacts(
                    pmid=next((r.pmid for r in report.records if r.pmid), None),
                    doi=next((r.doi for r in report.records if r.doi), None),
                    journal=best.journal,
                    year=best.year,
                    publication_types=tuple(
                        dict.fromkeys(t for r in report.records for t in r.publication_types)
                    ),
                    registry_ids=tuple(
                        dict.fromkeys(i for r in report.records for i in r.registry_ids)
                    ),
                ),
            )
        )
    return out


# Full texts


def attach_full_text(
    tx: Tx,
    project_id: Id,
    item_ref: str,
    pdf: bytes,
    *,
    actor_id: Id,
    artifacts: ArtifactStore,
    ledger: Ledger,
    licence: str | None = None,
    sentinel: tuple[str, Literal["include", "exclude"]] | None = None,
) -> FullText:
    """Store a PDF, parse its base layer, and make it the record's full text.

    This is the human upload of the full-text waterfall (DESIGN.md, "Full text"); the automatic
    sources attach through `attach_acquired_full_text`. With `sentinel` (its title and expected
    outcome) the PDF joins the project's full-text sentinels instead, under an item reference of
    its own; use `attach_sentinel_full_text`.
    """
    if full_text(tx, project_id, item_ref) is not None:
        raise LifecycleError(f"record {item_ref} already has a full text")
    document = ingest_pdf(tx, artifacts, pdf)
    return _attach(
        tx,
        project_id,
        item_ref,
        document,
        actor_id=actor_id,
        artifacts=artifacts,
        ledger=ledger,
        licence=licence,
        sentinel=sentinel,
    )


def attach_acquired_full_text(
    tx: Tx,
    project_id: Id,
    item_ref: str,
    acquisition: Acquisition,
    *,
    actor_id: Id,
    artifacts: ArtifactStore,
    ledger: Ledger,
) -> FullText:
    """Make a PDF the waterfall fetched (`kittum.documents.acquire`) the record's full text, with
    the licence its source stated and a reference to the acquisition."""
    if full_text(tx, project_id, item_ref) is not None:
        raise LifecycleError(f"record {item_ref} already has a full text")
    return _attach(
        tx,
        project_id,
        item_ref,
        acquisition.document,
        actor_id=actor_id,
        artifacts=artifacts,
        ledger=ledger,
        licence=acquisition.licence,
        acquisition=acquisition,
    )


def _attach(
    tx: Tx,
    project_id: Id,
    item_ref: str,
    document: Document,
    *,
    actor_id: Id,
    artifacts: ArtifactStore,
    ledger: Ledger,
    licence: str | None,
    sentinel: tuple[str, Literal["include", "exclude"]] | None = None,
    acquisition: Acquisition | None = None,
) -> FullText:
    layer = derive(tx, document, PyMuPDFParser(), artifacts=artifacts)
    payload = {
        "item_ref": item_ref,
        "document_id": str(document.id),
        "artifact_hash": document.artifact_hash,
        "layer_id": str(layer.id),
        "licence": licence,
        "sentinel_outcome": sentinel[1] if sentinel else None,
    }
    if acquisition is not None:
        payload |= {
            "acquisition_id": str(acquisition.id),
            "source": acquisition.source,
            "url": acquisition.url,
        }
    event = ledger.append(tx, _event(project_id, FULL_TEXT_ATTACHED, actor_id, payload))
    tx.execute(
        "insert into full_text (project_id, item_ref, document_id, layer_id, licence,"
        " sentinel_title, sentinel_outcome, event_id) values (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project_id,
            item_ref,
            document.id,
            layer.id,
            licence,
            sentinel[0] if sentinel else None,
            sentinel[1] if sentinel else None,
            event.event_id,
        ),
    )
    found = full_text(tx, project_id, item_ref)
    assert found is not None
    return found


def attach_sentinel_full_text(
    tx: Tx,
    project_id: Id,
    title: str,
    pdf: bytes,
    expected_outcome: Literal["include", "exclude"],
    *,
    actor_id: Id,
    artifacts: ArtifactStore,
    ledger: Ledger,
    licence: str | None = None,
) -> FullText:
    """A report with a known outcome for full-text queues, as title and abstract stages take
    sentinel records. Full-text stages draw their sentinels from these."""
    return attach_full_text(
        tx,
        project_id,
        f"sentinel:{sha256_hex(pdf)[:16]}",
        pdf,
        actor_id=actor_id,
        artifacts=artifacts,
        ledger=ledger,
        licence=licence,
        sentinel=(title, expected_outcome),
    )


def full_text_sentinels(tx: Tx, project_id: Id, artifacts: ArtifactStore) -> tuple[Sentinel, ...]:
    rows = tx.execute(
        "select item_ref, layer_id, sentinel_title, sentinel_outcome from full_text"
        " where project_id = %s and sentinel_outcome is not null order by item_ref",
        (project_id,),
    ).fetchall()
    return tuple(
        Sentinel(
            record=ScreeningRecord(
                item_ref=r["item_ref"],
                title=r["sentinel_title"],
                text=get_layer(tx, r["layer_id"], artifacts=artifacts).text,
            ),
            expected_outcome=r["sentinel_outcome"],
        )
        for r in rows
    )


def full_text(tx: Tx, project_id: Id, item_ref: str) -> FullText | None:
    row = tx.execute(
        "select item_ref, document_id, layer_id, licence from full_text"
        " where project_id = %s and item_ref = %s",
        (project_id, item_ref),
    ).fetchone()
    if row is None:
        return None
    document = get_document(tx, row["document_id"])
    return FullText(**row, page_count=document.page_count)


def full_text_records(
    tx: Tx, project_id: Id, scope_id: str, screening: Screening, artifacts: ArtifactStore
) -> list[ScreeningRecord]:
    """The records included at title and abstract that have a full text, with the full text as
    the text to screen."""
    included = {
        ref
        for ref, outcome in screening.outcomes(tx, project_id, scope_id, "title_abstract").items()
        if outcome == "include"
    }
    out = []
    for record in corpus_records(tx, project_id):
        found = full_text(tx, project_id, record.item_ref) if record.item_ref in included else None
        if found is not None:
            layer = get_layer(tx, found.layer_id, artifacts=artifacts)
            out.append(record.model_copy(update={"text": layer.text}))
    return out


def retrieval(tx: Tx, project_id: Id, screening: Screening) -> list[RetrievalItem]:
    """Every report of the newest identity revision, in search order, with its outcome in each
    open stage and its full text. Full-text sentinels are not reports and are left out."""
    revision = latest_revision(tx, project_id)
    if revision is None:
        raise LifecycleError("ingest the corpus first")
    outcomes: dict[str, dict[str, dict[str, str | None]]] = {}
    for info in screening.stages(tx, project_id):
        found = screening.outcomes(
            tx, project_id, info.scope_id, info.stage, topic_id=info.topic_id
        )
        for ref, outcome in found.items():
            outcomes.setdefault(ref, {}).setdefault(info.scope_id, {})[info.stage] = outcome
    texts = {
        r["item_ref"]: r
        for r in tx.execute(
            "select item_ref from full_text where project_id = %s and sentinel_outcome is null",
            (project_id,),
        ).fetchall()
    }
    out = []
    for report in reports(tx, project_id, revision.id):
        ref = str(report.report_id)
        best = max(report.records, key=lambda r: len(r.abstract or ""))
        out.append(
            RetrievalItem(
                item_ref=ref,
                report_id=report.report_id,
                study_id=report.study_id,
                title=best.title,
                year=best.year,
                journal=best.journal,
                doi=next((r.doi for r in report.records if r.doi), None),
                registry_ids=tuple(
                    dict.fromkeys(i for r in report.records for i in r.registry_ids)
                ),
                outcomes=outcomes.get(ref, {}),
                full_text=full_text(tx, project_id, ref) if ref in texts else None,
            )
        )
    return out


# Screening


def parse_sentinels(text: str, what: str = "the sentinel file") -> tuple[Sentinel, ...]:
    """Sentinels from YAML or JSON text: a list of `Sentinel` records (see examples/sentinels/)."""
    data = parse_source(text, what)
    if not isinstance(data, list):
        raise LifecycleError(f"{what} must hold a list of sentinels")
    out = []
    for index, item in enumerate(data):
        try:
            out.append(Sentinel.model_validate(item))
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'the entry'}: {err['msg']}"
                for err in e.errors()
            )
            raise LifecycleError(f"sentinel {index + 1} in {what}: {problems}") from None
    return tuple(out)


def load_sentinels(path: Path) -> tuple[Sentinel, ...]:
    """Sentinels from a YAML or JSON file: a list of `Sentinel` records (see
    examples/sentinels/)."""
    return parse_sentinels(path.read_text(), str(path))


def open_screening(
    tx: Tx,
    ctx: AppContext,
    project_id: Id,
    scope_id: str,
    stage: Stage,
    *,
    actor_id: Id,
    reveal: RevealSetting = "on_disagreement",
    sentinels: tuple[Sentinel, ...] = (),
    topic_id: str | None = None,
) -> StageInfo:
    """Open a stage for one scope with the project's members and enqueue the judge.

    The protocol's mode sets the screeners: the first two reviewers in M1 and M3, the first in
    M2. The first auditor audits; every adjudicator adjudicates. A full-text stage without given
    sentinels draws them from the project's sentinel full texts.
    """
    if ctx.judge is None:
        raise LifecycleError(
            "no judge is configured (KITTUM_LLM_ENDPOINTS or KITTUM_JUDGE_BACKEND)"
        )
    stored = _published(tx, project_id)
    topic = topic_for(stored.version.protocol, scope_id, topic_id)
    people = members(tx, project_id)
    reviewers = _with_role(people, "reviewer")
    needed = 1 if topic.mode == "M2" else 2
    if len(reviewers) < needed:
        raise LifecycleError(
            f"{topic.mode} needs {needed} reviewers; the project has {len(reviewers)}"
        )
    auditors = _with_role(people, "auditor")
    adjudicators = _with_role(people, "adjudicator")
    if not auditors or not adjudicators:
        raise LifecycleError("a stage needs an auditor and at least one adjudicator")
    screening = ctx.screening()
    if stage == "title_abstract":
        records = corpus_records(tx, project_id)
    else:
        records = full_text_records(tx, project_id, scope_id, screening, ctx.artifacts)
        sentinels = sentinels or full_text_sentinels(tx, project_id, ctx.artifacts)
    if not records:
        raise LifecycleError(f"there are no records to screen at {stage}")
    info = screening.open_stage(
        tx,
        command_id=new_id(),
        project_id=project_id,
        actor_id=actor_id,
        spec=StageSpec(
            protocol_version_id=stored.id,
            scope_id=scope_id,
            stage=stage,
            topic_id=topic_id,
            records=tuple(records),
            screeners=tuple(reviewers[:needed]),
            auditor_id=auditors[0],
            adjudicators=tuple(adjudicators),
            sentinels=sentinels,
            reveal=reveal,
            judge_config_id=ctx.judge.config.id,
        ),
    )
    screening.request_judging(
        tx, command_id=new_id(), project_id=project_id, actor_id=actor_id, stage_id=info.stage_id
    )
    return info


# Extraction


def visible_reports(tx: Tx, project_id: Id, viewer_id: Id, report_ids: list[Id]) -> set[Id]:
    """The reports whose extraction (runs, proposals, accepted values) the viewer may see: all of
    them for a lead who screens nothing; for any other member of the project, those they have no
    undecided screening assignment for. A run exists only for a report included at full text, so
    seeing it before deciding would tell a screener how the others decided, and that it is no
    sentinel."""
    if sees_lead_data(tx, project_id, viewer_id):
        return set(report_ids)
    if not any(m.actor_id == viewer_id for m in members(tx, project_id)):
        return set()
    undecided = undecided_items(tx, project_id, viewer_id, [str(r) for r in report_ids])
    return {r for r in report_ids if str(r) not in undecided}


def check_extractable(
    tx: Tx,
    project_id: Id,
    report_id: Id,
    layer_id: Id,
    screening: Screening,
    artifacts: ArtifactStore,
) -> None:
    """Refuse an extraction run unless the report is included at full text in some scope and the
    layer is one of its full text's document. Raises `LifecycleError`."""
    ref = str(report_id)
    found = full_text(tx, project_id, ref)
    if found is None:
        raise LifecycleError(f"report {report_id} has no full text")
    if get_layer(tx, layer_id, artifacts=artifacts).document_id != found.document_id:
        raise LifecycleError(f"layer {layer_id} is not a layer of the report's full text")
    stages = [s for s in screening.stages(tx, project_id) if s.stage == "full_text"]
    if not any(
        screening.outcomes(tx, project_id, s.scope_id, "full_text", topic_id=s.topic_id).get(ref)
        == "include"
        for s in stages
    ):
        raise LifecycleError(f"report {report_id} is not included at full text")


# Progress


def require_project_lead(tx: Tx, project_id: Id, actor_id: Id) -> None:
    """Refuse a lead's write (adding people, publishing the protocol) to anyone who is not one of
    the project's leads. Until the API has authentication the actor is the caller's claim."""
    if not is_lead(tx, project_id, actor_id):
        raise NotLead(f"only the project's methods lead may do this; {actor_id} is not one")


def require_lead(tx: Tx, project_id: Id, actor_id: Id) -> None:
    """Refuse reads of the whole catalogue to anyone but a lead who screens nothing
    (`sees_lead_data`): whoever could list every record and has a queue could tell which records
    in it are sentinels by their absence."""
    if not sees_lead_data(tx, project_id, actor_id):
        raise NotLead(f"only the project's methods lead may read this; {actor_id} is not one")


def progress(tx: Tx, project_id: Id, viewer_id: Id, screening: Screening) -> Progress:
    """Each stage the viewer has a part in, as `Screening.status` shows it to them, and, for the
    methods lead, the identification counts and each record's outcome. Stages the viewer has no
    part in are left out."""
    lead = sees_lead_data(tx, project_id, viewer_id)
    revision = latest_revision(tx, project_id) if lead else None
    stages = []
    for info in screening.stages(tx, project_id):
        try:
            status = screening.status(
                tx, project_id, viewer_id, info.scope_id, info.stage, topic_id=info.topic_id
            )
        except NotAssigned:
            continue
        outcomes = None
        if status.view == "lead" and lead:
            found = screening.outcomes(
                tx, project_id, info.scope_id, info.stage, topic_id=info.topic_id
            )
            outcomes = dict(Counter(o or "pending" for o in found.values()))
        elif status.view == "lead":
            continue  # a stage's lead by the ledger who no longer leads the project
        stages.append(StageProgress(stage=info, status=status, outcomes=outcomes))
    full_texts = tx.execute(
        "select count(*) as n from full_text where project_id = %s", (project_id,)
    ).fetchone()
    return Progress(
        identity_revision_id=revision.id if revision else None,
        identification=get_counts(tx, project_id, revision.id) if revision else None,
        searches=len(list_executions(tx, project_id)) if lead else 0,
        full_texts=(full_texts["n"] if full_texts else 0) if lead else 0,
        stages=tuple(stages),
    )


__all__ = [
    "EXAMPLES",
    "FULL_TEXT_ATTACHED",
    "MEMBER_ADDED",
    "PROJECT_CREATED",
    "ExampleProtocol",
    "FullText",
    "LifecycleError",
    "Member",
    "MemberRole",
    "NotLead",
    "Progress",
    "ProjectSummary",
    "ProtocolRef",
    "RetrievalItem",
    "StageProgress",
    "add_member",
    "attach_acquired_full_text",
    "attach_full_text",
    "attach_sentinel_full_text",
    "check_extractable",
    "corpus_records",
    "create_project",
    "example_protocols",
    "full_text",
    "full_text_records",
    "full_text_sentinels",
    "ingest_corpus",
    "is_lead",
    "list_projects",
    "load_sentinels",
    "members",
    "open_screening",
    "parse_sentinels",
    "parse_source",
    "progress",
    "project_summary",
    "protocol_source",
    "publish_protocol",
    "require_lead",
    "require_project_lead",
    "retrieval",
    "visible_reports",
]
