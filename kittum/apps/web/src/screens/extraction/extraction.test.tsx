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
});
