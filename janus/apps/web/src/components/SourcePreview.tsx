import { useState } from "react";
import { ChevronLeft, ChevronRight, ExternalLink } from "lucide-react";
import type { ExtractedField, StructuredExtraction } from "@janus/contracts";

export function SourcePreview({
  id,
  result,
  selected,
}: {
  id: string;
  result: StructuredExtraction;
  selected: ExtractedField | undefined;
}) {
  const [navigation, setNavigation] = useState<{
    field: ExtractedField | undefined;
    page: number;
  } | null>(null);
  const page =
    navigation && navigation.field === selected
      ? navigation.page
      : (selected?.source?.page ?? 0);
  const [failedPage, setFailedPage] = useState<number | null>(null);
  const failed = failedPage === page;
  function setPage(next: number) {
    setNavigation({ field: selected, page: next });
  }
  const dimensions = result.pages[page];
  const location = selected?.source?.page === page ? selected.source : null;
  const candidate = location?.cell_bbox;
  const box =
    candidate &&
    typeof candidate["x"] === "number" &&
    typeof candidate["y"] === "number" &&
    typeof candidate["width"] === "number" &&
    typeof candidate["height"] === "number"
      ? {
          x: candidate["x"],
          y: candidate["y"],
          width: candidate["width"],
          height: candidate["height"],
        }
      : null;
  return (
    <section className="source-panel" aria-label="Source PDF">
      <header className="panel-toolbar">
        <h2>Source document</h2>
        <div className="page-controls">
          <button
            className="icon-action"
            aria-label="Previous page"
            disabled={page === 0}
            onClick={() => setPage(page - 1)}
          >
            <ChevronLeft size={16} />
          </button>
          <span>
            Page {page + 1} of {result.pages.length}
          </span>
          <button
            className="icon-action"
            aria-label="Next page"
            disabled={page >= result.pages.length - 1}
            onClick={() => setPage(page + 1)}
          >
            <ChevronRight size={16} />
          </button>
          <a
            className="icon-action"
            href={`/api/extractions/${id}/document#page=${page + 1}`}
            target="_blank"
            rel="noreferrer"
            aria-label="Open original PDF"
          >
            <ExternalLink size={16} />
          </a>
        </div>
      </header>
      <div
        className="source-scroll"
        tabIndex={0}
        role="region"
        aria-label="PDF page preview"
      >
        {failed ? (
          <p role="alert">
            Could not render this page.{" "}
            <a
              href={`/api/extractions/${id}/document`}
              target="_blank"
              rel="noreferrer"
            >
              Open the original PDF
            </a>
            .
          </p>
        ) : (
          <div
            className="source-sheet"
            style={{
              aspectRatio: dimensions
                ? `${dimensions.width} / ${dimensions.height}`
                : undefined,
            }}
          >
            <img
              src={`/api/extractions/${id}/pages/${page}`}
              alt={`PDF page ${page + 1}`}
              onError={() => setFailedPage(page)}
            />
            {box && dimensions && (
              <div
                className="source-highlight"
                aria-label={`Source of ${selected?.path}`}
                style={{
                  left: `${(box.x / dimensions.width) * 100}%`,
                  top: `${(box.y / dimensions.height) * 100}%`,
                  width: `${(box.width / dimensions.width) * 100}%`,
                  height: `${(box.height / dimensions.height) * 100}%`,
                }}
              />
            )}
          </div>
        )}
      </div>
      <footer className="source-caption">
        {selected?.source ? (
          <>
            <strong>{selected.path}</strong>
            <span>
              {location
                ? box
                  ? "Highlighted source"
                  : "Exact region unavailable; inspect this page."
                : "Use the page controls to return to the selected field."}
            </span>
            <blockquote>{selected.source.raw_text}</blockquote>
          </>
        ) : (
          <p>Select a field with a source location to inspect its evidence.</p>
        )}
      </footer>
    </section>
  );
}
