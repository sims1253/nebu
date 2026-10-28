# Standards and regulation

This page maps the standards and laws that apply to any AI-assisted systematic review to the parts
of Kittum that meet them. Requirements of a particular agency or jurisdiction live in its
[profile](../DESIGN.md#profiles), and each profile's doc maps them the same way.

The status column says how each fact was confirmed:

- **Read** means a primary document was read in October 2026.
- **Reported** means a council member cited it and nobody rechecked it.

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
| RoB 2 and ROBINS-I V2 (released 2025) | Current risk-of-bias instruments for randomized and non-randomized studies | Core appraisal instruments, forms as data | Reported |
| GRADE | Current | Not modelled in the PoC. A profile can add it. | Reported |

## Law and regulation

| Item | What applies | Kittum | Status |
| --- | --- | --- | --- |
| EU AI Act, Regulation (EU) 2024/1689 | Not high-risk: Kittum is not in Annex III, not a medical device, and not a safety component. Article 4 AI literacy applies to the firm. Article 50(4) is avoided because humans take editorial responsibility. | Training records; disclosure; human sign-off events | Reported. Write a documented legal assessment. |
| Digital Omnibus on AI, Regulation (EU) 2026/1744 | In force since 27.07.2026. Annex III obligations moved to 02.12.2027. AI literacy kept. | No change for a system that is not high-risk | Read |
| GDPR | CSRs hold pseudonymized health data (Article 9). The firm acts as processor for the client (Article 28). Author names in records are low-risk personal data. | Confidentiality tiers; local inference by default; DPIA; minimal CSR intake | Reported |
| Trade secrets law (in Germany the GeschGehG) and client NDAs | Often stricter than GDPR for CSRs | Access control; no external model for CSRs without sign-off | Reported |
| DSM Directive Article 4 (in Germany §44b UrhG) | Commercial text and data mining is allowed unless rights are reserved in machine-readable form. Publisher licences can restrict it further. | Licence recorded per acquisition | Reported. Check per publisher. |

## Quality frameworks

These are not legally required for evidence synthesis, which is not GxP work. Pharma clients ask
for them in vendor audits.

| Framework | Use | Kittum |
| --- | --- | --- |
| GAMP 5, 2nd edition (2022), and the ISPE GAMP guide on AI (2025) | Risk-based validation | Validation pack: requirements, risks, regression evidence, release record |
| ISO/IEC 42001:2023 | AI management system | Model registry and ledger supply most of the evidence. Relevant for the product phase. |
| 21 CFR Part 11 and EU GMP Annex 11 | Electronic records and audit trails in GxP | Not required. The ledger follows their principles (attributable, contemporaneous, original records). |

Kittum must never describe its hash chain as certification or as GxP compliance.
