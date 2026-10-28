"""A cheap, conservative filter on the judge's answers.

The filter rejects exclusions that its evidence does not structurally support: every such
`not_met` becomes `unclear` and names the check. It is not a semantic verifier and proves
nothing. `Verdict.excludes` is a proposal: no screening mode lets the AI exclude a record that no
human saw (DESIGN.md, "Screening modes"), and AI-only exclusions, disagreements, and audit
samples go to adjudication.

A `not_met` stands only if all of these hold:

1. The quote is in the record (`kittum.core.find_quote`) and the record does not try to
   instruct the model.
2. The criterion has an English or German wording: negation is read only in those languages.
3. The quote lies within one sentence.
4. The quote contains a key term of the condition: the protocol's `keywords` if it has any,
   otherwise the content words of the en and de wordings. Terms match as whole words with one
   inflection (-s, -es, -en, -e, -n) and a tiny en/de table. A term that differs from a key term
   only by a negating compound prefix ("non-smokers", "nicht-") does not count, and a match on a
   prefixed term is not read.
5. The quote is positively English or German: it has a function word of those languages (or is
   made only of the condition's own terms and digits), and no token, in any case, that could be
   a function word or negation of another language ("non", "sin", "pas").
6. The quote's sentence has at most one negation cue, inside the quote, at most three tokens
   before a key term and in the same clause. A cue such as "excluded", "with or without", or
   "not only" anywhere in the sentence makes the scope ambiguous. A key term the quote negates
   does not also stand unnegated in the sentence ("adults who did not smoke tobacco and adults
   who smoke tobacco").
7. If the cue negates some of the quote's key terms and not others, the quote states the
   opposite of one requirement of an affirmative condition: "adults who did not smoke tobacco"
   against "Adults who smoke tobacco". Then the key terms it leaves unnegated come before the
   cue. A negative condition is not read this way.
8. The quote fails the criterion in its polarity: it negates an affirmative inclusion condition,
   states a negative inclusion condition's term unnegated, or states an exclusion condition with
   the condition's own polarity. The condition's polarity is read the same way from each wording
   (at most one cue), and the wordings must agree.
9. A negated quote that excludes is read with the whole sentence, or semicolon clause, that it
   ends: the quote runs to its end, and nowhere in it does a word limit the negation to a time,
   a frequency, or an age ("before age 18", "regularly", "every day", "aged 18 years") or
   coordinate or compare another population ("and", "but", "compared with", "matched to").
   Words of the criterion's own wording do not count. So a shorter quote hides nothing.
10. On a study design inclusion criterion with an affirmative wording, a key term with a word
   the wording does not have names another design ("cohort" or "case-control" against
   "Randomized controlled trial"). A quote whose key terms all name another design fails the
   criterion only if the design is the record's own: the key term lies in the noun phrase of a
   sentence that says what the study is ("This was a ...", "This study was a ...", "We
   conducted a ...", "Design: ...", or the title's subtitle after its colon), and that phrase
   ends in a design noun ("study", "analysis", "survey"). And the record names nothing of the
   required design anywhere: no other word of the wording or a near spelling ("trials",
   "randomised"), and for a randomized design no hint of random allocation in any language
   ("RCT", "CRT", "randomly", "assigned by lottery", "Zufallsprinzip", "aleatorio"). A
   prefixed "non-randomized" negates it and does not count. A negated other design ("not a
   cohort study") is no evidence either way.

What the filter cannot catch, by design:

- Foreign content words that carry negation ("Melanoma assente").
- A negation in an intervening clause about another condition, within the window ("Patients
  with no diabetes received chemotherapy").
- Evidence about another arm, cohort, or population than the one the criterion is about.
- A self-description that is about another study than the record's own ("This was a cohort
  study" in a commentary on it).
- Partial matches of a condition ("skeletal metastases" against "brain metastases").
- Anything else that needs understanding rather than structure.

The mitigation is the screening design, not more patterns: every record is seen by a human, an AI
exclusion is a proposal, and disagreements, AI-only exclusions, and audits go to adjudication.
"""

import re
from typing import TYPE_CHECKING, NamedTuple

from kittum.core import Span, find_quote
from kittum.judge.types import CriterionVerdict

if TYPE_CHECKING:
    from kittum.judge.prompt import CriterionAnswer
    from kittum.judge.types import FailedCheck
    from kittum.protocol import Criterion


LIMITS: tuple[str, ...] = (
    'Foreign content words that carry negation ("Melanoma assente").',
    'A negation in an intervening clause about another condition, within the window ("Patients '
    'with no diabetes received chemotherapy").',
    "Evidence about another arm, cohort, or population than the one the criterion is about.",
    "A self-description that is about another study than the record's own "
    '("This was a cohort study" in a commentary on it).',
    'Partial matches of a condition ("skeletal metastases" against "brain metastases").',
    "Anything else that needs understanding rather than structure.",
)
"""What the filter cannot catch, by design, as listed above; reports disclose them."""


def _vocabulary(words: str) -> frozenset[str]:
    return frozenset(words.split())


_STOPWORDS = _vocabulary(
    """a an and are as at be been by for from had has have in into is it its of on or that the
    their them these this those to was were which who whom with within must should least
    years year more than less any all other such only also both either each per
    der die das und oder mit von zu im in ist sind ein eine einer eines den dem des für auf bei
    als aus mindestens nach wie"""
)

_NEGATION_WORDS = _vocabulary(
    """no not without never none neither nor nobody nothing absence absent lack lacking naive
    untreated excluding except cannot didn doesn don isn aren wasn weren hasn haven hadn
    kein keine keiner keinen keinem keines nicht ohne nie niemals weder ausgenommen"""
)

# Cues anywhere in the quote's sentence that turn its meaning around or make it two-sided.
_SCOPE_CUES = re.compile(
    r"\b(excluded|exclude|excluding|exclusion|ineligible|not eligible|were not included|"
    r"was not included|not enrolled|with or without|with and without|regardless of|"
    r"irrespective of|whether or not|independent of|independently of|not only|not just|"
    r"ausgeschlossen|ausschluss|mit oder ohne|unabhängig von|nicht nur)\b",
    re.IGNORECASE,
)

# Languages whose negation Kittum reads. Polarity in any other language cannot be verified.
POLARITY_LANGUAGES = frozenset({"en", "de"})

# Negation cues and common function words of other languages, to notice quotes Kittum cannot read.
_FOREIGN_NEGATION = _vocabulary(
    """non sin nunca ningún ninguna ninguno ni sans aucun aucune jamais pas senza nessun nessuna
    sem nenhum nenhuma não nao zonder geen niet nooit"""
)
_FOREIGN_WORDS = (
    _vocabulary(
        """de del el la los las y con que por para una sus le les des du avec pour une il lo gli
    della delle os dos das com het een en voor"""
    )
    - _STOPWORDS
)


# Other English and German function words. A quote needs at least one function word to count as
# English or German.
_FUNCTION_WORDS = (
    _STOPWORDS
    | _NEGATION_WORDS
    | _vocabulary(
        """after before during but if then there over under up out who whose what when where
        while upon via versus vs did do does done being can could may might will would
        wurden wurde werden worden waren war hatten hatte haben hat vor über unter durch gegen
        bis seit sowie aber zur zum vom beim am"""
    )
)

# Short English and German content words that are not function words.
_SHORT_WORDS = _vocabulary(
    """aged age old men mg kg ml day days week year arm arms dose rate risk cell lung type stage
    line oral iv alt jahr tag tage"""
)

# Tokens that end a clause between a negation cue and the term it would negate.
_CLAUSE_BREAKS = _vocabulary(", ; : . ! ? and or but whereas while und oder aber sowie während")

# All-caps tokens shorter than three letters that are known abbreviations, not foreign words.
_ACRONYMS = _vocabulary("hr ci os iv sc im po ae uk us eu")

# Inflections a term may carry, longest first, and en/de pairs that differ by more.
_SUFFIXES = ("es", "en", "s", "e", "n")
_EQUIVALENT = {"chemotherapie": "chemotherapy", "chemotherapies": "chemotherapy"}

_WINDOW = 3  # a negation cue negates a key term at most this many tokens later

# A negated quote that excludes is read with the whole sentence, or semicolon clause, it ends: in
# it, no word may limit the negation to a time, a frequency, or an age ("did not smoke tobacco
# before age 18", "did not regularly smoke"), and none may bring in another population ("and
# smokers of cigars", "compared with"). A shorter quote cannot hide either.
_QUALIFIER = re.compile(
    r"\b(before|after|since|until|till|during|ever|every|each|daily|weekly|monthly|yearly|"
    r"annually|nightly|regularly|occasionally|usually|always|often|frequently|sometimes|rarely|"
    r"habitually|heavily|previously|formerly|lifetime|history|past|anymore|longer|yet|"
    r"prior\s+to|at\s+least|at\s+most|more\s+than|less\s+than|fewer\s+than|per|"
    r"age|aged|ages|old|older|younger|"
    r"\d+(?:[.,]\d+)?\s*-?\s*(?:years?|yrs?|months?|weeks?|wks?|days?|hours?|hrs?|minutes?|mins?)|"
    r"vor|nach|seit|bis|während|jemals|je|jede[nmrs]?|täglich|wöchentlich|monatlich|jährlich|"
    r"regelmäßig|gelegentlich|früher|zuvor|bisher|mindestens|höchstens|alter|alt|älter|jünger|"
    r"jahre?n?|monate?n?|wochen?|tage?n?)\b",
    re.IGNORECASE,
)
_COORDINATION = re.compile(
    r"\b(and|or|as\s+well\s+as|plus|including|but|except|excluding|versus|vs|compared|"
    r"comparison|than|whereas|while|whilst|unlike|together\s+with|along\s+with|matched|"
    r"und|oder|sowie|aber|außer|einschließlich|gegenüber|verglichen|sondern)\b|&|(?<=\w)/(?=\w)",
    re.IGNORECASE,
)

# Where a record says what its own study is, so that the noun phrase after it names its design:
# "This study was a ...", "This was a ...", "We conducted a ...", "Design: ...", and the title's
# "...: a ...". The phrase runs to the first function word and ends in a design noun.
_SELF_DESCRIPTION = re.compile(
    r"(?:(?:methods?|design|study\s+design)\s*:\s*)?"
    r"(?:(?:this|our|the(?:\s+present|\s+current)?)\s+(?:study|trial|analysis|work|paper|report)"
    r"\s+(?:is|was)|this\s+(?:is|was)|we\s+(?:conducted|performed|carried\s+out|undertook|report"
    r"|present|designed))\s+an?\s+"
    r"|(?:design|study\s+design|study\s+type)\s*:\s*(?:an?\s+)?",
    re.IGNORECASE,
)
_SUBTITLE = re.compile(r"\s*(?:an?\s+)?", re.IGNORECASE)
_DESIGN_NOUNS = _vocabulary(
    """study studies design analysis analyses survey surveys review reviews series"""
)

# Any hint that a record's allocation was random, in the languages abstracts come in. On a
# randomized design criterion, it blocks every other design ("Randomization was not possible"
# too: the filter stays conservative). A prefixed "non-randomized" is no such hint.
_RANDOM_ALLOCATION = re.compile(
    r"(?<!non-)(?<!nicht-)\b(?:c?rcts?|crts?|random\w*|quasi-?random\w*|lotter\w*|"
    r"coin\s+(?:toss|flip)\w*|toss(?:ing)?\s+(?:of\s+)?a\s+coin|zufall\w*|aleatori\w*|"
    r"al[eé]atoire\w*|al\s+azar|au\s+hasard|tirage\s+au\s+sort|sorteo|sorteggio)",
    re.IGNORECASE,
)

# A line break alone does not end a sentence: abstracts are often wrapped mid-sentence.
# A sentence ends at . ! or ?, after any closing bracket or quote mark. A semicolon joins two
# clauses of one sentence, so it bounds negation scope but not the sentence.
_CLOSERS = ")\\]\"'\u201d\u2019\u00bb"
_SENTENCE_END = re.compile(rf"(?<=[.!?])[{_CLOSERS}]*\s+|\n\s*\n")

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


# Words, with a negating compound prefix kept as part of the word ("non-smokers" is not
# "smokers"), clause punctuation, and sentence ends.
_TOKEN = re.compile(
    rf"(?:non|nicht|kein)-[^\W_]+|[^\W_]+|[,;:]|[.!?](?=[{_CLOSERS}]*(?:\s|$))", re.IGNORECASE
)
_PREFIX = re.compile(r"^(?:non|nicht|kein)-")
_PUNCTUATION = frozenset(",;:.!?")


def _tokens(text: str, offset: int = 0) -> list[tuple[str, int, int, str]]:
    """Lowercased tokens with their offsets (plus `offset`) and original form."""
    return [
        (m.group().lower(), m.start() + offset, m.end() + offset, m.group())
        for m in _TOKEN.finditer(text)
    ]


def _term(word: str) -> str:
    """A whole word without one inflection: "patients" and "Patienten" are both "patient"."""
    word = _EQUIVALENT.get(word, word)
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _unprefixed(term: str) -> str:
    return _term(_PREFIX.sub("", term))


def _content(word: str) -> bool:
    return (
        len(word) > 2
        and word.replace("-", "").isalpha()
        and word not in _STOPWORDS
        and word not in _NEGATION_WORDS
    )


def _wordings(criterion: Criterion) -> list[str]:
    return [t for lang, t in criterion.text.items() if lang.split("-")[0] in POLARITY_LANGUAGES]


def _wording_terms(criterion: Criterion) -> frozenset[str]:
    return frozenset(_term(w) for t in _wordings(criterion) for w, *_ in _tokens(t) if _content(w))


def _key_terms(criterion: Criterion) -> tuple[tuple[str, ...], ...]:
    """Each key term as a sequence of stems: the protocol's keywords, else the wording's terms."""
    if criterion.keywords:
        return tuple(
            tuple(_term(w) for w, *_ in _tokens(k) if w not in _PUNCTUATION)
            for k in criterion.keywords
        )
    return tuple((t,) for t in sorted(_wording_terms(criterion)))


def _negates(words: list[str], cue: int, target: int) -> bool:
    """Whether the cue at index `cue` scopes over the token at index `target`."""
    return 0 < target - cue <= _WINDOW and not any(
        w in _CLAUSE_BREAKS for w in words[cue + 1 : target]
    )


def _condition_negative(wording: str) -> bool | None:
    """True for "no prior chemotherapy", False for "prior chemotherapy", None if mixed."""
    words = [w for w, *_ in _tokens(wording)]
    cues = [i for i, w in enumerate(words) if w in _NEGATION_WORDS]
    if not cues:
        return False
    if len(cues) > 1:
        return None  # "not without chemotherapy": a double negation is not read
    terms = [j for j, w in enumerate(words) if _content(w)]
    every_term_negated = all(any(_negates(words, c, j) for c in cues) for j in terms)
    every_cue_used = all(any(_negates(words, c, j) for j in terms) for c in cues)
    return True if terms and every_term_negated and every_cue_used else None


def _quote_readable(quote: str, criterion: Criterion) -> bool:
    known = _wording_terms(criterion) | {t for key in _key_terms(criterion) for t in key}
    tokens = [(w, original) for w, _, _, original in _tokens(quote) if w not in _PUNCTUATION]
    if not any(w in _FUNCTION_WORDS for w, _ in tokens) and not all(
        w.isdigit() or _term(w) in known for w, _ in tokens
    ):
        return False
    for w, original in tokens:
        if w in _FOREIGN_NEGATION or w in _FOREIGN_WORDS:
            return False
        short_unknown = (
            w.isalpha()
            and len(w) <= 4
            and w not in _FUNCTION_WORDS
            and w not in _SHORT_WORDS
            and _term(w) not in known
        )
        acronym = (original.isupper() and len(original) >= 3) or w in _ACRONYMS
        if short_unknown and not acronym:
            return False
    return True


def _sentence(source: str, span: Span) -> tuple[int, int]:
    starts = [0] + [m.end() for m in _SENTENCE_END.finditer(source, 0, span.start)]
    end_match = _SENTENCE_END.search(source, span.end)
    return starts[-1], end_match.start() if end_match else len(source)


class _Reading(NamedTuple):
    """How one occurrence of a quote reads, or the check that fails reading it."""

    failed: FailedCheck | None
    negated: bool = False  # the cue negates the quote's key terms
    partial: bool = False  # ... some of them, the last ones: one requirement of the condition
    keys: frozenset[tuple[str, ...]] = frozenset()  # the key terms the quote contains
    own_design: bool = False  # another design the quote names is the record's own


def _related(word: str, terms: frozenset[str]) -> bool:
    """Whether a stem is a term or shares its first six letters ("randomised", "randomized")."""
    return word in terms or (len(word) >= 6 and any(word[:6] == t[:6] for t in terms))


def _other_designs(criterion: Criterion) -> frozenset[tuple[str, ...]]:
    """Key terms with a word the wording does not have: on a design criterion, other designs."""
    own = _wording_terms(criterion)
    return frozenset(k for k in _key_terms(criterion) if any(not _related(t, own) for t in k))


def _names_required_design(criterion: Criterion, source: str) -> bool:
    """Whether the record names the required design anywhere: a word of the wording or a near
    spelling, outside the other designs' own words, or for a randomized design any hint of
    random allocation ("RCT", "assigned by lottery", "Zufallsprinzip")."""
    own = _wording_terms(criterion)
    if any(t.startswith("random") for t in own) and _RANDOM_ALLOCATION.search(source):
        return True
    tokens = _tokens(source)
    words = [_term(w) for w, *_ in tokens]
    others = {
        i + k
        for key in _other_designs(criterion)
        for i in range(len(words) - len(key) + 1)
        if tuple(words[i : i + len(key)]) == key
        for k in range(len(key))
    }
    return any(
        _related(w, own) and not _PREFIX.match(raw) and i not in others
        for i, (w, (raw, *_)) in enumerate(zip(words, tokens, strict=True))
    )


def _design_phrase(source: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """The noun phrase in which the sentence names the record's own design, as (word, start,
    end): after "This was a", "We conducted a", or "Design:", up to the first function word, or
    in the title the whole subtitle after its last colon. Empty if the sentence has none."""
    sentence = source[start:end]
    frame = _SELF_DESCRIPTION.match(sentence)
    subtitle = frame is None and start == 0 and ":" in sentence  # a title runs from offset 0
    if subtitle:
        frame = _SUBTITLE.match(sentence, sentence.rindex(":") + 1)
    if frame is None:
        return []
    phrase = []
    for w, s, e, _ in _tokens(sentence[frame.end() :], start + frame.end()):
        if w in _PUNCTUATION or w in _FUNCTION_WORDS:
            if subtitle and w not in ".!?":
                return []  # a subtitle names the design only as a whole
            break
        phrase.append((w, s, e))
    return phrase if phrase and phrase[-1][0] in _DESIGN_NOUNS else []


def _reads_whole(criterion: Criterion, source: str, span: Span) -> bool:
    """Whether a negated quote can be read with the whole sentence, or semicolon clause, that it
    ends: nothing follows it there, and nothing in it limits the negation to a time, a
    frequency, or an age or brings in another population."""
    start, end = _sentence(source, span)
    start = source.rfind(";", start, span.start) + 1 or start
    if (semicolon := source.find(";", span.end, end)) >= 0:
        end = semicolon
    if source[span.end : end].strip(" \t\n.!?" + _CLOSERS):
        return False
    unit = source[start:end]
    own = _wording_terms(criterion)
    limits = [
        m
        for m in _QUALIFIER.finditer(unit)
        if not all(_term(w) in own for w, *_ in _tokens(m.group()))  # "older" in "or older"
    ]
    return not limits and _COORDINATION.search(unit) is None


def _anchored_negation(criterion: Criterion, source: str, span: Span) -> _Reading:
    """Whether the quote's key terms are negated, or the check that fails reading it."""
    start, end = _sentence(source, span)
    sentence = source[start:end]
    tokens = _tokens(sentence, start)
    words = [_term(w) for w, *_ in tokens]
    inside = [span.start <= s and e <= span.end for _, s, e, _ in tokens]
    matches = [
        (i, key)
        for key in _key_terms(criterion)
        for i in range(len(words) - len(key) + 1)
        if all(inside[i : i + len(key)]) and tuple(words[i : i + len(key)]) == key
    ]
    anchors = sorted({i for i, _ in matches})
    keys = {t for key in _key_terms(criterion) for t in key}
    quoted = {w for w, i in zip(words, inside, strict=True) if i}
    if any(
        _unprefixed(q) == _unprefixed(k) and (_PREFIX.match(q) or _PREFIX.match(k))
        for q in quoted
        for k in keys
    ):
        return _Reading("negation")  # "non-smokers" against "smokers", or either way round
    if not anchors:
        return _Reading("off_criterion")
    if _SCOPE_CUES.search(sentence):
        return _Reading("negation")
    raw = [w for w, *_ in tokens]
    cues = [i for i, w in enumerate(raw) if w in _NEGATION_WORDS]
    if len(cues) > 1:
        return _Reading("negation")  # "not without chemotherapy": a double negation is not read
    if any(not inside[c] for c in cues):
        return _Reading("negation")  # a negation outside the quote may reverse it
    if any(not any(_negates(raw, c, a) for a in anchors) for c in cues):
        return _Reading("negation")  # a negation the key term does not explain
    others = _other_designs(criterion)
    phrase = {(s, e) for _, s, e in _design_phrase(source, start, end)}
    reading = _Reading(
        None,
        keys=frozenset(key for _, key in matches),
        own_design=all(
            all(tokens[j][1:3] in phrase for j in range(i, i + len(key)))
            for i, key in matches
            if key in others
        ),
    )
    negated = [a for a in anchors if any(_negates(raw, c, a) for c in cues)]
    if not negated:
        return reading
    if any(words[j] == words[a] for a in negated for j in range(len(words)) if j not in negated):
        return _Reading("negation")  # the sentence also states the negated term unnegated
    if len(negated) == len(anchors):
        return reading._replace(negated=True)
    # Some key terms negated, some not: the quote negates one requirement of the condition only
    # if the cue comes after every term it leaves alone.
    (cue,) = cues
    if any(a > cue for a in anchors if a not in negated):
        return _Reading("negation")
    return reading._replace(negated=True, partial=True)


def _check_not_met(
    criterion: Criterion, quote: str, source: str, spans: tuple[Span, ...]
) -> FailedCheck | None:
    wordings = _wordings(criterion)
    if not wordings:
        return "language_unverifiable"
    if any(_SENTENCE_END.search(source, span.start, span.end) for span in spans):
        return "multiple_sentences"
    readings = [_anchored_negation(criterion, source, span) for span in spans]
    failures = [r.failed for r in readings if r.failed]
    if "off_criterion" in failures:
        return "off_criterion"
    if not _quote_readable(quote, criterion):
        return "language_unverifiable"
    if failures:
        return failures[0]
    negated = {r.negated for r in readings}
    conditions = {_condition_negative(w) for w in wordings}
    if len(negated) != 1 or len(conditions) != 1 or None in conditions:
        return "polarity"
    (quote_negated,) = negated
    (condition_negative,) = conditions
    if any(r.partial for r in readings) and condition_negative:
        return "negation"  # one negated requirement of a negative condition is not read
    if (
        criterion.slot == "study_type"
        and criterion.polarity == "include"
        and not condition_negative
    ):
        others = _other_designs(criterion)
        designs = [r.keys <= others for r in readings]
        if any(designs):
            # Another design than the one required: stated as the record's own, it fails the
            # criterion; negated, it says nothing. A record that also names the required design
            # anywhere is not read.
            if (
                not all(designs)
                or quote_negated
                or not all(r.own_design for r in readings)
                or _names_required_design(criterion, source)
            ):
                return "polarity"
            return None
    contradicts = quote_negated != condition_negative
    fails = contradicts if criterion.polarity == "include" else not contradicts
    if not fails:
        return "polarity"
    if quote_negated and not all(_reads_whole(criterion, source, span) for span in spans):
        return "negation"  # "did not smoke tobacco" from "... every day", "... and smokers"
    return None


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
        failed = (
            "injection_suspected" if injection else _check_not_met(criterion, quote, source, spans)
        )
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
