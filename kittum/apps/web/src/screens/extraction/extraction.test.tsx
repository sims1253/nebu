import { afterEach, describe, expect, test } from "vite-plus/test";
import { page } from "vite-plus/test/browser";

import { mount, press } from "../../test/app.tsx";
import { VIEW_MS } from "./ExtractionReview.tsx";

let unmount: (() => void) | undefined;
afterEach(() => unmount?.());

const notice = () => page.getByTestId("extraction-notice");
const selected = () =>
  document.querySelector("[data-testid=field-row][aria-selected=true]")?.textContent ?? "";
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("extraction review", () => {
  test("accepts a value only after its source was on screen, and makes the reviewer pick between paths", async () => {
    const accepted: Array<{ fieldId: string; path: string; sourceViewedMs: number }> = [];
    const app = mount("/p/ivosidenib-cca/extraction", {
      log: (call, detail) => {
        if (call === "accept") accepted.push(detail as (typeof accepted)[number]);
      },
    });
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 19 accepted")).toBeVisible();

    await press("Tab"); // patients randomised to ASC alone
    expect(selected()).toContain("Patients randomised");
    await press("Enter");
    await expect
      .element(notice())
      .toMatchTextContent("Look at the highlighted source before accepting");
    await expect.element(page.getByTestId("source-highlight").first()).toBeVisible();
    await pause(VIEW_MS + 200);
    await press("Enter");
    await expect.element(page.getByText("2 of 19 accepted")).toBeVisible();
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
    await expect.element(page.getByText("3 of 19 accepted")).toBeVisible();
    expect(accepted.at(-1)).toEqual(expect.objectContaining({ path: "janus" }));
  });

  test("does not count time before the page image has drawn", async () => {
    const app = mount("/p/ivosidenib-cca/extraction", { latency: { page: 2500 } });
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 19 accepted")).toBeVisible();
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
    await expect.element(page.getByText("2 of 19 accepted")).toBeVisible();
  });

  test("never accepts a value whose page image failed to load", async () => {
    const app = mount("/p/ivosidenib-cca/extraction", { brokenPages: [6] });
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 19 accepted")).toBeVisible();
    await press("Tab");
    await expect.element(page.getByText("Page 6 could not load.")).toBeVisible();
    await pause(VIEW_MS + 300);
    await press("Enter");
    await expect.element(notice()).toMatchTextContent("Look at the highlighted source");
    expect(app.workbench.acceptances).toHaveLength(0);
  });

  test("time on one path's source does not count for the other path's", async () => {
    const app = mount("/p/ivosidenib-cca/extraction");
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 19 accepted")).toBeVisible();
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
    await expect.element(page.getByText("2 of 19 accepted")).toBeVisible();
    expect(app.workbench.acceptances.at(-1)).toEqual(
      expect.objectContaining({ fieldId: "field-09", path: "llm", edit: null }),
    );
  });

  test("accepting an edited value keeps the edit and the span it came from", async () => {
    const app = mount("/p/ivosidenib-cca/extraction");
    unmount = app.unmount;
    await expect.element(page.getByText("1 of 19 accepted")).toBeVisible();
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
    await expect.element(page.getByText("2 of 19 accepted")).toBeVisible();
    const accepted = app.workbench.acceptances.at(-1);
    expect(accepted?.edit).toEqual({
      value: "21·7",
      start: Number(word.dataset["s"]),
      end: Number(word.dataset["e"]),
      quote: "21·7",
      boxes: [expect.objectContaining({ page: 6 })],
    });
  });
});

const wordSpan = (text: string) =>
  [...document.querySelectorAll<HTMLElement>("[data-page='6'] .text-layer > span")].find(
    (span) => span.textContent === text,
  );
