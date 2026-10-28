import { afterEach, describe, expect, test } from "vite-plus/test";
import { page } from "vite-plus/test/browser";

import { mount, press } from "../test/app.tsx";

let unmount: (() => void) | undefined;
afterEach(() => unmount?.());

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("layout keys", () => {
  test("a stage chord in full text navigates without deciding the report", async () => {
    const app = mount("/p/ivosidenib-cca/screening/full-text");
    unmount = app.unmount;
    await expect.element(page.getByText("Page 1 of 12")).toBeVisible();
    // Make every decision key live: evidence adopted and every criterion verdicted.
    await press("]");
    await press("Enter");
    for (let i = 0; i < 6; i++) await press("y");
    await expect
      .element(page.getByTestId("fulltext-notice"))
      .toMatchTextContent("Every criterion has a verdict");

    await press("g");
    await press("x"); // extraction, not "insufficient information"
    await expect.element(page.getByTestId("extraction-run").first()).toBeVisible();
    await pause(200);
    expect(app.workbench.decisions).toHaveLength(0);
  });

  test("a chord key in title and abstract never arms an exclusion", async () => {
    const app = mount("/p/ivosidenib-cca/screening/title-abstract");
    unmount = app.unmount;
    await expect.element(page.getByTestId("record-title")).toMatchTextContent("ClarIDHy");
    await press("g");
    await press("e"); // no stage has `e`; the chord consumes it
    await press("1");
    await pause(200);
    expect(app.workbench.decisions).toHaveLength(0);
    await expect.element(page.getByTestId("record-title")).toMatchTextContent("ClarIDHy");
    await expect
      .element(page.getByText("To exclude, press e first, then the criterion number."))
      .toBeVisible();
  });

  test("an open project menu keeps decision keys from the record behind it", async () => {
    const app = mount("/p/ivosidenib-cca/screening/title-abstract");
    unmount = app.unmount;
    await expect.element(page.getByTestId("record-title")).toMatchTextContent("ClarIDHy");
    const switcher = page.getByRole("button", { name: /Second-line therapy review/ });
    await switcher.click();
    await expect.element(page.getByRole("menu")).toBeVisible();
    // Inside the popup Base UI keeps letters for type-ahead; once focus is back on the page
    // with the menu still open, nothing else stood between the keys and the record.
    (document.activeElement as HTMLElement | null)?.blur();
    await press("i");
    await press("u");
    await pause(200);
    await expect.element(page.getByRole("menu")).toBeVisible();
    expect(app.workbench.decisions).toHaveLength(0);

    await switcher.click();
    await expect.element(page.getByRole("menu")).not.toBeInTheDocument();
    await press("i");
    await expect.element(page.getByTestId("record-title")).toMatchTextContent("ABC-06");
    expect(app.workbench.decisions).toHaveLength(1);
  });
});
