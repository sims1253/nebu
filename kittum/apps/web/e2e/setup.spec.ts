// A review set up from nothing in the browser by its methods lead, on the real stack (see
// stack.ts): the review and its people, a protocol from the shipped example (a slip shown beside
// the text, then two published versions with the change between them), three searches imported
// from the demo's export files, the corpus, and title and abstract screening with the example
// sentinels. The same steps then run through `kittum project`, and the two reviews must open the
// same stage and fill the same queues; the first reviewer takes over in the browser.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";

import { expectNothingLeaked, recordResponses } from "./blind.ts";
import { SERVER, stackEnv } from "./stack.ts";

const DEMO_DATA = join(SERVER, "src", "kittum", "app", "demo_data");
const EXAMPLES = join(SERVER, "..", "..", "examples");
const PROTOCOL = join(EXAMPLES, "protocols", "nicotine-replacement.yaml");
const SENTINELS = join(EXAMPLES, "sentinels", "nicotine-replacement.yaml");

const PEOPLE = [
  ["Sam Okafor", "Reviewer"],
  ["Jun Park", "Reviewer"],
  ["Rita Nunes", "Auditor"],
  ["Elena Weiss", "Adjudicator"],
] as const;

const PUBMED =
  '("nicotine replacement"[tiab] OR "nicotine patch"[tiab] OR "nicotine gum"[tiab])' +
  ' AND "smoking cessation"[tiab] AND randomized controlled trial[pt]' +
  " AND 2024/01/01:2024/06/30[dp]";

// The demo's searches, as export files: what the review's PRISMA-S table will list.
const SEARCHES = [
  {
    file: "pubmed_efetch.xml",
    format: "pubmed-xml",
    source: "pubmed",
    platform: "PubMed",
    strategy: PUBMED,
    records: 20,
  },
  {
    file: "central_export.ris",
    format: "central",
    source: "central",
    platform: "Cochrane Library (Wiley)",
    strategy: "#1 (nicotine NEAR/3 (replacement OR patch* OR gum OR lozenge*)):ti,ab,kw",
    records: 7,
  },
  {
    file: "citation_search.ris",
    format: "ris",
    source: "citation_search",
    platform: "Reference lists of included studies",
    strategy: "Backward citation search of the included trials' reference lists",
    records: 3,
  },
] as const;

const cli = (...args: string[]) =>
  JSON.parse(
    execFileSync("uv", ["run", "kittum", "project", ...args], {
      cwd: SERVER,
      env: stackEnv(),
      encoding: "utf8",
    }),
  ) as Record<string, unknown>;

interface Stage {
  stage_id: string;
  scope_id: string;
  stage: string;
  topic_id: string;
  mode: string;
  reveal: string;
  protocol_hash: string;
  judge_config_id: string;
  screeners: string[];
  auditor_id: string;
  adjudicators: string[];
}
interface Project {
  id: string;
  members: Array<{ actor_id: string; display_name: string; roles: string[] }>;
  stages: Stage[];
}

const project = async (request: APIRequestContext, id: string) =>
  (await (await request.get(`/api/projects/${id}`)).json()) as Project;

/** The stage with people named instead of their IDs, which differ between the two reviews. */
const named = (p: Project) => {
  const name = (id: string) => p.members.find((m) => m.actor_id === id)?.display_name ?? id;
  return p.stages.map((s) => ({
    scope: s.scope_id,
    stage: s.stage,
    topic: s.topic_id,
    mode: s.mode,
    reveal: s.reveal,
    protocol: s.protocol_hash,
    judge: s.judge_config_id,
    screeners: s.screeners.map(name),
    auditor: name(s.auditor_id),
    adjudicators: s.adjudicators.map(name),
  }));
};

/** A person's whole queue once the AI has read every record: the titles, in no fixed order. */
const fullQueue = async (request: APIRequestContext, projectId: string, actorId: string) => {
  const headers = { "X-Kittum-Reviewer": actorId };
  const params = { scope_id: "nrt-vs-control", stage: "title_abstract" };
  const status = await (
    await request.get(`/api/projects/${projectId}/screening/status`, { params, headers })
  ).json();
  let titles: string[] = [];
  await expect
    .poll(
      async () => {
        const items = (await (
          await request.get(`/api/projects/${projectId}/screening/queue`, {
            params: { ...params, limit: 50 },
            headers,
          })
        ).json()) as Array<{ title: string }>;
        titles = items.map((i) => i.title).sort();
        return titles.length;
      },
      { timeout: 120_000, message: "the AI judge reads every record" },
    )
    .toBe((status as { remaining: number }).remaining);
  return titles;
};

const step = (page: Page, title: string) =>
  page.getByTestId("setup-row").filter({ hasText: title });

test("a lead sets up a review from nothing in the browser, as the CLI would", async ({
  browser,
}) => {
  const page = await (await browser.newContext()).newPage();
  const name = `First run ${new Date().toISOString()}`;
  const versions = mkdtempSync(join(tmpdir(), "kittum-e2e-protocol-"));

  await test.step("the lead creates the review and becomes its methods lead", async () => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await page.keyboard.press("c");
    const dialog = page.getByRole("dialog", { name: "Create a review" });
    await dialog.getByLabel("Review name").fill(name);
    await dialog.getByLabel("Your name").fill("Morgan Lee");
    await dialog.getByRole("button", { name: "Create the review" }).click();
    await expect(page).toHaveURL(/\/p\/[^/]+\/setup$/);
    await expect(page.getByTestId("identity")).toContainText("Morgan Lee");
    await expect(page.getByTestId("identity")).toContainText("Methods lead");
    // Nothing is done but the review itself; people come first.
    await expect(page.getByTestId("setup-row").first()).toHaveAttribute("data-state", "next");
    await expect(step(page, "Protocol")).toHaveAttribute("data-state", "later");
  });

  await test.step("the lead adds the reviewers, the auditor, and the adjudicator", async () => {
    const form = page.getByRole("form", { name: "Add a person" });
    await page.keyboard.press("a"); // to the name field, where focus stays for the next person
    for (const [person, role] of PEOPLE) {
      await expect(form.getByLabel("Name")).toBeFocused();
      await page.keyboard.type(person);
      for (const r of ["Reviewer", "Auditor", "Adjudicator"]) {
        const box = form.getByRole("checkbox", { name: r });
        if ((await box.getAttribute("aria-checked")) !== String(r === role)) await box.click();
      }
      await form.getByRole("button", { name: "Add" }).click();
      await expect(page.getByTestId("setup-notice")).toHaveText(`Added ${person}.`);
    }
    await expect(page.getByRole("table", { name: "People on this review" })).toContainText(
      "Elena WeissAdjudicator",
    );
    await expect(step(page, "People")).toHaveAttribute("data-state", "done");
    await expect(step(page, "Protocol")).toHaveAttribute("data-state", "next");
  });

  await test.step("the protocol: a slip is shown beside the text, then version one is published", async () => {
    await page.keyboard.press("Escape"); // out of the name field
    await page.keyboard.press("j"); // the protocol, next on the spine
    await expect(page.getByTestId("setup-step")).toContainText(
      "Write it, check it, and publish it",
    );
    await page.keyboard.press("Enter"); // its screen: the protocol editor
    await expect(page.getByRole("heading", { name: "Write the protocol" })).toBeVisible();
    await page.getByLabel("Start from an example").selectOption("nicotine-replacement");
    await page.getByRole("button", { name: "Load" }).click();
    const editor = page.getByRole("textbox", { name: "Protocol", exact: true });
    await expect(editor).toHaveValue(readFileSync(PROTOCOL, "utf8"));

    await editor.fill(readFileSync(PROTOCOL, "utf8").replace("mode: M1", "mode: M7"));
    await editor.press("Control+Enter");
    const issue = page.getByTestId("protocol-issue");
    await expect(issue).toHaveCount(1);
    await expect(issue).toContainText("search_topics[0].mode");
    await expect(issue).toContainText("Input should be 'M1', 'M2' or 'M3', got 'M7'");
    await expect(page.getByRole("button", { name: "Publish this version" })).toHaveCount(0);

    await editor.fill(readFileSync(PROTOCOL, "utf8"));
    await editor.press("Control+Enter");
    await expect(page.getByTestId("protocol-check")).toContainText("It compiles");
    await expect(page.getByTestId("protocol-check")).toContainText("The first version");
    await page.getByLabel("Reason").fill("The protocol as approved by the steering group.");
    await page.getByRole("button", { name: "Publish this version" }).click();
    await expect(page.getByTestId("protocol-notice")).toHaveText(
      "Published version 0.3. Your approval is in the ledger.",
    );
    await expect(
      page.getByRole("heading", { name: "Nicotine replacement therapy for smoking cessation" }),
    ).toBeVisible();
  });

  await test.step("version two: the editor starts from version one and shows what changes", async () => {
    await page.keyboard.press("v");
    await expect(page.getByRole("heading", { name: "A new version after 0.3" })).toBeVisible();
    const editor = page.getByRole("textbox", { name: "Protocol", exact: true });
    await expect(editor).toHaveValue(/version_label: '0.3'/);
    // An unedited version changes nothing and cannot be published.
    await editor.press("Control+Enter");
    await expect(page.getByTestId("protocol-diff")).toContainText("Nothing changes");
    await expect(page.getByRole("button", { name: "Publish this version" })).toHaveCount(0);

    const second = (await editor.inputValue())
      .replace("version_label: '0.3'", "version_label: '0.4'")
      .replace(
        "Adults (18 years or older) who smoke tobacco.",
        "Adults (18 years or older) who smoke tobacco daily or occasionally.",
      );
    writeFileSync(join(versions, "second.yaml"), second);
    await editor.fill(second);
    await editor.press("Control+Enter");
    const diff = page.getByTestId("protocol-diff");
    await expect(page.getByTestId("protocol-check")).toContainText("Against version 0.3");
    await expect(diff).toContainText("Criteria changed: POP");
    await expect(page.getByTestId("protocol-change")).toHaveText([
      /^version_label0\.30\.4$/,
      /^search_topics\[TRIALS\]\.criteria\[POP\]\.text\.enAdults \(18 years or older\) who smoke tobacco\.Adults \(18 years or older\) who smoke tobacco daily or occasionally\.$/,
    ]);
    await page.getByLabel("Reason").fill("Occasional smokers are eligible too.");
    await page.getByRole("button", { name: "Publish this version" }).click();
    await expect(page.getByTestId("protocol-notice")).toContainText("Published version 0.4");
  });

  await test.step("the lead imports the three searches from their export files", async () => {
    await page.keyboard.press("g");
    await page.keyboard.press("s");
    await expect(page.getByRole("heading", { name: "Searches" })).toBeVisible();
    for (const search of SEARCHES) {
      await page.keyboard.press("i");
      const dialog = page.getByRole("dialog", { name: "Import a search file" });
      await dialog.getByLabel("File format").selectOption(search.format);
      await dialog.getByLabel("Source").fill(search.source);
      await dialog.getByLabel("Platform").fill(search.platform);
      await dialog.getByLabel("Strategy").fill(search.strategy);
      await dialog.getByLabel("Search file").setInputFiles(join(DEMO_DATA, search.file));
      await dialog.getByRole("button", { name: "Import the search" }).click();
      await expect(page.getByTestId("search-notice")).toContainText(
        `Added the ${search.platform} search: ${search.records} records.`,
      );
    }
    await expect(page.getByTestId("search-counts")).toHaveText(
      SEARCHES.map((s) => `${s.records} reported, ${s.records} retrieved`),
    );
  });

  await test.step("the lead builds the corpus", async () => {
    await page.keyboard.press("g");
    await page.keyboard.press("d");
    await expect(page.getByText("The corpus is not built yet.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Build the corpus" })).toBeEnabled();
    await page.keyboard.press("b");
    await expect(page.getByText(/^30 reports in 30 studies/)).toBeVisible();
    // The PubMed and CENTRAL records of one trial share a DOI: proposed, left for the lead.
    await expect(page.getByTestId("proposal").first()).toContainText(
      "The same record, found twice",
    );
  });

  let projectId = "";
  await test.step("the lead opens title and abstract screening with the example sentinels", async () => {
    await page.keyboard.press("g");
    await page.keyboard.press("u");
    projectId = /\/p\/([^/]+)\//.exec(page.url())?.[1] ?? "";
    await expect(step(page, "Corpus")).toHaveAttribute("data-state", "done");
    await expect(step(page, "Corpus")).toContainText(
      "30 records identified, 0 duplicates removed so far",
    );
    const form = page.getByRole("form", { name: "Open a screening stage" });
    await expect(form.getByLabel("Question and stage")).toHaveValue(
      "nrt-vs-control|title_abstract",
    );
    await expect(form.getByTestId("stage-people")).toHaveText(
      "Sam Okafor and Jun Park screen; Rita Nunes audits; Elena Weiss adjudicates.",
    );
    await form.getByLabel("Sentinel file").setInputFiles(SENTINELS);
    await expect(form.getByTestId("sentinel-preview")).toContainText(
      "2 sentinels, expected to be 2 included and 0 excluded",
    );
    await form.getByRole("button", { name: "Open title and abstract screening" }).click();
    await expect(page.getByTestId("setup-notice")).toContainText(
      "Title and abstract screening is open for",
    );
    await expect(step(page, "Title and abstract screening")).toHaveAttribute("data-state", "done");
  });

  // The same review through `kittum project`, from the same files.
  const lead = cli("create", `${name} (CLI)`, "--lead", "Morgan Lee") as {
    project_id: string;
    lead: string;
  };
  const by = ["--by", lead.lead];
  for (const [person, role] of PEOPLE) {
    cli("add-member", lead.project_id, person, "--role", role.toLowerCase(), ...by);
  }
  cli("publish-protocol", lead.project_id, PROTOCOL, ...by);
  cli(
    "publish-protocol",
    lead.project_id,
    join(versions, "second.yaml"),
    "--reason",
    "Occasional smokers",
    ...by,
  );
  for (const s of SEARCHES) {
    cli(
      "import-search",
      lead.project_id,
      join(DEMO_DATA, s.file),
      "--topic",
      "TRIALS",
      "--source",
      s.source,
      "--platform",
      s.platform,
      "--strategy",
      s.strategy,
      "--format",
      s.format,
      ...by,
    );
  }
  cli("ingest", lead.project_id, ...by);
  cli(
    "open-stage",
    lead.project_id,
    "nrt-vs-control",
    "title_abstract",
    "--sentinels",
    SENTINELS,
    ...by,
  );

  await test.step("the stage and every queue match what the CLI opened", async () => {
    const ui = await project(page.request, projectId);
    const viaCli = await project(page.request, lead.project_id);
    expect(named(ui)).toEqual(named(viaCli));
    expect(named(ui)[0]).toEqual(
      expect.objectContaining({
        mode: "M1",
        reveal: "on_disagreement",
        screeners: ["Sam Okafor", "Jun Park"],
      }),
    );
    const id = (p: Project, person: string) =>
      p.members.find((m) => m.display_name === person)?.actor_id ?? "";
    for (const person of ["Sam Okafor", "Jun Park"]) {
      const mine = await fullQueue(page.request, ui.id, id(ui, person));
      expect(mine, person).toEqual(await fullQueue(page.request, viaCli.id, id(viaCli, person)));
      // Every report, and the sentinel the protocol's seed draws (checked alike below).
      expect(mine.filter((t) => /^Nicotine (lozenge|mouth spray)/.test(t))).toHaveLength(1);
      expect(mine).toHaveLength(31);
    }
    // The auditor's sample is drawn over the records' own IDs, which differ between the two
    // reviews; its size does not.
    expect((await fullQueue(page.request, ui.id, id(ui, "Rita Nunes"))).length).toBe(
      (await fullQueue(page.request, viaCli.id, id(viaCli, "Rita Nunes"))).length,
    );
  });

  await test.step("the first reviewer picks themselves and starts screening", async () => {
    await page.getByTestId("identity").click();
    await expect(page.getByRole("heading", { name: "Who is screening?" })).toBeVisible();
    await page.keyboard.press("2"); // the lead is first, Sam Okafor second
    await expect(page.getByTestId("identity")).toContainText("Sam Okafor");
    await page.keyboard.press("g");
    await page.keyboard.press("t");
    await expect(page.getByTestId("record-title")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText("0 screened, 31 left")).toBeVisible();
  });

  await test.step("a reviewer who has decided nothing receives none of the setup's data", async () => {
    const ui = await project(page.request, projectId);
    const sam = ui.members.find((m) => m.display_name === "Sam Okafor")?.actor_id ?? "";
    const own = await (await browser.newContext()).newPage();
    const responses = recordResponses(own);
    // Every screen of the review, as Sam sees it, before he has decided a single record.
    for (const screen of ["setup", "protocol", "search", "studies", "retrieval", "progress"]) {
      await own.goto(`/p/${projectId}/${screen}?as=${sam}`);
      await expect(own.getByTestId("identity")).toContainText("Sam Okafor");
      await expect(own.locator("main")).not.toBeEmpty();
    }
    await own.goto(`/p/${projectId}/extraction?as=${sam}`);
    await expect(own.getByRole("heading", { level: 1 })).toBeVisible();
    await own.goto(`/p/${projectId}/screening/title-abstract?as=${sam}`);
    await expect(own.getByTestId("record-title")).toBeVisible();
    await expect(own.getByTestId("lead-only")).toHaveCount(0);
    for (const screen of ["search", "studies", "retrieval"]) {
      await own.goto(`/p/${projectId}/${screen}?as=${sam}`);
      await expect(own.getByTestId("lead-only")).toBeVisible();
    }

    const as = { "X-Kittum-Reviewer": sam };
    for (const path of [
      "corpus/revisions/latest",
      "corpus/proposals",
      "search/executions",
      "full-texts",
    ]) {
      const refused = await own.request.get(`/api/projects/${projectId}/${path}`, { headers: as });
      expect(refused.status(), path).toBe(403);
    }
    const progress = (await (
      await own.request.get(`/api/projects/${projectId}/progress`, { headers: as })
    ).json()) as { identification: unknown; searches: number; full_texts: number };
    expect([progress.identification, progress.searches, progress.full_texts]).toEqual([null, 0, 0]);
    expect(expectNothingLeaked(responses)).toBeGreaterThan(10);

    // The sentinel in Sam's queue is alike in every key to the records beside it.
    const items = (await (
      await own.request.get(`/api/projects/${projectId}/screening/queue`, {
        params: { scope_id: "nrt-vs-control", stage: "title_abstract", limit: 50 },
        headers: as,
      })
    ).json()) as Array<{ title: string; record: Record<string, unknown> }>;
    const sentinel = items.find((i) => /^Nicotine (lozenge|mouth spray)/.test(i.title));
    expect(sentinel).toBeDefined();
    const keys = (item: { record: Record<string, unknown> }) => [
      Object.keys(item).sort(),
      Object.keys(item.record).sort(),
    ];
    for (const item of items) expect(keys(item), item.title).toEqual(keys(sentinel!));
    await own.context().close();
  });
  await page.context().close();
});
