"""Group rewrites of an identity revision's links (WP-07).

A link puts one occurrence in a report and the report in a study. Every occurrence of a report
belongs to the report's study, so the rewrites move whole groups: a report merge unions complete
report groups (and with them their studies), a study merge unions complete study groups and leaves
the report groups alone. The functions do no I/O; callers persist the result as a new revision.
"""

from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from kittum.core import new_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from kittum.core import Id
    from kittum.corpus.models import Relation, SplitLevel

type Link = dict[str, Any]
"""`occurrence_id`, `report_id`, `study_id`, `relation`, and `source` of one occurrence."""

_REPORT_LEVEL = ("same_record", "same_report")


def merge_links(
    links: Sequence[Mapping[str, Any]], occurrence_ids: Iterable[Id], relation: Relation
) -> list[Link]:
    """Merge the groups of at least two distinct occurrences at the level of `relation`."""
    by_occurrence = _index(links)
    selected = _select(by_occurrence, occurrence_ids)
    if len(selected) < 2:
        raise ValueError("a merge needs at least two distinct occurrences")

    target = by_occurrence[selected[0]]
    reports = {by_occurrence[o]["report_id"] for o in selected}
    # A report belongs to one study, so merging reports also merges their studies.
    studies = {by_occurrence[o]["study_id"] for o in selected}
    merges_reports = relation in _REPORT_LEVEL

    merged: list[Link] = []
    for row in links:
        new = _copy(row)
        if row["study_id"] in studies:
            new["study_id"] = target["study_id"]
        if merges_reports and row["report_id"] in reports:
            new["report_id"] = target["report_id"]
            if row["relation"] not in _REPORT_LEVEL:
                new["relation"] = relation
        merged.append(new)
    return _relabel(merged)


def split_links(
    links: Sequence[Mapping[str, Any]], occurrence_ids: Iterable[Id], level: SplitLevel
) -> list[Link]:
    """Split occurrences out of their reports, or their report groups out of their studies.

    A report split gives each named occurrence a new report and a new study; the other
    occurrences keep their links. A study split gives each named occurrence's complete report
    group a new study and keeps the report group intact.
    """
    by_occurrence = _index(links)
    selected = set(_select(by_occurrence, occurrence_ids))
    if not selected:
        raise ValueError("a split needs at least one occurrence")

    if level == "report":
        split: list[Link] = []
        for row in links:
            new = _copy(row)
            if row["occurrence_id"] in selected:
                new["report_id"] = new_id()
                new["study_id"] = new_id()
            split.append(new)
        return _relabel(split)

    new_studies = {by_occurrence[o]["report_id"]: new_id() for o in sorted(selected)}
    split = []
    for row in links:
        new = _copy(row)
        if row["report_id"] in new_studies:
            new["study_id"] = new_studies[row["report_id"]]
        split.append(new)
    return _relabel(split)


def _index(links: Sequence[Mapping[str, Any]]) -> dict[Id, Mapping[str, Any]]:
    return {row["occurrence_id"]: row for row in links}


def _select(
    by_occurrence: Mapping[Id, Mapping[str, Any]], occurrence_ids: Iterable[Id]
) -> list[Id]:
    selected = list(dict.fromkeys(occurrence_ids))
    for oid in selected:
        if oid not in by_occurrence:
            raise ValueError(f"Occurrence {oid} is not in this revision")
    return selected


def _copy(row: Mapping[str, Any]) -> Link:
    return {
        key: row[key] for key in ("occurrence_id", "report_id", "study_id", "relation", "source")
    }


def _relabel(links: list[Link]) -> list[Link]:
    """Label each link with the strongest identity it shares with another occurrence."""
    report_sizes = Counter(row["report_id"] for row in links)
    study_reports: dict[Id, set[Id]] = defaultdict(set)
    for row in links:
        study_reports[row["study_id"]].add(row["report_id"])
    for row in links:
        if report_sizes[row["report_id"]] > 1:
            continue
        row["relation"] = "same_study" if len(study_reports[row["study_id"]]) > 1 else "standalone"
    return links
