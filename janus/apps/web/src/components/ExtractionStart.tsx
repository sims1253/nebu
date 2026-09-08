import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Braces, Play, Trash2, Download } from "lucide-react";
import { extractions, saveJson } from "~/lib/extractions";
import sampleSpec from "../../../../examples/purchase-order/extraction.json";
import samplePdf from "../../../../examples/purchase-order/document.pdf?url";

export function ExtractionStart() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const history = useQuery({
    queryKey: ["extractions"],
    queryFn: extractions.list,
  });
  const [document, setDocument] = useState<File | null>(null);
  const [specification, setSpecification] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: ({ pdf, spec }: { pdf: File; spec: File }) =>
      extractions.create(pdf, spec),
    onSuccess: (saved) => {
      client.setQueryData(["extraction", saved.id], saved);
      void client.invalidateQueries({ queryKey: ["extractions"] });
      void navigate({
        to: "/extractions/$extractionId",
        params: { extractionId: saved.id },
      });
    },
  });
  const remove = useMutation({
    mutationFn: extractions.remove,
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: ["extractions"] }),
  });
  const [loadingSample, setLoadingSample] = useState(false);
  const busy = run.isPending || loadingSample;

  async function sample() {
    setLoadingSample(true);
    setError(null);
    run.reset();
    try {
      const response = await fetch(samplePdf);
      if (!response.ok) throw new Error("Could not load the sample PDF.");
      run.mutate({
        pdf: new File([await response.blob()], "purchase-order.pdf", {
          type: "application/pdf",
        }),
        spec: new File([JSON.stringify(sampleSpec)], "extraction.json", {
          type: "application/json",
        }),
      });
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setLoadingSample(false);
    }
  }

  return (
    <div className="extraction-start">
      <header className="start-heading">
        <div className="document-mark" aria-hidden="true">
          <FileText size={28} />
        </div>
        <h1>Turn a PDF into structured data.</h1>
        <p>
          Define the fields you need. Extract their values, inspect the source,
          then export or compare the data.
        </p>
      </header>
      <form
        className="extraction-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (document && specification)
            run.mutate({ pdf: document, spec: specification });
        }}
      >
        <fieldset disabled={busy}>
          <label className="file-picker">
            <FileText size={22} />
            <span>
              <strong>PDF document</strong>
              <small>Text-based PDF, up to 50 MB</small>
            </span>
            <input
              type="file"
              accept=".pdf"
              aria-label="PDF document"
              onChange={(event) => {
                setDocument(event.target.files?.[0] ?? null);
                run.reset();
                setError(null);
              }}
            />
          </label>
          <label className="file-picker">
            <Braces size={22} />
            <span>
              <strong>Extraction specification</strong>
              <small>JSON defining fields, types, and locations</small>
            </span>
            <input
              type="file"
              accept=".json"
              aria-label="Extraction specification"
              onChange={(event) => {
                setSpecification(event.target.files?.[0] ?? null);
                run.reset();
                setError(null);
              }}
            />
          </label>
          <div className="start-actions">
            <button
              className="action-primary"
              disabled={!document || !specification}
              type="submit"
            >
              {run.isPending ? "Extracting data…" : "Extract data"}
            </button>
            <button
              className="action-quiet"
              type="button"
              onClick={() => void sample()}
            >
              <Play size={15} />
              Try purchase order
            </button>
          </div>
        </fieldset>
        {busy && (
          <p role="status" className="form-status">
            Reading the PDF and extracting fields. Keep this page open until the
            result appears.
          </p>
        )}
        {(run.error || error) && (
          <p role="alert" className="form-error">
            {run.error?.message ?? error}
          </p>
        )}
      </form>
      <div className="starter-help">
        <span>Start with the two-field purchase-order specification.</span>
        <button
          className="text-action"
          onClick={() => saveJson(sampleSpec, "extraction.json")}
        >
          <Download size={14} />
          Download specification
        </button>
        <a href={samplePdf} download="purchase-order.pdf">
          Download PDF
        </a>
      </div>
      <section className="extraction-history">
        <h2>Saved extractions</h2>
        {history.isLoading && <p>Loading saved extractions…</p>}
        {history.error && (
          <p role="alert">
            Could not load saved extractions. {history.error.message}
          </p>
        )}
        {remove.error && (
          <p role="alert">
            Could not delete the extraction. {remove.error.message}
          </p>
        )}
        {history.data?.length === 0 && (
          <p>
            Your completed extractions will appear here. You can reopen their
            data and source document.
          </p>
        )}
        {history.data?.map((item) => (
          <div className="history-row" key={item.id}>
            <Link
              to="/extractions/$extractionId"
              params={{ extractionId: item.id }}
            >
              <FileText size={18} />
              <span>
                <strong>{item.document_filename}</strong>
                <small>{item.specification_name}</small>
              </span>
              <span className={item.issues ? "issue-text" : "muted-text"}>
                {item.issues
                  ? `${item.issues} to inspect`
                  : `${item.fields} fields`}
              </span>
              <time dateTime={item.created_at}>
                {new Date(item.created_at).toLocaleDateString()}
              </time>
            </Link>
            <button
              className="icon-action"
              disabled={remove.isPending}
              aria-label={`Delete ${item.document_filename}`}
              onClick={() => {
                if (
                  window.confirm(
                    `Delete ${item.document_filename} and its saved extraction?`,
                  )
                )
                  remove.mutate(item.id);
              }}
            >
              <Trash2 size={15} />
            </button>
          </div>
        ))}
      </section>
    </div>
  );
}
