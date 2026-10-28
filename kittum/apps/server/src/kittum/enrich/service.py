"""Enrichment with a "first healthy source" policy.

Each field tries its sources in order. A source that errors, is out of budget, or has no record is
skipped, and the next one answers, so one failing provider never blocks a review. The sources that
did answer are recorded per field.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kittum.enrich import crossref, europepmc, openalex, pubtator, unpaywall
from kittum.enrich._http import SourceUnavailable
from kittum.enrich.store import store
from kittum.enrich.types import (
    Enrichment,
    EnrichmentConfig,
    EntityTag,
    FieldSource,
    OaLocation,
    RecordEnrichment,
    SourceFailure,
    WorkUpdate,
)

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Tx
    from kittum.gateway import Gateway
    from kittum.search import SourceRecord


@dataclass
class _State:
    record: SourceRecord
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract: str | None = None
    retracted: bool = False
    retraction_notice_doi: str | None = None
    openalex_retracted: bool = False
    oa_locations: list[OaLocation] = field(default_factory=list)
    entities: list[EntityTag] = field(default_factory=list)
    updates: list[WorkUpdate] = field(default_factory=list)
    clinical_trial_numbers: list[str] = field(default_factory=list)
    field_sources: dict[tuple[str, str], FieldSource] = field(default_factory=dict)

    @classmethod
    def start(cls, record: SourceRecord) -> _State:
        state = cls(
            record=record,
            doi=record.doi.lower() if record.doi else None,
            pmid=record.pmid,
            abstract=record.abstract,
        )
        if record.doi:
            state.note("doi", record.source)
        if record.pmid:
            state.note("pmid", record.source)
        if record.abstract:
            state.note("abstract", record.source)
        return state

    def note(self, name: str, source: str) -> None:
        self.field_sources.setdefault((name, source), FieldSource(field=name, source=source))

    def identity(self, doi: str | None, pmid: str | None, source: str) -> None:
        if doi and not self.doi:
            self.doi = doi
            self.note("doi", source)
        if pmid and not self.pmid:
            self.pmid = pmid
            self.note("pmid", source)

    def add_locations(self, locations: tuple[OaLocation, ...], source: str) -> None:
        if not locations:
            return
        known = {location.url for location in self.oa_locations}
        self.oa_locations.extend(location for location in locations if location.url not in known)
        # The source answered for this field even when its URL duplicates an earlier one.
        self.note("oa_locations", source)

    def add_entities(self, entities: tuple[EntityTag, ...]) -> None:
        known = {(entity.start, entity.end, entity.label) for entity in self.entities}
        added = [
            entity for entity in entities if (entity.start, entity.end, entity.label) not in known
        ]
        if added:
            self.entities.extend(added)
            self.note("entities", pubtator.SOURCE)

    def finish(self) -> RecordEnrichment:
        sources_used = tuple(dict.fromkeys(item.source for item in self.field_sources.values()))
        return RecordEnrichment(
            occurrence_id=self.record.occurrence_id,
            provider_record_id=self.record.provider_record_id,
            doi=self.doi,
            pmid=self.pmid,
            pmcid=self.pmcid,
            abstract=self.abstract,
            retracted=self.retracted,
            retraction_notice_doi=self.retraction_notice_doi,
            oa_locations=tuple(self.oa_locations),
            entities=tuple(self.entities),
            updates=tuple(self.updates),
            clinical_trial_numbers=tuple(self.clinical_trial_numbers),
            field_sources=tuple(self.field_sources.values()),
            sources_used=sources_used,
        )


class _Failures:
    def __init__(self) -> None:
        self.items: list[SourceFailure] = []

    def add(self, error: SourceUnavailable) -> None:
        failure = SourceFailure(source=error.source, reason=error.reason)
        if failure not in self.items:
            self.items.append(failure)


def enrich(
    tx: Tx,
    project_id: Id,
    records: tuple[SourceRecord, ...],
    gateway: Gateway,
    *,
    config: EnrichmentConfig | None = None,
) -> Enrichment:
    """Fill identifiers, abstracts, OA locations, retractions, and entities for `records`.

    Writes one `record_enrichment` row per occurrence in the caller's transaction, replacing an
    earlier row for the same occurrence, and returns the same values.
    """
    if not records:
        return Enrichment(records=())
    config = config or EnrichmentConfig()
    states = [_State.start(record) for record in records]
    failures = _Failures()
    _from_openalex(states, gateway, config, failures)
    _from_europepmc(states, gateway, config, failures)
    _from_crossref(states, gateway, config, failures)
    _from_unpaywall(states, gateway, config, failures)
    _from_pubtator(states, gateway, config, failures)

    enrichment = Enrichment(
        records=tuple(state.finish() for state in states),
        failures=tuple(failures.items),
    )
    store(tx, project_id, enrichment.records)
    return enrichment


def _from_openalex(
    states: list[_State], gateway: Gateway, config: EnrichmentConfig, failures: _Failures
) -> None:
    dois = [state.doi for state in states if state.doi]
    pmids = [state.pmid for state in states if state.pmid]
    if not dois and not pmids:
        return
    try:
        works = openalex.works(
            gateway,
            dois=dois,
            pmids=pmids,
            api_key=config.openalex_api_key,
            batch_size=config.batch_size,
        )
    except SourceUnavailable as error:
        failures.add(error)
        return
    by_doi = {work.doi: work for work in works if work.doi}
    by_pmid = {work.pmid: work for work in works if work.pmid}
    for state in states:
        work = (by_doi.get(state.doi) if state.doi else None) or (
            by_pmid.get(state.pmid) if state.pmid else None
        )
        if work is None:
            continue
        state.identity(work.doi, work.pmid, openalex.SOURCE)
        if not state.abstract and work.abstract:
            state.abstract = work.abstract
            state.note("abstract", openalex.SOURCE)
        state.add_locations(work.oa_locations, openalex.SOURCE)
        state.openalex_retracted = work.is_retracted


def _from_europepmc(
    states: list[_State], gateway: Gateway, config: EnrichmentConfig, failures: _Failures
) -> None:
    dois = [state.doi for state in states if state.doi]
    pmids = [state.pmid for state in states if state.pmid]
    if not dois and not pmids:
        return
    try:
        works = europepmc.works(gateway, dois=dois, pmids=pmids, batch_size=config.batch_size)
    except SourceUnavailable as error:
        failures.add(error)
        return
    by_doi = {work.doi: work for work in works if work.doi}
    by_pmid = {work.pmid: work for work in works if work.pmid}
    for state in states:
        work = (by_doi.get(state.doi) if state.doi else None) or (
            by_pmid.get(state.pmid) if state.pmid else None
        )
        if work is None:
            continue
        state.identity(work.doi, work.pmid, europepmc.SOURCE)
        if work.pmcid and not state.pmcid:
            state.pmcid = work.pmcid
            state.note("pmcid", europepmc.SOURCE)
        if not state.abstract and work.abstract:
            state.abstract = work.abstract
            state.note("abstract", europepmc.SOURCE)
        state.add_locations(work.oa_locations, europepmc.SOURCE)


def _from_crossref(
    states: list[_State], gateway: Gateway, config: EnrichmentConfig, failures: _Failures
) -> None:
    works: dict[str, crossref.Work | None] = {}
    for state in states:
        if not state.doi:
            continue
        if state.doi not in works:
            try:
                works[state.doi] = crossref.work(gateway, state.doi, email=config.contact_email)
            except SourceUnavailable as error:
                failures.add(error)
                works[state.doi] = None
        work = works[state.doi]
        if work is None:
            continue
        if not state.abstract and work.abstract:
            state.abstract = work.abstract
            state.note("abstract", crossref.SOURCE)
        if work.updates:
            state.updates.extend(work.updates)
        if work.clinical_trial_numbers:
            state.clinical_trial_numbers.extend(work.clinical_trial_numbers)
            state.note("clinical_trial_numbers", crossref.SOURCE)
        notice = crossref.retraction_notice(work)
        if notice:
            state.retracted = True
            state.retraction_notice_doi = notice
            state.note("retraction", crossref.SOURCE)

    for state in states:
        if state.openalex_retracted:
            state.retracted = True
            if state.retraction_notice_doi is None:
                state.note("retraction", openalex.SOURCE)


def _from_unpaywall(
    states: list[_State], gateway: Gateway, config: EnrichmentConfig, failures: _Failures
) -> None:
    if not config.contact_email:
        return
    seen: dict[str, OaLocation | None] = {}
    for state in states:
        if not state.doi:
            continue
        if state.doi not in seen:
            try:
                seen[state.doi] = unpaywall.best_oa_location(
                    gateway, state.doi, email=config.contact_email
                )
            except SourceUnavailable as error:
                failures.add(error)
                seen[state.doi] = None
        location = seen[state.doi]
        if location is not None:
            state.add_locations((location,), unpaywall.SOURCE)


def _from_pubtator(
    states: list[_State], gateway: Gateway, config: EnrichmentConfig, failures: _Failures
) -> None:
    pmids = [state.pmid for state in states if state.pmid]
    if not pmids:
        return
    try:
        annotations = pubtator.annotations(gateway, pmids, batch_size=config.batch_size)
    except SourceUnavailable as error:
        failures.add(error)
        return
    for state in states:
        if not state.pmid:
            continue
        entities = _entities(state, annotations.get(state.pmid, ()))
        state.add_entities(entities)


def _entities(state: _State, annotations: tuple[pubtator.Annotation, ...]) -> tuple[EntityTag, ...]:
    """Re-anchor PubTator's passage offsets in Kittum's title-plus-abstract text.

    PubTator's passage text can differ from Kittum's (section markup, spacing, or a different
    abstract source). The annotation's own offset is used when the text there still matches;
    otherwise the annotation maps to the same occurrence number of its text within the passage.
    Either way the mapped span is checked against the annotation text, and one span is used once,
    so repeated mentions stay distinct.
    """
    passages = {"title": state.record.title, "abstract": state.abstract}
    title_length = len(state.record.title)
    ranks: dict[tuple[str, str], int] = {}
    used: dict[tuple[str, str], set[int]] = {}
    found = []
    ordered = sorted(annotations, key=lambda a: (a.passage != "title", a.offset))
    for annotation in ordered:
        passage = passages.get(annotation.passage)
        if passage is None:
            continue
        key = (annotation.passage, annotation.text)
        rank = ranks.get(key, 0)
        spans = used.setdefault(key, set())
        start = _passage_offset(passage, annotation, rank, spans)
        if start is None:
            continue
        ranks[key] = rank + 1
        spans.add(start)
        base = 0 if annotation.passage == "title" else title_length + 1
        found.append(
            EntityTag(
                start=base + start,
                end=base + start + len(annotation.text),
                label=annotation.label,
                normalized_id=annotation.normalized_id,
                tagger=pubtator.SOURCE,
            )
        )
    return tuple(found)


def _passage_offset(
    passage: str, annotation: pubtator.Annotation, rank: int, used: set[int]
) -> int | None:
    """Where one annotation sits in Kittum's version of its passage, or `None` if it is absent."""
    text = annotation.text
    if (
        passage[annotation.offset : annotation.offset + len(text)] == text
        and annotation.offset not in used
    ):
        return annotation.offset
    position = -1
    for _ in range(rank + 1):
        position = passage.find(text, position + 1)
        if position < 0:
            return None
    while position in used:
        position = passage.find(text, position + 1)
        if position < 0:
            return None
    return position
