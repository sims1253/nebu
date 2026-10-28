// Extraction from the browser on a real trial PDF, on the real stack (see stack.ts). A small review
// is set up through `kittum project` (the browser path is setup.spec.ts), and screened in the
// browser: both reviewers include at title and abstract, the methods lead uploads the Lesotho
// trial's PDF and a full-text sentinel on Retrieval, and nothing can be extracted until both
// reviewers include the trial at full text. Then the lead requests a run with its extraction
// schema, the Janus path only, so no model is needed; the stack's worker runs it. The auditor,
// whose audit screen of the trial is still open, learns nothing of the run. A reviewer accepts a
// value Janus read from Table 1 once its source was on screen, corrects one Janus missed by
// selecting it on the page, and finds both among the accepted values the reports use.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { expectNothingLeaked, recordResponses } from "./blind.ts";
import { SERVER, stackEnv } from "./stack.ts";

const EXAMPLES = join(SERVER, "..", "..", "examples");
const PDF = join(SERVER, "tests", "documents", "fixtures", "gerber2026-lesotho-t2d-crct.pdf");
const SCHEMA = join(SERVER, "tests", "extraction", "fixtures", "lesotho_schema.yaml");
const TRIAL = "Community health worker-led";
const SENTINELS = join(EXAMPLES, "sentinels", "nicotine-replacement.yaml");
const SENTINEL_PDF = join(
  SERVER,
  "tests",
  "documents",
  "fixtures",
  "li2026-vitamin-c-kidney-biopsy-rct.pdf",
);
const SENTINEL_TITLE =
  "Effect of Vitamin C on bleeding and pain after kidney biopsy: a randomized controlled trial";
// The 12-month HbA1c of the intervention arm, in the text on page 5; Janus reads tables only.
const SPAN = "6.5% (SD 1.3) in the intervention";

const cli = (...args: string[]) =>
  JSON.parse(
    execFileSync("uv", ["run", "kittum", "project", ...args], {
      cwd: SERVER,
      env: stackEnv(),
      encoding: "utf8",
    }),
  ) as Record<string, string>;

const fieldIndex = async (page: Page, projectId: string, runId: string, as: string) => {
  const review = (await (
    await page.request.get(`/api/projects/${projectId}/extraction/runs/${runId}/review`, {
      headers: { "X-Kittum-Reviewer": as },
    })
  ).json()) as {
    layer: { text: string };
    fields: Array<{ key: { endpoint: string; arm: string; quantity: string } }>;
  };
  const at = (endpoint: string, arm: string, quantity: string) =>
    review.fields.findIndex(
      (f) => f.key.endpoint === endpoint && f.key.arm === arm && f.key.quantity === quantity,
    );
  return { text: review.layer.text, at };
};

/** Screens a person's whole title and abstract queue: includes everything, keeps it on a reveal. */
const includeAll = async (page: Page) => {
  const title = page.getByTestId("record-title");
  const finished = page.getByRole("heading", { name: "Queue finished" });
  const banner = page.getByTestId("ai-reveal");
  // The record on screen now, or null between records and while the AI is still reading: never
  // wait for a title that may not come back (as in review.spec.ts).
  const titleNow = async () =>
    (await title.count()) > 0
      ? await title.textContent({ timeout: 1_000 }).catch(() => null)
      : null;
  for (;;) {
    await expect(title.or(finished)).toBeVisible({ timeout: 60_000 });
    if (await finished.isVisible()) return;
    const text = await titleNow();
    await page.keyboard.press("i");
    await expect
      .poll(
        async () =>
          (await banner.isVisible()) || (await finished.isVisible()) || (await titleNow()) !== text,
        { timeout: 60_000 },
      )
      .toBe(true);
    if (await banner.isVisible()) await page.keyboard.press("Space"); // keep my decision
  }
};

/** Screens a person's full texts: the trial included with every criterion met, the rest
 * excluded on the population, each on the first protocol-term mention adopted as evidence. */
const screenFullTexts = async (page: Page) => {
  const heading = page.getByRole("complementary", { name: "Criteria" }).getByRole("heading");
  const finished = page.getByRole("heading", { name: "Full texts finished" });
  const notice = page.getByTestId("fulltext-notice");
  const reveal = page.getByTestId("ai-reveal");
  for (;;) {
    await expect(heading.or(finished)).toBeVisible({ timeout: 60_000 });
    if (await finished.isVisible()) return;
    const title = (await heading.textContent()) ?? "";
    await page.keyboard.press("]");
    await page.keyboard.press("Enter");
    await expect(notice).toContainText("Adopted as evidence");
    const include = title.startsWith(TRIAL);
    for (const key of include ? ["y", "y", "y", "y", "y"] : ["n", "?", "?", "y", "?"]) {
      await page.keyboard.press(key);
    }
    await page.keyboard.press(include ? "i" : "e");
    const done = include ? "Included" : "Excluded on Population";
    await expect(reveal.or(notice.getByText(done))).toBeVisible();
    if (await reveal.isVisible()) await page.keyboard.press("Space");
    await expect(notice).toContainText(done);
    await expect(heading.or(finished)).not.toHaveText(title);
  }
};

test("a reviewer accepts and corrects values Janus read from a trial the screeners included", async ({
  browser,
}) => {
  const created = cli("create", `Extraction ${new Date().toISOString()}`, "--lead", "Morgan Lee");
  const projectId = created["project_id"] ?? "";
  const leadId = created["lead"] ?? "";
  const by = ["--by", leadId];
  const person = (name: string, role: string) =>
    cli("add-member", projectId, name, "--role", role, ...by)["actor_id"] ?? "";
  const sam = person("Sam Okafor", "reviewer");
  const jun = person("Jun Park", "reviewer");
  const rita = person("Rita Nunes", "auditor");
  person("Elena Weiss", "adjudicator");
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
    ...["--topic", "TRIALS", "--source", "citation_search", "--platform", "Reference lists"],
    ...["--strategy", "Backward citation search", "--format", "ris", ...by],
  );
  cli("ingest", projectId, ...by);
  cli("open-stage", projectId, "nrt-vs-control", "title_abstract", "--sentinels", SENTINELS, ...by);

  await test.step("both reviewers include the three reports at title and abstract", async () => {
    for (const id of [sam, jun]) {
      const page = await (await browser.newContext()).newPage();
      await page.goto(`/p/${projectId}/screening/title-abstract?as=${id}`);
      await includeAll(page);
      await page.context().close();
    }
  });

  const lead = await (await browser.newContext()).newPage();
  await test.step("the lead uploads the trial's PDF and a full-text sentinel on Retrieval", async () => {
    await lead.goto(`/p/${projectId}/retrieval?as=${leadId}`);
    await expect(lead.getByTestId("retrieval-counts")).toHaveText(
      "0 of 3 sought for retrieval retrieved",
    );
    const titles = await lead.getByTestId("retrieval-row").locator("h2").allTextContents();
    for (let i = 0; i < titles.findIndex((t) => t.startsWith(TRIAL)); i++) {
      await lead.keyboard.press("j");
    }
    await expect(lead.locator('[data-testid=retrieval-row][aria-current="true"]')).toContainText(
      TRIAL,
    );
    await lead.keyboard.press("u");
    const dialog = lead.getByRole("dialog", { name: "Upload the full text" });
    await dialog.getByLabel("PDF file").setInputFiles(PDF);
    await dialog.getByLabel("Licence").fill("CC BY 4.0");
    await dialog.getByRole("button", { name: "Attach the PDF" }).click();
    await expect(lead.getByTestId("retrieval-notice")).toContainText(
      "Attached a 13-page full text",
    );
    await expect(lead.getByTestId("retrieval-counts")).toHaveText(
      "1 of 3 sought for retrieval retrieved",
    );
    await lead.keyboard.press("s");
    const sentinel = lead.getByRole("dialog", { name: "Add a full-text sentinel" });
    await sentinel.getByLabel("Title").fill(SENTINEL_TITLE);
    await sentinel.getByLabel("Sentinel PDF").setInputFiles(SENTINEL_PDF);
    await sentinel.getByRole("button", { name: "Add the sentinel" }).click();
    await expect(lead.getByTestId("retrieval-notice")).toContainText(
      "Added the full-text sentinel",
    );
    // It is no report: the list does not grow.
    await expect(lead.getByTestId("retrieval-row")).toHaveCount(3);
  });

  await test.step("nothing is extracted before the trial is included at full text", async () => {
    await lead.keyboard.press("g");
    await lead.keyboard.press("x");
    await expect(lead.getByText(/^No runs yet\./)).toBeVisible();
    await lead.keyboard.press("n");
    const dialog = lead.getByRole("dialog", { name: "Request an extraction run" });
    await expect(dialog.getByText("No report is included at full text yet.")).toBeVisible();
    await lead.keyboard.press("Escape");
    // Nor does the server take one around the workbench.
    const reports = (await (
      await lead.request.get(`/api/projects/${projectId}/full-texts`, {
        headers: { "X-Kittum-Reviewer": leadId },
      })
    ).json()) as Array<{
      title: string;
      report_id: string;
      study_id: string;
      full_text: { layer_id: string } | null;
    }>;
    const trial = reports.find((r) => r.title.startsWith(TRIAL))!;
    const refused = await lead.request.post(`/api/projects/${projectId}/extraction/runs`, {
      data: {
        command_id: crypto.randomUUID(),
        actor_id: leadId,
        layer_id: trial.full_text?.layer_id,
        study_id: trial.study_id,
        report_id: trial.report_id,
        extraction_schema: await (
          await lead.request.post(`/api/projects/${projectId}/extraction/schemas/parse`, {
            data: { text: readFileSync(SCHEMA, "utf8") },
          })
        ).json(),
      },
    });
    expect(refused.status()).toBe(409);
    expect(await refused.text()).toContain("not included at full text");
  });

  await test.step("the lead opens full text, and both reviewers include the trial", async () => {
    await lead.keyboard.press("g");
    await lead.keyboard.press("o");
    await expect(lead.getByRole("heading", { name: "Progress" })).toBeVisible();
    await lead.keyboard.press("n");
    await expect(lead.getByTestId("flow-full_text-nrt-vs-control-screened")).toHaveText("1");
    for (const id of [sam, jun]) {
      const page = await (await browser.newContext()).newPage();
      await page.goto(`/p/${projectId}/screening/full-text?as=${id}`);
      await screenFullTexts(page);
      await page.context().close();
    }
    await lead.reload();
    await expect(lead.getByTestId("flow-full_text-nrt-vs-control-included")).toHaveText("1");
  });

  let runId = "";
  await test.step("the lead requests a Janus-only run with the trial's schema", async () => {
    await lead.keyboard.press("g");
    await lead.keyboard.press("x");
    await expect(lead.getByText(/^No runs yet\./)).toBeVisible();
    await lead.keyboard.press("n");
    const dialog = lead.getByRole("dialog", { name: "Request an extraction run" });
    await expect(dialog.getByLabel(/^Report/)).toContainText(TRIAL);
    await dialog.getByLabel("Schema file").setInputFiles(SCHEMA);
    await expect(dialog.getByTestId("schema-preview")).toHaveText(
      "CHW-led diabetes care in Lesotho: 2 arms, 3 endpoints (Participants, HbA1c, Deaths).",
    );
    // No model server in this stack: the Janus path is the only one on offer.
    await expect(dialog.getByText("No model server is configured")).toBeVisible();
    await dialog.getByRole("button", { name: "Request the run" }).click();
    await expect(lead.getByTestId("extraction-runs-notice")).toContainText("Requested a run");
    // The stack's worker runs it; the list looks again until it is done.
    await expect(lead.getByTestId("run-status")).toHaveText("Ready to review", { timeout: 60_000 });
    await expect(lead.getByTestId("run-progress")).toHaveText("0 of 10 accepted");
    await expect(lead.getByTestId("extraction-run")).toContainText("Janus path");
    const runs = (await (
      await lead.request.get(`/api/projects/${projectId}/extraction/runs`, {
        headers: { "X-Kittum-Reviewer": leadId },
      })
    ).json()) as Array<{
      run: { id: string; model_identity: string | null };
    }>;
    runId = runs[0]?.run.id ?? "";
    expect(runs[0]?.run.model_identity).toBeNull();
  });
  await lead.context().close();

  await test.step("the auditor, whose audit screen of the trial is still open, learns nothing of the run", async () => {
    const page = await (await browser.newContext()).newPage();
    const responses = recordResponses(page);
    await page.goto(`/p/${projectId}/extraction?as=${rita}`);
    await expect(page.getByTestId("identity")).toContainText("Rita Nunes");
    await expect(page.getByText(/^No runs yet\./)).toBeVisible();
    await expect(page.getByText("None yet.")).toBeVisible();
    await page.goto(`/p/${projectId}/extraction/${runId}?as=${rita}`);
    await expect(page.getByRole("alert")).toContainText(`extraction run ${runId}`);
    const as = { "X-Kittum-Reviewer": rita };
    for (const path of ["", "/fields", "/review"]) {
      const hidden = await page.request.get(
        `/api/projects/${projectId}/extraction/runs/${runId}${path}`,
        { headers: as },
      );
      expect(hidden.status(), path).toBe(404);
    }
    expectNothingLeaked(responses);
    // Only the answers to her own guess at its address name the run, and those say what the
    // answer for a run that never existed says.
    for (const { url, body } of responses) {
      if (!url.includes(runId)) expect(body, url).not.toContain(runId);
    }
    // Her audit screen of the trial is in her full-text queue, like any other report.
    await page.goto(`/p/${projectId}/screening/full-text?as=${rita}`);
    await expect(
      page.getByRole("complementary", { name: "Criteria" }).getByRole("heading"),
    ).toBeVisible({ timeout: 60_000 });
    await page.context().close();
  });

  const page = await (await browser.newContext()).newPage();
  const { text, at } = await fieldIndex(page, projectId, runId, sam);
  const notice = page.getByTestId("extraction-notice");
  const selected = page.locator("[data-testid=field-row][aria-selected=true]");

  await test.step("a reviewer accepts the Janus value once its source was on screen", async () => {
    await page.goto(`/p/${projectId}/extraction?as=${sam}`);
    await expect(page.getByTestId("identity")).toContainText("Sam Okafor");
    await page.keyboard.press("Enter"); // the one run
    await expect(page.getByTestId("accepted-count")).toHaveText("0 of 10 accepted");
    expect(at("participants", "intervention", "n_analysed")).toBe(0);
    await expect(selected.getByTestId("field-value")).toHaveText("52participants");
    await expect(page.getByTestId("source-highlight").first()).toBeVisible();
    await page.waitForTimeout(700);
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("accepted-count")).toHaveText("1 of 10 accepted");
  });

  await test.step("the reviewer corrects a value Janus missed with its span on the page", async () => {
    const target = at("hba1c", "intervention", "mean");
    for (let i = 1; i < target; i++) await page.keyboard.press("Tab");
    await expect(selected).toContainText("Missing");
    await page.keyboard.press("Enter");
    await expect(notice).toContainText("has no source on the page");

    // The span's code-point offsets in the layer, as the text layer's words carry them.
    const index = text.indexOf(SPAN);
    const start = [...text.slice(0, index)].length;
    const end = start + [...SPAN].length;
    const word = (attr: "s" | "e", value: number) =>
      page.locator(`[data-page='5'] .text-layer > span[data-${attr}='${value}']`);
    await page.mouse.move(1100, 500); // over the page images, the right half of the screen
    for (let i = 0; i < 60 && (await word("s", start).count()) === 0; i++) {
      await page.mouse.wheel(0, 500);
      await page.waitForTimeout(100);
    }
    const first = word("s", start);
    const last = word("e", end);
    await first.scrollIntoViewIfNeeded();
    const [a, b] = [await first.boundingBox(), await last.boundingBox()];
    if (!a || !b) throw new Error("the span's words are not on screen");
    await page.mouse.move(a.x + 1, a.y + a.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width - 1, b.y + b.height / 2, { steps: 8 });
    await page.mouse.up();

    await page.keyboard.press("e");
    const value = page.getByRole("textbox", { name: "Edited value" });
    await expect(value).toHaveValue("6.5"); // the number in the selection, to confirm
    await expect(notice).toContainText("Edited to “6.5”");
    await page.waitForTimeout(700);
    await value.press("Enter");
    await expect(page.getByTestId("accepted-count")).toHaveText("2 of 10 accepted");
    const row = page.getByTestId("field-row").nth(target);
    await expect(row.getByTestId("field-value")).toHaveText("6.5%");
    await expect(row).toContainText("Edited");
  });

  await test.step("both values are among the accepted values, the edit with its own span", async () => {
    await page.getByRole("link", { name: "Runs" }).click();
    await expect(page.getByTestId("run-progress")).toHaveText("2 of 10 accepted");
    await expect(page.getByTestId("assertion")).toHaveText([
      /^hba1cinterventionm12mean6\.5%/,
      /^participantsinterventionbaselinen_analysed52participantscritical$/,
    ]);
    const assertions = (await (
      await page.request.get(`/api/projects/${projectId}/extraction/assertions`, {
        headers: { "X-Kittum-Reviewer": sam },
      })
    ).json()) as Array<{ value: string; anchor_id: string }>;
    const edited = assertions.find((x) => x.value === "6.5");
    const anchor = (await (await page.request.get(`/api/anchors/${edited?.anchor_id}`)).json()) as {
      quote: { exact: string };
      boxes: Array<{ page: number }>;
    };
    expect(anchor.quote.exact.replace(/\s+/g, " ")).toBe(SPAN);
    expect(new Set(anchor.boxes.map((x) => x.page))).toEqual(new Set([5]));
  });
  await page.context().close();
});
