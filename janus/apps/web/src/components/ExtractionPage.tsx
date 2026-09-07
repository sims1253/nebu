import { useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Braces,
  Download,
  FileCheck2,
  Pencil,
  X,
} from "lucide-react";
import type {
  CheckResult,
  ExtractedField,
  SavedExtraction,
} from "@janus/contracts";
import { atPointer, extractions, saveJson } from "~/lib/extractions";
import { SourcePreview } from "./SourcePreview";

const fieldLabels = {
  extracted: "Extracted",
  missing: "Missing",
  ambiguous: "Ambiguous",
  invalid: "Invalid format",
  error: "Incomplete",
};
const checkLabels = {
  match: "Match",
  mismatch: "Mismatch",
  not_compared: "Not compared",
};
function display(value: unknown) {
  return value == null
    ? "No value"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
}

function ComparePanel({
  saved,
  onClose,
  onChecks,
}: {
  saved: SavedExtraction;
  onClose: () => void;
  onChecks: (checks: CheckResult[], filename: string) => void;
}) {
  const [reference, setReference] = useState<File | null>(null);
  const [rules, setRules] = useState(
    JSON.stringify(
      saved.result.fields
        .filter((f) => f.type !== "records")
        .map((f) => ({ path: f.path, reference_pointer: f.path })),
      null,
      2,
    ),
  );
  const compare = useMutation({
    mutationFn: () => extractions.compare(saved.id, reference!, rules),
    onSuccess: (checks) => onChecks(checks, reference!.name),
  });
  return (
    <section
      className="workbench-editor"
      aria-label="Compare with reference data"
    >
      <header>
        <h2>Compare with reference data</h2>
        <button
          className="icon-action"
          aria-label="Close comparison"
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </header>
      <p>
        Choose trusted data, then pair each output path with its reference path.
        Numeric checks accept an absolute tolerance. This uses the saved
        extraction. Export the comparison to keep it after closing this tab.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          compare.mutate();
        }}
      >
        <fieldset disabled={compare.isPending}>
          <label>
            Reference data
            <input
              type="file"
              accept=".json,.csv,.xlsx"
              required
              onChange={(e) => {
                setReference(e.target.files?.[0] ?? null);
                compare.reset();
              }}
            />
          </label>
          <label>
            Comparison rules
            <textarea
              spellCheck={false}
              value={rules}
              onChange={(e) => {
                setRules(e.target.value);
                compare.reset();
              }}
              rows={9}
            />
          </label>
          <div className="editor-actions">
            <button
              className="action-primary"
              disabled={!reference || compare.isPending}
            >
              {compare.isPending ? "Comparing…" : "Compare data"}
            </button>
            <span>Rules use JSON Pointers. Array indexes start at zero.</span>
          </div>
        </fieldset>
      </form>
      {compare.error && (
        <p className="form-error" role="alert">
          {compare.error.message}
        </p>
      )}
      {compare.data && (
        <p role="status">
          {compare.data.filter((c) => c.status === "match").length} matched;{" "}
          {compare.data.filter((c) => c.status === "mismatch").length}{" "}
          mismatched;{" "}
          {compare.data.filter((c) => c.status === "not_compared").length} not
          compared.
        </p>
      )}
    </section>
  );
}

function SpecificationEditor({
  saved,
  onClose,
}: {
  saved: SavedExtraction;
  onClose: () => void;
}) {
  const spec = useQuery({
    queryKey: ["extraction-spec", saved.id],
    queryFn: () => extractions.specification(saved.id),
  });
  const [draft, setDraft] = useState<string | null>(null);
  const client = useQueryClient();
  const navigate = useNavigate();
  const text = draft ?? (spec.data ? JSON.stringify(spec.data, null, 2) : "");
  const rerun = useMutation({
    mutationFn: async () => {
      const response = await fetch(`/api/extractions/${saved.id}/document`);
      if (!response.ok) throw new Error("Could not load the source PDF.");
      return extractions.create(
        new File([await response.blob()], saved.document_filename, {
          type: "application/pdf",
        }),
        new File([text], "extraction.json", { type: "application/json" }),
      );
    },
    onSuccess: (next) => {
      client.setQueryData(["extraction", next.id], next);
      void client.invalidateQueries({ queryKey: ["extractions"] });
      void navigate({
        to: "/extractions/$extractionId",
        params: { extractionId: next.id },
      });
      onClose();
    },
  });
  return (
    <section
      className="workbench-editor"
      aria-label="Edit extraction specification"
    >
      <header>
        <h2>Extraction specification</h2>
        <button
          className="icon-action"
          aria-label="Close specification"
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </header>
      <p>
        Adjust fields or locations and extract again. Each run saves a separate
        result.
      </p>
      <label className="sr-only" htmlFor="specification-editor">
        Specification JSON
      </label>
      <textarea
        id="specification-editor"
        value={text}
        disabled={rerun.isPending || !spec.data}
        onChange={(event) => setDraft(event.target.value)}
        rows={14}
        spellCheck={false}
      />
      <div className="editor-actions">
        <button
          className="action-primary"
          disabled={!spec.data || rerun.isPending}
          onClick={() => rerun.mutate()}
        >
          {rerun.isPending ? "Extracting…" : "Extract again"}
        </button>
        <button
          className="action-quiet"
          disabled={!spec.data}
          onClick={() => saveJson(spec.data, "extraction.json")}
        >
          <Download size={14} />
          Download saved specification
        </button>
        <a href="/api/extractions/schema" target="_blank" rel="noreferrer">
          Specification schema
        </a>
      </div>
      {(spec.error || rerun.error) && (
        <p role="alert" className="form-error">
          {spec.error?.message ?? rerun.error?.message}
        </p>
      )}
    </section>
  );
}

function ExtractionResultView({ saved }: { saved: SavedExtraction }) {
  const [selectedField, setSelectedField] = useState<ExtractedField | null>(
    null,
  );
  const [filter, setFilter] = useState<"all" | "issues">("all");
  const [view, setView] = useState<"fields" | "json">("fields");
  const [panel, setPanel] = useState<"compare" | "specification" | null>(null);
  const [checks, setChecks] = useState<CheckResult[] | null>(null);
  const [referenceName, setReferenceName] = useState("");
  const result = saved.result;
  const issues = result.fields.filter((f) => f.status !== "extracted");
  const visible = filter === "issues" ? issues : result.fields;
  const selected =
    selectedField && visible.includes(selectedField)
      ? selectedField
      : visible[0];
  const selectedChecks = checks?.filter((c) => c.path === selected?.path);
  return (
    <div className="extraction-workbench">
      <header className="workbench-heading">
        <div>
          <Link to="/" className="back-link">
            <ArrowLeft size={14} />
            Extractions
          </Link>
          <h1>{saved.document_filename}</h1>
          <p>{result.specification_name}</p>
        </div>
        <div className="workbench-actions">
          <button
            className="action-quiet"
            onClick={() =>
              setPanel(panel === "specification" ? null : "specification")
            }
          >
            <Pencil size={15} />
            Specification
          </button>
          <button
            className="action-quiet"
            onClick={() => setPanel(panel === "compare" ? null : "compare")}
          >
            <FileCheck2 size={16} />
            Compare data
          </button>
          <button
            className="action-primary"
            onClick={() =>
              saveJson(
                result.data,
                `${saved.document_filename.replace(/\.pdf$/i, "")}.data.json`,
              )
            }
          >
            <Download size={15} />
            Export data
          </button>
        </div>
      </header>
      {panel === "compare" && (
        <ComparePanel
          saved={saved}
          onClose={() => setPanel(null)}
          onChecks={(values, filename) => {
            setChecks(values);
            setReferenceName(filename);
          }}
        />
      )}
      {panel === "specification" && (
        <SpecificationEditor saved={saved} onClose={() => setPanel(null)} />
      )}
      <div className="workbench-panes">
        <section className="data-panel" aria-label="Extracted data">
          <header className="panel-toolbar">
            <h2>Extracted data</h2>
            <div className="segmented-control">
              <button
                aria-pressed={view === "fields"}
                onClick={() => setView("fields")}
              >
                Fields
              </button>
              <button
                aria-pressed={view === "json"}
                onClick={() => setView("json")}
              >
                <Braces size={14} />
                JSON
              </button>
            </div>
          </header>
          <div className="data-summary">
            <span>
              {result.fields.filter((f) => f.status === "extracted").length}{" "}
              extracted
            </span>
            <button
              className={filter === "issues" ? "filter-active" : ""}
              aria-pressed={filter === "issues"}
              onClick={() => setFilter(filter === "issues" ? "all" : "issues")}
            >
              {issues.length} to inspect
            </button>
            {filter === "issues" && (
              <button onClick={() => setFilter("all")}>Show all</button>
            )}
          </div>
          {issues.length > 0 && (
            <p className="data-notice">
              Some data is unresolved. Inspect these fields before using the
              export.
            </p>
          )}
          {checks && (
            <div className="comparison-summary">
              <strong>{referenceName}</strong>
              <span>
                {checks.filter((c) => c.status === "mismatch").length}{" "}
                mismatches;{" "}
                {checks.filter((c) => c.status === "not_compared").length} not
                compared
              </span>
              <button
                className="text-action"
                onClick={() => saveJson(checks, "comparison.json")}
              >
                Export comparison
              </button>
              <button
                className="icon-action"
                aria-label="Clear comparison"
                onClick={() => setChecks(null)}
              >
                <X size={14} />
              </button>
            </div>
          )}
          {view === "json" ? (
            <pre className="data-json">
              {JSON.stringify(result.data, null, 2)}
            </pre>
          ) : (
            <div className="field-list">
              <div className="field-columns" aria-hidden="true">
                <span>Field / value</span>
                <span>State</span>
              </div>
              {visible.length === 0 && (
                <p className="empty-fields">
                  {filter === "issues"
                    ? "Every extracted field has a value. Select a field to check its source."
                    : "The selected table contains no data rows."}
                </p>
              )}
              {visible.map((field) => (
                <button
                  key={JSON.stringify(field)}
                  className={`extracted-field ${selected === field ? "is-selected" : ""}`}
                  aria-pressed={selected === field}
                  onClick={() => setSelectedField(field)}
                >
                  <span className="field-value">
                    <span className="field-path">{field.path}</span>
                    <strong
                      className={
                        field.status !== "extracted" ? "unresolved-value" : ""
                      }
                    >
                      {field.status === "extracted"
                        ? display(atPointer(result.data, field.path))
                        : "Unresolved"}
                    </strong>
                    <small>
                      {field.type}
                      {field.source ? ` / page ${field.source.page + 1}` : ""}
                    </small>
                  </span>
                  <span className="field-states">
                    <span className={`state-label state-${field.status}`}>
                      {fieldLabels[field.status]}
                    </span>
                    {checks
                      ?.filter((c) => c.path === field.path)
                      .map((check) => (
                        <span
                          key={JSON.stringify(check)}
                          className={`state-label state-${check.status}`}
                        >
                          {checkLabels[check.status]}
                        </span>
                      ))}
                  </span>
                </button>
              ))}
            </div>
          )}
          {selected && (
            <aside className="field-detail">
              <strong>{selected.path}</strong>
              <p>
                {selected.message ||
                  "The value was parsed using the specification. Check its source before relying on it."}
              </p>
              {selectedChecks?.map((check) => (
                <div key={JSON.stringify(check)} className="check-detail">
                  <span>{check.reference_pointer}</span>
                  <p>Reference: {display(check.expected)}</p>
                  <p>{check.message}</p>
                </div>
              ))}
            </aside>
          )}
          <footer className="data-footer">
            <button
              className="text-action"
              onClick={() =>
                saveJson(
                  result,
                  `${saved.document_filename.replace(/\.pdf$/i, "")}.extraction.json`,
                )
              }
            >
              <Download size={14} />
              Export with evidence
            </button>
            <span>Values and source locations</span>
          </footer>
        </section>
        <SourcePreview id={saved.id} result={result} selected={selected} />
      </div>
    </div>
  );
}

export function ExtractionPage() {
  const { extractionId } = useParams({ from: "/extractions/$extractionId" });
  const run = useQuery({
    queryKey: ["extraction", extractionId],
    queryFn: () => extractions.get(extractionId),
  });
  if (run.isPending)
    return (
      <p className="page-message" role="status">
        Loading extraction…
      </p>
    );
  if (run.error)
    return (
      <div className="page-message" role="alert">
        <h1>Could not load this extraction</h1>
        <p>{run.error.message}</p>
        <Link to="/">Return to extractions</Link>
      </div>
    );
  return <ExtractionResultView key={run.data.id} saved={run.data} />;
}
