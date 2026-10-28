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


def extract_distinguishing_tokens(title: str) -> tuple[set[str], set[str]]:
    """Numbers and acronym-number names (KEYNOTE 024, IMpower150) that tell trials apart.

    Numbers cover trial numbers, phases (roman numerals too), years, and follow-up lengths.
    """
    text = unicodedata.normalize("NFC", title)
    lowered = re.sub(
        r"\bphase\s+(iv|v|i{1,3})\b",
        lambda m: f"phase {_ROMAN_TO_NUM[m.group(1)]}",
        text.lower(),
    )
    numbers = {str(int(n)) for n in re.findall(r"\d+", lowered)}
    acronyms = {
        f"{m.group(1).lower()} {int(m.group(2))}"
        for m in re.finditer(r"\b([A-Za-z]{2,})[\s_-]?(\d+)\b", text)
        if sum(c.isupper() for c in m.group(1)) >= 2
    }
    return numbers, acronyms


def distinguishing_tokens_conflict(title_a: str, title_b: str) -> bool:
    """Whether both titles name numbers or numbered acronyms and they differ."""
    numbers_a, acronyms_a = extract_distinguishing_tokens(title_a)
    numbers_b, acronyms_b = extract_distinguishing_tokens(title_b)
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return True
    return bool(acronyms_a and acronyms_b and acronyms_a != acronyms_b)


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
