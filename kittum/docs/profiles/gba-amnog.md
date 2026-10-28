# Profile: G-BA benefit dossier (AMNOG)

Profile ID `gba_amnog`, package `kittum.profiles.gba_amnog`.

This profile adapts Kittum to the systematic literature review inside a German benefit dossier
under AMNOG, and to the joint clinical assessments (JCA) of the EU HTA Regulation that feed into
it. Everything on this page is specific to that setting. The core it builds on is described in
[DESIGN.md](../../DESIGN.md); [Profiles](../../DESIGN.md#profiles) explains what a profile can
supply and what the core guarantees regardless.

## The setting in one page

A pharmaceutical company that launches a drug in Germany submits a dossier to the Federal Joint
Committee (G-BA). The Institute for Quality and Efficiency in Health Care (IQWiG) then assesses it.
Modul 4 of the dossier must show the complete evidence for the drug against the comparator the
G-BA chose, the *zweckmäßige Vergleichstherapie* (zVT). Completeness is checked: IQWiG reruns
searches and spot-checks the studies the company excluded. A missed study can sink the assessment.

The review follows the core shape, with these specifics:

1. The protocol states PICO per subpopulation. One dossier can have several *Anwendungsgebiete*
   (therapeutic indications), each with *Teilpopulationen* (subpopulations) and its own zVT.
2. A search specialist runs one Boolean search per database. The minimum is MEDLINE and Cochrane
   CENTRAL, plus the registries ClinicalTrials.gov, EU-CTR, CTIS, and the EMA Clinical Data portal.
3. Two people screen titles and abstracts independently, then full texts. They resolve
   disagreements and record a reason for every full-text exclusion.
4. Records are grouped into studies. Modul 4 asks how many studies the relevant records belong to.
5. Reviewers extract results per study, endpoint, Teilpopulation, arm, and timepoint, and assess
   risk of bias on the G-BA form.
6. The output is the Modul 4 text and tables, Anhänge 4-A to 4-F, and RIS files in a directory tree
   whose names Anlage II.1 fixes.

Searches must be at most three months old at submission, so every review ends with an update
search. Since January 2025 the EU HTA Regulation adds joint clinical assessments. These arrive with
several PICOs that member states consolidate, often late.

### What this means for the core claims

- **Recall cannot be certified statistically at AMNOG scale.** Most search topics have tens of
  relevant records, so the core's recall arithmetic (10 of 10 found gives a one-sided 95% lower
  bound of 74%) applies directly. The honest pitch for AI screening here is that it finds studies
  humans miss. It is not that it cuts the human reading load on submission-critical topics.
  Workload savings are real only for large searches, such as indirect comparisons or JCA sweeps.
- **Mode M3 is rare.** It needs roughly 10,000 records and 100 expected relevant records per topic.
- **JCA PICOs are scopes.** When a new JCA PICO arrives, Kittum evaluates only the new criteria
  over the screened corpus. It does not rescreen everything.
- **Study-level recall is what IQWiG checks.** That is why the core makes it the primary metric.

## What the profile supplies

| Profile part | Content |
| --- | --- |
| Protocol schema extensions | `Dossier`, `Anwendungsgebiet`, `Teilpopulation`, `zVT`, JCA PICO links, the IQWiG endpoint categories |
| Vocabularies | Endpoint categories *Mortalität*, *Morbidität*, *Lebensqualität*, *Nebenwirkungen*; certainty of evidence *Beleg*, *Hinweis*, *Anhaltspunkt*; risk of bias *niedrig* and *hoch* |
| Validation and method rules | Search freshness of three months; the Modul 4 source minimum; zVT consistency with Modul 3; a written §4.2.3.5 justification for any mode other than M1; no routine preprint-server search; WHO ICTRP only for non-drug assessments |
| Labels for core concepts | Scope as *Teilpopulation* or JCA PICO; source plan items named as in Modul 4; exclusion reasons rendered as Anhang 4-C and 4-D reasons |
| Report targets | `gba_amnog.modul4`, `gba_amnog.anlage_ii_1`, `gba_amnog.justification_4235`, `gba_amnog.jca` |
| Appraisal instruments | `gba_amnog.bewertungsbogen` (the Anhang 4-F form), plus the core RoB 2 and ROBINS-I V2 for JCA |
| Other-methods searches | The assisted G-BA website lookup |
| Evaluation data | The G2 tier: public G-BA dossiers and IQWiG assessments |

## Protocol extensions

The profile wraps the core protocol in a dossier hierarchy. Each `Teilpopulation` and each JCA PICO
is a core scope; decisions are keyed by it.

```text
Dossier(active_substance, trade_name, version)
	Anwendungsgebiet(code)
		Teilpopulation(id, definition, G-BA consultation reference)    # a core scope
			zVT(options, "nach ärztlicher Maßgabe" flag, source document, qualifying comparator arms)
			jca_pico_links[]
		Endpoint(category: Mortalität | Morbidität | Lebensqualität | Nebenwirkungen,
			operationalization, patient-relevance rationale, surrogate validation reference)
		EffectModifier[] (default: sex, age, disease severity, centre or country)
		SearchTopic(kind: RCT with the drug | RCT for indirect comparison | non-randomized | other)
			Criterion[] (core)
			SourcePlan (core, plus the G-BA website search)
			ReferenceSet (core)
			ScreeningMode (core) with AuditPlan and StoppingPlan
			FreshnessPolicy (maximum age three months before submission)
```

Criteria carry German and English text (`text_de`, `text_en`), because the Modul 4 text is German
and most source documents are English.

`protocol.compile` under this profile also checks zVT consistency with Modul 3.

## Search and sources

The Modul 4 template (Anlage II.6, 18.11.2025) sets the minimum:

- Bibliographic databases: MEDLINE, including in-process citations, and Cochrane CENTRAL. Further
  databases such as Embase are optional, and in practice expected.
- Registries: ClinicalTrials.gov, EU-CTR, CTIS, and the EMA Clinical Data portal. For EMA Clinical
  Data, a lookup of studies already identified is enough, with no strategy documentation.
- The G-BA website, searched by active substance and by zVT, with the *Vorgangsnummern*
  (procedure numbers) documented.

CENTRAL, EU-CTR, and CTIS have no free search interface, so the free proof of concept cannot meet
the Modul 4 minimum without the core `FileImport` adapter.

| Source | Why it matters here | Kittum adapter |
| --- | --- | --- |
| Cochrane CENTRAL (Wiley) | Modul 4 minimum | `FileImport` |
| EU-CTR | Modul 4 minimum | `FileImport` |
| CTIS | Modul 4 minimum | `FileImport` |
| EMA Clinical Data portal | Required for results of identified studies | A per-study lookup |
| G-BA website | Required | A semi-manual lookup by active substance and zVT that captures the documents, recorded as an `other_methods` execution |
| WHO ICTRP | Required by IQWiG only for non-drug assessments | `FileImport` |

IQWiG's methods (v8.0, §9.1.1 C) exclude routine searches of preprint servers. Under this profile
there is no preprint-server `SearchExecution` by default. Preprints found another way (citation
chasing, Europe PMC, the company's study list) still go through normal screening and quality
assessment.

Anhang 4-A counts all hits per database and Anhang 4-D requires an undeduplicated RIS file. The core
keeps every source occurrence after merging, which makes both possible.

## Screening modes and the §4.2.3.5 justification

IQWiG's methods (v8.0, §9.1.1 F) require two people to perform every selection step
independently. Modul 4 §4.2.3.5 asks the company to justify any other procedure. IQWiG v8.0 also
states for the first time that AI in study selection is possible, if the tool's suitability is
shown against RAISE.

So under this profile:

- M1 is the default and the expected mode for every submission-critical topic.
- Any topic in M2 or M3 needs a written justification in the protocol, and M2 needs a validation
  pack with pooled gold data.
- The `gba_amnog.justification_4235` target writes the §4.2.3.5 text from the ledger: the declared
  mode, the audit plan, the audit result, and the validation evidence.

IQWiG excludes at title and abstract only records that are certainly irrelevant. The core rule that
`unclear` means include at title and abstract matches this.

## Appraisal

Modul 4 rates risk of bias as *niedrig* (low) or *hoch* (high), at study level (Tabelle 4-9) and per
endpoint (Tabelle 4-12, and Tabelle 4-19 for indirect comparisons). Anhang 4-F holds one
*Bewertungsbogen* (assessment form) per study. JCA uses RoB 2 and ROBINS-I V2. The profile registers
the Bewertungsbogen as an instrument; the core renders the same human answers onto every instrument
the dossier needs.

Certainty of evidence follows IQWiG's scale of *Beleg*, *Hinweis*, and *Anhaltspunkt* (proof,
indication, hint), not GRADE. GRADE is optional for JCA and is not modelled in the PoC.

## Reports

| Target | Content |
| --- | --- |
| `gba_amnog.modul4` | Modul 4 sections 4.2 and 4.3: the method text and the results tables per endpoint and subgroup. Anhang 4-A (bibliographic strategies), 4-B (registry strategies), 4-C (full-text exclusions with reasons), 4-D (excluded registry entries with reasons), 4-E (methods of included RCTs), and 4-F (risk-of-bias forms). Tabelle 4-1 (the company's own study list) and Tabelle 4-3 (registry hits, flagged as in the company list or found by the bibliographic search). The flow diagram as Abbildung 1 in section 4.3. |
| `gba_amnog.anlage_ii_1` | The Anlage II.1 directory tree for Modul 5 with its exact file names, RIS files for every reference list and every search, and machine-readable, unprotected PDFs |
| `gba_amnog.justification_4235` | The §4.2.3.5 justification for the screening mode |
| `gba_amnog.jca` | The JCA dossier sections (Implementing Regulation (EU) 2024/1381) from the same evidence base |

Exclusion reasons are protocol criterion IDs, so Anhang 4-C and 4-D reasons come from data instead
of free text. Sections can reference the EU dossier, but Modul 4 tables must still be filled, so the
Modul 4 and JCA targets render from one evidence base.

In the handover bundle, `reports/dossier/` holds the Anlage II.1 tree and the annexes beside the core PRISMA
reports and AI disclosure. The bundle's acceptance test reads: pick any cell in an exported Modul 4
table and trace it to the accepted value, its PDF regions, and the human approval.

Anlage II.1 allows leaving out CSR patient listings, so Kittum ingests only the CSR core report and
section 14 tables.

## Evaluation data

| Tier | Gold data | Strength | Weakness |
| --- | --- | --- | --- |
| G1 | The firm's past dossiers with per-record screening logs | Exact domain | Only if the firm kept logs. Client confidentiality applies. |
| G2 | Public G-BA dossiers (Anhang 4-A strategies and counts, 4-C and 4-D exclusions) and IQWiG assessments. Studies that IQWiG found and the company missed are labelled hard positives. | Domain-matched, free, many topics | Searches can be reconstructed only approximately. Internal TDM use needs a legal check. |
| G3 | SYNERGY and CLEF eHealth TAR (core) | Statistical power | Not zVT-specific |

## Risks specific to this profile

| Risk | Likelihood and impact | Response |
| --- | --- | --- |
| IQWiG finds a study the AI-assisted process missed | Low, severe | M1 by default; study-level recall on hard positives; materiality review of every AI-only exclusion |
| The free PoC cannot reach CENTRAL, Embase, or CTIS | Certain, low | `FileImport` from day one |
| JCA PICOs arrive late and multiply | High, high | Scope-keyed decisions and delta screening |

## Two-week spike under this profile

The core spike uses one search topic from a completed dossier, ideally one where IQWiG found a
study the company missed. Under this profile the spike also produces:

- Days 3 to 4: `FileImport` for the original dossier's CENTRAL, Embase, and CTIS exports, and the
  Anlage II.1 RIS tree with exact names.
- Day 10: Anhang 4-A and 4-C drafts and the generated §4.2.3.5 text in the bundle.

## Requirements

This table maps the binding documents to Kittum. The status column says how each fact was
confirmed:

- **Read** means a primary document was read in October 2026. The G-BA templates were downloaded
  from [the G-BA Anlage II page](https://www.g-ba.de/richtlinien/anlage/167/).
- **Reported** means a council member cited it and nobody rechecked it.

Generic standards and law (PRISMA, RAISE, the EU AI Act, GDPR, and others) are in
[Standards](../standards.md).

### Binding documents

| Document | Version | Requirement | Kittum | Status |
| --- | --- | --- | --- | --- |
| G-BA Modul 4 template (Anlage II.6) | 18.11.2025 | Bibliographic search in at least MEDLINE (with in-process citations) and Cochrane CENTRAL | `search` source plan; `FileImport` for CENTRAL | Read |
| | | Registry search in at least ClinicalTrials.gov, EU-CTR, CTIS, and the EMA Clinical Data portal. Completed, terminated, and ongoing studies. | Registry adapters and imports | Read |
| | | Search the G-BA website for the drug, and for the zVT when indirect evidence is used. Document the *Vorgangsnummern*. | Assisted G-BA lookup recorded as an "other methods" execution | Read |
| | | One strategy per database, adapted to it, built in blocks (indication, intervention, study type). Validated study-type filters. All strategies in Anhang 4-A (bibliographic) and 4-B (registries). | `SearchExecution`; `gba_amnog.modul4` renders 4-A and 4-B | Read |
| | | Search date at most three months before submission. An older EU-dossier search must be updated. | `FreshnessPolicy`; update search with delta screening | Read |
| | | §4.2.3.5: justify any selection not done by two people independently | Screening modes; `gba_amnog.justification_4235` writes the justification from the ledger | Read |
| | | A flow diagram per search with hits, duplicates, title-and-abstract exclusions, full texts, full-text exclusions with reasons, and the number of individual studies | PRISMA flow per scope from a frozen identity revision | Read |
| | | Anhang 4-C lists full-text exclusions with reasons. Anhang 4-D lists excluded registry entries with reasons. | Reasons are protocol criterion IDs | Read |
| | | Anhang 4-E holds the methods of included RCTs | `gba_amnog.modul4` from extraction | Read |
| | | Risk of bias rated *niedrig* or *hoch* at study level (Tabelle 4-9) and per endpoint (Tabelle 4-12, and Tabelle 4-19 for indirect comparisons). One *Bewertungsbogen* per study in Anhang 4-F. | `appraisal` forms as data; human judgement | Read |
| | | Tabelle 4-1 (the company's own study list) and Tabelle 4-3 (registry hits, flagged as in the company list or found by the bibliographic search) | Cross-flags from `corpus` identities | Read |
| | | Sections can reference the EU dossier, but Modul 4 tables must still be filled | Two report targets from one evidence base | Read |
| G-BA Anlage II.1, dossier preparation and submission | 08.04.2026 | Exact directory tree and file names for Modul 5. RIS files for all reference lists and every search. Machine-readable, unprotected PDFs. | `gba_amnog.anlage_ii_1` target | Reported, file located |
| G-BA Verfahrensordnung, chapter 5 | Amendments of 17.07.2025 (EU HTA) and 20.11.2025 | Rules for referencing the EU dossier | `report` | Read (change list) |
| IQWiG Allgemeine Methoden | v8.0, 19.12.2025 | §9.1.1 F: every selection step by two people independently | Mode M1 by default | Reported |
| | | AI in study selection only with evidence of suitability against RAISE | Validation pack; M2 needs pooled evidence | Reported |
| | | §9.1.1 C: no routine search of preprint servers; preprints found otherwise are assessed | No preprint-server `SearchExecution` by default; preprints from other sources are screened and appraised normally | Reported |
| | | Objective search approach with development and validation reference sets; PRESS peer review | `ReferenceSet` in the protocol; search review screen | Reported |
| | | §9.2.2: IQWiG checks completeness, reruns searches, and spot-checks excluded citations | Every exclusion carries a criterion, a quote, and a reviewer | Reported |
| | | §9.6: handling of untrustworthy, retracted, or fabricated studies | Retraction check from Crossref and PubMed during enrichment | Reported |
| | | Certainty of evidence as *Beleg*, *Hinweis*, *Anhaltspunkt*. Endpoint categories mortality, morbidity, quality of life, adverse events. | Profile endpoint vocabulary; report vocabulary | Reported |
| GRADE | | Not used by IQWiG. Optional for JCA. | Not modelled in the PoC | Reported |

### EU HTA Regulation

| Item | Requirement | Kittum | Status |
| --- | --- | --- | --- |
| Regulation (EU) 2021/2282 | Joint clinical assessments since 12.01.2025 for oncology drugs and ATMPs, orphan drugs from 2028, all drugs from 2030 | JCA as a second report target | Read |
| Implementing Regulation (EU) 2024/1381 | JCA dossier template | `gba_amnog.jca` target | Reported |
| PICO scoping | Several consolidated PICOs per assessment, often many, often late | Decisions keyed by scope; delta screening for new PICOs | Reported |
| Appraisal | RoB 2 and ROBINS-I V2 (released 2025) | Appraisal forms as data, rendered per instrument | Reported |
| First JCA report | Approved 30.04.2026 (tovorafenib) | None | Reported |

The JCA parts may later move into their own `eu_jca` profile, so a JCA-only review does not need
the German dossier vocabulary. Until then they live here.

### Quality frameworks in this setting

Dossier preparation is not GxP work, so GAMP 5, ISO/IEC 42001, and 21 CFR Part 11 are not legally
required. Pharma clients ask for them in vendor audits; [Standards](../standards.md#quality-frameworks)
maps them.
