"""Screening rows in Postgres. Reviewer reads go through the SQL functions of the migration,
which return nothing a reviewer may not see; everything else here is for the service itself."""

from typing import TYPE_CHECKING, Any

from kittum.core import NotFoundError
from kittum.judge import CriterionVerdict
from kittum.protocol import ScreeningMode
from kittum.screening.rules import Reading
from kittum.screening.types import (
    CriterionDecision,
    Decision,
    InvalidDecision,
    QueueRecord,
    RecordFacts,
    StageInfo,
)

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Tx


def stage_info(row: dict[str, Any]) -> StageInfo:
    return StageInfo(
        stage_id=row["id"],
        project_id=row["project_id"],
        scope_id=row["scope_id"],
        stage=row["stage"],
        topic_id=row["topic_id"],
        mode=ScreeningMode(row["mode"]),
        reveal=row["reveal"],
        protocol_hash=row["protocol_hash"],
        judge_config_id=row["judge_config_id"],
        screeners=tuple(row["screeners"]),
        auditor_id=row["auditor_id"],
        adjudicators=tuple(row["adjudicators"]),
    )


def stage_row(tx: Tx, project_id: Id, stage_id: Id) -> dict[str, Any]:
    row = tx.execute(
        "select * from screening_stage where id = %s and project_id = %s", (stage_id, project_id)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"screening stage {stage_id} not found in project {project_id}")
    return row


def find_stage(
    tx: Tx, project_id: Id, scope_id: str, stage: str, topic_id: str | None
) -> dict[str, Any]:
    rows = tx.execute(
        "select * from screening_stage where project_id = %s and scope_id = %s and stage = %s"
        " and (%s::text is null or topic_id = %s)",
        (project_id, scope_id, stage, topic_id, topic_id),
    ).fetchall()
    if not rows:
        raise NotFoundError(f"scope {scope_id!r} has no open {stage} screening")
    if len(rows) > 1:
        topics = ", ".join(sorted(r["topic_id"] for r in rows))
        raise InvalidDecision(f"scope {scope_id!r} is screened in several topics ({topics})")
    return rows[0]


def decision_of(row: dict[str, Any], prefix: str = "") -> Decision:
    return Decision(
        outcome=row[f"{prefix}outcome"],
        reason_criterion_id=row[f"{prefix}reason_criterion_id"],
        evidence_anchor_ids=tuple(row[f"{prefix}evidence_anchor_ids"]),
        comment=row[f"{prefix}comment"],
        criteria=tuple(CriterionDecision.model_validate(c) for c in row[f"{prefix}criteria"]),
    )


def reading_of(row: dict[str, Any]) -> Reading:
    return Reading(
        excludes=row["excludes"],
        criteria=tuple(CriterionVerdict.model_validate(c) for c in row["criteria"]),
        flags=tuple(row["flags"]),
    )


def queue_record(facts: dict[str, Any], **extra: Any) -> QueueRecord:
    return QueueRecord(**RecordFacts.model_validate(facts).model_dump(), **extra)


def reading(tx: Tx, stage_id: Id, item_ref: str) -> Reading | None:
    row = tx.execute(
        "select excludes, criteria, flags from screening_ai_reading"
        " where stage_id = %s and item_ref = %s",
        (stage_id, item_ref),
    ).fetchone()
    return reading_of(row) if row else None


_ASSIGNMENT = """
    select a.*, i.kind, i.audit, i.rescreen,
        p.outcome as p_outcome, p.reason_criterion_id as p_reason_criterion_id,
        p.evidence_anchor_ids as p_evidence_anchor_ids, p.comment as p_comment,
        p.criteria as p_criteria,
        f.outcome as f_outcome, f.reason_criterion_id as f_reason_criterion_id,
        f.evidence_anchor_ids as f_evidence_anchor_ids, f.comment as f_comment,
        f.criteria as f_criteria
    from screening_assignment a
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    left join screening_decision p on p.id = a.provisional_decision_id
    left join screening_decision f on f.id = a.final_decision_id
"""


def assignment(tx: Tx, assignment_id: Id, *, lock: bool = False) -> dict[str, Any] | None:
    sql = _ASSIGNMENT + " where a.id = %s" + (" for update of a" if lock else "")
    return tx.execute(sql, (assignment_id,)).fetchone()


def item_assignments(tx: Tx, stage_id: Id, item_ref: str) -> list[dict[str, Any]]:
    return tx.execute(
        _ASSIGNMENT + " where a.stage_id = %s and a.item_ref = %s order by a.role, a.id",
        (stage_id, item_ref),
    ).fetchall()


def conflicts(tx: Tx, stage_id: Id, item_ref: str) -> list[dict[str, Any]]:
    return tx.execute(
        "select * from screening_conflict where stage_id = %s and item_ref = %s"
        " order by opened_at, id",
        (stage_id, item_ref),
    ).fetchall()


def item(tx: Tx, stage_id: Id, item_ref: str) -> dict[str, Any]:
    row = tx.execute(
        "select * from screening_item where stage_id = %s and item_ref = %s", (stage_id, item_ref)
    ).fetchone()
    assert row is not None
    return row
