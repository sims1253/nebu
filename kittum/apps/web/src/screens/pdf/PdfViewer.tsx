// The source pane: server-rendered page images with an invisible text layer laid from the
// layer's word boxes, as in Janus. The browser never interprets the PDF, so a selection here is
// exactly what the parser saw, in code points.
import { LegendList } from "@legendapp/list/react";
import type { LegendListRef } from "@legendapp/list/react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { Box, LayerSummary, PageLayer, PageSize } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import type { Workbench } from "../../data/workbench.ts";
import { codePointIndex } from "../../lib/codepoints.ts";

export interface PageHighlight {
  readonly id: string;
  readonly boxes: ReadonlyArray<Box>;
  /**
   * `current` is the selected item's source; `adopted` is evidence already taken; `ai` is the AI
   * judge's verified quote, shown after the reviewer decided.
   */
  readonly kind: "current" | "adopted" | "mention" | "ai";
  readonly label: string;
}

export interface TextSelection {
  readonly start: number;
  readonly end: number;
  readonly quote: string;
  readonly boxes: ReadonlyArray<Box>;
}

const GAP = 16;
const VISIBLE_RATIO = 0.9;
const MAX_WIDTH = 860;
/** Pages kept per workbench, most recently used last; older ones are dropped and reloaded. */
const PAGE_CACHE_SIZE = 16;
const pageCaches = new WeakMap<Workbench, Map<string, Promise<PageLayer>>>();

const loadPage = (workbench: Workbench, layer: LayerSummary, page: number) => {
  let cache = pageCaches.get(workbench);
  if (!cache) pageCaches.set(workbench, (cache = new Map()));
  const key = `${layer.id}/${page}`;
  let pending = cache.get(key);
  if (pending) {
    cache.delete(key);
    cache.set(key, pending);
    return pending;
  }
  const entries = cache;
  pending = run(workbench.page(layer, page));
  const loading = pending;
  loading.catch(() => {
    if (entries.get(key) === loading) entries.delete(key);
  });
  entries.set(key, loading);
  for (const oldest of entries.keys()) {
    if (entries.size <= PAGE_CACHE_SIZE) break;
    entries.delete(oldest);
  }
  return loading;
};

export function PdfViewer({
  workbench,
  layer,
  highlights,
  target,
  onSelection,
  onCurrentVisible,
  label,
}: {
  workbench: Workbench;
  layer: LayerSummary;
  highlights: ReadonlyArray<PageHighlight>;
  /** Scrolls so this box is in view whenever `key` changes. */
  target: { readonly key: string; readonly box: Box } | null;
  onSelection?: (selection: TextSelection | null) => void;
  onCurrentVisible?: (visible: boolean) => void;
  label: string;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const list = useRef<LegendListRef>(null);
  const [width, setWidth] = useState(0);
  const [visiblePage, setVisiblePage] = useState(1);

  useLayoutEffect(() => {
    const element = frame.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry?.contentRect.width ?? 0));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const pageWidth = Math.max(0, Math.min(width - 2 * GAP, MAX_WIDTH));
  // The source is on screen when every page it touches reports all of its boxes on screen.
  const currentPages = [
    ...new Set(
      highlights.filter((h) => h.kind === "current").flatMap((h) => h.boxes.map((b) => b.page)),
    ),
  ].join(",");
  const pagesVisible = useRef(new Map<number, boolean>());
  const reportPage = useMemo(() => {
    if (!onCurrentVisible) return undefined;
    const needed = currentPages.split(",").filter(Boolean).map(Number);
    return (page: number, visible: boolean) => {
      pagesVisible.current.set(page, visible);
      onCurrentVisible(
        needed.length > 0 && needed.every((p) => pagesVisible.current.get(p) === true),
      );
    };
  }, [onCurrentVisible, currentPages]);
  // One code-point map per layer; every word and selection on every page reads from it.
  const at = useMemo(() => codePointIndex(layer.text), [layer.text]);
  const heights = useMemo(
    () => layer.pages.map((p) => Math.round((p.height / p.width) * pageWidth) + GAP),
    [layer.pages, pageWidth],
  );

  useEffect(() => {
    if (!target || pageWidth === 0) return;
    const index = target.box.page - 1;
    const size = layer.pages[index];
    if (!size) return;
    const before = heights.slice(0, index).reduce((a, b) => a + b, 0);
    const y = (target.box.y0 / size.height) * (heights[index] ?? 0);
    void list.current?.scrollToOffset({
      offset: Math.max(0, GAP + before + y - 120),
      animated: false,
    });
    // Only a new target moves the page; re-renders of the same target leave the reader's scroll alone.
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.key, pageWidth]);

  // Load the pages around the target before they scroll into view.
  useEffect(() => {
    const around = target?.box.page ?? visiblePage;
    for (const page of [around - 1, around, around + 1, around + 2]) {
      if (page >= 1 && page <= layer.pages.length) void loadPage(workbench, layer, page);
    }
  }, [workbench, layer, target?.box.page, visiblePage]);

  const select = () => {
    if (!onSelection || !frame.current) return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      onSelection(null);
      return;
    }
    const range = selection.getRangeAt(0);
    const words = [...frame.current.querySelectorAll<HTMLSpanElement>(".text-layer > span")].filter(
      (span) => range.intersectsNode(span),
    );
    if (words.length === 0) {
      onSelection(null);
      return;
    }
    const start = Math.min(...words.map((w) => Number(w.dataset["s"])));
    const end = Math.max(...words.map((w) => Number(w.dataset["e"])));
    onSelection({
      start,
      end,
      quote: layer.text.slice(at(start), at(end)),
      boxes: lineBoxes(
        words.map((w) => ({
          page: Number(w.dataset["p"]),
          x0: Number(w.dataset["x0"]),
          y0: Number(w.dataset["y0"]),
          x1: Number(w.dataset["x1"]),
          y1: Number(w.dataset["y1"]),
        })),
      ),
    });
  };

  return (
    <section aria-label={label} className="flex h-full min-h-0 flex-col bg-paper">
      <div className="flex h-8 flex-none items-center justify-between border-b border-rule px-4 text-xs text-ink-3">
        <span>{label}</span>
        <span>
          Page {visiblePage} of {layer.pages.length}
        </span>
      </div>
      <div ref={frame} className="min-h-0 flex-1" onMouseUp={select}>
        {pageWidth > 0 ? (
          <LegendList
            ref={list}
            data={layer.pages}
            extraData={highlights}
            keyExtractor={(p) => String(p.page)}
            getFixedItemSize={(_, index) => heights[index] ?? 0}
            estimatedItemSize={heights[0] ?? 800}
            drawDistance={1200}
            style={{ height: "100%" }}
            onViewableItemsChanged={({ viewableItems }) => {
              const first = viewableItems[0]?.item;
              if (first) setVisiblePage(first.page);
            }}
            renderItem={({ item }) => (
              <PageView
                workbench={workbench}
                layer={layer}
                at={at}
                size={item}
                width={pageWidth}
                highlights={highlights}
                {...(reportPage ? { onCurrentVisible: reportPage } : {})}
              />
            )}
          />
        ) : null}
      </div>
    </section>
  );
}

function PageView({
  workbench,
  layer,
  at,
  size,
  width,
  highlights,
  onCurrentVisible,
}: {
  workbench: Workbench;
  layer: LayerSummary;
  at: (offset: number) => number;
  size: PageSize;
  width: number;
  highlights: ReadonlyArray<PageHighlight>;
  onCurrentVisible?: (page: number, visible: boolean) => void;
}) {
  const [page, setPage] = useState<PageLayer | null>(null);
  const [failed, setFailed] = useState(false);
  // The source exists for the reviewer only once the page image has drawn.
  const [imageReady, setImageReady] = useState(false);
  const frame = useRef<HTMLDivElement>(null);
  const scale = width / size.width;
  const height = Math.round(size.height * scale);

  useEffect(() => {
    let live = true;
    loadPage(workbench, layer, size.page).then(
      (loaded) => live && setPage(loaded),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [workbench, layer, size.page]);

  const boxes = highlights.flatMap((h) =>
    h.boxes.filter((b) => b.page === size.page).map((box, i) => ({ highlight: h, box, i })),
  );

  // Every line of the current source on this page, not only the first, must be on screen.
  const currentBoxes = boxes
    .filter((b) => b.highlight.kind === "current")
    .map(({ box }) => `${box.x0},${box.y0},${box.x1},${box.y1}`)
    .join(";");
  useEffect(() => {
    const elements = [
      ...(frame.current?.querySelectorAll(".evidence-box[data-kind=current]") ?? []),
    ];
    if (elements.length === 0 || !onCurrentVisible || !imageReady) return;
    const report = (visible: boolean) => onCurrentVisible(size.page, visible);
    // `isIntersecting` is true for any overlap; the gate wants 90% of each box on screen. A
    // hidden page shows nothing; on its return each box needs a report computed after it, since
    // the source may have scrolled away meanwhile.
    const ratios = new Map<Element, number>();
    let shownSince = document.hidden ? Infinity : 0;
    const observer = new IntersectionObserver(
      (entries) => {
        if (document.hidden) return;
        for (const entry of entries) {
          if (entry.time >= shownSince) ratios.set(entry.target, entry.intersectionRatio);
        }
        report(elements.every((e) => (ratios.get(e) ?? 0) >= VISIBLE_RATIO));
      },
      { threshold: VISIBLE_RATIO },
    );
    for (const element of elements) observer.observe(element);
    const onVisibility = () => {
      ratios.clear();
      if (document.hidden) {
        shownSince = Infinity;
        report(false);
        return;
      }
      // Observing afresh makes the observer report each box as it is now.
      shownSince = performance.now();
      for (const element of elements) {
        observer.unobserve(element);
        observer.observe(element);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      report(false);
    };
  }, [currentBoxes, onCurrentVisible, imageReady, size.page]);

  return (
    <div className="flex justify-center pt-4" style={{ height: height + GAP }}>
      <div
        className="relative bg-white shadow-[0_0_0_1px_var(--rule),0_1px_3px_rgb(0_0_0/0.08)]"
        ref={frame}
        style={{ width, height }}
        data-page={size.page}
      >
        {failed ? (
          <p role="alert" className="p-6 text-sm text-exclude">
            Page {size.page} could not load.
          </p>
        ) : page ? (
          <>
            <img
              src={page.image_url}
              alt={`Page ${size.page}`}
              className="page-image absolute inset-0 h-full w-full select-none"
              draggable={false}
              onLoad={() => setImageReady(true)}
              onError={() => {
                setImageReady(false);
                setFailed(true);
              }}
            />
            <TextLayer page={page} text={layer.text} at={at} scale={scale} />
          </>
        ) : null}
        {boxes.map(({ highlight, box, i }) => (
          <div
            key={`${highlight.id}-${i}`}
            className="evidence-box"
            data-kind={highlight.kind}
            data-testid={
              highlight.kind === "current"
                ? "source-highlight"
                : highlight.kind === "ai"
                  ? "ai-quote-highlight"
                  : undefined
            }
            title={highlight.label}
            style={{
              left: box.x0 * scale - 2,
              top: box.y0 * scale - 1,
              width: (box.x1 - box.x0) * scale + 4,
              height: (box.y1 - box.y0) * scale + 2,
            }}
          />
        ))}
      </div>
    </div>
  );
}

let canvas: CanvasRenderingContext2D | null | undefined;
const measurer = () =>
  canvas === undefined ? (canvas = document.createElement("canvas").getContext("2d")) : canvas;

function TextLayer({
  page,
  text,
  at,
  scale,
}: {
  page: PageLayer;
  text: string;
  at: (offset: number) => number;
  scale: number;
}) {
  const spans = useMemo(() => {
    const measure = measurer();
    return page.words.map((word) => {
      const content = text.slice(at(word.start), at(word.end));
      const height = (word.box.y1 - word.box.y0) * scale;
      const width = (word.box.x1 - word.box.x0) * scale;
      const size = height * 0.86;
      let stretch = 1;
      if (measure) {
        measure.font = `${size}px "IBM Plex Sans Variable", sans-serif`;
        const natural = measure.measureText(content).width;
        if (natural > 0) stretch = width / natural;
      }
      return { word, content, size, stretch };
    });
  }, [page, text, at, scale]);
  return (
    <div className="text-layer">
      {spans.map(({ word, content, size, stretch }) => (
        <span
          key={word.start}
          data-s={word.start}
          data-e={word.end}
          data-p={word.box.page}
          data-x0={word.box.x0}
          data-y0={word.box.y0}
          data-x1={word.box.x1}
          data-y1={word.box.y1}
          style={{
            left: word.box.x0 * scale,
            top: word.box.y0 * scale,
            fontSize: size,
            transform: `scaleX(${stretch})`,
          }}
        >
          {content}
        </span>
      ))}
    </div>
  );
}

/** Merges word boxes into one rectangle per line, never across columns or pages. */
const lineBoxes = (boxes: ReadonlyArray<Box>): Box[] => {
  const lines: Box[] = [];
  for (const box of [...boxes].sort((a, b) => a.page - b.page || a.y0 - b.y0 || a.x0 - b.x0)) {
    const line = lines.find(
      (l) =>
        l.page === box.page &&
        Math.abs(l.y0 - box.y0) < 3 &&
        box.x0 - l.x1 < 12 &&
        box.x0 >= l.x0 - 1,
    );
    if (line) {
      lines[lines.indexOf(line)] = {
        ...line,
        x1: Math.max(line.x1, box.x1),
        y1: Math.max(line.y1, box.y1),
      };
    } else {
      lines.push(box);
    }
  }
  return lines;
};
