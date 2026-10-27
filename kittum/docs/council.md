# Council record

Four models reviewed the seed brief independently on 27.10.2026. Each covered the full design and
went deeper on one focus area. Claude Opus 5.5 merged the four reports into the design and checked
the disputed claims.

| Member | Focus area | Name pick |
| --- | --- | --- |
| GPT-6 Astra | Data model, provenance, audit ledger, reproducibility | Geshtinanna |
| Claude Opus 5.5 | Methodology, statistics, regulation | Edubba |
| Claude Fable 5.1 | Reviewer experience, naming | Kittum |
| Gemini 4 Argon | Sources, document processing, models | Kummu |

This was a design review, so nobody gave a score.

## Where all four agreed

- Correct the seed brief's unit of inclusion. The study is the unit, not the record. Linking records
  to studies is its own human-confirmed step.
- PyMuPDF makes Janus AGPL. Swap the reader to pypdfium2 and pdfplumber, then use Janus as a
  library. Superseded on 28.10.2026: Nebu, Kittum included, is now AGPL, so Janus keeps PyMuPDF.
- Use Postgres as the live store, content-addressed objects for files, and SQLite for the handover.
- Use Python for the backend and a TypeScript workbench like the one in Janus. Skip Rust and skip agent
  frameworks.
- Keep federation out of primary search. Federation is for enrichment only.
- Exclude MinerU, the marker weights, and the Nougat weights. Exclude Semantic Scholar from commercial use.
- Treat PRISMA-trAIce as unendorsed, and store the AI disclosure as data.

## Disagreements and how the design resolved them

| Question | Positions | Resolution |
| --- | --- | --- |
| How much screening can AI take over? | Astra: two humans on every record in release one. Opus: modes M1 to M3, with M1 as the default. Fable: levels L0 to L2, with L0 or L1 as the product. Gemini: AI exclusion after a unanimous ensemble vote plus an audit, aiming at 70% less human reading. | Modes M1 to M3 with M1 as the default. No mode lets AI exclude a record that no human saw. Gemini's 70% target fails on statistics: AMNOG topics have too few relevant records to certify it. |
| Keep the ranker cascade? | Opus, Fable, and Astra: no, the judge runs on every record. Gemini: keep a cross-encoder in front of a three-model ensemble on every record. | No ranker in the PoC. A ranker can return as an adapter for large topics, for queue order only. |
| How many judges? | Astra: one configuration first. Fable: a second and third judge only on uncertain records. Gemini: three families on everything. Opus: measure the double-fault rate before trusting an ensemble. | Two families on every record. A third family runs as an experiment and stays only if it finds relevant records the others miss. |
| When does a reviewer see AI output? | Fable: after committing, on disagreement only. Opus: after committing, plus a randomized 10% slice shown assisted-first. Gemini: show PICO entity chips before the decision and the ensemble verdict after it. Astra: delay the rationale, and measure anchoring. | Human first, AI on disagreement, provisional and final decisions logged. Protocol-derived highlights and PubTator entities may show before the decision, because they are not AI verdicts. The randomized slice is an option for measuring anchoring. |
| Pure event sourcing or relational revisions? | Fable and Gemini: an event-sourced ledger with projections. Astra: relational revisions with append-only events in the same transaction. | Astra's version. Screens query normal tables, and projections can still be rebuilt. |
| Licence rule | Seed brief: Apache-2.0 or MIT only. Astra: that rule excludes PostgreSQL and SQLite. | First an allowlist of permissive licences. Superseded on 28.10.2026: Kittum is AGPL-3.0-or-later, any open-source code licence is acceptable, and restrictive weights terms stay blocked. See [Dependencies](dependencies.md). |
| Name | Four different picks | Kittum. See below. |

## Name check

| Name | Meaning | Collision found | Outcome |
| --- | --- | --- | --- |
| Kittum | Akkadian "truth", a deity invoked as witness in legal texts | None in software. Near misses: KITRUM, Kittl. | **Picked** |
| Kummu | Reported as the inner tablet room of Nabu's temple Ezida | None in software. It is also a Finnish surname. The meaning was not confirmed against a primary source. | Runner-up |
| Nanshe | Sumerian goddess who checks weights and measures | nanshe.io, an AI document platform | Rejected |
| Edubba | Sumerian "tablet house", the scribal school | Eduba, an AI consulting and software company (eduba.io) | Rejected |
| Geshtinanna | Goddess linked with scribal arts and interpretation | None found | Rejected: long, and the link to scribal arts is uncertain |
| Kudurru | Boundary stone recording a witnessed grant | Spawning's Kudurru, an AI-scraper blocker | Rejected |
| Dubsar | Sumerian "scribe" | A GitHub Spec Kit extension and a WordNet app | Rejected |

Fable suggested sub-names: `tamitu` for the protocol, `baru` for the AI judge, and `sippar` for the
handover bundle. The design uses plain module names instead, because a reader should not need a
glossary to find the protocol code.

## Claims checked after the council

| Claim | Source of the claim | Result |
| --- | --- | --- |
| Modul 4 annexes are 4-A to 4-F, and 4-E is the methods of included RCTs | Opus | Confirmed in the Modul 4 template of 18.11.2025 |
| Risk of bias is rated low or high in Tabelle 4-23 and 4-24 | Gemini | Wrong numbers. The low-or-high rating is right. The tables are 4-9 (study level) and 4-12 and 4-19 (endpoint level), with forms in Anhang 4-F. |
| Registry minimum is ClinicalTrials.gov, EU-CTR, CTIS, and EMA Clinical Data | Opus | Confirmed |
| Searches at most three months old; §4.2.3.5 dual-selection justification | Opus | Confirmed |
| A flow diagram in "Anhang 4-G" | Gemini | Wrong. The flow diagram is Abbildung 1 inside section 4.3. There is no Anhang 4-G. |
| Gemma models have custom licence terms | Opus, Fable | Out of date. Gemma 4 moved to Apache-2.0 in March 2026. |
| RAISE version 4 released in March 2026 | Gemini | Confirmed by the Cochrane methods report 2026 |
| PRISMA Executive letter against PRISMA-trAIce | Opus, Fable, Gemini | Confirmed: JMIR AI 2026;5:e104210 |
| AI Omnibus, Regulation (EU) 2026/1744, in force 27.07.2026 | Opus | Confirmed. Annex III obligations moved to 02.12.2027. |
| MinerU moved to a custom licence with commercial thresholds | Astra, Gemini | Confirmed through a secondary summary. Read the licence file before relying on it. |
| Janus has a `DocumentReader` protocol and depends on PyMuPDF | Gemini, Fable | Confirmed in `janus/apps/server/src/janus/comparison/reader.py`. The tests import PyMuPDF too. |
| Recall bounds: 10 of 10 found gives a 69% lower bound; 119 relevant records with no misses are needed for a miss rate below 2.5% | Opus | Recomputed: 0.025^(1/10) = 0.692, and 0.975^119 = 0.049 |
