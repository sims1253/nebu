"""Review outputs (DESIGN.md, "Reports" and "The handover bundle").

Nobody types counts: every target renders from a `Review`, the review as the ledger recorded it
through a cutoff (`load_review`). The core targets are:

- `prisma`: the PRISMA 2020 flow per scope as JSON and SVG, with the reports behind every count
  and the sums that must hold;
- `ris`: one undeduplicated RIS file per search, the deduplicated corpus, and each scope's
  included and excluded lists;
- `search_report`: every search with the fields PRISMA-S asks for;
- `ai_disclosure`: the models, weights, licences, prompt hashes, modes, reveal settings,
  agreement, audit results, and the judge filter's limits, as data and as one RAISE-aligned
  rendering;
- `bundle`: the handover bundle, sealed with `seal_bundle` rather than rendered.

`render` stores a target's files by hash and returns an `Artifact`; `record_render` keeps the
render in `report_artifact` with a `report.rendered` event, so a download can be limited to
what was rendered for the project. `seal_bundle` renders every target into `reports/`, profile
targets into `reports/dossier/`, and seals the bundle with every registered exporter: this
package registers the review's tables (protocol, search, corpus, judge, screening, report) at
import.

Profiles add targets with `register_target` under IDs of the form `<profile id>.<name>`. A
profile renderer gets the same `Review` and returns files; core never imports a profile.

Owner: WP-14.
"""

from typing import TYPE_CHECKING, Literal, get_args

from psycopg.types.json import Jsonb
from pydantic import Field

from kittum.core import Id, KittumError, Record, Sha256, SystemClock, canonical_json, new_id
from kittum.ledger import EXPORTERS as LEDGER_EXPORTERS
from kittum.ledger import EventDraft, PostgresLedger, register_exporter
from kittum.ledger import seal as seal_ledger
from kittum.people import is_lead
from kittum.report.exporters import EXPORTERS as _EXPORTERS
from kittum.report.prisma import (
    AiInvolvement,
    Check,
    PrismaFlow,
    ReasonCount,
    SourceCount,
    flow,
    flows,
)
from kittum.report.renderers import (
    CORE_RENDERERS,
    RenderedFile,
    Renderer,
    disclosure,
    json_file,
    md_table,
    representative,
    safe_name,
    search_file_name,
    search_rows,
    text_file,
)
from kittum.report.review import (
    Actor,
    Adjudication,
    Conflict,
    HumanDecision,
    Identity,
    ItemResult,
    JudgeInfo,
    Link,
    ReportRequestError,
    Review,
    SearchRun,
    StageResult,
    head_seq,
    load_review,
)
from kittum.report.ris_format import one_line, ris_dumps, ris_entry

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.core import Clock
    from kittum.db import Tx
    from kittum.ledger import Bundle, BundleWriter, Ledger, SigningKey
    from kittum.storage import ArtifactStore

type CoreTarget = Literal["prisma", "ris", "search_report", "ai_disclosure", "bundle"]
type Target = str  # a CoreTarget, or "<profile id>.<name>" from a profile

RENDERED_EVENT = "report.rendered"
REPORTS_DIR = "reports"
DOSSIER_DIR = "dossier"


class ArtifactFile(Record):
    path: str
    sha256: Sha256
    media_type: str


class Artifact(Record):
    target: Target
    project_id: Id
    scope_id: str | None = None
    cutoff_seq: int
    identity_revision_id: Id | None = None
    files: tuple[ArtifactFile, ...]


CORE_TARGETS: tuple[CoreTarget, ...] = get_args(CoreTarget.__value__)
_profile_targets: dict[Target, Renderer] = {}


def register_target(target: Target, renderer: Renderer) -> None:
    """Add a profile's target. Called from the entry points, once per process."""
    profile, dot, name = target.partition(".")
    if not (profile and dot and name):
        raise ValueError(f"profile targets are named '<profile id>.<name>', got {target!r}")
    if target in _profile_targets:
        raise ValueError(f"target {target!r} is already registered")
    _profile_targets[target] = renderer


def targets(profile: str | None = None) -> tuple[Target, ...]:
    """The core targets, then the profile targets: all of them, or only `profile`'s."""
    names = sorted(t for t in _profile_targets if profile is None or t.startswith(f"{profile}."))
    return (*CORE_TARGETS, *names)


def _renderer(target: Target) -> Renderer:
    if target in _profile_targets:
        return _profile_targets[target]
    if target in CORE_RENDERERS:
        return CORE_RENDERERS[target]
    if target == "bundle":
        raise ReportRequestError("the bundle is sealed with seal_bundle, not rendered")
    raise ReportRequestError(f"unknown report target {target!r}")


def render_files(review: Review, target: Target, scope_id: str | None = None) -> list[RenderedFile]:
    """A target's files for a review, without storing anything."""
    renderer = _renderer(target)
    profile = target.partition(".")[0] if target in _profile_targets else None
    if profile is not None and review.document.profile != profile:
        raise ReportRequestError(f"target {target!r} needs a protocol with profile {profile!r}")
    return list(renderer(review, scope_id))


def render(
    tx: Tx,
    target: Target,
    project_id: Id,
    scope_id: str | None = None,
    *,
    store: ArtifactStore,
    cutoff_seq: int | None = None,
    identity_revision_id: Id | None = None,
    ledger: Ledger | None = None,
) -> Artifact:
    """Render a target at the cutoff (default: the ledger head) and store its files by hash."""
    review = load_review(
        tx,
        project_id,
        cutoff_seq=cutoff_seq,
        identity_revision_id=identity_revision_id,
        ledger=ledger,
    )
    files = render_files(review, target, scope_id)
    return Artifact(
        target=target,
        project_id=project_id,
        scope_id=scope_id,
        cutoff_seq=review.cutoff_seq,
        identity_revision_id=review.identity.revision_id if review.identity else None,
        files=tuple(
            ArtifactFile(path=f.path, sha256=store.put(f.data), media_type=f.media_type)
            for f in files
        ),
    )


def record_render(
    tx: Tx,
    artifact: Artifact,
    *,
    store: ArtifactStore,
    actor_id: Id,
    ledger: Ledger | None = None,
    clock: Clock | None = None,
) -> Sha256:
    """Keep a render: its index goes to the store, a row and a `report.rendered` event to `tx`.

    Returns the index hash, which names the artifact.
    """
    ledger = ledger or PostgresLedger(clock)
    index_hash = store.put(canonical_json(artifact))
    render_id = new_id()
    files = [f.model_dump(mode="json") for f in artifact.files]
    event = ledger.append(
        tx,
        EventDraft(
            project_id=artifact.project_id,
            type=RENDERED_EVENT,
            schema_version=1,
            actor_id=actor_id,
            payload={
                "render_id": str(render_id),
                "target": artifact.target,
                "scope_id": artifact.scope_id,
                "cutoff_seq": artifact.cutoff_seq,
                "identity_revision_id": str(artifact.identity_revision_id)
                if artifact.identity_revision_id
                else None,
                "index_hash": index_hash,
                "files": files,
            },
        ),
    )
    tx.execute(
        "insert into report_artifact (id, project_id, target, scope_id, cutoff_seq,"
        " identity_revision_id, index_hash, files, actor_id, event_id, rendered_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            render_id,
            artifact.project_id,
            artifact.target,
            artifact.scope_id,
            artifact.cutoff_seq,
            artifact.identity_revision_id,
            index_hash,
            Jsonb(files),
            actor_id,
            event.event_id,
            event.recorded_at,
        ),
    )
    return index_hash


class NotTheLead(KittumError):
    """The actor is not a methods lead of the project (`kittum.people`), or is one who also
    screens, audits, or adjudicates a stage, or is not a human.

    Reports show every reviewer's decisions, the AI's readings, and its raw outputs, so only the
    methods lead may render, download, or seal them: the person `Screening.status` gives the
    lead's view. Anyone else, another reviewer of the project included, is refused.
    """


def require_lead(tx: Tx, project_id: Id, actor_id: Id) -> None:
    """Raise `NotTheLead` unless the actor may see the whole review."""
    actor = tx.execute("select kind from actor where id = %s", (actor_id,)).fetchone()
    if actor is None or actor["kind"] != "human":
        raise NotTheLead(f"actor {actor_id} is not a human")
    if not is_lead(tx, project_id, actor_id):
        raise NotTheLead(f"actor {actor_id} is not a methods lead of this project")
    role = tx.execute(
        "select 1 from screening_stage where project_id = %s"
        " and (%s = any(screeners) or auditor_id = %s or %s = any(adjudicators)) limit 1",
        (project_id, actor_id, actor_id, actor_id),
    ).fetchone()
    if role is not None:
        raise NotTheLead(
            f"actor {actor_id} screens, audits, or adjudicates in this project; only the "
            "methods lead sees its reports"
        )


def rendered_file(tx: Tx, project_id: Id, sha256: str) -> str | None:
    """The media type of a file or index rendered for the project, or None if it was not."""
    row = tx.execute(
        "select index_hash, files from report_artifact where project_id = %s"
        " and (index_hash = %s or files @> %s) limit 1",
        (project_id, sha256, Jsonb([{"sha256": sha256}])),
    ).fetchone()
    if row is None:
        return None
    if row["index_hash"] == sha256:
        return "application/json"
    return next(f["media_type"] for f in row["files"] if f["sha256"] == sha256)


# The handover bundle


class RightsProfile(Record):
    """Which artifacts may leave with a bundle. A withheld artifact is listed by hash and reason.

    Rows that describe a record (titles, abstracts) still travel; the raw files do not.
    """

    id: str = "default"
    withhold_sources: dict[str, str] = Field(default_factory=dict)
    """Search source (as run, or the protocol source ID) -> reason; withholds its raw files."""
    withhold_artifacts: dict[str, str] = Field(default_factory=dict)
    """Artifact hash -> reason."""


def bundle_reports(review: Review) -> dict[str, bytes]:
    """Every target the review renders, by bundle path: core targets under `reports/<target>/`,
    the protocol's profile targets under `reports/dossier/<name>/`."""
    files: dict[str, bytes] = {}
    profile = review.document.profile
    own = sorted(t for t in _profile_targets if profile and t.startswith(f"{profile}."))
    for target in (*CORE_RENDERERS, *own):
        if target in _profile_targets:
            base = f"{REPORTS_DIR}/{DOSSIER_DIR}/{target.partition('.')[2]}"
        else:
            base = f"{REPORTS_DIR}/{target}"
        for f in render_files(review, target):
            files[f"{base}/{f.path}"] = f.data
    return files


class _Reports:
    name = "reports"

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:  # noqa: ARG002
        for path, data in self.files.items():
            bundle.file(path, data)


class _Rights:
    name = "rights"

    def __init__(self, rights: RightsProfile, review: Review) -> None:
        self.rights = rights
        self.review = review

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:  # noqa: ARG002
        withheld = dict(self.rights.withhold_artifacts)
        for run in self.review.searches:
            plan = run.execution.plan
            names = {plan.source, *([run.source.id] if run.source else [])}
            reason = next(
                (
                    self.rights.withhold_sources[n]
                    for n in names
                    if n in self.rights.withhold_sources
                ),
                None,
            )
            if reason is not None:
                for h in run.execution.raw_artifact_hashes:
                    withheld.setdefault(h, reason)
        for h, reason in sorted(withheld.items()):
            if h in bundle.artifacts:
                media_type = bundle.artifacts[h][0]
                bundle.artifact(
                    h, media_type=media_type, withheld_reason=f"{self.rights.id}: {reason}"
                )


def seal_bundle(
    tx: Tx,
    project_id: Id,
    store: ArtifactStore,
    out_dir: Path,
    *,
    signer: SigningKey,
    actor_id: Id,
    cutoff_seq: int | None = None,
    rights: RightsProfile | None = None,
    ledger: Ledger | None = None,
    clock: Clock | None = None,
) -> Bundle:
    """Seal the handover bundle at the cutoff (default: the ledger head) with every report.

    The reports render from the same cutoff as the tables, so every count in `reports/` can be
    recomputed from `kittum.sqlite`.
    """
    clock = clock or SystemClock()
    ledger = ledger or PostgresLedger(clock)
    cutoff = head_seq(tx, project_id) if cutoff_seq is None else cutoff_seq
    review = load_review(tx, project_id, cutoff_seq=cutoff, ledger=ledger)
    exporters = [
        *LEDGER_EXPORTERS,
        _Reports(bundle_reports(review)),
        _Rights(rights or RightsProfile(), review),
    ]
    return seal_ledger(
        tx,
        project_id,
        cutoff,
        store,
        out_dir,
        signer=signer,
        actor_id=actor_id,
        ledger=ledger,
        exporters=exporters,
        clock=clock,
    )


for _exporter in _EXPORTERS:
    register_exporter(_exporter)


__all__ = [
    "CORE_TARGETS",
    "DOSSIER_DIR",
    "RENDERED_EVENT",
    "REPORTS_DIR",
    "Actor",
    "Adjudication",
    "AiInvolvement",
    "Artifact",
    "ArtifactFile",
    "Check",
    "Conflict",
    "CoreTarget",
    "HumanDecision",
    "Identity",
    "ItemResult",
    "JudgeInfo",
    "Link",
    "NotTheLead",
    "PrismaFlow",
    "ReasonCount",
    "RenderedFile",
    "Renderer",
    "ReportRequestError",
    "Review",
    "RightsProfile",
    "SearchRun",
    "SourceCount",
    "StageResult",
    "Target",
    "bundle_reports",
    "disclosure",
    "flow",
    "flows",
    "head_seq",
    "json_file",
    "load_review",
    "md_table",
    "one_line",
    "record_render",
    "register_target",
    "render",
    "render_files",
    "rendered_file",
    "representative",
    "require_lead",
    "ris_dumps",
    "ris_entry",
    "safe_name",
    "seal_bundle",
    "search_file_name",
    "search_rows",
    "targets",
    "text_file",
]
