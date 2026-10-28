// An in-memory Workbench over recorded fixtures: 28 PubMed records for the title and abstract
// queue, and the open-access ABC-06 trial rendered by scripts/render_fixture.py for full text and
// extraction. It behaves like the server should: it decodes every response through the schemas,
// keeps AI output out of queue items, and reveals only after a provisional decision.
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import {
  CriterionVerdict as CriterionVerdictSchema,
  DecideResult,
  ExtractionReview,
  FullTextAssignment,
  PageLayer,
  QueueItem as QueueItemSchema,
} from "../schema.ts";
import type {
  Anchor,
  CriterionVerdict,
  Decision,
  DecisionTiming,
  ProjectSummary,
  QueueItem,
  Reveal,
  ReviewField,
  Stage,
} from "../schema.ts";
import { WorkbenchError } from "../workbench.ts";
import type { Workbench } from "../workbench.ts";
import anchorsJson from "./fixtures/abc06/anchors.json";
import layerJson from "./fixtures/abc06/layer.json";
import recordsJson from "./fixtures/records.json";
import { extractionFields, REPORT_ID, STUDY_ID } from "./extraction.ts";
import { tag } from "./highlights.ts";
import { verdictsFor } from "./judge.ts";
import { CRITERIA, PROJECTS, PROTOCOL, SCOPE_A } from "./protocol.ts";

const pageImages = import.meta.glob<string>("./fixtures/abc06/pages/*.png", {
  eager: true,
  query: "?url",
  import: "default",
});
const pageWords = import.meta.glob<ReadonlyArray<readonly number[]>>("./fixtures/abc06/words/*.json", {
  import: "default",
});

export interface MockOptions {
  /** Milliseconds each kind of call waits, to exercise prefetching and loading states. */
  readonly latency?: Partial<Record<"queue" | "decide" | "page" | "other", number>>;
  /** Overrides the project's reveal setting. */
  readonly reveal?: ProjectSummary["reveal"];
  /** Receives every call, for tests. */
  readonly log?: (call: string, detail: unknown) => void;
}

/** Everything a mock call recorded, for tests and the session summary. */
export interface DecisionRecord {
  readonly assignmentId: string;
  readonly decision: Decision;
  readonly phase: "provisional" | "final";
  readonly revealed: boolean;
  readonly timing: DecisionTiming;
}

const INLINE_LABEL = /(?:^|\s)([A-Z][A-Z &]{2,}[A-Z]):\s/g;

const sentence = (label: string) => label.charAt(0) + label.slice(1).toLowerCase();

/** Abstract text with labels lifted out as (label, start, end) code-point ranges. */
const composeAbstract = (sections: ReadonlyArray<{ label: string | null; text: string }>) => {
  const parts: Array<{ label: string; text: string }> = [];
  for (const section of sections) {
    if (section.label) {
      parts.push({ label: section.label, text: section.text });
      continue;
    }
    // Some records keep their labels inline ("BACKGROUND: ... METHODS: ...").
    const matches = [...section.text.matchAll(INLINE_LABEL)];
    if (matches.length === 0) {
      parts.push({ label: "", text: section.text });
      continue;
    }
    const head = section.text.slice(0, matches[0]?.index ?? 0).trim();
    if (head) parts.push({ label: "", text: head });
    matches.forEach((match, i) => {
      const from = match.index + match[0].length;
      const to = matches[i + 1]?.index ?? section.text.length;
      parts.push({ label: sentence(match[1] ?? ""), text: section.text.slice(from, to).trim() });
    });
  }
  let text = "";
  const ranges: Array<readonly [string, number, number]> = [];
  for (const part of parts) {
    if (text) text += "\n\n";
    const start = [...text].length;
    text += part.text;
    ranges.push([part.label, start, [...text].length]);
  }
  return { text, sections: ranges };
};

const RECORDS = recordsJson.records.map((record, index) => {
  const { text, sections } = composeAbstract(record.sections);
  const item: QueueItem = {
    assignment_id: `asg-ta-${record.pmid}`,
    stage: "title_abstract",
    scope_id: SCOPE_A,
    title: record.title,
    text,
    highlights: tag(text),
    revision: 0,
    record: {
      pmid: record.pmid,
      doi: record.doi ?? null,
      journal: record.journal,
      year: record.year,
      publication_types: record.publication_types,
      registry_ids: record.registry_ids,
      title_highlights: tag(record.title),
      sections,
    },
  };
  return { order: index, pmid: record.pmid, item };
});

const TA_CRITERIA = CRITERIA.filter((c) => c.decidable_at.includes("title_abstract")).map((c) => c.id);

/** The judge's verdicts with each quote checked against the record, as `kittum.core.find_quote` would. */
const verdicts = (pmid: string, item: QueueItem): CriterionVerdict[] =>
  verdictsFor(pmid, TA_CRITERIA).map((v) => {
    const found = v.quote !== null && (item.title.includes(v.quote) || item.text.includes(v.quote));
    return Schema.decodeUnknownSync(CriterionVerdictSchema)({
      ...v,
      status: v.status === "not_met" && !found ? "unclear" : v.status,
      quote_verified: found,
      downgraded_from: v.status === "not_met" && !found ? "not_met" : null,
    });
  });

const toReveal = (verdict: CriterionVerdict): Reveal => ({
  criterion_id: verdict.criterion_id,
  quote: verdict.quote ?? "",
  rationale: verdict.rationale,
  status: verdict.status,
});

/**
 * The reveal rule from DESIGN.md: at title and abstract, `unclear` means include, so the AI
 * disagrees when exactly one side excludes. Returns null when they agree.
 */
export const disagreement = (decision: Decision, judged: ReadonlyArray<CriterionVerdict>): Reveal | null => {
  const exclusion = judged.find((v) => v.status === "not_met" && v.quote_verified);
  if (decision.outcome === "exclude") {
    if (exclusion) return null;
    const reading = judged.find((v) => v.criterion_id === decision.reason_criterion_id);
    return reading ? toReveal(reading) : null;
  }
  return exclusion ? toReveal(exclusion) : null;
};

const layer = {
  id: layerJson.layer.id,
  document_id: layerJson.layer.document_id,
  recipe_hash: layerJson.layer.recipe_hash,
  output_hash: layerJson.layer.output_hash,
  text: layerJson.layer.text,
  pages: layerJson.pages,
};
const report = {
  id: REPORT_ID,
  study_id: STUDY_ID,
  study_label: "ABC-06",
  title:
    "Second-line FOLFOX chemotherapy versus active symptom control for advanced biliary tract cancer (ABC-06): a phase 3, open-label, randomised, controlled trial",
  citation: "Lamarca A, Palmer DH, Wasan HS, et al. Lancet Oncol 2021; 22: 690–701",
  licence: "CC BY 4.0",
};
const valueAnchors = anchorsJson.values as Record<string, Anchor>;
const mentionAnchors = anchorsJson.mentions as Record<string, Anchor[]>;

export const makeMockWorkbench = (options: MockOptions = {}): Workbench & {
  readonly decisions: ReadonlyArray<DecisionRecord>;
} => {
  const decisions: DecisionRecord[] = [];
  const state = new Map<string, { revision: number; revealed: boolean; final: Decision | null }>();
  const accepted = new Map<string, "janus" | "llm">();

  const wait = (kind: keyof NonNullable<MockOptions["latency"]>) => {
    const ms = options.latency?.[kind] ?? 0;
    return ms > 0 ? Effect.sleep(ms) : Effect.void;
  };
  const call = <A>(kind: keyof NonNullable<MockOptions["latency"]>, name: string, detail: unknown, body: () => A) =>
    Effect.gen(function* () {
      options.log?.(name, detail);
      yield* wait(kind);
      return body();
    });
  const fail = (reason: WorkbenchError["reason"], message: string) =>
    Effect.fail(new WorkbenchError({ reason, message }));

  const project = (projectId: string) => PROJECTS.find((p) => p.id === projectId);
  const revealSetting = () => options.reveal ?? PROJECTS[0]?.reveal ?? "on_disagreement";

  const pending = (scopeId: string, stage: Stage) =>
    stage === "title_abstract" && scopeId === SCOPE_A
      ? RECORDS.filter((r) => state.get(r.item.assignment_id)?.final == null)
      : [];

  return {
    decisions,

    projects: () => call("other", "projects", null, () => PROJECTS),

    protocol: (projectId) =>
      project(projectId) ? call("other", "protocol", projectId, () => PROTOCOL) : fail("not_found", `No project ${projectId}`),

    queue: ({ scopeId, stage, after, limit }) =>
      call("queue", "queue", { scopeId, stage, after, limit }, () => {
        const open = pending(scopeId, stage);
        const from = after === null ? 0 : open.findIndex((r) => r.item.assignment_id === after) + 1;
        return open.slice(from, from + limit).map((r) =>
          Schema.decodeUnknownSync(QueueItemSchema)({
            ...r.item,
            revision: state.get(r.item.assignment_id)?.revision ?? 0,
          }),
        );
      }),

    decide: ({ assignmentId, decision, phase, expectedRevision, timing }) =>
      Effect.gen(function* () {
        options.log?.("decide", { assignmentId, decision, phase });
        yield* wait("decide");
        const record = RECORDS.find((r) => r.item.assignment_id === assignmentId);
        if (!record) return yield* fail("not_found", `No assignment ${assignmentId}`);
        const current = state.get(assignmentId) ?? { revision: 0, revealed: false, final: null };
        if (current.revision !== expectedRevision) {
          return yield* fail("conflict", "This record changed since it was loaded. Reload the queue.");
        }
        const setting = revealSetting();
        let reveal: Reveal | null = null;
        if (phase === "provisional" && !current.revealed && setting !== "never") {
          const judged = verdicts(record.pmid, record.item);
          reveal = disagreement(decision, judged);
          if (!reveal && setting === "always") {
            const basis = judged.find((v) => v.status === "not_met" && v.quote_verified) ??
              judged.find((v) => v.status === "met" && v.quote_verified);
            reveal = basis ? toReveal(basis) : null;
          }
        }
        const revealed = current.revealed || reveal !== null;
        const settled = reveal === null;
        state.set(assignmentId, {
          revision: current.revision + 1,
          revealed,
          final: settled ? decision : current.final,
        });
        decisions.push({ assignmentId, decision, phase: settled ? "final" : "provisional", revealed, timing });
        return {
          ...Schema.decodeUnknownSync(DecideResult)({
            phase: settled ? "final" : "provisional",
            reveal,
            recorded_at: timing.decided_at,
          }),
          revision: current.revision + 1,
        };
      }),

    status: (scopeId, stage) =>
      call("other", "status", { scopeId, stage }, () => {
        const total = stage === "title_abstract" && scopeId === SCOPE_A ? RECORDS.length : 0;
        const open = pending(scopeId, stage).length;
        return { scope_id: scopeId, stage, screened: total - open, remaining: open, conflicts: 0 };
      }),

    fullText: (projectId) =>
      call("other", "fullText", projectId, () =>
        project(projectId)?.scope_id === SCOPE_A
          ? Schema.decodeUnknownSync(FullTextAssignment)({
              assignment_id: "asg-ft-abc06",
              scope_id: SCOPE_A,
              revision: 0,
              report,
              document: layerJson.document,
              layer,
              mentions: CRITERIA.filter((c) => c.decidable_at.includes("full_text")).map((c) => [
                c.id,
                mentionAnchors[c.id] ?? [],
              ]),
            })
          : null,
      ),

    page: (layerId, page) =>
      Effect.gen(function* () {
        options.log?.("page", { layerId, page });
        yield* wait("page");
        const size = layer.pages.find((p) => p.page === page);
        const load = pageWords[`./fixtures/abc06/words/${page}.json`];
        const image = pageImages[`./fixtures/abc06/pages/${page}.png`];
        if (layerId !== layer.id || !size || !load || !image) {
          return yield* fail("not_found", `No page ${page} in layer ${layerId}`);
        }
        const rows = yield* Effect.promise(load);
        return Schema.decodeUnknownSync(PageLayer)({
          ...size,
          image_url: image,
          words: rows.map(([start, end, x0, y0, x1, y1]) => ({ start, end, box: { page, x0, y0, x1, y1 } })),
        });
      }),

    extraction: (projectId) =>
      call("other", "extraction", projectId, () => {
        if (project(projectId)?.scope_id !== SCOPE_A) return null;
        const fields: ReviewField[] = extractionFields((name) => {
          const anchor = valueAnchors[name];
          if (!anchor) throw new Error(`fixture has no anchor ${name}`);
          return anchor.id;
        }).map((field) => ({ ...field, accepted: accepted.get(field.id) ?? field.accepted }));
        return Schema.decodeUnknownSync(ExtractionReview)({
          report,
          document: layerJson.document,
          layer,
          anchors: Object.values(valueAnchors),
          fields,
        });
      }),

    accept: ({ fieldId, path, sourceViewedMs }) =>
      call("decide", "accept", { fieldId, path, sourceViewedMs }, () => {
        accepted.set(fieldId, path);
      }),
  };
};
