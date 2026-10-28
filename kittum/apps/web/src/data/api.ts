// The Workbench on the server, through the client @kittum/contracts generates from its OpenAPI
// document. The screens never fetch; everything they read or write goes through here.
import { KittumApi } from "@kittum/contracts";
import * as Effect from "effect/Effect";
import * as FetchHttpClient from "effect/http/FetchHttpClient";
import * as HttpClient from "effect/http/HttpClient";
import * as HttpClientRequest from "effect/http/HttpClientRequest";

import type {
  Criterion,
  Decision,
  DecisionTiming,
  ProfileLabels,
  ProtocolVersion,
} from "./schema.ts";
import { WorkbenchError } from "./workbench.ts";
import type { Workbench } from "./workbench.ts";

const client = Effect.gen(function* () {
  const http = yield* HttpClient.HttpClient;
  return KittumApi.make(http, {
    transformClient: (c) =>
      Effect.succeed(
        HttpClient.mapRequest(c, HttpClientRequest.prependUrl(window.location.origin)),
      ),
  });
});

/** The server's `detail`, or the error's own message. */
const describe = (error: unknown): { status: number | null; message: string } => {
  const e = error as {
    _tag?: string;
    reason?: { _tag?: string; response?: { status: number }; description?: string };
    response?: { status: number };
    cause?: { detail?: unknown };
    message?: string;
  };
  const status = e.reason?.response?.status ?? e.response?.status ?? null;
  let detail: unknown = e.cause?.detail;
  if (detail === undefined && e.reason?.description) {
    try {
      detail = (JSON.parse(e.reason.description) as { detail?: unknown }).detail;
    } catch {
      detail = e.reason.description;
    }
  }
  const message =
    typeof detail === "string"
      ? detail
      : Array.isArray(detail)
        ? detail.map((d: { msg?: string }) => d.msg ?? "").join(" ")
        : detail
          ? JSON.stringify(detail)
          : e.reason?._tag === "TransportError"
            ? "The server is not reachable."
            : (e.message ?? "The server could not answer.");
  return { status, message };
};

const toError = (error: unknown) => {
  const { status, message } = describe(error);
  const reason =
    status === 404
      ? "not_found"
      : status === 403
        ? "forbidden"
        : status === 409
          ? "conflict"
          : status === 422 || status === 400
            ? "invalid"
            : "unavailable";
  return new WorkbenchError({ reason, message });
};

const call = <A, E>(program: (api: KittumApi.KittumApi) => Effect.Effect<A, E>) =>
  client.pipe(
    Effect.flatMap(program),
    Effect.provide(FetchHttpClient.layer),
    Effect.mapError(toError),
  );

/** Null instead of a 404, for reads of something that may not exist yet. */
const orNull = <A>(effect: Effect.Effect<A, WorkbenchError>) =>
  effect.pipe(
    Effect.catchIf(
      (e) => e.reason === "not_found",
      () => Effect.succeed(null),
    ),
  );

const reviewer = (as: string) => ({ "X-Kittum-Reviewer": as });

const commandId = () => crypto.randomUUID();

const decisionBody = (d: Decision) => ({
  outcome: d.outcome,
  reason_criterion_id: d.reason_criterion_id,
  evidence_anchor_ids: d.evidence_anchor_ids,
  comment: d.comment,
  criteria: d.criteria ?? [],
});

const timingBody = (t: DecisionTiming) => t;

// The protocol, as the screens read it.

type Localized = { readonly [language: string]: string };

const LABELS: ReadonlyArray<readonly [keyof ProfileLabels, string]> = [
  ["scope", "question"],
  ["population", "slot.population"],
  ["intervention", "slot.intervention"],
  ["comparator", "slot.comparator"],
  ["outcome", "slot.outcome"],
  ["study_type", "slot.study_type"],
  ["other", "slot.other"],
  ["report", "report"],
  ["study", "study"],
  ["search_topic", "search_topic"],
];

const toProtocol = (published: KittumApi.PublishedProtocol): ProtocolVersion => {
  const protocol = published.stored.version.protocol;
  const lang = protocol.default_language;
  const text = (t: Localized | null | undefined) =>
    t ? (t[lang] ?? t["en"] ?? Object.values(t)[0] ?? "") : "";
  const labels = Object.fromEntries(
    LABELS.map(([key, server]) => [key, published.labels[server] ?? key]),
  ) as unknown as ProfileLabels;
  return {
    version_id: published.stored.id,
    content_hash: published.stored.version.content_hash,
    published_at: published.stored.published_at ?? null,
    profile: { id: protocol.profile ?? null, labels },
    protocol: {
      title: text(protocol.title),
      version_label: protocol.version_label,
      questions: protocol.questions.map((q) => ({
        id: q.id,
        label: text(q.label),
        population: text(q.population),
        intervention: text(q.intervention),
        comparators: (q.comparators ?? []).map((c) => text(c.text)),
      })),
      outcomes: protocol.outcomes.map((o) => ({
        id: o.id,
        category: o.category,
        name: text(o.name),
      })),
      topics: protocol.search_topics.map((topic) => {
        const scopes = topic.scopes.map((s) => ({ id: s.id, label: s.label }));
        return {
          id: topic.id,
          label: text(topic.label),
          kind: topic.kind,
          mode: topic.mode,
          mode_rationale: topic.mode_rationale ? text(topic.mode_rationale) : null,
          scopes,
          sentinel_percent: topic.audit?.sentinel_percent ?? "0",
          random_audit_percent: topic.audit?.random_audit_percent ?? "0",
          sources: topic.source_plan.sources.map((s) => ({ id: s.id, kind: s.kind, name: s.name })),
          criteria: topic.criteria.map((c): Criterion => ({
            id: c.id,
            slot: c.slot,
            polarity: c.polarity ?? "include",
            text_en: c.text["en"] ?? text(c.text),
            text_de: c.text["de"] ?? c.text["en"] ?? text(c.text),
            decidable_at: c.decidable_at,
            // A criterion for the whole topic applies to each of its scopes.
            applies_to: c.applies_to.flatMap((a) =>
              a === topic.id ? scopes.map((s) => s.id) : [a],
            ),
            keywords: c.keywords ?? [],
            positive_examples: c.positive_examples ?? [],
            negative_examples: c.negative_examples ?? [],
          })),
        };
      }),
    },
  };
};

export const makeApiWorkbench = (): Workbench => ({
  live: true,

  projects: () => call((api) => api.listProjects(undefined)),

  protocol: (projectId) =>
    orNull(call((api) => api.getPublishedProtocol(projectId, undefined))).pipe(
      Effect.map((p) => (p ? toProtocol(p) : null)),
    ),

  queue: ({ projectId, as, scopeId, stage, after, limit }) =>
    call((api) =>
      api.getScreeningQueue(projectId, {
        params: {
          scope_id: scopeId,
          stage,
          limit,
          ...(after ? { after } : {}),
          ...reviewer(as),
        },
      }),
    ),

  decide: ({ projectId, as, assignmentId, decision, phase, expectedEtag, timing }) =>
    call((api) =>
      api.decideScreeningAssignment(projectId, assignmentId, {
        params: reviewer(as),
        payload: {
          command_id: commandId(),
          decision: decisionBody(decision),
          phase,
          expected_etag: expectedEtag,
          timing: timingBody(timing),
        },
      }),
    ),

  undo: ({ projectId, as, assignmentId, expectedEtag }) =>
    call((api) =>
      api.undoScreeningDecision(projectId, assignmentId, {
        params: reviewer(as),
        payload: { command_id: commandId(), expected_etag: expectedEtag },
      }),
    ),

  status: ({ projectId, as, scopeId, stage }) =>
    call((api) =>
      api.getScreeningStatus(projectId, {
        params: { scope_id: scopeId, stage, ...reviewer(as) },
      }),
    ),

  fullText: ({ projectId, as, scopeId }) =>
    call((api) =>
      Effect.gen(function* () {
        const [item] = yield* api.getScreeningQueue(projectId, {
          params: { scope_id: scopeId, stage: "full_text", limit: 1, ...reviewer(as) },
        });
        if (!item) return null;
        const source = yield* api.getScreeningAssignmentFullText(projectId, item.assignment_id, {
          params: reviewer(as),
        });
        const view = yield* api.getDocument(source.document_id, undefined);
        const facts = [item.record.journal, item.record.year].filter(Boolean).join(" ");
        return {
          assignment_id: item.assignment_id,
          scope_id: item.scope_id,
          etag: item.etag,
          phase: item.phase,
          reveal: item.reveal,
          report: {
            id: item.assignment_id,
            study_label: item.record.registry_ids[0] ?? item.record.doi ?? "Record",
            title: item.title,
            citation: facts || "No journal recorded",
            licence: source.licence ?? "Licence not recorded",
          },
          document: {
            id: view.document.id,
            artifact_hash: view.document.artifact_hash,
            media_type: view.document.media_type,
            page_count: view.document.page_count ?? null,
          },
          // A full-text item's text is its layer's text, so offsets agree with the words.
          layer: {
            id: source.layer_id,
            document_id: source.document_id,
            text: item.text,
            pages: view.pages.map((p) => ({ page: p.number, width: p.width, height: p.height })),
          },
          mentions: source.mentions.map(
            (m) =>
              [
                m.criterion_id,
                m.mentions.map((a) => ({
                  id: a.id,
                  layer_id: a.layer_id,
                  quote: {
                    exact: a.quote.exact,
                    prefix: a.quote.prefix ?? "",
                    suffix: a.quote.suffix ?? "",
                  },
                  start: a.start,
                  end: a.end,
                  boxes: a.boxes,
                })),
              ] as const,
          ),
        };
      }),
    ),

  page: (layer, page) =>
    call((api) => api.getPageWords(layer.id, String(page), undefined)).pipe(
      Effect.map((words) => ({
        page,
        width: words.width,
        height: words.height,
        image_url: `/api/documents/${layer.document_id}/pages/${page}/image?scale=2`,
        words: words.words.map((w) => ({
          start: w.start,
          end: w.end,
          box: { page, x0: w.x0, y0: w.y0, x1: w.x1, y1: w.y1 },
        })),
      })),
    ),

  adopt: ({ layerId, quote, start }) =>
    call((api) => api.createAnchor(layerId, { payload: { quote, start } })).pipe(
      Effect.map((a) => ({
        id: a.id,
        layer_id: a.layer_id,
        quote: { exact: a.quote.exact, prefix: a.quote.prefix ?? "", suffix: a.quote.suffix ?? "" },
        start: a.start,
        end: a.end,
        boxes: a.boxes,
      })),
    ),

  adjudications: ({ projectId, as, scopeId, stage }) =>
    call((api) =>
      api.listScreeningAdjudications(projectId, {
        params: { scope_id: scopeId, stage, ...reviewer(as) },
      }),
    ),

  adjudicate: ({ projectId, as, conflictId, expectedEtag, adopted, decision, timing }) =>
    call((api) =>
      api.decideScreeningAdjudication(projectId, conflictId, {
        params: reviewer(as),
        payload: {
          command_id: commandId(),
          expected_etag: expectedEtag,
          adopted,
          decision: decision ? decisionBody(decision) : null,
          timing,
        },
      }),
    ),

  progress: ({ projectId, as }) =>
    call((api) => api.getProjectProgress(projectId, { params: reviewer(as) })),

  openStage: ({ projectId, as, scopeId, stage }) =>
    call((api) =>
      api.openScreeningStage(projectId, {
        payload: { scope_id: scopeId, stage, actor_id: as },
      }),
    ),

  ledger: (projectId) => call((api) => api.verifyProjectLedger(projectId, undefined)),

  searches: ({ projectId, as }) =>
    call((api) => api.listSearchExecutions(projectId, { params: reviewer(as) })),

  corpus: ({ projectId, as }) =>
    orNull(call((api) => api.getLatestCorpusRevision(projectId, { params: reviewer(as) }))),

  proposals: ({ projectId, as }) =>
    call((api) => api.listCorpusProposals(projectId, { params: reviewer(as) })),

  link: (request) =>
    call((api): Effect.Effect<KittumApi.IdentityRevision, unknown> => {
      const base = { revision_id: request.revisionId, actor_id: request.as };
      switch (request.action) {
        case "confirm":
          return api.confirmCorpusProposal(request.projectId, {
            payload: { ...base, proposal_id: request.proposalId },
          });
        case "merge":
          return api.mergeCorpusOccurrences(request.projectId, {
            payload: { ...base, occurrence_ids: request.occurrenceIds, relation: request.relation },
          });
        case "split":
          return api.splitCorpusOccurrences(request.projectId, {
            payload: { ...base, occurrence_ids: request.occurrenceIds, level: request.level },
          });
      }
    }),

  renderReport: ({ projectId, as, target }) =>
    call((api) => api.renderReportTarget(projectId, target, { payload: { actor_id: as } })),

  reportFile: ({ projectId, as, sha256 }) =>
    call((api) => api.downloadReportArtifact(projectId, sha256, { params: reviewer(as) })),

  sealBundle: ({ projectId, as }) =>
    call((api) => api.sealReportBundle(projectId, { payload: { actor_id: as } })),

  // The server serves extraction (WP-11), but this screen is not wired to it yet: it shows what
  // it will do.
  extraction: () => Effect.succeed(null),
  accept: () =>
    Effect.fail(
      new WorkbenchError({ reason: "unavailable", message: "Extraction is not served yet." }),
    ),
});

/** The server's health, for the start page. */
export const health = () => Effect.runPromise(call((api) => api.getHealth(undefined)));
