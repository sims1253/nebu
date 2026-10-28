"""ClinicalTrials.gov API v2 source adapter (DESIGN.md, "Search"; docs/sources.md).

Queries using Essie syntax via `query.term`, paginates through `nextPageToken`,
and maps studies to SourceRecord models.
"""

import json
import re
from typing import TYPE_CHECKING, Any

from kittum.core import Clock, Sha256, SystemClock, new_id, sha256_hex
from kittum.gateway import HttpRequest
from kittum.search.models import SearchExecution, SearchPlan, SourceRecord

if TYPE_CHECKING:
    from kittum.gateway import Gateway
    from kittum.storage import ArtifactStore

CTGOV_BASE = "https://clinicaltrials.gov/api/v2/studies"
YEAR_PATTERN = re.compile(r"\b(19\d\d|20\d\d)\b")


def _parse_study(study: dict[str, Any], raw_hash: Sha256, ordinal: int) -> SourceRecord:
    proto = study.get("protocolSection", {})
    ident = proto.get("identificationModule", {})
    nct_id = ident.get("nctId", "")

    title = ident.get("briefTitle") or ident.get("officialTitle") or ""
    desc = proto.get("descriptionModule", {})
    abstract = desc.get("briefSummary")

    status_mod = proto.get("statusModule", {})
    overall_status = status_mod.get("overallStatus")

    year: int | None = None
    start_date = status_mod.get("startDateStruct", {}).get("date")
    if start_date:
        m = YEAR_PATTERN.search(start_date)
        if m:
            try:
                year = int(m.group(1))
            except ValueError:
                year = None

    sponsors = proto.get("sponsorCollaboratorsModule", {})
    lead_sponsor = sponsors.get("leadSponsor", {}).get("name")
    authors = (lead_sponsor,) if lead_sponsor else ()
    journal = lead_sponsor

    design = proto.get("designModule", {})
    phases = design.get("phases", [])

    pub_types: list[str] = []
    for ph in phases:
        pub_types.append(f"Phase: {ph}" if not ph.startswith("Phase") else ph)
    if overall_status:
        pub_types.append(f"Status: {overall_status}")
    if not pub_types:
        pub_types.append("Clinical Trial")

    registry_ids: list[str] = []
    if nct_id:
        registry_ids.append(nct_id)

    org_id = ident.get("orgStudyIdInfo", {}).get("id")
    if org_id:
        registry_ids.append(org_id)

    for sec in ident.get("secondaryIdInfos", []):
        sid = sec.get("id")
        if sid:
            registry_ids.append(sid)

    unique_reg_ids = tuple(dict.fromkeys(registry_ids))

    return SourceRecord(
        occurrence_id=new_id(),
        source="ctgov",
        provider_record_id=nct_id,
        ordinal=ordinal,
        title=title,
        abstract=abstract,
        authors=authors,
        year=year,
        journal=journal,
        doi=None,
        pmid=None,
        registry_ids=unique_reg_ids,
        publication_types=tuple(pub_types),
        raw_hash=raw_hash,
    )


class CtgovSource:
    """Live API source adapter for ClinicalTrials.gov API v2."""

    name: str = "ctgov"

    def __init__(
        self,
        gateway: Gateway,
        artifacts: ArtifactStore,
        *,
        clock: Clock | None = None,
        page_size: int = 100,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._clock = clock or SystemClock()
        self._page_size = page_size

    def _build_url(self, strategy: str, page_token: str | None = None) -> str:
        parts = [
            f"{CTGOV_BASE}?query.term={strategy}",
            f"pageSize={self._page_size}",
        ]
        if page_token:
            parts.append(f"pageToken={page_token}")
        return "&".join(parts)

    def run(self, plan: SearchPlan) -> SearchExecution:
        started_at = self._clock.now()
        exec_id = new_id()
        raw_hashes: list[Sha256] = []
        all_records: list[SourceRecord] = []

        next_token: str | None = None
        ordinal = 0
        reported_count = 0

        while True:
            url = self._build_url(plan.strategy, next_token)
            req = HttpRequest(url=url)
            resp = self._gateway.fetch("ctgov", req)
            body = resp.body
            body_hash = sha256_hex(body)
            self._artifacts.put(body)
            raw_hashes.append(body_hash)

            data = json.loads(body)
            total_count = data.get("totalCount")
            if total_count is not None and reported_count == 0:
                reported_count = int(total_count)

            studies = data.get("studies", [])
            for study in studies:
                rec = _parse_study(study, body_hash, ordinal)
                all_records.append(rec)
                ordinal += 1

            next_token = data.get("nextPageToken")
            if not next_token:
                break

        if reported_count == 0:
            reported_count = len(all_records)

        ended_at = self._clock.now()

        return SearchExecution(
            id=exec_id,
            plan=plan,
            kind="live_api",
            started_at=started_at,
            ended_at=ended_at,
            query_translation=None,
            reported_count=reported_count,
            retrieved_count=len(all_records),
            raw_artifact_hashes=tuple(raw_hashes),
            records=tuple(all_records),
        )
