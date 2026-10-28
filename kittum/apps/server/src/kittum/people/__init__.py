"""Who works on a project, and in which roles: lead, reviewer, auditor, adjudicator.

The table `project_member` (migration `20261029040000_app_projects.sql`) holds one row per
person and role. Screening stages take their screeners, auditor, and adjudicators from it, and
`is_lead` decides who may see the whole review: every record, a stage's agreement and audit
figures, the reports. Until the API has authentication, the person comes from its identity
stand-in (`X-Kittum-Reviewer`).

Writes go through `kittum.app.lifecycle`, which records each change in the ledger.

Owner: WP-15.
"""

from typing import TYPE_CHECKING, Literal

from kittum.core import Id, Record

if TYPE_CHECKING:
    from kittum.db import Tx

type MemberRole = Literal["lead", "reviewer", "auditor", "adjudicator"]

ROLE_ORDER: tuple[MemberRole, ...] = ("lead", "reviewer", "auditor", "adjudicator")


class Member(Record):
    actor_id: Id
    display_name: str
    roles: tuple[MemberRole, ...]


def record_roles(
    tx: Tx,
    project_id: Id,
    actor_id: Id,
    display_name: str,
    roles: tuple[MemberRole, ...],
    *,
    event_id: Id,
) -> None:
    """Give a person roles in the project; `event_id` is the ledger event that records it."""
    for role in roles:
        tx.execute(
            "insert into project_member (project_id, actor_id, role, display_name, event_id)"
            " values (%s, %s, %s, %s, %s) on conflict do nothing",
            (project_id, actor_id, role, display_name, event_id),
        )


def members(tx: Tx, project_id: Id) -> list[Member]:
    """The lead first, then the reviewers in the order they joined, the auditor, the
    adjudicators."""
    rows = tx.execute(
        "select actor_id, display_name, array_agg(role order by role) as roles,"
        " min(event_id::text) as first_event from project_member where project_id = %s"
        " group by actor_id, display_name order by first_event",
        (project_id,),
    ).fetchall()
    people = [
        Member(actor_id=r["actor_id"], display_name=r["display_name"], roles=tuple(r["roles"]))
        for r in rows
    ]
    return sorted(people, key=lambda m: min(ROLE_ORDER.index(r) for r in m.roles))


def is_lead(tx: Tx, project_id: Id, actor_id: Id) -> bool:
    """Whether the person is a methods lead of the project."""
    row = tx.execute(
        "select 1 from project_member where project_id = %s and actor_id = %s and role = 'lead'",
        (project_id, actor_id),
    ).fetchone()
    return row is not None


__all__ = ["ROLE_ORDER", "Member", "MemberRole", "is_lead", "members", "record_roles"]
