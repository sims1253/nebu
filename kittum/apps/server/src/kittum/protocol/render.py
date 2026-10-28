"""Criteria as a reviewer and as the AI judge see them.

Both renderings ask the same thing: does the record pass this criterion? For an `include`
criterion that means the record meets the condition; for an `exclude` criterion it means the
record does not show it. So `not_met` always means "exclude on this criterion", for humans and
for the judge, and the exclusion reason is the criterion ID.

Interface strings exist in English and German; other languages use the English ones around the
criterion's own text. A profile may rename the PICO slots in headings ("slot.<slot>" labels).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from kittum.core import Record

if TYPE_CHECKING:
    from kittum.protocol.model import Criterion, Stage
    from kittum.protocol.profile import Profile

type Answer = Literal["met", "not_met", "unclear"]


@dataclass(frozen=True)
class _Ui:
    slot: dict[str, str]
    stage: dict[str, str]
    kind: dict[str, str]
    question: dict[str, str]
    keys: dict[str, tuple[str, str, str]]  # key for "met", label for "met", label for "not_met"
    unclear: str
    hint: dict[str, str]


_UI = {
    "en": _Ui(
        slot={
            "population": "Population",
            "intervention": "Intervention",
            "comparator": "Comparator",
            "outcome": "Outcomes",
            "study_type": "Study type",
            "other": "Other",
        },
        stage={"title_abstract": "title and abstract", "full_text": "full text"},
        kind={"include": "inclusion", "exclude": "exclusion"},
        question={
            "include": "Does the record meet this inclusion criterion?",
            "exclude": "Is the record free of this exclusion reason?",
        },
        keys={
            "include": ("y", "yes, met", "no, not met: exclude"),
            "exclude": ("y", "yes, does not apply", "no, applies: exclude"),
        },
        unclear="unclear",
        hint={
            "title_abstract": "When in doubt choose unclear: at title and abstract only records "
            "that are certainly irrelevant are excluded.",
            "full_text": "To exclude, mark the passage that shows it.",
        },
    ),
    "de": _Ui(
        slot={
            "population": "Population",
            "intervention": "Intervention",
            "comparator": "Vergleich",
            "outcome": "Endpunkte",
            "study_type": "Studientyp",
            "other": "Sonstiges",
        },
        stage={"title_abstract": "Titel und Abstract", "full_text": "Volltext"},
        kind={"include": "Einschluss", "exclude": "Ausschluss"},
        question={
            "include": "Erfüllt die Publikation dieses Einschlusskriterium?",
            "exclude": "Ist die Publikation frei von diesem Ausschlussgrund?",
        },
        keys={
            "include": ("j", "ja, erfüllt", "nein, nicht erfüllt: ausschließen"),
            "exclude": ("j", "ja, trifft nicht zu", "nein, trifft zu: ausschließen"),
        },
        unclear="unklar",
        hint={
            "title_abstract": "Im Zweifel unklar wählen: bei Titel und Abstract wird nur "
            "ausgeschlossen, was sicher nicht relevant ist.",
            "full_text": "Für einen Ausschluss die Textstelle markieren, die ihn belegt.",
        },
    ),
}


class AnswerKey(Record):
    key: str
    answer: Answer
    label: str


class CriterionQuestion(Record):
    criterion_id: str
    language: str
    heading: str
    question: str
    hint: str
    keys: tuple[AnswerKey, ...]


def render_question(
    criterion: Criterion, language: str, stage: Stage, *, profile: Profile | None = None
) -> CriterionQuestion:
    """The question a reviewer answers with one key, in one of the criterion's languages."""
    _check_stage(criterion, stage)
    if language not in criterion.text:
        raise ValueError(f"criterion {criterion.id} has no text in {language!r}")
    ui = _UI.get(language, _UI["en"])
    slot = ui.slot[criterion.slot]
    if profile is not None:
        slot = profile.label_for(f"slot.{criterion.slot}", language) or slot
    yes_key, yes, no = ui.keys[criterion.polarity]
    return CriterionQuestion(
        criterion_id=criterion.id,
        language=language,
        heading=" · ".join((criterion.id, slot, ui.kind[criterion.polarity], ui.stage[stage])),
        question=f"{ui.question[criterion.polarity]} {criterion.text[language]}",
        hint=ui.hint[stage],
        keys=(
            AnswerKey(key=yes_key, answer="met", label=yes),
            AnswerKey(key="n", answer="not_met", label=no),
            AnswerKey(key="u", answer="unclear", label=ui.unclear),
        ),
    )


def render_prompt(criterion: Criterion, stage: Stage) -> str:
    """The prompt fragment that asks the AI judge about one criterion.

    The judge answers `met`, `not_met`, or `unclear`. `not_met` must come with a verbatim quote
    from the record; Kittum checks the quote and treats an unverifiable `not_met` as `unclear`.
    The text appears in every language the criterion has, sorted, so the prompt (and its cache
    key) does not depend on how the source ordered them.
    """
    _check_stage(criterion, stage)
    where = _UI["en"].stage[stage]
    lines = [f"### Criterion {criterion.id} ({_UI['en'].slot[criterion.slot].lower()})"]
    texts = [f"  {lang.upper()}: {criterion.text[lang]}" for lang in sorted(criterion.text)]
    if criterion.polarity == "include":
        lines += [
            "Inclusion condition. The record passes if it meets this condition:",
            *texts,
            "Answer `met` if the record meets it, `not_met` if the record shows that it does "
            "not, and `unclear` if the record does not say.",
        ]
    else:
        lines += [
            "Exclusion condition. The record passes unless it shows this:",
            *texts,
            "Answer `met` if the record does not show it, `not_met` if the record shows it, and "
            "`unclear` if the record does not say.",
        ]
    lines.append(
        f"For `not_met`, copy a verbatim quote from the {where}: the exact, contiguous words that "
        "show it, without paraphrase, ellipsis, or translation. If you cannot quote such words, "
        "answer `unclear`."
    )
    if criterion.keywords:
        terms = ", ".join(f'"{k}"' for k in criterion.keywords)
        lines.append(f"A `not_met` quote must contain one of: {terms}.")
    if stage == "title_abstract":
        lines.append(
            "At title and abstract, absence of information is never `not_met`: answer `unclear`."
        )
    if criterion.positive_examples:
        lines.append("Records that pass:")
        lines += [f"- {e}" for e in criterion.positive_examples]
    if criterion.negative_examples:
        lines.append("Records that do not pass:")
        lines += [f"- {e}" for e in criterion.negative_examples]
    return "\n".join(lines)


def _check_stage(criterion: Criterion, stage: Stage) -> None:
    if stage not in criterion.decidable_at:
        raise ValueError(f"criterion {criterion.id} is not decidable at {stage}")
