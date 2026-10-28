"""What the protocol assistant asks a model. The instructions are fixed text, versioned and
hashed for the disclosure; only the lead's description and the protocol itself vary."""

import json
from typing import Any

from kittum.core import sha256_hex

PROMPT_VERSION = "assist-1"

_SHAPE = """\
Answer with one JSON object in the given schema, nothing else.

How Kittum reads the answer:
- IDs start with a letter and use letters, digits, `_`, `.` and `-` (at most 64 characters).
- Texts in several languages are lists of {"key": language code, "value": text}, and every one of
  them includes the protocol's default language.
- Write null for an optional field you leave out. Leave `profile` null and `reference_set` with
  empty lists: do not invent known studies.
- A question names its outcomes by ID from `outcomes` (an empty list means every outcome).
- A search topic lists one scope per question it covers: {"id": the question's ID, "label": that
  question's label in the default language, word for word, "kind": "question"}.
- Mode "M1" (two people screen every record, the AI reads beside them) needs no rationale; leave
  `mode_rationale`, `validation_pack` and `stopping` null. Audit: sentinel_percent "1",
  random_audit_percent "10", census_of_disagreements true. Freshness: max_age_months 12.
- Criteria are executable: one per eligibility condition, with the PICOS slot it checks
  (population, intervention, comparator, outcome, study_type, other), polarity "include" or
  "exclude", a one-sentence text a reviewer can decide, `decidable_at` ["title_abstract",
  "full_text"] or ["full_text"] for what only a full text shows, and `applies_to` the topic ID or
  question IDs. Every question needs a criterion decidable at title and abstract. `keywords` are
  words an exclusion quote must contain; leave them empty unless the criterion excludes on a
  named term.
- Sources: always MEDLINE on PubMed {"id": "medline", "kind": "bibliographic", "name": "MEDLINE",
  "platform": "PubMed"} and ClinicalTrials.gov {"id": "clinicaltrials_gov", "kind": "registry",
  "name": "ClinicalTrials.gov", "platform": "ClinicalTrials.gov"}, each with its `strategy`; add
  other sources the review needs with a null strategy.
- A PubMed strategy: one block of synonyms per concept in parentheses, joined with AND; each
  block ORs a MeSH heading with [mh] and free-text words and quoted phrases with [tiab],
  truncation with * where it helps; then the study design (e.g. randomized controlled trial[pt]
  OR randomized[tiab] OR placebo[tiab]) and limits such as [la] or a date range with [dp]. Use
  only PubMed's field tags.
- A ClinicalTrials.gov strategy uses its Essie syntax: words and quoted phrases joined with AND,
  OR and NOT, grouped in parentheses, optionally restricted with AREA[Condition] or
  AREA[InterventionName]."""

DRAFT_INSTRUCTIONS = f"""\
You draft the protocol of a systematic literature review for Kittum, a workbench where human
reviewers screen and extract under that protocol. The lead describes the review in a few
sentences; you write the protocol's first draft: its title, the review questions with their
population, intervention and comparators, the outcomes, one search topic with its screening plan
and its eligibility criteria, and the sources with a search strategy for PubMed and for
ClinicalTrials.gov. The lead checks and edits every part before anyone uses it.

{_SHAPE}"""

IMPROVE_INSTRUCTIONS = f"""\
You improve one part of a systematic review protocol written for Kittum. You get the part and the
whole protocol for context. Keep what the authors meant; make the part more precise and complete
for the reviewers who apply it and, for a search strategy, more sensitive without losing
precision. A reviewer accepts or rejects each of your changes on its own, so make few changes
(two to five), each independent of the others, and give each a rationale of one short sentence.

For a search strategy, answer with changes: `find` is an exact, unique passage of the current
strategy, `replace` the text that takes its place. Changes must not overlap, and each one alone
must leave a valid query (balanced parentheses and quotes). Typical improvements: a MeSH heading
next to the free-text words of a concept, missing synonyms and forms, truncation, a sensitive
study-design filter. Use only the source's own syntax.

For any other part, answer with the whole part as `replacement`, under the same ID, and one entry
in `changes` per field you changed, naming the field.

{_SHAPE}"""

# Sent with a Europe PMC strategy only, so the requests for PubMed and ClinicalTrials.gov, and
# their recorded answers, stay as they were.
EUROPEPMC_SYNTAX = """\
The strategy is written for Europe PMC's search. Its syntax: FIELD:term or FIELD:"a phrase", with
fields such as TITLE, ABSTRACT, TITLE_ABS, KW, MESH, AUTH, JOURNAL, PUB_TYPE, SRC (MED, PMC, PPR),
LANG, and FIRST_PDATE:[2024-01-01 TO 2024-12-31]; words and fields joined with AND, OR and NOT and
grouped in parentheses; * truncates a word. Kittum runs it with synonyms off, so write every
synonym and spelling out. Europe PMC finds nothing for a field it does not know."""

REPAIR = """\
Kittum could not use your answer. Fix every problem below and answer with the whole corrected
JSON object again.

{problems}"""


def instructions_hash(text: str) -> str:
    return sha256_hex(text.encode())


def draft_request(description: str, language: str) -> str:
    return (
        f"The protocol's default language: {language}.\n\n"
        f"The review, in the lead's words:\n\n{description.strip()}"
    )


def improve_request(
    kind: str, path: str, part: Any, protocol: dict[str, Any], syntax: str | None = None
) -> str:
    what = {
        "strategy": "the search strategy",
        "criterion": "the eligibility criterion",
        "question": "the review question with its population, intervention and comparators",
    }[kind]
    shown = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False, indent=1)
    return (
        f"Improve {what} at `{path}`:\n\n{shown}\n\n"
        + (f"{syntax}\n\n" if syntax else "")
        + f"The whole protocol, for context:\n\n{json.dumps(protocol, ensure_ascii=False)}"
    )


def problems(issues: list[tuple[str, str]]) -> str:
    return REPAIR.format(
        problems="\n".join(f"- {p or '(the whole answer)'}: {m}" for p, m in issues)
    )
