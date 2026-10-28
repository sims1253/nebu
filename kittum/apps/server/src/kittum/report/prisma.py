"""The PRISMA 2020 flow per scope, from a frozen identity revision and the screening decisions.

Every count is the length of a list of item references (`PrismaFlow.members`), so a number in
the flow traces to the reports behind it and, through the bundle, to their decisions, events,
and reviewers. `checks` states the sums that must hold; a broken one is reported, not hidden.

PRISMA 2020's "records marked as ineligible by automation tools" is always zero here: no
screening mode lets the AI exclude a record that no human saw. What the AI did contribute is
counted separately: exclusions the AI also proposed, exclusions decided after a reveal, and
exclusions that stood after a blind extra screen the AI's disagreement triggered (whether the
auditor upheld them or an adjudicator decided).
"""

from collections import Counter
from html import escape
from typing import TYPE_CHECKING
from uuid import UUID

from kittum.core import Id, Record

if TYPE_CHECKING:
    from kittum.report.review import ItemResult, Review, StageResult


class SourceCount(Record):
    source: str  # the protocol source ID, or the search's own source name
    name: str
    kind: str  # bibliographic, registry, website, other
    records: int


class ReasonCount(Record):
    criterion_id: str
    reports: int


class AiInvolvement(Record):
    """Human exclusions the AI took part in. The AI alone never excludes."""

    marked_ineligible_by_automation: int = 0
    excluded_with_ai_proposal: int
    """The humans excluded, and the AI's verified reading also excluded."""
    excluded_after_ai_reveal: int
    """A screener saw the AI's reading before the final decision."""
    excluded_after_ai_rescreen: int
    """The AI's disagreement sent the record to the auditor's blind extra screen, and it was still
    excluded."""
    excluded_with_ai_involvement: int
    """Any of the three; each record counts once."""


class Check(Record):
    name: str
    expected: int
    actual: int
    holds: bool


class PrismaFlow(Record):
    topic_id: str
    scope_id: str
    identity_revision_id: Id | None
    cutoff_seq: int
    identified_by_source: tuple[SourceCount, ...]
    records_identified: int
    identified_from_databases: int
    identified_from_registers: int
    identified_from_other_sources: int
    duplicates_removed: int
    records_outside_identity: int
    """Records of the topic's searches that the identity revision does not hold yet (a search run
    after it was frozen). They are in no other count."""
    records_screened: int
    """Decided by every screener, and by the auditor if the AI sent it to an extra screen."""
    records_awaiting_screening: int
    records_excluded: int
    records_unresolved: int  # screened, but in conflict at title and abstract
    ai_title_abstract: AiInvolvement
    reports_sought: int
    reports_not_retrieved: int
    reports_assessed: int
    reports_awaiting_assessment: int  # retrieved, not yet decided by every screener
    reports_excluded: int
    exclusion_reasons: tuple[ReasonCount, ...]
    reports_awaiting_classification: int  # insufficient information at full text
    reports_unresolved: int  # assessed, but in conflict at full text
    ai_full_text: AiInvolvement
    reports_included: int
    studies_included: int
    members: dict[str, tuple[str, ...]]
    """The item references (corpus report IDs, or occurrence IDs for identification) behind each
    count, keyed by the count's field name."""
    checks: tuple[Check, ...]


def _ai(excluded: list[ItemResult]) -> tuple[AiInvolvement, dict[str, tuple[str, ...]]]:
    proposal = [i.item_ref for i in excluded if i.ai_excludes]
    reveal = [i.item_ref for i in excluded if i.ai_revealed]
    rescreened = [i.item_ref for i in excluded if i.ai_rescreened]
    union = sorted(set(proposal) | set(reveal) | set(rescreened))
    return (
        AiInvolvement(
            excluded_with_ai_proposal=len(proposal),
            excluded_after_ai_reveal=len(reveal),
            excluded_after_ai_rescreen=len(rescreened),
            excluded_with_ai_involvement=len(union),
        ),
        {
            "excluded_with_ai_proposal": tuple(sorted(proposal)),
            "excluded_after_ai_reveal": tuple(sorted(reveal)),
            "excluded_after_ai_rescreen": tuple(sorted(rescreened)),
            "excluded_with_ai_involvement": tuple(union),
        },
    )


def _as_id(ref: str) -> UUID | None:
    """A corpus report ID, or None for an item that is not one (a sentinel)."""
    try:
        return UUID(ref)
    except ValueError:
        return None


def _records(stage: StageResult | None) -> list[ItemResult]:
    return list(stage.records) if stage else []


def flow(review: Review, topic_id: str, scope_id: str) -> PrismaFlow:
    """The flow of one scope of one search topic."""
    identity = review.identity
    report_of = identity.report_of() if identity else {}
    study_of = identity.study_of() if identity else {}

    # Identification: the occurrences of the topic's searches that the frozen identity revision
    # holds, deduplicated by its reports. Records of searches run after it are listed apart.
    by_source: dict[str, SourceCount] = {}
    occurrences: list[str] = []
    unique_reports: set[str] = set()
    kinds: Counter[str] = Counter()
    by_occurrence = review.records_by_occurrence()
    for link in identity.links if identity else ():
        found = by_occurrence.get(link.occurrence_id)
        if found is None or found[0].execution.plan.topic_id != topic_id:
            continue
        run = found[0]
        key = run.source.id if run.source else run.execution.plan.source
        name = run.source.name if run.source else run.execution.plan.platform
        known = by_source.get(key)
        by_source[key] = SourceCount(
            source=key, name=name, kind=run.source_kind, records=(known.records if known else 0) + 1
        )
        kinds[run.source_kind] += 1
        occurrences.append(str(link.occurrence_id))
        unique_reports.add(str(link.report_id))
    unique = sorted(unique_reports)
    outside = sorted(
        str(r.occurrence_id)
        for run in review.searches
        if run.execution.plan.topic_id == topic_id
        for r in run.execution.records
        if r.occurrence_id not in report_of
    )

    # Screening: a record counts as screened once every screener (and the auditor of an extra
    # screen) has decided it, as screening counts it; the rest await screening.
    ta = review.stage(scope_id, "title_abstract", topic_id)
    ft = review.stage(scope_id, "full_text", topic_id)
    stage_records = _records(ta)
    screened = [i for i in stage_records if i.screened]
    awaiting = [i for i in stage_records if not i.screened]
    excluded = [i for i in screened if i.outcome == "exclude"]
    sought = [i for i in screened if i.outcome == "include"]
    unresolved = [i for i in screened if i.outcome in ("pending", "conflict")]
    sought_refs = {i.item_ref for i in sought}
    retrieved = [i for i in _records(ft) if i.item_ref in sought_refs]
    not_sought = sorted(i.item_ref for i in _records(ft) if i.item_ref not in sought_refs)
    not_retrieved = sorted(sought_refs - {i.item_ref for i in retrieved})
    assessed = [i for i in retrieved if i.screened]
    awaiting_assessment = [i for i in retrieved if not i.screened]
    ft_excluded = [i for i in assessed if i.outcome == "exclude"]
    awaiting_classification = [i for i in assessed if i.outcome == "insufficient_information"]
    ft_unresolved = [i for i in assessed if i.outcome in ("pending", "conflict")]
    included = [i for i in assessed if i.outcome == "include"]
    reasons: Counter[str] = Counter()
    by_reason: dict[str, list[str]] = {}
    for i in ft_excluded:
        # One reason per report: the adjudicated one, or the one both screeners agreed on.
        reason = i.reason_criterion_ids[0] if i.reason_criterion_ids else "unstated"
        reasons[reason] += 1
        by_reason.setdefault(f"reason:{reason}", []).append(i.item_ref)
    studies = sorted(
        {str(study) for i in included if (study := study_of.get(_as_id(i.item_ref))) is not None}
    )
    ai_ta, ai_ta_members = _ai(excluded)
    ai_ft, ai_ft_members = _ai(ft_excluded)

    def refs(items: list[ItemResult]) -> tuple[str, ...]:
        return tuple(sorted(i.item_ref for i in items))

    members: dict[str, tuple[str, ...]] = {
        "records_identified": tuple(sorted(occurrences)),
        "unique_records": tuple(unique),
        "records_outside_identity": tuple(outside),
        "records_screened": refs(screened),
        "records_awaiting_screening": refs(awaiting),
        "records_excluded": refs(excluded),
        "records_unresolved": refs(unresolved),
        "reports_sought": refs(sought),
        "reports_not_retrieved": tuple(not_retrieved),
        "reports_assessed": refs(assessed),
        "reports_awaiting_assessment": refs(awaiting_assessment),
        "full_texts_not_sought": tuple(not_sought),
        "reports_excluded": refs(ft_excluded),
        "reports_awaiting_classification": refs(awaiting_classification),
        "reports_unresolved": refs(ft_unresolved),
        "reports_included": refs(included),
        "studies_included": tuple(studies),
        **{f"title_abstract:{k}": v for k, v in ai_ta_members.items()},
        **{f"full_text:{k}": v for k, v in ai_ft_members.items()},
        **{k: tuple(sorted(v)) for k, v in by_reason.items()},
    }
    stage_refs = {i.item_ref for i in stage_records}

    def check(name: str, expected: int, actual: int) -> Check:
        return Check(name=name, expected=expected, actual=actual, holds=expected == actual)

    checks = (
        check(
            "identified minus duplicates equals screened plus awaiting screening",
            len(unique),
            len(screened) + len(awaiting),
        ),
        check(
            "every unique record is in the screening stage",
            len(unique),
            len(set(unique) & stage_refs),
        ),
        check(
            "screened equals excluded plus sought plus unresolved",
            len(screened),
            len(excluded) + len(sought) + len(unresolved),
        ),
        check(
            "sought equals assessed plus awaiting assessment plus not retrieved",
            len(sought),
            len(assessed) + len(awaiting_assessment) + len(not_retrieved),
        ),
        check("every full text assessed was sought", 0, len(not_sought)),
        check(
            "assessed equals excluded plus included plus awaiting plus unresolved",
            len(assessed),
            len(ft_excluded) + len(included) + len(awaiting_classification) + len(ft_unresolved),
        ),
        check(
            "excluded full texts equal the sum over reasons",
            len(ft_excluded),
            sum(reasons.values()),
        ),
        check(
            "every included report belongs to a study",
            len(included),
            sum(1 for i in included if _as_id(i.item_ref) in study_of),
        ),
    )
    return PrismaFlow(
        topic_id=topic_id,
        scope_id=scope_id,
        identity_revision_id=identity.revision_id if identity else None,
        cutoff_seq=review.cutoff_seq,
        identified_by_source=tuple(by_source[k] for k in sorted(by_source)),
        records_identified=len(occurrences),
        identified_from_databases=kinds["bibliographic"],
        identified_from_registers=kinds["registry"],
        identified_from_other_sources=kinds["website"] + kinds["other"],
        duplicates_removed=len(occurrences) - len(unique),
        records_outside_identity=len(outside),
        records_screened=len(screened),
        records_awaiting_screening=len(awaiting),
        records_excluded=len(excluded),
        records_unresolved=len(unresolved),
        ai_title_abstract=ai_ta,
        reports_sought=len(sought),
        reports_not_retrieved=len(not_retrieved),
        reports_assessed=len(assessed),
        reports_awaiting_assessment=len(awaiting_assessment),
        reports_excluded=len(ft_excluded),
        exclusion_reasons=tuple(
            ReasonCount(criterion_id=c, reports=n) for c, n in sorted(reasons.items())
        ),
        reports_awaiting_classification=len(awaiting_classification),
        reports_unresolved=len(ft_unresolved),
        ai_full_text=ai_ft,
        reports_included=len(included),
        studies_included=len(studies),
        members=members,
        checks=checks,
    )


def flows(review: Review, scope_id: str | None = None) -> list[PrismaFlow]:
    return [flow(review, t, s) for t, s in review.scopes() if scope_id in (None, s)]


# The diagram: PRISMA 2020's flow for new reviews of databases and registers.

_W, _GAP, _LINE, _PAD, _BAND = 330, 60, 17, 10, 34
_FAMILY = "font-family='IBM Plex Sans, Helvetica, Arial, sans-serif'"
_FONT = f"{_FAMILY} font-size='12'"


def _box(x: int, y: int, lines: list[str], *, bold_first: bool = True) -> tuple[str, int]:
    height = _PAD * 2 + _LINE * len(lines)
    parts = [
        f"<rect x='{x}' y='{y}' width='{_W}' height='{height}' rx='4' fill='#ffffff' "
        "stroke='#1f2933' stroke-width='1.2'/>"
    ]
    for n, line in enumerate(lines):
        weight = " font-weight='600'" if bold_first and n == 0 else ""
        parts.append(
            f"<text x='{x + _PAD}' y='{y + _PAD + _LINE * (n + 1) - 4}' {_FONT}{weight}>"
            f"{escape(line)}</text>"
        )
    return "".join(parts), height


def _arrow(x1: int, y1: int, x2: int, y2: int) -> str:
    return (
        f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='#1f2933' stroke-width='1.2' "
        "marker-end='url(#arrow)'/>"
    )


def svg(f: PrismaFlow, title: str) -> str:
    """The flow as a standalone SVG."""
    left, right = _BAND + 20, _BAND + 20 + _W + _GAP
    source_lines = [f"  {s.name}: n = {s.records}" for s in f.identified_by_source]
    reasons = [f"  {r.criterion_id}: n = {r.reports}" for r in f.exclusion_reasons]
    rows: list[tuple[str, list[str], list[str]]] = [
        (
            "Identification",
            [
                "Records identified from:",
                f"Databases: n = {f.identified_from_databases}",
                f"Registers: n = {f.identified_from_registers}",
                f"Other sources: n = {f.identified_from_other_sources}",
                *source_lines,
            ],
            [
                "Records removed before screening:",
                f"Duplicate records removed: n = {f.duplicates_removed}",
                "Records marked as ineligible by automation",
                f"  tools: n = {f.ai_title_abstract.marked_ineligible_by_automation}",
                *(
                    [f"Not yet in the identity revision: n = {f.records_outside_identity}"]
                    if f.records_outside_identity
                    else []
                ),
            ],
        ),
        (
            "Screening",
            [
                f"Records screened: n = {f.records_screened}",
                *(
                    [f"Awaiting screening: n = {f.records_awaiting_screening}"]
                    if f.records_awaiting_screening
                    else []
                ),
            ],
            [
                f"Records excluded: n = {f.records_excluded}",
                "  of which with AI involvement:",
                f"  n = {f.ai_title_abstract.excluded_with_ai_involvement}",
                *([f"Unresolved: n = {f.records_unresolved}"] if f.records_unresolved else []),
            ],
        ),
        (
            "",
            [f"Reports sought for retrieval: n = {f.reports_sought}"],
            [f"Reports not retrieved: n = {f.reports_not_retrieved}"],
        ),
        (
            "",
            [
                f"Reports assessed for eligibility: n = {f.reports_assessed}",
                *(
                    [f"Awaiting assessment: n = {f.reports_awaiting_assessment}"]
                    if f.reports_awaiting_assessment
                    else []
                ),
            ],
            [
                f"Reports excluded: n = {f.reports_excluded}",
                *reasons,
                f"  of which with AI involvement: n = {f.ai_full_text.excluded_with_ai_involvement}",
                *(
                    [f"Awaiting classification: n = {f.reports_awaiting_classification}"]
                    if f.reports_awaiting_classification
                    else []
                ),
                *([f"Unresolved: n = {f.reports_unresolved}"] if f.reports_unresolved else []),
            ],
        ),
        (
            "Included",
            [
                f"Studies included in review: n = {f.studies_included}",
                f"Reports of included studies: n = {f.reports_included}",
            ],
            [],
        ),
    ]
    body: list[str] = [
        f"<text x='{left}' y='24' {_FAMILY} font-size='15' font-weight='600'>{escape(title)}</text>"
    ]
    y = 40
    spans: list[tuple[str, int, int]] = []  # (band, top, bottom)
    previous_bottom: int | None = None
    for band, main, side in rows:
        main_svg, main_h = _box(left, y, main)
        side_svg, side_h = _box(right, y, side) if side else ("", 0)
        height = max(main_h, side_h)
        body += [main_svg, side_svg]
        if side:
            body.append(_arrow(left + _W, y + main_h // 2, right - 2, y + main_h // 2))
        if previous_bottom is not None:
            body.append(_arrow(left + _W // 2, previous_bottom, left + _W // 2, y - 2))
        if band or not spans:
            spans.append((band, y, y + height))
        else:
            spans[-1] = (spans[-1][0], spans[-1][1], y + height)
        previous_bottom = y + main_h
        y += height + 30
    for band, top, bottom in spans:
        body.append(
            f"<rect x='8' y='{top}' width='{_BAND}' height='{bottom - top}' rx='4' "
            "fill='#dbe7f3' stroke='#1f2933' stroke-width='1'/>"
        )
        cx, cy = 8 + _BAND // 2, (top + bottom) // 2
        body.append(
            f"<text x='{cx}' y='{cy}' {_FONT} font-weight='600' text-anchor='middle' "
            f"transform='rotate(-90 {cx} {cy})'>{band}</text>"
        )
    width = right + _W + 20
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{y}' "
        f"viewBox='0 0 {width} {y}' role='img' aria-label='{escape(title)}'>"
        "<defs><marker id='arrow' markerWidth='8' markerHeight='8' refX='7' refY='4' "
        "orient='auto'><path d='M0,0 L8,4 L0,8 z' fill='#1f2933'/></marker></defs>"
        f"<rect width='{width}' height='{y}' fill='#ffffff'/>{''.join(body)}</svg>\n"
    )
