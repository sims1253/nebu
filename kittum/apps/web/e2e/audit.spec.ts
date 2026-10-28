// The audit trail in the browser, on the real stack (see stack.ts). A small review is set up
// through `kittum project` and screened through the API: the citation search and a hand search
// find the Lesotho trial twice, the lead confirms the duplicate, the screeners disagree on it at
// title and abstract and the adjudicator includes it, the lead uploads its PDF, and at full text
// reviewer A excludes it blind, is shown the AI's reading, and revises to include.
//
// Then the methods lead, on the Audit screen, opens the "Reports included" count, traces the
// trial from its two searches to A's revised decision, looks at the review as it stood when title
// and abstract closed and keeps that report, signs a checkpoint, and verifies the chain. Someone
// with the database superuser's rights edits B's blind decision in place; verifying again names
// that event and the payload hash check, on the integrity view and on the trial's timeline. A
// screener sees no Audit screen and is refused by every audit route.
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";

import { SERVER, stackEnv } from "./stack.ts";
import { forgePayload } from "./tamper.ts";

const EXAMPLES = join(SERVER, "..", "..", "examples");
const DEMO_DATA = join(SERVER, "src", "kittum", "app", "demo_data");
const FIXTURES = join(SERVER, "tests", "documents", "fixtures");
const TRIAL = "Community health worker-led";
const SCOPE = "nrt-vs-control";
// Screenshots for the record, when KITTUM_AUDIT_SHOTS names a directory.
const SHOTS = process.env["KITTUM_AUDIT_SHOTS"];

const cli = (...args: string[]) =>
  JSON.parse(
    execFileSync("uv", ["run", "kittum", "project", ...args], {
      cwd: SERVER,
      env: stackEnv(),
      encoding: "utf8",
    }),
  ) as Record<string, string>;

const shot = async (page: Page, name: string) => {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
};

interface Item {
  readonly assignment_id: string;
  readonly etag: string;
  readonly title: string;
}
type Decision = Record<string, unknown>;

const include = (): Decision => ({
  outcome: "include",
  reason_criterion_id: null,
  evidence_anchor_ids: [],
  comment: null,
  criteria: [],
});

/** The review's API as one person: the screening steps the journey does not click through. */
const as = (request: APIRequestContext, projectId: string, actorId: string) => {
  const headers = { "X-Kittum-Reviewer": actorId };
  const base = `/api/projects/${projectId}`;
  const ok = async (response: Awaited<ReturnType<APIRequestContext["get"]>>) => {
    expect(response.ok(), await response.text()).toBe(true);
    return response.json() as Promise<unknown>;
  };
  const get = (path: string) => request.get(`${base}/${path}`, { headers }).then(ok);
  const post = (path: string, data: unknown) =>
    request.post(`${base}/${path}`, { headers, data }).then(ok);

  /** Full text: every criterion met on the first protocol-term mention, adopted as evidence. */
  const fullTextInclude = async (item: Item): Promise<Decision> => {
    const source = (await get(`screening/assignments/${item.assignment_id}/full-text`)) as {
      layer_id: string;
      mentions: Array<{
        criterion_id: string;
        mentions: Array<{ quote: { exact: string }; start: number }>;
      }>;
    };
    const mention = source.mentions.flatMap((c) => c.mentions)[0];
    if (!mention) throw new Error(`no mention in ${item.title}`);
    const anchor = (await ok(
      await request.post(`/api/layers/${source.layer_id}/anchors`, {
        data: { quote: mention.quote.exact, start: mention.start },
      }),
    )) as { id: string };
    return {
      ...include(),
      evidence_anchor_ids: [anchor.id],
      criteria: source.mentions.map((c) => ({
        criterion_id: c.criterion_id,
        status: "met",
        evidence_anchor_ids: [anchor.id],
      })),
    };
  };

  /**
   * Decides `expected` items of the person's queue as the AI reads them; `decide` gives the blind
   * decision and the one to revise to if the AI is shown (null keeps the blind one).
   */
  const screen = async (
    stage: "title_abstract" | "full_text",
    expected: number,
    decide: (item: Item) => Promise<[Decision, Decision | null]>,
  ) => {
    let done = 0;
    for (let polls = 0; done < expected; polls++) {
      if (polls > 240) throw new Error(`only ${done} of ${expected} items arrived`);
      const items = (await get(
        `screening/queue?scope_id=${SCOPE}&stage=${stage}&limit=50`,
      )) as Item[];
      if (items.length === 0) await new Promise((r) => setTimeout(r, 500));
      for (const item of items) {
        const [blind, revised] = await decide(item);
        const now = new Date();
        const at = (s: number) => new Date(now.getTime() + s * 1000).toISOString();
        const timing = { shown_at: at(0), first_key_at: at(2), decided_at: at(9) };
        const path = `screening/assignments/${item.assignment_id}/decide`;
        const result = (await post(path, {
          command_id: crypto.randomUUID(),
          phase: "provisional",
          decision: blind,
          timing: { ...timing, reveal_shown_at: null },
          expected_etag: item.etag,
        })) as { phase: string; etag: string };
        if (result.phase === "provisional") {
          await post(path, {
            command_id: crypto.randomUUID(),
            phase: "final",
            decision: revised ?? blind,
            timing: { ...timing, reveal_shown_at: at(10), decided_at: at(25) },
            expected_etag: result.etag,
          });
        }
        done++;
      }
    }
  };
  return { get, post, screen, fullTextInclude };
};

test("the lead traces a count to a record, looks back at an earlier event, and sees a tampered decision pinpointed", async ({
  browser,
  request,
}) => {
  const created = cli("create", `Audit ${new Date().toISOString()}`, "--lead", "Morgan Lee");
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
  const citations = readFileSync(join(DEMO_DATA, "citation_search.ris"), "utf8");
  const hand = join(mkdtempSync(join(tmpdir(), "kittum-audit-")), "handsearch.ris");
  writeFileSync(hand, `${citations.slice(0, citations.indexOf("ER  -") + 5)}\n`);
  for (const [file, source, platform, strategy] of [
    [
      join(DEMO_DATA, "citation_search.ris"),
      "citation_search",
      "Reference lists",
      "Backward citation search",
    ],
    [hand, "handsearch", "BMC Medicine, 2026 issues", "Hand search of the 2026 issues"],
  ] as const) {
    cli(
      "import-search",
      projectId,
      file,
      ...["--topic", "TRIALS", "--source", source, "--platform", platform],
      ...["--strategy", strategy, "--format", "ris", ...by],
    );
  }
  cli("ingest", projectId, ...by);
  const lead = as(request, projectId, leadId);
  const latest = (await lead.get("corpus/revisions/latest")) as { revision: { id: string } };
  const proposals = (await lead.get("corpus/proposals?status=pending")) as Array<{
    id: string;
    relation: string;
  }>;
  const duplicate = proposals.find((p) => p.relation === "same_record");
  if (!duplicate) throw new Error("the hand search's duplicate was not proposed");
  await lead.post("corpus/confirm", {
    revision_id: latest.revision.id,
    proposal_id: duplicate.id,
    actor_id: leadId,
  });
  cli(
    "open-stage",
    projectId,
    SCOPE,
    "title_abstract",
    "--sentinels",
    join(EXAMPLES, "sentinels", "nicotine-replacement.yaml"),
    ...by,
  );

  await test.step("the review is screened: a conflict, an adjudication, and a full-text reveal", async () => {
    await as(request, projectId, sam).screen("title_abstract", 4, async () => [include(), null]);
    await as(request, projectId, jun).screen("title_abstract", 4, async (item) => [
      item.title.startsWith(TRIAL)
        ? { ...include(), outcome: "exclude", reason_criterion_id: "POP" }
        : include(),
      null,
    ]);
    const adjudicator = as(request, projectId, elena);
    const [conflict] = (await adjudicator.get(
      `screening/adjudications?scope_id=${SCOPE}&stage=title_abstract`,
    )) as Array<{ conflict_id: string; etag: string; title: string }>;
    expect(conflict?.title).toContain(TRIAL);
    const now = new Date().toISOString();
    await adjudicator.post(`screening/adjudications/${conflict?.conflict_id}/decide`, {
      command_id: crypto.randomUUID(),
      expected_etag: conflict?.etag,
      adopted: null,
      decision: include(),
      timing: { shown_at: now, first_key_at: now, decided_at: now },
    });

    const reports = (await lead.get("full-texts")) as Array<{ item_ref: string; title: string }>;
    const trial = reports.find((r) => r.title.startsWith(TRIAL));
    cli(
      "attach-full-text",
      projectId,
      trial?.item_ref ?? "",
      join(FIXTURES, "gerber2026-lesotho-t2d-crct.pdf"),
      "--licence",
      "CC BY 4.0",
      ...by,
    );
    const sentinel = await request.post(`/api/projects/${projectId}/full-text-sentinels`, {
      params: { actor_id: leadId, title: "Vitamin C", expected_outcome: "exclude" },
      headers: { "Content-Type": "application/pdf" },
      data: readFileSync(join(FIXTURES, "li2026-vitamin-c-kidney-biopsy-rct.pdf")),
    });
    expect(sentinel.status(), await sentinel.text()).toBe(201);
    await lead.post("screening/stages", { scope_id: SCOPE, stage: "full_text", actor_id: leadId });

    // A excludes the trial on the intervention; the model saw no failing criterion, so its
    // reading is shown, and A revises to include.
    const a = as(request, projectId, sam);
    await a.screen("full_text", 2, async (item) => {
      const included = await a.fullTextInclude(item);
      if (!item.title.startsWith(TRIAL)) return [included, null];
      const criteria = (included["criteria"] as Array<Record<string, unknown>>).map((c) =>
        c["criterion_id"] === "INT" ? { ...c, status: "not_met" } : c,
      );
      return [{ ...included, outcome: "exclude", reason_criterion_id: "INT", criteria }, included];
    });
    const b = as(request, projectId, jun);
    await b.screen("full_text", 2, async (item) => [await b.fullTextInclude(item), null]);
    const auditor = as(request, projectId, rita);
    await auditor.screen("full_text", 1, async (item) => [
      await auditor.fullTextInclude(item),
      null,
    ]);
  });

  const page = await (await browser.newContext()).newPage();
  const strip = page.getByTestId("chain-status");
  const timeline = page.getByTestId("timeline");

  await test.step("the lead opens a PRISMA count and traces the trial through every stage", async () => {
    await page.goto(`/p/${projectId}/progress?as=${leadId}`);
    await expect(page.getByRole("heading", { name: "Progress", level: 1 })).toBeVisible();
    await page.keyboard.press("g");
    await page.keyboard.press("l");
    await expect(page.getByRole("heading", { name: "Audit", level: 1 })).toBeVisible();
    await expect(strip).toContainText(/Every event up to \d+ of \d+ matches its hashes\./);

    await page.getByTestId("box-reports_included").click();
    await expect(page.getByTestId("box-reports_included")).toContainText("1");
    await page.getByTestId("member").filter({ hasText: TRIAL }).click();
    await expect(page.getByTestId("trace-title")).toContainText(TRIAL);
    await expect(timeline.getByText("Found 2 times")).toBeVisible();

    const events = timeline.getByTestId("trace-event");
    const kinds = await events.evaluateAll((rows) =>
      rows.map((r) => [Number(r.getAttribute("data-seq")), r.getAttribute("data-kind")] as const),
    );
    const seqs = kinds.map(([seq]) => seq);
    expect(seqs).toEqual([...seqs].sort((x, y) => x - y));
    const first = (kind: string) => kinds.findIndex(([, k]) => k === kind);
    for (const [earlier, later] of [
      ["search", "identity"],
      ["identity", "stage"],
      ["stage", "decision"],
      ["decision", "adjudication"],
      ["adjudication", "full_text"],
    ]) {
      expect(first(earlier ?? ""), `${earlier} before ${later}`).toBeLessThan(first(later ?? ""));
    }
    await expect(
      events.filter({ hasText: "Morgan Lee confirmed 2 search results as the same record" }),
    ).toHaveCount(1);
    await expect(
      events.filter({ hasText: "Jun Park excluded the record on POP, blind" }),
    ).toHaveCount(1);
    await expect(
      events.filter({ hasText: "Elena Weiss adjudicated and included the record" }),
    ).toHaveCount(1);
    await expect(events.filter({ hasText: "Morgan Lee uploaded the full text" })).toHaveCount(1);
    await expect(
      events.filter({
        hasText: "Rita Nunes included the record, blind (the auditor's audit sample)",
      }),
    ).toHaveCount(1);
    // A decided blind; the AI's reading came only afterwards, and A revised.
    const reveal = page.getByTestId("reveal");
    await expect(reveal).toContainText("Sam Okafor decided blind at event");
    await expect(reveal).toContainText("Full text");
    await expect(reveal).toContainText("They changed it at event");
    await expect(
      events.filter({ hasText: "Sam Okafor excluded the record on INT, blind" }),
    ).toHaveCount(1);
    await expect(
      events.filter({
        hasText: "Sam Okafor included the record, after seeing the AI's reading",
      }),
    ).toHaveCount(1);
    // Each event says what happened first; its hashes open to the content they cover, each
    // with Kittum's check that the content still hashes to the recorded value.
    const upload = events.filter({ hasText: "Morgan Lee uploaded the full text" });
    await expect(upload.getByTestId("event-facts")).toContainText("CC BY 4.0");
    await upload.getByRole("button", { name: /^Full text PDF/ }).click();
    const pdf = upload.getByTestId("artifact-content");
    await expect(pdf.getByTestId("hash-check")).toHaveAttribute("data-matches", "true");
    await expect(pdf).toContainText("The hash matches the recorded one.");
    await upload.getByRole("button", { name: /Show the hashes/ }).click();
    await upload.getByRole("button", { name: /Payload hash/ }).click();
    await expect(upload.getByTestId("event-hashes")).toContainText('"licence": "CC BY 4.0"');
    await expect(upload.getByTestId("event-hashes").getByTestId("hash-check")).toHaveAttribute(
      "data-matches",
      "true",
    );
    await shot(page, "1-trace");

    // The AI judge's reading of the full text opens to the exact request and response.
    const judged = events.filter({ hasText: "The AI judge" }).last();
    await expect(judged).toHaveAttribute("data-kind", "ai");
    await judged.getByRole("button", { name: "Show the request and the response" }).click();
    const request = judged.getByTestId("call-request");
    await expect(request.getByLabel("System prompt")).toContainText(
      "You screen records for a systematic literature review.",
    );
    await expect(request.getByLabel("User prompt")).toContainText(`Title: ${TRIAL}`);
    await expect(request.getByTestId("hash-check")).toHaveAttribute("data-matches", "true");
    const response = judged.getByTestId("call-response");
    await expect(response.getByLabel("Answer")).toContainText('"status"');
    await expect(response.getByTestId("hash-check")).toHaveAttribute("data-matches", "true");
    await judged.scrollIntoViewIfNeeded();
    await shot(page, "1b-model-call");
  });

  await test.step("the lead looks back at the review when title and abstract closed, and keeps that report", async () => {
    await page.keyboard.press("3");
    await page.getByTestId("milestone").filter({ hasText: "Title and abstract closed" }).click();
    const report = page.getByTestId("as-of");
    await expect(report).toContainText("Title and abstract closed");
    const flow = report.locator(`[data-testid=asof-flow][data-scope=${SCOPE}]`);
    await expect(flow.getByTestId("asof-reports_sought")).toHaveText("3");
    await expect(flow.getByTestId("asof-reports_assessed")).toHaveText("0");
    await expect(flow.getByTestId("now-reports_assessed")).toHaveText("1");
    await expect(flow.getByTestId("asof-reports_included")).toHaveText("0");
    await expect(flow.getByTestId("now-reports_included")).toHaveText("1");
    const disclosure = page.getByTestId("asof-disclosure");
    await expect(disclosure.locator("tbody tr")).toHaveCount(1);
    await expect(disclosure.locator("tbody tr").first()).toHaveAttribute(
      "data-stage",
      "title_abstract",
    );
    await page.keyboard.press("p");
    const kept = page.getByTestId("kept-report");
    await expect(kept).toContainText("Rendered at event");
    await expect(kept).toContainText("prisma");
    await expect(kept).toContainText("ai_disclosure");
    await expect(kept.getByRole("img")).toBeVisible();
    await shot(page, "3-as-of");
  });

  let tampered = 0;
  await test.step("a decision edited in the database is pinpointed: the event and the check it fails", async () => {
    await page.keyboard.press("2");
    await page.keyboard.press("c");
    await expect(page.getByTestId("checkpoints").locator("tbody tr")).toHaveCount(1);
    await expect(page.getByTestId("integrity-verdict")).toContainText(
      "matches its hashes and links",
    );

    // Jun's blind exclusion of the trial at title and abstract becomes an inclusion.
    const trace = (await lead.get(`audit/records/${await traceRef(lead)}/trace`)) as {
      events: Array<{ seq: number; type: string; actor_id: string; stage: string | null }>;
    };
    const target = trace.events.find(
      (e) => e.type === "screening.decided" && e.actor_id === jun && e.stage === "title_abstract",
    );
    tampered = target?.seq ?? 0;
    expect(tampered).toBeGreaterThan(0);
    forgePayload(projectId, tampered, "outcome", "include");

    await page.keyboard.press("v");
    await expect(page.getByTestId("integrity-verdict")).toHaveText(
      `The chain breaks at event ${tampered}.`,
    );
    const finding = page.getByTestId("finding");
    await expect(finding).toHaveCount(1);
    await expect(finding).toHaveAttribute("data-seq", String(tampered));
    await expect(finding).toHaveAttribute("data-check", "payload_hash");
    await expect(finding).toContainText("Payload hash, screening.decided");
    await expect(finding).toContainText("Recomputed");
    await expect(page.getByTestId("strip-break")).toHaveCount(1);
    await expect(strip).toContainText(`The chain breaks at event ${tampered}`);
    // The checkpoint still verifies: an edit that keeps the hashes leaves the chain's head alone.
    await expect(page.getByTestId("checkpoints")).toContainText("Valid");
    await finding.getByRole("button", { name: `Show event ${tampered} as stored` }).click();
    await expect(page.getByTestId("shown-event")).toContainText('"outcome": "include"');
    await shot(page, "2-integrity-tampered");

    // The trial's timeline marks the event.
    await page
      .getByTestId("shown-event")
      .getByRole("button", { name: "Trace the record it concerns" })
      .click();
    const broken = page.locator(`[data-testid=trace-event][data-seq="${tampered}"]`);
    await expect(broken).toHaveAttribute("data-broken", "true");
    await expect(broken.getByTestId("event-finding")).toContainText("Fails the payload hash check");
    await broken.getByRole("button", { name: /Show the hashes/ }).click();
    await broken.getByRole("button", { name: /Payload hash/ }).click();
    await expect(broken.getByTestId("hash-check")).toHaveAttribute("data-matches", "false");
    await broken.scrollIntoViewIfNeeded();
    await shot(page, "4-trace-tampered");

    const verified = (() => {
      try {
        return execFileSync("uv", ["run", "kittum", "ledger", "verify", projectId], {
          cwd: SERVER,
          env: stackEnv(),
          encoding: "utf8",
        });
      } catch (e) {
        return String((e as { stdout?: string }).stdout ?? "");
      }
    })();
    expect(verified).toContain(`FAILED project ${projectId}: seq ${tampered} payload_mismatch`);
    await page.context().close();
  });

  await test.step("a screener has no Audit screen and every audit route refuses them", async () => {
    const screener = await (await browser.newContext()).newPage();
    await screener.goto(`/p/${projectId}/progress?as=${sam}`);
    await expect(screener.getByTestId("identity")).toContainText("Sam Okafor");
    const nav = screener.getByRole("navigation", { name: "Review stages" });
    await expect(nav.getByRole("link", { name: "Reports" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Audit" })).toHaveCount(0);
    await screener.goto(`/p/${projectId}/audit?as=${sam}`);
    await expect(screener.getByText(/The ledger contains every reviewer/)).toBeVisible();
    await expect(screener.getByTestId("chain-strip")).toHaveCount(0);
    const ref = await traceRef(lead);
    const headers = { "X-Kittum-Reviewer": sam };
    for (const path of [
      `records/${ref}/trace`,
      "events",
      "verify",
      "checkpoints",
      "milestones",
      "as-of",
      `artifacts/${"0".repeat(64)}`,
      `artifacts/${"0".repeat(64)}/check`,
    ]) {
      const refused = await request.get(`/api/projects/${projectId}/audit/${path}`, { headers });
      expect(refused.status(), path).toBe(403);
    }
    const signed = await request.post(`/api/projects/${projectId}/audit/checkpoints`, {
      data: { actor_id: sam },
    });
    expect(signed.status()).toBe(403);
    await screener.context().close();
  });
});

/** The trial's reference, as the lead's PRISMA count lists it. */
const traceRef = async (lead: ReturnType<typeof as>) => {
  const now = (await lead.get("audit/as-of")) as {
    flows: Array<{ scope_id: string; members: Record<string, string[]> }>;
  };
  const flow = now.flows.find((f) => f.scope_id === SCOPE);
  const ref = flow?.members["reports_included"]?.[0];
  if (!ref) throw new Error("no report is included");
  return ref;
};
