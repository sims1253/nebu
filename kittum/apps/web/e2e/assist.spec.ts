// The methods lead writes the protocol in the form, on the real stack (see stack.ts) with
// GLM-5.3-Flash's recorded answers and PubMed's recorded counts.
//
// With the model: a draft from a description (the model's first two answers left a parenthesis
// open, so Kittum sent them back), every field it wrote marked AI with its name, a better PubMed
// query counted in three versions, two of its three changes taken, a step back through the history
// that changes nothing, a restore under the lead's name, and the publication, whose ledger event
// names the fields the model wrote.
//
// Without it: the lead fills in an empty form, sees what is missing at each field, saves, edits
// the PubMed query in place and counts it before saving, sees a count that could not reach PubMed
// with a retry, and edits the same protocol as YAML and back. A reviewer may do none of it.
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";

import { SERVER } from "./stack.ts";

const ANSWERS = join(SERVER, "src", "kittum", "app", "demo_data", "assist", "answers.json");
const { description } = JSON.parse(readFileSync(ANSWERS, "utf8")) as { description: string };
const MEDLINE = "search_topics[topic_nrt].source_plan.sources[medline].strategy";
// A query whose recorded count carries PubMed's notes on its words (tests/assist/support.py).
const MISTYPED =
  'by "nicotine replacment zzqx"[tiab] OR nicotinne[tiab] OR smoking cessation[tiab]';
const SHOTS = process.env["KITTUM_E2E_SHOTS"];

const shoot = async (page: Page, name: string) => {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
};

const newProject = async (request: APIRequestContext, name: string) => {
  const project = (await (
    await request.post("/api/projects", {
      data: { name: `${name} ${new Date().toISOString()}`, lead_name: "Morgan Lee" },
    })
  ).json()) as { id: string; members: Array<{ actor_id: string }> };
  return { id: project.id, lead: project.members[0]!.actor_id };
};

test("a lead drafts with the model, sees what it wrote, steps back, restores, and publishes", async ({
  page,
  request,
}) => {
  const project = await newProject(request, "Assisted");
  const { lead } = project;
  const sam = (await (
    await request.post(`/api/projects/${project.id}/members`, {
      data: { display_name: "Sam Okafor", roles: ["reviewer"], added_by: lead },
    })
  ).json()) as { actor_id: string };
  const rail = page.getByTestId("revision");
  const side = page.getByRole("complementary", { name: "Revision details" });
  const form = page.getByTestId("revision-view");
  const medline = form.locator('[data-testid="source"][data-source="medline"]');

  await test.step("the lead describes the review and the model drafts it", async () => {
    await page.goto(`/p/${project.id}/protocol?as=${lead}`);
    await page.getByRole("button", { name: /Ask glm-5\.3-flash for a draft/ }).click();
    await expect(page.getByRole("heading", { name: "Describe the review" })).toBeVisible();
    const box = page.getByLabel("Description of the review");
    await box.fill(description);
    await shoot(page, "01-describe");
    await box.press("Control+Enter");
    await expect(rail).toHaveCount(1);
    await expect(form.getByRole("textbox", { name: "Title", exact: true })).toHaveValue(
      /Nicotine replacement/,
    );
    // Two answers went back with their problems; the third compiled.
    const attempts = side.getByTestId("attempt");
    await expect(attempts).toHaveCount(3);
    await expect(attempts.nth(0)).toContainText("Sent back. 2 opening parentheses are not closed");
    await expect(attempts.nth(2)).toContainText("Compiled.");
    // The history names the model as the author, and every field it wrote says so.
    await expect(rail.first().getByTestId("author")).toContainText("glm-5.3-flash");
    await expect(medline.getByTestId("ai-written").last()).toHaveAttribute(
      "data-model",
      "glm-5.3-flash",
    );
    // The choices it made are marked too: the screening mode and each criterion's.
    await expect(form.getByTestId("mode").getByTestId("ai-mark")).toHaveAccessibleName(
      "Written by glm-5.3-flash in revision 1.",
    );
    // Its kind, its part of the question, its stages, and its text.
    await expect(form.getByTestId("criterion").first().getByTestId("ai-mark")).toHaveCount(4);
    const mark = medline.getByTestId("ai-written").last().getByTestId("ai-mark");
    await expect(mark).toHaveAccessibleName("Written by glm-5.3-flash in revision 1.");
    await mark.focus();
    await expect(medline.getByRole("tooltip")).toBeVisible();
    await shoot(page, "02-draft-ai-marks");
  });

  await test.step("the PubMed query is counted, improved, and its three versions counted", async () => {
    await medline.getByRole("button", { name: "Count hits in PubMed" }).click();
    await expect(medline.getByTestId("hit-count")).toHaveText("5,846 hits in PubMed today");
    // The registry's query is counted too, through the gateway's ClinicalTrials.gov policy.
    const registry = form.locator('[data-testid="source"][data-source="clinicaltrials_gov"]');
    await registry.getByRole("button", { name: "Count hits in ClinicalTrials.gov" }).click();
    await expect(registry.getByTestId("hit-count")).toHaveText(
      "2,310 hits in ClinicalTrials.gov today",
    );
    await medline
      .getByRole("button", { name: "Ask the model to improve the MEDLINE query" })
      .click();
    await expect(rail).toHaveCount(2);
    const suggestion = side.getByTestId("suggestion");
    await expect(suggestion.getByRole("heading", { level: 2 })).toHaveText(
      "3 suggested changes to the MEDLINE query",
    );
    await expect(suggestion.getByTestId("change")).toHaveCount(3);
    const counts = suggestion.getByTestId("counts");
    await expect(counts.getByRole("row")).toHaveText([
      /^As it is now/,
      /^With all suggestions/,
      /^With your selection/,
    ]);
    await counts.getByRole("button", { name: "Count the hits of all three versions" }).click();
    await expect(suggestion.getByTestId("count-Now")).toHaveText("5,846");
    await expect(suggestion.getByTestId("count-change")).toHaveText("6,281");
    // Leave the third change out: the count of the selection follows.
    await page.keyboard.press("3");
    await expect(suggestion.getByRole("checkbox", { name: "Change 3" })).not.toBeChecked();
    await expect(suggestion.getByTestId("strategy-markup").locator("ins")).toHaveCount(2);
    await counts.getByRole("button", { name: "Count the hits of all three versions" }).click();
    await expect(suggestion.getByTestId("count-choice")).toHaveText("5,878");
    await shoot(page, "03-suggestion-counts");
  });

  await test.step("the lead accepts two of the three changes, under their own name", async () => {
    await page.keyboard.press("a");
    await expect(rail).toHaveCount(3);
    await expect(page.getByText("Revision 3 of 3")).toBeVisible();
    await expect(rail.first().getByTestId("author")).toContainText("Morgan Lee");
    await expect(side.getByTestId("provenance")).toContainText(
      "Morgan Lee accepted 2 changes from revision 2 into revision 1.",
    );
    await expect(
      medline.getByTestId("ai-written").last().getByTestId("ai-mark"),
    ).toHaveAccessibleName(
      "Suggested by glm-5.3-flash in revision 2. Accepted by Morgan Lee in revision 3.",
    );
    await medline.getByRole("button", { name: "Count hits in PubMed" }).click();
    await expect(medline.getByTestId("hit-count")).toHaveText("5,878 hits in PubMed today");
    await expect(side.getByTestId("revision-change")).toHaveCount(1);
    await expect(side.getByTestId("revision-change")).toContainText(MEDLINE);
    await shoot(page, "04-accepted-authors");
  });

  await test.step("stepping back with , changes nothing, and the lead restores revision 1", async () => {
    await page.keyboard.press(",");
    await expect(page.getByText("Revision 2 of 3")).toBeVisible();
    await page.keyboard.press("Escape"); // leave the suggestion: back to the revision it improves
    await expect(page.getByText("Revision 1 of 3")).toBeVisible();
    const older = page.getByTestId("older-revision");
    await expect(older).toContainText("Looking does not change anything.");
    await expect(older).toContainText("Revision 3 stays the newest");
    await expect(older).toContainText("Restoring saves a copy of revision 1 as revision 4");
    await expect(medline.getByTestId("hit-count")).toHaveText("5,846 hits in PubMed today");
    await shoot(page, "05-older-revision");
    await page.keyboard.press(".");
    await page.keyboard.press(".");
    await expect(page.getByText("Revision 3 of 3")).toBeVisible();
    await expect(rail).toHaveCount(3); // looking added no revision
    await page.keyboard.press(",");
    await page.keyboard.press(",");
    await page.keyboard.press("r");
    await expect(rail).toHaveCount(4);
    await expect(page.getByText("Revision 4 of 4")).toBeVisible();
    await expect(rail.first()).toContainText("Restored 1");
    await expect(rail.first().getByTestId("author")).toContainText("Morgan Lee");
    await expect(side.getByTestId("provenance")).toContainText(
      "Morgan Lee restored revision 1. This revision is a copy of it.",
    );
    await shoot(page, "06-restored");
  });

  await test.step("the lead publishes revision 3, and the ledger names what the model wrote", async () => {
    await rail.filter({ hasText: "Changes accepted" }).click();
    await expect(page.getByText("Revision 3 of 4")).toBeVisible();
    await side.getByLabel("Reason").fill("First version, drafted with the assistant");
    await side.getByRole("button", { name: "Publish revision 3" }).click();
    await expect(page.getByTestId("protocol-notice")).toContainText(
      "from revision 3. The ledger records your approval and the",
    );
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
    await expect(page.getByRole("tab", { name: "Form" })).toHaveCount(0);
  });
});

test("a lead writes the protocol by hand in the form, counts a query, and edits it as YAML", async ({
  page,
  request,
}) => {
  const project = await newProject(request, "By hand");
  const rail = page.getByTestId("revision");
  const side = page.getByRole("complementary", { name: "Revision details" });
  const form = page.getByTestId("revision-view");
  const medline = form.locator('[data-testid="source"][data-source="medline"]');
  const query = medline.getByLabel("Query in PubMed's syntax");

  await test.step("the lead fills in an empty form and sees what is missing at each field", async () => {
    await page.goto(`/p/${project.id}/protocol?as=${project.lead}`);
    await page.getByRole("button", { name: /Fill in the form/ }).click();
    await expect(page.getByTestId("unsaved")).toHaveText("A new protocol, not saved yet");
    await form
      .getByRole("textbox", { name: "Title", exact: true })
      .fill("Nicotine replacement therapy for smoking cessation");
    const question = form.getByTestId("question");
    await question.getByLabel("Question", { exact: true }).fill("NRT versus placebo");
    await question.getByLabel("Population").fill("Adults who smoke tobacco.");
    await page.getByRole("button", { name: "Save as revision 1" }).first().click();
    // What is still empty is marked at its field and listed beside the form, in plain words.
    await expect(side.getByTestId("protocol-issue")).toHaveText([
      /^Question 1, interventionThis field is empty\.$/,
      /^Outcome 1This field is empty\.$/,
      /^Criterion C1This field is empty\.$/,
    ]);
    await expect(question.getByTestId("field-issues")).toHaveText("This field is empty.");
    await shoot(page, "07-form-problems");

    await question.getByLabel("Intervention").fill("Any form of nicotine replacement therapy.");
    await question.getByRole("button", { name: "Add a comparator" }).click();
    await question.getByRole("textbox", { name: "Comparator 1" }).fill("Placebo");
    await form.getByLabel("Primary outcome").fill("Smoking cessation at six months");
    const criterion = form.getByTestId("criterion");
    await criterion.getByLabel("Text of the criterion").fill("Adults who smoke tobacco.");
    await form.getByRole("button", { name: "Add a criterion" }).click();
    await criterion
      .nth(1)
      .getByLabel("Part of the question for criterion C2")
      .selectOption("study_type");
    await criterion
      .nth(1)
      .getByLabel("Text of the criterion")
      .fill("A randomized controlled trial.");
    await query.fill('"nicotine replacement"[tiab] AND "smoking cessation"[tiab]');
    await form.getByRole("button", { name: "Add ClinicalTrials.gov" }).click();
    await form
      .locator('[data-testid="source"][data-source="clinicaltrials_gov"]')
      .getByLabel("Query in ClinicalTrials.gov's syntax")
      .fill('"nicotine replacement" AND "smoking cessation"');
    await query.press("Control+Enter");
    await expect(rail).toHaveCount(1);
    await expect(page.getByText("Revision 1 of 1")).toBeVisible();
    await expect(rail.first()).toContainText("Written by hand");
    await expect(rail.first().getByTestId("author")).toContainText("Morgan Lee");
    await expect(form.getByTestId("ai-mark")).toHaveCount(0);
    await expect(side.getByTestId("written-count")).toHaveText("A person wrote every field.");
    await shoot(page, "08-form-saved");
  });

  await test.step("the lead edits the PubMed query in place and counts it before saving", async () => {
    // Key by key, as a person types: the first key starts the edit, and the rest must land too.
    await query.selectText();
    await page.keyboard.type(MISTYPED);
    await expect(query).toBeFocused();
    await expect(query).toHaveValue(MISTYPED);
    await expect(page.getByTestId("unsaved")).toHaveText(
      "Your changes to revision 1, not saved yet",
    );
    await medline.getByRole("button", { name: "Count hits in PubMed" }).click();
    await expect(medline.getByTestId("hit-count")).toHaveText("35,484 hits in PubMed today");
    await expect(medline.getByTestId("strategy-notes")).toContainText(
      'PubMed did not find the quoted phrase: "nicotine replacment zzqx"[tiab]',
    );
    await shoot(page, "09-hand-edited-count");
    // Nobody recorded this query, so offline PubMed cannot be reached: the form says so and offers
    // a retry, which asks again.
    await query.fill(`${MISTYPED} AND 2026[dp]`);
    await medline.getByRole("button", { name: "Count hits in PubMed" }).click();
    await expect(medline.getByTestId("count-failed")).toContainText(
      "Kittum could not reach PubMed. No recorded response for",
    );
    await shoot(page, "10-count-not-reached");
    await medline.getByRole("button", { name: "Retry" }).click();
    await expect(medline.getByTestId("count-failed")).toContainText(
      "Kittum could not reach PubMed",
    );
    await query.fill(MISTYPED);
    await page.getByRole("button", { name: "Save as revision 2" }).first().click();
    await expect(page.getByText("Revision 2 of 2")).toBeVisible();
    await expect(side.getByTestId("provenance")).toContainText(
      "Morgan Lee wrote this revision by hand, starting from revision 1.",
    );
    await expect(side.getByTestId("revision-change")).toContainText("strategy");
  });

  const editor = page.getByRole("textbox", { name: "Protocol", exact: true });
  const title = form.getByRole("textbox", { name: "Title", exact: true });
  const yamlTab = page.getByRole("tab", { name: "YAML" });
  const formTab = page.getByRole("tab", { name: "Form" });
  const unsaved = page.getByTestId("unsaved");

  await test.step("the YAML view shows the same protocol, and the form comes back as the lead left it", async () => {
    // An older revision on screen: the YAML shows it, and the form comes back to it.
    await page.keyboard.press(",");
    await expect(page.getByText("Revision 1 of 2")).toBeVisible();
    await yamlTab.click();
    await expect(editor).toHaveValue(/"nicotine replacement"\[tiab\] AND "smoking cessation"/);
    await expect(editor).not.toHaveValue(/zzqx/);
    await formTab.click();
    await expect(page.getByText("Revision 1 of 2")).toBeVisible();
    await expect(unsaved).toHaveCount(0);
    await page.keyboard.press(".");
    await expect(page.getByText("Revision 2 of 2")).toBeVisible();
    // An edit not saved yet stays through a look at the YAML.
    await title.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" in adults");
    await yamlTab.click();
    await expect(editor).toHaveValue(
      /title:\n {2}en: Nicotine replacement therapy for smoking cessation in adults\n/,
    );
    await shoot(page, "11-yaml");
    await formTab.click();
    await expect(title).toHaveValue("Nicotine replacement therapy for smoking cessation in adults");
    await expect(unsaved).toHaveText("Your changes to revision 2, not saved yet");
  });

  await test.step("YAML the form only partly understands still opens in it, and saving says what is missing", async () => {
    await yamlTab.click();
    const full = await editor.inputValue();
    // A value the form reads as text that is something else: the YAML tab stays, and says which.
    const refusal = page.getByRole("tablist", { name: "How to write" }).getByRole("alert");
    for (const [yaml, says] of [
      [
        full.replace("platform: PubMed", "platform: 123"),
        "search_topics[0].source_plan.sources[0].platform must be text. Put the value in quotes.",
      ],
      [
        full.replace(/^( *)name: MEDLINE$/m, "$1name:\n$1  en: MEDLINE"),
        "search_topics[0].source_plan.sources[0].name must be plain text, not text per language.",
      ],
    ] as const) {
      expect(yaml).not.toBe(full);
      await editor.fill(yaml);
      await formTab.click();
      await expect(refusal).toContainText(`The form cannot show this YAML. ${says}`);
      await expect(yamlTab).toHaveAttribute("aria-selected", "true");
      await expect(editor).toHaveValue(yaml);
    }
    // A mapping with a title alone: the form opens with empty lists, and saving lists the rest.
    await editor.fill("title:\n  en: Only a title\n");
    await formTab.click();
    await expect(title).toHaveValue("Only a title");
    await expect(form.getByTestId("question")).toHaveCount(0);
    await page.keyboard.press("Control+Enter");
    await expect(
      side.getByTestId("protocol-issue").filter({ hasText: "default_language" }),
    ).toContainText("Field required");
    await expect(page.getByText("Revision 3 of 3")).toHaveCount(0);
    // A valid protocol may leave out a question's comparators.
    await yamlTab.click();
    const without = full
      .replace(/^ {2}comparators:\n(?: {2}- .*\n| {4,}.*\n)+/m, "")
      .replace("smoking cessation in adults", "quitting smoking");
    expect(without).not.toMatch(/^ {2}comparators:$/m);
    await editor.fill(without);
    await formTab.click();
    await expect(title).toHaveValue("Nicotine replacement therapy for quitting smoking");
    await expect(form.getByRole("textbox", { name: "Comparator 1" })).toHaveCount(0);
    await expect(unsaved).toHaveText("Your changes to revision 2, not saved yet");
    await page.keyboard.press("Control+Enter");
    await expect(page.getByText("Revision 3 of 3")).toBeVisible();
    await expect(rail.first().getByTestId("author")).toContainText("Morgan Lee");
  });
});
