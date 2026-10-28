// One review, end to end, on the real stack (see stack.ts): two reviewers screen title and
// abstract blind and by keyboard, the AI is revealed only after a reviewer's own decision, the
// auditor screens the AI-only disagreements blind, the adjudicator resolves the human conflict
// before seeing the AI, both reviewers and the auditor screen a real PDF with adopted evidence,
// the lead links a study, and the progress numbers and the ledger check out.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { Browser, Page } from "@playwright/test";

import { SERVER, STACK_FILE } from "./stack.ts";

interface Stack {
  project_id: string;
  people: Record<string, string>;
  records: number;
}

const stack = (): Stack => JSON.parse(readFileSync(STACK_FILE, "utf8")) as Stack;

// What the recorded model said, so the test can tell AI output from anything else on screen.
const ANSWERS = JSON.parse(
  readFileSync(join(SERVER, "src", "kittum", "app", "demo_data", "judge_answers.json"), "utf8"),
) as Record<string, string>;
const RATIONALES = Object.entries(ANSWERS)
  .filter(([key]) => key.startsWith("title_abstract|"))
  .flatMap(([, text]) =>
    Object.values(JSON.parse(text) as Record<string, { rationale: string }>).map(
      (a) => a.rationale,
    ),
  )
  // The model left a rationale empty once; only sentences can be told apart from other text.
  .filter((r) => r.length > 30);
const rationaleOf = (titlePrefix: string, criterion: string) => {
  const entry = Object.entries(ANSWERS).find(([key]) =>
    key.startsWith(`title_abstract|${titlePrefix}`),
  );
  if (!entry) throw new Error(`no recorded answer for ${titlePrefix}`);
  return (
    (JSON.parse(entry[1]) as Record<string, { rationale: string }>)[criterion]?.rationale ?? ""
  );
};

const POP = ["e", "1"];
const INT = ["e", "2"];
const DESIGN = ["e", "3"];

// Title prefix to the keys every screener presses; the e-cigarette trial is the one conflict.
const PLAN: ReadonlyArray<readonly [string, ReadonlyArray<string>]> = [
  ["Nicotine patch versus placebo", ["i"]],
  ["Nicotine gum for smoking", ["i"]],
  ["Combination nicotine replacement", ["i"]],
  ["Varenicline and Nicotine Replacement Therapy for Smokers", ["i"]],
  ["Smoking Cessation After Initial Treatment Failure", ["i"]],
  ["Combination Treatment With Varenicline and Nicotine Patch", ["i"]],
  ["Nicotine lozenge", ["i"]], // sentinels
  ["Nicotine mouth spray", ["i"]],
  // The abstract does not say whether the participants smoke: unclear passes to full text.
  ["Community health worker-led", ["u"]],
  ["Transdermal nicotine for active ulcerative colitis", POP],
  ["Effect of Vitamin C", POP],
  ["Assessing acute nicotine impact", POP],
  ["Use of nicotine replacement therapy and quit success", DESIGN],
  ["Economic Evaluation", DESIGN],
  ["Genetic Prediction", DESIGN],
  ["Individual Predictors", DESIGN],
  ["Co-action and changes", DESIGN],
  ["Smoking Cessation Pharmacotherapy Efficacy", DESIGN],
  ["Community-Based Chronic Disease Care", DESIGN],
  ["Learning to quit", DESIGN],
];
const INCLUDED = 7; // six included records and the unclear one; sentinels are not records
const CONFLICT = "Electronic Nicotine-Delivery Systems";
const REVEALED = "Varenicline versus placebo for smoking cessation in adults with depression";
const RESCREENED = "Effect of a Personalized Tobacco Treatment Intervention";

const keysFor = (title: string, overrides: Record<string, ReadonlyArray<string>> = {}) => {
  const override = Object.entries(overrides).find(([prefix]) => title.startsWith(prefix));
  if (override) return override[1];
  return PLAN.find(([prefix]) => title.startsWith(prefix))?.[1] ?? INT; // the rest: no NRT arm
};

/** Everything the server sent this page, to check later what it saw. */
const recordResponses = (page: Page) => {
  const bodies: Array<{ url: string; body: string }> = [];
  page.on("response", async (response) => {
    if (!response.url().includes("/api/")) return;
    try {
      bodies.push({ url: response.url(), body: await response.text() });
    } catch {
      // a navigation dropped it
    }
  });
  return bodies;
};

const open = async (browser: Browser, person: string, path: string) => {
  const { project_id, people } = stack();
  const page = await (await browser.newContext()).newPage();
  const responses = recordResponses(page);
  await page.goto(`/p/${project_id}/${path}?as=${people[person]}`);
  await expect(page.getByTestId("identity")).toContainText(person);
  return { page, responses };
};

/**
 * Screens the whole title and abstract queue by keyboard. Keeps the decision when the AI is
 * revealed, and calls `onReveal` with the record's title. Returns every title screened.
 */
const screenQueue = async (
  page: Page,
  options: {
    overrides?: Record<string, ReadonlyArray<string>>;
    before?: (title: string) => Promise<void>;
    onReveal?: (title: string) => Promise<void>;
    /** A record to decide wrongly first, then reopen with Backspace and decide again. */
    slip?: { readonly prefix: string; readonly keys: ReadonlyArray<string>; readonly note: string };
  } = {},
) => {
  const title = page.getByTestId("record-title");
  const banner = page.getByTestId("ai-reveal");
  const finished = page.getByRole("heading", { name: "Queue finished" });
  const seen: string[] = [];
  for (;;) {
    await expect(title.or(finished)).toBeVisible();
    if (await finished.isVisible()) return seen;
    const text = (await title.textContent()) ?? "";
    await options.before?.(text);
    const slip = options.slip && text.startsWith(options.slip.prefix) ? options.slip : null;
    if (slip) {
      for (const key of slip.keys) await page.keyboard.press(key);
      await expect
        .poll(async () => (await banner.isVisible()) || (await title.textContent()) !== text)
        .toBe(true);
      if ((await title.textContent()) === text) await page.keyboard.press("Space");
      await expect(title).not.toHaveText(text);
      await page.keyboard.press("Backspace");
      await expect(title).toHaveText(text);
      await expect(page.getByTestId("decision-note")).toContainText(slip.note);
    }
    for (const key of keysFor(text, options.overrides)) await page.keyboard.press(key);
    await expect
      .poll(
        async () =>
          (await banner.isVisible()) ||
          (await finished.isVisible()) ||
          (await title.textContent()) !== text,
      )
      .toBe(true);
    if ((await banner.isVisible()) && (await title.textContent()) === text) {
      await options.onReveal?.(text);
      await page.keyboard.press("Space"); // keep my decision
      await expect
        .poll(async () => (await finished.isVisible()) || (await title.textContent()) !== text)
        .toBe(true);
    }
    seen.push(text);
  }
};

/**
 * Screens every full text in the queue (the record and the full-text sentinel, in the reviewer's
 * seeded order): evidence for the population, a verdict per criterion, exclude, and keep the
 * decision if the AI's reading is shown. Returns the titles and whether the AI was revealed.
 */
const screenFullTexts = async (page: Page) => {
  const heading = page.getByRole("complementary", { name: "Criteria" }).getByRole("heading");
  const finished = page.getByRole("heading", { name: "Full texts finished" });
  const notice = page.getByTestId("fulltext-notice");
  const reveal = page.getByTestId("ai-reveal");
  const seen: Array<{ title: string; revealed: boolean }> = [];
  for (;;) {
    // The AI reads a new full-text stage first; until then the queue is empty.
    await expect(heading.or(finished)).toBeVisible({ timeout: 60_000 });
    if (await finished.isVisible()) return seen;
    const title = (await heading.textContent()) ?? "";
    await expect(reveal).toHaveCount(0);
    await page.keyboard.press("]"); // the first protocol-term mention of the population
    await expect(page.getByTestId("mention").first()).toHaveAttribute("aria-current", "true");
    await page.keyboard.press("Enter"); // adopt it: the server saves it as an anchor
    await expect(notice).toContainText("Adopted as evidence");
    for (const key of ["n", "?", "?", "y", "?"]) await page.keyboard.press(key);
    await page.keyboard.press("e");
    await expect(reveal.or(notice.getByText("Excluded on Population"))).toBeVisible();
    const revealed = await reveal.isVisible();
    if (revealed) {
      await expect(reveal).toContainText("read this differently");
      await page.keyboard.press("Space"); // keep my decision
    }
    await expect(notice).toContainText("Excluded on Population");
    seen.push({ title, revealed });
    await expect(heading.or(finished)).not.toHaveText(title);
  }
};

test("a review from search to full text, blind, audited, adjudicated, and verified", async ({
  browser,
}) => {
  const { project_id, people } = stack();

  await test.step("reviewer A screens title and abstract; the AI shows only after A decides", async () => {
    const { page } = await open(browser, "Sam Okafor", "screening/title-abstract");
    let revealed = 0;
    const seen = await screenQueue(page, {
      overrides: { [CONFLICT]: ["i"] },
      // A first excludes the economic evaluation on the intervention, takes it back, and
      // excludes it on the design. The server keeps both decisions; the second one stands.
      slip: { prefix: "Economic Evaluation", keys: INT, note: "Excluded on 2" },
      before: async (title) => {
        // Before a decision, nothing of the AI is on screen.
        await expect(page.getByTestId("ai-reveal")).toHaveCount(0);
        if (title.startsWith(REVEALED)) {
          await expect(page.locator("body")).not.toContainText(rationaleOf(REVEALED, "INT"));
        }
      },
      onReveal: async (title) => {
        revealed++;
        if (!title.startsWith(REVEALED)) return;
        // A excluded for want of an NRT arm; the model read the intervention as met.
        const banner = page.getByTestId("ai-reveal");
        await expect(banner).toContainText("The AI judge disagrees");
        await expect(banner).toContainText("2 Intervention");
        await expect(banner).toContainText("Met");
        await expect(page.locator("body")).toContainText(rationaleOf(REVEALED, "INT"));
      },
    });
    expect(seen).toHaveLength(stack().records + 1); // and one sentinel
    expect(seen.some((t) => t.startsWith(REVEALED))).toBe(true);
    expect(revealed).toBeGreaterThan(0);

    // The server agrees: after a reload there is nothing left in A's queue.
    await page.reload();
    await expect(page.getByRole("heading", { name: "Queue finished" })).toBeVisible();
    await page.context().close();
  });

  await test.step("reviewer B screens blind: nothing of A, nothing of the AI before B decides", async () => {
    const { page, responses } = await open(browser, "Jun Park", "screening/title-abstract");
    await screenQueue(page, { overrides: { [CONFLICT]: INT } });
    // Every screening answer B received: no trace of A, and AI rationales only in answers to B's
    // own provisional decisions (a reveal), never in a queue item B had not decided. (Who is on
    // the team is no secret; the project's member list names A.)
    const screening = responses.filter(({ url }) => url.includes("/screening/"));
    expect(screening.length).toBeGreaterThan(20);
    for (const { url, body } of screening) {
      expect(body).not.toContain(people["Sam Okafor"]);
      if (RATIONALES.some((r) => body.includes(r))) {
        expect(url).toMatch(/\/decide$/);
      }
    }
    // B's progress page shows B's own counts and none of the lead's figures.
    await page.goto(`/p/${project_id}/progress?as=${people["Jun Park"]}`);
    await expect(page.getByText("Your own work")).toBeVisible();
    await expect(page.getByTestId("kappa")).toHaveCount(0);
    await page.context().close();
  });

  await test.step("the auditor screens the audit sample and the AI-only disagreements, blind", async () => {
    const { page } = await open(browser, "Rita Nunes", "screening/title-abstract");
    const seen = await screenQueue(page, { overrides: { [CONFLICT]: INT } });
    // Both screeners excluded this one while the model saw no failing criterion, so the auditor
    // got a blind extra screen of it, alike in every way to the rest of the audit queue.
    expect(seen.some((t) => t.startsWith(RESCREENED))).toBe(true);
    await page.context().close();
  });

  await test.step("the adjudicator resolves the human conflict before seeing the AI", async () => {
    const { page, responses } = await open(browser, "Elena Weiss", "adjudication");
    await expect(page.getByTestId("record-title")).toContainText(CONFLICT);
    await expect(page.getByTestId("ai-column")).toHaveCount(0);
    for (const { body } of responses) {
      for (const r of RATIONALES) expect(body).not.toContain(r);
    }
    // Pseudonyms only, in random order: adopt whichever column excluded.
    const columns = [page.getByTestId("column-A"), page.getByTestId("column-B")];
    const excluder = (await columns[0]?.textContent())?.includes("Exclude") ? "1" : "2";
    await page.keyboard.press(excluder);
    await expect(page.getByTestId("adjudicated")).toContainText("Excluded on 2 Intervention");
    await expect(page.getByTestId("ai-column")).toBeVisible();
    await page.keyboard.press("Space");
    await expect(page.getByRole("heading", { name: "No open conflicts" })).toBeVisible();
    await page.context().close();
  });

  await test.step("the lead sees the title and abstract numbers and opens full text", async () => {
    const { page } = await open(browser, "Morgan Lee", "progress");
    await expect(page.getByTestId("flow-identified")).toHaveText("30");
    await expect(page.getByTestId("duplicates")).toHaveText("1");
    await expect(page.getByTestId("flow-title_abstract-nrt-vs-control-screened")).toHaveText("29");
    await expect(page.getByTestId("flow-title_abstract-nrt-vs-control-included")).toHaveText(
      String(INCLUDED),
    );
    await expect(page.getByTestId("title_abstract-nrt-vs-control-excluded")).toHaveText(
      String(29 - INCLUDED),
    );
    await expect(page.getByTestId("title_abstract-nrt-vs-control-conflict")).toHaveText("0");
    await expect(page.getByTestId("title_abstract-nrt-vs-control-pending")).toHaveText("0");
    await expect(page.getByTestId("title_abstract-adjudicated")).toHaveText("1");
    await expect(page.getByTestId("title_abstract-rescreens")).toContainText("0 waiting");
    // Blind provisional decisions: A and B disagree on one of 29 records (8 against 7 included).
    const pe = (8 * 7 + 21 * 22) / 29 ** 2;
    await expect(page.getByTestId("kappa").first()).toHaveText(
      ((28 / 29 - pe) / (1 - pe)).toFixed(2),
    );

    await page.keyboard.press("n"); // open full-text screening for the scope
    // One record passed with a full text; the sentinel is not a record.
    await expect(page.getByTestId("flow-full_text-nrt-vs-control-screened")).toHaveText("1");
    await page.context().close();
  });

  await test.step("both reviewers screen the full text with adopted evidence", async () => {
    for (const person of ["Sam Okafor", "Jun Park"]) {
      const { page } = await open(browser, person, "screening/full-text");
      const seen = await screenFullTexts(page);
      // The trial and the sentinel, alike: nothing tells the reviewer which is which.
      expect(seen.map((s) => s.title).sort()).toEqual([
        expect.stringMatching(/^Community health worker-led/),
        expect.stringMatching(/^Effect of Vitamin C/),
      ]);
      // The model saw no failing criterion in the trial, so excluding it reveals its reading.
      expect(seen.find((s) => s.title.startsWith("Community"))?.revealed).toBe(true);
      await page.context().close();
    }
  });

  await test.step("the auditor gets the full text as an extra screen and agrees", async () => {
    const { page } = await open(browser, "Rita Nunes", "screening/full-text");
    const seen = await screenFullTexts(page);
    expect(seen.some((s) => s.title.startsWith("Community health worker-led"))).toBe(true);
    await page.context().close();
  });

  await test.step("the lead confirms the study link and checks the flow and the ledger", async () => {
    const { page } = await open(browser, "Morgan Lee", "search");
    // PRISMA-S: what each source reported and what Kittum retrieved, per search.
    await expect(page.getByTestId("search-counts")).toHaveText([
      /3 reported, 3 retrieved/,
      /7 reported, 7 retrieved/,
      /20 reported, 20 retrieved/,
    ]);
    await page.goto(`/p/${project_id}/studies?as=${people["Morgan Lee"]}`);
    const proposal = page.getByTestId("proposal").first();
    await expect(proposal).toContainText("NCT05596773");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("studies-notice")).toContainText(/onfirmed/);

    await page.goto(`/p/${project_id}/progress?as=${people["Morgan Lee"]}`);
    await expect(page.getByTestId("flow-full_text-nrt-vs-control-included")).toHaveText("0");
    await expect(page.getByTestId("full_text-nrt-vs-control-excluded")).toHaveText("1");
    await expect(page.getByTestId("full_text-rescreens")).toContainText(
      "1 sent, 0 waiting, 1 upheld",
    );
    // 29 reports in 29 studies, until the paper and its registry entry became one study.
    await expect(page.getByTestId("flow-studies")).toHaveText("28");
    await expect(page.getByTestId("ledger")).toContainText("Ledger verified");
    await page.context().close();

    const verified = execFileSync("uv", ["run", "kittum", "ledger", "verify", project_id], {
      cwd: SERVER,
      encoding: "utf8",
    });
    expect(verified).toContain("OK project");
  });
});
