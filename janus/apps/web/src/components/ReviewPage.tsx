import { useMemo, useState } from "react";
import { useParams } from "@tanstack/react-router";
import type {
  ComparisonStatus,
  FieldComparison,
  Resolution,
} from "@janus/contracts";
import {
  exportFilename,
  exportReviewCsv,
  exportReviewJson,
} from "@janus/shared";
import { api } from "~/lib/api";
import {
  useReview,
  useReviewProgress,
  useReviewResult,
  useSubmitReviewAnnotation,
} from "~/lib/queries";
import { Button } from "~/components/ui/button";

const statusLabel: Record<ComparisonStatus, string> = {
  match: "Match",
  mismatch: "Mismatch",
  not_compared: "Not compared",
  ambiguous: "Ambiguous",
};

const resolutions: { value: Resolution; label: string }[] = [
  { value: "accepted", label: "Accept" },
  { value: "reference_value_incorrect", label: "Reference value incorrect" },
  { value: "incorrect_document_match", label: "Incorrect document match" },
  { value: "not_present_in_document", label: "Not present in document" },
];

function download(content: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = window.document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function formatValue(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function ComparisonRow({
  item,
  selected,
  onSelect,
  onResolve,
}: {
  item: FieldComparison;
  selected: boolean;
  onSelect: () => void;
  onResolve: (resolution: Resolution) => void;
}) {
  return (
    <tr
      className={`${selected ? "bg-primary/5" : "hover:bg-muted/40"} cursor-pointer`}
      tabIndex={0}
      aria-selected={selected}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (
          event.target === event.currentTarget &&
          (event.key === "Enter" || event.key === " ")
        ) {
          event.preventDefault();
          onSelect();
        }
      }}
    >
      <td className="px-2 py-1.5">
        <div className="font-medium">{item.field_label}</div>
        <div className="text-[10px] text-muted-foreground">
          {item.reference_pointer}
        </div>
      </td>
      <td className="px-2 py-1.5 font-mono">
        {item.reference_value_present ? formatValue(item.reference_value) : "—"}
      </td>
      <td className="px-2 py-1.5 font-mono">{item.document_value ?? "—"}</td>
      <td className="px-2 py-1.5">
        <span
          className={
            item.status === "mismatch"
              ? "text-destructive"
              : item.status === "match"
                ? "text-success"
                : "text-warning-foreground"
          }
        >
          {statusLabel[item.status]}
        </span>
      </td>
      <td className="px-2 py-1.5" onClick={(event) => event.stopPropagation()}>
        <select
          aria-label={`Resolution for ${item.field_label}`}
          className="h-7 max-w-44 rounded border bg-background px-1 text-xs"
          value={item.resolution}
          onChange={(event) => onResolve(event.target.value as Resolution)}
        >
          <option value="pending">Pending</option>
          {resolutions.map((entry) => (
            <option key={entry.value} value={entry.value}>
              {entry.label}
            </option>
          ))}
        </select>
      </td>
    </tr>
  );
}

export function ReviewPage() {
  const { reviewId } = useParams({ from: "/reviews/$reviewId" });
  const review = useReview(reviewId);
  const progress = useReviewProgress(reviewId);
  const result = useReviewResult(reviewId, review.data?.status);
  const annotate = useSubmitReviewAnnotation(reviewId);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | ComparisonStatus>("all");
  const comparisons = useMemo(
    () =>
      result.data?.comparisons.filter(
        (item) => filter === "all" || item.status === filter,
      ) ?? [],
    [filter, result.data],
  );
  const selected =
    comparisons.find((item) => item.id === selectedId) ?? comparisons[0];

  if (review.isError || result.isError) {
    return (
      <div role="alert" className="p-8">
        <h1 className="font-medium text-destructive">
          Could not load the review
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {(review.error ?? result.error)?.message}
        </p>
      </div>
    );
  }
  if (review.isLoading || !review.data)
    return <div className="p-8 text-muted-foreground">Loading review…</div>;
  if (review.data.status === "error")
    return (
      <div role="alert" className="p-8 text-destructive">
        {review.data.error_message ?? "Review failed."}
      </div>
    );
  if (review.data.status !== "ready" || !result.data) {
    return (
      <div className="mx-auto max-w-md p-8">
        <h1 className="font-medium">{review.data.document_filename}</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {progress.data?.current_stage_detail ?? "Starting…"}
        </p>
        <div className="mt-3 h-2 overflow-hidden rounded bg-muted">
          <div
            className="h-full bg-primary transition-all"
            style={{ width: `${progress.data?.progress_percent ?? 0}%` }}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="grid h-full min-h-0 grid-cols-[minmax(560px,1fr)_minmax(380px,0.8fr)]">
      <section className="min-h-0 overflow-auto border-r">
        <header className="sticky top-0 z-10 flex items-center justify-between border-b bg-background p-2">
          <div>
            <h1 className="font-medium">{review.data.document_filename}</h1>
            <p className="text-[10px] text-muted-foreground">
              Specification v{review.data.specification_version} ·{" "}
              {result.data.summary.total_fields} fields
            </p>
          </div>
          <div className="flex gap-1">
            {(
              ["all", "match", "mismatch", "not_compared", "ambiguous"] as const
            ).map((value) => (
              <Button
                key={value}
                size="sm"
                variant={filter === value ? "default" : "outline"}
                onClick={() => setFilter(value)}
              >
                {value.replace("_", " ")}
              </Button>
            ))}
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                download(
                  exportReviewCsv(result.data),
                  exportFilename(review.data.document_filename, "csv"),
                  "text/csv",
                )
              }
            >
              CSV
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                download(
                  exportReviewJson(result.data),
                  exportFilename(review.data.document_filename, "json"),
                  "application/json",
                )
              }
            >
              JSON
            </Button>
          </div>
        </header>
        <table className="w-full table-fixed text-xs">
          <thead className="sticky top-[53px] bg-muted/95 text-left">
            <tr>
              <th className="w-[26%] px-2 py-1">Field</th>
              <th className="w-[18%] px-2">Reference</th>
              <th className="w-[18%] px-2">Document</th>
              <th className="w-[14%] px-2">Status</th>
              <th className="w-[24%] px-2">Resolution</th>
            </tr>
          </thead>
          <tbody>
            {comparisons.map((item) => (
              <ComparisonRow
                key={item.id}
                item={item}
                selected={selected?.id === item.id}
                onSelect={() => setSelectedId(item.id)}
                onResolve={(resolution) =>
                  annotate.mutate({ comparison_id: item.id, resolution })
                }
              />
            ))}
          </tbody>
        </table>
      </section>
      <section className="grid min-h-0 grid-rows-[minmax(0,1fr)_auto]">
        {/* The browser PDF viewer does not render inside a sandboxed iframe. */}
        {/* oxlint-disable-next-line react/iframe-missing-sandbox */}
        <iframe
          className="h-full w-full bg-muted"
          title="Source document"
          src={`${api.getReviewDocumentUrl(reviewId)}#page=${(selected?.document_location?.page ?? 0) + 1}`}
        />
        <aside className="max-h-52 overflow-auto border-t p-3 text-xs">
          {selected ? (
            <>
              <div className="flex items-center justify-between">
                <h2 className="font-medium">
                  {selected.section_label} / {selected.field_label}
                </h2>
                <span>{Math.round(selected.confidence * 100)}% confidence</span>
              </div>
              <p className="mt-2 text-muted-foreground">
                {selected.explanation}
              </p>
              <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                <dt>Extraction</dt>
                <dd>{selected.extraction_status}</dd>
                <dt>Comparator</dt>
                <dd>{selected.comparator}</dd>
                <dt>Context</dt>
                <dd>
                  {[selected.row_context, selected.column_context]
                    .filter(Boolean)
                    .join(" · ") || "—"}
                </dd>
                <dt>Page</dt>
                <dd>
                  {selected.document_location
                    ? selected.document_location.page + 1
                    : "—"}
                </dd>
              </dl>
            </>
          ) : (
            <p>No fields match this filter.</p>
          )}
        </aside>
      </section>
    </div>
  );
}
