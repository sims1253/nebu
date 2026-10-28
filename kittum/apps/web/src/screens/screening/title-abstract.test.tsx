import { afterEach, describe, expect, test } from "vite-plus/test";
import { page } from "vite-plus/test/browser";

import { hold, mount, press } from "../../test/app.tsx";

const QUEUE = "/p/ivosidenib-cca/screening/title-abstract";

const title = () => page.getByTestId("record-title");
const banner = () => page.getByTestId("ai-reveal");

const expectTitle = (text: string) => expect.element(title()).toMatchTextContent(text);

let unmount: (() => void) | undefined;
afterEach(() => unmount?.());

describe("title and abstract queue", () => {
  test("screens ten records by keyboard, with an undo and a reveal on disagreement", async () => {
    const app = mount(QUEUE);
    unmount = app.unmount;

    await expectTitle("ClarIDHy");
    await press("i");
    await expectTitle("ABC-06");
    await press("i");

    // ABC-02 does not say whether patients were pretreated. Excluding on criterion 2 makes the
    // judge's "unclear" (which means include) disagree, so the banner names criterion 2.
    await expectTitle("Cisplatin plus gemcitabine versus gemcitabine");
    await press("e");
    await press("2");
    await expect.element(banner()).toMatchTextContent("The AI judge disagrees");
    await expect.element(banner()).toMatchTextContent("2 Population");
    await expect.element(banner()).toMatchTextContent("Unclear");
    await expect
      .element(
        page
          .getByRole("article")
          .getByText("We randomly assigned 410 patients", { exact: false })
          .first(),
      )
      .toBeVisible();
    expect(document.querySelector("mark[data-reveal-quote]")?.textContent).toContain(
      "We randomly assigned 410 patients",
    );
    await press(" "); // keep

    await expectTitle("NIFTY");
    await press("i");
    await expectTitle("KEYNOTE-966");
    await press("e");
    await press("2");

    // A wrong criterion on FOENIX-CCA2, then undo and the right one.
    await expectTitle("Futibatinib");
    await press("e");
    await press("1");
    await expectTitle("BILCAP");
    await press("Backspace");
    await expectTitle("Futibatinib");
    await expect.element(page.getByTestId("decision-note")).toMatchTextContent("Excluded on 1");
    await press("e");
    await press("4");

    // Including BILCAP: the judge read a resection, so it reveals criterion 1 with its quote.
    await expectTitle("BILCAP");
    await press("i");
    await expect.element(banner()).toMatchTextContent("1 Population");
    await expect.element(banner()).toMatchTextContent("Not met");
    expect(document.querySelector("mark[data-reveal-quote]")?.textContent).toBe(
      "had undergone a macroscopically complete resection",
    );
    await press("e");
    await press("1"); // revise

    await expectTitle("NAPOLI-1");
    await press("e");
    await press("1");
    await expectTitle("systematic review, meta-analysis");
    await press("e");
    await press("5");
    await expectTitle("XELIRI");
    await press("u");

    await expectTitle("REACHIN");
    await expect.element(page.getByText("10 screened, 18 left")).toBeVisible();

    const finals = new Map(
      app.workbench.decisions
        .filter((d) => d.phase === "final")
        .map((d) => [
          d.assignmentId,
          [d.decision.outcome, d.decision.reason_criterion_id, d.revealed],
        ]),
    );
    expect(finals.size).toBe(10);
    expect(finals.get("asg-ta-20375404")).toEqual(["exclude", "C_POP_PRETREATED", true]);
    expect(finals.get("asg-ta-36652354")).toEqual(["exclude", "C_DES_RCT", false]);
    expect(finals.get("asg-ta-30922733")).toEqual(["exclude", "C_POP_ADV_BTC", true]);
    expect(finals.get("asg-ta-29955136")).toEqual(["unclear", null, false]);
    // Each decision reports when the record was shown and when the first key came.
    for (const d of app.workbench.decisions) {
      expect(Date.parse(d.timing.decided_at)).toBeGreaterThanOrEqual(Date.parse(d.timing.shown_at));
      expect(d.timing.first_key_at).not.toBeNull();
    }
  });

  test("never shows the AI's reading before the provisional decision", async () => {
    // With `reveal: always`, every record has a reading to show, agreeing or not.
    const app = mount(QUEUE, { reveal: "always" });
    unmount = app.unmount;
    const events: string[] = [];
    const seen = new Set<string>();
    const observer = new MutationObserver(() => {
      const shown = document.querySelector("[data-testid=ai-reveal], mark[data-reveal-quote]");
      const current = document.querySelector("[data-testid=record-title]")?.textContent ?? "";
      if (shown && !seen.has(current)) {
        seen.add(current);
        events.push(`shown:${current}`);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    const decided = () => app.workbench.decisions.length;

    for (const [expected, keys] of [
      ["ClarIDHy", ["i"]],
      ["ABC-06", ["u"]],
      ["Cisplatin plus gemcitabine", ["e", "4"]],
      ["NIFTY", ["i"]],
      ["KEYNOTE-966", ["i"]],
      ["Futibatinib", ["e", "4"]],
    ] as const) {
      await expectTitle(expected);
      expect(document.querySelector("[data-testid=ai-reveal]")).toBeNull();
      expect(document.querySelector("mark[data-reveal-quote]")).toBeNull();
      const before = decided();
      events.push(`decide:${expected}`);
      for (const key of keys) await press(key);
      await expect.element(banner()).toBeVisible();
      expect(decided()).toBe(before + 1);
      await press(" ");
    }
    observer.disconnect();

    // Every reveal came after the decision on that same record.
    for (const event of events.filter((e) => e.startsWith("shown:"))) {
      const record = event.slice(6);
      const decision = events.findIndex(
        (e) => e.startsWith("decide:") && record.includes(e.slice(7)),
      );
      expect(decision, `${record} was decided before its reveal`).toBeGreaterThanOrEqual(0);
      expect(decision).toBeLessThan(events.indexOf(event));
    }
    expect(events.filter((e) => e.startsWith("shown:"))).toHaveLength(6);
  });

  test("keeps the next five records loaded, so the next one shows at once", async () => {
    const requests: Array<{ after: string | null; limit: number }> = [];
    const app = mount(QUEUE, {
      latency: { queue: 1500 },
      log: (call, detail) => {
        if (call === "queue") requests.push(detail as { after: string | null; limit: number });
      },
    });
    unmount = app.unmount;
    await expect.element(title(), { timeout: 4000 }).toMatchTextContent("ClarIDHy");
    expect(requests[0]).toEqual(expect.objectContaining({ after: null, limit: 6 }));

    // The queue is slow, but the next records are already here.
    await press("i");
    await expect.element(title(), { timeout: 300 }).toMatchTextContent("ABC-06");
    await press("i");
    await expect
      .element(title(), { timeout: 300 })
      .toMatchTextContent("Cisplatin plus gemcitabine");
    // Each step asked for one more record behind the five.
    expect(requests.at(-1)?.limit).toBe(1);
  });

  test("undoing twice and moving forward again keeps each reopened record's decision", async () => {
    const app = mount(QUEUE);
    unmount = app.unmount;
    await expectTitle("ClarIDHy");
    await press("i");
    await expectTitle("ABC-06");
    await press("i");
    await expectTitle("Cisplatin plus gemcitabine");

    await press("Backspace");
    await expectTitle("ABC-06");
    await press("Backspace");
    await expectTitle("ClarIDHy");

    // Forward again: ABC-06 comes back with its decision, not as a fresh record.
    await press(" ");
    await expectTitle("ABC-06");
    await expect.element(page.getByTestId("decision-note")).toMatchTextContent("Included");
    // Re-deciding it uses its current revision, so the server accepts the change.
    await press("u");
    await expectTitle("Cisplatin plus gemcitabine");
    expect(document.querySelector("[role=alert]")).toBeNull();
    const abc06 = app.workbench.decisions.filter((d) => d.assignmentId === "asg-ta-33798493");
    expect(abc06.map((d) => d.decision.outcome)).toEqual(["include", "unclear"]);
  });

  test("shows an abstract without section labels whole, with its protocol terms", async () => {
    const app = mount(QUEUE, { reveal: "never" });
    unmount = app.unmount;
    await expectTitle("ClarIDHy");
    for (let i = 0; i < 20; i++) await press("i");
    // PMID 35510484's abstract has no labels: one unlabelled section, as the server sends it.
    await expectTitle("Multicenter phase II trial of trastuzumab deruxtecan");
    const record = page.getByRole("article", { name: "Record" });
    await expect
      .element(
        record.getByText("Trastuzumab deruxtecan (DS-8201) is an antibody-drug", { exact: false }),
      )
      .toBeVisible();
    expect(document.body.textContent).not.toContain("This record has no abstract");
    expect(document.querySelectorAll("article .term").length).toBeGreaterThan(0);
  });

  test("a held decision key decides one record, not the ones behind it", async () => {
    const app = mount(QUEUE);
    unmount = app.unmount;
    await expectTitle("ClarIDHy");
    await hold("i", 6);
    await expectTitle("ABC-06");
    await hold("u", 6);
    await expectTitle("Cisplatin plus gemcitabine");
    // Held `e` then held `2`: one exclusion, and the reveal waits for the reviewer.
    await hold("e", 3);
    await hold("2", 3);
    await expect.element(banner()).toBeVisible();
    // Let any queued repeat land before counting.
    await new Promise((resolve) => setTimeout(resolve, 300));
    await expectTitle("Cisplatin plus gemcitabine");
    expect(app.workbench.decisions.map((d) => d.assignmentId)).toEqual([
      "asg-ta-32416072",
      "asg-ta-33798493",
      "asg-ta-20375404",
    ]);
  });
});
