"""Tests for WP-07 (corpus): deduplication, link proposals, identity revisions, and PRISMA counts."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psycopg
import pytest

from kittum.core import Id, NotFoundError, new_id
from kittum.corpus import (
    IdentityRevision,
    Relation,
    SplitLevel,
    confirm,
    get_counts,
    ingest,
    list_proposals,
    merge,
    split,
)
from kittum.corpus.dedupe import (
    check_same_record,
    generate_link_proposals,
)
from kittum.ledger.testing import InMemoryLedger
from kittum.search import SearchExecution, SearchPlan, SourceRecord

if TYPE_CHECKING:
    from kittum.db import Tx

DUMMY_HASH = "0" * 64


def make_record(
    *,
    occurrence_id: Id | None = None,
    source: str = "pubmed",
    provider_record_id: str | None = None,
    ordinal: int = 1,
    title: str = "Test Title",
    authors: tuple[str, ...] = ("Smith J",),
    year: int | None = 2020,
    journal: str | None = "N Engl J Med",
    doi: str | None = None,
    pmid: str | None = None,
    registry_ids: tuple[str, ...] = (),
    publication_types: tuple[str, ...] = (),
) -> SourceRecord:
    oid = occurrence_id or new_id()
    return SourceRecord(
        occurrence_id=oid,
        source=source,
        provider_record_id=provider_record_id or str(oid),
        ordinal=ordinal,
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        doi=doi,
        pmid=pmid,
        registry_ids=registry_ids,
        publication_types=publication_types,
        raw_hash=DUMMY_HASH,
    )


def make_execution(
    source: str,
    records: list[SourceRecord],
    kind: str = "live_api",
) -> SearchExecution:
    now = datetime.now(UTC)
    return SearchExecution(
        id=new_id(),
        plan=SearchPlan(
            topic_id="T1",
            source=source,
            platform=source.capitalize(),
            strategy="pembrolizumab AND nsclc",
        ),
        kind="live_api" if kind == "live_api" else "file_import",
        started_at=now,
        ended_at=now,
        query_translation=None,
        reported_count=len(records),
        retrieved_count=len(records),
        raw_artifact_hashes=(),
        records=tuple(records),
    )


def create_test_project_and_actor(tx: Tx) -> tuple[Id, Id]:
    proj_id = new_id()
    actor_id = new_id()
    with tx.cursor() as cur:
        cur.execute(
            "insert into project (id, name, policy_version) values (%s, %s, %s)",
            (proj_id, "Test Project WP-07", "1.0"),
        )
        cur.execute(
            "insert into actor (id, identity_ref, kind) values (%s, %s, %s)",
            (actor_id, f"actor-{actor_id}@kittum.org", "human"),
        )
    return proj_id, actor_id


def test_dedupe_same_record_exact_doi() -> None:
    rec1 = make_record(doi="10.1056/NEJMoa1606774", title="Pembrolizumab in NSCLC")
    rec2 = make_record(
        doi="https://doi.org/10.1056/nejmoa1606774",
        title="Pembrolizumab in NSCLC (RIS)",
    )
    is_match, ev = check_same_record(rec1, rec2)
    assert is_match
    assert ev == "exact DOI 10.1056/nejmoa1606774"


def test_dedupe_same_record_exact_pmid() -> None:
    rec1 = make_record(pmid="27718847", title="Trial Publication")
    rec2 = make_record(pmid="27718847", title="Trial Publication Other Database")
    is_match, ev = check_same_record(rec1, rec2)
    assert is_match
    assert ev == "exact PMID 27718847"


def test_dedupe_same_record_fuzzy_title_and_author_and_year() -> None:
    # PubMed vs CENTRAL RIS export with slightly different title punctuation and author format
    rec1 = make_record(
        source="pubmed",
        title="Pembrolizumab versus Chemotherapy for PD-L1-Positive Non-Small-Cell Lung Cancer.",
        authors=("Reck M", "Rodríguez-Abreu D"),
        year=2016,
    )
    rec2 = make_record(
        source="centralexport",
        title="Pembrolizumab versus chemotherapy for PD-L1-positive non-small-cell lung cancer",
        authors=("Reck, Martin", "Rodriguez-Abreu, D."),
        year=2016,
    )
    is_match, ev = check_same_record(rec1, rec2)
    assert is_match
    assert ev is not None
    assert "normalized title similarity" in ev
    assert "year 2016" in ev
    assert "first author reck" in ev


def test_near_identical_titles_different_trials_not_merged() -> None:
    """Two different trials with near-identical titles that must NOT be merged."""
    trial_phase2 = make_record(
        title="Efficacy and safety of pembrolizumab in patients with advanced non-small-cell lung cancer: a randomized phase 2 trial",
        authors=("Smith A",),
        year=2017,
        doi="10.1016/j.lungcan.2017.01.001",
        registry_ids=("NCT01111111",),
    )
    trial_phase3 = make_record(
        title="Efficacy and safety of pembrolizumab in patients with advanced non-small-cell lung cancer: a randomized phase 3 trial",
        authors=("Jones C",),
        year=2021,
        doi="10.1016/j.lungcan.2021.05.002",
        registry_ids=("NCT02222222",),
    )
    is_match, _ = check_same_record(trial_phase2, trial_phase3)
    assert not is_match

    proposals = generate_link_proposals([trial_phase2, trial_phase3])
    # No link proposals between them (neither same_record nor same_study)
    assert len(proposals) == 0


def test_trial_identifiers_drive_only_same_study_proposals() -> None:
    """Trial identifiers (NCT numbers) must NEVER merge records into same_record, only same_study."""
    primary = make_record(
        title="Pembrolizumab versus investigator-choice chemotherapy (KEYNOTE-042): a randomised trial",
        authors=("Mok TSK",),
        year=2019,
        doi="10.1016/S0140-6736(18)32409-7",
        registry_ids=("NCT02220894",),
    )
    followup = make_record(
        title="Five-Year Survival Outcomes With Pembrolizumab: KEYNOTE-042",
        authors=("de Castro G",),
        year=2023,
        doi="10.1200/JCO.21.02885",
        registry_ids=("NCT02220894",),
    )
    is_same_rec, _ = check_same_record(primary, followup)
    assert not is_same_rec

    proposals = generate_link_proposals([primary, followup])
    assert len(proposals) == 1
    assert proposals[0].relation == "same_study"
    assert proposals[0].evidence == ("shared registry ID NCT02220894",)
    assert set(proposals[0].occurrence_ids) == {
        primary.occurrence_id,
        followup.occurrence_id,
    }


@pytest.mark.parametrize(
    ("title_a", "title_b"),
    [
        (
            "Pembrolizumab versus chemotherapy in PD-L1 positive NSCLC KEYNOTE 024",
            "Pembrolizumab versus chemotherapy in PD-L1 positive NSCLC KEYNOTE 042",
        ),
        (
            "5-year outcomes of pembrolizumab versus chemotherapy in metastatic NSCLC",
            "3-year outcomes of pembrolizumab versus chemotherapy in metastatic NSCLC",
        ),
        (
            "Pembrolizumab plus chemotherapy in metastatic NSCLC: a randomized phase II trial",
            "Pembrolizumab plus chemotherapy in metastatic NSCLC: a randomized phase 3 trial",
        ),
        (
            "Atezolizumab plus chemotherapy in squamous NSCLC: overall survival of IMpower 131",
            "Atezolizumab plus chemotherapy in squamous NSCLC: overall survival of IMbrave 131",
        ),
    ],
)
def test_titles_naming_different_trials_or_follow_ups_are_not_the_same_record(
    title_a: str, title_b: str
) -> None:
    first = make_record(title=title_a, authors=("Reck M",), year=2019)
    second = make_record(title=title_b, authors=("Reck M",), year=2019)
    assert check_same_record(first, second) == (False, None)
    assert generate_link_proposals([first, second]) == []

    # The same number written differently is still the same record.
    reformatted = make_record(
        title=title_a.replace(" 024", "-024").replace("phase II", "Phase 2"),
        authors=("Reck, Martin",),
        year=2019,
    )
    assert check_same_record(first, reformatted)[0]


def test_a_record_without_identifiers_never_bridges_conflicting_dois_or_pmids() -> None:
    title = "Pembrolizumab versus chemotherapy for PD-L1-positive non-small-cell lung cancer"
    doi_x = make_record(title=title, doi="10.1056/NEJMoa1606774")
    bare = make_record(title=title)
    doi_y = make_record(title=title, doi="10.1200/JCO.2016.34.15_suppl.9000")
    pmid_1 = make_record(title=title, pmid="27718847")
    pmid_2 = make_record(title=title, pmid="30955977", doi="10.1056/NEJMoa1606774")

    proposals = generate_link_proposals([doi_x, bare, doi_y])
    assert [set(p.occurrence_ids) for p in proposals] == [{doi_x.occurrence_id, bare.occurrence_id}]

    # The same DOI does not join two records once their PMIDs disagree.
    proposals = generate_link_proposals([pmid_1, doi_x, pmid_2])
    groups = [set(p.occurrence_ids) for p in proposals]
    assert {pmid_1.occurrence_id, pmid_2.occurrence_id} not in groups
    assert all(not {pmid_1.occurrence_id, pmid_2.occurrence_id} <= g for g in groups)


@pytest.mark.postgres
def test_two_distinct_publications_same_nct_stay_separate_reports_linked_to_one_study(
    pg: Tx,
) -> None:
    """Explicit brief requirement:

    Two distinct publications of one trial with the same NCT number stay
    separate reports, linked to one study.
    """
    proj_id, actor_id = create_test_project_and_actor(pg)

    primary = make_record(
        source="pubmed",
        title="Pembrolizumab versus chemotherapy (KEYNOTE-042): a randomised trial",
        authors=("Mok TSK",),
        year=2019,
        doi="10.1016/S0140-6736(18)32409-7",
        pmid="30955977",
        registry_ids=("NCT02220894",),
    )
    followup = make_record(
        source="pubmed",
        title="Five-Year Survival Outcomes With Pembrolizumab: KEYNOTE-042",
        authors=("de Castro G",),
        year=2023,
        doi="10.1200/JCO.21.02885",
        pmid="36306479",
        registry_ids=("NCT02220894",),
    )

    exec1 = make_execution("pubmed", [primary, followup])
    ledger = InMemoryLedger()
    corpus_rev = ingest(pg, proj_id, [exec1], actor_id=actor_id, ledger=ledger)

    assert len(corpus_rev.proposals) == 1
    proposal = corpus_rev.proposals[0]
    assert proposal.relation == "same_study"

    # Confirm the same_study proposal
    assert corpus_rev.identity_revision_id is not None
    new_rev = confirm(
        pg,
        proj_id,
        corpus_rev.identity_revision_id,
        proposal.id,
        actor_id,
        ledger=ledger,
    )
    assert isinstance(new_rev, IdentityRevision)
    assert new_rev.parent_id == corpus_rev.identity_revision_id

    # Verify links in DB: separate report_ids, same study_id!
    with pg.cursor() as cur:
        cur.execute(
            "select occurrence_id, report_id, study_id, relation from identity_link where revision_id = %s",
            (new_rev.id,),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        rep_ids = {r["report_id"] for r in rows}
        study_ids = {r["study_id"] for r in rows}
        # Two distinct reports!
        assert len(rep_ids) == 2
        # Exactly one study!
        assert len(study_ids) == 1
        for r in rows:
            assert r["relation"] == "same_study"

    # PRISMA counts
    counts = get_counts(pg, proj_id, new_rev.id)
    assert counts.total_records_identified == 2
    assert counts.duplicates_removed == 0
    assert counts.unique_records == 2
    assert counts.studies_identified == 1


def links_of(tx: Tx, revision_id: Id) -> dict[Id, dict[str, object]]:
    with tx.cursor() as cur:
        cur.execute(
            "select occurrence_id, report_id, study_id, relation from identity_link"
            " where revision_id = %s",
            (revision_id,),
        )
        return {row["occurrence_id"]: row for row in cur.fetchall()}


@pytest.mark.postgres
def test_human_merges_and_splits_move_whole_identity_groups(pg: Tx) -> None:
    """Merges join complete report or study groups; a split names the level it separates."""
    proj_id, actor_id = create_test_project_and_actor(pg)
    ledger = InMemoryLedger()
    a, b, c, d, e = (
        make_record(title=title, authors=(author,), year=year)
        for title, author, year in [
            ("Pembrolizumab in NSCLC: primary analysis", "Reck M", 2016),
            ("Pembrolizumab monotherapy in lung cancer (Embase copy)", "Reck M", 2016),
            ("Pembrolizumab: updated overall survival", "Brahmer J", 2019),
            ("Health-related quality of life with pembrolizumab", "Brahmer J", 2017),
            ("Nivolumab in squamous NSCLC", "Brahmer J", 2015),
        ]
    )
    corpus_rev = ingest(
        pg, proj_id, [make_execution("pubmed", [a, b, c, d, e])], actor_id=actor_id, ledger=ledger
    )
    assert corpus_rev.proposals == ()
    assert corpus_rev.identity_revision_id is not None

    def do_merge(parent: Id, occurrences: tuple[Id, ...], relation: Relation) -> Id:
        return merge(pg, proj_id, parent, occurrences, relation, actor_id, ledger=ledger).id

    def do_split(parent: Id, occurrences: tuple[Id, ...], level: SplitLevel) -> Id:
        return split(pg, proj_id, parent, occurrences, level, actor_id, ledger=ledger).id

    def counts(revision_id: Id) -> tuple[int, int]:
        got = get_counts(pg, proj_id, revision_id)
        assert got.total_records_identified == 5  # every occurrence is kept
        return got.unique_records, got.studies_identified

    # A and B are one record; then C joins B's study. A goes along with its report.
    rev1 = do_merge(
        corpus_rev.identity_revision_id, (a.occurrence_id, b.occurrence_id), "same_record"
    )
    rev2 = do_merge(rev1, (c.occurrence_id, b.occurrence_id), "same_study")
    links2 = links_of(pg, rev2)
    assert links2[a.occurrence_id]["report_id"] == links2[b.occurrence_id]["report_id"]
    assert links2[a.occurrence_id]["study_id"] == links2[c.occurrence_id]["study_id"]
    assert links2[c.occurrence_id]["relation"] == "same_study"
    assert counts(rev2) == (4, 3)

    # D is the same record as B, so it joins the whole A-B report and its study.
    rev3 = do_merge(rev2, (d.occurrence_id, b.occurrence_id), "same_record")
    links3 = links_of(pg, rev3)
    assert len({links3[o.occurrence_id]["report_id"] for o in (a, b, d)}) == 1
    assert len({links3[o.occurrence_id]["study_id"] for o in (a, b, c, d)}) == 1
    assert {links3[o.occurrence_id]["relation"] for o in (a, b, d)} == {"same_record"}
    assert counts(rev3) == (3, 2)

    # A study split moves the A-B-D report out of C's study and keeps the report whole.
    rev4 = do_split(rev3, (a.occurrence_id,), "study")
    links4 = links_of(pg, rev4)
    assert len({links4[o.occurrence_id]["report_id"] for o in (a, b, d)}) == 1
    assert links4[a.occurrence_id]["study_id"] != links4[c.occurrence_id]["study_id"]
    assert links4[c.occurrence_id]["study_id"] == links3[c.occurrence_id]["study_id"]
    assert links4[c.occurrence_id]["relation"] == "standalone"
    assert counts(rev4) == (3, 3)

    # A report split takes D out on its own; A and B keep their report and study.
    rev5 = do_split(rev4, (d.occurrence_id,), "report")
    links5 = links_of(pg, rev5)
    for kept in (a, b, c, e):
        oid = kept.occurrence_id
        assert links5[oid]["report_id"] == links4[oid]["report_id"]
        assert links5[oid]["study_id"] == links4[oid]["study_id"]
    assert links5[d.occurrence_id]["report_id"] != links4[d.occurrence_id]["report_id"]
    assert links5[d.occurrence_id]["study_id"] != links4[d.occurrence_id]["study_id"]
    assert counts(rev5) == (4, 4)

    # A merge needs two distinct occurrences of the revision.
    for occurrences in [
        (),
        (a.occurrence_id,),
        (a.occurrence_id, a.occurrence_id),
        (a.occurrence_id, new_id()),
    ]:
        with pytest.raises(ValueError, match=r"at least two|not in this revision"):
            do_merge(rev5, occurrences, "same_record")
    with pytest.raises(ValueError, match="at least one"):
        do_split(rev5, (), "report")

    assert [event.type for event in ledger.events(pg, proj_id)] == [
        "corpus.ingested",
        "corpus.merged",
        "corpus.merged",
        "corpus.merged",
        "corpus.split",
        "corpus.split",
    ]
    assert ledger.events(pg, proj_id)[-1].payload["level"] == "report"


@pytest.mark.postgres
def test_full_corpus_flow_and_prisma_counts_against_postgres(pg: Tx) -> None:
    """Full end-to-end test against Postgres:

    - PubMed + CENTRAL duplicate RCT
    - Primary pub, follow-up, congress abstract, CT.gov entry linked by NCT
    - Two different trials with near-identical titles not merged
    - PRISMA counts verified
    - Human confirmation creates new revision and ledger event
    - Human split undoes wrong merge without losing history.
    """
    proj_id, actor_id = create_test_project_and_actor(pg)
    ledger = InMemoryLedger()

    # 1. Duplicate RCT: PubMed vs CENTRAL
    rct_pubmed = make_record(
        source="pubmed",
        title="Pembrolizumab versus Chemotherapy for PD-L1-Positive Non-Small-Cell Lung Cancer.",
        authors=("Reck M", "Rodríguez-Abreu D"),
        year=2016,
        journal="N Engl J Med",
        doi="10.1056/NEJMoa1606774",
        pmid="27718847",
        registry_ids=("NCT02142738",),
    )
    rct_central = make_record(
        source="central",
        title="Pembrolizumab versus chemotherapy for PD-L1-positive non-small-cell lung cancer",
        authors=("Reck, Martin", "Rodriguez-Abreu, D."),
        year=2016,
        journal="New England Journal of Medicine",
        doi="https://doi.org/10.1056/nejmoa1606774",
        registry_ids=("NCT02142738",),
    )

    # 2. Multi-report trial: KEYNOTE-042 (Lancet, JCO, ASCO abstract, CT.gov)
    t2_primary = make_record(
        source="pubmed",
        title="Pembrolizumab versus investigator-choice chemotherapy (KEYNOTE-042): a randomised trial",
        authors=("Mok TSK",),
        year=2019,
        doi="10.1016/S0140-6736(18)32409-7",
        registry_ids=("NCT02220894",),
    )
    t2_followup = make_record(
        source="pubmed",
        title="Five-Year Survival Outcomes With Pembrolizumab: KEYNOTE-042",
        authors=("de Castro G",),
        year=2023,
        doi="10.1200/JCO.21.02885",
        registry_ids=("NCT02220894",),
    )
    t2_abstract = make_record(
        source="central",
        title="KEYNOTE-042: Phase 3 study of pembrolizumab vs chemotherapy (Abstract LBA4)",
        authors=("Lopes G",),
        year=2018,
        doi="10.1200/JCO.2018.36.18_suppl.LBA4",
        registry_ids=("NCT02220894",),
        publication_types=("conference_abstract",),
    )
    t2_ctgov = make_record(
        source="ctgov",
        title="Study of Pembrolizumab (MK-3475) in Participants With NSCLC (KEYNOTE-042)",
        provider_record_id="NCT02220894",
        registry_ids=("NCT02220894",),
        publication_types=("registry_entry",),
    )

    # 3. Two different trials with near-identical titles
    t3_p2 = make_record(
        source="pubmed",
        title="Efficacy and safety of pembrolizumab in patients with advanced NSCLC: a randomized phase 2 trial",
        authors=("Smith A",),
        year=2017,
        doi="10.1016/j.lungcan.2017.01.001",
        registry_ids=("NCT01111111",),
    )
    t4_p3 = make_record(
        source="pubmed",
        title="Efficacy and safety of pembrolizumab in patients with advanced NSCLC: a randomized phase 3 trial",
        authors=("Jones C",),
        year=2021,
        doi="10.1016/j.lungcan.2021.05.002",
        registry_ids=("NCT02222222",),
    )

    exec_pubmed = make_execution("pubmed", [rct_pubmed, t2_primary, t2_followup, t3_p2, t4_p3])
    exec_central = make_execution("central", [rct_central, t2_abstract])
    exec_ctgov = make_execution("ctgov", [t2_ctgov])

    # Ingest
    corpus_rev = ingest(
        pg, proj_id, [exec_pubmed, exec_central, exec_ctgov], actor_id=actor_id, ledger=ledger
    )
    assert len(corpus_rev.occurrences) == 8
    assert corpus_rev.identity_revision_id is not None

    # Baseline counts before confirmation
    counts_base = get_counts(pg, proj_id, corpus_rev.identity_revision_id)
    assert counts_base.total_records_identified == 8
    assert counts_base.records_identified_per_source == {
        "pubmed": 5,
        "central": 2,
        "ctgov": 1,
    }
    assert counts_base.duplicates_removed == 0
    assert counts_base.unique_records == 8
    assert counts_base.studies_identified == 8

    # List proposals
    proposals = list_proposals(pg, proj_id)
    # Proposals should include:
    # - 1 same_record proposal for RCT (PubMed + CENTRAL)
    # - 1 same_study proposal for KEYNOTE-042 (4 occurrences)
    # - 1 same_study proposal for NCT02142738 (the 2 occurrences of duplicate RCT)
    same_rec_props = [p for p in proposals if p.relation == "same_record"]
    assert len(same_rec_props) == 1
    rct_prop = same_rec_props[0]
    assert set(rct_prop.occurrence_ids) == {
        rct_pubmed.occurrence_id,
        rct_central.occurrence_id,
    }

    keynote_study_props = [
        p for p in proposals if p.relation == "same_study" and "NCT02220894" in "".join(p.evidence)
    ]
    assert len(keynote_study_props) == 1
    assert set(keynote_study_props[0].occurrence_ids) == {
        t2_primary.occurrence_id,
        t2_followup.occurrence_id,
        t2_abstract.occurrence_id,
        t2_ctgov.occurrence_id,
    }

    # Step 1: Human confirms the duplicate RCT proposal
    rev1 = confirm(
        pg,
        proj_id,
        corpus_rev.identity_revision_id,
        rct_prop.id,
        actor_id,
        ledger=ledger,
    )
    counts1 = get_counts(pg, proj_id, rev1.id)
    assert counts1.total_records_identified == 8
    assert counts1.duplicates_removed == 1  # 1 duplicate removed!
    assert counts1.unique_records == 7  # 7 unique records!
    assert counts1.studies_identified == 7

    # Verify ledger event was created
    events1 = ledger.events(pg, proj_id)
    assert len(events1) >= 1
    assert events1[-1].type == "corpus.confirmed"
    assert events1[-1].payload["proposal_id"] == str(rct_prop.id)

    # Step 2: Human confirms the multi-report study proposal (KEYNOTE-042)
    rev2 = confirm(
        pg,
        proj_id,
        rev1.id,
        keynote_study_props[0].id,
        actor_id,
        ledger=ledger,
    )
    counts2 = get_counts(pg, proj_id, rev2.id)
    assert counts2.total_records_identified == 8
    assert counts2.duplicates_removed == 1
    assert counts2.unique_records == 7
    # Studies identified: 1 (rct) + 1 (keynote-042 with 4 reports) + 1 (t3) + 1 (t4) = 4 studies!
    assert counts2.studies_identified == 4

    events2 = ledger.events(pg, proj_id)
    assert events2[-1].type == "corpus.confirmed"
    assert events2[-1].payload["proposal_id"] == str(keynote_study_props[0].id)

    # Step 3: Human makes a wrong merge, then split undoes it without losing history
    # Suppose human accidentally merges t3_p2 and t4_p3 into same_record
    rev_wrong = merge(
        pg,
        proj_id,
        rev2.id,
        (t3_p2.occurrence_id, t4_p3.occurrence_id),
        "same_record",
        actor_id,
        reason="Mistaken merge",
        ledger=ledger,
    )
    counts_wrong = get_counts(pg, proj_id, rev_wrong.id)
    assert counts_wrong.unique_records == 6
    assert counts_wrong.duplicates_removed == 2
    assert counts_wrong.studies_identified == 3

    # Now human splits them to undo the wrong merge
    rev_split = split(
        pg,
        proj_id,
        rev_wrong.id,
        (t3_p2.occurrence_id, t4_p3.occurrence_id),
        "report",
        actor_id,
        reason="Undoing wrong merge",
        ledger=ledger,
    )
    counts_fixed = get_counts(pg, proj_id, rev_split.id)
    # The counts are restored to exactly what they were before the wrong merge!
    assert counts_fixed.total_records_identified == 8
    assert counts_fixed.duplicates_removed == 1
    assert counts_fixed.unique_records == 7
    assert counts_fixed.studies_identified == 4

    # History is fully preserved across the chain of revisions:
    assert rev_split.parent_id == rev_wrong.id
    assert rev_wrong.parent_id == rev2.id
    assert rev2.parent_id == rev1.id
    assert rev1.parent_id == corpus_rev.identity_revision_id

    # Another project can neither read nor clone this project's revisions.
    other_proj, _ = create_test_project_and_actor(pg)
    other_rev = ingest(
        pg, other_proj, [exec_pubmed, exec_central], actor_id=actor_id, ledger=ledger
    )
    other_prop = next(p for p in other_rev.proposals if p.relation == "same_record")
    pair = (t3_p2.occurrence_id, t4_p3.occurrence_id)
    with pytest.raises(NotFoundError):
        get_counts(pg, other_proj, rev2.id)
    with pytest.raises(NotFoundError):
        confirm(pg, other_proj, rev2.id, other_prop.id, actor_id, ledger=ledger)
    with pytest.raises(NotFoundError):
        merge(pg, other_proj, rev2.id, pair, "same_record", actor_id, ledger=ledger)
    with pytest.raises(NotFoundError):
        split(pg, other_proj, rev2.id, pair, "report", actor_id, ledger=ledger)

    # The database freezes a revision and its links once it is written.
    frozen = [
        ("update identity_link set study_id = report_id where revision_id = %s", (rev1.id,)),
        ("delete from identity_link where revision_id = %s", (rev1.id,)),
        (
            "insert into identity_link (revision_id, occurrence_id, report_id, study_id)"
            " select revision_id, gen_random_uuid(), report_id, study_id"
            " from identity_link where revision_id = %s limit 1",
            (rev1.id,),
        ),
        ("update identity_revision set parent_id = null where id = %s", (rev2.id,)),
        ("update identity_revision set finalized = false where id = %s", (rev2.id,)),
        ("delete from identity_revision where id = %s", (rev_split.id,)),
    ]
    for sql, params in frozen:
        with pytest.raises(psycopg.errors.RaiseException), pg.transaction():
            pg.execute(sql, params)
    with pytest.raises(psycopg.errors.RaiseException, match="not finalized"), pg.transaction():
        pg.execute("insert into identity_revision (project_id) values (%s)", (proj_id,))
        pg.execute("set constraints identity_revision_finalized immediate")
    assert get_counts(pg, proj_id, rev1.id) == counts1

    # Ledger recorded every step
    all_events = ledger.events(pg, proj_id)
    event_types = [e.type for e in all_events]
    assert event_types == [
        "corpus.ingested",
        "corpus.confirmed",
        "corpus.confirmed",
        "corpus.merged",
        "corpus.split",
    ]
