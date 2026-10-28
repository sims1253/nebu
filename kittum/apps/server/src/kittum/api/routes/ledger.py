"""Ledger routes: verify a project's hash chain.

Events themselves are not served here: their payloads hold every reviewer's decisions, so the
audit routes (`routes/audit.py`) serve them to the methods lead only. Anyone may learn whether the chain holds and
how far it was checked. The event counts by type, and where a broken chain breaks, are the methods
lead's alone (`require_lead`): the counts of acquisition or retrieval events say how many reports
were sought for retrieval while reviewers still screen.
"""

from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Header

from kittum.api import Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.core import Id, Record
from kittum.ledger import VerifyReport
from kittum.screening import sees_lead_data

router = APIRouter(prefix="/api/projects/{project_id}/ledger", tags=["ledger"])


class LedgerSummary(Record):
    ok: bool
    checked_through_seq: int
    verify: VerifyReport | None = None
    """The full report, for the methods lead only."""
    events: int | None = None
    """For the methods lead only."""
    by_type: dict[str, int] | None = None
    """Event counts by type, for the methods lead only."""


@router.get("/verify", operation_id="verifyProjectLedger")
def verify_ledger(
    project_id: Id,
    ctx: Context,
    viewer: Annotated[
        Id | None,
        Header(
            alias="X-Kittum-Reviewer",
            description="The caller's actor ID. A stand-in until the API has authentication.",
        ),
    ] = None,
) -> LedgerSummary:
    """Recompute every event hash and link of the project's chain."""
    ledger = ctx.require_ledger()
    with ctx.db.transaction() as tx:
        report = ledger.verify(tx, project_id)
        lead = viewer is not None and sees_lead_data(tx, project_id, viewer)
        events = ledger.events(tx, project_id) if lead else []
    summary = LedgerSummary(ok=report.ok, checked_through_seq=report.checked_through_seq)
    if not lead:
        return summary
    return summary.model_copy(
        update={
            "verify": report,
            "events": len(events),
            "by_type": dict(sorted(Counter(e.type for e in events).items())),
        }
    )
