// What is behind a hash on the Audit screen. A ledger event names its payload, the event before
// it, and artifacts (a PDF, a raw search response, a model call's request and response) by
// SHA-256. Each hash here opens to the content it covers, with Kittum's check that the content
// still hashes to the recorded value: the server recomputes payload and event hashes, and reads
// each artifact back from the store and hashes it again.
import { useRouteContext } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import type { ArtifactCheck, CallPart, ModelCall, TracedEvent } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";

export type Caller = { readonly projectId: string; readonly as: string };

/** A hash's first digits in groups of four, so two hashes can be compared at a glance. */
export const short = (hash: string, digits = 16) =>
  hash
    .slice(0, digits)
    .replace(/(.{4})/g, "$1 ")
    .trim();

const bytesLabel = (n: number) => `${n.toLocaleString("en-GB")} bytes`;

/** A line saying whether recomputing gave the recorded hash. */
function Check({ matches, children }: { matches: boolean; children: ReactNode }) {
  return (
    <p
      className={`mt-1.5 flex items-baseline gap-2 text-sm ${matches ? "text-include" : "font-medium text-exclude"}`}
      data-testid="hash-check"
      data-matches={matches}
    >
      <svg
        width="12"
        height="12"
        viewBox="0 0 12 12"
        aria-hidden="true"
        className="flex-none translate-y-px"
      >
        <path
          d={matches ? "M2 6.5l2.6 2.6L10 3.2" : "M3 3l6 6M9 3l-6 6"}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span>{children}</span>
    </p>
  );
}

function Block({ children, label }: { children: string; label?: string }) {
  return (
    <pre
      aria-label={label}
      className="mt-1 max-h-96 overflow-auto rounded-[3px] bg-sheet-2 p-2.5 text-xs leading-relaxed whitespace-pre-wrap"
    >
      {children}
    </pre>
  );
}

const pretty = (text: string) => {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
};

/** The hashes of one event, each opening to what it covers. */
export function EventHashes({ event: e }: { event: TracedEvent }) {
  return (
    <section className="mt-2" aria-label={`Hashes of event ${e.seq}`} data-testid="event-hashes">
      <HashRow label="Payload hash" hash={e.payload_hash}>
        <p className="text-sm text-ink-2">
          The payload is what the event records, as Kittum stored it.
        </p>
        <Block label="Payload">{JSON.stringify(e.payload, null, 2)}</Block>
        <Check matches={e.payload_hash_matches}>
          {e.payload_hash_matches
            ? "Kittum hashed this payload again and got the recorded hash."
            : "Kittum hashed this payload again and got a different hash. The payload changed after the event was recorded."}
        </Check>
      </HashRow>
      <HashRow label="Event hash" hash={e.event_hash}>
        <p className="text-sm text-ink-2">
          The event hash covers these fields, among them the payload hash and the hash of the event
          before.
        </p>
        <Block label="Fields the event hash covers">
          {JSON.stringify(e.hashed_fields, null, 2)}
        </Block>
        <Check matches={e.event_hash_matches}>
          {e.event_hash_matches
            ? "Kittum hashed these fields again and got the recorded hash."
            : "Kittum hashed these fields again and got a different hash. The event changed after it was recorded."}
        </Check>
      </HashRow>
      <HashRow label="Previous hash" hash={e.prev_hash}>
        <p className="text-sm text-ink-2">
          {e.seq === 1
            ? "The first event links to a hash of zeros."
            : `The event hash of event ${e.seq - 1}. The integrity check compares the two for every event.`}
        </p>
      </HashRow>
      <dl className="narrow mt-1 grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 text-xs text-ink-3 tabular-nums">
        <dt>Event ID</dt>
        <dd className="break-all">{e.event_id}</dd>
        <dt>Type</dt>
        <dd>{e.type}</dd>
      </dl>
    </section>
  );
}

function HashRow({ label, hash, children }: { label: string; hash: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-rule py-1.5 first:border-t-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="grid w-full grid-cols-[7rem_minmax(0,1fr)] gap-x-4 text-left text-xs tabular-nums"
      >
        <span className="text-ink-2">{label}</span>
        <span className="break-all text-petrol underline decoration-petrol/40 underline-offset-2">
          {hash}
        </span>
      </button>
      {open ? <div className="mt-1.5 ml-[8rem]">{children}</div> : null}
    </div>
  );
}

/** Fetches an artifact's bytes and the server's check of them, once opened. */
function useArtifact(caller: Caller, sha256: string, open: boolean) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const [bytes, setBytes] = useState<Uint8Array | null>(null);
  const [check, setCheck] = useState<ArtifactCheck | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open || bytes || error) return;
    let live = true;
    Promise.all([
      run(workbench.auditArtifact({ ...caller, sha256 })),
      run(workbench.checkArtifact({ ...caller, sha256 })),
    ]).then(
      ([data, checked]) => {
        if (!live) return;
        setBytes(data);
        setCheck(checked);
      },
      (e: unknown) =>
        live &&
        setError(e instanceof WorkbenchError ? e.message : "Kittum could not read this artifact."),
    );
    return () => {
      live = false;
    };
  }, [workbench, caller, sha256, open, bytes, error]);
  const text = bytes ? new TextDecoder().decode(bytes) : null;
  return { bytes, text, check, error };
}

function ArtifactCheckLine({ check }: { check: ArtifactCheck }) {
  return (
    <Check matches={check.matches}>
      {check.matches
        ? `Kittum read ${bytesLabel(check.bytes ?? 0)} from the artifact store and hashed them again. The hash matches the recorded one.`
        : `Kittum read ${bytesLabel(check.bytes ?? 0)} from the artifact store. They now hash to ${short(check.recomputed ?? "", 16)}, not to the recorded hash. The artifact changed after it was recorded.`}
    </Check>
  );
}

/** An artifact an event names, opening in place to its content and the check of its hash. */
export function ArtifactToggle({
  caller,
  sha256,
  label,
  stored = true,
}: {
  caller: Caller;
  sha256: string;
  label: string;
  stored?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!stored) {
    return (
      <span className="text-xs text-ink-3" title={sha256}>
        {label} {short(sha256, 8)}. This store does not have it, so Kittum cannot show or check it.
      </span>
    );
  }
  return (
    <div className="w-full" data-testid="artifact">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={sha256}
        className="text-xs text-petrol underline decoration-petrol/40 underline-offset-2 hover:decoration-petrol"
      >
        {label} {short(sha256, 8)}
      </button>
      {open ? <ArtifactContent caller={caller} sha256={sha256} /> : null}
    </div>
  );
}

function ArtifactContent({ caller, sha256 }: { caller: Caller; sha256: string }) {
  const { bytes, text, check, error } = useArtifact(caller, sha256, true);
  if (error) return <p className="mt-1 text-sm text-exclude">{error}</p>;
  if (!bytes || !check) return <p className="mt-1 text-sm text-ink-2">Reading the artifact.</p>;
  const pdf = check.media_type === "application/pdf";
  const openPdf = () => {
    const url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: "application/pdf" }));
    window.open(url, "_blank");
  };
  return (
    <div className="mt-1.5 border-l-2 border-rule-strong pl-3" data-testid="artifact-content">
      <p className="text-xs break-all text-ink-3 tabular-nums">SHA-256 {sha256}</p>
      {pdf ? (
        <button
          type="button"
          onClick={openPdf}
          className="mt-1 text-sm text-petrol underline decoration-petrol/40 underline-offset-2"
        >
          Open the PDF in a new tab
        </button>
      ) : (
        <Block label="Artifact content">{pretty(text ?? "")}</Block>
      )}
      <ArtifactCheckLine check={check} />
    </div>
  );
}

// Model calls.

const ROLES: Record<string, string> = {
  system: "System prompt",
  user: "User prompt",
  assistant: "The model's earlier answer, sent back",
};

const PROMPT_KEYS = new Set(["messages", "response_format", "json_schema", "model", "model_id"]);

interface Message {
  readonly role: string;
  readonly content: string;
}

/** A request body, or Kittum's built request, read for display. */
const readRequest = (text: string) => {
  try {
    const body = JSON.parse(text) as Record<string, unknown>;
    const messages = Array.isArray(body["messages"]) ? (body["messages"] as Message[]) : [];
    const format = body["response_format"] as { json_schema?: { schema?: unknown } } | undefined;
    const schema = format?.json_schema?.schema ?? body["json_schema"] ?? null;
    const settings = Object.entries(body).filter(([k, v]) => !PROMPT_KEYS.has(k) && v !== null);
    const model = (body["model"] ?? body["model_id"] ?? null) as string | null;
    return { messages, schema, settings, model };
  } catch {
    return null;
  }
};

interface ChatMessage {
  content?: string | null;
  reasoning?: string | null;
  reasoning_content?: string | null;
}

interface ChatCompletion {
  choices?: Array<{ message?: ChatMessage; delta?: ChatMessage; index?: number }>;
  usage?: Record<string, unknown> | null;
}

/** A streamed answer (server-sent events) added up to one chat completion, as the server does in
 *  `kittum.models.stream`: each chunk's delta adds to the answer or the reasoning. */
const assembleStream = (text: string): ChatCompletion => {
  let content = "";
  let reasoning = "";
  let usage: Record<string, unknown> | null = null;
  for (const event of text.split(/\r\n\r\n|\n\n|\r\r/)) {
    const data = event
      .split(/\r\n|\r|\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) continue;
    if (data.trim() === "[DONE]") break;
    const chunk = JSON.parse(data) as ChatCompletion;
    usage = chunk.usage ?? usage;
    for (const choice of chunk.choices ?? []) {
      if ((choice.index ?? 0) !== 0) continue;
      content += choice.delta?.content ?? "";
      reasoning += choice.delta?.reasoning_content ?? choice.delta?.reasoning ?? "";
    }
  }
  return { choices: [{ message: { content, reasoning: reasoning || null } }], usage };
};

/** A chat completion response, streamed or in one body, read for display; null for a bare
 *  answer text. */
const readResponse = (text: string) => {
  try {
    const body: ChatCompletion = /^\s*(data|event|id|retry)?:/.test(text)
      ? assembleStream(text)
      : (JSON.parse(text) as ChatCompletion);
    const choice = body.choices?.[0];
    if (!choice) return null;
    let answer = choice.message?.content ?? "";
    let reasoning = choice.message?.reasoning ?? choice.message?.reasoning_content ?? null;
    const think = /^\s*<think>([\s\S]*?)<\/think>\s*/.exec(answer);
    if (think && !reasoning) {
      reasoning = think[1]?.trim() ?? null;
      answer = answer.slice(think[0].length);
    }
    return { answer, reasoning, usage: body.usage ?? null };
  } catch {
    return null;
  }
};

/** The model calls behind an AI step, each opening to its request and response. */
export function ModelCalls({ calls, caller }: { calls: ReadonlyArray<ModelCall>; caller: Caller }) {
  return (
    <section className="mt-2" aria-label="Model calls" data-testid="model-calls">
      <h4 className="text-xs font-semibold text-ink-2">
        {calls.length === 1 ? "The model call" : `${calls.length} model calls`}
      </h4>
      <ol className="mt-1 grid gap-1">
        {calls.map((call) => (
          <CallRow key={call.number} call={call} caller={caller} only={calls.length === 1} />
        ))}
      </ol>
    </section>
  );
}

function CallRow({ call, caller, only }: { call: ModelCall; caller: Caller; only: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <li data-testid="model-call" data-number={call.number}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="text-sm text-petrol underline decoration-petrol/40 underline-offset-2"
      >
        {only ? "Show the request and the response" : `Call ${call.number}: request and response`}
      </button>
      <span className="ml-3 text-xs text-ink-3">
        {[call.model_identity, call.finish_reason ? `finish reason ${call.finish_reason}` : null]
          .filter(Boolean)
          .join(", ")}
      </span>
      {call.problem ? (
        <p className="mt-0.5 text-xs text-exclude">Kittum sent this back: {call.problem}</p>
      ) : null}
      {open ? (
        <div className="mt-2 grid gap-4 border-l-2 border-petrol pl-4">
          <RequestView part={call.request} caller={caller} />
          <ResponseView part={call.response} caller={caller} />
        </div>
      ) : null}
    </li>
  );
}

function Missing({ what }: { what: string }) {
  return <p className="text-sm text-ink-2">{what}</p>;
}

function RequestView({ part, caller }: { part: CallPart | null; caller: Caller }) {
  if (!part) return <Missing what="The backend recorded no request for this call." />;
  if (!part.stored)
    return <Missing what="This store does not have the request, so Kittum cannot show it." />;
  return <StoredRequest part={part} caller={caller} />;
}

function StoredRequest({ part, caller }: { part: CallPart; caller: Caller }) {
  const { text, check, error } = useArtifact(caller, part.sha256, true);
  if (error) return <p className="text-sm text-exclude">{error}</p>;
  if (text === null || !check) return <p className="text-sm text-ink-2">Reading the request.</p>;
  const request = readRequest(text);
  return (
    <section aria-label="Request" data-testid="call-request">
      <h5 className="text-sm font-semibold">
        {part.form === "http_body"
          ? "The request body Kittum sent to the model server"
          : "The request as Kittum built it. The backend stored no request body."}
      </h5>
      {request ? (
        <>
          {request.model ? <p className="text-xs text-ink-2">Model {request.model}</p> : null}
          {request.messages.map((m, i) => (
            <div key={i} className="mt-2">
              <p className="text-xs font-medium text-ink-2">{ROLES[m.role] ?? m.role}</p>
              <Block label={ROLES[m.role] ?? m.role}>{m.content}</Block>
            </div>
          ))}
          {request.settings.length > 0 ? (
            <dl
              className="mt-2 grid grid-cols-[minmax(8rem,auto)_minmax(0,1fr)] gap-x-4 text-xs"
              aria-label="Request settings"
            >
              {request.settings.map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-ink-2">{k}</dt>
                  <dd className="break-all tabular-nums">{JSON.stringify(v)}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {request.schema ? (
            <details className="mt-2">
              <summary className="cursor-default text-xs text-ink-2">
                The JSON schema the answer must follow
              </summary>
              <Block>{JSON.stringify(request.schema, null, 2)}</Block>
            </details>
          ) : null}
        </>
      ) : null}
      <details className="mt-2">
        <summary className="cursor-default text-xs text-ink-2">The bytes as stored</summary>
        <Block label="Request bytes">{text}</Block>
      </details>
      <p className="text-xs break-all text-ink-3 tabular-nums">SHA-256 {part.sha256}</p>
      <ArtifactCheckLine check={check} />
    </section>
  );
}

function ResponseView({ part, caller }: { part: CallPart | null; caller: Caller }) {
  if (!part) return <Missing what="The backend recorded no response for this call." />;
  if (!part.stored)
    return <Missing what="This store does not have the response, so Kittum cannot show it." />;
  return <StoredResponse part={part} caller={caller} />;
}

function StoredResponse({ part, caller }: { part: CallPart; caller: Caller }) {
  const { text, check, error } = useArtifact(caller, part.sha256, true);
  if (error) return <p className="text-sm text-exclude">{error}</p>;
  if (text === null || !check) return <p className="text-sm text-ink-2">Reading the response.</p>;
  const response = part.form === "http_response" ? readResponse(text) : null;
  return (
    <section aria-label="Response" data-testid="call-response">
      <h5 className="text-sm font-semibold">
        {part.form === "http_response"
          ? "The response the model server sent"
          : "The answer text. The backend kept no full response."}
      </h5>
      {response?.reasoning ? (
        <div className="mt-2">
          <p className="text-xs font-medium text-ink-2">The model&rsquo;s reasoning</p>
          <Block label="Reasoning">{response.reasoning}</Block>
        </div>
      ) : null}
      <div className="mt-2">
        <p className="text-xs font-medium text-ink-2">The answer</p>
        <Block label="Answer">{pretty(response ? response.answer : text)}</Block>
      </div>
      {response?.usage ? (
        <p className="mt-1 text-xs text-ink-3 tabular-nums">
          {Object.entries(response.usage)
            .filter(([, v]) => typeof v === "number")
            .map(([k, v]) => `${k.replaceAll("_", " ")} ${String(v)}`)
            .join(", ")}
        </p>
      ) : null}
      <details className="mt-2">
        <summary className="cursor-default text-xs text-ink-2">The bytes as stored</summary>
        <Block label="Response bytes">{text}</Block>
      </details>
      <p className="text-xs break-all text-ink-3 tabular-nums">SHA-256 {part.sha256}</p>
      <ArtifactCheckLine check={check} />
    </section>
  );
}
