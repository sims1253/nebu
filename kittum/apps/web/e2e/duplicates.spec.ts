// Duplicates across databases, on the real stack: the methods lead runs one review's searches on
// PubMed, Europe PMC, and ClinicalTrials.gov from the workbench (the gateway answers from real
// recorded responses), builds the corpus, and sees on Studies what the searches returned, the
// duplicates Kittum proposes, and, once confirmed, one record per paper with every search that
// found it, a study with its registry entry, and why each was linked.
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { SERVER, stackEnv } from "./stack.ts";

const PROTOCOL = join(SERVER, "..", "..", "examples", "protocols", "nicotine-replacement.yaml");

// The searches tests/search/record_fixtures.py recorded (`duplicates`).
const SEARCHES = [
  {
    source: "PubMed",
    strategy:
      '"nicotine replacement"[tiab] AND "smoking cessation"[tiab] AND randomized controlled trial[pt] AND 2024/01/01:2024/03/31[dp]',
    records: 10,
  },
  {
    source: "Europe PMC",
    strategy:
      'TITLE_ABS:"nicotine replacement" AND TITLE_ABS:"smoking cessation" AND PUB_TYPE:"Randomized Controlled Trial" AND FIRST_PDATE:[2024-01-01 TO 2024-03-31]',
    records: 4,
  },
  { source: "ClinicalTrials.gov", strategy: "NCT03589989 OR NCT02564315", records: 2 },
] as const;
const ECIG = "Electronic Nicotine-Delivery Systems for Smoking Cessation.";

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

test("records found twice become one, and a registry entry joins its study", async ({ page }) => {
  const created = cli("create", `Duplicates ${new Date().toISOString()}`, "--lead", "Morgan Lee");
  const projectId = created["project_id"] ?? "";
  const leadId = created["lead"] ?? "";
  cli("publish-protocol", projectId, PROTOCOL, "--by", leadId);

  await test.step("the lead runs the three searches", async () => {
    await page.goto(`/p/${projectId}/search?as=${leadId}`);
    await expect(page.getByRole("button", { name: "Run a live search" })).toBeVisible();
    for (const search of SEARCHES) {
      await page.keyboard.press("n");
      const dialog = page.getByRole("dialog", { name: "Run a live search" });
      await dialog.getByLabel("Source").selectOption({ label: search.source });
      await dialog.getByLabel("Query").fill(search.strategy);
      await dialog.getByRole("button", { name: "Run the search" }).click();
      await expect(page.getByTestId("search-notice")).toContainText(
        `Added the ${search.source} search with ${search.records} records.`,
      );
    }
  });

  await test.step("building the corpus counts the records and proposes the duplicates", async () => {
    await page.goto(`/p/${projectId}/studies?as=${leadId}`);
    await expect(page.getByRole("button", { name: "Build the corpus" })).toBeEnabled();
    await page.keyboard.press("b");
    await expect(page.getByTestId("corpus-count")).toHaveText(
      "The searches returned 16 records. None is a duplicate yet. That leaves 16 reports in 16 studies.",
    );
    await expect(page.getByTestId("open-duplicates")).toContainText(
      "Kittum proposes 4 more duplicates.",
    );
    await expect(page.getByTestId("corpus-search")).toHaveText([
      /^PubMed.*10 records$/,
      /^Europe PMC.*4 records$/,
      /^ClinicalTrials\.gov.*2 records$/,
    ]);
    const ecig = page.getByTestId("proposal").filter({ hasText: "Same DOI 10.1056/nejmoa2308815" });
    await expect(ecig).toContainText("Duplicate records");
    await expect(ecig.getByRole("listitem")).toHaveText([/^PubMed/, /^Europe PMC/]);
    await expect(page.getByTestId("proposal").filter({ hasText: "Same NCT ID" })).toHaveCount(2);
    await shot(page, "studies-proposed");
  });

  await test.step("one key confirms every duplicate on an identifier", async () => {
    await page.keyboard.press("a");
    await expect(page.getByTestId("studies-notice")).toHaveText("Confirmed 4 duplicates.");
    await expect(page.getByTestId("corpus-count")).toHaveText(
      "The searches returned 16 records. 4 are duplicates. That leaves 12 reports in 12 studies.",
    );
    await expect(page.getByTestId("open-duplicates")).toHaveCount(0);
    // The paper is one report now, found by both searches, with the reason it was merged.
    const study = page.getByTestId("linked-study").filter({ hasText: ECIG });
    await expect(study.getByTestId("found-by")).toHaveText("Found by 2 searches");
    await expect(study.getByTestId("record-search")).toHaveText([/^PubMed/, /^Europe PMC/]);
    await expect(study.getByTestId("link-reason")).toHaveText(
      "MergedSame DOI 10.1056/nejmoa2308815. The methods lead confirmed it.",
    );
  });

  await test.step("the lead links the registry entries to their papers", async () => {
    for (let i = 0; i < 2; i++) {
      await expect(page.getByTestId("proposal").first()).toContainText("Same NCT ID");
      await page.getByTestId("proposal").first().click();
      await page.keyboard.press("Enter");
      await expect(page.getByTestId("studies-notice")).toHaveText("Confirmed the link.");
    }
    await expect(page.getByTestId("corpus-count")).toHaveText(
      "The searches returned 16 records. 4 are duplicates. That leaves 12 reports in 10 studies.",
    );
    const study = page.getByTestId("linked-study").filter({ hasText: ECIG });
    await expect(study.getByTestId("study-report")).toHaveCount(2);
    await expect(study.locator("[data-testid=study-report][data-registry]")).toContainText(
      "Registry entry",
    );
    await expect(study.getByTestId("link-reason").first()).toHaveText(
      "LinkedSame NCT ID NCT03589989. The methods lead confirmed it.",
    );
    await study.scrollIntoViewIfNeeded();
    await shot(page, "studies-linked");
    await page.getByTestId("corpus-count").scrollIntoViewIfNeeded();
    await shot(page, "studies-count");
  });
});
