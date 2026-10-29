import { afterEach, describe, expect, test } from "vite-plus/test";
import { page } from "vite-plus/test/browser";

import { MOCK_RUN_ID } from "../../data/mock/index.ts";
import { mount, press } from "../../test/app.tsx";
import { VIEW_MS } from "./ExtractionReview.tsx";

const RUN = `/p/ivosidenib-cca/extraction/${MOCK_RUN_ID}`;

let unmount: (() => void) | undefined;
afterEach(() => {
  release();
  unmount?.();
});

const notice = () => page.getByTestId("extraction-notice");
const selected = () =>
  document.querySelector("[data-testid=field-row][aria-selected=true]")?.textContent ?? "";
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
// Room for the page images and the scroll under a full `pnpm check` (see the multi-line test).
const settle = { timeout: 15_000 };

/**
 * Holds back every IntersectionObserver report while `held` is set, as a browser may when the page
 * comes back from a hidden tab; `release` delivers them in order. The reports are the browser's own.
 */
let held: Array<() => void> | null = null;
// Reports delivered that say an element is in view, per element: a test hides the page right
// after one, so a "glance" lasts no longer than the test means it to on a slow machine.
const inViewOf = new Map<Element, number>();
const RealObserver = window.IntersectionObserver;
window.IntersectionObserver = class extends RealObserver {
  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    super((entries, observer) => {
      const deliver = () => {
        callback(entries, observer);
        for (const e of entries) {
          if (e.isIntersecting) inViewOf.set(e.target, (inViewOf.get(e.target) ?? 0) + 1);
        }
      };
      if (held) held.push(deliver);
      else deliver();
    }, options);
  }
};
/** The two agreeing standard fields a group accept takes, at the end of the mock list. */
const groupRows = () =>
  [...document.querySelectorAll<HTMLElement>("[data-testid=field-row]")].filter(
    (r) => r.textContent?.includes("standard") && r.textContent.includes("Both agree"),
  );
/** Scrolls the group's rows into view and waits until the page has been told each one is. */
const showGroupRows = async () => {
  const rows = groupRows();
  expect(rows).toHaveLength(2);
  const before = rows.map((r) => inViewOf.get(r) ?? 0);
  rows.at(-1)!.scrollIntoView({ block: "end" });
  await expect
    .poll(() => rows.every((r, i) => (inViewOf.get(r) ?? 0) > before[i]!), settle)
    .toBe(true);
  return rows;
};
/** Waits until the page has been told again, since `counts`, that each row is in view. */
const reportedAgain = (rows: HTMLElement[], counts: number[]) =>
  expect.poll(() => rows.every((r, i) => (inViewOf.get(r) ?? 0) > counts[i]!), settle).toBe(true);
// Long enough that a slow machine's pauses after the return cannot add up to it.
const HIDDEN_MS = 2500;
// These tests wait on the page several times; the default 15 s budget would cut them short.
const BUDGET = 60_000;
const hold = () => {
  held = [];
};
const release = () => {
  const queued = held ?? [];
  held = null;
  for (const deliver of queued) deliver();
};

/** Hides or shows the page as switching tabs does, for the page's own visibility checks. */
const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
  document.dispatchEvent(new Event("visibilitychange"));
};

describe("extraction review", () => {
  test("accepts a value only after its source was on screen, and makes the reviewer pick between paths", async () => {
    const accepted: Array<{ fieldId: string; path: string; sourceViewedMs: number }> = [];
    const app = mount(RUN, {
      log: (call, detail) => {
        if (call === "accept") accepted.push(detail as (typeof accepted)[number]);
      },
    });
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 21 accepted")).toBeVisible();

    await press("Tab"); // patients randomised to ASC alone
    expect(selected()).toContain("Patients randomised");
    await press("Enter");
    await expect
      .element(notice())
      .toMatchTextContent("Look at the highlighted source before accepting");
    await expect.element(page.getByTestId("source-highlight").first()).toBeVisible();
    await pause(VIEW_MS + 200);
    await press("Enter");
    await expect.element(page.getByText("2 of 21 accepted")).toBeVisible();
    expect(accepted).toHaveLength(1);
    expect(accepted[0]?.sourceViewedMs).toBeGreaterThanOrEqual(VIEW_MS);

    // The hazard ratio: Janus read 0.69, the LLM path the post-hoc 0.68.
    for (let i = 0; i < 6; i++) await press("Tab"); // accepting moved on by one
    expect(selected()).toContain("Adjusted hazard ratio");
    await pause(VIEW_MS + 200);
    await press("Enter");
    await expect.element(notice()).toMatchTextContent("The paths disagree");
    await press("1");
    await pause(VIEW_MS + 200);
    await press("Enter");
    await expect.element(page.getByText("3 of 21 accepted")).toBeVisible();
    expect(accepted.at(-1)).toEqual(expect.objectContaining({ path: "janus" }));
  });

  test("accepts as a group only the agreeing standard fields that were on screen, for as long as they were", async () => {
    const app = mount(RUN);
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 21 accepted"), settle).toBeVisible();
    // The two agreeing standard fields are last in the list, below the fold: a group accept
    // now would take values the reviewer never saw, however long the screen has been open.
    await pause(VIEW_MS + 300);
    await press("a");
    await expect.element(notice(), settle).toMatchTextContent("has been on screen long enough");
    expect(app.workbench.groupAccepts).toHaveLength(0);

    const list = document.querySelector<HTMLElement>("[data-testid=fields]")!;
    list.scrollTop = list.scrollHeight;
    const rows = () =>
      [...document.querySelectorAll<HTMLElement>("[data-testid=field-row]")].filter((r) =>
        r.textContent?.includes("Patients with any adverse event"),
      );
    await expect.poll(() => rows().length, settle).toBe(2);
    const shownAt = performance.now();
    await pause(VIEW_MS + 300);
    await press("a");
    await expect.element(page.getByText("3 of 21 accepted"), settle).toBeVisible();
    const [group] = app.workbench.groupAccepts;
    expect(group?.fieldIds).toEqual(rows().map((r) => r.dataset["fieldId"]));
    // The time they were on screen, not the time since the screen opened.
    expect(group?.listViewedMs).toBeGreaterThanOrEqual(VIEW_MS);
    expect(group?.listViewedMs).toBeLessThanOrEqual(performance.now() - shownAt + 50);
  });

  test(
    "does not count the time the tab was hidden towards a group accept",
    async () => {
      const app = mount(RUN);
      unmount = app.unmount;
      await expect.element(page.getByText("1 of 21 accepted"), settle).toBeVisible();
      // The rows come into view as the page goes to another tab: the browser's reports reach the
      // page while it is hidden, and no time may run then. Holding the reports at both edges
      // makes this independent of how fast the machine is.
      const rows = groupRows();
      expect(rows).toHaveLength(2);
      hold();
      rows.at(-1)!.scrollIntoView({ block: "end" });
      await expect.poll(() => (held?.length ?? 0) > 0, settle).toBe(true);
      setHidden(true);
      release();
      await pause(HIDDEN_MS);
      // Back, the fresh reports wait: nothing has been on screen since the return.
      const counts = rows.map((r) => inViewOf.get(r) ?? 0);
      hold();
      setHidden(false);
      await press("a");
      await expect.element(notice(), settle).toMatchTextContent("has been on screen long enough");
      expect(app.workbench.groupAccepts).toHaveLength(0);
      // Their time runs once the page has said they are in view again.
      release();
      await reportedAgain(rows, counts);
      await pause(VIEW_MS + 100);
      await press("a");
      await expect.element(page.getByText("3 of 21 accepted"), settle).toBeVisible();
      expect(app.workbench.groupAccepts[0]?.listViewedMs).toBeLessThan(HIDDEN_MS);
    },
    BUDGET,
  );

  test(
    "does not count the time the tab was hidden towards a source's view",
    async () => {
      const app = mount(RUN);
      unmount = app.unmount;
      await expect.element(page.getByText("1 of 21 accepted"), settle).toBeVisible();
      // Its reports wait until the page has drawn, so the glance is only as long as the test says.
      hold();
      await press("Tab"); // patients randomised to ASC alone, on page 6
      await expect
        .poll(
          () => document.querySelector<HTMLImageElement>("[data-page='6'] img")?.complete,
          settle,
        )
        .toBe(true);
      await expect.element(page.getByTestId("source-highlight").first(), settle).toBeVisible();
      // A glance at the source, a while in another tab, and back: not a look at it.
      release();
      setHidden(true);
      await pause(HIDDEN_MS);
      setHidden(false);
      await press("Enter");
      await expect.element(notice(), settle).toMatchTextContent("Look at the highlighted source");
      expect(app.workbench.acceptances).toHaveLength(0);
      // Its time runs again once the page is back.
      await pause(VIEW_MS + 200);
      await press("Enter");
      await expect.element(page.getByText("2 of 21 accepted"), settle).toBeVisible();
      expect(app.workbench.acceptances[0]?.sourceViewedMs).toBeLessThan(HIDDEN_MS);
    },
    BUDGET,
  );

  test(
    "does not resume a group's time on return until a fresh report says the rows are in view",
    async () => {
      const app = mount(RUN);
      unmount = app.unmount;
      await expect.element(page.getByText("1 of 21 accepted"), settle).toBeVisible();
      const list = document.querySelector<HTMLElement>("[data-testid=fields]")!;
      await showGroupRows();
      // Away, the list scrolls off the two rows; back, the observer is slow to say so.
      setHidden(true);
      hold();
      list.scrollTop = 0;
      await pause(300);
      setHidden(false);
      await pause(VIEW_MS + 200);
      await press("a");
      await expect.element(notice(), settle).toMatchTextContent("has been on screen long enough");
      expect(app.workbench.groupAccepts).toHaveLength(0);
      // Once the reports arrive, the rows count only when they are in view again.
      release();
      await pause(VIEW_MS + 200);
      await press("a");
      await expect.element(notice(), settle).toMatchTextContent("has been on screen long enough");
      await showGroupRows();
      await pause(VIEW_MS + 100);
      await press("a");
      await expect.element(page.getByText("3 of 21 accepted"), settle).toBeVisible();
    },
    BUDGET,
  );

  test(
    "does not resume a source's time on return until a fresh report says it is in view",
    async () => {
      const app = mount(RUN);
      unmount = app.unmount;
      await expect.element(page.getByText("1 of 21 accepted"), settle).toBeVisible();
      hold(); // as above: the glance starts when the reports are let through
      await press("Tab"); // patients randomised to ASC alone, on page 6
      await expect
        .poll(
          () => document.querySelector<HTMLImageElement>("[data-page='6'] img")?.complete,
          settle,
        )
        .toBe(true);
      const box = page.getByTestId("source-highlight").first();
      await expect.element(box, settle).toBeVisible();
      release();
      // Away, the viewer scrolls the source out of view, its page still drawn; back, the observer
      // is slow to say so.
      const highlight = document.querySelector<HTMLElement>("[data-testid=source-highlight]")!;
      const scroller = scrollParent(highlight);
      // The pages above give the viewer the height to scroll the source below its bottom edge.
      // Until their images load, the scroll stops short.
      await expect
        .poll(
          () =>
            [...document.querySelectorAll<HTMLImageElement>("[data-page] img")].every(
              (i) => i.complete,
            ),
          settle,
        )
        .toBe(true);
      setHidden(true);
      hold();
      // Scroll until the source sits below the bottom edge; a scroll that stops short fails here.
      await expect
        .poll(() => {
          const below =
            highlight.getBoundingClientRect().top - scroller.getBoundingClientRect().bottom;
          if (below <= 40) scroller.scrollTop -= 40 - below;
          return below > 0;
        }, settle)
        .toBe(true);
      await pause(300);
      setHidden(false);
      await pause(VIEW_MS + 200);
      expect(highlight.getBoundingClientRect().top).toBeGreaterThan(
        scroller.getBoundingClientRect().bottom,
      );
      await press("Enter");
      await expect.element(notice(), settle).toMatchTextContent("Look at the highlighted source");
      expect(app.workbench.acceptances).toHaveLength(0);
      // The refusal brings the source back; once the reports arrive, its time runs again.
      release();
      await pause(VIEW_MS + 300);
      await press("Enter");
      await expect.element(page.getByText("2 of 21 accepted"), settle).toBeVisible();
    },
    BUDGET,
  );

  test("does not count time before the page image has drawn", async () => {
    const app = mount(RUN, { latency: { page: 2500 } });
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 21 accepted")).toBeVisible();
    await press("Tab"); // patients randomised to ASC alone, on page 6
    await pause(VIEW_MS + 300);
    expect(document.querySelector("[data-page='6'] img")).toBeNull();
    await press("Enter");
    await expect.element(notice()).toMatchTextContent("Look at the highlighted source");

    await expect
      .poll(() => document.querySelector<HTMLImageElement>("[data-page='6'] img")?.complete, {
        timeout: 5000,
      })
      .toBe(true);
    await pause(VIEW_MS + 200);
    await press("Enter");
    await expect.element(page.getByText("2 of 21 accepted")).toBeVisible();
  });

  test("never accepts a value whose page image failed to load", async () => {
    const app = mount(RUN, { brokenPages: [6] });
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 21 accepted")).toBeVisible();
    await press("Tab");
    await expect.element(page.getByText("Page 6 could not load.")).toBeVisible();
    await pause(VIEW_MS + 300);
    await press("Enter");
    await expect.element(notice()).toMatchTextContent("Look at the highlighted source");
    expect(app.workbench.acceptances).toHaveLength(0);
  });

  test("time on one path's source does not count for the other path's", async () => {
    const app = mount(RUN);
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 21 accepted")).toBeVisible();
    for (let i = 0; i < 8; i++) await press("Tab");
    expect(selected()).toContain("Adjusted hazard ratio");
    await press("1"); // Janus: 0.69 from the primary analysis
    await pause(VIEW_MS + 300);
    await press("2"); // LLM: 0.68 from the sensitivity analysis, a different sentence
    await press("Enter");
    await expect.element(notice()).toMatchTextContent("Look at the highlighted source");
    expect(app.workbench.acceptances).toHaveLength(0);

    await pause(VIEW_MS + 300);
    await press("Enter");
    await expect.element(page.getByText("2 of 21 accepted")).toBeVisible();
    expect(app.workbench.acceptances.at(-1)).toEqual(
      expect.objectContaining({ fieldId: "field-09", path: "llm", edit: null }),
    );
  });

  test("accepting an edited value keeps the edit and the span it came from", async () => {
    const app = mount(RUN);
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 21 accepted")).toBeVisible();
    for (let i = 0; i < 3; i++) await press("Tab");
    expect(selected()).toContain("Median follow-up");

    // Select the number on the page, as a drag over the text layer would.
    await expect.poll(() => wordSpan("21·7")).toBeTruthy();
    const word = wordSpan("21·7")!;
    const range = document.createRange();
    range.selectNodeContents(word);
    getSelection()?.removeAllRanges();
    getSelection()?.addRange(range);
    word.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));

    await press("e");
    await expect.element(notice()).toMatchTextContent("Edited to “21·7”");
    await pause(VIEW_MS + 300);
    await press("Enter");
    await expect.element(page.getByText("2 of 21 accepted")).toBeVisible();
    const accepted = app.workbench.acceptances.at(-1);
    expect(accepted?.edit).toEqual({
      value: "21·7",
      start: Number(word.dataset["s"]),
      end: Number(word.dataset["e"]),
      quote: "21·7",
      boxes: [expect.objectContaining({ page: 6 })],
    });
  });

  test("needs every line of a multi-line source on screen, not only the first", async () => {
    const app = mount(RUN);
    unmount = app.unmount;
    // `expect.poll` and `expect.element` give up after 1 s by default; under a full
    // `pnpm check` the page images and the scroll can take longer than that, so the waits get room.
    await expect.element(page.getByText("1 of 21 accepted"), settle).toBeVisible();
    await press("Tab"); // "randomly assigned to ASC alone (n=81)" breaks over two lines
    await expect
      .poll(
        () => document.querySelectorAll("[data-page='6'] [data-testid=source-highlight]").length,
        settle,
      )
      .toBe(2);
    // Every page image, not only page 6: the pages below give the viewer the height to scroll
    // the source down to its bottom edge. Until they load, the scroll stops short.
    await expect
      .poll(
        () =>
          [...document.querySelectorAll<HTMLImageElement>("[data-page] img")].every(
            (i) => i.complete,
          ),
        settle,
      )
      .toBe(true);
    const [first, second] = [
      ...document.querySelectorAll<HTMLElement>("[data-page='6'] [data-testid=source-highlight]"),
    ];
    const scroller = scrollParent(first!);

    // The first line sits just above the bottom edge; at most a sliver of the second shows.
    const bottom = scroller.getBoundingClientRect().bottom;
    // Scroll until the line lands there; a scroll that stops short fails here, not below.
    await expect
      .poll(() => {
        scroller.scrollTop += first!.getBoundingClientRect().bottom - bottom + 2;
        const gap = bottom - first!.getBoundingClientRect().bottom;
        return gap >= 0 && gap < 4;
      }, settle)
      .toBe(true);
    const rest = second!.getBoundingClientRect();
    expect((bottom - rest.top) / rest.height).toBeLessThan(0.5);
    await pause(VIEW_MS + 300);
    await press("Enter");
    await expect.element(notice(), settle).toMatchTextContent("Look at the highlighted source");
    expect(app.workbench.acceptances).toHaveLength(0);

    // The refusal brought the source back into view; centre it to be sure, and the gate opens.
    first!.scrollIntoView({ block: "center" });
    await pause(VIEW_MS + 300);
    await press("Enter");
    await expect.element(page.getByText("2 of 21 accepted"), settle).toBeVisible();
  }, 60_000); // its waits get 15 s each; the default 15 s budget would cut them short
});

const scrollParent = (element: HTMLElement): HTMLElement => {
  let node = element.parentElement;
  while (
    node &&
    !(node.scrollHeight > node.clientHeight && getComputedStyle(node).overflowY !== "visible")
  ) {
    node = node.parentElement;
  }
  if (!node) throw new Error("no scrolling ancestor");
  return node;
};

const wordSpan = (text: string) =>
  [...document.querySelectorAll<HTMLElement>("[data-page='6'] .text-layer > span")].find(
    (span) => span.textContent === text,
  );
