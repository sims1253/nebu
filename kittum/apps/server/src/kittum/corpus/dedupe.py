"""Deduplication and proposal generation rules for corpus (WP-07).

Rules:
1. Record deduplication (same_record and same_report) uses report identifiers (DOI, PMID)
   and normalized title plus year plus first author (via rapidfuzz).
   Trial identifiers (NCT, EudraCT, CTIS numbers) must NEVER merge records or reports.
2. Trial identifiers drive only `same_study` proposals.
"""

import re
import unicodedata
from collections import defaultdict
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from kittum.core import Id, new_id
from kittum.corpus.models import LinkProposal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kittum.search import SourceRecord

_ROMAN_TO_NUM = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if d.startswith(prefix):
            d = d[len(prefix) :]
    d = d.strip()
    return d or None


def normalize_pmid(pmid: str | None) -> str | None:
    if not pmid:
        return None
    p = re.sub(r"[^\d]", "", pmid.strip())
    return p or None


def normalize_registry_id(reg: str) -> str:
    return reg.strip().upper()


def extract_first_author_family(authors: tuple[str, ...]) -> str | None:
    if not authors:
        return None
    first = authors[0].strip()
    if not first:
        return None
    if "," in first:
        family = first.split(",")[0].strip()
    else:
        parts = first.split()
        if len(parts) > 1 and len(parts[-1]) <= 2:
            family = parts[0]
        elif len(parts) > 1 and len(parts[0]) <= 2:
            family = parts[-1]
        else:
            family = parts[0]
    clean = re.sub(r"[^\w]", "", family).lower()
    return clean or None


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFC", title).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Typed tokens that tell trials and reports apart. Two titles conflict only when both have
# tokens of one kind and the values differ; a token on one side only (a citation's volume, page,
# or year, say) never blocks a match, and other numbers are ignored.
_RANGE_TAIL = r"(?:\s*[-\u2013/]\s*(?P<b>\d+))?"
_PHASE = re.compile(
    r"\bphase\s*(?P<a>iv|v|i{1,3}|\d+)(?:\s*[-\u2013/]\s*(?P<b>iv|v|i{1,3}|\d+))?\b", re.I
)
_FOLLOW_UP = (
    re.compile(rf"\b(?P<a>\d+){_RANGE_TAIL}[\s-]*(?P<unit>year|yr|month|week|day)s?\b", re.I),
    re.compile(rf"\b(?P<unit>year|month|week|day)s?\s+(?P<a>\d+){_RANGE_TAIL}\b", re.I),
)
_STUDY = re.compile(rf"\b(?:study|trial)\s*(?:no\.?|number|#)?\s*(?P<a>\d+){_RANGE_TAIL}\b", re.I)
# A trial name is a word with two capitals right before a number (KEYNOTE 024, IMpower150).
# Once either title writes it that way, the word counts as a trial name in any case.
_TRIAL_NAME = re.compile(r"\b([A-Za-z]*[A-Z][A-Za-z]*[A-Z][A-Za-z]*)[\s_-]?\d")
_NAMED_NUMBER = re.compile(r"\b(?P<w>[A-Za-z]+)[\s_-]?(?P<n>\d+)")
_KINDS = ("trial", "phase", "follow_up", "study")


def _number(token: str) -> str:
    token = token.lower()
    return _ROMAN_TO_NUM.get(token) or str(int(token))


def _ends(m: re.Match[str]) -> list[str]:
    return [_number(m[g]) for g in ("a", "b") if m[g]]


def _trial_names(title: str) -> set[str]:
    return {name.lower() for name in _TRIAL_NAME.findall(unicodedata.normalize("NFC", title))}


def extract_distinguishing_tokens(
    title: str, trial_names: Iterable[str] = ()
) -> dict[str, set[str]]:
    """Trial, phase, follow-up, and study tokens of a title, by kind.

    A trial token is a trial name (from `trial_names` or written with two capitals) and the
    digits right after it, so `CheckMate 067(2016)` gives `checkmate 67`. Ranges such as
    `phase 1-2` or `12-24 months` give both ends.
    """
    text = unicodedata.normalize("NFC", title)
    names = set(trial_names) | _trial_names(text)
    tokens: dict[str, set[str]] = {kind: set() for kind in _KINDS}
    for m in _NAMED_NUMBER.finditer(text):
        if m["w"].lower() in names:
            tokens["trial"].add(f"{m['w'].lower()} {_number(m['n'])}")
    for m in _PHASE.finditer(text):
        tokens["phase"].update(_ends(m))
    for pattern in _FOLLOW_UP:
        for m in pattern.finditer(text):
            unit = "year" if m["unit"].lower() == "yr" else m["unit"].lower()
            tokens["follow_up"].update(f"{unit} {n}" for n in _ends(m))
    for m in _STUDY.finditer(text):
        tokens["study"].update(_ends(m))
    return tokens


def distinguishing_tokens_conflict(title_a: str, title_b: str) -> bool:
    """Whether both titles name a trial, phase, follow-up, or study number, and they differ."""
    names = _trial_names(title_a) | _trial_names(title_b)
    tokens_a = extract_distinguishing_tokens(title_a, names)
    tokens_b = extract_distinguishing_tokens(title_b, names)
    return any(
        tokens_a[kind] and tokens_b[kind] and tokens_a[kind] != tokens_b[kind] for kind in _KINDS
    )


def check_same_record(rec_a: SourceRecord, rec_b: SourceRecord) -> tuple[bool, str | None]:
    """Check if two records represent the same bibliographic record across databases.

    Trial identifiers must NEVER merge records or reports.
    """
    doi_a = normalize_doi(rec_a.doi)
    doi_b = normalize_doi(rec_b.doi)
    if doi_a and doi_b:
        if doi_a == doi_b:
            return True, f"exact DOI {doi_a}"
        return False, None

    pmid_a = normalize_pmid(rec_a.pmid)
    pmid_b = normalize_pmid(rec_b.pmid)
    if pmid_a and pmid_b:
        if pmid_a == pmid_b:
            return True, f"exact PMID {pmid_a}"
        return False, None

    if distinguishing_tokens_conflict(rec_a.title, rec_b.title):
        return False, None

    if rec_a.year is not None and rec_b.year is not None and abs(rec_a.year - rec_b.year) > 1:
        return False, None

    auth_a = extract_first_author_family(rec_a.authors)
    auth_b = extract_first_author_family(rec_b.authors)
    if auth_a and auth_b and auth_a != auth_b:
        return False, None

    title_a = normalize_title(rec_a.title)
    title_b = normalize_title(rec_b.title)
    if not title_a or not title_b:
        return False, None

    score = fuzz.token_sort_ratio(title_a, title_b)
    if score >= 90.0:
        has_year_match = (
            rec_a.year is not None and rec_b.year is not None and abs(rec_a.year - rec_b.year) <= 1
        )
        has_author_match = auth_a is not None and auth_b is not None and auth_a == auth_b
        if has_year_match or has_author_match or score >= 98.0:
            evidence_parts = [f"normalized title similarity {score / 100:.2f}"]
            if has_year_match and rec_a.year is not None:
                evidence_parts.append(f"year {rec_a.year}")
            if has_author_match and auth_a is not None:
                evidence_parts.append(f"first author {auth_a}")
            return True, ", ".join(evidence_parts)

    return False, None


class _DisjointSet:
    """Groups of occurrences that never hold two different DOIs or two different PMIDs."""

    def __init__(self) -> None:
        self.parent: dict[Id, Id] = {}
        self.dois: dict[Id, set[str]] = defaultdict(set)
        self.pmids: dict[Id, set[str]] = defaultdict(set)

    def add(self, rec: SourceRecord) -> None:
        root = self.find(rec.occurrence_id)
        if doi := normalize_doi(rec.doi):
            self.dois[root].add(doi)
        if pmid := normalize_pmid(rec.pmid):
            self.pmids[root].add(pmid)

    def find(self, item: Id) -> Id:
        if item not in self.parent:
            self.parent[item] = item
            return item
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a: Id, b: Id) -> bool:
        """Join the groups of `a` and `b` unless the joined group would hold conflicting IDs."""
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            return True
        dois = self.dois[root_a] | self.dois[root_b]
        pmids = self.pmids[root_a] | self.pmids[root_b]
        if len(dois) > 1 or len(pmids) > 1:
            return False
        self.parent[root_b] = root_a
        self.dois[root_a] = dois
        self.pmids[root_a] = pmids
        return True


def generate_link_proposals(records: list[SourceRecord]) -> list[LinkProposal]:
    """Generate link proposals over a set of SourceRecords."""
    proposals: list[LinkProposal] = []

    # 1. same_record proposals (report dedupe)
    dset = _DisjointSet()
    for rec in records:
        dset.add(rec)
    evidence_map: dict[Id, list[str]] = defaultdict(list)

    n = len(records)
    for i in range(n):
        rec_a = records[i]
        for j in range(i + 1, n):
            rec_b = records[j]
            is_match, ev = check_same_record(rec_a, rec_b)
            if is_match and ev and dset.union(rec_a.occurrence_id, rec_b.occurrence_id):
                evidence_map[rec_a.occurrence_id].append(ev)
                evidence_map[rec_b.occurrence_id].append(ev)

    record_groups: dict[Id, list[Id]] = defaultdict(list)
    for rec in records:
        root = dset.find(rec.occurrence_id)
        record_groups[root].append(rec.occurrence_id)

    for group in record_groups.values():
        if len(group) >= 2:
            all_ev: list[str] = []
            for oid in group:
                all_ev.extend(evidence_map.get(oid, []))
            deduped_ev = tuple(sorted(set(all_ev)))
            proposals.append(
                LinkProposal(
                    id=new_id(),
                    occurrence_ids=tuple(sorted(group)),
                    relation="same_record",
                    evidence=deduped_ev,
                )
            )

    # 2. same_study proposals (trial identifiers)
    registry_to_records: dict[str, list[Id]] = defaultdict(list)
    for rec in records:
        seen_regs: set[str] = set()
        for reg in rec.registry_ids:
            norm_reg = normalize_registry_id(reg)
            if norm_reg and norm_reg not in seen_regs:
                seen_regs.add(norm_reg)
                registry_to_records[norm_reg].append(rec.occurrence_id)
        if rec.source == "ctgov" or rec.provider_record_id.upper().startswith("NCT"):
            norm_pr = normalize_registry_id(rec.provider_record_id)
            if norm_pr and norm_pr not in seen_regs:
                seen_regs.add(norm_pr)
                registry_to_records[norm_pr].append(rec.occurrence_id)

    for reg_id, occ_ids in sorted(registry_to_records.items()):
        unique_occ_ids = sorted(set(occ_ids))
        if len(unique_occ_ids) >= 2:
            proposals.append(
                LinkProposal(
                    id=new_id(),
                    occurrence_ids=tuple(unique_occ_ids),
                    relation="same_study",
                    evidence=(f"shared registry ID {reg_id}",),
                )
            )

    return proposals
