"""The G-BA profile's Modul 4 targets on the capivasertib dossier: the search strategies, the
exclusion annexes with criterion reasons in German, the §4.2.3.5 text from the screening record,
and the Anlage II.1 tree, all from the same ledger as the core reports, and in the bundle."""

import csv
import io
import json
from typing import TYPE_CHECKING

import pytest
import rispy

from kittum.cli import main
from kittum.core import sha256_hex
from kittum.ledger import SigningKey
from kittum.profiles import load_profiles
from kittum.profiles.gba_amnog.reports import ANLAGE_II_1, TARGETS
from kittum.report import flow, load_review, register_target, render_files, seal_bundle, targets
from tests.report.support import CAPI_SCOPE, CAPI_TITLES

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.report import Review
    from tests.report.support import NrtSeqs, ReviewWorld

pytestmark = pytest.mark.postgres


def files(review: Review, target: str) -> dict[str, bytes]:
    return {f.path: f.data for f in render_files(review, target)}


def rows(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))


@pytest.fixture(scope="module")
def review(capivasertib: ReviewWorld) -> Review:
    with capivasertib.db.transaction() as tx:
        return load_review(tx, capivasertib.project_id)


def test_the_profile_registers_its_targets_once(nrt: tuple[ReviewWorld, NrtSeqs]) -> None:
    load_profiles()
    load_profiles()  # safe to call again
    assert set(targets("gba_amnog")) - set(targets(None)[:5]) == set(TARGETS)
    with pytest.raises(ValueError, match="already registered"):
        register_target("gba_amnog.anhang_4c", TARGETS["gba_amnog.anhang_4c"])
    with pytest.raises(ValueError, match="<profile id>"):
        register_target("modul4", TARGETS["gba_amnog.anhang_4c"])
    # A review without the profile cannot render its targets.
    world, _ = nrt
    with world.db.transaction() as tx:
        plain = load_review(tx, world.project_id)
    with pytest.raises(ValueError, match="profile 'gba_amnog'"):
        render_files(plain, "gba_amnog.anhang_4c")


def test_anhang_4a_and_4b_list_each_search_with_its_strategy(review: Review) -> None:
    a = files(review, "gba_amnog.anhang_4a")
    text = a["4-A.md"].decode()
    assert "## Anhang 4-A1: Recherche RCT" in text and "## Anhang 4-A2: Recherche IC" in text
    assert "### MEDLINE" in text and "### Cochrane Central Register of Controlled Trials" in text
    assert "| Suchoberfläche | PubMed |" in text
    assert "Wong 2006" in text  # the study filter from the source plan
    assert "| 4 | #4 #1 AND #2 AND #3 | 5 |" in text  # the last line carries the hits
    assert "Clinical Trials Information System" not in text  # registries go to 4-B
    assert [(r["Quelle"], r["Treffer"]) for r in rows(a["4-A.csv"])] == [
        ("MEDLINE", "5"),
        ("Cochrane Central Register of Controlled Trials", "2"),
    ]
    b = files(review, "gba_amnog.anhang_4b")
    assert [(r["Quelle"], r["Suchstrategie"], r["Treffer"]) for r in rows(b["4-B.csv"])] == [
        ("Clinical Trials Information System", "capivasertib", "3")
    ]


def test_anhang_4c_and_4d_give_the_protocol_criterion_as_the_reason(review: Review) -> None:
    c = files(review, "gba_amnog.anhang_4c")
    (row,) = rows(c["4-C.csv"])
    assert row["Anhang"] == "4-C1" and row["Teilpopulation"] == CAPI_SCOPE
    assert (
        row["Dokument"].startswith("Jones, Robert H.")
        and "DOI: 10.1016/S1470-2045(19)30817-4" in row["Dokument"]
    )
    assert row["Ausschlussgrund"].startswith("RCT-E1-ALT (Ergebnisse liegen für Patientinnen")
    # The same exclusion is in the core flow, counted under the same criterion.
    f = flow(review, "RCT", CAPI_SCOPE)
    assert f.members["reason:RCT-E1-ALT"] == (row["Kittum-Report"],)
    assert {r.criterion_id: r.reports for r in f.exclusion_reasons} == {"RCT-E1-ALT": 1}

    d = files(review, "gba_amnog.anhang_4d")
    registry = rows(d["4-D.csv"])
    assert sorted(
        (r["Register-ID"], r["Stufe"], r["Ausschlussgrund"].split(" ")[0]) for r in registry
    ) == [
        ("2024-512345-18-00", "Titel/Abstract", "RCT-E2"),
        ("2024-598765-31-00", "Titel/Abstract", "RCT-E1"),
    ]
    # Bibliographic exclusions are not registry entries, and the included CTIS entry is absent.
    titles = {r["Titel"] for r in registry}
    assert not any(t.startswith(CAPI_TITLES["pakt"]) for t in titles)
    assert not any(t.startswith(CAPI_TITLES["ctis_capitello"]) for t in titles)


def test_justification_4235_is_written_from_the_mode_and_the_screening_record(
    review: Review,
) -> None:
    text = files(review, "gba_amnog.justification_4235")["begruendung_4-2-3-5.md"].decode()
    rct, ic = text.split("## Recherche IC")
    assert "durch zwei Personen unabhängig voneinander" in rct
    assert "bedarf keiner Begründung nach 4.2.3.5" in rct
    ta = flow(review, "RCT", CAPI_SCOPE)
    stage = review.stage(CAPI_SCOPE, "title_abstract", "RCT")
    assert stage is not None
    assert (
        f"**Titel/Abstract, Teilpopulation a1.** {ta.records_screened} Dokumente; zwei Personen"
        in rct
    )
    assert f"- Ausgeschlossen: {ta.records_excluded};" in rct
    (pair,) = [a for a in stage.agreement if a.kind == "human_human"]
    assert f"Cohens Kappa {pair.kappa:.2f}".replace(".", ",") in rct
    assert "Qwen/Qwen3.8-27B" in rct and "allein durch die KI: 0" in rct
    # The IC topic is declared in M2: the protocol's reason and validation pack, not yet screened.
    topic = review.topic("IC")
    assert "Abweichend von der Selektion durch zwei Personen wurde Modus M2" in ic
    assert topic.mode_rationale is not None and topic.mode_rationale["de"] in ic
    assert f"Validierung: {topic.validation_pack}." in ic
    assert "noch kein Screening erfasst" in ic


def test_anlage_ii_1_holds_a_ris_file_for_every_search_and_reference_list(review: Review) -> None:
    tree = files(review, "gba_amnog.anlage_ii_1")
    area = f"{ANLAGE_II_1['root']}/Modul4A"
    searches = sorted(p for p in tree if p.startswith(f"{area}/{ANLAGE_II_1['searches']}/"))
    assert [p.rsplit("/", 1)[1].split("_")[:3] for p in searches] == [
        ["RCT", "01", "medline"],
        ["RCT", "02", "central"],
        ["RCT", "03", "ctis"],
    ]
    for path, run in zip(searches, review.searches, strict=True):
        assert len(rispy.loads(tree[path].decode())) == run.execution.retrieved_count
    lists = f"{area}/{ANLAGE_II_1['lists']}"
    included = rispy.loads(tree[f"{lists}/RCT_eingeschlossen.ris"].decode())
    assert len(included) == flow(review, "RCT", CAPI_SCOPE).reports_included == 3
    assert len(rispy.loads(tree[f"{lists}/RCT_Anhang_4-C_ausgeschlossen.ris"].decode())) == 1
    assert {
        e["type_of_reference"]
        for e in rispy.loads(tree[f"{lists}/RCT_Anhang_4-D_ausgeschlossen.ris"].decode())
    } == {"DBASE"}
    # The provisional names are flagged, and the index lists every file by hash.
    assert "vorläufig" in tree["GAPS.md"].decode()
    index = {r["Pfad"]: r["SHA-256"] for r in rows(tree["index.csv"])}
    assert index == {p: sha256_hex(d) for p, d in tree.items() if p not in ("GAPS.md", "index.csv")}


def test_the_dossier_travels_in_the_bundle(
    capivasertib: ReviewWorld, review: Review, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = SigningKey.generate()
    with capivasertib.db.transaction() as tx:
        bundle = seal_bundle(
            tx,
            capivasertib.project_id,
            capivasertib.artifacts,
            tmp_path / "bundle",
            signer=key,
            actor_id=capivasertib.people.lead,
            cutoff_seq=review.cutoff_seq,
            ledger=capivasertib.ledger,
        )
    assert main(["verify", str(bundle.path)]) == 0
    capsys.readouterr()
    manifest = json.loads((bundle.path / "manifest.json").read_text())
    for target, renderer in TARGETS.items():
        name = target.partition(".")[2]
        for f in renderer(review, None):
            path = f"reports/dossier/{name}/{f.path}"
            assert manifest["files"][path] == sha256_hex(f.data), path
    assert "reports/prisma/RCT-a1.json" in manifest["files"]
