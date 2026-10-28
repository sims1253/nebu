import { afterEach, describe, expect, test } from "vite-plus/test";
import { page } from "vite-plus/test/browser";

import anchors from "../../data/mock/fixtures/abc06/anchors.json";
import { mount, press } from "../../test/app.tsx";

let unmount: (() => void) | undefined;
afterEach(() => unmount?.());

const current = () => document.querySelector<HTMLElement>("[data-testid=mention][aria-current]");
const sourceBox = () => document.querySelector<HTMLElement>("[data-testid=source-highlight]");
const pageOf = (element: Element | null) =>
  element?.closest("[data-page]")?.getAttribute("data-page");

describe("full-text screening", () => {
  test("jumps between mentions in document order and adopts one as evidence", async () => {
    const app = mount("/p/ivosidenib-cca/screening/full-text");
    unmount = app.unmount;
    const pretreated = anchors.mentions.C_POP_PRETREATED;

    await expect.element(page.getByText("Page 1 of 12")).toBeVisible();
    await press("ArrowDown"); // criterion 2: progressed after prior therapy
    await expect.element(page.getByText(`– of ${pretreated.length}`)).toBeVisible();
    expect(sourceBox()).toBeNull();

    // `]` walks the mentions in document order, and the page follows with the marker on the span.
    const pages: string[] = [];
    for (let i = 0; i < 3; i++) {
      await press("]");
      await expect.element(page.getByText(`${i + 1} of ${pretreated.length}`)).toBeVisible();
      await expect.poll(() => pageOf(sourceBox())).toBe(String(pretreated[i]!.boxes[0]!.page));
      pages.push(pageOf(sourceBox()) ?? "");
      expect(current()?.textContent).toContain(pretreated[i]!.quote.exact.replace(/\s+/g, " "));
    }
    expect(pages).toEqual([...pages].sort());
    await press("[");
    await expect.element(page.getByText(`2 of ${pretreated.length}`)).toBeVisible();
    await expect.poll(() => pageOf(sourceBox())).toBe(String(pretreated[1]!.boxes[0]!.page));

    // The highlight sits on the words of the mention: its box covers the mention's first word.
    const box = sourceBox()!.getBoundingClientRect();
    const word = [...document.querySelectorAll<HTMLElement>(".text-layer > span")].find(
      (span) => Number(span.dataset["s"]) === pretreated[1]!.start,
    );
    expect(word).toBeDefined();
    const wordBox = word!.getBoundingClientRect();
    expect(wordBox.top).toBeGreaterThanOrEqual(box.top - 1);
    expect(wordBox.bottom).toBeLessThanOrEqual(box.bottom + 1);

    await press("Enter");
    await expect.element(page.getByText("1 evidence span")).toBeVisible();
    expect(current()?.querySelector("[title='Adopted as evidence']")).not.toBeNull();

    // A decision needs a verdict on every criterion, and cites the adopted span.
    await press("i");
    await expect
      .element(page.getByTestId("fulltext-notice"))
      .toMatchTextContent("Give every criterion a verdict first");
    await press("ArrowUp");
    for (const key of ["y", "y", "y", "y", "y", "y"]) await press(key);
    await press("i");
    await expect.element(page.getByTestId("fulltext-notice")).toMatchTextContent("Included");
    const decision = app.workbench.decisions.at(-1)?.decision;
    expect(decision?.outcome).toBe("include");
    expect(decision?.evidence_anchor_ids).toEqual([pretreated[1]!.id]);
  });
});
