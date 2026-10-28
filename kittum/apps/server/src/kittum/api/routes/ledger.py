"""Ledger routes: verify a project's hash chain.

Events themselves are not served here: their payloads hold every reviewer's decisions, so the
audit routes (`routes/audit.py`) serve them to the methods lead only. The counts by type reveal
nothing a project's status does not.
"""

from collections import Counter

from fastapi import APIRouter

from kittum.api import Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.core import Id, Record
from kittum.ledger import VerifyReport

router = APIRouter(prefix="/api/projects/{project_id}/ledger", tags=["ledger"])


class LedgerSummary(Record):
    verify: VerifyReport
    events: int
    by_type: dict[str, int]


@router.get("/verify", operation_id="verifyProjectLedger")
def verify_ledger(project_id: Id, ctx: Context) -> LedgerSummary:
    """Recompute every event hash and link of the project's chain."""
    ledger = ctx.require_ledger()
    with ctx.db.transaction() as tx:
        report = ledger.verify(tx, project_id)
        events = ledger.events(tx, project_id)
    return LedgerSummary(
        verify=report,
        events=len(events),
        by_type=dict(sorted(Counter(e.type for e in events).items())),
    )
