# Requirements and standards

This page maps the rules that govern an AMNOG literature review to the parts of Kittum that meet
them. The status column says how each fact was confirmed:

- **Read** means a primary document was read in October 2026. The G-BA templates were downloaded
  from [the G-BA Anlage II page](https://www.g-ba.de/richtlinien/anlage/167/).
- **Reported** means a council member cited it and nobody rechecked it.

## Binding documents

| Document | Version | Requirement | Kittum | Status |
| --- | --- | --- | --- | --- |
| G-BA Modul 4 template (Anlage II.6) | 18.11.2025 | Bibliographic search in at least MEDLINE (with in-process citations) and Cochrane CENTRAL | `search` source plan; `FileImport` for CENTRAL | Read |
| | | Registry search in at least ClinicalTrials.gov, EU-CTR, CTIS, and the EMA Clinical Data portal. Completed, terminated, and ongoing studies. | Registry adapters and imports | Read |
| | | Search the G-BA website for the drug, and for the zVT when indirect evidence is used. Document the *Vorgangsnummern*. | Assisted G-BA lookup recorded as an "other methods" execution | Read |
| | | One strategy per database, adapted to it, built in blocks (indication, intervention, study type). Validated study-type filters. All strategies in Anhang 4-A (bibliographic) and 4-B (registries). | `SearchExecution`; `report` renders 4-A and 4-B | Read |
| | | Search date at most three months before submission. An older EU-dossier search must be updated. | `FreshnessPolicy`; update search with delta screening | Read |
| | | §4.2.3.5: justify any selection not done by two people independently | Screening modes; `report` writes the justification from the ledger | Read |
| | | A flow diagram per search with hits, duplicates, title-and-abstract exclusions, full texts, full-text exclusions with reasons, and the number of individual studies | PRISMA flow per scope from a frozen identity revision | Read |
| | | Anhang 4-C lists full-text exclusions with reasons. Anhang 4-D lists excluded registry entries with reasons. | Reasons are protocol criterion IDs | Read |
| | | Anhang 4-E holds the methods of included RCTs | `report` from extraction | Read |
| | | Risk of bias rated *niedrig* or *hoch* at study level (Tabelle 4-9) and per endpoint (Tabelle 4-12, and Tabelle 4-19 for indirect comparisons). One *Bewertungsbogen* per study in Anhang 4-F. | `appraisal` forms as data; human judgement | Read |
| | | Tabelle 4-1 (the company's own study list) and Tabelle 4-3 (registry hits, flagged as in the company list or found by the bibliographic search) | Cross-flags from `corpus` identities | Read |
| | | Sections can reference the EU dossier, but Modul 4 tables must still be filled | Two report targets from one evidence base | Read |
| G-BA Anlage II.1, dossier preparation and submission | 08.04.2026 | Exact directory tree and file names for Modul 5. RIS files for all reference lists and every search. Machine-readable, unprotected PDFs. | `report` target for the Anlage II.1 tree | Reported, file located |
| G-BA Verfahrensordnung, chapter 5 | Amendments of 17.07.2025 (EU HTA) and 20.11.2025 | Rules for referencing the EU dossier | `report` | Read (change list) |
| IQWiG Allgemeine Methoden | v8.0, 19.12.2025 | §9.1.1 F: every selection step by two people independently | Mode M1 by default | Reported |
| | | AI in study selection only with evidence of suitability against RAISE | Validation pack; M2 needs pooled evidence | Reported |
| | | §9.1.1 C: no preprint servers by default | Preprints supplementary only | Reported |
| | | Objective search approach with development and validation reference sets; PRESS peer review | `ReferenceSet` in the protocol; search review screen | Reported |
| | | §9.2.2: IQWiG checks completeness, reruns searches, and spot-checks excluded citations | Every exclusion carries a criterion, a quote, and a reviewer | Reported |
| | | §9.6: handling of untrustworthy, retracted, or fabricated studies | Retraction check from Crossref and PubMed during enrichment | Reported |
| | | Certainty of evidence as *Beleg*, *Hinweis*, *Anhaltspunkt*. Endpoint categories mortality, morbidity, quality of life, adverse events. | Protocol endpoint catalogue; report vocabulary | Reported |

## EU HTA Regulation

| Item | Requirement | Kittum | Status |
| --- | --- | --- | --- |
| Regulation (EU) 2021/2282 | Joint clinical assessments since 12.01.2025 for oncology drugs and ATMPs, orphan drugs from 2028, all drugs from 2030 | JCA as a second report target | Read |
| Implementing Regulation (EU) 2024/1381 | JCA dossier template | `report` target | Reported |
| PICO scoping | Several consolidated PICOs per assessment, often many, often late | Decisions keyed by scope; delta screening for new PICOs | Reported |
| Appraisal | RoB 2 and ROBINS-I V2 (released 2025) | Appraisal forms as data, rendered per instrument | Reported |
| First JCA report | Approved 30.04.2026 (tovorafenib) | None | Reported |

## Reporting and method guidance

| Guidance | Status in 2026 | Kittum | Status |
| --- | --- | --- | --- |
| PRISMA 2020 | Current. Its flow diagram has a slot for records that automation tools mark ineligible. | Flow per scope, generated | Reported |
| PRISMA-S (2021) | Current. 16 items for search reporting. | Fields of `SearchExecution` | Reported |
| PRESS 2015 | Current | Checklist on the search review screen | Reported |
| PRISMA-trAIce (JMIR AI 2025) | A proposal. The PRISMA Executive published "PRISMA-trAIce: A Name Without Endorsement" (JMIR AI 2026;5:e104210). | One rendering of the AI disclosure, not the standard | Read |
| RAISE, with the joint position statement of Cochrane, Campbell, JBI, and CEE (2025) | Version 4 released in March 2026, per the Cochrane methods report. Users stay accountable. Tool developers publish evaluations. | Public evaluation record from the regression suite; AI disclosure | Read |
| GAMER (2025) and TRIPOD-LLM (2025) | Reporting guidance for LLM use | Optional disclosure renderings | Reported |
| Cochrane Handbook | 6.5 (2024), 6.5.1 (2025) | Method reference | Reported |
| GRADE | Not used by IQWiG. Optional for JCA. | Not modelled in the PoC | Reported |

## Law and regulation

| Item | What applies | Kittum | Status |
| --- | --- | --- | --- |
| EU AI Act, Regulation (EU) 2024/1689 | Not high-risk: Kittum is not in Annex III, not a medical device, and not a safety component. Article 4 AI literacy applies to the firm. Article 50(4) is avoided because humans take editorial responsibility. | Training records; disclosure; human sign-off events | Reported. Write a documented legal assessment. |
| Digital Omnibus on AI, Regulation (EU) 2026/1744 | In force since 27.07.2026. Annex III obligations moved to 02.12.2027. AI literacy kept. | No change for a system that is not high-risk | Read |
| GDPR | CSRs hold pseudonymized health data (Article 9). The firm acts as processor for the client (Article 28). Author names in records are low-risk personal data. | Confidentiality tiers; local inference by default; DPIA; minimal CSR intake | Reported |
| German trade secrets act (GeschGehG) and client NDAs | Often stricter than GDPR for CSRs | Access control; no external model for CSRs without sign-off | Reported |
| DSM Directive Article 4 and §44b UrhG | Commercial text and data mining is allowed unless rights are reserved in machine-readable form. Publisher licences can restrict it further. | Licence recorded per acquisition | Reported. Check per publisher. |

## Quality frameworks

These are not legally required, because dossier preparation is not GxP work. Pharma clients ask for
them in vendor audits.

| Framework | Use | Kittum |
| --- | --- | --- |
| GAMP 5, 2nd edition (2022), and the ISPE GAMP guide on AI (2025) | Risk-based validation | Validation pack: requirements, risks, regression evidence, release record |
| ISO/IEC 42001:2023 | AI management system | Model registry and ledger supply most of the evidence. Relevant for the product phase. |
| 21 CFR Part 11 and EU GMP Annex 11 | Electronic records and audit trails in GxP | Not required. The ledger follows their principles (attributable, contemporaneous, original records). |

Kittum must never describe its hash chain as certification or as GxP compliance.
