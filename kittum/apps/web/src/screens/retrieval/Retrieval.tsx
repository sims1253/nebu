// Full-text retrieval (DESIGN.md, "Full text"): the human end of the waterfall. The methods lead
// sees every report with its outcome at title and abstract, uploads the PDF of each one sought
// for retrieval, and adds the full-text sentinels that full-text queues get unmarked. The list
// is the lead's alone: it names every record, and a reviewer who saw it could tell sentinels.
import { LegendList } from "@legendapp/list/react";
import type { LegendListRef } from "@legendapp/list/react";
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { RetrievalItem } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { failure, Field, FileInput, FormDialog, Select, TextInput } from "../../ui/Form.tsx";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { LEAD_ONLY, plural, useProject } from "../project.tsx";

const OUTCOME: Record<string, { label: string; className: string }> = {
  include: { label: "Included", className: "text-include" },
  exclude: { label: "Excluded", className: "text-exclude" },
  conflict: { label: "In conflict", className: "text-unclear" },
};

/** Sought for retrieval: included at title and abstract in at least one scope. */
const sought = (item: RetrievalItem) =>
  Object.values(item.outcomes).some((stages) => stages["title_abstract"] === "include");

export function RetrievalScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, seesLeadData, labels } = useProject();
  const [items, setItems] = useState<ReadonlyArray<RetrievalItem> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const [selected, setSelected] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [addingSentinel, setAddingSentinel] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const list = useRef<LegendListRef>(null);

  const load = useCallback(
    () =>
      run(workbench.fullTexts({ projectId: project.id, as: me.actor_id })).then(setItems, (e) =>
        setError(failure(e, "The reports could not load.")),
      ),
    [workbench, project.id, me.actor_id],
  );
  useEffect(() => {
    if (seesLeadData && project.protocol) void load();
  }, [load, seesLeadData, project.protocol]);

  const screened = (items ?? []).some((i) => Object.keys(i.outcomes).length > 0);
  const shown = useMemo(
    () => (items ?? []).filter((i) => all || !screened || sought(i)),
    [items, all, screened],
  );
  const current = shown[Math.min(selected, shown.length - 1)];
  const retrieved = shown.filter((i) => i.full_text).length;

  useEffect(() => {
    if (shown.length)
      void list.current?.scrollIndexIntoView({ index: Math.min(selected, shown.length - 1) });
  }, [selected, shown.length]);

  useKeys((event) => {
    if (!items) return false;
    switch (event.key) {
      case "j":
      case "ArrowDown":
        setSelected((i) => Math.min(i + 1, shown.length - 1));
        return true;
      case "k":
      case "ArrowUp":
        setSelected((i) => Math.max(i - 1, 0));
        return true;
      case "u":
        if (!current) return false;
        if (current.full_text) {
          setNotice("This report has its full text already.");
          return true;
        }
        setUploading(true);
        return true;
      case "s":
        setAddingSentinel(true);
        return true;
      case "f":
        if (!screened) return false;
        setAll((a) => !a);
        setSelected(0);
        return true;
      default:
        return false;
    }
  });

  if (!seesLeadData) {
    return (
      <Shell>
        <h1 className="text-xl font-semibold tracking-[-0.01em]">Retrieval</h1>
        <p className="mt-4 max-w-[60ch] text-ink-2" data-testid="lead-only">
          {LEAD_ONLY}
        </p>
      </Shell>
    );
  }

  return (
    <div className="grid h-full grid-rows-[auto_minmax(0,1fr)_auto] bg-paper">
      <header className="mx-auto w-full max-w-[1180px] px-10 pt-8 pb-4">
        <div className="flex items-baseline gap-6">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">Retrieval</h1>
          <span className="text-sm text-ink-2" data-testid="retrieval-counts">
            {items
              ? `${retrieved} of ${shown.length} ${screened && !all ? "sought for retrieval" : plural(labels.report).toLowerCase()} retrieved`
              : ""}
          </span>
          <span className="ml-auto flex flex-wrap gap-x-5 text-sm">
            <KeyHint keys={["j", "k"]}>Move</KeyHint>
            <KeyHint keys={["u"]} muted={!current || Boolean(current.full_text)}>
              Upload its PDF
            </KeyHint>
            <KeyHint keys={["s"]}>Add a full-text sentinel</KeyHint>
            {screened ? (
              <KeyHint keys={["f"]}>{all ? "Only those sought" : "Every report"}</KeyHint>
            ) : null}
          </span>
        </div>
        <p className="mt-2 max-w-[80ch] text-sm text-ink-2">
          {!project.protocol
            ? "Publish the protocol and build the corpus first."
            : screened
              ? `${all ? "Every report of the corpus." : "The reports included at title and abstract in at least one scope."} Upload each one's PDF with its licence; full-text screening opens on those that have one.`
              : "No stage is screened yet, so every report of the corpus is listed. A full text attached now waits for its record to pass title and abstract."}
        </p>
        {error || notice ? (
          <p
            role={error ? "alert" : "status"}
            className={`mt-2 text-sm ${error ? "text-exclude" : ""}`}
            data-testid="retrieval-notice"
          >
            {error ?? notice}
          </p>
        ) : null}
      </header>
      <section
        aria-label="Reports"
        className="mx-auto min-h-0 w-full max-w-[1180px] border-t border-rule-strong px-10"
      >
        {items === null ? (
          <p className="pt-4 text-ink-3">{project.protocol ? "Loading the reports." : ""}</p>
        ) : shown.length === 0 ? (
          <p className="pt-4 text-ink-2">
            {screened
              ? "No report is included at title and abstract yet. Press f to see every report."
              : "The corpus has no reports yet."}
          </p>
        ) : (
          <LegendList
            ref={list}
            data={shown}
            extraData={current?.item_ref}
            keyExtractor={(item) => item.item_ref}
            estimatedItemSize={64}
            style={{ height: "100%" }}
            renderItem={({ item, index }) => (
              <Row
                item={item}
                selected={item.item_ref === current?.item_ref}
                onSelect={() => setSelected(index)}
              />
            )}
          />
        )}
      </section>
      <footer className="h-6" />
      {current && uploading ? (
        <UploadFullText
          item={current}
          onClose={() => setUploading(false)}
          onDone={(message) => {
            setUploading(false);
            setNotice(message);
            void load();
          }}
        />
      ) : null}
      <AddSentinel
        open={addingSentinel}
        onOpenChange={setAddingSentinel}
        onDone={(message) => {
          setAddingSentinel(false);
          setNotice(message);
        }}
      />
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[980px] px-10 pt-10 pb-16">{children}</div>
    </section>
  );
}

function Row({
  item,
  selected,
  onSelect,
}: {
  item: RetrievalItem;
  selected: boolean;
  onSelect: () => void;
}) {
  const { scopeLabel } = useProject();
  const facts = [item.journal, item.year].filter(Boolean).join(" ");
  const ids = [item.doi, ...item.registry_ids].filter(Boolean).join(", ");
  return (
    <article
      onClick={onSelect}
      aria-current={selected || undefined}
      data-testid="retrieval-row"
      className={`grid grid-cols-[minmax(0,1fr)_14rem_11rem] items-baseline gap-x-5 border-b border-rule py-2.5 pr-3 pl-4 ${
        selected ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : ""
      }`}
    >
      <div className="min-w-0">
        <h2 className="truncate font-medium" title={item.title}>
          {item.title}
        </h2>
        <p className="narrow truncate text-xs text-ink-3">
          {[facts, ids].filter(Boolean).join(", ") || "No journal or identifier recorded"}
        </p>
      </div>
      <p className="narrow text-xs">
        {Object.entries(item.outcomes).length === 0 ? (
          <span className="text-ink-3">Not screened yet</span>
        ) : (
          Object.entries(item.outcomes).map(([scope, stages]) => {
            const outcome = stages["title_abstract"];
            const shown = outcome ? OUTCOME[outcome] : null;
            return (
              <span key={scope} className="block">
                <span className="text-ink-3">{scopeLabel(scope)}: </span>
                <span className={shown?.className ?? "text-ink-2"}>
                  {shown?.label ?? "Being screened"}
                </span>
              </span>
            );
          })
        )}
      </p>
      <p className="narrow text-xs" data-testid="full-text-status">
        {item.full_text ? (
          <span className="text-include">
            PDF, {item.full_text.page_count ?? "?"} pages
            <span className="block text-ink-3">
              {item.full_text.licence ?? "Licence not recorded"}
            </span>
          </span>
        ) : (
          <span className="text-ink-3">Not retrieved</span>
        )}
      </p>
    </article>
  );
}

const readPdf = async (file: File) => new Uint8Array(await file.arrayBuffer());

function UploadFullText({
  item,
  onClose,
  onDone,
}: {
  item: RetrievalItem;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [file, setFile] = useState<File | null>(null);
  const [licence, setLicence] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (!file) {
      setError("Choose the PDF.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const attached = await run(
        workbench.attachFullText({
          projectId: project.id,
          as: me.actor_id,
          itemRef: item.item_ref,
          pdf: await readPdf(file),
          licence: licence.trim() || null,
        }),
      );
      onDone(`Attached a ${attached.page_count ?? "?"}-page full text to “${item.title}”.`);
    } catch (e) {
      setError(failure(e, "The full text was not attached."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Upload the full text"
      description={item.title}
      submit="Attach the PDF"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <Field
        label="PDF"
        hint="Kittum stores it by its hash and parses its text layer for screening and extraction."
      >
        <FileInput label="PDF file" accept="application/pdf,.pdf" onFile={setFile} />
      </Field>
      <Field
        label="Licence"
        hint="As the publisher states it, e.g. CC BY 4.0. The reports cite it."
      >
        <TextInput value={licence} onChange={(e) => setLicence(e.currentTarget.value)} />
      </Field>
    </FormDialog>
  );
}

function AddSentinel({
  open,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [title, setTitle] = useState("");
  const [outcome, setOutcome] = useState<"include" | "exclude">("exclude");
  const [file, setFile] = useState<File | null>(null);
  const [licence, setLicence] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (!title.trim() || !file) {
      setError("Give the report's title and its PDF.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await run(
        workbench.attachSentinelFullText({
          projectId: project.id,
          as: me.actor_id,
          title: title.trim(),
          expectedOutcome: outcome,
          pdf: await readPdf(file),
          licence: licence.trim() || null,
        }),
      );
      setTitle("");
      setFile(null);
      onDone(
        `Added the full-text sentinel “${title.trim()}”. Full-text stages opened from now on seed it into every queue.`,
      );
    } catch (e) {
      setError(failure(e, "The sentinel was not added."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Add a full-text sentinel"
      description="A report whose outcome you know. Every full-text queue gets it like any other report; a reviewer who decides it wrongly sets off the drift alarm. It never shows in this list."
      submit="Add the sentinel"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <Field label="Title">
        <TextInput value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
      </Field>
      <Field label="Expected outcome">
        <Select
          value={outcome}
          onChange={(e) => setOutcome(e.currentTarget.value as "include" | "exclude")}
        >
          <option value="exclude">Exclude</option>
          <option value="include">Include</option>
        </Select>
      </Field>
      <Field label="PDF">
        <FileInput label="Sentinel PDF" accept="application/pdf,.pdf" onFile={setFile} />
      </Field>
      <Field label="Licence">
        <TextInput value={licence} onChange={(e) => setLicence(e.currentTarget.value)} />
      </Field>
    </FormDialog>
  );
}
