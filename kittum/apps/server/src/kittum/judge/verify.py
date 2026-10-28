"""Deterministic checks that decide whether a model's answer may stand.

An exclusion stands only if its quote is in the record (`kittum.core.find_quote`), relates to the
criterion, is not turned around by a negation or exclusion cue, and fails the criterion in its
polarity: it contradicts an inclusion condition or affirms an exclusion condition. Negation is
read only in the languages Kittum has cues for; a criterion without a wording in one of them, or
a quote in another language, cannot exclude. Any failure downgrades the
criterion to `unclear` and names the check. The checks are deliberately conservative: a wrong
downgrade costs a human a look, a wrong exclusion can lose a study.
"""

import re
from typing import TYPE_CHECKING

from kittum.core import Span, find_quote
from kittum.judge.types import CriterionVerdict

if TYPE_CHECKING:
    from kittum.judge.prompt import CriterionAnswer
    from kittum.judge.types import FailedCheck
    from kittum.protocol import Criterion

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _vocabulary(words: str) -> frozenset[str]:
    return frozenset(words.split())


_STOPWORDS = _vocabulary(
    """a an and are as at be been by for from had has have in into is it its of on or that the
    their them these this those to was were which who whom with within must should least
    years year more than less any all other such only also both either each per
    der die das und oder mit von zu im in ist sind ein eine einer eines den dem des für auf bei
    als aus mindestens nach wie"""
)

# Words that tie a quote to a PICO slot even when the criterion's own wording is not reused.
_SLOT_WORDS: dict[str, frozenset[str]] = {
    "population": _vocabulary(
        "patient participant subject adult child adolescent pediatric paediatric infant "
        "neonate elderly aged age old men women male female people individual volunteer "
        "healthy diagnosed diagnosis disease disorder cancer carcinoma tumour tumor stage "
        "metastatic advanced recurrent refractory relapsed prior previous previously "
        "pretreated untreated naive history line enrolled eligible inclusion "
        "patienten erwachsene kinder jugendliche alter jahre frauen männer erkrankung"
    ),
    "intervention": _vocabulary(
        "treat treated treatment therapy dose dosage mg kg administered received receive "
        "given regimen drug infusion oral intravenous subcutaneous monotherapy combination "
        "plus arm group randomized randomised assigned allocated "
        "behandlung behandelt therapie dosis"
    ),
    "comparator": _vocabulary(
        "placebo versus vs compared comparison comparator control controlled standard care "
        "usual sham active alone arm group single uncontrolled best supportive "
        "vergleich kontrolle"
    ),
    "outcome": _vocabulary(
        "survival mortality death response progression endpoint outcome quality life "
        "adverse event safety toxicity hazard ratio efficacy pain symptom remission relapse "
        "hospitalization hospitalisation exacerbation function score measured "
        "überleben endpunkt lebensqualität nebenwirkung"
    ),
    "study_type": _vocabulary(
        "randomized randomised random trial study controlled blind blinded double open label "
        "phase cohort case report series review meta analysis observational retrospective "
        "prospective cross sectional registry single arm crossover design editorial letter "
        "comment protocol systematic narrative animal mice rats vitro preclinical "
        "conference "
        "randomisiert studie kontrolliert"
    ),
    "other": frozenset(),
}

_NEGATION_WORDS = _vocabulary(
    """no not without never none neither nor nobody nothing absence absent lack lacking naive
    untreated excluding except cannot didn doesn don isn aren wasn weren hasn haven hadn
    kein keine keiner keinen keinem keines nicht ohne nie niemals weder ausgenommen"""
)

# Cues anywhere in the quote's sentence that turn its meaning around or make it two-sided.
_SCOPE_CUES = re.compile(
    r"\b(excluded|exclude|excluding|exclusion|ineligible|not eligible|were not included|"
    r"was not included|not enrolled|with or without|with and without|regardless of|"
    r"irrespective of|whether or not|independent of|independently of|"
    r"ausgeschlossen|ausschluss|mit oder ohne|unabhängig von)\b",
    re.IGNORECASE,
)

# Languages whose negation Kittum reads. Polarity in any other language cannot be verified.
POLARITY_LANGUAGES = frozenset({"en", "de"})

# Negation cues and common function words of other languages, to notice quotes Kittum cannot read.
_FOREIGN_NEGATION = _vocabulary(
    """sin nunca ningún ninguna ninguno ni sans aucun aucune jamais pas senza nessun nessuna
    sem nenhum nenhuma não nao zonder geen niet nooit"""
)
_FOREIGN_WORDS = (
    _vocabulary(
        """de del el la los las y con que por para una sus le les des du avec pour une il lo gli
    della delle os dos das com het een en voor"""
    )
    - _STOPWORDS
)

# "nicht-kleinzellig", "non-small": a compound modifier, not a negation.
_COMPOUND_PREFIX = re.compile(r"\b(nicht|non|kein)-(?=\w)", re.IGNORECASE)

# A line break alone does not end a sentence: abstracts are often wrapped mid-sentence.
_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+|\n\s*\n")

# Text in a record that addresses the model. The only harmful effect of an injection is an
# exclusion, so a record that matches cannot be excluded by the judge.
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(the\s+|your\s+)?"
    r"(previous|prior|above|earlier|preceding|system|screening)?\s*"
    r"(instructions?|rules|prompts?|directions|guidelines)"
    r"|\b(new|updated|revised)\s+instructions?\b"
    r"|\b(system|developer|assistant)\s*(prompt|message)\b"
    r"|\byou\s+(are\s+now|must\s+now|must\s+answer|should\s+answer|will\s+answer)\b"
    r"|\b(language\s+model|llm|ai\s+(system|model|assistant|screener|reviewer)s?|"
    r"automated\s+(screening|review)\w*|screening\s+(system|tool|model|assistant)s?)\b"
    r"|\b(mark|label|classify|rate|answer|set|code)\s+(this|the|all|every|each)\s+"
    r"(record|study|article|abstract|paper|criteri\w*)\b"
    r"|\bnot_met\b"
    r"|<\|[a-z_]+\|?>|<\|?[a-z_]+\|>|</?record\b"
    r"|ignoriere\s+(alle\s+)?(vorherigen|bisherigen|obigen)\s+anweisungen"
    # Directives aimed at whoever assesses the record: "output a negative decision", "for
    # eligibility assessment, ...", "using this evidence", "actual methods:".
    r"|\b(output|return|give|produce|emit|issue|assign|reach|respond\s+with)"
    r"\s+(an?\s+|the\s+|your\s+)?(negative|positive|exclusion|inclusion|exclude|include)"
    r"\s+(decision|verdict|label|answer|rating|judge?ment|classification|status)"
    r"|\b(negative|positive)\s+(screening\s+|eligibility\s+)?(decision|verdict|classification)"
    r"|\bfor\s+(the\s+)?(eligibility|inclusion|exclusion)\s+(assessment|decision|review|check)"
    r"|\b(decide|mark|classify|label|flag|treat|consider|deem)\s+(this|the)\s+"
    r"(record|study|trial|article|abstract|paper|publication|citation)\s+(as\s+)?"
    r"(excluded|included|ineligible|eligible|irrelevant|relevant|not\s+relevant)"
    r"|\b(exclude|include|reject|accept)\s+(this|the)\s+"
    r"(record|study|article|abstract|paper|publication|citation)\b"
    r"|\busing\s+this\s+evidence\b"
    r"|\b(actual|real|true)\s+(methods|results|data|population)\s*:"
    r"|\bnote\s+to\s+(the\s+)?(reviewers?|assessors?|screeners?|evaluators?|models?|ai)\b",
    re.IGNORECASE,
)


def injection_suspected(text: str) -> bool:
    return _INJECTION.search(text) is not None


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _stem(word: str) -> str:
    return word[:6]


def criterion_keywords(criterion: Criterion) -> frozenset[str]:
    """Stems of the criterion's own content words and of its slot's vocabulary."""
    texts = [
        *criterion.text.values(),
        *criterion.keywords,
        *criterion.positive_examples,
        *criterion.negative_examples,
    ]
    own = {
        _stem(w)
        for t in texts
        for w in _words(t)
        if len(w) > 2 and not w.isdigit() and w not in _STOPWORDS
    }
    return frozenset(own | {_stem(w) for w in _SLOT_WORDS[criterion.slot]})


def _relates_to_criterion(quote: str, criterion: Criterion) -> bool:
    if criterion.keywords:  # the protocol's own rule: the quote must contain one of them
        folded = quote.casefold()
        return any(k.casefold() in folded for k in criterion.keywords)
    keywords = criterion_keywords(criterion)
    return any(_stem(w) in keywords for w in _words(quote))


def _sentence_around(source: str, span: Span) -> tuple[str, str]:
    """The text before and after the span, within the span's sentence."""
    starts = [0] + [m.end() for m in _SENTENCE_END.finditer(source, 0, span.start)]
    end_match = _SENTENCE_END.search(source, span.end)
    end = end_match.start() if end_match else len(source)
    return source[starts[-1] : span.start], source[span.end : end]


def _negative(text: str) -> bool:
    return any(w in _NEGATION_WORDS for w in _words(_COMPOUND_PREFIX.sub("", text)))


def _supported_wordings(criterion: Criterion) -> list[str]:
    return [t for lang, t in criterion.text.items() if lang.split("-")[0] in POLARITY_LANGUAGES]


def _quote_readable(quote: str) -> bool:
    """False if the quote carries a negation cue, or is mostly words, of another language."""
    words = _words(quote)
    if any(w in _FOREIGN_NEGATION for w in words):
        return False
    foreign = sum(w in _FOREIGN_WORDS for w in words)
    own = sum(w in _STOPWORDS or w in _NEGATION_WORDS for w in words)
    return not foreign or foreign < own


def _term(word: str) -> str:
    return (word[:-1] if word.endswith("s") and len(word) > 4 else word)[:6]


def _condition_terms(criterion: Criterion) -> frozenset[str]:
    """Content words of the condition, without its slot's generic vocabulary where possible."""
    terms = {
        _term(w)
        for t in _supported_wordings(criterion)
        for w in _words(_COMPOUND_PREFIX.sub("", t))
        if len(w) > 2 and not w.isdigit() and w not in _STOPWORDS and w not in _NEGATION_WORDS
    }
    specific = terms - {_term(w) for w in _SLOT_WORDS[criterion.slot]}
    return frozenset(specific or terms)


def _fails_in_polarity(quote: str, criterion: Criterion) -> bool:
    """Whether the quote contradicts an inclusion condition or affirms an exclusion condition.

    A quote that mentions the condition's terms is compared by negation: "patients without prior
    chemotherapy" contradicts "previously treated with chemotherapy" and affirms "no prior
    chemotherapy". A quote that does not mention them states another value ("patients aged 12 to
    17 years" against "adults"), which the model judged; a negation in such a quote cannot be
    tied to the condition, so it does not stand.
    """
    readings = {_negative(t) for t in _supported_wordings(criterion)}
    if len(readings) != 1:
        return False  # the wordings disagree, or none can be read
    (condition_negative,) = readings
    quote_negative = _negative(quote)
    if not any(_term(w) in _condition_terms(criterion) for w in _words(quote)):
        return not quote_negative
    contradicts = quote_negative != condition_negative
    return contradicts if criterion.polarity == "include" else not contradicts


def _negation_flips(source: str, quote: str, spans: tuple[Span, ...]) -> bool:
    for span in spans:
        before, after = _sentence_around(source, span)
        # "We excluded patients without ...", "patients with or without ...", "[patients
        # without prior chemotherapy] did not participate": any negation or exclusion in the
        # rest of the sentence may reverse the quote, so the exclusion goes to a human.
        if _SCOPE_CUES.search(f"{before} {quote} {after}") or _negative(f"{before} {after}"):
            return True
    return False


def verify(
    criterion: Criterion,
    answer: CriterionAnswer,
    source: str,
    *,
    injection: bool,
) -> CriterionVerdict:
    quote = answer.quote.strip() or None
    spans = find_quote(quote, source) if quote else ()
    verified = bool(spans)
    failed: FailedCheck | None = None
    if quote and not verified and answer.status != "unclear":
        failed = "quote_not_found"
    elif answer.status == "not_met" and quote is not None:
        if injection:
            failed = "injection_suspected"
        elif not _relates_to_criterion(quote, criterion):
            failed = "off_criterion"
        elif not _supported_wordings(criterion) or not _quote_readable(quote):
            failed = "language_unverifiable"
        elif _negation_flips(source, quote, spans):
            failed = "negation"
        elif not _fails_in_polarity(quote, criterion):
            failed = "polarity"
    elif answer.status == "not_met":
        failed = "no_quote"
    return CriterionVerdict(
        criterion_id=criterion.id,
        status="unclear" if failed else answer.status,
        quote=quote,
        rationale=answer.rationale.strip(),
        quote_verified=verified,
        downgraded_from=answer.status if failed else None,
        failed_check=failed,
    )
