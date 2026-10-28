// Enrichment and open-access retrieval on the real stack (see stack.ts), with the source gateway
// answering from responses recorded once from OpenAlex, Europe PMC, Crossref, Unpaywall,
// PubTator, and the publishers. A review is set up through `kittum project` from the citation
// search (two CC BY trials and a registry entry) and a guideline's reference list (a trial the
// Lancet retracted); building the corpus queues its enrichment, which the stack's worker runs.
// The lead follows it per source on Studies, sees the retraction flagged, and opens records to
// see where each field came from and which drugs and diseases PubTator tagged. Both reviewers
// include everything; on Retrieval the lead uploads one trial by hand and has the worker fetch
// the rest from open access, every attempt listed under its report. The lead exports the reports
// still without a PDF, then uploads a folder: a supplement named after the retracted trial is held
// because its pages show another document, and a scan is assigned by hand. A reviewer gets none
// of it.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { expectNothingLeaked, recordResponses } from "./blind.ts";
import { includeAll } from "./screen.ts";
import { SERVER, stackEnv } from "./stack.ts";

const EXAMPLES = join(SERVER, "..", "..", "examples");
const SENTINELS = join(EXAMPLES, "sentinels", "nicotine-replacement.yaml");
const CITATIONS = join(SERVER, "src", "kittum", "app", "demo_data", "citation_search.ris");
const GUIDELINE = join(SERVER, "tests", "enrich", "fixtures", "guideline_references.ris");
const VITAMIN_C_PDF = join(
  SERVER,
  "tests",
  "documents",
  "fixtures",
  "li2026-vitamin-c-kidney-biopsy-rct.pdf",
);
const CHECKLIST = join(SERVER, "tests", "documents", "fixtures", "jmir", "app2.pdf");
// A one-page PDF without a text layer, as a scanner makes it.
const SCAN = Buffer.from(
  "%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n" +
    "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n",
);
const LESOTHO = "Community health worker-led";
const VITAMIN_C = "Effect of Vitamin C";
const RETRACTED = "Hydroxychloroquine or chloroquine";
const REGISTRY = "Community-Based Chronic Disease Care";

// Screenshots for review, when asked for: KITTUM_E2E_SHOTS=/some/dir.
const SHOTS = process.env["KITTUM_E2E_SHOTS"];
const shot = async (page: Page, name: string) => {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
};

const cli = (...args: string[]) =>
  JSON.parse(
    execFileSync("uv", ["run", "kittum", "project", ...args], {
      cwd: SERVER,
      env: stackEnv(),
      encoding: "utf8",
    }),
  ) as Record<string, string>;

test("a lead enriches the corpus from the open sources and fetches open-access full texts", async ({
  browser,
}) => {
  const created = cli("create", `Enrichment ${new Date().toISOString()}`, "--lead", "Morgan Lee");
  const projectId = created["project_id"] ?? "";
  const leadId = created["lead"] ?? "";
  const by = ["--by", leadId];
  const person = (name: string, role: string) =>
    cli("add-member", projectId, name, "--role", role, ...by)["actor_id"] ?? "";
  const sam = person("Sam Okafor", "reviewer");
  const jun = person("Jun Park", "reviewer");
  person("Rita Nunes", "auditor");
  person("Elena Weiss", "adjudicator");
  cli(
    "publish-protocol",
    projectId,
    join(EXAMPLES, "protocols", "nicotine-replacement.yaml"),
    ...by,
  );
  for (const [file, source, platform] of [
    [CITATIONS, "citation_search", "Reference lists of included studies"],
    [GUIDELINE, "guideline_references", "A guideline's reference list"],
  ] as const) {
    cli(
      "import-search",
      projectId,
      file,
      "--topic",
      "TRIALS",
      "--source",
      source,
      "--platform",
      platform,
      "--strategy",
      "Hand search",
      "--format",
      "ris",
      ...by,
    );
  }
  // Building the corpus queues its enrichment for the worker.
  const ingested = cli("ingest", projectId, ...by);
  expect(ingested["enrichment_run_id"]).toBeTruthy();

  const lead = await (await browser.newContext()).newPage();
  const row = (title: string) => lead.getByTestId("study-record").filter({ hasText: title });
  const provenance = (field: string) =>
    lead.locator(`[data-testid=provenance-row][data-field=${field}]`);

  await test.step("the lead follows the enrichment per source on Studies", async () => {
    await lead.goto(`/p/${projectId}/studies?as=${leadId}`);
    const status = lead.getByTestId("enrichment-status");
    await expect(status).toContainText("Enriched 4 records after the corpus was built", {
      timeout: 60_000,
    });
    await expect(status).toContainText("1 record is retracted.");
    // Three records have a DOI; the registry entry has nothing to look up.
    const counts = (source: string) =>
      lead.locator(`[data-testid=source-row][data-source=${source}] td`).allInnerTexts();
    for (const source of ["openalex", "europepmc", "crossref", "unpaywall", "pubtator"]) {
      expect((await counts(source)).slice(0, 4), source).toEqual(["3", "3", "0", "0"]);
    }
    // The retraction is flagged in the list; a correction and an expression of concern beside it.
    const flagged = row(RETRACTED).getByTestId("record-flag");
    await expect(flagged).toContainText("Retracted");
    await expect(flagged).toContainText("Expression of concern");
    await expect(row(LESOTHO)).toContainText("Open-access location");
    await shot(lead, "studies-enrichment");
  });

  await test.step("the retracted trial: the notice, and where each field came from", async () => {
    await row(RETRACTED).click();
    await lead.keyboard.press("o");
    const detail = lead.getByRole("complementary", { name: "Enrichment of the selected record" });
    await expect(detail.getByTestId("retraction")).toContainText(
      "Crossref links the retraction notice 10.1016/s0140-6736(20)31324-6 to this article.",
    );
    // The reference list gave the DOI; OpenAlex the PMID; Europe PMC the abstract; Crossref the
    // retraction.
    await expect(provenance("doi")).toHaveText(/^DOI.*The search/);
    await expect(provenance("pmid")).toContainText("OpenAlex");
    await expect(provenance("abstract")).toContainText("Europe PMC");
    await expect(provenance("retraction")).toContainText("Crossref");
    await expect(detail.getByTestId("enriched-abstract")).toContainText("Hydroxychloroquine");
    await expect(detail.getByText("32450107")).toBeVisible();
    await shot(lead, "studies-retracted-record");
  });

  await test.step("the Lesotho trial: entity tags and its open-access locations", async () => {
    await row(LESOTHO).click();
    const detail = lead.getByRole("complementary", { name: "Enrichment of the selected record" });
    await expect(detail.getByTestId("retraction")).toHaveCount(0);
    const metformin = detail.locator("[data-testid=entity][data-label=drug]", {
      hasText: /^metformin$/,
    });
    await expect(metformin.first()).toBeVisible();
    await expect(
      detail.locator("[data-testid=entity][data-label=disease]", { hasText: "type 2 diabetes" }),
    ).not.toHaveCount(0);
    await expect(provenance("entities")).toContainText("PubTator 3");
    await expect(
      detail.getByTestId("oa-location").filter({ hasText: "link.springer.com" }),
    ).toContainText("cc-by-4.0");
    await metformin.first().scrollIntoViewIfNeeded();
    await shot(lead, "studies-entity-tags");
    await lead.keyboard.press("Escape");
    await expect(detail).toHaveCount(0);
  });

  await test.step("both reviewers include everything at title and abstract", async () => {
    cli(
      "open-stage",
      projectId,
      "nrt-vs-control",
      "title_abstract",
      "--sentinels",
      SENTINELS,
      ...by,
    );
    const looked: string[] = [];
    for (const id of [sam, jun]) {
      const page = await (await browser.newContext()).newPage();
      await page.goto(`/p/${projectId}/screening/title-abstract?as=${id}`);
      // The registry entry's summary has no section labels; the reviewer still reads all of it.
      await includeAll(page, async (title) => {
        if (!title.startsWith(REGISTRY)) return;
        const record = page.getByRole("article", { name: "Record" });
        await expect(record).toContainText("This ComBaCaL cohort study is to assess the impact");
        await expect(record).toContainText("Trial registration: NCT05596773.");
        await expect(record.getByText("This record has no abstract")).toHaveCount(0);
        looked.push(id);
      });
      await page.context().close();
    }
    expect(looked).toEqual([sam, jun]);
  });

  const retrievalRow = (title: string) =>
    lead.getByTestId("retrieval-row").filter({ hasText: title });
  await test.step("the lead uploads one trial by hand, then fetches the rest from open access", async () => {
    await lead.goto(`/p/${projectId}/retrieval?as=${leadId}`);
    const counts = lead.getByTestId("retrieval-counts");
    await expect(counts).toHaveText("0 of 4 reports sought have a PDF");
    const titles = await lead.getByTestId("retrieval-row").locator("h2").allTextContents();
    for (let i = 0; i < titles.findIndex((t) => t.startsWith(VITAMIN_C)); i++) {
      await lead.keyboard.press("j");
    }
    await lead.keyboard.press("u");
    const dialog = lead.getByRole("dialog", { name: "Upload the full text" });
    await dialog.getByLabel("PDF file").setInputFiles(VITAMIN_C_PDF);
    await dialog.getByLabel("Licence").fill("CC BY 4.0");
    await dialog.getByRole("button", { name: "Attach the PDF" }).click();
    await expect(counts).toHaveText("1 of 4 reports sought have a PDF");

    await lead.keyboard.press("o");
    await expect(lead.getByTestId("open-access-status")).toHaveText(
      "Fetched 1 of 3 reports. The others need a PDF from you.",
      { timeout: 60_000 },
    );
    await expect(counts).toHaveText("2 of 4 reports sought have a PDF");
    // Semantic Scholar and CORE need a key, which the stack does not have.
    await expect(lead.getByTestId("skipped-sources")).toHaveText(
      "Kittum skipped Semantic Scholar because no Semantic Scholar key is set" +
        " (KITTUM_SEMANTIC_SCHOLAR_API_KEY). Kittum skipped CORE because no CORE key is set" +
        " (KITTUM_CORE_API_KEY).",
    );

    // Europe PMC's PDF link is refused; PubMed Central, asked next, names its CC BY copy, which
    // is fetched.
    const lesotho = retrievalRow(LESOTHO);
    await expect(lesotho.getByTestId("attempt")).toHaveText([
      /^1Europe PMCeuropepmc\.orgRefused, HTTP 403cc by$/,
      /^2PubMed CentralAsked pmc-oa-opendata\.s3\.amazonaws\.comNamed 1 file$/,
      /^3PubMed Centralpmc-oa-opendata\.s3\.amazonaws\.comFetchedCC BY$/,
    ]);
    await expect(lesotho.getByTestId("full-text-status")).toContainText("PDF, 13 pages");
    await expect(lesotho.getByTestId("full-text-status")).toContainText(
      "CC BY, from PubMed Central",
    );
    await expect(retrievalRow(REGISTRY).getByTestId("open-access")).toContainText(
      "No source has an open copy. Upload it with u.",
    );
    const lancet = retrievalRow(RETRACTED);
    await expect(lancet.getByTestId("open-access")).toHaveAttribute("data-outcome", "not_acquired");
    await expect(lancet.locator("[data-testid=attempt][data-kind=fetch]").first()).toContainText(
      "Refused, HTTP 403",
    );
    await expect(lancet.getByTestId("full-text-status")).toHaveText("No PDF");
    // The hand upload is untouched and stays next to it.
    await expect(retrievalRow(VITAMIN_C).getByTestId("open-access")).toHaveCount(0);
    await expect(retrievalRow(VITAMIN_C).getByTestId("full-text-status")).toContainText(
      "CC BY 4.0",
    );
    await shot(lead, "retrieval-open-access");
  });

  await test.step("the lead exports the reports without a PDF, then uploads a folder", async () => {
    await expect(lead.getByTestId("missing-count")).toContainText("2 reports sought have no PDF");
    await lead.keyboard.press("e");
    const exporting = lead.getByRole("dialog", { name: "Export the reports without a PDF" });
    const download = lead.waitForEvent("download");
    await exporting.getByRole("button", { name: "Download" }).click();
    const saved = await download;
    expect(saved.suggestedFilename()).toBe("missing-full-texts.csv");
    const csv = readFileSync((await saved.path()) ?? "", "utf8");
    expect(csv.split(/\r?\n/)[0]).toBe(
      "DOI,PMID,Title,Authors,Journal,Year,Sources tried,Why each attempt failed,Name the PDF",
    );
    expect(csv).toContain("10.1016/s0140-6736(20)31180-6,32450107,");
    expect(csv).toContain("PubMed Central has no open copy.");

    await lead.keyboard.press("b");
    const many = lead.getByRole("dialog", { name: "Upload many PDFs" });
    await many.getByLabel("PDF files").setInputFiles([
      // A JMIR trial's CONSORT checklist, named after the retracted trial.
      {
        name: "10.1016_s0140-6736(20)31180-6.pdf",
        mimeType: "application/pdf",
        buffer: readFileSync(CHECKLIST),
      },
      { name: "registry-scan.pdf", mimeType: "application/pdf", buffer: SCAN },
    ]);
    await many.getByRole("button", { name: "Upload 2 PDFs" }).click();
    await expect(lead.getByTestId("retrieval-notice")).toHaveText(
      "Attached 0 of 2 PDFs. 2 wait for you to assign.",
    );
    const held = lead.getByTestId("held-upload");
    await expect(held).toHaveCount(2);
    const checklist = held.filter({ hasText: "10.1016_s0140-6736" });
    await expect(checklist).toContainText(
      "Its file name names a report, but its first pages show neither that report's DOI nor its title.",
    );
    await shot(lead, "retrieval-held-uploads");
    await checklist.getByRole("button", { name: "Discard" }).click();
    const scan = held.filter({ hasText: "registry-scan.pdf" });
    const registry = await lead
      .getByTestId("retrieval-row")
      .filter({ hasText: REGISTRY })
      .locator("h2")
      .textContent();
    await scan.getByLabel("Report for registry-scan.pdf").selectOption({ label: registry ?? "" });
    await scan.getByRole("button", { name: "Attach" }).click();
    await expect(held).toHaveCount(0);
    await expect(lead.getByTestId("retrieval-counts")).toHaveText(
      "3 of 4 reports sought have a PDF",
    );
    await expect(lead.getByTestId("missing-count")).toContainText("1 report sought has no PDF");
  });

  await test.step("a reviewer learns none of it", async () => {
    const own = await (await browser.newContext()).newPage();
    const responses = recordResponses(own);
    for (const screen of ["studies", "retrieval"]) {
      await own.goto(`/p/${projectId}/${screen}?as=${sam}`);
      await expect(own.getByTestId("lead-only")).toBeVisible();
    }
    await expect(own.getByTestId("enrichment")).toHaveCount(0);
    const as = { "X-Kittum-Reviewer": sam };
    const base = `/api/projects/${projectId}`;
    const occurrence = (
      (await (
        await lead.request.get(`${base}/enrichment/records`, {
          headers: { "X-Kittum-Reviewer": leadId },
        })
      ).json()) as Array<{ occurrence_id: string }>
    )[0]?.occurrence_id;
    for (const path of [
      "enrichment/runs/latest",
      "enrichment/records",
      `enrichment/records/${occurrence}`,
      "retrieval/open-access/latest",
      "retrieval/retry",
      "retrieval/missing",
      "retrieval/missing/export",
      "retrieval/uploads",
    ]) {
      const refused = await own.request.get(`${base}/${path}`, { headers: as });
      expect(refused.status(), path).toBe(403);
    }
    for (const path of ["enrichment/runs", "retrieval/open-access", "retrieval/retry"]) {
      const refused = await own.request.post(`${base}/${path}`, { data: { actor_id: sam } });
      expect(refused.status(), path).toBe(403);
    }
    expect(expectNothingLeaked(responses)).toBeGreaterThan(3);
    await own.context().close();
  });
  await lead.context().close();
});
