// Writing a protocol version (DESIGN.md, "Protocol" and "Reviewer workbench"): YAML or JSON as
// `kittum project publish-protocol` takes it, from an example, the published version, or a file.
// The server reads and checks it; the problems sit beside the text, and before publishing the lead
// sees what the version changes against the one every decision reads now.
import { useRouteContext, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import type {
  DiffPreview,
  ExampleProtocol,
  ProtocolIssue,
  ProtocolRevision,
  ProtocolSource,
} from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { Button, Field, FileInput, Select, TextInput, failure } from "../../ui/Form.tsx";
import { Key } from "../../ui/Key.tsx";
import { useProject } from "../project.tsx";

type Check =
  | { readonly state: "unchecked" }
  | { readonly state: "checking" }
  | {
      readonly state: "invalid";
      readonly text: string;
      readonly issues: ReadonlyArray<ProtocolIssue>;
    }
  | {
      readonly state: "valid";
      readonly text: string;
      readonly source: ProtocolSource;
      readonly preview: DiffPreview;
      /** What may be wrong though it compiles, such as a field tag PubMed does not list. */
      readonly warnings: ReadonlyArray<ProtocolIssue>;
    };

const show = (value: unknown): string =>
  value === null || value === undefined
    ? "Nothing"
    : typeof value === "string"
      ? value
      : JSON.stringify(value);

export function ProtocolEditor({
  onPublished,
  onCancel,
  initialText = null,
  base = null,
  onRevisionSaved,
}: {
  onPublished: (message: string) => void;
  onCancel: (() => void) | null;
  /** The text to start from, such as an assistant revision's. */
  initialText?: string | null;
  /** The assistant revision the text comes from; the edit can be saved as the next revision. */
  base?: ProtocolRevision | null;
  onRevisionSaved?: (revision: ProtocolRevision) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const router = useRouter();
  const { project, me, protocol } = useProject();
  const [examples, setExamples] = useState<ReadonlyArray<ExampleProtocol>>([]);
  const [example, setExample] = useState("");
  const [text, setText] = useState(initialText ?? "");
  const [check, setCheck] = useState<Check>({ state: "unchecked" });
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editor = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let live = true;
    run(workbench.protocolExamples()).then(
      (found) => {
        if (!live) return;
        setExamples(found);
        setExample((e) => e || (found[0]?.id ?? ""));
      },
      () => live && setExamples([]),
    );
    // A new version starts from the published one.
    if (protocol && initialText === null) {
      run(workbench.protocolSource(project.id)).then(
        (source) => live && setText((t) => t || source),
        () => undefined,
      );
    }
    return () => {
      live = false;
    };
  }, [workbench, project.id, protocol, initialText]);

  const load = (source: string) => {
    setText(source);
    setCheck({ state: "unchecked" });
    setError(null);
    editor.current?.focus();
  };

  const verify = async () => {
    const current = text;
    setCheck({ state: "checking" });
    setError(null);
    try {
      const parsed = await run(workbench.parseProtocol(current));
      if (!parsed.source) {
        setCheck({ state: "invalid", text: current, issues: parsed.issues ?? [] });
        return;
      }
      const report = await run(workbench.validateProtocol(parsed.source));
      if (!report.ok) {
        setCheck({ state: "invalid", text: current, issues: report.issues ?? [] });
        return;
      }
      const preview = await run(
        workbench.previewProtocol({
          projectId: project.id,
          as: me.actor_id,
          source: parsed.source,
        }),
      );
      setCheck({
        state: "valid",
        text: current,
        source: parsed.source,
        preview,
        warnings: report.warnings ?? [],
      });
    } catch (e) {
      setCheck({ state: "unchecked" });
      setError(failure(e, "The protocol could not be checked."));
    }
  };

  const fresh = check.state === "valid" && check.text === text;
  const unchanged =
    fresh && check.preview.diff !== null && check.preview.diff.changes?.length === 0;
  const modeChanged = fresh && (check.preview.diff?.mode_changes.length ?? 0) > 0;

  const saveRevision = async () => {
    if (check.state !== "valid" || !fresh || !base) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await run(
        workbench.saveRevision({
          projectId: project.id,
          as: me.actor_id,
          source: check.source,
          baseId: base.id,
        }),
      );
      onRevisionSaved?.(saved);
    } catch (e) {
      setError(failure(e, "The revision was not saved."));
    } finally {
      setBusy(false);
    }
  };

  const publish = async () => {
    if (check.state !== "valid" || !fresh) return;
    if (modeChanged && !reason.trim()) {
      setError("A change of screening mode needs a reason; the methods text reports it.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const stored = await run(
        workbench.publishProtocol({
          projectId: project.id,
          as: me.actor_id,
          source: check.source,
          reason: reason.trim() || null,
        }),
      );
      await router.invalidate();
      onPublished(
        `Published version ${stored.version.protocol.version_label}. Your approval is in the ledger.`,
      );
    } catch (e) {
      setError(failure(e, "The protocol was not published."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid h-full grid-cols-[minmax(0,1fr)_minmax(380px,34%)]">
      <section
        className="flex min-h-0 flex-col border-r border-rule-strong bg-sheet"
        aria-label="Protocol source"
      >
        <header className="flex flex-wrap items-end gap-3 border-b border-rule px-5 pt-4 pb-3">
          <div className="mr-auto">
            <h1 className="text-lg font-semibold">
              {protocol
                ? `A new version after ${protocol.protocol.version_label}`
                : "Write the protocol"}
            </h1>
            <p className="text-xs text-ink-2">
              YAML or JSON, as <code>kittum project publish-protocol</code> reads it.
            </p>
          </div>
          {examples.length ? (
            <div className="flex items-end gap-2">
              <Field label="Start from an example">
                <Select
                  value={example}
                  onChange={(e) => setExample(e.currentTarget.value)}
                  className="w-[260px]"
                >
                  {examples.map((x) => (
                    <option key={x.id} value={x.id}>
                      {x.title}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button onClick={() => load(examples.find((x) => x.id === example)?.text ?? "")}>
                Load
              </Button>
            </div>
          ) : null}
          <div className="w-[220px]">
            <Field label="Or open a file">
              <FileInput
                label="Protocol file"
                accept=".yaml,.yml,.json"
                onFile={(file) => void file?.text().then(load)}
              />
            </Field>
          </div>
        </header>
        <textarea
          ref={editor}
          aria-label="Protocol"
          spellCheck={false}
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void verify();
            }
          }}
          placeholder="Load an example, open a file, or write the protocol here."
          className="min-h-0 flex-1 resize-none bg-sheet px-5 py-4 font-mono text-[13px] leading-[20px] text-ink outline-none placeholder:text-ink-3"
        />
        <footer className="flex items-center gap-4 border-t border-rule-strong bg-sheet-2 px-5 py-2.5 text-sm">
          <Button
            primary
            onClick={() => void verify()}
            disabled={!text.trim() || check.state === "checking"}
          >
            {check.state === "checking" ? "Checking" : "Check"}
            <span className="inline-flex gap-0.5">
              <Key k="Ctrl" />
              <Key k="Enter" />
            </span>
          </Button>
          {onCancel ? (
            <button type="button" onClick={onCancel} className="text-sm text-ink-2 hover:text-ink">
              Back to the published version
            </button>
          ) : null}
        </footer>
      </section>
      <aside className="min-h-0 overflow-y-auto bg-paper px-6 py-5" aria-label="Check and publish">
        <Result
          check={check}
          stale={check.state !== "unchecked" && check.state !== "checking" && check.text !== text}
        />
        {fresh && !unchanged ? (
          <form
            className="mt-6 grid gap-3 border-t border-rule-strong pt-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (!busy) void publish();
            }}
          >
            <Field
              label={modeChanged ? "Reason (needed: a screening mode changes)" : "Reason"}
              hint="Why this version, in a sentence. The ledger keeps it with your approval."
            >
              <TextInput value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
            </Field>
            <div className="flex flex-wrap gap-3">
              <Button primary type="submit" disabled={busy}>
                {busy ? "Publishing" : "Publish this version"}
              </Button>
              {base ? (
                <Button onClick={() => void saveRevision()} disabled={busy}>
                  Save as a revision after {base.seq}
                </Button>
              ) : null}
            </div>
          </form>
        ) : null}
        {error ? (
          <p role="alert" className="mt-4 text-sm text-exclude" data-testid="form-error">
            {error}
          </p>
        ) : null}
      </aside>
    </div>
  );
}

function Result({ check, stale }: { check: Check; stale: boolean }) {
  if (check.state === "unchecked" || check.state === "checking") {
    return (
      <p className="max-w-[46ch] text-sm text-ink-2">
        Check the protocol before you publish it. Kittum compiles every criterion into the question
        reviewers and the AI answer, and lists every problem it finds.
      </p>
    );
  }
  return (
    <div data-testid="protocol-check">
      {stale ? (
        <p className="mb-3 text-sm text-unclear">
          The text changed since this check. Check it again.
        </p>
      ) : null}
      {check.state === "invalid" ? (
        <>
          <h2 className="text-md font-semibold text-exclude">
            {check.issues.length} {check.issues.length === 1 ? "problem" : "problems"} to fix
          </h2>
          <ol className="mt-3 grid gap-2.5">
            {check.issues.map((issue, i) => (
              <li
                key={i}
                className="border-l-2 border-exclude pl-3 text-sm"
                data-testid="protocol-issue"
              >
                {issue.path ? (
                  <span className="block text-xs font-semibold text-ink-2">{issue.path}</span>
                ) : null}
                {issue.message}
              </li>
            ))}
          </ol>
        </>
      ) : (
        <>
          <Changes preview={check.preview} />
          {check.warnings.length ? (
            <ol className="mt-4 grid gap-2.5" aria-label="Warnings">
              {check.warnings.map((warning, i) => (
                <li
                  key={i}
                  className="border-l-2 border-unclear pl-3 text-sm"
                  data-testid="protocol-warning"
                >
                  <span className="block text-xs font-semibold text-ink-2">{warning.path}</span>
                  {warning.message}
                </li>
              ))}
            </ol>
          ) : null}
        </>
      )}
    </div>
  );
}

function Changes({ preview }: { preview: DiffPreview }) {
  const { diff, previous } = preview;
  return (
    <>
      <h2 className="text-md font-semibold text-include">It compiles</h2>
      <p className="mt-1 text-xs text-ink-3" title={preview.content_hash}>
        Content hash {preview.content_hash.slice(0, 16)}
      </p>
      <h3 className="mt-5 text-sm font-semibold">
        {previous ? `Against version ${previous.version_label}` : "The first version"}
      </h3>
      {!diff || !previous ? (
        <p className="mt-1 text-sm text-ink-2">
          Nothing is published yet, so there is nothing to compare it with.
        </p>
      ) : (diff.changes?.length ?? 0) === 0 ? (
        <p className="mt-1 text-sm text-ink-2" data-testid="protocol-diff">
          Nothing changes: this is the published version. Edit it first.
        </p>
      ) : (
        <div className="mt-2 grid gap-3 text-sm" data-testid="protocol-diff">
          <ChangeList
            label="Screening modes"
            items={diff.mode_changes.map((m) => `${m.topic_id}: ${m.before} becomes ${m.after}`)}
          />
          <ChangeList label="Criteria added" items={diff.added_criteria} />
          <ChangeList label="Criteria removed" items={diff.removed_criteria} />
          <ChangeList label="Criteria changed" items={diff.changed_criteria} />
          <ChangeList label="Scopes added" items={diff.added_scopes ?? []} />
          <ChangeList label="Scopes removed" items={diff.removed_scopes ?? []} />
          <div>
            <h4 className="text-xs font-semibold text-ink-2">Every change</h4>
            <ol className="mt-1 grid gap-2">
              {(diff.changes ?? []).map((c) => (
                <li
                  key={c.path}
                  className="border-l-2 border-rule-strong pl-3"
                  data-testid="protocol-change"
                >
                  <span className="block text-xs font-semibold break-words text-ink-2">
                    {c.path}
                  </span>
                  <span className="block text-ink-3 line-through decoration-rule-strong">
                    {show(c.before)}
                  </span>
                  <span className="block">{show(c.after)}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      )}
    </>
  );
}

function ChangeList({ label, items }: { label: string; items: ReadonlyArray<string> }) {
  if (items.length === 0) return null;
  return (
    <p>
      <span className="text-xs font-semibold text-ink-2">{label}: </span>
      {items.join(", ")}
    </p>
  );
}
