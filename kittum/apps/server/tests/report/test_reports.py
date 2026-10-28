"""The core report targets on two screened reviews: the PRISMA flow reconciles by hand and with
screening and the corpus, at the head and at earlier cutoffs; the RIS files round-trip through
the search module's import; the search report holds every PRISMA-S field; the AI disclosure names
the models, both agreement readings, and the extra screens the AI triggered; and every metric the
report computes itself equals what screening reports."""

import json
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any

import pytest
import rispy
import yaml

from kittum.core import new_id
from kittum.corpus import confirm, get_counts, ingest, list_proposals
from kittum.judge import USER_TEMPLATE, VERIFIER_LIMITS
from kittum.models import weights_hash
from kittum.report import flow, load_review, one_line, render, render_files
from kittum.screening import Decision
from kittum.search import SearchPlan, import_file
from tests.report.support import (
    CAPI_SCOPE,
    CAPI_TITLES,
    EXAMPLES,
    FIXTURES,
    NRT_SCOPE,
    NRT_TITLES,
    RECIPE,
    build,
    seed_people,
)
from tests.screening.support import (
    NON_SMOKERS,
    OFF_TOPIC,
    RELEVANT,
    SCOPE,
    STAGE,
    build_world,
    central_import,
    pubmed_search,
    recorded_backend,
    simulated_answers,
    timing,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.db import Database
    from kittum.report import PrismaFlow, Review, StageResult
    from kittum.screening import StageMetrics
    from tests.report.support import NrtSeqs, ReviewWorld

pytestmark = pytest.mark.postgres


def review_at(world: ReviewWorld, seq: int | None = None) -> Review:
    with world.db.transaction() as tx:
        return load_review(tx, world.project_id, cutoff_seq=seq)


def prisma(review: Review, scope: str = NRT_SCOPE) -> PrismaFlow:
    return flow(review, "TRIALS", scope)


def refs(world: ReviewWorld, *keys: str) -> tuple[str, ...]:
    return tuple(sorted(world.record(NRT_TITLES[k]).item_ref for k in keys))


def test_prisma_flow_reconciles_by_hand_with_screening_and_the_corpus(
    nrt: tuple[ReviewWorld, NrtSeqs],
) -> None:
    world, seqs = nrt
    review = review_at(world)
    assert review.cutoff_seq == seqs.full_text
    f = prisma(review)

    # By hand: 24 PubMed, 6 CENTRAL, 5 Embase records. Two Embase records repeat CENTRAL DOIs.
    assert {s.source: s.records for s in f.identified_by_source} == {
        "medline": 24,
        "central": 6,
        "embase": 5,
    }
    assert (f.records_identified, f.identified_from_databases, f.identified_from_registers) == (
        35,
        35,
        0,
    )
    assert (f.duplicates_removed, f.records_screened) == (2, 33)
    # 24 off-topic PubMed records, colitis, varenicline, the cohort study. The auditor's extra
    # screens upheld one PubMed exclusion; the adjudicator kept the one the auditor included.
    assert (f.records_excluded, f.records_unresolved) == (27, 0)
    assert f.members["reports_sought"] == refs(
        world, "patch", "gum", "combination", "followup", "inhaler", "spray"
    )
    # The inhaler trial's report was never obtained.
    assert f.members["reports_not_retrieved"] == refs(world, "inhaler")
    assert (f.reports_sought, f.reports_not_retrieved, f.reports_assessed) == (6, 1, 5)
    assert {r.criterion_id: r.reports for r in f.exclusion_reasons} == {
        "COMP-CONTROL": 1,
        "FOLLOWUP": 1,
    }
    assert f.members["reason:COMP-CONTROL"] == refs(world, "combination")
    assert f.members["reports_included"] == refs(world, "patch", "gum", "followup")
    # The follow-up is a second report of the patch trial: three reports, two studies.
    assert (f.reports_excluded, f.reports_included, f.studies_included) == (2, 3, 2)

    # From data: every box is the sum it must be, and every count is its list of members.
    assert all(c.holds for c in f.checks), [c for c in f.checks if not c.holds]
    assert f.records_identified - f.duplicates_removed == f.records_screened
    assert f.records_screened == f.records_excluded + f.reports_sought + f.records_unresolved
    assert f.reports_sought == f.reports_assessed + f.reports_not_retrieved
    assert f.reports_assessed == f.reports_excluded + f.reports_included
    assert f.reports_excluded == sum(r.reports for r in f.exclusion_reasons)
    for name in ("records_screened", "records_excluded", "reports_assessed", "studies_included"):
        assert len(f.members[name]) == getattr(f, name)

    # Screening and the corpus count the same records from the same rows.
    with world.db.transaction() as tx:
        counts = get_counts(tx, world.project_id, f.identity_revision_id)  # type: ignore[arg-type]
        ta = world.screening.status(
            tx, world.project_id, world.people.lead, NRT_SCOPE, "title_abstract"
        )
        ft = world.screening.status(tx, world.project_id, world.people.lead, NRT_SCOPE, "full_text")
    assert (counts.total_records_identified, counts.duplicates_removed, counts.unique_records) == (
        f.records_identified,
        f.duplicates_removed,
        f.records_screened,
    )
    assert (ta.screened, ta.remaining, ta.conflicts) == (f.records_screened, 0, 0)
    assert (ft.screened, ft.remaining, ft.conflicts) == (f.reports_assessed, 0, 0)

    # The AI removes no record; what it took part in is counted beside the human counts. It
    # proposed two of the exclusions on a verified quote: the colitis trial's non-smokers and the
    # cohort study's design.
    assert f.members["title_abstract:excluded_with_ai_proposal"] == refs(world, "colitis", "cohort")
    assert f.members["title_abstract:excluded_after_ai_rescreen"] == refs(
        world, "glpg", "predictors"
    )
    assert f.members["full_text:excluded_after_ai_rescreen"] == refs(world, "combination", "spray")
    assert refs(world, "colitis")[0] in f.members["title_abstract:excluded_after_ai_reveal"]
    assert f.ai_title_abstract.excluded_with_ai_involvement == len(
        set(f.members["title_abstract:excluded_with_ai_proposal"])
        | set(f.members["title_abstract:excluded_after_ai_reveal"])
        | set(f.members["title_abstract:excluded_after_ai_rescreen"])
    )

    # The rendered diagram shows these numbers, and the same cutoff renders the same bytes.
    with world.db.transaction() as tx:
        artifact = render(tx, "prisma", world.project_id, NRT_SCOPE, store=world.artifacts)
        again = render(tx, "prisma", world.project_id, NRT_SCOPE, store=world.artifacts)
    assert again == artifact
    files = {a.path: world.artifacts.get(a.sha256) for a in artifact.files}
    assert json.loads(files["TRIALS-nrt-vs-control.json"]) == f.model_dump(mode="json")
    svg = ET.fromstring(files["TRIALS-nrt-vs-control.svg"])
    texts = [t.text for t in svg.iter("{http://www.w3.org/2000/svg}text")]
    for line in (
        "Records screened: n = 33",
        "Records excluded: n = 27",
        "Reports not retrieved: n = 1",
        "Reports assessed for eligibility: n = 5",
        "  COMP-CONTROL: n = 1",
        "Studies included in review: n = 2",
        "Reports of included studies: n = 3",
        "Duplicate records removed: n = 2",
    ):
        assert line in texts, line
    # No box claims what cannot happen: the AI never marks a record ineligible on its own.
    assert not any("automation" in (t or "") for t in texts)
    # Legible at the width a report page or the Reports screen gives it: shown 870 px wide, the
    # smallest text is still 14 px.
    width = float(svg.get("width") or 0)
    smallest = min(
        float(t.get("font-size") or 0) for t in svg.iter("{http://www.w3.org/2000/svg}text")
    )
    assert smallest * 870 / width >= 14, (smallest, width)
    # Every line fits its box, even in a monospaced fallback font (0.6 em a character).
    _title, *lines = texts
    boxes = [t for t in lines if t not in ("Identification", "Screening", "Included")]
    assert max(len(t or "") * 16 * 0.6 for t in boxes) <= 420 - 2 * 12

    # The other scope was never screened, and its checks say so.
    failing = {c.name for c in prisma(review, "combination-vs-single").checks if not c.holds}
    assert "identified minus duplicates equals screened plus awaiting screening" in failing


def test_prisma_at_earlier_cutoffs_shows_the_review_as_it_stood(
    nrt: tuple[ReviewWorld, NrtSeqs],
) -> None:
    world, seqs = nrt
    inhaler, rescreened = refs(world, "inhaler")[0], refs(world, "glpg", "predictors")
    # Right after B excluded the inhaler trial, before the undo: an open disagreement.
    before = prisma(review_at(world, seqs.inhaler_excluded))
    assert inhaler in before.members["records_unresolved"]
    assert inhaler not in before.members["reports_sought"]
    # Both screeners are done, but the AI sent two exclusions to the auditor, who has not screened
    # them yet: they still await screening, as screening counts them, and the sums hold.
    # The varenicline disagreement is screened but waits for the adjudicator.
    waiting = prisma(review_at(world, seqs.rescreens_open))
    assert waiting.members["records_awaiting_screening"] == rescreened
    assert waiting.members["records_unresolved"] == refs(world, "varenicline")
    assert (waiting.records_screened, waiting.records_excluded, waiting.records_unresolved) == (
        31,
        24,
        1,
    )
    assert all(c.holds for c in waiting.checks)
    # After title and abstract, before any full text: every sought report is not yet retrieved.
    ta = prisma(review_at(world, seqs.title_abstract))
    assert (ta.records_excluded, ta.reports_sought, ta.reports_assessed) == (27, 6, 0)
    assert ta.reports_not_retrieved == 6 and all(c.holds for c in ta.checks)
    # Before screening, nothing is screened, and the identification is already final.
    corpus = prisma(review_at(world, seqs.corpus))
    assert (corpus.records_identified, corpus.duplicates_removed, corpus.records_screened) == (
        35,
        2,
        0,
    )


def test_ris_files_round_trip_through_the_search_import(nrt: tuple[ReviewWorld, NrtSeqs]) -> None:
    world, seqs = nrt
    review = review_at(world, seqs.full_text)
    files = {f.path: f.data for f in render_files(review, "ris")}
    assert sorted(p for p in files if p.startswith("searches/")) == [
        f"searches/01_medline_{review.searches[0].execution.started_at.date()}.ris",
        f"searches/02_central_{review.searches[1].execution.started_at.date()}.ris",
        f"searches/03_embase_{review.searches[2].execution.started_at.date()}.ris",
    ]

    # Each undeduplicated file holds its search's records, and importing it gives them back.
    with world.db.transaction() as tx:
        scratch = seed_people(tx, "RIS round trip")
        for path, run in zip(
            sorted(p for p in files if p.startswith("searches/")), review.searches, strict=True
        ):
            original = sorted(run.execution.records, key=lambda r: r.ordinal)
            imported = import_file(
                tx,
                scratch.project_id,
                SearchPlan(topic_id="TRIALS", source="reimport", platform="RIS", strategy=path),
                files[path],
                "ris",
                ledger=world.ledger,
                actor_id=scratch.lead,
                artifacts=world.artifacts,
            ).records
            assert len(imported) == len(original) == run.execution.retrieved_count
            for back, rec in zip(imported, original, strict=True):
                assert (back.title, back.abstract, back.authors, back.year, back.doi) == (
                    one_line(rec.title),
                    one_line(rec.abstract) if rec.abstract else None,
                    rec.authors,
                    rec.year,
                    rec.doi,
                )
                assert back.provider_record_id == rec.provider_record_id
                assert set(rec.registry_ids) <= set(back.registry_ids)
                assert back.journal == rec.journal

    # The deduplicated file has one entry per report; the scope lists carry report and study.
    dedup = rispy.loads(files["deduplicated.ris"].decode())
    reports = {e["custom2"].removeprefix("kittum:report:") for e in dedup}
    assert len(dedup) == len(reports) == 33
    assert reports == {str(link.report_id) for link in review.identity.links}  # type: ignore[union-attr]
    included = rispy.loads(files["TRIALS-nrt-vs-control/included.ris"].decode())
    assert tuple(sorted(e["custom2"].removeprefix("kittum:report:") for e in included)) == refs(
        world, "patch", "gum", "followup"
    )
    assert len({e["custom3"] for e in included}) == 2
    excluded_ft = rispy.loads(files["TRIALS-nrt-vs-control/excluded_full_text.ris"].decode())
    assert sorted(n for e in excluded_ft for n in e["notes"] if n.startswith("Excluded")) == [
        "Excluded at full text: COMP-CONTROL",
        "Excluded at full text: FOLLOWUP",
    ]
    assert (
        len(rispy.loads(files["TRIALS-nrt-vs-control/excluded_title_abstract.ris"].decode())) == 27
    )


def test_search_report_records_every_prisma_s_field(nrt: tuple[ReviewWorld, NrtSeqs]) -> None:
    world, seqs = nrt
    files = {
        f.path: f.data for f in render_files(review_at(world, seqs.full_text), "search_report")
    }
    data = json.loads(files["search_report.json"])
    pubmed, central, embase = data["searches"]
    assert (pubmed["database"], pubmed["platform"], pubmed["search_type"]) == (
        "MEDLINE",
        "PubMed",
        "live_api",
    )
    assert pubmed["strategy"] == "lumacaftor[ti] AND trial[tiab]"
    assert pubmed["query_translation"]  # what PubMed made of the query, as recorded
    assert (pubmed["reported_count"], pubmed["retrieved_count"]) == (24, 24)
    assert pubmed["study_filter"] == "Cochrane highly sensitive search strategy for RCTs"
    assert pubmed["run_by"].startswith("lead@example.org")
    assert (central["search_type"], central["reported_count"]) == ("file_import", 6)
    assert embase["protocol_source_id"] == "embase" and embase["retrieved_count"] == 5
    for row in data["searches"]:
        assert row["raw_artifact_hashes"] and all(
            world.artifacts.exists(h) for h in row["raw_artifact_hashes"]
        )
    assert {u["source_id"] for u in data["planned_sources_not_searched"]} == {
        "psycinfo",
        "clinicaltrials_gov",
        "ictrp",
    }
    text = files["search_report.md"].decode()
    assert "lumacaftor[ti] AND trial[tiab]" in text
    assert pubmed["query_translation"] in text
    assert "'nicotine replacement therapy'/exp" in text


def test_prisma_counts_as_screened_only_what_screening_counts_as_screened(
    nrt: tuple[ReviewWorld, NrtSeqs], database: Database
) -> None:
    world, seqs = nrt
    # As the review ran: right after each stage opened, while the extra screens were open, and at
    # the end, the flow at that ledger position shows what the lead's status showed then.
    for seq, status in seqs.statuses:
        f = prisma(review_at(world, seq))
        if status.stage == "title_abstract":
            counts = (f.records_screened, f.records_awaiting_screening, f.records_unresolved)
        else:
            counts = (f.reports_assessed, f.reports_awaiting_assessment, f.reports_unresolved)
        assert counts == (status.screened, status.remaining, status.conflicts), (seq, status)
        assert all(c.holds for c in f.checks), (seq, [c for c in f.checks if not c.holds])
    opened, rescreens_open, *_ = seqs.statuses
    assert (opened[1].screened, opened[1].remaining) == (0, 33)
    assert (rescreens_open[1].screened, rescreens_open[1].remaining) == (31, 2)

    # M3: the humans screen in the AI's order until the stop test certifies, and stop. The rest
    # were never screened, and the flow says so for good.
    m3 = build_world(database, "M3", simulate={RELEVANT: 8, NON_SMOKERS: 30, OFF_TOPIC: 112})
    m3.open(
        auditor=m3.people.auditor,
        backend=recorded_backend(m3.records, m3.criteria(), answers=simulated_answers(m3.records)),
    )
    for reviewer in (m3.people.a, m3.people.b):
        with m3.db.transaction() as tx:
            items = m3.screening.queue(tx, m3.project_id, reviewer, SCOPE, STAGE, limit=110)
        for item in items:
            relevant = item.title.startswith(RELEVANT) or item.title.startswith("Nicotine")
            decision = (
                Decision(outcome="include")
                if relevant
                else Decision(outcome="exclude", reason_criterion_id="POP")
            )
            with m3.db.transaction() as tx:
                result = m3.screening.decide(
                    tx,
                    command_id=new_id(),
                    project_id=m3.project_id,
                    reviewer_id=reviewer,
                    assignment_id=item.assignment_id,
                    decision=decision,
                    phase="provisional",
                    expected_etag=item.etag,
                    timing=timing(0),
                )
                if result.phase == "provisional":
                    m3.screening.decide(
                        tx,
                        command_id=new_id(),
                        project_id=m3.project_id,
                        reviewer_id=reviewer,
                        assignment_id=item.assignment_id,
                        decision=decision,
                        phase="final",
                        expected_etag=result.etag,
                        timing=timing(20, revealed=True),
                    )
    with m3.db.transaction() as tx:
        status = m3.screening.status(tx, m3.project_id, m3.people.lead, SCOPE, STAGE)
        review = load_review(tx, m3.project_id)
    assert status.metrics is not None and status.metrics.stop_test is not None
    assert status.metrics.stop_test.stop
    f = flow(review, "TRIALS", SCOPE)
    assert (f.records_screened, f.records_awaiting_screening) == (status.screened, status.remaining)
    assert f.records_awaiting_screening > 0
    assert f.records_screened == f.records_excluded + f.reports_sought + f.records_unresolved
    assert all(c.holds for c in f.checks), [c for c in f.checks if not c.holds]


def test_identification_comes_from_the_frozen_revision_when_a_search_follows_it(
    database: Database,
) -> None:
    """An update search imported after the corpus was frozen is not identified yet: the flow
    counts what the selected identity revision holds, as the corpus does, and lists the rest."""
    source = yaml.safe_load((EXAMPLES / "nicotine-replacement.yaml").read_text())
    world = build(
        database,
        "Update search",
        source,
        topic="TRIALS",
        scope=NRT_SCOPE,
        config_id=f"upd-{new_id()}",
    )
    with database.transaction() as tx:
        world.executions.append(central_import(tx, world.people, world.ledger, world.artifacts))  # type: ignore[arg-type]
        world.import_file(tx, "embase", "Embase", "nicotine", FIXTURES / "nrt_embase.ris", "embase")
        frozen = ingest(
            tx, world.project_id, world.executions, actor_id=world.people.lead, ledger=world.ledger
        ).identity_revision_id
        (proposal, *_) = list_proposals(tx, world.project_id, status="pending")
        merged = confirm(
            tx, world.project_id, frozen, proposal.id, world.people.lead, ledger=world.ledger
        ).id
        # The update search: PubMed, run after the corpus was built and never ingested.
        update = pubmed_search(tx, world.people, world.ledger, world.artifacts)  # type: ignore[arg-type]

    with database.transaction() as tx:
        for revision in (frozen, merged):
            review = load_review(tx, world.project_id, identity_revision_id=revision)
            f = flow(review, "TRIALS", NRT_SCOPE)
            counts = get_counts(tx, world.project_id, revision)
            assert (f.records_identified, f.duplicates_removed) == (
                counts.total_records_identified,
                counts.duplicates_removed,
            ), revision
            assert len(f.members["unique_records"]) == counts.unique_records
            assert sum(s.records for s in f.identified_by_source) == f.records_identified == 11
            assert {s.source for s in f.identified_by_source} == {"central", "embase"}
            assert f.members["records_outside_identity"] == tuple(
                sorted(str(r.occurrence_id) for r in update.records)
            )
            assert f.records_outside_identity == 24
            # No stage is open yet, so nothing is screened or awaiting screening.
            assert f.records_awaiting_screening == 0 and f.records_screened == 0
    # Selecting the older revision undoes the lead's confirmed duplicate, and only that.
    with database.transaction() as tx:
        older = flow(
            load_review(tx, world.project_id, identity_revision_id=frozen), "TRIALS", NRT_SCOPE
        )
        newer = flow(
            load_review(tx, world.project_id, identity_revision_id=merged), "TRIALS", NRT_SCOPE
        )
    assert (older.duplicates_removed, newer.duplicates_removed) == (
        0,
        len(proposal.occurrence_ids) - 1,
    )


def test_each_topic_flow_identifies_its_own_searches_and_together_they_cover_the_revision(
    capivasertib: ReviewWorld,
) -> None:
    """Two searched topics: the RCT searches, and the indirect comparison's bridge search, which
    finds CAPItello-291 and FAKTION again. Each flow stands alone, as PRISMA reports one per
    question, so both reports are identified and unique in both."""
    world = capivasertib
    review = review_at(world)
    rct, ic = flow(review, "RCT", CAPI_SCOPE), flow(review, "IC", CAPI_SCOPE)
    turner = world.record(CAPI_TITLES["turner"]).item_ref
    jones = world.record(CAPI_TITLES["jones"]).item_ref

    # By hand: RCT has 5 MEDLINE, 2 CENTRAL, and 3 CTIS records, two of them CENTRAL repeats of
    # MEDLINE; IC has the 2 CENTRAL records, both reports RCT found too.
    assert (rct.records_identified, rct.duplicates_removed, len(rct.members["unique_records"])) == (
        10,
        2,
        8,
    )
    assert (ic.records_identified, ic.duplicates_removed, ic.members["unique_records"]) == (
        2,
        0,
        tuple(sorted((turner, jones))),
    )
    assert {turner, jones} <= set(rct.members["unique_records"])

    # Each topic identifies exactly its own searches' occurrences in the revision.
    with world.db.transaction() as tx:
        per_topic = {
            r["topic_id"]: r["n"]
            for r in tx.execute(
                "select e.topic_id, count(*) as n from identity_link l"
                " join source_occurrence o on o.occurrence_id = l.occurrence_id"
                " join search_execution e on e.id = o.execution_id"
                " where l.revision_id = %s group by e.topic_id",
                (rct.identity_revision_id,),
            ).fetchall()
        }
        counts = get_counts(tx, world.project_id, rct.identity_revision_id)  # type: ignore[arg-type]
    assert per_topic == {"RCT": rct.records_identified, "IC": ic.records_identified}

    # Together the flows cover the revision: the identified records sum to the corpus's total,
    # and the union of the unique records is its reports. Neither flow alone equals get_counts.
    assert review.identity is not None
    union = set(rct.members["unique_records"]) | set(ic.members["unique_records"])
    assert union == {str(link.report_id) for link in review.identity.links}
    assert len(union) == counts.unique_records == 8
    assert rct.records_identified + ic.records_identified == counts.total_records_identified == 12
    assert counts.duplicates_removed == 4 != rct.duplicates_removed + ic.duplicates_removed


def test_ai_disclosure_names_the_models_both_readings_and_the_extra_screens(
    nrt: tuple[ReviewWorld, NrtSeqs],
) -> None:
    world, _ = nrt
    files = {f.path: f.data for f in render_files(review_at(world), "ai_disclosure")}
    d = json.loads(files["ai_disclosure.json"])
    (model,) = d["models"]
    assert (model["repository"], model["revision"], model["weights_licence"]) == (
        RECIPE.repository,
        RECIPE.revision,
        RECIPE.weights_licence,
    )
    assert model["weights_hash"] == weights_hash(RECIPE)
    assert model["reported_model_identities"] == ["Qwen/Qwen3.8-27B"]
    with world.db.transaction() as tx:
        config = tx.execute(
            "select prompt_hash, schema_hash from judge_config where id = %s", (world.config.id,)
        ).fetchone()
        verdicts = tx.execute(
            "select count(*) as n from verdict where judge_config_id = %s", (world.config.id,)
        ).fetchone()
    assert config is not None and verdicts is not None
    assert (model["prompt_hash"], model["schema_hash"]) == (
        config["prompt_hash"],
        config["schema_hash"],
    )
    assert model["verdicts"] == verdicts["n"]
    assert [t["mode"] for t in d["topics"]] == ["M1"]
    assert d["verifier_limits"] == list(VERIFIER_LIMITS)

    ta, ft = d["stages"]
    # Both readings of the AI against each screener, from the blind decisions.
    assert sorted(a["kind"] for a in ta["agreement"]) == [
        "human_ai",
        "human_ai",
        "human_ai_verified",
        "human_ai_verified",
        "human_human",
    ]
    assert ta["revised_after_reveal"] == 1  # A revised the colitis trial after the reveal
    # The extra screens the AI triggered, and what came of them.
    cases = {c["item_ref"]: (c["reason"], c["result"], c["outcome"]) for c in ta["rescreen_cases"]}
    glpg, predictors = refs(world, "glpg")[0], refs(world, "predictors")[0]
    assert cases == {
        glpg: ("ai_include", "upheld", "exclude"),
        predictors: ("ai_include", "adjudicated", "exclude"),
    }
    assert ta["rescreens"] == {"triggered": 2, "pending": 0, "upheld": 1, "adjudicated": 1}
    assert {c["result"] for c in ft["rescreen_cases"]} == {"upheld"}
    assert ft["rescreens"]["triggered"] == 2
    # Nothing that cannot happen is counted: the AI never excludes on its own.
    assert "excluded_by_ai_alone" not in ta

    # Agreement covers every record both sides decided, with its 2x2 table.
    text = files["ai_disclosure.md"].decode()
    assert "AI alone" not in text and "by AI alone" not in text
    for stage in (ta, ft):
        for a in stage["agreement"]:
            cells = a["table"]
            assert sum(cells.values()) == a["items"]
            assert a["agreed"] == cells["include_include"] + cells["exclude_exclude"]
    human = next(a for a in ta["agreement"] if a["kind"] == "human_human")
    assert (
        f"Reviewer 1 and Reviewer 2 agreed on {human['agreed']} of {human['items']} records" in text
    )
    assert f"Cohen's kappa is {human['kappa']:.2f}" in text
    assert f"Both cover all {human['items']} records, agreed or not." in text
    own = next(
        a for a in ta["agreement"] if a["kind"] == "human_ai" and a["raters"][0] == "Reviewer 1"
    )
    cells = own["table"]
    assert "|  | the AI's own reading: include | the AI's own reading: exclude |" in text
    assert (
        f"| Reviewer 1: exclude | {cells['exclude_include']} | {cells['exclude_exclude']} |" in text
    )
    assert "the AI's own reading" in text and "the AI's checked reading" in text
    assert "| nrt-vs-control | Title and abstract | 2 | 1 | 1 | 0 |" in text
    assert RECIPE.repository in text
    for limit in VERIFIER_LIMITS:
        assert limit in text

    # The prompts, read back from the stored requests: one card per stage the judge read.
    with world.db.transaction() as tx:
        review = load_review(tx, world.project_id, store=world.artifacts)
    stored = {f.path: f.data for f in render_files(review, "ai_disclosure")}
    d = json.loads(stored["ai_disclosure.json"])
    cards = {card["label"]: card for card in d["prompts"]}
    assert set(cards) == {f"Title and abstract, scope {NRT_SCOPE}", f"Full text, scope {NRT_SCOPE}"}
    card = cards[f"Title and abstract, scope {NRT_SCOPE}"]
    assert card["role"] == "judge" and card["request_form"] == "built_request"
    assert card["system_prompt"].startswith("You screen records for a systematic literature")
    assert "This is title and abstract screening." in card["system_prompt"]
    assert card["user_prompt_template"] == USER_TEMPLATE
    assert card["first_user_prompt"].startswith("Stage: title and abstract screening")
    criteria = {c.id for c in world.criteria("title_abstract")}
    assert set(card["json_schema"]["properties"]) == criteria
    assert card["request_settings"]["temperature"] == 0.0
    assert card["model"]["revision"] == RECIPE.revision
    assert card["calls"] >= 33
    md = stored["ai_disclosure.md"].decode()
    assert "## 6. Prompts" in md
    assert card["system_prompt"] in md and USER_TEMPLATE in md
    assert json.dumps(card["json_schema"], indent=2) in md


# Report computes these from the replayed rows instead of calling screening, so they stay exact at
# old cutoffs. Each is compared with what screening reports at the latest cutoff, so that a change
# to screening's definitions fails here, under the metric's name.
METRICS: dict[str, Callable[[StageResult], Any]] = {
    "screened": lambda s: (s.screened, len(s.records) - s.screened, s.conflicts_open),
    "conflicts_resolved": lambda s: s.conflicts_resolved,
    "agreement": lambda s: s.agreement,
    "reveals": lambda s: (s.reveals_shown, s.revised_after_reveal),
    "audit": lambda s: s.audit,
    "rescreens": lambda s: s.rescreens,
}
FROM_STATUS: dict[str, Callable[[Any], Any]] = {
    "screened": lambda st: (st.screened, st.remaining, st.conflicts),
    "conflicts_resolved": lambda st: st.metrics.conflicts_resolved,
    "agreement": lambda st: st.metrics.agreement,
    "reveals": lambda st: (st.metrics.reveals_shown, st.metrics.revised_after_reveal),
    "audit": lambda st: st.metrics.audit,
    "rescreens": lambda st: st.metrics.rescreens,
}


@pytest.mark.parametrize("metric", sorted(METRICS))
def test_a_metric_the_report_computes_equals_what_screening_reports(
    metric: str, nrt: tuple[ReviewWorld, NrtSeqs], capivasertib: ReviewWorld
) -> None:
    compared = 0
    for world in (nrt[0], capivasertib):
        review = review_at(world)
        with world.db.transaction() as tx:
            for stage in review.stages:
                status = world.screening.status(
                    tx,
                    world.project_id,
                    world.people.lead,
                    stage.scope_id,
                    stage.stage,
                    topic_id=stage.topic_id,
                )
                metrics: StageMetrics | None = status.metrics
                assert metrics is not None
                assert METRICS[metric](stage) == FROM_STATUS[metric](status), (
                    world.protocol.version.protocol.title,
                    stage.stage,
                )
                compared += 1
    assert compared == 4
