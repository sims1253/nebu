"""Compile a protocol source into a hashed, cross-checked `ProtocolVersion`.

Compilation runs in passes, and each reports every problem it finds, not just the first:

1. YAML or JSON parsing, rejecting duplicate keys.
2. Schema validation of every core field.
3. Core cross-checks that a schema cannot express: unique IDs, scopes that exist, text in the
   default language, criteria a reviewer can decide at title and abstract, the screening mode
   rules, and the syntax of each source's search strategy as far as it can be checked without
   running it (balanced brackets and quotes). A PubMed field tag PubMed does not list is a
   warning (`protocol_warnings`), not a problem: PubMed searches it anyway.
4. The profile, if the protocol names one: its extension schemas, its vocabularies, then its rules.

Each problem carries the path into the source (`search_topics[1].criteria[0].applies_to[2]`), so an
author can find it.
"""

import re
from collections import Counter
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any, Literal

import yaml
import yaml.constructor
import yaml.resolver
from pydantic import BaseModel, ValidationError

from kittum.core import KittumError, Record, Sha256, content_hash
from kittum.protocol.model import ReviewProtocol, ScreeningMode, SearchTopic, Source
from kittum.protocol.profile import ExtensionPoint, Profile, registered_profiles

HASH_DOMAIN = "kittum.protocol.v1"

_IDENTIFIER_PATTERNS = {
    "doi": re.compile(r"^10\.\d{4,9}/\S+$"),
    "pmid": re.compile(r"^[1-9]\d{0,8}$"),
    "nct": re.compile(r"^NCT\d{8}$"),
    "isrctn": re.compile(r"^ISRCTN\d{8}$"),
    "eudract": re.compile(r"^\d{4}-\d{6}-\d{2}$"),
    "euct": re.compile(r"^\d{4}-\d{6}-\d{2}-\d{2}$"),
}

_LOCALIZED_KEYS = frozenset(
    {"title", "label", "population", "intervention", "text", "name", "definition", "rationale"}
    | {"justification", "mode_rationale"}
)


class ProtocolIssue(Record):
    path: str
    message: str


class ProtocolError(KittumError):
    """A protocol source that does not compile. `issues` lists every problem found."""

    def __init__(self, issues: list[ProtocolIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.path or '<root>'}: {i.message}" for i in issues))


class ProtocolVersion(Record):
    protocol: ReviewProtocol
    content_hash: Sha256


def protocol_hash(protocol: ReviewProtocol) -> str:
    return content_hash(protocol, domain=HASH_DOMAIN)


def compile(
    source: Mapping[str, Any] | str, *, profiles: Mapping[str, Profile] | None = None
) -> ProtocolVersion:
    """Compile a mapping, or YAML or JSON text, into a `ProtocolVersion`.

    `profiles` defaults to the registered profiles. Raises `ProtocolError` with every problem.
    """
    if isinstance(source, str):
        source = _load(source)
    if not isinstance(source, Mapping):
        raise ProtocolError([ProtocolIssue(path="", message="a protocol must be a mapping")])
    profiles = registered_profiles() if profiles is None else profiles
    raw = dict(source)
    issues: list[ProtocolIssue] = []

    profile: Profile | None = None
    profile_id = raw.get("profile")
    if isinstance(profile_id, str):
        profile = profiles.get(profile_id)
        if profile is None:
            known = ", ".join(sorted(profiles)) or "none"
            issues.append(
                ProtocolIssue(
                    path="profile",
                    message=f"unknown profile {profile_id!r}; registered: {known}",
                )
            )
        elif "effect_modifiers" not in raw and profile.default_effect_modifiers:
            raw["effect_modifiers"] = [
                m.model_dump(mode="json") for m in profile.default_effect_modifiers
            ]

    _resolve_topic_scopes(raw)
    try:
        protocol = ReviewProtocol.model_validate(raw)
    except ValidationError as e:
        issues += _issues_from_pydantic(e)
        raise ProtocolError(issues) from None
    issues += _cross_check(protocol)
    if profile_id is not None and profile is None:
        raise ProtocolError(issues)

    protocol, extension_issues = _validate_extensions(protocol, profile)
    issues += extension_issues
    if profile is not None:
        issues += _check_vocabularies(protocol, profile)
        if not extension_issues:  # rules may rely on valid extensions, nothing else
            for rule in profile.rules:
                issues += rule(protocol)
    if issues:
        raise ProtocolError(issues)
    return ProtocolVersion(protocol=protocol, content_hash=protocol_hash(protocol))


# Parsing


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,  # ty: ignore[invalid-argument-type]
)


def _load(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown position"
        problem = e.problem or e.context or "invalid YAML"
        raise ProtocolError([ProtocolIssue(path="", message=f"{problem} at {where}")]) from None
    except yaml.YAMLError as e:
        raise ProtocolError([ProtocolIssue(path="", message=str(e))]) from None


# Paths


def path_of(*parts: str | int) -> str:
    """`path_of("search_topics", 0, "mode")` is "search_topics[0].mode"."""
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def _issues_from_pydantic(
    error: ValidationError, prefix: tuple[str | int, ...] = ()
) -> list[ProtocolIssue]:
    issues = []
    errors = error.errors()
    nested = [tuple(e["loc"]) for e in errors]
    for err in errors:
        here = tuple(err["loc"])
        if err["type"] == "too_short" and any(
            len(loc) > len(here) and loc[: len(here)] == here for loc in nested
        ):
            continue  # a container is "too short" only because an item inside it failed
        loc = [p for p in err["loc"] if not (isinstance(p, str) and p.startswith("function-"))]
        message = err["msg"]
        if err["type"] not in {"missing", "extra_forbidden"} and "input" in err:
            shown = repr(err["input"])
            message += f", got {shown if len(shown) <= 60 else shown[:57] + '...'}"
        if err["type"] == "extra_forbidden":
            message = "unknown field"
        issues.append(ProtocolIssue(path=path_of(*prefix, *loc), message=message))
    return issues


# Scope resolution


def _sequence(value: Any) -> list[Any] | tuple[Any, ...] | None:
    return value if isinstance(value, list | tuple) else None


def _resolve_topic_scopes(raw: dict[str, Any]) -> None:
    """Let authors name a topic's scopes by question ID: expands each ID into a `Scope` object.

    This is a convenience only. Malformed input is left for schema validation to report, and
    `_check_topic` checks every scope against its question after parsing, whatever the source
    looked like.
    """
    lang = raw.get("default_language")
    labels: dict[str, str] = {}
    for question in _sequence(raw.get("questions")) or ():
        if isinstance(question, Mapping) and isinstance(question.get("id"), str):
            label = question.get("label")
            text = label.get(lang) if isinstance(label, Mapping) and isinstance(lang, str) else None
            labels[question["id"]] = text.strip() if isinstance(text, str) else ""
    topics = _sequence(raw.get("search_topics"))
    if topics is None:
        return
    new_topics: list[Any] = []
    for topic in topics:
        scopes = _sequence(topic.get("scopes")) if isinstance(topic, Mapping) else None
        if scopes is not None:
            topic = {
                **topic,
                "scopes": [
                    {"id": s, "label": labels.get(s, ""), "kind": "question"}
                    if isinstance(s, str)
                    else s
                    for s in scopes
                ],
            }
        new_topics.append(topic)
    raw["search_topics"] = new_topics


# Core cross-checks


def _duplicates(ids: list[tuple[str, str]], what: str) -> list[ProtocolIssue]:
    """`ids` holds (path, id) pairs. Reports every repeat after the first."""
    counts = Counter(i for _, i in ids)
    first: dict[str, str] = {}
    issues: list[ProtocolIssue] = []
    for path, i in ids:
        if counts[i] > 1:
            if i in first:
                issues.append(
                    ProtocolIssue(path=path, message=f"duplicate {what} {i!r}, first at {first[i]}")
                )
            else:
                first[i] = path
    return issues


def _localized(node: Any, path: tuple[str | int, ...]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every localized text in a dumped protocol, outside extensions."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "extensions":
                continue
            if key in _LOCALIZED_KEYS and isinstance(value, dict):
                yield path_of(*path, key), value
            else:
                yield from _localized(value, (*path, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _localized(value, (*path, i))


def _cross_check(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    lang = protocol.default_language
    for path, text in _localized(protocol.model_dump(mode="json"), ()):
        if lang not in text:
            issues.append(
                ProtocolIssue(path=path, message=f"needs text in the default language {lang!r}")
            )

    outcome_ids = {o.id for o in protocol.outcomes}
    issues += _duplicates(
        [(path_of("outcomes", i, "id"), o.id) for i, o in enumerate(protocol.outcomes)],
        "outcome ID",
    )
    issues += _duplicates(
        [
            (path_of("effect_modifiers", i, "id"), m.id)
            for i, m in enumerate(protocol.effect_modifiers)
        ],
        "effect modifier ID",
    )
    for qi, question in enumerate(protocol.questions):
        issues += _duplicates(
            [
                (path_of("questions", qi, "comparators", ci, "id"), c.id)
                for ci, c in enumerate(question.comparators)
            ],
            "comparator ID",
        )
        for oi, outcome in enumerate(question.outcomes):
            if outcome not in outcome_ids:
                issues.append(
                    ProtocolIssue(
                        path=path_of("questions", qi, "outcomes", oi),
                        message=f"unknown outcome {outcome!r}",
                    )
                )

    scope_ids = [(path_of("questions", i, "id"), q.id) for i, q in enumerate(protocol.questions)]
    scope_ids += [
        (path_of("search_topics", i, "id"), t.id) for i, t in enumerate(protocol.search_topics)
    ]
    issues += _duplicates(scope_ids, "scope ID")
    questions = {q.id: q.label.get(lang, "") for q in protocol.questions}
    criterion_ids: list[tuple[str, str]] = []
    covered: set[str] = set()
    for ti, topic in enumerate(protocol.search_topics):
        here = ("search_topics", ti)
        covered |= {s.id for s in topic.scopes}
        criterion_ids += [
            (path_of(*here, "criteria", ci, "id"), c.id) for ci, c in enumerate(topic.criteria)
        ]
        issues += _check_topic(topic, here, questions)
    issues += _duplicates(criterion_ids, "criterion ID")
    for qi, question in enumerate(protocol.questions):
        if question.id not in covered:
            issues.append(
                ProtocolIssue(
                    path=path_of("questions", qi),
                    message=f"question {question.id!r} is in no search topic's scopes, so no "
                    "search would find its evidence",
                )
            )
    return issues


def _check_topic(
    topic: SearchTopic, here: tuple[str | int, ...], questions: dict[str, str]
) -> list[ProtocolIssue]:
    """`questions` maps each question ID to its label in the default language."""
    issues = _duplicates(
        [(path_of(*here, "scopes", si), s.id) for si, s in enumerate(topic.scopes)], "scope"
    )
    for si, scope in enumerate(topic.scopes):
        path = path_of(*here, "scopes", si)
        if scope.id not in questions:
            known = ", ".join(questions) or "none"
            message = f"unknown scope {scope.id!r}; a topic covers review questions ({known})"
            issues.append(ProtocolIssue(path=path, message=message))
        elif scope.kind != "question" or scope.label != questions[scope.id]:
            message = f"scope {scope.id!r} does not match its question"
            issues.append(ProtocolIssue(path=path, message=message))
    valid_targets = {s.id for s in topic.scopes} | {topic.id}
    screenable: set[str] = set()
    for ci, criterion in enumerate(topic.criteria):
        at = (*here, "criteria", ci)
        if len(set(criterion.decidable_at)) != len(criterion.decidable_at):
            issues.append(
                ProtocolIssue(path=path_of(*at, "decidable_at"), message="repeated stage")
            )
        for ri, target in enumerate(criterion.applies_to):
            if target not in valid_targets:
                known = ", ".join(sorted(valid_targets))
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "applies_to", ri),
                        message=f"unknown scope {target!r}; criterion {criterion.id!r} can apply "
                        f"to its topic or the topic's scopes ({known})",
                    )
                )
        if "title_abstract" in criterion.decidable_at:
            targets = set(criterion.applies_to)
            screenable |= {s.id for s in topic.scopes} if topic.id in targets else targets
    if not screenable:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "criteria"),
                message=f"topic {topic.id!r} has no criterion decidable at title_abstract",
            )
        )
    else:
        for si, scope in enumerate(topic.scopes):
            if scope.id not in screenable:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*here, "scopes", si),
                        message=f"scope {scope.id!r} has no criterion decidable at title_abstract",
                    )
                )
    issues += _check_mode(topic, here)
    issues += _check_sources(topic, here)
    issues += _check_reference_set(topic, here)
    return issues


def _check_mode(topic: SearchTopic, here: tuple[str | int, ...]) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    if topic.mode != ScreeningMode.M1 and topic.mode_rationale is None:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "mode_rationale"),
                message=f"mode {topic.mode} needs a rationale: a selection not done by two "
                "people independently must be justified",
            )
        )
    if topic.mode == ScreeningMode.M2 and topic.validation_pack is None:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "validation_pack"),
                message="mode M2 needs a validation pack that shows the AI's performance",
            )
        )
    if topic.mode == ScreeningMode.M3 and topic.stopping is None:
        issues.append(
            ProtocolIssue(path=path_of(*here, "stopping"), message="mode M3 needs a stopping plan")
        )
    if topic.mode != ScreeningMode.M3 and topic.stopping is not None:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "stopping"),
                message=f"mode {topic.mode} screens every record; only M3 may have a stopping plan",
            )
        )
    # DESIGN.md, "Screening modes" and "Statistics": M1 adjudicates records the AI includes and
    # both humans exclude; M2 and M3 audit AI exclusions with a census and a random sample.
    if not topic.audit.census_of_disagreements:
        why = (
            "records the AI includes and both humans exclude go to adjudication"
            if topic.mode == ScreeningMode.M1
            else "every human-AI disagreement goes into the audit census"
        )
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "audit", "census_of_disagreements"),
                message=f"mode {topic.mode} needs census_of_disagreements: {why}",
            )
        )
    if topic.mode != ScreeningMode.M1 and Decimal(topic.audit.random_audit_percent) == 0:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "audit", "random_audit_percent"),
                message=f"mode {topic.mode} needs a random audit of exclusions above 0 percent",
            )
        )
    return issues


def _check_sources(topic: SearchTopic, here: tuple[str | int, ...]) -> list[ProtocolIssue]:
    plan = topic.source_plan
    at = (*here, "source_plan")
    issues = _duplicates(
        [(path_of(*at, "sources", si, "id"), s.id) for si, s in enumerate(plan.sources)],
        "source ID",
    )
    for si, source in enumerate(plan.sources):
        if source.strategy is not None:
            path = path_of(*at, "sources", si, "strategy")
            issues += [
                ProtocolIssue(path=path, message=m) for m in strategy_problems(source.strategy)
            ]
    present = {s.id for s in plan.sources}
    for di, deviation in enumerate(plan.deviations):
        if deviation.source_id in present:
            issues.append(
                ProtocolIssue(
                    path=path_of(*at, "deviations", di, "source_id"),
                    message=f"{deviation.source_id!r} is in the plan; a deviation is for a source "
                    "that is left out",
                )
            )
    return issues


type StrategyDialect = Literal["pubmed", "europepmc", "ctgov"]


def strategy_dialect(source: Source) -> StrategyDialect | None:
    """The live interface whose syntax a source's strategy is written in, when Kittum knows it:
    PubMed (MEDLINE searched on PubMed), Europe PMC's search syntax, or ClinicalTrials.gov's Essie.
    Other platforms (Ovid, Cochrane Library) have their own syntax, which Kittum does not check or
    run."""
    platform = (source.platform or "").casefold()
    if platform == "pubmed" or source.id.casefold() == "pubmed":
        return "pubmed"
    names = {source.id.casefold(), platform, source.name.casefold()}
    if names & {"europepmc", "europe_pmc", "europe pmc"}:
        return "europepmc"
    if names & {"clinicaltrials_gov", "ctgov", "clinicaltrials.gov"}:
        return "ctgov"
    return None


# PubMed's search field tags, short and long ("Search Field Descriptions and Tags",
# https://pubmed.ncbi.nlm.nih.gov/help/#search-tags), each confirmed against ESearch, which reports
# a tag it does not know as FieldNotFound. Proximity searches add `:~N` to a title, abstract, or
# affiliation field.
PUBMED_FIELD_TAGS = frozenset(
    {
        "ad",
        "affl",
        "all",
        "aid",
        "au",
        "auid",
        "book",
        "cn",
        "cois",
        "crdt",
        "dcom",
        "dp",
        "ed",
        "edat",
        "epdat",
        "ppdat",
        "pdat",
        "fau",
        "1au",
        "lastau",
        "fir",
        "filter",
        "sb",
        "gr",
        "ir",
        "isbn",
        "ip",
        "ta",
        "jour",
        "jid",
        "la",
        "lang",
        "lid",
        "lr",
        "majr",
        "mh",
        "mesh",
        "mhda",
        "nm",
        "ot",
        "pa",
        "pg",
        "pl",
        "pmid",
        "uid",
        "ps",
        "pt",
        "ptyp",
        "pubn",
        "rn",
        "si",
        "sh",
        "so",
        "ti",
        "tiab",
        "tt",
        "tw",
        "vi",
        "mh:noexp",
        "majr:noexp",
        "sh:noexp",
        "mesh:noexp",
        "pt:noexp",
        "ptyp:noexp",
        "subheading",
        "subheading:noexp",
        "is",
        "subs",
        "affiliation",
        "all fields",
        "author",
        "author - corporate",
        "author - first",
        "author - full",
        "author - identifier",
        "author - last",
        "conflict of interest statements",
        "corporate author",
        "date - completion",
        "date - create",
        "date - entry",
        "date - mesh",
        "date - modification",
        "date - publication",
        "ec/rn number",
        "editor",
        "first author name",
        "full author name",
        "full investigator name",
        "grants and funding",
        "grant number",
        "investigator",
        "investigator - full",
        "issue",
        "journal",
        "language",
        "last author name",
        "location id",
        "mesh major topic",
        "mesh subheading",
        "mesh terms",
        "mesh terms:noexp",
        "mesh major topic:noexp",
        "mesh subheading:noexp",
        "publication type:noexp",
        "other term",
        "pagination",
        "personal name as subject",
        "pharmacological action",
        "place of publication",
        "publication type",
        "publisher",
        "secondary source id",
        "subset",
        "supplementary concept",
        "substance name",
        "text word",
        "title",
        "title/abstract",
        "transliterated title",
        "volume",
        "publication date",
        "entry date",
        "create date",
        "completion date",
        "modification date",
        "mesh date",
        "electronic publication date",
        "print publication date",
    }
)

# Europe PMC's search fields, as its `fields` endpoint lists them
# (https://www.ebi.ac.uk/europepmc/webservices/rest/fields), and MESH and SUBSET, which it accepts
# without listing. Europe PMC finds nothing for a field it does not know, without an error.
EUROPEPMC_FIELDS = frozenset(
    {
        "abbr", "abstract", "accession_id", "accession_type", "ack_fund", "aff", "all_fields",
        "annotation_provider", "annotation_type", "appendix", "arxpr_pubs", "auth", "authorid",
        "authorid_type", "author_roles", "auth_collective_list", "auth_con", "auth_first", "auth_last",
        "auth_man", "auth_man_id", "back", "back_noref", "body", "book_id", "case", "chebiterm",
        "chebiterm_id", "chebi_pubs", "chem", "chembl_pubs", "cited", "cites", "comp_int", "concl",
        "creation_date", "data_availability", "discuss", "disease", "disease_id", "doi", "ed",
        "embargo_date", "embl_pubs", "embl_ror_id", "epmc_auth_man", "essn", "experimental_method",
        "experimental_method_id", "ext_id", "e_pdate", "fig", "first_idate", "first_idate_d",
        "first_pdate", "ft_cdate", "ft_cdate_d", "ft_id", "funder_initiative", "gene_protein",
        "goterm", "goterm_id", "grant_agency", "grant_agency_id", "grant_id", "has_abstract",
        "has_arxpr", "has_book", "has_chebi", "has_chembl", "has_data", "has_doi", "has_embl",
        "has_free_fulltext", "has_ft", "has_fulltext", "has_fulltextdata", "has_intact",
        "has_interpro", "has_labslinks", "has_omim", "has_pdb", "has_pdf", "has_preprint", "has_pride",
        "has_published_version", "has_reflist", "has_suppl", "has_tm", "has_uniprot",
        "has_version_evaluations", "has_xrefs", "index_date", "intact_pubs", "interpro_pubs", "intro",
        "investigator", "in_epmc", "in_pmc", "isbn", "issn", "issue", "journal", "keyword", "kw",
        "labs_pubs", "lang", "license", "methods", "nih_auth_man", "omim_pubs", "open_access",
        "organism", "organism_id", "org_id", "other", "parent_title", "pdb_pubs", "pmcid",
        "pride_pubs", "publisher", "pub_type", "pub_year", "p_pdate", "qn1", "qn2", "ref", "reffed_by",
        "resource_name", "results", "series_name", "shard", "src", "suppl", "table", "title",
        "title_abs", "uniprot_pubs", "update_date", "volume", "mesh", "subset"
    }
)  # fmt: skip
_TAG = re.compile(r"\[([^\]]*)\]")
_FIELD = re.compile(r"(?<![\w-])([A-Za-z_][A-Za-z0-9_]*):")
_PROXIMITY = re.compile(r"^(tiab|ti|ad|title/abstract|title|affiliation):~\d+$")


def _unquoted(strategy: str) -> list[str]:
    """The parts of a strategy outside its quoted phrases."""
    return [part for i, part in enumerate(strategy.split('"')) if not i % 2]


def strategy_problems(strategy: str) -> list[str]:
    """What is wrong with a strategy's syntax, as far as a check without running it can tell:
    quotes, parentheses, and square brackets must balance. Whether the query finds what it should
    is for a hit count and the reference set to show."""
    problems: list[str] = []
    if strategy.count('"') % 2:
        problems.append("a double quote is not closed")
    depth = 0
    for part in _unquoted(strategy):
        for char in part:
            depth += {"(": 1, ")": -1}.get(char, 0)
            if depth < 0:
                problems.append("a closing parenthesis has no opening one")
                depth = 0
    if depth > 0:
        problems.append(
            f"{depth} opening parenthes{'is is' if depth == 1 else 'es are'} not closed"
        )
    text = " ".join(_unquoted(strategy))
    if text.count("[") != text.count("]"):
        problems.append("square brackets do not balance")
    return problems


def strategy_warnings(strategy: str, dialect: StrategyDialect | None) -> list[str]:
    """What may be wrong with a strategy that compiles: a field PubMed or Europe PMC does not
    list. PubMed searches such a term anyway, with a warning, and Europe PMC finds nothing for it,
    so the protocol warns and compiles."""
    if dialect == "europepmc":
        return [
            f"Europe PMC has no field {field}. It finds no records for a search in it."
            for field in dict.fromkeys(_FIELD.findall(" ".join(_unquoted(strategy))))
            if field.casefold() not in EUROPEPMC_FIELDS
        ]
    if dialect != "pubmed":
        return []
    unknown = []
    for tag in _TAG.findall(" ".join(_unquoted(strategy))):
        name = " ".join(tag.casefold().split())
        if name not in PUBMED_FIELD_TAGS and not _PROXIMITY.match(name):
            unknown.append(f"[{tag}] is not a PubMed field tag; PubMed would search it as text")
    return unknown


def protocol_warnings(protocol: ReviewProtocol) -> tuple[ProtocolIssue, ...]:
    """What may be wrong in a protocol that compiles, each with its path: for now, fields in a
    PubMed or Europe PMC strategy that the platform does not list."""
    return tuple(
        ProtocolIssue(
            path=path_of("search_topics", ti, "source_plan", "sources", si, "strategy"),
            message=message,
        )
        for ti, topic in enumerate(protocol.search_topics)
        for si, source in enumerate(topic.source_plan.sources)
        if source.strategy is not None
        for message in strategy_warnings(source.strategy, strategy_dialect(source))
    )


def _check_reference_set(topic: SearchTopic, here: tuple[str | int, ...]) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    at = (*here, "reference_set")
    seen: dict[tuple[str, str], str] = {}
    for part in ("development", "validation"):
        for ii, item in enumerate(getattr(topic.reference_set, part)):
            path = path_of(*at, part, ii, "value")
            if not _IDENTIFIER_PATTERNS[item.kind].match(item.value):
                issues.append(
                    ProtocolIssue(path=path, message=f"{item.value!r} is not a valid {item.kind}")
                )
            key = (item.kind, item.value.lower())
            if key in seen:
                issues.append(
                    ProtocolIssue(
                        path=path,
                        message=f"{item.kind} {item.value!r} is already listed at {seen[key]}; "
                        "development and validation sets must not overlap",
                    )
                )
            else:
                seen[key] = path
    return issues


# Profiles


def _extension_points(
    data: dict[str, Any],
) -> Iterator[tuple[ExtensionPoint, tuple[str | int, ...], dict[str, Any]]]:
    yield "protocol", (), data
    for qi, question in enumerate(data["questions"]):
        yield "question", ("questions", qi), question
        for ci, comparator in enumerate(question["comparators"]):
            yield "comparator", ("questions", qi, "comparators", ci), comparator
    for oi, outcome in enumerate(data["outcomes"]):
        yield "outcome", ("outcomes", oi), outcome
    for ti, topic in enumerate(data["search_topics"]):
        yield "search_topic", ("search_topics", ti), topic
        for ci, criterion in enumerate(topic["criteria"]):
            yield "criterion", ("search_topics", ti, "criteria", ci), criterion


def _validate_extensions(
    protocol: ReviewProtocol, profile: Profile | None
) -> tuple[ReviewProtocol, list[ProtocolIssue]]:
    """Validate every `extensions` mapping against the profile and store it normalized."""
    data = protocol.model_dump(mode="json")
    issues: list[ProtocolIssue] = []
    for point, path, node in _extension_points(data):
        extensions = node["extensions"]
        schema: type[BaseModel] | None = profile.extensions.get(point) if profile else None
        if schema is None:
            if extensions:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*path, "extensions"),
                        message="extensions need a profile that defines them"
                        if profile is None
                        else f"profile {profile.id!r} defines no {point} extensions",
                    )
                )
            continue
        try:
            node["extensions"] = schema.model_validate(extensions).model_dump(mode="json")
        except ValidationError as e:
            issues += _issues_from_pydantic(e, (*path, "extensions"))
    if issues:
        return protocol, issues
    return ReviewProtocol.model_validate(data), issues


def _check_vocabularies(protocol: ReviewProtocol, profile: Profile) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    checks = [
        (
            "outcome_category",
            [
                (path_of("outcomes", i, "category"), o.category)
                for i, o in enumerate(protocol.outcomes)
            ],
        ),
        (
            "topic_kind",
            [
                (path_of("search_topics", i, "kind"), t.kind)
                for i, t in enumerate(protocol.search_topics)
            ],
        ),
    ]
    for vocabulary, values in checks:
        terms = profile.vocabularies.get(vocabulary)
        if terms is None:
            continue
        for path, value in values:
            if value not in terms:
                issues.append(
                    ProtocolIssue(
                        path=path,
                        message=f"{value!r} is not a {vocabulary} of profile {profile.id!r} "
                        f"({', '.join(terms)})",
                    )
                )
    return issues
