"""Bundle exporters for the review's tables, as of the bundle's cutoff.

One exporter per module that holds review state: protocol versions, searches and their
occurrences, corpus revisions and identity links, judge configurations and verdicts, screening,
and rendered reports with the project and its actors. Each keeps only rows whose ledger event is
at or before the cutoff. Mutable columns (an assignment's current state, a conflict's state) are
left out or recomputed for the cutoff, so the bundle holds what the ledger recorded then.

Artifacts the rows reference are indexed with the bundle. One that is not in the artifact store
(a recorded test completion, say) is listed by hash with that reason, never silently dropped.

They live here because the report module reads every table already. A module that wants to own
its export can move its exporter into its own package; the names stay the same.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from kittum.report.review import conflict_states
from kittum.screening import AI_DISAGREEMENT_RESCREEN

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kittum.core import Id
    from kittum.db import Tx
    from kittum.ledger import BundleWriter

NOT_STORED = "not in the artifact store when the bundle was sealed"


def _events(tx: Tx, project_id: Id, cutoff_seq: int) -> list[dict[str, Any]]:
    return tx.execute(
        "select event_id, seq, type, payload, actor_id from audit_event"
        " where project_id = %s and seq <= %s order by seq",
        (project_id, cutoff_seq),
    ).fetchall()


def _index(
    bundle: BundleWriter, hashes: Iterable[str | None], media_type: str | None = None
) -> None:
    for h in hashes:
        if not h:
            continue
        try:
            bundle.artifact(h, media_type=media_type)
        except ValueError:
            bundle.artifact(h, media_type=media_type, withheld_reason=NOT_STORED)


def _plain(value: Any) -> Any:
    """Arrays of UUIDs (`uuid[]` columns) as arrays of text, so they store as JSON."""
    if isinstance(value, list):
        return [str(v) if isinstance(v, UUID) else _plain(v) for v in value]
    return value


def _write(
    bundle: BundleWriter, table: str, rows: list[dict[str, Any]], columns: list[str]
) -> None:
    bundle.table(table, columns)
    bundle.insert(table, ({c: _plain(r[c]) for c in columns} for r in rows))


@dataclass
class ProtocolExporter:
    name: str = "protocol"

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:
        events = {e["event_id"] for e in _events(tx, project_id, cutoff_seq)}
        rows = tx.execute(
            "select * from protocol_version where project_id = %s and status = 'published'"
            " order by published_at, id",
            (project_id,),
        ).fetchall()
        _write(
            bundle,
            "protocol_version",
            [r for r in rows if r["approved_event_id"] in events],
            [
                "id",
                "project_id",
                "parent_id",
                "version_label",
                "content_hash",
                "document",
                "created_at",
                "published_at",
                "approved_by",
                "approved_event_id",
                "publish_reason",
            ],
        )


@dataclass
class SearchExporter:
    name: str = "search"

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:
        ids = [
            e["payload"]["execution_id"]
            for e in _events(tx, project_id, cutoff_seq)
            if e["type"] == "search.executed"
        ]
        executions = tx.execute(
            "select * from search_execution where project_id = %s and id = any(%s::uuid[])"
            " order by started_at, id",
            (project_id, ids),
        ).fetchall()
        _write(
            bundle,
            "search_execution",
            executions,
            [
                "id",
                "project_id",
                "topic_id",
                "source",
                "platform",
                "kind",
                "strategy",
                "limits",
                "query_translation",
                "reported_count",
                "retrieved_count",
                "started_at",
                "ended_at",
                "raw_artifact_hashes",
                "skipped_record_ids",
            ],
        )
        occurrences = tx.execute(
            "select * from source_occurrence where project_id = %s and execution_id = any(%s::uuid[])"
            " order by execution_id, ordinal",
            (project_id, ids),
        ).fetchall()
        _write(
            bundle,
            "source_occurrence",
            occurrences,
            [
                "occurrence_id",
                "execution_id",
                "project_id",
                "source",
                "provider_record_id",
                "ordinal",
                "title",
                "abstract",
                "authors",
                "year",
                "journal",
                "doi",
                "pmid",
                "registry_ids",
                "publication_types",
                "raw_hash",
            ],
        )
        _index(bundle, (h for e in executions for h in e["raw_artifact_hashes"]))


@dataclass
class CorpusExporter:
    name: str = "corpus"

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:
        corpus_ids = [
            e["payload"]["corpus_revision_id"]
            for e in _events(tx, project_id, cutoff_seq)
            if e["type"] == "corpus.ingested"
        ]
        _write(
            bundle,
            "corpus_revision",
            tx.execute(
                "select * from corpus_revision where id = any(%s::uuid[]) order by id",
                (corpus_ids,),
            ).fetchall(),
            ["id", "project_id", "created_at"],
        )
        # A proposal's status changes after the cutoff; the confirmations are ledger events.
        _write(
            bundle,
            "proposal",
            tx.execute(
                "select * from proposal where corpus_revision_id = any(%s::uuid[]) order by id",
                (corpus_ids,),
            ).fetchall(),
            ["id", "project_id", "corpus_revision_id", "relation", "occurrence_ids", "evidence"],
        )
        revisions = tx.execute(
            "select * from identity_revision where project_id = %s and finalized"
            " and event_seq <= %s order by event_seq, id",
            (project_id, cutoff_seq),
        ).fetchall()
        _write(
            bundle,
            "identity_revision",
            revisions,
            ["id", "project_id", "parent_id", "event_seq", "event_id", "created_at"],
        )
        links = tx.execute(
            "select * from identity_link where revision_id = any(%s) order by revision_id, id",
            ([r["id"] for r in revisions],),
        ).fetchall()
        _write(
            bundle,
            "identity_link",
            links,
            ["id", "revision_id", "occurrence_id", "report_id", "study_id", "relation", "source"],
        )
        for table, column in (("report", "report_id"), ("study", "study_id")):
            _write(
                bundle,
                table,
                tx.execute(
                    f"select * from {table} where id = any(%s) order by id",
                    (list({link[column] for link in links}),),
                ).fetchall(),
                ["id", "project_id", "created_at"],
            )


@dataclass
class JudgeExporter:
    name: str = "judge"

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:
        events = _events(tx, project_id, cutoff_seq)
        ids = [e["payload"]["verdict_id"] for e in events if e["type"] == "judge.verdict_recorded"]
        opened = {e["event_id"] for e in events}
        verdicts = tx.execute(
            "select * from verdict where project_id = %s and id = any(%s::uuid[])"
            " order by created_at, id",
            (project_id, ids),
        ).fetchall()
        stages = tx.execute(
            "select judge_config_id, opened_event_id from screening_stage where project_id = %s",
            (project_id,),
        ).fetchall()
        config_ids = sorted(
            {v["judge_config_id"] for v in verdicts}
            | {s["judge_config_id"] for s in stages if s["opened_event_id"] in opened}
        )
        _write(
            bundle,
            "judge_config",
            tx.execute(
                "select * from judge_config where id = any(%s) order by id", (config_ids,)
            ).fetchall(),
            [
                "id",
                "model_recipe_id",
                "model_recipe",
                "weights_hash",
                "prompt_version",
                "prompt_hash",
                "schema_hash",
                "decoding",
                "created_at",
            ],
        )
        _write(
            bundle,
            "verdict",
            verdicts,
            [
                "id",
                "project_id",
                "judge_config_id",
                "cache_key",
                "item_ref",
                "scope_id",
                "stage",
                "input_hash",
                "protocol_hash",
                "rendered_prompt_hash",
                "schema_hash",
                "criteria",
                "excludes",
                "flags",
                "model_identity",
                "raw_output_hash",
                "attempts",
                "created_at",
            ],
        )
        for v in verdicts:
            _index(bundle, [v["raw_output_hash"]], "text/plain")
            for a in v["attempts"]:
                _index(bundle, [a.get("rendered_request")], "application/json")
                _index(bundle, [a.get("raw_request_hash"), a.get("raw_response_hash")])
                _index(bundle, [a.get("output_hash")], "text/plain")


@dataclass
class ScreeningExporter:
    name: str = "screening"

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:
        log = _events(tx, project_id, cutoff_seq)
        events = {e["event_id"] for e in log}
        stages = [
            s
            for s in tx.execute(
                "select * from screening_stage where project_id = %s order by opened_at, id",
                (project_id,),
            ).fetchall()
            if s["opened_event_id"] in events
        ]
        stage_ids = [s["id"] for s in stages]
        _write(
            bundle,
            "screening_stage",
            stages,
            [
                "id",
                "project_id",
                "protocol_version_id",
                "protocol_hash",
                "topic_id",
                "scope_id",
                "stage",
                "mode",
                "reveal",
                "judge_config_id",
                "seed",
                "screeners",
                "auditor_id",
                "adjudicators",
                "opened_event_id",
                "opened_at",
            ],
        )
        # `rescreen` is set when an AI-only disagreement sends a record to the auditor; the
        # `screening.ai_disagreement_rescreen` events say which had been by the cutoff.
        rescreened = {
            (e["payload"]["stage_id"], e["payload"]["item_ref"])
            for e in log
            if e["type"] == AI_DISAGREEMENT_RESCREEN
        }
        items = tx.execute(
            "select * from screening_item where stage_id = any(%s) order by stage_id, item_ref",
            (stage_ids,),
        ).fetchall()
        _write(
            bundle,
            "screening_item",
            [{**i, "rescreen": (str(i["stage_id"]), i["item_ref"]) in rescreened} for i in items],
            [
                "stage_id",
                "item_ref",
                "kind",
                "expected_outcome",
                "audit",
                "rescreen",
                "title",
                "text",
                "facts",
            ],
        )
        # The assignment's state, revision, and etag change; the decisions and undos below
        # replay them. The auditor holds sentinels and audit samples from the stage's opening, and
        # an extra screen from its event on.
        audited = {
            (str(i["stage_id"]), i["item_ref"])
            for i in items
            if i["audit"] or i["kind"] == "sentinel"
        }
        assignments = [
            a
            for a in tx.execute(
                "select * from screening_assignment where stage_id = any(%s) order by id",
                (stage_ids,),
            ).fetchall()
            if a["role"] != "auditor" or (str(a["stage_id"]), a["item_ref"]) in audited | rescreened
        ]
        _write(
            bundle,
            "screening_assignment",
            assignments,
            ["id", "stage_id", "item_ref", "reviewer_id", "role", "sort_key"],
        )
        assignment_ids = [a["id"] for a in assignments]
        _write(
            bundle,
            "screening_decision",
            [
                d
                for d in tx.execute(
                    "select * from screening_decision where assignment_id = any(%s)"
                    " order by recorded_at, id",
                    (assignment_ids,),
                ).fetchall()
                if d["event_id"] in events
            ],
            [
                "id",
                "assignment_id",
                "command_id",
                "event_id",
                "phase",
                "outcome",
                "reason_criterion_id",
                "criteria",
                "evidence_anchor_ids",
                "comment",
                "revision",
                "reveal",
                "shown_at",
                "first_key_at",
                "decided_at",
                "reveal_shown_at",
                "recorded_at",
            ],
        )
        _write(
            bundle,
            "screening_undo",
            [
                u
                for u in tx.execute(
                    "select * from screening_undo where assignment_id = any(%s) order by recorded_at",
                    (assignment_ids,),
                ).fetchall()
                if u["event_id"] in events
            ],
            [
                "id",
                "assignment_id",
                "command_id",
                "event_id",
                "undone_decision_ids",
                "revision",
                "recorded_at",
            ],
        )
        readings = [
            r
            for r in tx.execute(
                "select * from screening_ai_reading where stage_id = any(%s)"
                " order by stage_id, item_ref",
                (stage_ids,),
            ).fetchall()
            if r["event_id"] in events
        ]
        _write(
            bundle,
            "screening_ai_reading",
            readings,
            [
                "stage_id",
                "item_ref",
                "judge_config_id",
                "excludes",
                "rank",
                "criteria",
                "flags",
                "raw_output_hash",
                "event_id",
                "recorded_at",
            ],
        )
        _index(bundle, (r["raw_output_hash"] for r in readings), "text/plain")
        adjudications = [
            j
            for j in tx.execute(
                "select j.* from screening_adjudication j join screening_conflict c"
                " on c.id = j.conflict_id where c.stage_id = any(%s) order by j.recorded_at",
                (stage_ids,),
            ).fetchall()
            if j["event_id"] in events
        ]
        # A conflict's kinds and state change; its ledger events replay them.
        states = conflict_states((e["type"], e["payload"], e["event_id"]) for e in log)
        conflicts = []
        for c in tx.execute(
            "select * from screening_conflict where stage_id = any(%s) order by opened_at, id",
            (stage_ids,),
        ).fetchall():
            if (state := states.get(str(c["id"]))) is not None:
                conflicts.append({**c, **state, "kinds": list(state["kinds"])})
        _write(
            bundle,
            "screening_conflict",
            conflicts,
            [
                "id",
                "stage_id",
                "item_ref",
                "kinds",
                "state",
                "opened_event_id",
                "opened_at",
                "closed_event_id",
            ],
        )
        _write(
            bundle,
            "screening_adjudication",
            adjudications,
            [
                "id",
                "conflict_id",
                "adjudicator_id",
                "command_id",
                "event_id",
                "adopted",
                "outcome",
                "reason_criterion_id",
                "criteria",
                "evidence_anchor_ids",
                "comment",
                "shown_at",
                "decided_at",
                "recorded_at",
            ],
        )


@dataclass
class ReportExporter:
    """The project, every actor the bundle names, and the reports rendered by the cutoff."""

    name: str = "report"

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None:
        events = _events(tx, project_id, cutoff_seq)
        _write(
            bundle,
            "project",
            tx.execute("select * from project where id = %s", (project_id,)).fetchall(),
            ["id", "name", "policy_version", "created_at"],
        )
        named = {e["actor_id"] for e in events}
        ids = {e["event_id"] for e in events}
        # The people of the stages opened by the cutoff, and no later stage's.
        for s in tx.execute(
            "select screeners, auditor_id, adjudicators, opened_event_id from screening_stage"
            " where project_id = %s",
            (project_id,),
        ).fetchall():
            if s["opened_event_id"] in ids:
                named |= set(s["screeners"]) | set(s["adjudicators"]) | {s["auditor_id"]}
        _write(
            bundle,
            "actor",
            tx.execute(
                "select * from actor where id = any(%s) order by id", (list(named),)
            ).fetchall(),
            ["id", "identity_ref", "kind", "organization"],
        )
        rendered = [
            r
            for r in tx.execute(
                "select * from report_artifact where project_id = %s order by rendered_at, id",
                (project_id,),
            ).fetchall()
            if r["event_id"] in ids
        ]
        _write(
            bundle,
            "report_artifact",
            rendered,
            [
                "id",
                "project_id",
                "target",
                "scope_id",
                "cutoff_seq",
                "identity_revision_id",
                "index_hash",
                "files",
                "actor_id",
                "event_id",
                "rendered_at",
            ],
        )
        for r in rendered:
            _index(bundle, [r["index_hash"]], "application/json")
            for f in r["files"]:
                _index(bundle, [f["sha256"]], f["media_type"])


EXPORTERS = (
    ProtocolExporter(),
    SearchExporter(),
    CorpusExporter(),
    JudgeExporter(),
    ScreeningExporter(),
    ReportExporter(),
)
