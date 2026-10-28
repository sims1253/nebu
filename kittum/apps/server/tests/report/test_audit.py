"""Time travel over HTTP: the NRT review as the ledger stood when title and abstract closed.

The lead picks the cutoff by name from the milestones, and the PRISMA flow and the AI disclosure
at that cutoff are what the lead's status showed at that moment: title and abstract settled,
every report sought, nothing yet assessed at full text. At the head the same flow has the full
text stage. Nothing is stored for a look back: the ledger does not grow.
"""

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.settings import Settings
from tests.report.support import NRT_SCOPE, NRT_TITLES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.report.support import NrtSeqs, ReviewWorld

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(nrt: tuple[ReviewWorld, NrtSeqs], tmp_path: Path) -> Iterator[TestClient]:
    world, _ = nrt
    settings = Settings(database_url="postgresql://unused", artifact_dir=tmp_path / "artifacts")
    context = AppContext(
        settings=settings, db=world.db, artifacts=world.artifacts, ledger=world.ledger
    )
    with TestClient(create_app(context)) as client:
        yield client


def test_the_flow_when_title_and_abstract_closed_is_the_flow_the_lead_saw_then(
    nrt: tuple[ReviewWorld, NrtSeqs], client: TestClient
) -> None:
    world, seqs = nrt
    base = f"/api/projects/{world.project_id}/audit"
    lead = {"X-Kittum-Reviewer": str(world.people.lead)}
    head = world.head()

    milestones = client.get(f"{base}/milestones", headers=lead).json()
    by_label = {m["label"]: m["seq"] for m in milestones}
    closed = by_label["Title and abstract closed, nrt-vs-control"]
    opened = by_label["Full text opened, nrt-vs-control"]
    assert closed == seqs.title_abstract < opened
    assert by_label["Title and abstract opened, nrt-vs-control"] < seqs.rescreens_open < closed
    assert by_label["Full text, last decision so far, nrt-vs-control"] == seqs.full_text

    def at(cutoff: int | None) -> tuple[dict, dict]:
        params = {} if cutoff is None else {"cutoff_seq": cutoff}
        answer = client.get(f"{base}/as-of", params=params, headers=lead)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        (flow,) = [f for f in body["flows"] if f["scope_id"] == NRT_SCOPE]
        return body, flow

    then, flow = at(closed)
    assert (then["cutoff_seq"], then["head_seq"]) == (closed, head)
    assert then["cutoff_type"] == "screening.adjudicated"

    # What the lead's status showed at that moment.
    (status,) = [s for seq, s in seqs.statuses if seq == closed and s.stage == "title_abstract"]
    assert (
        (
            flow["records_screened"],
            flow["records_awaiting_screening"],
            flow["records_unresolved"],
        )
        == (status.screened, status.remaining, status.conflicts)
        == (33, 0, 0)
    )
    assert flow["records_screened"] == flow["records_excluded"] + flow["reports_sought"]
    sought = {then["items"][ref]["title"] for ref in flow["members"]["reports_sought"]}
    keys = ("patch", "gum", "combination", "followup", "spray", "inhaler")
    assert len(sought) == len(keys) == flow["reports_sought"]
    for key in keys:
        assert any(title.startswith(NRT_TITLES[key]) for title in sought), key
    # Full text had not opened: every report sought, none assessed, none included.
    assert flow["reports_not_retrieved"] == flow["reports_sought"]
    assert flow["reports_assessed"] == flow["reports_included"] == flow["studies_included"] == 0
    assert [s["stage"] for s in then["stages"] if s["scope_id"] == NRT_SCOPE] == ["title_abstract"]
    assert all(c["holds"] for c in flow["checks"])

    # The head: full text screened, three reports in, the inhaler trial never obtained.
    now, latest = at(None)
    assert now["cutoff_seq"] == head
    assert latest["reports_sought"] == flow["reports_sought"]
    assert latest["records_excluded"] == flow["records_excluded"]
    assert (latest["reports_assessed"], latest["reports_included"]) == (5, 3)
    included = [now["items"][ref]["title"] for ref in latest["members"]["reports_included"]]
    for key in ("patch", "gum", "followup"):
        assert any(title.startswith(NRT_TITLES[key]) for title in included), key
    (missing,) = latest["members"]["reports_not_retrieved"]
    assert now["items"][missing]["title"].startswith(NRT_TITLES["inhaler"])
    assert [s["stage"] for s in now["stages"] if s["scope_id"] == NRT_SCOPE] == [
        "title_abstract",
        "full_text",
    ]
    assert now["disclosure_hash"] != then["disclosure_hash"]
    # The same cutoff gives the same disclosure: it is computed from the ledger, nothing else.
    assert at(closed)[0]["disclosure_hash"] == then["disclosure_hash"]

    # A look back stores nothing, a cutoff past the head is refused, and only the lead may ask.
    assert world.head() == head
    beyond = client.get(f"{base}/as-of", params={"cutoff_seq": head + 1}, headers=lead)
    assert beyond.status_code == 400
    for person in (world.people.a, world.people.auditor, world.people.adjudicator):
        refused = client.get(f"{base}/as-of", headers={"X-Kittum-Reviewer": str(person)})
        assert refused.status_code == 403
