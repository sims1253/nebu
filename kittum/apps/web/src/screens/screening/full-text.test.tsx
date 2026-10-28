import { afterEach, describe, expect, test } from "vite-plus/test";
import { page, userEvent } from "vite-plus/test/browser";

import anchors from "../../data/mock/fixtures/abc06/anchors.json";
import { hold, mount, press } from "../../test/app.tsx";

let unmount: (() => void) | undefined;
afterEach(() => unmount?.());

const current = () => document.querySelector<HTMLElement>("[data-testid=mention][aria-current]");
const sourceBox = () => document.querySelector<HTMLElement>("[data-testid=source-highlight]");
const aiBox = () => document.querySelector<HTMLElement>("[data-testid=ai-quote-highlight]");
const pageOf = (element: Element | null) =>
  element?.closest("[data-page]")?.getAttribute("data-page");
const notice = () => page.getByTestId("fulltext-notice");

describe("full-text screening", () => {
  test("goes through mentions with , and . and includes in one action", async () => {
    const app = mount("/p/ivosidenib-cca/screening/full-text");
    unmount = app.unmount;
    const pretreated = anchors.mentions.C_POP_PRETREATED;

    await expect.element(page.getByText("Page 1 of 12")).toBeVisible();
    await press("ArrowDown"); // criterion 2: progressed after prior therapy
    await expect.element(page.getByText(`– of ${pretreated.length}`)).toBeVisible();
    expect(sourceBox()).toBeNull();

    // `.` walks the mentions in document order, and the page follows with the marker on the span.
    const pages: string[] = [];
    for (let i = 0; i < 3; i++) {
      await press(".");
      await expect.element(page.getByText(`${i + 1} of ${pretreated.length}`)).toBeVisible();
      await expect.poll(() => pageOf(sourceBox())).toBe(String(pretreated[i]!.boxes[0]!.page));
      pages.push(pageOf(sourceBox()) ?? "");
      expect(current()?.textContent).toContain(pretreated[i]!.quote.exact.replace(/\s+/g, " "));
    }
    expect(pages).toEqual([...pages].sort());
    await press(",");
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

    // Include says every criterion is met, so a verdict that says otherwise stops it.
    await press("?");
    await press("i");
    await expect
      .element(notice())
      .toMatchTextContent(
        "Include means every criterion is met, but you marked 2 Population unclear",
      );
    expect(app.workbench.decisions).toHaveLength(0);
    await press("ArrowUp"); // the verdict moved on to criterion 3
    await press("y");

    // No verdict on the other five criteria: include is one action.
    await press("i");
    await expect.element(notice()).toMatchTextContent("Included");
    const decision = app.workbench.decisions.at(-1)?.decision;
    expect(decision?.outcome).toBe("include");
    expect(decision?.evidence_anchor_ids).toEqual([pretreated[1]!.id]);
    expect(decision?.criteria).toEqual([
      {
        criterion_id: "C_POP_PRETREATED",
        status: "met",
        evidence_anchor_ids: [pretreated[1]!.id],
      },
    ]);
  });

  test("excludes on the one failing criterion, then shows why the AI disagreed, with its quote on the page", async () => {
    const app = mount("/p/ivosidenib-cca/screening/full-text");
    unmount = app.unmount;
    const pretreated = anchors.mentions.C_POP_PRETREATED;
    await expect.element(page.getByText("Page 1 of 12")).toBeVisible();

    // An exclusion needs evidence for its criterion.
    await press("e");
    await press("2");
    await expect.element(notice()).toMatchTextContent("To exclude on 2 Population, adopt evidence");
    expect(app.workbench.decisions).toHaveLength(0);
    expect(current()).toBeNull(); // criterion 2 is active, no mention chosen yet

    // Criterion 2 is active now. Adopt its third mention, then exclude on it.
    for (let i = 0; i < 3; i++) await press(".");
    await press("Enter");
    await expect.element(page.getByText("1 evidence span")).toBeVisible();
    expect(page.getByTestId("ai-reveal").query()).toBeNull();
    await press("e");
    await press("2");

    // The AI read criterion 2 as met. The panel names the criterion, its verdict, and why, and
    // the page shows its quote where it is.
    const reveal = page.getByTestId("ai-reveal");
    await expect.element(reveal).toMatchTextContent("The AI judge disagrees");
    await expect.element(reveal.getByTestId("ai-criterion")).toMatchTextContent("2 Population");
    await expect.element(reveal).toMatchTextContent("Met");
    await expect.element(reveal.getByTestId("ai-rationale")).toMatchTextContent("reads as met");
    const quote = pretreated[0]!;
    await expect
      .element(reveal.getByTestId("ai-quote"))
      .toMatchTextContent(`Marked on page ${quote.boxes[0]!.page}.`);
    await expect.poll(() => pageOf(aiBox())).toBe(String(quote.boxes[0]!.page));
    expect(aiBox()?.getAttribute("title")).toBe("The AI judge's quote");

    // Back to a mention, then q shows the AI's quote again.
    await press(",");
    await press("q");
    await expect.poll(() => pageOf(aiBox())).toBe(String(quote.boxes[0]!.page));

    await press(" "); // keep my exclusion
    await expect.element(notice()).toMatchTextContent("Excluded on 2 Population");
    const [provisional, final] = app.workbench.decisions;
    expect(provisional?.phase).toBe("provisional");
    expect(final?.phase).toBe("final");
    expect(final?.decision).toMatchObject({
      outcome: "exclude",
      reason_criterion_id: "C_POP_PRETREATED",
      evidence_anchor_ids: [pretreated[2]!.id],
      criteria: [
        {
          criterion_id: "C_POP_PRETREATED",
          status: "not_met",
          evidence_anchor_ids: [pretreated[2]!.id],
        },
      ],
    });
    expect(final?.timing.reveal_shown_at).not.toBeNull();
  });

  test("marks the PDF as the wrong document with a note, without deciding the report", async () => {
    const app = mount("/p/ivosidenib-cca/screening/full-text");
    unmount = app.unmount;
    await expect.element(page.getByText("Page 1 of 12")).toBeVisible();

    await press("w");
    const form = page.getByTestId("wrong-document");
    await expect.element(form).toMatchTextContent("This is not an exclusion.");
    // Escape takes it back; nothing is recorded.
    await press("Escape");
    expect(form.query()).toBeNull();
    expect(app.workbench.wrongDocuments).toHaveLength(0);

    await press("w");
    await expect.element(page.getByLabelText("What is the PDF instead? Optional.")).toHaveFocus();
    // Typing in the note never reaches the screen's keys: "i" and "e" decide nothing.
    await userEvent.keyboard("CONSORT checklist, not the trial{Enter}");
    await expect
      .element(page.getByTestId("wrong-document-sent"))
      .toMatchTextContent("The record now shows as missing its full text.");
    expect(app.workbench.wrongDocuments).toEqual([
      {
        assignmentId: "asg-ft-abc06",
        documentId: expect.any(String),
        note: "CONSORT checklist, not the trial",
      },
    ]);
    expect(app.workbench.decisions).toHaveLength(0);
    await expect.element(page.getByRole("heading", { name: "Full texts finished" })).toBeVisible();
  });

  test("a held key adopts or marks once, and the decision reports when the report was shown and first acted on", async () => {
    const app = mount("/p/ivosidenib-cca/screening/full-text");
    unmount = app.unmount;
    await expect.element(page.getByText("Page 1 of 12")).toBeVisible();
    await pause(200);

    await press("ArrowDown"); // first key
    await press(".");
    await hold("Enter", 3); // four keydowns: without the guard, adopt, drop, adopt, drop
    await expect.element(page.getByText("1 evidence span")).toBeVisible();

    await press("ArrowUp");
    await hold("y", 3); // without the guard, four criteria would be marked met
    await expect.element(notice()).toMatchTextContent("1 of 6 criteria have your verdict");

    await pause(300);
    await press("i");
    await expect.element(notice()).toMatchTextContent("Included");

    const timing = app.workbench.decisions.at(-1)!.timing;
    const shown = Date.parse(timing.shown_at);
    const first = Date.parse(timing.first_key_at!);
    const decided = Date.parse(timing.decided_at);
    expect(first - shown).toBeGreaterThanOrEqual(200);
    expect(decided - first).toBeGreaterThanOrEqual(300);
  });
});

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
