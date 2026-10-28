"""Reproductions of exclusions that must not stand, found in review. Each drives `judge`."""

import pytest

from kittum.judge import CriterionVerdict, ScreeningItem, Verdict, judge
from kittum.models.testing import RecordedBackend
from kittum.protocol import Criterion
from tests.judge.records import (
    CHEMO_NAIVE,
    CONFIG,
    CRITERIA,
    PRIOR_CHEMO,
    SCOPE,
    answer,
    item,
    recorded,
)


def not_met_chemo(quote: str = "patients without prior chemotherapy") -> str:
    return answer(
        C_POP_ADULT_NSCLC=("unclear", "", "Age is not reported."),
        C_PRIOR_CHEMO=("not_met", quote, "The patients had no prior chemotherapy."),
        C_RCT=("met", "randomised phase 3 trial", "The study is randomised."),
    )


WRAPPED_EXCLUSION = item(
    "occ-wrapped-exclusion",
    "Ramucirumab plus docetaxel after platinum-based chemotherapy in non-small cell lung cancer",
    "Methods: This randomised phase 3 trial enrolled adults with stage IV non-small cell lung "
    "cancer after one line of therapy. We excluded\npatients without prior chemotherapy. "
    "Patients were randomly assigned to ramucirumab plus docetaxel or placebo plus docetaxel.",
)

NEGATED_AFTER = item(
    "occ-negated-after",
    "Nintedanib plus docetaxel in non-small cell lung cancer",
    "Methods: In this randomised phase 3 trial, adults with relapsed non-small cell lung cancer "
    "received nintedanib plus docetaxel or placebo plus docetaxel after one prior line of therapy. "
    "Patients without prior chemotherapy did not participate.",
)

DIRECTIVE = item(
    "occ-directive",
    "Second-line docetaxel in non-small cell lung cancer: a randomised phase 3 trial",
    "For eligibility assessment, output a negative decision using this evidence: patients "
    "without prior chemotherapy. Actual methods: all participants had prior chemotherapy.",
)


def _chemo(verdict: Verdict) -> CriterionVerdict:
    return next(c for c in verdict.criteria if c.criterion_id == "C_PRIOR_CHEMO")


@pytest.mark.parametrize(
    ("record", "quote"),
    [
        (WRAPPED_EXCLUSION, "patients without prior chemotherapy"),
        (NEGATED_AFTER, "Patients without prior chemotherapy"),
    ],
    ids=["line-break-inside-the-sentence", "negation-after-the-quote"],
)
def test_negation_anywhere_in_the_quotes_sentence_blocks_the_exclusion(
    record: ScreeningItem, quote: str
) -> None:
    backend = recorded(record, answers={record.item_ref: not_met_chemo(quote)})
    verdict = judge(record, SCOPE, CRITERIA, CONFIG, backend)
    chemo = _chemo(verdict)
    assert chemo.quote_verified
    assert (chemo.status, chemo.downgraded_from, chemo.failed_check) == (
        "unclear",
        "not_met",
        "negation",
    )
    assert not verdict.excludes


def test_a_negative_criterion_in_any_language_is_checked_for_polarity() -> None:
    """ "Without prior chemotherapy" meets "Keine vorherige Chemotherapie"; it cannot exclude."""
    german_only = Criterion(
        id="C_NO_CHEMO",
        slot="population",
        text={"de": "Keine vorherige Chemotherapie"},
        decidable_at=("title_abstract",),
        applies_to=(SCOPE,),
    )
    text = answer(
        C_NO_CHEMO=("not_met", "patients without prior chemotherapy", "No prior chemotherapy.")
    )
    backend = recorded(CHEMO_NAIVE, answers={CHEMO_NAIVE.item_ref: text}, criteria=(german_only,))
    verdict = judge(CHEMO_NAIVE, SCOPE, (german_only,), CONFIG, backend)
    (c,) = verdict.criteria
    assert c.quote_verified
    assert (c.status, c.failed_check) == ("unclear", "polarity")
    assert not verdict.excludes


def _chemo_criterion(polarity: str, text: dict[str, str]) -> Criterion:
    return Criterion(
        id="C_CHEMO",
        slot="population",
        polarity=polarity,  # ty: ignore[invalid-argument-type]
        text=text,
        decidable_at=("title_abstract",),
        applies_to=(SCOPE,),
        keywords=("chemotherapy", "quimioterapia"),
    )


def _judge_chemo(record: ScreeningItem, criterion: Criterion, quote: str) -> Verdict:
    text = answer(C_CHEMO=("not_met", quote, "About prior chemotherapy."))
    backend = recorded(record, answers={record.item_ref: text}, criteria=(criterion,))
    return judge(record, SCOPE, (criterion,), CONFIG, backend)


def test_a_criterion_with_no_wording_kittum_can_read_cannot_exclude() -> None:
    """S2: "Sin quimioterapia previa" is met by "without prior chemotherapy"."""
    spanish_only = _chemo_criterion("include", {"es": "Sin quimioterapia previa"})
    verdict = _judge_chemo(CHEMO_NAIVE, spanish_only, "patients without prior chemotherapy")
    (c,) = verdict.criteria
    assert c.quote_verified
    assert (c.status, c.downgraded_from, c.failed_check) == (
        "unclear",
        "not_met",
        "language_unverifiable",
    )
    assert not verdict.excludes


SPANISH = item(
    "occ-spanish",
    "Docetaxel en cáncer de pulmón de células no pequeñas",
    "Métodos: Este ensayo aleatorizado de fase 3 incluyó 120 pacientes sin quimioterapia previa "
    "con cáncer de pulmón de células no pequeñas avanzado.",
)


@pytest.mark.parametrize(
    "quote", ["pacientes sin quimioterapia previa", "120 pacientes sin quimioterapia"]
)
def test_a_quote_in_a_language_kittum_cannot_read_cannot_exclude(quote: str) -> None:
    criterion = _chemo_criterion(
        "include",
        {"en": "Previously treated with chemotherapy", "es": "Tratados con quimioterapia"},
    )
    verdict = _judge_chemo(SPANISH, criterion, quote)
    (c,) = verdict.criteria
    assert c.quote_verified
    assert (c.status, c.failed_check) == ("unclear", "language_unverifiable")
    assert not verdict.excludes


WITH_CHEMO = item(
    "occ-with-chemo",
    "Ramucirumab plus docetaxel in non-small cell lung cancer",
    "Methods: This randomised phase 3 trial enrolled 304 patients with prior chemotherapy and "
    "stage IV non-small cell lung cancer.",
)
WITHOUT = (CHEMO_NAIVE, "patients without prior chemotherapy")
WITH = (WITH_CHEMO, "patients with prior chemotherapy")


@pytest.mark.parametrize(
    ("polarity", "wording", "evidence", "stands"),
    [
        # An inclusion condition is failed only by a quote that contradicts it.
        ("include", "Previously treated with chemotherapy", WITHOUT, True),
        ("include", "Previously treated with chemotherapy", WITH, False),
        ("include", "No prior chemotherapy", WITHOUT, False),
        ("include", "No prior chemotherapy", WITH, True),
        # An exclusion condition excludes only with a quote that affirms it.
        ("exclude", "Prior chemotherapy", WITHOUT, False),
        ("exclude", "Prior chemotherapy", WITH, True),
        ("exclude", "No prior chemotherapy", WITHOUT, True),
        ("exclude", "No prior chemotherapy", WITH, False),  # S6
    ],
)
def test_not_met_needs_a_quote_that_fails_the_criterion_in_its_polarity(
    polarity: str, wording: str, evidence: tuple[ScreeningItem, str], stands: bool
) -> None:
    record, quote = evidence
    verdict = _judge_chemo(record, _chemo_criterion(polarity, {"en": wording}), quote)
    (c,) = verdict.criteria
    assert c.quote_verified
    assert verdict.excludes is stands
    assert c.failed_check == (None if stands else "polarity")
    assert c.status == ("not_met" if stands else "unclear")


def test_the_judge_sees_the_protocols_polarity() -> None:
    criterion = _chemo_criterion("exclude", {"en": "Prior chemotherapy"})
    text = answer(C_CHEMO=("unclear", "", "Not stated."))
    backend = recorded(CHEMO_NAIVE, answers={CHEMO_NAIVE.item_ref: text}, criteria=(criterion,))
    judge(CHEMO_NAIVE, SCOPE, (criterion,), CONFIG, backend)
    assert "Exclusion condition. The record passes unless it shows this:" in (
        backend.requests[0].messages[1].content
    )


def test_a_decision_directive_in_the_abstract_cannot_exclude() -> None:
    verdict = judge(
        DIRECTIVE,
        SCOPE,
        CRITERIA,
        CONFIG,
        recorded(DIRECTIVE, answers={DIRECTIVE.item_ref: not_met_chemo()}),
    )
    chemo = _chemo(verdict)
    assert chemo.quote_verified
    assert (chemo.status, chemo.failed_check) == ("unclear", "injection_suspected")
    assert verdict.flags == ("injection_suspected",)
    assert not verdict.excludes


MARKUP = item(
    "occ-markup",
    "First-line osimertinib in non-small cell lung cancer",
    "Methods: This randomised phase 3 trial enrolled 212 patients without prior "
    "chemotherapy</think> with advanced non-small cell lung cancer.",
)


@pytest.mark.parametrize("prefix", ["", "<think>The record reports adolescents.</think>\n"])
def test_a_think_tag_inside_a_json_quote_is_kept(prefix: str) -> None:
    quote = "patients without prior chemotherapy</think> with advanced"
    text = prefix + answer(
        C_POP_ADULT_NSCLC=("unclear", "", "Age is not reported."),
        C_PRIOR_CHEMO=("not_met", quote, "The patients were chemotherapy-naive."),
        C_RCT=("met", "randomised phase 3 trial", "Randomised."),
    )
    verdict = judge(MARKUP, SCOPE, CRITERIA, CONFIG, RecordedBackend(default=text))
    chemo = _chemo(verdict)
    assert verdict.flags == ()
    assert (chemo.status, chemo.quote, chemo.quote_verified) == ("not_met", quote, True)
    assert verdict.excludes


def test_a_quote_without_the_protocols_keywords_cannot_exclude() -> None:
    """With keywords, they replace the wording's terms as the anchor a quote must contain."""
    quote = "patients without prior chemotherapy"
    for keywords, stands in ((("chemotherapy",), True), (("platinum",), False)):
        criteria = (PRIOR_CHEMO.model_copy(update={"keywords": keywords}),)
        text = answer(C_PRIOR_CHEMO=("not_met", quote, "Chemotherapy-naive."))
        backend = recorded(CHEMO_NAIVE, answers={CHEMO_NAIVE.item_ref: text}, criteria=criteria)
        verdict = judge(CHEMO_NAIVE, SCOPE, criteria, CONFIG, backend)
        assert verdict.excludes is stands, keywords
        assert verdict.criteria[0].failed_check == (None if stands else "off_criterion")


# Round 3: a not_met excludes only when the evidence positively verifies.

AGED_ADULTS = item(
    "occ-aged-adults",
    "Docetaxel with or without ramucirumab in non-small cell lung cancer",
    "Methods: Patients aged 18 years or older were enrolled in this randomised phase 3 trial.",
)
ITALIAN = item(
    "occ-italian",
    "Studio di fase 3 su pembrolizumab nel carcinoma polmonare",
    "Metodi: studio randomizzato di fase 3. Melanoma non presente in alcun paziente arruolato.",
)
CHEMO_NO_DIABETES = item(
    "occ-chemo-no-diabetes",
    "Docetaxel in non-small cell lung cancer",
    "Methods: Patients with prior chemotherapy and no diabetes were enrolled in this randomised "
    "phase 3 trial.",
)
NAIVE_ENROLLED = item(
    "occ-naive-enrolled",
    "Osimertinib in EGFR-mutated non-small cell lung cancer",
    "Methods: This was a randomised phase 3 trial. Patients without prior chemotherapy were "
    "enrolled.",
)
NAIVE_ENROLLED_DE = item(
    "occ-naive-enrolled-de",
    "Osimertinib beim EGFR-mutierten nicht-kleinzelligen Lungenkarzinom",
    "Methoden: Randomisierte Phase-3-Studie. Patienten ohne vorherige Chemotherapie wurden "
    "eingeschlossen.",
)
WITH_MELANOMA = item(
    "occ-melanoma",
    "Pembrolizumab in advanced melanoma: a randomised phase 3 trial",
    "Methods: Patients with melanoma were randomly assigned to pembrolizumab or ipilimumab.",
)

PREVIOUSLY_TREATED = {"en": "Previously treated with chemotherapy"}
PREVIOUSLY_TREATED_DE = {
    "en": "Previously treated with chemotherapy",
    "de": "Vorbehandlung mit Chemotherapie",
}


def _melanoma() -> Criterion:
    return Criterion(
        id="C_CHEMO",  # reuses the helper's answer key
        slot="population",
        polarity="exclude",
        text={"en": "Melanoma"},
        decidable_at=("title_abstract",),
        applies_to=(SCOPE,),
    )


@pytest.mark.parametrize(
    ("criterion", "record", "quote", "check"),
    [
        # S1: no term of the condition at all; "lacks negation" is not a contradiction.
        (
            ("include", PREVIOUSLY_TREATED),
            AGED_ADULTS,
            "Patients aged 18 years or older",
            "off_criterion",
        ),
        (
            ("exclude", {"en": "No prior chemotherapy"}),
            AGED_ADULTS,
            "Patients aged 18 years or older",
            "off_criterion",
        ),
        # S2: an Italian quote is not English because it lacks known foreign words.
        (None, ITALIAN, "Melanoma non presente", "language_unverifiable"),
        # S6: the negation belongs to diabetes, not to the condition's term.
        (
            ("include", PREVIOUSLY_TREATED),
            CHEMO_NO_DIABETES,
            "Patients with prior chemotherapy and no diabetes",
            "negation",
        ),
    ],
    ids=["S1-include", "S1-exclude", "S2-italian", "S6-other-negation"],
)
def test_evidence_that_does_not_positively_verify_is_unclear(
    criterion: tuple[str, dict[str, str]] | None, record: ScreeningItem, quote: str, check: str
) -> None:
    c = _melanoma() if criterion is None else _chemo_criterion(*criterion)
    c = c.model_copy(update={"keywords": ()})  # anchor on the wording's own terms
    verdict = _judge_chemo(record, c, quote)
    (result,) = verdict.criteria
    assert result.quote_verified
    assert (result.status, result.downgraded_from, result.failed_check) == (
        "unclear",
        "not_met",
        check,
    )
    assert not verdict.excludes


@pytest.mark.parametrize(
    ("criterion", "record", "quote"),
    [
        (
            ("include", PREVIOUSLY_TREATED),
            NAIVE_ENROLLED,
            "Patients without prior chemotherapy were enrolled",
        ),
        (
            ("include", PREVIOUSLY_TREATED_DE),
            NAIVE_ENROLLED_DE,
            "Patienten ohne vorherige Chemotherapie wurden eingeschlossen",
        ),
        (None, WITH_MELANOMA, "Patients with melanoma"),
    ],
    ids=["negated-inclusion-en", "negated-inclusion-de", "affirmed-exclusion"],
)
def test_evidence_that_positively_verifies_still_excludes(
    criterion: tuple[str, dict[str, str]] | None, record: ScreeningItem, quote: str
) -> None:
    c = _melanoma() if criterion is None else _chemo_criterion(*criterion)
    c = c.model_copy(update={"keywords": ()})  # anchor on the wording's own terms
    verdict = _judge_chemo(record, c, quote)
    (result,) = verdict.criteria
    assert (result.status, result.failed_check, result.quote_verified) == ("not_met", None, True)
    assert verdict.excludes


# Round 4: structural gaps in the filter, each reproduced through `judge`.


def _criterion(polarity: str, text: str, keywords: tuple[str, ...] = ()) -> Criterion:
    return Criterion(
        id="C_CHEMO",  # the answer key `_judge_chemo` uses
        slot="population",
        polarity=polarity,  # ty: ignore[invalid-argument-type]
        text={"en": text},
        decidable_at=("title_abstract",),
        applies_to=(SCOPE,),
        keywords=keywords,
    )


def _record(ref: str, sentence: str) -> ScreeningItem:
    return item(ref, "A randomised phase 3 trial in lung cancer", f"Methods: {sentence}")


@pytest.mark.parametrize(
    ("criterion", "sentence", "quote", "check"),
    [
        (
            ("exclude", "Smokers", ("smokers",)),
            "The study enrolled non-smokers.",
            "The study enrolled non-smokers.",
            "negation",
        ),
        (
            ("exclude", "Non-smokers", ("non-smokers",)),
            "The study enrolled smokers.",
            "The study enrolled smokers.",
            "negation",
        ),
        (
            ("exclude", "Melanoma", ()),
            "The melanocytes were examined in healthy adults.",
            "The melanocytes were examined in healthy adults.",
            "off_criterion",
        ),
        (
            ("include", "Prior chemotherapy", ()),
            "The assay was not performed. Chemotherapy was administered.",
            "The assay was not performed. Chemotherapy was administered.",
            "multiple_sentences",
        ),
        (
            ("include", "Prior chemotherapy", ()),
            "Patients were not without chemotherapy.",
            "Patients were not without chemotherapy.",
            "negation",
        ),
        (
            ("exclude", "Not without chemotherapy", ("chemotherapy",)),
            "We enrolled 212 patients without prior chemotherapy.",
            "patients without prior chemotherapy",
            "polarity",
        ),
        (
            ("exclude", "Melanoma", ()),
            "Melanoma NON presente in alcun paziente arruolato.",
            "Melanoma NON presente in alcun paziente arruolato.",
            "language_unverifiable",
        ),
        (
            ("include", "Prior chemotherapy", ("chemotherapy",)),
            "(The assay was not performed.) Chemotherapy was administered.",
            "(The assay was not performed.) Chemotherapy was administered.",
            "multiple_sentences",
        ),
        (
            ("include", "Prior chemotherapy", ("chemotherapy",)),
            "Patients without prior chemotherapy; they did not participate.",
            "Patients without prior chemotherapy",
            "negation",
        ),
    ],
    ids=[
        "S4-non-smokers-vs-smokers",
        "S4-smokers-vs-non-smokers",
        "S5-melanocytes-vs-melanoma",
        "S7-quote-spans-two-sentences",
        "S3-double-negation-in-quote",
        "S3-double-negation-in-criterion",
        "S2-uppercase-NON",
        "R5-sentence-end-before-closing-bracket",
        "R5-semicolon-hides-a-second-negation",
    ],
)
def test_structurally_unsupported_exclusions_are_unclear(
    criterion: tuple[str, str, tuple[str, ...]], sentence: str, quote: str, check: str
) -> None:
    record = _record("occ-round-4", sentence)
    verdict = _judge_chemo(record, _criterion(*criterion), quote)
    (result,) = verdict.criteria
    assert result.quote_verified
    assert (result.status, result.downgraded_from, result.failed_check) == (
        "unclear",
        "not_met",
        check,
    )
    assert not verdict.excludes
