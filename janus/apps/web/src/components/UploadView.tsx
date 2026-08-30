import { useCallback, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Braces, FileText, Table2, X } from "lucide-react";
import type { CompilationDiagnostic } from "@janus/contracts";
import { ApiClientError, api } from "~/lib/api";
import { useCreateReview } from "~/lib/queries";
import { Button } from "~/components/ui/button";

type Slot = "document" | "specification" | "reference";

function FileSlot({
  label,
  accept,
  file,
  inputRef,
  icon,
  onFile,
  onClear,
}: {
  label: string;
  accept: string;
  file: File | null;
  inputRef: React.RefObject<HTMLInputElement | null>;
  icon: React.ReactNode;
  onFile: (file: File) => void;
  onClear: () => void;
}) {
  return (
    <div className="flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-xs">
      <span className="text-muted-foreground">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="font-medium">{label}</div>
        <div className="truncate text-muted-foreground">
          {file?.name ?? "No file selected"}
        </div>
      </div>
      {file ? (
        <button type="button" onClick={onClear} aria-label={`Remove ${label}`}>
          <X className="size-4" />
        </button>
      ) : (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => inputRef.current?.click()}
        >
          Browse
        </Button>
      )}
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        className="sr-only"
        aria-label={`Select ${label}`}
        onChange={(event) => {
          const selected = event.target.files?.[0];
          if (selected) onFile(selected);
          event.target.value = "";
        }}
      />
    </div>
  );
}

export function UploadView() {
  const navigate = useNavigate();
  const create = useCreateReview();
  const documentInput = useRef<HTMLInputElement>(null);
  const specificationInput = useRef<HTMLInputElement>(null);
  const referenceInput = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<Record<Slot, File | null>>({
    document: null,
    specification: null,
    reference: null,
  });
  const [diagnostics, setDiagnostics] = useState<CompilationDiagnostic[]>([]);
  const [validated, setValidated] = useState<string | null>(null);

  const update = useCallback((slot: Slot, file: File | null) => {
    setFiles((current) => ({ ...current, [slot]: file }));
    setDiagnostics([]);
    setValidated(null);
  }, []);

  const validate = useCallback(async () => {
    if (!files.specification || !files.reference) return false;
    try {
      const response = await api.validateInputs(
        files.specification,
        files.reference,
      );
      setValidated(
        `${response.field_count} fields · specification v${response.specification_version}`,
      );
      setDiagnostics([]);
      return true;
    } catch (error) {
      setDiagnostics(
        error instanceof ApiClientError
          ? (error.apiError?.diagnostics ?? [])
          : [],
      );
      setValidated(null);
      return false;
    }
  }, [files.reference, files.specification]);

  const submit = useCallback(async () => {
    if (!files.document || !files.specification || !files.reference) return;
    if (!(await validate())) return;
    try {
      const review = await create.mutateAsync({
        document: files.document,
        specification: files.specification,
        reference: files.reference,
      });
      void navigate({
        to: "/reviews/$reviewId",
        params: { reviewId: review.id },
      });
    } catch (error) {
      // Compilation failures carry diagnostics for the panel below; anything
      // else is surfaced by the global mutation error toast.
      setDiagnostics(
        error instanceof ApiClientError
          ? (error.apiError?.diagnostics ?? [])
          : [],
      );
      setValidated(null);
    }
  }, [create, files, navigate, validate]);

  return (
    <div className="flex justify-center p-8">
      <div className="w-full max-w-lg space-y-3">
        <header className="text-center">
          <h1 className="text-base font-semibold">
            Start a document comparison
          </h1>
          <p className="text-xs text-muted-foreground">
            Upload a PDF, a v1 comparison specification, and reference data.
          </p>
        </header>
        <FileSlot
          label="Source document (PDF)"
          accept=".pdf"
          file={files.document}
          inputRef={documentInput}
          icon={<FileText className="size-4" />}
          onFile={(file) => update("document", file)}
          onClear={() => update("document", null)}
        />
        <FileSlot
          label="Comparison specification (JSON)"
          accept=".json"
          file={files.specification}
          inputRef={specificationInput}
          icon={<Braces className="size-4" />}
          onFile={(file) => update("specification", file)}
          onClear={() => update("specification", null)}
        />
        <FileSlot
          label="Reference dataset (JSON, CSV, or XLSX)"
          accept=".json,.csv,.xlsx"
          file={files.reference}
          inputRef={referenceInput}
          icon={<Table2 className="size-4" />}
          onFile={(file) => update("reference", file)}
          onClear={() => update("reference", null)}
        />

        {validated && (
          <p
            role="status"
            className="rounded border border-success/30 bg-success/5 p-2 text-xs text-success"
          >
            Valid: {validated}
          </p>
        )}
        {diagnostics.length > 0 && (
          <div
            role="alert"
            className="rounded border border-destructive/30 bg-destructive/5 p-2 text-xs"
          >
            {diagnostics.map((item) => (
              <div
                key={`${item.specification_path}:${item.code}`}
                className="mb-2 last:mb-0"
              >
                <div className="font-medium">
                  {item.specification_path}: {item.message}
                </div>
                {item.reference_pointer && (
                  <div>Reference: {item.reference_pointer}</div>
                )}
                <div className="text-muted-foreground">
                  {item.remediation_hint}
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="flex gap-2">
          <Button
            variant="outline"
            className="flex-1"
            disabled={!files.specification || !files.reference}
            onClick={validate}
          >
            Validate inputs
          </Button>
          <Button
            className="flex-1"
            disabled={
              create.isPending || Object.values(files).some((file) => !file)
            }
            onClick={submit}
          >
            {create.isPending ? "Starting…" : "Start Review"}
          </Button>
        </div>
      </div>
    </div>
  );
}
