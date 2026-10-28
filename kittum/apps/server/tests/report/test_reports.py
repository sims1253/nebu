"""The core report targets on the nicotine replacement review: the PRISMA flow reconciles, the RIS
files round-trip through the search module's import, the search report holds every PRISMA-S
field, and the AI disclosure names the models and the agreement that screening measured."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import rispy

from kittum.corpus import get_counts
from kittum.judge import VERIFIER_LIMITS
from kittum.models import weights_hash
from kittum.report import load_review, one_line, render, render_files
from kittum.search import SearchPlan, import_file
from tests.report.support import NRT_SCOPE, NRT_TITLES, RECIPE, seed_people

if TYPE_CHECKING:
    from kittum.report import PrismaFlow, Review
    from tests.report.support import NrtSeqs, ReviewWorld

pytestmark = pytest.mark.postgres

SRC = Path(__file__).resolve().parents[2] / "src" / "kittum"


def review_at(world: ReviewWorld, seq: int) -> Review:
    with world.db.transaction() as tx:
        return load_review(tx, world.project_id, cutoff_seq=seq)


def prisma(review: Review, scope: str = NRT_SCOPE) -> PrismaFlow:
    from kittum.report import flow

    return flow(review, "TRIALS", scope)


def refs(world: ReviewWorld, *keys: str) -> list[str]:
    return sorted(world.record(NRT_TITLES[k]).item_ref for k in keys)


def test_prisma_flow_reconciles_by_hand_and_from_data(nrt: tuple[ReviewWorld, NrtSeqs]) -> None:
    world, seqs = nrt
    f = prisma(review_at(world, seqs.full_text))

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
    assert f.duplicates_removed == 2
    assert f.records_screened == 33
    # 24 off-topic PubMed records, colitis, varenicline, the cohort study.
    assert (f.records_excluded, f.records_unresolved) == (27, 0)
    assert f.members["reports_sought"] == tuple(
        refs(world, "patch", "gum", "combination", "followup", "inhaler", "spray")
    )
    # The inhaler trial's report was never obtained.
    assert f.members["reports_not_retrieved"] == tuple(refs(world, "inhaler"))
    assert (f.reports_sought, f.reports_not_retrieved, f.reports_assessed) == (6, 1, 5)
    assert {r.criterion_id: r.reports for r in f.exclusion_reasons} == {
        "COMP-CONTROL": 1,
        "FOLLOWUP": 1,
    }
    assert f.members["reason:COMP-CONTROL"] == tuple(refs(world, "combination"))
    assert f.reports_excluded == 2
    assert f.members["reports_included"] == tuple(refs(world, "patch", "gum", "followup"))
    # The follow-up is a second report of the patch trial: three reports, two studies.
    assert (f.reports_included, f.studies_included) == (3, 2)

    # From data: every box is the sum it must be, and every count is its list of members.
    assert all(c.holds for c in f.checks), [c for c in f.checks if not c.holds]
    assert f.records_identified - f.duplicates_removed == f.records_screened
    assert f.records_screened == f.records_excluded + f.reports_sought + f.records_unresolved
    assert f.reports_sought == f.reports_assessed + f.reports_not_retrieved
    assert f.reports_assessed == f.reports_excluded + f.reports_included
    assert f.reports_excluded == sum(r.reports for r in f.exclusion_reasons)
    for name in ("records_screened", "records_excluded", "reports_assessed", "studies_included"):
        assert len(f.members[name]) == getattr(f, name)
    # The corpus counts the same identification from the same frozen revision.
    with world.db.transaction() as tx:
        counts = get_counts(tx, world.project_id, f.identity_revision_id)  # type: ignore[arg-type]
    assert counts.total_records_identified == f.records_identified
    assert counts.duplicates_removed == f.duplicates_removed
    assert counts.unique_records == f.records_screened

    # The AI never removes a record; what it took part in is counted beside the human counts.
    ai = f.ai_title_abstract
    assert ai.marked_ineligible_by_automation == 0
    assert f.members["title_abstract:excluded_with_ai_proposal"] == tuple(refs(world, "colitis"))
    assert f.members["title_abstract:excluded_after_ai_adjudication"] == tuple(
        refs(world, "glpg", "predictors")
    )
    assert refs(world, "colitis")[0] in f.members["title_abstract:excluded_after_ai_reveal"]
    assert ai.excluded_with_ai_involvement == len(
        set(f.members["title_abstract:excluded_with_ai_proposal"])
        | set(f.members["title_abstract:excluded_after_ai_reveal"])
        | set(f.members["title_abstract:excluded_after_ai_adjudication"])
    )

    # The other scope was never screened, and its checks say so.
    other = prisma(review_at(world, seqs.full_text), "combination-vs-single")
    failing = {c.name for c in other.checks if not c.holds}
    assert "identified minus duplicates equals screened" in failing


def test_prisma_at_earlier_cutoffs_shows_the_review_as_it_stood(
    nrt: tuple[ReviewWorld, NrtSeqs],
) -> None:
    world, seqs = nrt
    # Right after B excluded the inhaler trial, before the undo: an open disagreement.
    before = prisma(review_at(world, seqs.inhaler_excluded))
    assert refs(world, "inhaler")[0] in before.members["records_unresolved"]
    assert refs(world, "inhaler")[0] not in before.members["reports_sought"]
    assert before.reports_assessed == 0
    # After title and abstract, before any full text: every sought report is not yet retrieved.
    ta = prisma(review_at(world, seqs.title_abstract))
    assert (ta.records_excluded, ta.reports_sought, ta.reports_assessed) == (27, 6, 0)
    assert ta.reports_not_retrieved == 6
    assert all(c.holds for c in ta.checks)
    # Before screening, nothing is screened, and the identification is already final.
    corpus = prisma(review_at(world, seqs.corpus))
    assert (corpus.records_identified, corpus.duplicates_removed, corpus.records_screened) == (
        35,
        2,
        0,
    )


def test_prisma_renders_json_and_an_svg_flow_diagram(nrt: tuple[ReviewWorld, NrtSeqs]) -> None:
    world, seqs = nrt
    with world.db.transaction() as tx:
        artifact = render(
            tx,
            "prisma",
            world.project_id,
            NRT_SCOPE,
            store=world.artifacts,
            cutoff_seq=seqs.full_text,
        )
    files = {f.path: f for f in artifact.files}
    assert set(files) == {"TRIALS-nrt-vs-control.json", "TRIALS-nrt-vs-control.svg"}
    data = json.loads(world.artifacts.get(files["TRIALS-nrt-vs-control.json"].sha256))
    assert data["records_screened"] == 33 and data["cutoff_seq"] == seqs.full_text
    svg = world.artifacts.get(files["TRIALS-nrt-vs-control.svg"].sha256).decode()
    texts = [t.text for t in ET.fromstring(svg).iter("{http://www.w3.org/2000/svg}text")]
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
    # Rendering the same cutoff again gives the same bytes.
    with world.db.transaction() as tx:
        again = render(
            tx,
            "prisma",
            world.project_id,
            NRT_SCOPE,
            store=world.artifacts,
            cutoff_seq=seqs.full_text,
        )
    assert again == artifact


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
    assert sorted(e["custom2"].removeprefix("kittum:report:") for e in included) == refs(
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


def test_ai_disclosure_lists_the_models_and_the_agreement_screening_measured(
    nrt: tuple[ReviewWorld, NrtSeqs],
) -> None:
    world, seqs = nrt
    files = {
        f.path: f.data for f in render_files(review_at(world, seqs.full_text), "ai_disclosure")
    }
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
    assert (model["prompt_hash"], model["schema_hash"]) == (
        config["prompt_hash"],
        config["schema_hash"],
    )  # type: ignore[index]
    assert model["verdicts"] == verdicts["n"]  # type: ignore[index]
    assert [t["mode"] for t in d["topics"]] == ["M1"]
    assert d["verifier_limits"] == list(VERIFIER_LIMITS)

    # Agreement, reveals, and the audit are the numbers screening reports to the methods lead.
    with world.db.transaction() as tx:
        for stage in d["stages"]:
            status = world.screening.status(
                tx, world.project_id, world.people.lead, NRT_SCOPE, stage["stage"]
            )
            assert status.metrics is not None
            assert stage["reveal"] == status.metrics.reveal == "on_disagreement"
            assert [
                (a["kind"], a["actor_ids"], a["items"], a["kappa"], a["pabak"])
                for a in stage["agreement"]
            ] == [
                (a.kind, list(a.raters), a.items, a.kappa, a.pabak)
                for a in status.metrics.agreement
            ]
            assert stage["reveals_shown"] == status.metrics.reveals_shown
            assert stage["revised_after_reveal"] == status.metrics.revised_after_reveal
            assert stage["audit"] == status.metrics.audit.model_dump(mode="json")
            assert stage["excluded_by_ai_alone"] == 0
    ta = d["stages"][0]
    human = next(a for a in ta["agreement"] if a["kind"] == "human_human")
    assert human["items"] == 33 and human["kappa"] is not None
    assert ta["revised_after_reveal"] == 1  # A revised the colitis trial after the reveal

    text = files["ai_disclosure.md"].decode()
    assert "not the standard" in text and RECIPE.repository in text
    assert f"| {human['kappa']:.2f} |" in text
    for limit in VERIFIER_LIMITS:
        assert limit in text


def test_core_report_code_names_no_profile() -> None:
    """The core never imports a profile (tests/test_architecture.py) and names none."""
    for path in sorted((SRC / "report").glob("*.py")):
        text = path.read_text().lower()
        for word in ("gba", "amnog", "iqwig", "modul 4", "anhang", "kittum.profiles"):
            assert word not in text, f"{path.name} names {word!r}"


def test_render_rejects_unknown_targets_and_the_bundle(nrt: tuple[ReviewWorld, NrtSeqs]) -> None:
    world, _ = nrt
    with world.db.transaction() as tx:
        with pytest.raises(ValueError, match="unknown report target"):
            render(tx, "nobody.registered_this", world.project_id, store=world.artifacts)
        with pytest.raises(ValueError, match="seal_bundle"):
            render(tx, "bundle", world.project_id, store=world.artifacts)
        with pytest.raises(ValueError, match="cutoff"):
            render(tx, "prisma", world.project_id, store=world.artifacts, cutoff_seq=10**9)
