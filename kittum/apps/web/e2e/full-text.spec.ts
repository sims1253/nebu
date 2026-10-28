// Full-text screening on the real stack (see stack.ts): one reason per decision, the AI's reading
// with its quote after the reviewer decides, and the reviewer's "wrong document" (issue #60).
// A small review is set up through `kittum project`. Both reviewers include the three reports at
// title and abstract. The lead attaches the Lesotho trial's PDF to the trial, and a reporting
// checklist to the cohort study, as the open-access sources did in the demo. Sam marks the
// checklist as the wrong document: the report leaves both queues, the record is back to missing
// its full text, and PRISMA counts it as not retrieved. Sam includes the trial in one action. Jun
// excludes it on the population, with evidence for that criterion alone, and sees why the AI
// disagrees. The adjudicator sees both reasons, then the AI's reading.
//
// With KITTUM_SHOTS set to a directory, the journey saves its screenshots there.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { SERVER, stackEnv } from "./stack.ts";

const EXAMPLES = join(SERVER, "..", "..", "examples");
const FIXTURES = join(SERVER, "tests", "documents", "fixtures");
const TRIAL_PDF = join(FIXTURES, "gerber2026-lesotho-t2d-crct.pdf");
const SENTINEL_PDF = join(FIXTURES, "li2026-vitamin-c-kidney-biopsy-rct.pdf");
const SENTINELS = join(EXAMPLES, "sentinels", "nicotine-replacement.yaml");
const TRIAL = "Community health worker-led";
const COHORT = "Community-Based Chronic Disease Care";
const NOTE = "The journal's CONSORT-EHEALTH checklist, not the cohort study.";

const cli = (...args: string[]) =>
  JSON.parse(
    execFileSync("uv", ["run", "kittum", "project", ...args], {
      cwd: SERVER,
      env: stackEnv(),
      encoding: "utf8",
    }),
  ) as Record<string, string>;

/** A three-page reporting checklist of the kind open-access sources list as a work's PDF. */
const checklistPdf = () => {
  const path = join(mkdtempSync(join(tmpdir(), "kittum-checklist-")), "checklist.pdf");
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      "import sys, pymupdf; d = pymupdf.open()\n" +
        "for n in range(3):\n" +
        "    d.new_page().insert_text((72, 72), f'CONSORT-EHEALTH checklist V1.6.1, page {n + 1}\\n" +
        "1a-i) Identify the mode of delivery in the title\\n" +
        "1b) Structured summary of trial design, methods, results, and conclusions')\n" +
        "d.save(sys.argv[1])",
      path,
    ],
    { cwd: SERVER },
  );
  return readFileSync(path);
};

const shot = async (page: Page, name: string) => {
  const dir = process.env["KITTUM_SHOTS"];
  if (dir) await page.screenshot({ path: join(dir, name) });
};

test("one reason per full-text decision, the AI's reading after it, and a wrong document", async ({
  browser,
}) => {
  const created = cli("create", `Full text ${new Date().toISOString()}`, "--lead", "Morgan Lee");
  const projectId = created["project_id"] ?? "";
  const leadId = created["lead"] ?? "";
  const by = ["--by", leadId];
  const person = (name: string, role: string) =>
    cli("add-member", projectId, name, "--role", role, ...by)["actor_id"] ?? "";
  const sam = person("Sam Okafor", "reviewer");
  const jun = person("Jun Park", "reviewer");
  const rita = person("Rita Nunes", "auditor");
  const elena = person("Elena Weiss", "adjudicator");
  cli(
    "publish-protocol",
    projectId,
    join(EXAMPLES, "protocols", "nicotine-replacement.yaml"),
    ...by,
  );
  cli(
    "import-search",
    projectId,
    join(SERVER, "src", "kittum", "app", "demo_data", "citation_search.ris"),
    "--topic",
    "TRIALS",
    "--source",
    "citation_search",
    "--platform",
    "Reference lists",
    "--strategy",
    "Backward citation search",
    "--format",
    "ris",
    ...by,
  );
  cli("ingest", projectId, ...by);
  cli("open-stage", projectId, "nrt-vs-control", "title_abstract", "--sentinels", SENTINELS, ...by);
  const open = async (id: string, path: string) => {
    const page = await (await browser.newContext()).newPage();
    await page.goto(`/p/${projectId}/${path}?as=${id}`);
    return page;
  };

  await test.step("both reviewers include everything at title and abstract; a reveal explains itself", async () => {
    for (const id of [sam, jun, rita]) {
      const page = await open(id, "screening/title-abstract");
      const title = page.getByTestId("record-title");
      const finished = page.getByRole("heading", { name: "Queue finished" });
      const banner = page.getByTestId("ai-reveal");
      for (;;) {
        await expect(title.or(finished)).toBeVisible({ timeout: 60_000 });
        if (await finished.isVisible()) break;
        const text = await title.textContent();
        await expect(banner).toHaveCount(0); // nothing of the AI before the decision
        await page.keyboard.press("i");
        await expect
          .poll(
            async () =>
              (await banner.isVisible()) ||
              (await finished.isVisible()) ||
              (await title.textContent().catch(() => null)) !== text,
            { timeout: 60_000 },
          )
          .toBe(true);
        if (await banner.isVisible()) {
          // The criterion, its verdict, the rationale, and the quote: marked in the record if
          // the judge verified it, never otherwise.
          await expect(banner.getByTestId("ai-criterion")).not.toBeEmpty();
          await expect(banner.getByTestId("ai-rationale")).not.toBeEmpty();
          const quote = banner.getByTestId("ai-quote");
          if ((await quote.getAttribute("data-verified")) === "true") {
            await expect(page.locator("mark[data-reveal-quote]")).toBeVisible();
          } else {
            await expect(page.locator("mark[data-reveal-quote]")).toHaveCount(0);
          }
          if (id === sam) await shot(page, "01-title-abstract-reveal.png");
          await page.keyboard.press("Space"); // keep my decision
        }
      }
      await page.context().close();
    }
  });

  const lead = await open(leadId, "retrieval");
  const as = (id: string) => ({ "X-Kittum-Reviewer": id });
  const reports = async () =>
    (await (
      await lead.request.get(`/api/projects/${projectId}/full-texts`, { headers: as(leadId) })
    ).json()) as Array<{ item_ref: string; title: string; full_text: unknown }>;
  const ref = async (prefix: string) => (await reports()).find((r) => r.title.startsWith(prefix))!;

  await test.step("the lead attaches the trial's PDF, a checklist as the cohort study's, and a sentinel", async () => {
    const pdf = { "Content-Type": "application/pdf" };
    for (const [prefix, data] of [
      [TRIAL, readFileSync(TRIAL_PDF)],
      [COHORT, checklistPdf()],
    ] as const) {
      const report = await ref(prefix);
      const attached = await lead.request.post(
        `/api/projects/${projectId}/full-texts/${report.item_ref}?actor_id=${leadId}`,
        { headers: pdf, data },
      );
      expect(attached.status()).toBe(201);
    }
    const sentinel = await lead.request.post(
      `/api/projects/${projectId}/full-text-sentinels?actor_id=${leadId}&title=Vitamin%20C&expected_outcome=exclude`,
      { headers: pdf, data: readFileSync(SENTINEL_PDF) },
    );
    expect(sentinel.status()).toBe(201);
    const opened = await lead.request.post(`/api/projects/${projectId}/screening/stages`, {
      data: { scope_id: "nrt-vs-control", stage: "full_text", actor_id: leadId },
    });
    expect(opened.status()).toBe(201);
  });

  const heading = (page: Page) =>
    page.getByRole("complementary", { name: "Criteria" }).getByRole("heading", { level: 1 });
  const nextFullText = async (page: Page) => {
    const finished = page.getByRole("heading", { name: "Full texts finished" });
    await expect(heading(page).or(finished)).toBeVisible({ timeout: 60_000 });
    return (await finished.isVisible()) ? null : ((await heading(page).textContent()) ?? "");
  };
  const excludeOnPopulation = async (page: Page) => {
    const notice = page.getByTestId("fulltext-notice");
    await page.keyboard.press(".");
    await page.keyboard.press("Enter");
    await expect(notice).toContainText("Adopted as evidence for 1 Population");
    await page.keyboard.press("e");
    await page.keyboard.press("1");
    const reveal = page.getByTestId("ai-reveal");
    await expect(reveal.or(notice.getByText("Excluded on 1 Population"))).toBeVisible();
    return reveal;
  };

  await test.step("Sam marks the checklist as the wrong document and includes the trial in one action", async () => {
    const page = await open(sam, "screening/full-text");
    const seen: string[] = [];
    for (let title = await nextFullText(page); title !== null; title = await nextFullText(page)) {
      seen.push(title);
      const notice = page.getByTestId("fulltext-notice");
      if (title.startsWith(COHORT)) {
        // The three-page checklist, where the cohort study should be.
        await expect(page.getByText("Page 1 of 3")).toBeVisible();
        await expect(page.locator("[data-page='1'] img")).toBeVisible();
        await shot(page, "02-full-text-checklist-shown.png");
        await page.keyboard.press("w");
        const form = page.getByTestId("wrong-document");
        await expect(form).toContainText("This is not an exclusion.");
        await page.getByLabel("What is the PDF instead? Optional.").fill(NOTE);
        await shot(page, "03-wrong-document-form.png");
        await page.keyboard.press("Enter");
        await expect(page.getByTestId("wrong-document-sent")).toContainText(
          "The record now shows as missing its full text.",
        );
        await shot(page, "04-wrong-document-sent.png");
      } else if (title.startsWith(TRIAL)) {
        // No verdict on any criterion, no evidence: include says every criterion is met.
        await expect(notice).toContainText("You do not need a verdict on every criterion.");
        await page.keyboard.press("i");
        await expect(notice).toContainText("Included");
        await shot(page, "05-include-in-one-action.png");
      } else {
        const reveal = await excludeOnPopulation(page);
        if (await reveal.isVisible()) await page.keyboard.press("Space");
        await expect(notice).toContainText("Excluded on 1 Population");
      }
      await expect(
        heading(page).or(page.getByRole("heading", { name: "Full texts finished" })),
      ).not.toHaveText(title);
    }
    expect(seen.some((t) => t.startsWith(COHORT))).toBe(true);
    await page.context().close();
  });

  await test.step("the record is missing its full text again, and listed so", async () => {
    expect((await ref(COHORT)).full_text).toBeNull();
    expect((await ref(TRIAL)).full_text).not.toBeNull();
    await lead.reload();
    await expect(lead.getByTestId("retrieval-row").filter({ hasText: COHORT })).toBeVisible();
    await shot(lead, "06-retrieval-missing-again.png");
  });

  await test.step("Jun no longer gets the checklist, excludes the trial on one criterion, and sees why the AI disagrees", async () => {
    const page = await open(jun, "screening/full-text");
    const seen: string[] = [];
    for (let title = await nextFullText(page); title !== null; title = await nextFullText(page)) {
      seen.push(title);
      const notice = page.getByTestId("fulltext-notice");
      const reveal = await excludeOnPopulation(page);
      if (title.startsWith(TRIAL)) {
        // The model saw no failing criterion in the trial: it names the criterion, its verdict,
        // and its rationale. It quoted nothing, so nothing is marked on the page.
        await expect(reveal).toContainText("The AI judge disagrees");
        await expect(reveal.getByTestId("ai-criterion")).toHaveText("1 Population");
        await expect(reveal).toContainText("Unclear");
        await expect(reveal.getByTestId("ai-rationale")).toContainText("smokers");
        await expect(reveal.getByTestId("ai-quote")).toHaveText("The AI gave no quote.");
        await expect(page.getByTestId("ai-quote-highlight")).toHaveCount(0);
        await shot(page, "07-full-text-reveal.png");
      }
      if (await reveal.isVisible()) await page.keyboard.press("Space");
      await expect(notice).toContainText("Excluded on 1 Population");
      await expect(
        heading(page).or(page.getByRole("heading", { name: "Full texts finished" })),
      ).not.toHaveText(title);
    }
    expect(seen.some((t) => t.startsWith(COHORT))).toBe(false);
    expect(seen.some((t) => t.startsWith(TRIAL))).toBe(true);
    await page.context().close();
  });

  await test.step("the adjudicator sees both reasons, decides, then sees the AI's reading", async () => {
    const page = await open(elena, "adjudication");
    await expect(page.getByTestId("record-title")).toContainText(TRIAL);
    await expect(page.getByTestId("ai-column")).toHaveCount(0);
    const columns = [page.getByTestId("column-A"), page.getByTestId("column-B")];
    const excluder = (await columns[0]?.textContent())?.includes("Exclude") ? "1" : "2";
    await expect(columns[Number(excluder) - 1]!).toContainText("on 1 Population");
    await page.keyboard.press(excluder);
    await expect(page.getByTestId("adjudicated")).toContainText("Excluded on 1 Population");
    const ai = page.getByTestId("ai-column");
    await expect(ai).toBeVisible();
    await expect(ai.getByTestId("ai-reading").first()).toBeVisible();
    await shot(page, "08-adjudication-ai-reading.png");
    await page.keyboard.press(".");
    await shot(page, "09-adjudication-next-criterion.png");
    await page.context().close();
  });

  await test.step("PRISMA counts the checklist's record as not retrieved, never as excluded", async () => {
    const cohort = (await ref(COHORT)).item_ref;
    const asOf = (await (
      await lead.request.get(`/api/projects/${projectId}/audit/as-of`, { headers: as(leadId) })
    ).json()) as {
      flows: Array<{
        scope_id: string;
        members: Record<string, string[]>;
        checks: Array<{ holds: boolean }>;
      }>;
    };
    const flow = asOf.flows.find((f) => f.scope_id === "nrt-vs-control")!;
    expect(flow.members["reports_not_retrieved"]).toContain(cohort);
    expect(flow.members["reports_excluded"] ?? []).not.toContain(cohort);
    expect(flow.checks.every((c) => c.holds)).toBe(true);
    const trace = (await (
      await lead.request.get(`/api/projects/${projectId}/audit/records/${cohort}/trace`, {
        headers: as(leadId),
      })
    ).json()) as { events: Array<{ summary: string; facts: Array<{ value: unknown }> }> };
    const detached = trace.events.find((e) => e.summary.endsWith("as the wrong document"));
    expect(detached?.facts.map((f) => f.value)).toContain(NOTE);
    await lead.keyboard.press("h");
    await expect(lead.getByRole("dialog", { name: "Keys" })).toBeVisible();
    await lead
      .getByRole("dialog", { name: "Keys" })
      .getByText("Full text", { exact: true })
      .scrollIntoViewIfNeeded();
    await shot(lead, "10-keys.png");
    await lead.context().close();
  });
});
