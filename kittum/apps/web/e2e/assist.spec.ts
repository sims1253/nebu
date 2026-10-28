// The methods lead writes the protocol with the assistant, on the real stack (see stack.ts) with
// GLM-5.3-Flash's recorded answers and PubMed's recorded counts: a draft from a description (the
// model's first two answers left a parenthesis open, so Kittum sent them back), a better PubMed
// strategy counted before and after, two of its three changes taken, a step back through the
// revisions and forward again, and the publication, whose ledger event names the field the model
// wrote. A reviewer may do none of it.
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { SERVER } from "./stack.ts";

const ANSWERS = join(SERVER, "src", "kittum", "app", "demo_data", "assist", "answers.json");
const { description } = JSON.parse(readFileSync(ANSWERS, "utf8")) as { description: string };
const MEDLINE = "search_topics[topic_nrt].source_plan.sources[medline].strategy";
const SHOTS = process.env["KITTUM_E2E_SHOTS"];

const shoot = async (page: Page, name: string) => {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
};

test("a lead drafts, improves, counts, steps back, and publishes a protocol with the assistant", async ({
  page,
  request,
}) => {
  const project = (await (
    await request.post("/api/projects", {
      data: { name: `Assisted ${new Date().toISOString()}`, lead_name: "Morgan Lee" },
    })
  ).json()) as { id: string; members: Array<{ actor_id: string }> };
  const lead = project.members[0]!.actor_id;
  const sam = (await (
    await request.post(`/api/projects/${project.id}/members`, {
      data: { display_name: "Sam Okafor", roles: ["reviewer"], added_by: lead },
    })
  ).json()) as { actor_id: string };
  const rail = page.getByTestId("revision");
  const side = page.getByRole("complementary", { name: "Revision details" });
  const view = page.getByTestId("revision-view");
  const medline = view.locator('[data-testid="source"][data-source="medline"]');

  await test.step("the lead describes the review and the model drafts it", async () => {
    await page.goto(`/p/${project.id}/protocol?as=${lead}`);
    await page.getByRole("tab", { name: "With the assistant" }).click();
    await expect(page.getByRole("heading", { name: "Describe the review" })).toBeVisible();
    const box = page.getByLabel("Description of the review");
    await box.fill(description);
    await shoot(page, "01-describe");
    await box.press("Control+Enter");
    await expect(rail).toHaveCount(1);
    await expect(view.getByRole("heading", { level: 1 })).toContainText("Nicotine replacement");
    // Two answers went back with their problems; the third compiled.
    const attempts = side.getByTestId("attempt");
    await expect(attempts).toHaveCount(3);
    await expect(attempts.nth(0)).toContainText("sent back, 2 opening parentheses are not closed");
    await expect(attempts.nth(2)).toContainText("compiled");
    await expect(medline.getByTestId("ai-written")).toHaveAttribute("data-model", "glm-5.3-flash");
  });

  await test.step("the PubMed strategy is counted, improved, and counted again", async () => {
    await medline.getByRole("button", { name: "Count" }).click();
    await expect(medline.getByTestId("hit-count")).toHaveText("5,846 in PubMed today");
    await shoot(page, "02-draft");
    await medline.getByRole("button", { name: "Improve MEDLINE strategy" }).click();
    await expect(rail).toHaveCount(2);
    const suggestion = side.getByTestId("suggestion");
    await expect(suggestion.getByRole("heading", { level: 2 })).toHaveText(
      "3 suggested changes to the MEDLINE strategy",
    );
    await expect(suggestion.getByTestId("change")).toHaveCount(3);
    await suggestion.getByRole("button", { name: "Count all three" }).click();
    await expect(suggestion.getByTestId("count-Now")).toHaveText("5,846");
    await expect(suggestion.getByTestId("count-change")).toHaveText("6,281");
    // Leave the third change out: the count of the choice follows.
    await page.keyboard.press("3");
    await expect(suggestion.getByRole("checkbox", { name: "Change 3" })).not.toBeChecked();
    await expect(suggestion.getByTestId("strategy-markup").locator("ins")).toHaveCount(2);
    await suggestion.getByRole("button", { name: "Count all three" }).click();
    await expect(suggestion.getByTestId("count-choice")).toHaveText("5,878");
    await shoot(page, "03-suggestion");
  });

  await test.step("the lead accepts two of the three changes", async () => {
    await page.keyboard.press("a");
    await expect(rail).toHaveCount(3);
    await expect(page.getByText("Revision 3 of 3")).toBeVisible();
    await expect(side.getByTestId("provenance")).toContainText(
      "You accepted 2 changes of revision 2 onto revision 1.",
    );
    await expect(medline.getByTestId("ai-written")).toContainText(
      "Suggested by glm-5.3-flash, accepted by you",
    );
    await medline.getByRole("button", { name: "Count" }).click();
    await expect(medline.getByTestId("hit-count")).toHaveText("5,878 in PubMed today");
    await expect(side.getByTestId("revision-change")).toHaveCount(1);
    await expect(side.getByTestId("revision-change")).toContainText(MEDLINE);
    await shoot(page, "04-accepted");
  });

  await test.step("the lead steps back through the revisions and forward again", async () => {
    await page.keyboard.press("[");
    await expect(page.getByText("Revision 2 of 3")).toBeVisible();
    await page.keyboard.press("Escape"); // leave the suggestion: back to the revision it improves
    await expect(page.getByText("Revision 1 of 3")).toBeVisible();
    await expect(medline.getByTestId("hit-count")).toHaveText("5,846 in PubMed today");
    await expect(side.getByRole("button", { name: "Restore revision 1" })).toBeVisible();
    await shoot(page, "05-back");
    await page.keyboard.press("]");
    await page.keyboard.press("]");
    await expect(page.getByText("Revision 3 of 3")).toBeVisible();
    await expect(rail).toHaveCount(3); // stepping restores nothing
  });

  await test.step("the lead publishes revision 3, and the ledger names what the model wrote", async () => {
    await side.getByLabel("Reason").fill("First version, drafted with the assistant");
    await side.getByRole("button", { name: "Publish revision 3" }).click();
    await expect(page.getByTestId("protocol-notice")).toContainText(
      "from revision 3. Your approval is in the ledger, with the",
    );
    await shoot(page, "06-published");
    const events = (await (
      await request.get(`/api/projects/${project.id}/audit/events`, {
        params: { type: "protocol.published", limit: 10 },
        headers: { "X-Kittum-Reviewer": lead },
      })
    ).json()) as { events: Array<{ payload: { ai_parts: Array<Record<string, string>> } }> };
    const parts = events.events[0]!.payload.ai_parts;
    const strategy = parts.find((p) => p["path"] === MEDLINE)!;
    expect(strategy["recipe_id"]).toBe("glm-5.3-flash");
    expect(strategy["accepted_in"]).not.toBe(strategy["revision_id"]);
    expect(parts.length).toBeGreaterThan(20); // and every field of the draft it left alone
  });

  await test.step("a reviewer may not use the assistant", async () => {
    const headers = { "X-Kittum-Reviewer": sam.actor_id };
    for (const [path, data] of [
      ["draft", { description }],
      ["count", { dialect: "pubmed", strategy: "nicotine" }],
    ] as const) {
      const refused = await request.post(`/api/projects/${project.id}/assist/${path}`, {
        data,
        headers,
      });
      expect(refused.status()).toBe(403);
    }
    expect(
      (await request.get(`/api/projects/${project.id}/assist/revisions`, { headers })).status(),
    ).toBe(403);
    await page.goto(`/p/${project.id}/protocol?as=${sam.actor_id}`);
    await expect(page.getByRole("tab", { name: "With the assistant" })).toHaveCount(0);
  });
});
