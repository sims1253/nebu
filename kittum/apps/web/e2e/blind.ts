// What a screener may never receive, checked on every answer the server sent their page: the
// shape of an AI verdict, another person's decision, the lead's figures, or anything that marks a
// record as a sentinel or an audit item. The journeys record a person's responses and run this.
import { expect } from "@playwright/test";
import type { Page } from "@playwright/test";

// Keys that only the AI's verdict or another person's decision carries.
const AI_FIELDS = ["quote_verified", "downgraded_from", "failed_check", "excludes", "ai"];
// What the person's own reveal may say of the AI's reading besides its words: whether the quote
// was verified, and what the model answered before the check. Nothing else of the verdict.
const REVEAL_FIELDS = new Set(["quote_verified", "downgraded_from"]);
const DECISION_FIELDS = ["columns", "outcome", "reason_criterion_id", "evidence_anchor_ids"];
// Keys that would tell a sentinel from any other record, anywhere; and, in screening answers, an
// audit item (elsewhere `audit` is the protocol's audit plan, which everyone may read).
const SENTINEL_FIELDS = ["expected_outcome", "sentinel_outcome", "sentinel_title"];
// The ledger's event counts by type: how many reports were sought, acquired, or not, by type.
const LEAD_FIELDS = ["by_type"];
const AUDIT_FIELDS = ["audit", "rescreen"];

/** Every object in a JSON value, with the path that leads to it. */
const objects = (value: unknown, path = "$"): Array<[string, Record<string, unknown>]> => {
  if (Array.isArray(value)) return value.flatMap((v, i) => objects(v, `${path}[${i}]`));
  if (value === null || typeof value !== "object") return [];
  const object = value as Record<string, unknown>;
  return [
    [path, object],
    ...Object.entries(object).flatMap(([k, v]) => objects(v, `${path}.${k}`)),
  ];
};

/** Everything the server sent this page, to check later what it saw. */
export const recordResponses = (page: Page) => {
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

/**
 * No answer a screener received has the shape of an AI verdict, another person's decision, the
 * lead's metrics, or a planted record. Their own reveal comes only in the answer to their own
 * decision, or with an item that waits for their final decision. Returns how many JSON answers
 * were checked.
 */
export const expectNothingLeaked = (responses: ReadonlyArray<{ url: string; body: string }>) => {
  let checked = 0;
  for (const { url, body } of responses) {
    let json: unknown;
    try {
      json = JSON.parse(body);
    } catch {
      continue; // page images and the workbench's own files
    }
    checked++;
    // Decisions and verdicts travel only in screening and progress answers; elsewhere the
    // protocol's own words (its "outcome" label, say) are not anyone's decision.
    const decisions = /\/(screening|progress)/.test(url);
    // The person's own reveal is the AI's reading, so it carries the verdict's fields (whether
    // the quote was verified, what the model said before the check). Only there.
    const ownReveals = new Set<string>();
    for (const [path, object] of objects(json)) {
      const keys = decisions
        ? [
            ...AI_FIELDS.filter((key) => !(ownReveals.has(path) && REVEAL_FIELDS.has(key))),
            ...DECISION_FIELDS,
            ...AUDIT_FIELDS,
            ...SENTINEL_FIELDS,
            ...LEAD_FIELDS,
          ]
        : [...SENTINEL_FIELDS, ...LEAD_FIELDS];
      for (const key of keys) {
        expect(key in object, `${url} ${path} has ${key}`).toBe(false);
      }
      if ("metrics" in object) expect(object["metrics"], `${url} ${path}`).toBeNull();
      // An AI reading, not a stage's reveal setting ("on_disagreement").
      if (typeof object["reveal"] === "object" && object["reveal"] !== null) {
        const own = url.endsWith("/decide") || object["phase"] === "final";
        expect(own, `${url} ${path} reveals the AI outside the person's own decision`).toBe(true);
        ownReveals.add(`${path}.reveal`);
      }
    }
    expect(body, `${url} names a sentinel`).not.toMatch(/"sentinel:/);
  }
  return checked;
};
