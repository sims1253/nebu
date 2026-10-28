# Reviewer workbench

The screens from DESIGN.md "Reviewer workbench", on the Kittum API. `pnpm dev` runs it against
`kittum serve` (proxied on `/api`); `VITE_KITTUM_WORKBENCH=mock pnpm dev` runs it on recorded
example data without a server. `pnpm test` runs the keyboard flows on the mock in headless
Chromium; `pnpm e2e` runs the whole review on the real stack (see `e2e/stack.ts`); `pnpm
screenshots` (with `VITE_KITTUM_WORKBENCH=mock pnpm dev` running) writes `screenshots/`.

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

Screens use one interface, `Workbench` in `src/data/workbench.ts`. `src/data/api.ts` implements
it on the client `@kittum/contracts` generates from the server's OpenAPI document, so server
records arrive typed as the Pydantic models define them; `src/data/schema.ts` holds only what the
screens compose from several responses and the extraction records, which the server does not serve
yet. `src/data/mock/` implements the same interface on fixtures for the unit tests and decodes its
answers through the generated schemas, so a queue item can never carry AI output. `src/main.tsx`
picks the implementation.

Who is at the workbench comes from `src/screens/project.tsx`: the person chosen on the project's
picker (remembered per browser tab, or `?as=<actor ID>`), passed to every call that depends on who
asks. There is no sign-in yet, and the picker says so.

Fixtures, recorded once by the scripts in `scripts/`:

- `records.json`: 28 PubMed records a second-line biliary tract cancer search returns, fetched
  with E-utilities.
- `abc06/`: the ABC-06 trial (Lancet Oncol 2021, CC BY 4.0) rendered with PyMuPDF to page PNGs,
  word boxes, mentions per criterion, and the anchors of the extraction values.

## What the API still lacks

The rest of the earlier list is served now (WP-15).

| Need | Where | Why |
| --- | --- | --- |
| Extraction fields as both paths' proposals per semantic key, with accept recording the path and source-view time | WP-11 | The review groups by key and gates Enter on the source. |
| Report targets and their rendering | WP-14 | The Reports screen. |
| The evidence quotes of a full-text column in adjudication | WP-09, WP-15 | Adjudication shows how many spans each reviewer cited, not which. |
| Authentication | WP-15 | `X-Kittum-Reviewer` is a stand-in. |
