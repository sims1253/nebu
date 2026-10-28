# Reviewer workbench

The screens from DESIGN.md "Reviewer workbench", built against a typed mock until the API serves
them. `pnpm dev` runs it on the mock; `pnpm test` runs the keyboard flows in headless Chromium;
`pnpm screenshots` (with `pnpm dev` running) writes `screenshots/`.

## Design

A reviewer spends hours here, deciding one record every 15 to 25 seconds. The design spends no
pixel on anything but the decision and its evidence.

- **A reading desk, not a dashboard.** Cool paper (`--paper`) for chrome, a lighter sheet for
  what is read, petrol ink (`--petrol`) for structure and focus. No cards, no shadows except on
  popups, rules instead of boxes.
- **One accent, for evidence only.** The highlighter yellow (`--marker`) marks the selected item
  and its source together: the revealed criterion and its quote in the abstract, the current
  mention and its box on the page, the selected field and its source region. Nothing else is
  yellow, so the eye learns that yellow means "this is where it says so".
- **Protocol terms are lines, never fills.** Protocol terms are underlined, entities dotted. They
  show before the decision because they come from the protocol, and they can never be mistaken
  for the AI's quote.
- **Status shows the key that sets it.** A status mark is the key letter in its colour: `i`
  include, `e` exclude, `u` unclear, `x` insufficient information; `y`, `n`, `?` for criterion
  verdicts. Colour never carries the meaning alone.
- **Every action has a keycap.** The dock under the record is the keymap: the criteria strip is
  numbered with the keys that exclude on each criterion, and `e` arms it (red rule, filled keys,
  full criterion text).
- **Type.** IBM Plex Sans, as in Janus, from the variable font: 13.5 px for the interface, 15.5 px
  on a 25 px line for abstracts (72 characters at most), 21 px record titles. Strips and tables
  use the same family at 87.5% width. Numbers are tabular everywhere.
- **Dark.** The same roles, lifted: a dark petrol desk, light ink one notch lighter in weight,
  and page images dimmed so a white PDF page does not glare.
- **Motion.** One: the AI banner slides up when it appears, and not with reduced motion.

`src/styles.css` holds the tokens; `src/ui/` holds `Key`, `KeyHint`, the status vocabulary, the
key handler, and the theme.

## Data

Screens use one interface, `Workbench` in `src/data/workbench.ts`. `src/data/schema.ts` mirrors
the Python records of `kittum.protocol`, `screening`, `judge`, `documents`, and `extraction` in
Effect Schema. `src/data/mock/` implements the interface on fixtures and decodes every response
through the schemas, so a queue item can never carry AI output. `src/main.tsx` is the one place to
swap in an implementation on `@kittum/contracts`.

Fixtures, recorded once by the scripts in `scripts/`:

- `records.json`: 28 PubMed records a second-line biliary tract cancer search returns, fetched
  with E-utilities.
- `abc06/`: the ABC-06 trial (Lancet Oncol 2021, CC BY 4.0) rendered with PyMuPDF to page PNGs,
  word boxes, mentions per criterion, and the anchors of the extraction values.

## What the API must provide

Beyond today's backend records:

| Need | Where | Why |
| --- | --- | --- |
| A batch of the next assignments after a given one | `ScreeningService` (WP-09) | The queue keeps five records ahead; `next()` returns one. |
| Record facts on `QueueItem`: journal, year, publication types, registry IDs, PMID, DOI, title highlights, structured-abstract sections as code-point ranges | WP-07, WP-09 | Shown above the abstract; Methods come first. |
| `status` on `Reveal` (`met`, `not_met`, `unclear`) | WP-09 | The banner says what the AI read, not only which criterion. |
| `DecideResult.phase` is `final` when no reveal follows, and a `revision` | WP-09 | The queue moves on at once when the AI agrees; undo re-decides with `expected_revision`. |
| Timing with each decision: shown, first key, decided, reveal shown | WP-09 | DESIGN.md "Metrics". |
| Review profile labels on the protocol (`profile.labels`) | WP-03 | The core is domain-neutral; a profile renames "population", "report", and so on. |
| A layer summary (page sizes, text) and per-page words and PNGs | WP-10 | The viewer loads words per page, not the whole layer. |
| Mentions per full-text criterion as anchors, in document order | WP-03, WP-10 | `[` and `]` walk them. |
| Per-criterion verdicts with evidence anchors on a full-text decision | WP-09 | `Decision` carries only the reason and a flat anchor list. |
| Anchors created from a reviewer's selection | WP-10 | Enter adopts a selection; offsets are code points. |
| Extraction fields as both paths' proposals per semantic key, with accept recording the path and source-view time | WP-11 | The review groups by key and gates Enter on the source. |
| Who the reviewer is and which scope they screen | WP-15 | `ProjectSummary.scope_id` stands in for assignment. |
