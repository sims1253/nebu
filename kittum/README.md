# Kittum

Kittum is a workbench for systematic literature reviews behind German AMNOG benefit dossiers. It
runs the review from the protocol to the Modul 4 annexes. AI screens and extracts beside two human
reviewers, and every decision lands in a hash-chained ledger. The deliverable is the dossier output
plus one database file that holds the whole audit trail.

Status: design only. No code exists yet.

## Documents

- [Design](DESIGN.md): the pipeline, modules, data model, audit ledger, reviewer UI, stack, and
  the two-week spike.
- [Sources](docs/sources.md): free and commercial search and enrichment sources, rate limits,
  licences, and how federation fits PRISMA-S.
- [Document and model tools](docs/document-tools.md): PDF parsers, OCR, extraction libraries, and
  local models, with licence verdicts.
- [Dependencies](docs/dependencies.md): the licence decision and an evaluation of every dependency
  for fit, quality, and maintenance.
- [Requirements and standards](docs/requirements.md): what IQWiG, G-BA, PRISMA, RAISE, the EU HTA
  Regulation, the EU AI Act, and GDPR require of the system.
- [Council record](docs/council.md): how the design was reached, where the council disagreed, and
  which claims were checked.

## Name

Kittum (Akkadian *kittu*, "truth, reliability") was a Babylonian deity, the child of the justice
god Šamaš. Legal texts invoke Kittum as a witness. The product exists to produce a witnessed,
checkable record, so the name describes the job. Say it KIT-toom.

## Licence

Kittum is licensed under the [GNU Affero General Public License v3.0 or later](LICENSE). The rest
of the Nebu repository is MIT.
