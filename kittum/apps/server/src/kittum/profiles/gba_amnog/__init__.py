"""The G-BA benefit assessment profile (AMNOG, § 35a SGB V): Modul 4 of a dossier.

What it adds to the generic protocol:

- Protocol extensions: the dossier (active substance, trade name, procedure) and its
  Anwendungsgebiete, each grouping Teilpopulationen and the search topics that serve them.
- Question extensions: a question is a Teilpopulation with its zweckmäßige Vergleichstherapie
  (zVT), or a PICO of an EU joint clinical assessment (JCA) linked from a Teilpopulation. The zVT
  options are the question's comparators, as alternatives; "nach ärztlicher Maßgabe" is a flag.
- Comparator extensions: off-label use (§ 6 Abs. 2 Satz 3 Nr. 3 AM-NutzenV).
- Vocabularies: the four AMNOG endpoint categories and the Modul 4 search kinds.
- Rules: searches at most three months old at submission, the Modul 4 source minimum
  (Anlage II.6, 18.11.2025), German as the default language, and a consistent dossier
  structure.
- Labels: Teilpopulation, zVT, Endpunkt, Anwendungsgebiet.
- Default effect modifiers from the Verfahrensordnung: sex, age, severity, centre or country.
"""

from collections import Counter
from typing import Literal

from pydantic import Field

from kittum.core import Record
from kittum.protocol import (
    EffectModifier,
    Profile,
    ProtocolIssue,
    ReviewProtocol,
    path_of,
)

PROFILE_ID = "gba_amnog"
MAX_SEARCH_AGE_MONTHS = 3

MODUL4_MINIMUM_SOURCES: dict[str, str] = {
    "medline": "MEDLINE",
    "central": "Cochrane CENTRAL",
    "clinicaltrials_gov": "ClinicalTrials.gov",
    "eu_ctr": "EU Clinical Trials Register",
    "ctis": "CTIS",
    "ema_clinical_data": "EMA Clinical Data",
    "gba_website": "G-BA website",
}
"""Sources Anlage II.6 (Modul 4, 18.11.2025) requires. Leaving one out needs a deviation."""


class Dossier(Record):
    active_substance: str
    trade_name: str
    version: str
    gba_procedure: str | None = None  # e.g. "D-1110"


class Anwendungsgebiet(Record):
    code: str  # the Modul 4 letter, e.g. "A"
    text_de: str
    questions: tuple[str, ...] = Field(min_length=1)
    search_topics: tuple[str, ...] = Field(min_length=1)


class GbaProtocol(Record):
    dossier: Dossier
    anwendungsgebiete: tuple[Anwendungsgebiet, ...] = Field(min_length=1)


class Zvt(Record):
    """The zweckmäßige Vergleichstherapie. Its options are the question's comparators."""

    nach_aerztlicher_massgabe: bool = False  # "Therapie nach ärztlicher Maßgabe" or individualized
    source_document: str
    qualifying_comparator_arms: tuple[str, ...] = ()
    """Trial arms that count as the zVT, e.g. fulvestrant monotherapy in postmenopausal women."""


class GbaQuestion(Record):
    kind: Literal["teilpopulation", "jca_pico"] = "teilpopulation"
    consultation_ref: str | None = None  # Beratungsanforderung, e.g. "2024-B-043"
    zvt: Zvt | None = None
    jca_pico_links: tuple[str, ...] = ()
    source_document: str | None = None  # for a JCA PICO: the consolidated PICO document


class GbaComparator(Record):
    off_label: bool = False  # zulassungsüberschreitend


class GbaOutcome(Record):
    surrogate_validation_ref: str | None = None


def _questions(protocol: ReviewProtocol) -> dict[str, GbaQuestion]:
    return {q.id: GbaQuestion.model_validate(q.extensions) for q in protocol.questions}


def check_search_age(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    return [
        ProtocolIssue(
            path=path_of("search_topics", i, "freshness", "max_age_months"),
            message=f"Modul 4 allows searches at most {MAX_SEARCH_AGE_MONTHS} months old at "
            f"submission, got {t.freshness.max_age_months}",
        )
        for i, t in enumerate(protocol.search_topics)
        if t.freshness.max_age_months > MAX_SEARCH_AGE_MONTHS
    ]


def check_source_minimum(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    issues = []
    for i, topic in enumerate(protocol.search_topics):
        present = {s.id for s in topic.source_plan.sources}
        deviated = {d.source_id for d in topic.source_plan.deviations}
        for source_id, name in MODUL4_MINIMUM_SOURCES.items():
            if source_id not in present | deviated:
                issues.append(
                    ProtocolIssue(
                        path=path_of("search_topics", i, "source_plan", "sources"),
                        message=f"{name} ({source_id!r}) is part of the Modul 4 minimum; add it "
                        "or add a deviation with a justification",
                    )
                )
    return issues


def check_language(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    """Modul 4 is written in German, so German is the default language and every text has it."""
    if protocol.default_language == "de":
        return []
    return [
        ProtocolIssue(
            path="default_language",
            message=f"Modul 4 is written in German; set the default language to 'de', got "
            f"{protocol.default_language!r}",
        )
    ]


def check_questions(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    issues = []
    questions = _questions(protocol)
    for qi, question in enumerate(protocol.questions):
        ext = questions[question.id]
        at = ("questions", qi)
        if ext.kind == "teilpopulation":
            if ext.zvt is None:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "extensions", "zvt"),
                        message=f"Teilpopulation {question.id!r} needs the zVT set by the G-BA",
                    )
                )
            if not question.comparators:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "comparators"),
                        message="the zVT options are the comparators; list at least one",
                    )
                )
            elif len(question.comparators) > 1 and not question.comparators_are_alternatives:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "comparators_are_alternatives"),
                        message="zVT options are alternatives; set comparators_are_alternatives",
                    )
                )
        else:
            if ext.zvt is not None:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "extensions", "zvt"),
                        message="a JCA PICO has comparators, not a zVT",
                    )
                )
            if ext.jca_pico_links:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "extensions", "jca_pico_links"),
                        message="only a Teilpopulation links to JCA PICOs",
                    )
                )
        for li, link in enumerate(ext.jca_pico_links):
            target = questions.get(link)
            if target is None or target.kind != "jca_pico":
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "extensions", "jca_pico_links", li),
                        message=f"{link!r} is not a question of kind jca_pico",
                    )
                )
    return issues


def check_dossier(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    """Every question and topic belongs to one Anwendungsgebiet; topics stay inside theirs."""
    issues = []
    ext = GbaProtocol.model_validate(protocol.extensions)
    question_ids = {q.id for q in protocol.questions}
    topics = {t.id: t for t in protocol.search_topics}
    question_area: dict[str, str] = {}
    topic_area: dict[str, str] = {}
    codes = Counter(a.code for a in ext.anwendungsgebiete)
    for ai, area in enumerate(ext.anwendungsgebiete):
        at = ("extensions", "anwendungsgebiete", ai)
        if codes[area.code] > 1:
            issues.append(
                ProtocolIssue(
                    path=path_of(*at, "code"),
                    message=f"duplicate Anwendungsgebiet code {area.code!r}",
                )
            )
        for kind, ids, known, owner in (
            ("questions", area.questions, question_ids, question_area),
            ("search_topics", area.search_topics, topics.keys(), topic_area),
        ):
            for i, item in enumerate(ids):
                path = path_of(*at, kind, i)
                if item not in known:
                    issues.append(ProtocolIssue(path=path, message=f"unknown {kind[:-1]} {item!r}"))
                elif item in owner:
                    issues.append(
                        ProtocolIssue(
                            path=path,
                            message=f"{item!r} is already in Anwendungsgebiet {owner[item]!r}",
                        )
                    )
                else:
                    owner[item] = area.code
    for qi, q in enumerate(protocol.questions):
        if q.id not in question_area:
            issues.append(
                ProtocolIssue(
                    path=path_of("questions", qi),
                    message=f"question {q.id!r} is in no Anwendungsgebiet",
                )
            )
    for ti, topic in enumerate(protocol.search_topics):
        area = topic_area.get(topic.id)
        if area is None:
            issues.append(
                ProtocolIssue(
                    path=path_of("search_topics", ti),
                    message=f"search topic {topic.id!r} is in no Anwendungsgebiet",
                )
            )
            continue
        for si, scope in enumerate(topic.scopes):
            if question_area.get(scope.id, area) != area:
                issues.append(
                    ProtocolIssue(
                        path=path_of("search_topics", ti, "scopes", si),
                        message=f"{scope.id!r} belongs to Anwendungsgebiet "
                        f"{question_area[scope.id]!r}, the topic to {area!r}",
                    )
                )
    return issues


def _labels(de: str, en: str) -> dict[str, str]:
    return {"de": de, "en": en}


GBA_AMNOG = Profile(
    id=PROFILE_ID,
    label=_labels("G-BA-Nutzenbewertung (AMNOG)", "G-BA benefit assessment (AMNOG)"),
    extensions={
        "protocol": GbaProtocol,
        "question": GbaQuestion,
        "comparator": GbaComparator,
        "outcome": GbaOutcome,
    },
    vocabularies={
        "outcome_category": {
            "mortality": _labels("Mortalität", "Mortality"),
            "morbidity": _labels("Morbidität", "Morbidity"),
            "quality_of_life": _labels(
                "Gesundheitsbezogene Lebensqualität", "Health-related quality of life"
            ),
            "side_effects": _labels("Nebenwirkungen", "Side effects"),
        },
        "topic_kind": {
            "rct_drug": _labels(
                "RCT mit dem zu bewertenden Arzneimittel", "RCTs with the drug under assessment"
            ),
            "rct_indirect": _labels(
                "RCT für indirekte Vergleiche", "RCTs for indirect comparisons"
            ),
            "non_randomized": _labels(
                "Nicht randomisierte vergleichende Studien", "Non-randomized comparative studies"
            ),
            "other": _labels("Weitere Untersuchungen", "Other studies"),
        },
    },
    rules=(
        check_dossier,
        check_questions,
        check_search_age,
        check_source_minimum,
        check_language,
    ),
    labels={
        "question": _labels("Teilpopulation", "Subpopulation"),
        "comparator": _labels(
            "Zweckmäßige Vergleichstherapie (zVT)", "Appropriate comparator therapy"
        ),
        "outcome": _labels("Endpunkt", "Endpoint"),
        "search_topic": _labels("Recherche", "Search"),
        "anwendungsgebiet": _labels("Anwendungsgebiet", "Therapeutic indication"),
        "slot.comparator": _labels("zVT", "Comparator (zVT)"),
        "slot.outcome": _labels("Endpunkte", "Endpoints"),
    },
    default_effect_modifiers=(
        EffectModifier(id="sex", label=_labels("Geschlecht", "Sex")),
        EffectModifier(id="age", label=_labels("Alter", "Age")),
        EffectModifier(id="severity", label=_labels("Krankheitsschwere", "Disease severity")),
        EffectModifier(id="centre", label=_labels("Zentrum oder Land", "Centre or country")),
    ),
)

__all__ = [
    "GBA_AMNOG",
    "MAX_SEARCH_AGE_MONTHS",
    "MODUL4_MINIMUM_SOURCES",
    "PROFILE_ID",
    "Anwendungsgebiet",
    "Dossier",
    "GbaComparator",
    "GbaOutcome",
    "GbaProtocol",
    "GbaQuestion",
    "Zvt",
]
