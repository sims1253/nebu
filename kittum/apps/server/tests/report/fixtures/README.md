# Report test fixtures

- `nrt_embase.ris`: an Embase-style export for the nicotine replacement example. Two records
  duplicate CENTRAL records of `tests/screening/fixtures/nrt_central.ris` (same DOIs), one is a
  follow-up report of the CENTRAL patch trial (same NCT number), two are further trials. The
  records are illustrative, like the CENTRAL fixture.
- `capivasertib_medline.ris`, `capivasertib_central.ris`: MEDLINE and CENTRAL exports for the
  capivasertib example (`examples/protocols/capivasertib.yaml`). The citations (authors, titles,
  journals, DOIs, PMIDs, trial numbers) are those of the published reports; the abstracts are
  short paraphrases written for the tests, not the published abstracts.
- `capivasertib_ctis.csv`: a CTIS export. The first row is CAPItello-291's EU CT number from the
  protocol's reference set; the other two rows are illustrative entries with invented numbers.
