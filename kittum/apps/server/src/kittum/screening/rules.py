"""Pure screening rules: which criteria apply, what a valid decision is, when the AI is revealed,
which records conflict, and the seeded orders and samples. No I/O."""

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from kittum.core import find_quote, sha256_hex
from kittum.protocol import ScreeningMode
from kittum.screening.types import InvalidDecision, QuoteSpan, Reveal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kittum.judge import CriterionVerdict, Verdict
    from kittum.protocol import Criterion, ReviewProtocol, SearchTopic, Stage
    from kittum.screening.types import (
        ConflictKind,
        Decision,
        Highlight,
        RescreenReason,
        RevealSetting,
    )

SCREENERS_PER_MODE = {ScreeningMode.M1: 2, ScreeningMode.M2: 1, ScreeningMode.M3: 2}
OUTCOMES_AT: dict[str, frozenset[str]] = {
    "title_abstract": frozenset({"include", "exclude", "unclear"}),
    "full_text": frozenset({"include", "exclude", "insufficient_information"}),
}


@dataclass(frozen=True)
class Reading:
    """The AI's verdict on one item, read two ways.

    `excludes` is the verified reading, `Verdict.excludes`: a `not_met` whose quote the judge's
    filter confirmed. Screening uses it wherever an AI exclusion would remove or deprioritize a
    record: the M2 disagreement when the human includes, the M3 order, and the audit strata.

    `includes_by_own_reading` is the model's own reading: no criterion the model itself answered
    `not_met` (`downgraded_from` if the filter changed it, else `status`). The filter exists to
    stop wrongful exclusions, not to manufacture disagreement, so this reading decides when the
    AI argues for keeping a record that humans excluded. A record whose content tried to instruct
    the model has no trustworthy own reading, and counts as included.
    """

    excludes: bool
    criteria: tuple[CriterionVerdict, ...]
    flags: tuple[str, ...] = ()

    @property
    def includes_by_own_reading(self) -> bool:
        if "injection_suspected" in self.flags:
            return True
        return not any(_model_status(c) == "not_met" for c in self.criteria)


def _model_status(verdict: CriterionVerdict) -> str:
    """What the model answered, before the filter downgraded it."""
    return verdict.downgraded_from or verdict.status


def disagrees(decision: Decision, reading: Reading) -> bool:
    """Whether the AI disagrees with a human decision.

    A human exclusion meets disagreement only if the model itself saw no failing criterion; a
    human inclusion only if the AI's exclusion is verified. An exclusion the filter could not
    verify is agreement with an inclusion, and the model's unverified `not_met` is agreement with
    an exclusion.
    """
    return reading.includes_by_own_reading if excludes(decision) else reading.excludes


def topic_for(protocol: ReviewProtocol, scope_id: str, topic_id: str | None) -> SearchTopic:
    """The search topic that screens `scope_id`. A scope in several topics needs `topic_id`."""
    topics = [
        t
        for t in protocol.search_topics
        if (topic_id is None or t.id == topic_id) and scope_id in {s.id for s in t.scopes}
    ]
    if not topics:
        raise InvalidDecision(f"no search topic screens scope {scope_id!r}")
    if len(topics) > 1:
        names = ", ".join(t.id for t in topics)
        raise InvalidDecision(f"scope {scope_id!r} is in several topics ({names}); name one")
    return topics[0]


def criteria_for(topic: SearchTopic, scope_id: str, stage: Stage) -> tuple[Criterion, ...]:
    """The topic's criteria for the scope at the stage, in protocol order.

    A criterion may apply to the whole topic; the copy returned applies to the scope itself, so
    `kittum.judge` (which matches `applies_to` against the scope) asks it.
    """
    return tuple(
        c.model_copy(update={"applies_to": (scope_id,)})
        for c in topic.criteria
        if stage in c.decidable_at and (scope_id in c.applies_to or topic.id in c.applies_to)
    )


def percent_of(percent: str, n: int) -> int:
    """How many of `n` items a protocol percentage asks for, rounded up: any share above zero
    gets at least one."""
    return math.ceil(Decimal(percent) * n / 100)


def default_seed(protocol_hash: str, topic_id: str, scope_id: str, stage: Stage) -> str:
    return sha256_hex(
        f"kittum.screening.seed\x1f{protocol_hash}\x1f{topic_id}\x1f{scope_id}\x1f{stage}".encode()
    )


def sort_key(seed: str, item_ref: str, reviewer: str = "") -> str:
    """A seeded, reproducible queue key. M3 passes no reviewer, so both humans share the order."""
    return sha256_hex(f"{seed}\x1f{reviewer}\x1f{item_ref}".encode())


def seeded_sample(seed: str, salt: str, refs: Iterable[str], k: int) -> list[str]:
    """`k` of `refs`, chosen by a seeded hash: a simple random sample that anyone can redraw."""
    return sorted(refs, key=lambda r: sort_key(seed, r, salt))[:k]


def ai_rank(verdict: Verdict) -> int:
    """The queue rank of an item in M3: proposed exclusions last, then by criteria not met.
    An order, never shown as a score."""
    met = sum(c.status == "met" for c in verdict.criteria)
    return (1000 if verdict.excludes else 0) + len(verdict.criteria) - met


def excludes(decision: Decision) -> bool:
    """`unclear` at title and abstract means include, and so does insufficient information."""
    return decision.outcome == "exclude"


def validate(decision: Decision, stage: Stage, criteria: Sequence[Criterion]) -> None:
    """Raise `InvalidDecision` if the decision breaks a rule of its stage."""
    known = {c.id for c in criteria}
    if decision.outcome not in OUTCOMES_AT[stage]:
        raise InvalidDecision(f"{decision.outcome!r} is not an outcome at {stage}")
    if decision.outcome == "exclude" and decision.reason_criterion_id is None:
        raise InvalidDecision("An exclusion needs the criterion it excludes on.")
    if decision.reason_criterion_id is not None and decision.reason_criterion_id not in known:
        raise InvalidDecision(
            f"criterion {decision.reason_criterion_id!r} does not apply to this scope at {stage}"
        )
    readings = {c.criterion_id: c for c in decision.criteria}
    if len(readings) != len(decision.criteria) or not set(readings) <= known:
        raise InvalidDecision("Give each criterion of this scope one reading at most.")
    if stage == "title_abstract":
        return
    # One reason per report is what PRISMA asks for: an exclusion cites the criterion it fails, an
    # inclusion says every criterion is met. Readings of the other criteria are optional.
    if decision.outcome == "exclude":
        reason = readings.get(decision.reason_criterion_id or "")
        if reason is None or reason.status != "not_met" or not reason.evidence_anchor_ids:
            raise InvalidDecision(
                "To exclude, mark the failing criterion not met and adopt evidence for it."
            )
    elif decision.outcome == "include":
        if any(c.status != "met" for c in decision.criteria):
            raise InvalidDecision(
                "Include means every criterion is met. Change the verdicts that say otherwise."
            )
    else:
        if not any(c.status == "unclear" for c in decision.criteria):
            raise InvalidDecision(
                "Insufficient information needs at least one criterion marked unclear."
            )
        if not decision.evidence_anchor_ids and not any(
            c.evidence_anchor_ids for c in decision.criteria
        ):
            raise InvalidDecision("Insufficient information needs at least one evidence anchor.")


def excluding_criterion(reading: Reading) -> CriterionVerdict | None:
    return next(
        (
            c
            for c in reading.criteria
            if c.status == "not_met" and c.quote_verified and c.failed_check is None
        ),
        None,
    )


def _reveal(verdict: CriterionVerdict) -> Reveal:
    return Reveal(
        criterion_id=verdict.criterion_id,
        status=verdict.status,
        quote=verdict.quote or "",
        rationale=verdict.rationale,
        quote_verified=verdict.quote_verified,
        downgraded_from=verdict.downgraded_from,
    )


def locate_quote(quote: str | None, title: str, text: str) -> QuoteSpan | None:
    """Where a quote the judge verified is in the item, by `find_quote`: its first occurrence in
    the title, else in the text. The judge checks quotes against the title and the text joined,
    so a quote across both is found in neither and stays unplaced."""
    if not quote:
        return None
    for part, source in (("title", title), ("text", text)):
        spans = find_quote(quote, source)
        if spans:
            return QuoteSpan(part=part, start=spans[0].start, end=spans[0].end)
    return None


def locate(reveal: Reveal, title: str, text: str) -> Reveal:
    """The reveal with its verified quote placed in the item."""
    if not reveal.quote_verified:
        return reveal
    return reveal.model_copy(update={"quote_span": locate_quote(reveal.quote, title, text)})


def reveal_for(setting: RevealSetting, decision: Decision, reading: Reading) -> Reveal | None:
    """What the reviewer sees after a provisional decision, or None to move on.

    On disagreement (see `disagrees`) the banner shows the AI's verified reason to exclude, or,
    when the human excludes and the model saw no failing criterion, the AI's reading of the
    human's criterion.
    """
    if setting == "never":
        return None
    by_id = {c.criterion_id: c for c in reading.criteria}
    own = by_id.get(decision.reason_criterion_id or "")
    ai_reason = excluding_criterion(reading)
    if disagrees(decision, reading):
        basis = own if excludes(decision) else ai_reason
    elif setting == "always":
        basis = (
            (
                own
                if own and _model_status(own) == "not_met"
                else next((c for c in reading.criteria if _model_status(c) == "not_met"), None)
            )
            if excludes(decision)
            else next((c for c in reading.criteria if c.status == "met"), None)
        )
    else:
        return None
    basis = basis or (reading.criteria[0] if reading.criteria else None)
    return _reveal(basis) if basis else None


def conflict_kinds(
    stage: Stage,
    screener_finals: Sequence[Decision],
    auditor_final: Decision | None,
) -> tuple[ConflictKind, ...]:
    """Why a record goes to adjudication once every screener has decided: human decisions that
    disagree, and nothing else.

    The screeners disagree on inclusion, or at full text exclude on different criteria; or the
    auditor's screen (a random audit sample, or the extra screen of an AI-only disagreement)
    disagrees with the screeners.
    """
    kinds: list[ConflictKind] = []
    excluded = [excludes(d) for d in screener_finals]
    if len(set(excluded)) > 1:
        kinds.append("human_disagreement")
    elif (
        stage == "full_text"
        and all(excluded)
        and len({d.reason_criterion_id for d in screener_finals}) > 1
    ):
        kinds.append("exclusion_reason")
    if auditor_final is not None and any(excludes(auditor_final) != e for e in excluded):
        kinds.append("audit_disagreement")
    return tuple(kinds)


def rescreen_reason(
    mode: ScreeningMode,
    screener_finals: Sequence[Decision],
    screener_provisionals: Sequence[Decision],
    reading: Reading | None,
) -> RescreenReason | None:
    """Whether an AI-only disagreement sends the record to a blind extra screen.

    M1: the screeners agree on an exclusion while the model, by its own reading, saw no failing
    criterion. M2: the AI disagrees with the lone human (`disagrees`), provisionally or finally
    (the protocol's census). M3 uses the AI for ranking only. The extra screen is the auditor's;
    if it disagrees with the screeners, that human disagreement goes to adjudication.
    """
    if reading is None:
        return None
    excluded = [excludes(d) for d in screener_finals]
    if len(set(excluded)) > 1:
        return None  # the humans disagree already
    if mode == ScreeningMode.M1 and all(excluded) and reading.includes_by_own_reading:
        return "ai_include"
    if mode == ScreeningMode.M2 and any(
        disagrees(d, reading) for d in (*screener_finals, *screener_provisionals)
    ):
        return "ai_disagreement"
    return None


def highlights(text: str, criteria: Sequence[Criterion]) -> tuple[Highlight, ...]:
    """Protocol terms in the text, from each criterion's keywords. These come from the protocol,
    not the AI, so reviewers may see them before deciding."""
    found: list[Highlight] = []
    for criterion in criteria:
        for keyword in criterion.keywords:
            pattern = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)
            found += [
                (m.start(), m.end(), f"protocol:{criterion.id}") for m in pattern.finditer(text)
            ]
    return tuple(sorted(found))


_LABEL = re.compile(r"(?:^|(?<=\s))([A-Z][A-Z &/-]{2,}[A-Z]):\s+")


def sections(text: str) -> tuple[tuple[str, int, int], ...]:
    """Abstract sections as (label, start, end), from inline labels ("METHODS: ").

    Text without labels (a registry summary, an unstructured abstract), or before the first
    label, is one section with the empty label. Only an empty text has no sections.
    """
    matches = list(_LABEL.finditer(text))
    out: list[tuple[str, int, int]] = []
    head = text[: matches[0].start() if matches else len(text)]
    if head.strip():
        start = len(head) - len(head.lstrip())
        out.append(("", start, len(head.rstrip())))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].rstrip()
        out.append((m.group(1), m.end(), m.end() + len(body)))
    return tuple(out)
