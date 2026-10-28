"""Modul 4 report targets of the G-BA profile (docs/profiles/gba-amnog.md, "Reports").

Every target renders from the core's `kittum.report.Review`, so its counts and reasons are the
ones the core PRISMA flow and the bundle hold:

- `gba_amnog.anhang_4a` and `gba_amnog.anhang_4b`: the search strategies per bibliographic
  database and per study registry, one block per search, numbered per Recherche (4-A1, 4-A2, ...).
- `gba_amnog.anhang_4c`: documents excluded at full text, with the protocol criterion as reason.
- `gba_amnog.anhang_4d`: registry entries excluded at any stage, with their reasons.
- `gba_amnog.justification_4235`: the §4.2.3.5 text on how studies were selected, from the
  declared screening mode, the audit plan, and the screening record.
- `gba_amnog.anlage_ii_1`: the Modul 5 directory tree of RIS files.

Anlage II.1 (08.04.2026) fixes the tree's file names, but the profile doc records only that the
document was located, not its names. The tree below is a provisional layout in one table
(`ANLAGE_II_1`); `GAPS.md` in the output says so. Replace the table once the names are read.
"""

import csv
import io
from typing import TYPE_CHECKING, Any

from kittum.core import sha256_hex
from kittum.profiles.gba_amnog import PROFILE_ID, GbaProtocol
from kittum.report import (
    ItemResult,
    RenderedFile,
    Review,
    StageResult,
    md_table,
    register_target,
    representative,
    ris_dumps,
    ris_entry,
    safe_name,
    targets,
    text_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from kittum.protocol import Criterion, SearchTopic

STAGE_DE = {"title_abstract": "Titel/Abstract", "full_text": "Volltext"}
REVEAL_DE = {"never": "nie", "on_disagreement": "bei Abweichung", "always": "immer"}
PEOPLE_DE = {1: "eine Person", 2: "zwei Personen"}

ANLAGE_II_1: dict[str, str] = {
    "root": "Modul5",
    "area": "Modul4{code}",
    "searches": "Recherchen",
    "lists": "Referenzlisten",
    "search_file": "{topic}_{n:02d}_{source}_{date}.ris",
    "included": "{topic}_eingeschlossen.ris",
    "excluded_full_text": "{topic}_Anhang_4-C_ausgeschlossen.ris",
    "excluded_registry": "{topic}_Anhang_4-D_ausgeschlossen.ris",
}
"""Provisional names of the Anlage II.1 tree. Not checked against the document; see GAPS.md."""

GAPS = """# Offene Punkte zu Anlage II.1

Die Verzeichnisstruktur und die Dateinamen dieses Baums sind vorläufig. Anlage II.1 (Stand
08.04.2026) legt sie verbindlich fest, aber die genauen Namen sind in Kittum noch nicht aus dem
Dokument übernommen (docs/profiles/gba-amnog.md: "Reported, file located").

- Die Namen stehen an einer Stelle (`kittum.profiles.gba_amnog.reports.ANLAGE_II_1`) und werden
  dort ersetzt, sobald sie aus Anlage II.1 gelesen sind.
- Die Volltexte (maschinenlesbare, ungeschützte PDF-Dateien) fehlen; sie kommen mit dem Modul
  `documents`.
- Jede Datei ist mit SHA-256 in `index.csv` verzeichnet.
"""


def _protocol(review: Review) -> GbaProtocol:
    return GbaProtocol.model_validate(review.document.extensions)


def _area(review: Review, topic_id: str) -> str:
    return next(
        (a.code for a in _protocol(review).anwendungsgebiete if topic_id in a.search_topics), "?"
    )


def _de(text: Any) -> str:
    """German text of a localized value, else English, else whatever is there."""
    if not text:
        return ""
    return text.get("de") or text.get("en") or next(iter(text.values()))


def _date(value: Any) -> str:
    return value.strftime("%d.%m.%Y")


def _criteria(topic: SearchTopic) -> dict[str, Criterion]:
    return {c.id: c for c in topic.criteria}


def _reason(topic: SearchTopic, ids: Sequence[str]) -> str:
    criteria = _criteria(topic)
    return "; ".join(f"{c} ({_de(criteria[c].text)})" if c in criteria else c for c in ids)


def _citation(record: Any) -> str:
    authors = list(record.authors)
    who = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    parts = [p for p in (who, record.title.rstrip("."), record.journal, record.year) if p]
    text = ". ".join(str(p) for p in parts) + "."
    if record.doi:
        text += f" DOI: {record.doi}."
    if record.pmid:
        text += f" PMID: {record.pmid}."
    return text


def _csv(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def _csv_file(path: str, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> RenderedFile:
    return RenderedFile(path=path, media_type="text/csv; charset=utf-8", data=_csv(header, rows))


def _topics(review: Review) -> list[tuple[int, SearchTopic]]:
    return list(enumerate(review.document.search_topics, start=1))


# Anhang 4-A and 4-B: the search strategies.


def _strategies(review: Review, kind: str, annex: str, title: str) -> list[RenderedFile]:
    lines = [f"# Anhang {annex}: {title}", ""]
    rows: list[list[Any]] = []
    for n, topic in _topics(review):
        runs = [
            r
            for r in review.searches
            if r.execution.plan.topic_id == topic.id and r.source_kind == kind
        ]
        lines += [
            f"## Anhang {annex}{n}: Recherche {topic.id}, {_de(topic.label)}",
            "",
            f"Anwendungsgebiet {_area(review, topic.id)}.",
            "",
        ]
        if not runs:
            lines += ["Für diese Recherche ist keine Suche dieser Art erfasst.", ""]
        for run in runs:
            e, plan = run.execution, run.execution.plan
            name = run.source.name if run.source else plan.platform
            limits = "; ".join(plan.limits) or topic.source_plan.limits or "keine"
            strategy = [s for s in plan.strategy.splitlines() if s.strip()] or [plan.strategy]
            lines += [
                f"### {name}",
                "",
                md_table(
                    ["Angabe", "Wert"],
                    [
                        ["Datenbankname" if kind == "bibliographic" else "Studienregister", name],
                        ["Suchoberfläche", plan.platform],
                        ["Datum der Suche", _date(e.started_at)],
                        ["Einschränkungen", limits],
                        [
                            "Suchfilter",
                            (run.source.study_filter if run.source else None) or "keiner",
                        ],
                        ["Treffer", e.reported_count],
                    ],
                ),
                md_table(
                    ["#", "Suchbegriffe", "Ergebnis"],
                    [
                        [i, line, e.reported_count if i == len(strategy) else "nicht erfasst"]
                        for i, line in enumerate(strategy, start=1)
                    ],
                ),
            ]
            if e.query_translation:
                lines += [
                    "Übersetzung der Suchoberfläche:",
                    "",
                    "```text",
                    e.query_translation,
                    "```",
                    "",
                ]
            rows.append(
                [
                    f"{annex}{n}",
                    topic.id,
                    name,
                    plan.platform,
                    _date(e.started_at),
                    limits,
                    plan.strategy,
                    e.reported_count,
                    e.retrieved_count,
                    str(e.id),
                ]
            )
    slug = safe_name(annex)
    return [
        text_file(f"{slug}.md", "\n".join(lines)),
        _csv_file(
            f"{slug}.csv",
            [
                "Anhang",
                "Recherche",
                "Quelle",
                "Suchoberfläche",
                "Datum",
                "Einschränkungen",
                "Suchstrategie",
                "Treffer",
                "Abgerufen",
                "Ausführung",
            ],
            rows,
        ),
    ]


def anhang_4a(review: Review, _scope: str | None) -> list[RenderedFile]:
    return _strategies(
        review, "bibliographic", "4-A", "Suchstrategien der bibliografischen Literaturrecherche"
    )


def anhang_4b(review: Review, _scope: str | None) -> list[RenderedFile]:
    return _strategies(review, "registry", "4-B", "Suchstrategien der Suche in Studienregistern")


# Anhang 4-C and 4-D: exclusions with reasons.


def _registry_reports(review: Review) -> set[str]:
    if review.identity is None:
        return set()
    by_occurrence = review.records_by_occurrence()
    kinds: dict[str, set[str]] = {}
    for link in review.identity.links:
        if (found := by_occurrence.get(link.occurrence_id)) is not None:
            kinds.setdefault(str(link.report_id), set()).add(found[0].source_kind)
    return {r for r, k in kinds.items() if k == {"registry"}}


def _exclusions(
    review: Review, topic: SearchTopic, keep: Callable[[StageResult, ItemResult], bool]
) -> list[tuple[str, StageResult, ItemResult]]:
    """(scope, stage, item) for every excluded item the filter keeps, in protocol order."""
    found = []
    for scope in topic.scopes:
        for stage in ("title_abstract", "full_text"):
            s = review.stage(scope.id, stage, topic.id)  # type: ignore[arg-type]
            if s is None:
                continue
            found += [(scope.id, s, i) for i in s.records if i.outcome == "exclude" and keep(s, i)]
    return found


def _exclusion_files(
    review: Review,
    annex: str,
    title: str,
    keep: Callable[[StageResult, ItemResult], bool],
    registry: bool,
) -> list[RenderedFile]:
    records = {str(k): v for k, v in representative(review).items()}
    lines = [f"# Anhang {annex}: {title}", ""]
    rows: list[list[Any]] = []
    for n, topic in _topics(review):
        excluded = _exclusions(review, topic, keep)
        lines += [f"## Anhang {annex}{n}: Recherche {topic.id}, {_de(topic.label)}", ""]
        if not excluded:
            lines += ["Keine ausgeschlossenen Dokumente.", ""]
            continue
        table = []
        for i, (scope, stage, item) in enumerate(excluded, start=1):
            record = records.get(item.item_ref)
            reason = _reason(topic, item.reason_criterion_ids)
            if registry:
                ident = record.provider_record_id if record else item.item_ref
                ids = ", ".join(record.registry_ids) if record and record.registry_ids else ident
                row = [i, ids, item.title, scope, STAGE_DE[stage.stage], reason]
            else:
                row = [i, _citation(record) if record else item.title, scope, reason]
            table.append(row)
            rows.append([f"{annex}{n}", topic.id, *row[1:], item.item_ref])
        header = (
            ["Nr.", "Register-ID", "Titel", "Teilpopulation", "Stufe", "Ausschlussgrund"]
            if registry
            else ["Nr.", "Dokument", "Teilpopulation", "Ausschlussgrund"]
        )
        lines.append(md_table(header, table))
    csv_header = (
        [
            "Anhang",
            "Recherche",
            "Register-ID",
            "Titel",
            "Teilpopulation",
            "Stufe",
            "Ausschlussgrund",
            "Kittum-Report",
        ]
        if registry
        else [
            "Anhang",
            "Recherche",
            "Dokument",
            "Teilpopulation",
            "Ausschlussgrund",
            "Kittum-Report",
        ]
    )
    slug = safe_name(annex)
    return [text_file(f"{slug}.md", "\n".join(lines)), _csv_file(f"{slug}.csv", csv_header, rows)]


def anhang_4c(review: Review, _scope: str | None) -> list[RenderedFile]:
    registry = _registry_reports(review)
    return _exclusion_files(
        review,
        "4-C",
        "Liste der im Volltext gesichteten und ausgeschlossenen Dokumente mit Ausschlussgrund",
        lambda s, i: s.stage == "full_text" and i.item_ref not in registry,
        registry=False,
    )


def anhang_4d(review: Review, _scope: str | None) -> list[RenderedFile]:
    registry = _registry_reports(review)
    return _exclusion_files(
        review,
        "4-D",
        "Liste der beim Screening ausgeschlossenen Studien aus Studienregistern mit "
        "Ausschlussgrund",
        lambda _s, i: i.item_ref in registry,
        registry=True,
    )


# §4.2.3.5: how studies were selected.


def _kappa(value: float | None) -> str:
    return "nicht definiert" if value is None else f"{value:.2f}".replace(".", ",")


def _stage_text(review: Review, stage: StageResult) -> list[str]:
    records = stage.records
    excluded = [i for i in records if i.outcome == "exclude"]
    adjudicated = sum(1 for i in records for c in i.genuine if c.state == "resolved")
    humans = [a for a in stage.agreement if a.kind == "human_human"]
    judge = next((j for j in review.judges if j.id == stage.judge_config_id), None)
    model = (
        f"{judge.recipe.get('repository')} (Revision {judge.recipe.get('revision')}, Gewichte "
        f"SHA-256 {judge.weights_hash[:12]}…)"
        if judge
        else stage.judge_config_id
    )
    lines = [
        f"**{STAGE_DE[stage.stage]}, Teilpopulation {stage.scope_id}.** "
        f"{len(records)} Dokumente; "
        f"{PEOPLE_DE.get(len(stage.screeners), f'{len(stage.screeners)} Personen')} "
        f"{'bewertete' if len(stage.screeners) == 1 else 'bewerteten'} jedes Dokument unabhängig, ohne Kenntnis anderer Bewertungen und vor jeder "
        f"Anzeige der KI-Bewertung. Das KI-Modell {model} bewertete jedes Dokument als zusätzliche "
        f"Instanz; es konnte kein Dokument ausschließen.",
        "",
        f"- Ausgeschlossen: {len(excluded)}; davon mit Ausschlussvorschlag der KI: "
        f"{sum(1 for i in excluded if i.ai_excludes)}; allein durch die KI: 0.",
        f"- Abweichungen zwischen Personen, die eine weitere Person entschieden hat: "
        f"{adjudicated}.",
        f"- Zusätzliche verblindete Sichtung durch eine dritte Person, weil nur die KI abwich: "
        f"{stage.rescreens.triggered}; Entscheidung bestätigt: {stage.rescreens.upheld}; "
        f"abweichend und entschieden: {stage.rescreens.adjudicated}; offen: "
        f"{stage.rescreens.pending}.",
    ]
    for a in humans:
        lines.append(
            f"- Übereinstimmung der zwei Personen vor Auflösung: Cohens Kappa {_kappa(a.kappa)}, "
            f"PABAK {_kappa(a.pabak)} ({a.items} Dokumente)."
        )
    audit = stage.audit
    lines.append(
        f"- Anzeige der KI-Bewertung nach der eigenen Entscheidung: "
        f"{REVEAL_DE[stage.reveal]}; angezeigt {stage.reveals_shown}-mal, danach "
        f"geändert {stage.revised_after_reveal}-mal."
    )
    if audit.sentinels:
        lines.append(
            f"- Kontrolldokumente mit bekannter Bewertung: {audit.sentinels_correct} von "
            f"{audit.sentinels_decided} richtig bewertet."
        )
    if audit.audit_samples:
        elusion = (
            f"; geschätzte übersehene relevante Dokumente {audit.elusion.estimate:.1f} "
            f"(obere 95-%-Grenze {audit.elusion.upper:.1f})"
            if audit.elusion
            else ""
        )
        lines.append(
            f"- Zufallsstichprobe der Ausschlüsse: {audit.audit_decided} von {audit.audit_samples} "
            f"geprüft, {audit.audit_relevant_found} relevante gefunden{elusion}."
        )
    return [*lines, ""]


def justification_4235(review: Review, _scope: str | None) -> list[RenderedFile]:
    protocol = review.document
    lines = [
        "# Abschnitt 4.2.3.5: Selektion relevanter Studien",
        "",
        f"Aus dem Protokoll (Version {protocol.version_label}, Hash "
        f"{review.protocol.version.content_hash[:12] if review.protocol else '-'}…) und dem "
        f"Audit-Protokoll bis Ereignis {review.cutoff_seq} erzeugt.",
        "",
    ]
    for _, topic in _topics(review):
        lines += [f"## Recherche {topic.id}: {_de(topic.label)}", ""]
        if topic.mode.value == "M1":
            lines += [
                "Die Selektion erfolgte in jedem Schritt durch zwei Personen unabhängig "
                "voneinander. Ein KI-Modell bewertete jedes Dokument zusätzlich als dritte, "
                "unabhängige Instanz: Ein Dokument, das die KI einschloss und beide Personen "
                "ausschlossen, sichtete eine dritte Person ein weiteres Mal, ohne Kenntnis der "
                "anderen Bewertungen; wich sie ab, entschied eine weitere Person. Ein von zwei Personen "
                "unabhängig durchgeführtes Vorgehen bedarf keiner Begründung nach 4.2.3.5.",
                "",
            ]
        else:
            people = (
                "eine Person bewertete"
                if topic.mode.value == "M2"
                else "zwei Personen bewerteten in der Reihenfolge der KI"
            )
            lines += [
                f"Abweichend von der Selektion durch zwei Personen wurde Modus {topic.mode.value} "
                f"gewählt: {people} die Dokumente, ein KI-Modell "
                + (
                    "ersetzte die zweite Person; jedes Dokument, bei dem KI und Person voneinander "
                    "abwichen, sichtete eine dritte Person verblindet ein weiteres Mal, und wich "
                    "sie ab, entschied eine weitere Person."
                    if topic.mode.value == "M2"
                    else "legte nur die Reihenfolge fest; das Screening endet erst nach einem vorab "
                    "festgelegten statistischen Test."
                ),
                "",
                f"Begründung laut Protokoll: {_de(topic.mode_rationale) or 'keine angegeben'}",
                "",
                f"Validierung: {topic.validation_pack or 'kein Validierungspaket angegeben'}.",
                "",
            ]
        plan = topic.audit
        lines += [
            f"Prüfplan: Kontrolldokumente {plan.sentinel_percent} %, Zufallsstichprobe der "
            f"Ausschlüsse {plan.random_audit_percent} %, Vollerhebung der Abweichungen zwischen "
            f"KI und Person: {'ja' if plan.census_of_disagreements else 'nein'}.",
            "",
        ]
        if topic.stopping:
            lines += [
                f"Abbruchregel: {topic.stopping.method}, Ziel-Sensitivität "
                f"{topic.stopping.target_recall_percent} % bei {topic.stopping.confidence_percent} % "
                f"Konfidenz, mindestens {topic.stopping.minimum_screened_percent} % gesichtet.",
                "",
            ]
        stages = [s for s in review.stages if s.topic_id == topic.id]
        if not stages:
            lines += ["Für diese Recherche ist noch kein Screening erfasst.", ""]
        for stage in stages:
            lines += _stage_text(review, stage)
    return [text_file("begruendung_4-2-3-5.md", "\n".join(lines))]


# Anlage II.1: the Modul 5 tree.


def anlage_ii_1(review: Review, _scope: str | None) -> list[RenderedFile]:
    layout = ANLAGE_II_1
    records = {str(k): v for k, v in representative(review).items()}
    report_of = review.identity.report_of() if review.identity else {}
    registry = _registry_reports(review)
    files: list[RenderedFile] = []

    def add(path: str, entries: list[dict[str, Any]]) -> None:
        files.append(
            RenderedFile(
                path=path, media_type="application/x-research-info-systems", data=ris_dumps(entries)
            )
        )

    def entries(items: Sequence[ItemResult], note: str) -> list[dict[str, Any]]:
        out = []
        for item in items:
            record = records.get(item.item_ref)
            if record is not None:
                out.append(ris_entry(record, registry=item.item_ref in registry, notes=[note]))
        return out

    for _, topic in _topics(review):
        area = f"{layout['root']}/{layout['area'].format(code=_area(review, topic.id))}"
        runs = [r for r in review.searches if r.execution.plan.topic_id == topic.id]
        for n, run in enumerate(runs, start=1):
            name = layout["search_file"].format(
                topic=safe_name(topic.id),
                n=n,
                source=safe_name(run.source.id if run.source else run.execution.plan.source),
                date=run.execution.started_at.date().isoformat(),
            )
            add(
                f"{area}/{layout['searches']}/{name}",
                [
                    ris_entry(
                        r,
                        registry=run.source_kind == "registry",
                        report_id=report_of.get(r.occurrence_id),
                    )
                    for r in sorted(run.execution.records, key=lambda r: r.ordinal)
                ],
            )
        included: dict[str, ItemResult] = {}
        excluded_ft: dict[str, ItemResult] = {}
        excluded_registry: dict[str, ItemResult] = {}
        for scope in topic.scopes:
            ft = review.stage(scope.id, "full_text", topic.id)
            for i in ft.records if ft else ():
                if i.outcome == "include":
                    included.setdefault(i.item_ref, i)
        for _, s, i in _exclusions(review, topic, lambda _s, _i: True):
            if s.stage == "full_text" and i.item_ref not in registry:
                excluded_ft.setdefault(i.item_ref, i)
            if i.item_ref in registry:
                excluded_registry.setdefault(i.item_ref, i)
        t = safe_name(topic.id)
        lists = f"{area}/{layout['lists']}"
        add(
            f"{lists}/{layout['included'].format(topic=t)}",
            entries(list(included.values()), "eingeschlossen"),
        )
        add(
            f"{lists}/{layout['excluded_full_text'].format(topic=t)}",
            entries(list(excluded_ft.values()), "Anhang 4-C"),
        )
        add(
            f"{lists}/{layout['excluded_registry'].format(topic=t)}",
            entries(list(excluded_registry.values()), "Anhang 4-D"),
        )
    index = [[f.path, sha256_hex(f.data), f.media_type] for f in files]
    return [
        *files,
        text_file("GAPS.md", GAPS),
        _csv_file("index.csv", ["Pfad", "SHA-256", "Medientyp"], index),
    ]


TARGETS: dict[str, Callable[[Review, str | None], list[RenderedFile]]] = {
    f"{PROFILE_ID}.anhang_4a": anhang_4a,
    f"{PROFILE_ID}.anhang_4b": anhang_4b,
    f"{PROFILE_ID}.anhang_4c": anhang_4c,
    f"{PROFILE_ID}.anhang_4d": anhang_4d,
    f"{PROFILE_ID}.justification_4235": justification_4235,
    f"{PROFILE_ID}.anlage_ii_1": anlage_ii_1,
}


def register_targets() -> None:
    """Register the profile's report targets; safe to call again."""
    known = set(targets())
    for target, renderer in TARGETS.items():
        if target not in known:
            register_target(target, renderer)
