// An in-memory Workbench over recorded fixtures: 28 PubMed records for the title and abstract
// queue, and the open-access ABC-06 trial rendered by scripts/render_fixture.py for full text and
// extraction. It behaves like the server should: it decodes every response through the schemas,
// keeps AI output out of queue items, and reveals only after a provisional decision.
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import {
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
  QueueItem,
  Reveal,
  RevealSetting,
  ReviewField,
  ScopeStatus,
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
const pageWords = import.meta.glob<ReadonlyArray<readonly number[]>>(
  "./fixtures/abc06/words/*.json",
  {
    import: "default",
  },
);

export interface MockOptions {
  /** Milliseconds each kind of call waits, to exercise prefetching and loading states. */
  readonly latency?: Partial<Record<"queue" | "decide" | "page" | "other", number>>;
  /** Overrides the stage's reveal setting. */
  readonly reveal?: RevealSetting;
  /** Pages whose image URL is broken, to exercise the failed-image path. */
  readonly brokenPages?: ReadonlyArray<number>;
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

const FULL_TEXT_ASSIGNMENT = "asg-ft-abc06";

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
  const item: Omit<QueueItem, "record"> & { record: QueueItem["record"] } = {
    assignment_id: `asg-ta-${record.pmid}`,
    stage: "title_abstract",
    scope_id: SCOPE_A,
    title: record.title,
    text,
    highlights: tag(text),
    etag: "e0",
    phase: "provisional",
    reveal: null,
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

const TA_CRITERIA = CRITERIA.filter((c) => c.decidable_at.includes("title_abstract")).map(
  (c) => c.id,
);

/** The judge's verdicts with each quote checked against the record, as `kittum.core.find_quote` would. */
const verdicts = (pmid: string, item: QueueItem): CriterionVerdict[] =>
  verdictsFor(pmid, TA_CRITERIA).map((v) => {
    const found = v.quote !== null && (item.title.includes(v.quote) || item.text.includes(v.quote));
    return {
      ...v,
      status: v.status === "not_met" && !found ? "unclear" : v.status,
      quote_verified: found,
      downgraded_from: v.status === "not_met" && !found ? "not_met" : null,
    };
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
const disagreement = (
  decision: Decision,
  judged: ReadonlyArray<CriterionVerdict>,
): Reveal | null => {
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
  text: layerJson.layer.text,
  pages: layerJson.pages,
};
const report = {
  id: REPORT_ID,
  study_label: "ABC-06",
  title:
    "Second-line FOLFOX chemotherapy versus active symptom control for advanced biliary tract cancer (ABC-06): a phase 3, open-label, randomised, controlled trial",
  citation: "Lamarca A, Palmer DH, Wasan HS, et al. Lancet Oncol 2021; 22: 690–701",
  licence: "CC BY 4.0",
};
const valueAnchors = anchorsJson.values as Record<string, Anchor>;
const mentionAnchors = anchorsJson.mentions as Record<string, Anchor[]>;

type Item = {
  etag: string;
  state: "pending" | "awaiting_final" | "decided";
  revealed: boolean;
  reveal: Reveal | null;
  final: Decision | null;
};

export const makeMockWorkbench = (
  options: MockOptions = {},
): Workbench & {
  readonly decisions: ReadonlyArray<DecisionRecord>;
  /** Every acceptance, with the reviewer's edit and its span when there was one. */
  readonly acceptances: ReadonlyArray<Parameters<Workbench["accept"]>[0]>;
} => {
  const decisions: DecisionRecord[] = [];
  const state = new Map<string, Item>();
  const accepted = new Map<string, "janus" | "llm">();
  const acceptances: Array<Parameters<Workbench["accept"]>[0]> = [];
  let tokens = 0;
  const etag = () => `e${++tokens}`;
  const itemOf = (id: string): Item =>
    state.get(id) ?? { etag: "e0", state: "pending", revealed: false, reveal: null, final: null };

  const wait = (kind: keyof NonNullable<MockOptions["latency"]>) => {
    const ms = options.latency?.[kind] ?? 0;
    return ms > 0 ? Effect.sleep(ms) : Effect.void;
  };
  const call = <A>(
    kind: keyof NonNullable<MockOptions["latency"]>,
    name: string,
    detail: unknown,
    body: () => A,
  ) =>
    Effect.gen(function* () {
      options.log?.(name, detail);
      yield* wait(kind);
      return body();
    });
  const fail = (reason: WorkbenchError["reason"], message: string) =>
    Effect.fail(new WorkbenchError({ reason, message }));

  const hasData = (projectId: string) => projectId === PROJECTS[0]?.id;
  const revealSetting = () => options.reveal ?? "on_disagreement";

  const pending = (scopeId: string, stage: Stage) =>
    stage === "title_abstract" && scopeId === SCOPE_A
      ? RECORDS.filter((r) => itemOf(r.item.assignment_id).state !== "decided")
      : [];

  const unavailable = () => fail("unavailable", "The example data has no server behind it.");

  return {
    live: false,
    decisions,
    acceptances,

    projects: () => call("other", "projects", null, () => PROJECTS),

    // Setting a review up needs the server: it reads the files and records each step.
    createProject: unavailable,
    addMember: unavailable,
    protocolExamples: () => call("other", "protocolExamples", null, () => []),
    protocolSource: unavailable,
    parseProtocol: unavailable,
    validateProtocol: unavailable,
    previewProtocol: unavailable,
    publishProtocol: unavailable,
    runSearch: unavailable,
    importSearch: unavailable,
    ingestCorpus: unavailable,
    parseSentinels: unavailable,
    fullTexts: () => call("other", "fullTexts", null, () => []),
    attachFullText: unavailable,
    attachSentinelFullText: unavailable,

    protocol: (projectId) => {
      if (!PROJECTS.some((p) => p.id === projectId)) {
        return fail("not_found", `No project ${projectId}`);
      }
      return call("other", "protocol", projectId, () => (hasData(projectId) ? PROTOCOL : null));
    },

    queue: ({ scopeId, stage, after, limit }) =>
      call("queue", "queue", { scopeId, stage, after, limit }, () => {
        // `after` keeps its place in queue order even once it has been decided.
        const cursor =
          after === null ? -1 : (RECORDS.find((r) => r.item.assignment_id === after)?.order ?? -1);
        return pending(scopeId, stage)
          .filter((r) => r.order > cursor)
          .slice(0, limit)
          .map((r) => {
            const item = itemOf(r.item.assignment_id);
            return Schema.decodeUnknownSync(QueueItemSchema)({
              ...r.item,
              etag: item.etag,
              phase: item.state === "awaiting_final" ? "final" : "provisional",
              reveal: item.state === "awaiting_final" ? item.reveal : null,
            });
          });
      }),

    decide: ({ assignmentId, decision, phase, expectedEtag, timing }) =>
      Effect.gen(function* () {
        options.log?.("decide", { assignmentId, decision, phase });
        yield* wait("decide");
        const current = itemOf(assignmentId);
        if (current.etag !== expectedEtag) {
          return yield* fail(
            "conflict",
            "This record changed since it was loaded. Reload the queue.",
          );
        }
        if (assignmentId === FULL_TEXT_ASSIGNMENT) {
          // The full-text judge is not modelled; the decision settles.
          const next = etag();
          state.set(assignmentId, { ...current, etag: next, state: "decided", final: decision });
          decisions.push({ assignmentId, decision, phase: "final", revealed: false, timing });
          return {
            phase: "final" as const,
            reveal: null,
            recorded_at: timing.decided_at,
            etag: next,
          };
        }
        const record = RECORDS.find((r) => r.item.assignment_id === assignmentId);
        if (!record) return yield* fail("not_found", `No assignment ${assignmentId}`);
        if (phase === "provisional" && current.state !== "pending") {
          return yield* fail("invalid", "This item is decided; undo it first.");
        }
        if (phase === "final" && current.state !== "awaiting_final") {
          return yield* fail("invalid", "A final decision follows a provisional one and a reveal.");
        }
        const setting = revealSetting();
        let reveal: Reveal | null = null;
        if (phase === "provisional" && !current.revealed && setting !== "never") {
          const judged = verdicts(record.pmid, record.item as QueueItem);
          reveal = disagreement(decision, judged);
          if (!reveal && setting === "always") {
            const basis =
              judged.find((v) => v.status === "not_met" && v.quote_verified) ??
              judged.find((v) => v.status === "met" && v.quote_verified);
            reveal = basis ? toReveal(basis) : null;
          }
        }
        const settled = reveal === null;
        const next = etag();
        state.set(assignmentId, {
          etag: next,
          state: settled ? "decided" : "awaiting_final",
          revealed: current.revealed || reveal !== null,
          reveal: reveal ?? current.reveal,
          final: settled ? decision : current.final,
        });
        decisions.push({
          assignmentId,
          decision,
          phase: settled ? "final" : "provisional",
          revealed: current.revealed || reveal !== null,
          timing,
        });
        return Schema.decodeUnknownSync(DecideResult)({
          phase: settled ? "final" : "provisional",
          reveal,
          recorded_at: timing.decided_at,
          etag: next,
        });
      }),

    undo: ({ assignmentId, expectedEtag }) =>
      call("decide", "undo", { assignmentId }, () => {
        const current = itemOf(assignmentId);
        if (current.etag !== expectedEtag || current.state === "pending") {
          throw new Error(`undo of ${assignmentId} with a stale token`);
        }
        const next = etag();
        state.set(assignmentId, { ...current, etag: next, state: "pending", final: null });
        return { etag: next };
      }),

    status: ({ scopeId, stage }) =>
      call("other", "status", { scopeId, stage }, (): ScopeStatus => {
        const total = stage === "title_abstract" && scopeId === SCOPE_A ? RECORDS.length : 0;
        const open = pending(scopeId, stage).length;
        return {
          scope_id: scopeId,
          stage,
          view: "reviewer",
          screened: total - open,
          remaining: open,
          conflicts: null,
          metrics: null,
        };
      }),

    fullText: ({ projectId }) =>
      call("other", "fullText", projectId, () =>
        hasData(projectId) && itemOf(FULL_TEXT_ASSIGNMENT).state !== "decided"
          ? Schema.decodeUnknownSync(FullTextAssignment)({
              assignment_id: FULL_TEXT_ASSIGNMENT,
              scope_id: SCOPE_A,
              etag: itemOf(FULL_TEXT_ASSIGNMENT).etag,
              phase: "provisional",
              reveal: null,
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

    page: (pageLayer, page) =>
      Effect.gen(function* () {
        options.log?.("page", { layerId: pageLayer.id, page });
        yield* wait("page");
        const size = layer.pages.find((p) => p.page === page);
        const load = pageWords[`./fixtures/abc06/words/${page}.json`];
        const image = pageImages[`./fixtures/abc06/pages/${page}.png`];
        if (pageLayer.id !== layer.id || !size || !load || !image) {
          return yield* fail("not_found", `No page ${page} in layer ${pageLayer.id}`);
        }
        const rows = yield* Effect.promise(load);
        return Schema.decodeUnknownSync(PageLayer)({
          ...size,
          image_url: options.brokenPages?.includes(page) ? `${image}.missing.png` : image,
          words: rows.map(([start, end, x0, y0, x1, y1]) => ({
            start,
            end,
            box: { page, x0, y0, x1, y1 },
          })),
        });
      }),

    adopt: ({ layerId, quote, start }) =>
      call("decide", "adopt", { layerId, quote, start }, (): Anchor => {
        const mention = Object.values(mentionAnchors)
          .flat()
          .find((a) => a.start === start);
        return (
          mention ?? {
            id: `anchor-${start}`,
            layer_id: layerId,
            quote: { exact: quote, prefix: "", suffix: "" },
            start,
            end: start + [...quote].length,
            boxes: [],
          }
        );
      }),

    adjudications: () => call("other", "adjudications", null, () => []),
    adjudicate: unavailable,
    progress: ({ projectId, as }) =>
      call("other", "progress", { projectId, as }, () => ({
        identity_revision_id: null,
        identification: null,
        searches: 0,
        full_texts: 0,
        stages: [],
      })),
    openStage: unavailable,
    ledger: unavailable,
    searches: () => call("other", "searches", null, () => []),
    corpus: () => call("other", "corpus", null, () => null),
    proposals: () => call("other", "proposals", null, () => []),
    link: unavailable,
    renderReport: unavailable,
    reportFile: unavailable,
    sealBundle: unavailable,

    extraction: (projectId) =>
      call("other", "extraction", projectId, () => {
        if (!hasData(projectId)) return null;
        const fields: ReviewField[] = extractionFields((name) => {
          const anchor = valueAnchors[name];
          if (!anchor) throw new Error(`fixture has no anchor ${name}`);
          return anchor.id;
        }).map((field) => ({ ...field, accepted: accepted.get(field.id) ?? field.accepted }));
        return Schema.decodeUnknownSync(ExtractionReview)({
          report: { ...report, study_id: STUDY_ID },
          document: layerJson.document,
          layer,
          anchors: Object.values(valueAnchors),
          fields,
        });
      }),

    accept: (request) =>
      call("decide", "accept", request, () => {
        accepted.set(request.fieldId, request.path);
        acceptances.push(request);
      }),
  };
};
