# Kittum

Kittum is a workbench for auditable, AI-assisted systematic literature reviews. It runs the review
from the protocol to the final reports. AI screens and extracts beside two human reviewers, and
every decision lands in a hash-chained ledger. The deliverable is the review output plus one
database file that holds the whole audit trail.

The core does not depend on any jurisdiction. The rules of a particular agency, such as the German
G-BA benefit dossier, come from an optional [profile](DESIGN.md#profiles).

Status: scaffold. Every module has its interface and the shared fakes; the work is split into
[work packages](docs/work-packages.md). [AGENTS.md](AGENTS.md) explains how to build, test, and
work on it in parallel.

## Documents

- [Design](DESIGN.md): the pipeline, modules, data model, audit ledger, reviewer UI, stack, and
  the two-week spike.
- [Sources](docs/sources.md): free and commercial search and enrichment sources, rate limits,
  licences, and how federation fits PRISMA-S.
- [Document and model tools](docs/document-tools.md): PDF parsers, OCR, extraction libraries, and
  local models, with licence verdicts.
- [Dependencies](docs/dependencies.md): the licence decision and an evaluation of every dependency
  for fit, quality, and maintenance.
- [Standards and regulation](docs/standards.md): what PRISMA, RAISE, the EU AI Act, GDPR, and
  quality frameworks require of the system.
- [Profiles](DESIGN.md#profiles): optional agency and jurisdiction rules. The first is the
  [G-BA benefit dossier profile](docs/profiles/gba-amnog.md).
- [Work packages](docs/work-packages.md): the build split into independent pieces, with status.
- [Council record](docs/council.md): how the design was reached, where the council disagreed, and
  which claims were checked.

## Name

Kittum (Akkadian *kittu*, "truth, reliability") was a Babylonian deity, the child of the justice
god Šamaš. Legal texts invoke Kittum as a witness. The product exists to produce a witnessed,
checkable record, so the name describes the job. Say it KIT-toom.

## Licence

Kittum, like the rest of Nebu, is licensed under the
[GNU Affero General Public License v3.0 or later](../LICENSE).
