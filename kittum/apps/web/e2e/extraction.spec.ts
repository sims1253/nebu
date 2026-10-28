// Extraction from the browser on a real trial PDF, on the real stack (see stack.ts). A small review
// is set up through `kittum project` (the browser path is setup.spec.ts); the methods lead uploads
// the Lesotho trial's PDF on Retrieval and requests a run with its extraction schema, the Janus
// path only, so no model is needed; the stack's worker runs it. A reviewer accepts a value Janus
// read from Table 1 once its source was on screen, corrects one Janus missed by selecting it on the
// page, and finds both among the accepted values the reports use.
import { execFileSync } from "node:child_process";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { SERVER, stackEnv } from "./stack.ts";

const EXAMPLES = join(SERVER, "..", "..", "examples");
const PDF = join(SERVER, "tests", "documents", "fixtures", "gerber2026-lesotho-t2d-crct.pdf");
const SCHEMA = join(SERVER, "tests", "extraction", "fixtures", "lesotho_schema.yaml");
const TRIAL = "Community health worker-led";
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

const fieldIndex = async (page: Page, projectId: string, runId: string) => {
  const review = (await (
    await page.request.get(`/api/projects/${projectId}/extraction/runs/${runId}/review`)
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

test("a lead requests a Janus-only run on a real PDF; a reviewer accepts and corrects values", async ({
  browser,
}) => {
  const created = cli("create", `Extraction ${new Date().toISOString()}`, "--lead", "Morgan Lee");
  const projectId = created["project_id"] ?? "";
  const by = ["--by", created["lead"] ?? ""];
  const sam = cli("add-member", projectId, "Sam Okafor", "--role", "reviewer", ...by)["actor_id"];
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

  const lead = await (await browser.newContext()).newPage();
  await test.step("the lead uploads the trial's PDF on Retrieval", async () => {
    await lead.goto(`/p/${projectId}/retrieval?as=${created["lead"]}`);
    await expect(lead.getByTestId("retrieval-counts")).toHaveText("0 of 3 reports retrieved");
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
    await expect(lead.getByTestId("retrieval-counts")).toHaveText("1 of 3 reports retrieved");
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
      await lead.request.get(`/api/projects/${projectId}/extraction/runs`)
    ).json()) as Array<{
      run: { id: string; model_identity: string | null };
    }>;
    runId = runs[0]?.run.id ?? "";
    expect(runs[0]?.run.model_identity).toBeNull();
  });
  await lead.context().close();

  const page = await (await browser.newContext()).newPage();
  const { text, at } = await fieldIndex(page, projectId, runId);
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
      await page.request.get(`/api/projects/${projectId}/extraction/assertions`)
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
