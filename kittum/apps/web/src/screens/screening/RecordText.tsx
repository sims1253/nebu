// Title and abstract with protocol terms, entities, and (after a reveal) the AI's quoted span.
import type { ReactNode } from "react";

import type { Highlight } from "../../data/schema.ts";
import { codePointIndex } from "../../lib/codepoints.ts";

interface Run {
  readonly start: number;
  readonly end: number;
  readonly criteria: ReadonlyArray<string>;
  readonly entities: ReadonlyArray<string>;
  readonly quote: boolean;
}

/**
 * Splits `text[from, to)` (UTF-16) at every highlight boundary. Highlights arrive in code points
 * and are converted here; `quote` is a UTF-16 range.
 */
const runs = (
  text: string,
  from: number,
  to: number,
  highlights: ReadonlyArray<Highlight>,
  quote: readonly [number, number] | null,
): Run[] => {
  const at = codePointIndex(text);
  const marks = highlights
    .map(([s, e, label]) => [at(s), at(e), label] as const)
    .filter(([s, e]) => s < to && e > from);
  const cuts = new Set([from, to]);
  for (const [s, e] of marks) {
    cuts.add(Math.max(s, from));
    cuts.add(Math.min(e, to));
  }
  if (quote && quote[0] < to && quote[1] > from) {
    cuts.add(Math.max(quote[0], from));
    cuts.add(Math.min(quote[1], to));
  }
  const points = [...cuts].sort((a, b) => a - b);
  const out: Run[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i] ?? 0;
    const end = points[i + 1] ?? 0;
    const covering = marks.filter(([s, e]) => s <= start && e >= end).map(([, , label]) => label);
    out.push({
      start,
      end,
      criteria: covering.filter((l) => l.startsWith("protocol:")).map((l) => l.slice(9)),
      entities: covering.filter((l) => l.startsWith("entity:")).map((l) => l.slice(7)),
      quote: quote !== null && quote[0] <= start && quote[1] >= end,
    });
  }
  return out;
};

export function Marked({
  text,
  from = 0,
  to = text.length,
  highlights,
  quote,
  describe,
}: {
  text: string;
  from?: number;
  to?: number;
  highlights: ReadonlyArray<Highlight>;
  quote: readonly [number, number] | null;
  describe: (criterionId: string) => string;
}) {
  const nodes: ReactNode[] = [];
  let quoted: ReactNode[] = [];
  const flush = () => {
    if (quoted.length === 0) return;
    // One mark for the whole quote, however many terms it crosses.
    nodes.push(
      <mark key={`q${nodes.length}`} className="evidence" data-reveal-quote="">
        {quoted}
      </mark>,
    );
    quoted = [];
  };
  for (const run of runs(text, from, to, highlights, quote)) {
    const slice = text.slice(run.start, run.end);
    const classes = [run.criteria.length ? "term" : "", run.entities.length ? "entity" : ""].filter(
      Boolean,
    );
    const title = [...run.criteria.map(describe), ...run.entities.map((e) => `Entity: ${e}`)].join(
      "\n",
    );
    const node: ReactNode = classes.length ? (
      <span key={`s${run.start}`} className={classes.join(" ")} title={title}>
        {slice}
      </span>
    ) : (
      slice
    );
    if (run.quote) {
      quoted.push(node);
    } else {
      flush();
      nodes.push(node);
    }
  }
  flush();
  return <>{nodes}</>;
}

/** Locates the AI's quote: in the title, the abstract, or nowhere (never guessed). */
export const locateQuote = (title: string, text: string, quote: string | null) => {
  if (!quote) return { title: null, text: null };
  const inTitle = title.indexOf(quote);
  if (inTitle >= 0) return { title: [inTitle, inTitle + quote.length] as const, text: null };
  const inText = text.indexOf(quote);
  return { title: null, text: inText >= 0 ? ([inText, inText + quote.length] as const) : null };
};

const METHODS = /method|design|participants|patients and/i;
const TRAILING = /funding|registration|translation|impact and/i;

/** Structured-abstract sections in reading order for screening: methods first, funding last. */
export const orderSections = (sections: ReadonlyArray<readonly [string, number, number]>) => {
  const rank = (label: string) => (METHODS.test(label) ? 0 : TRAILING.test(label) ? 2 : 1);
  return sections
    .map((section, index) => ({ section, index }))
    .sort((a, b) => rank(a.section[0]) - rank(b.section[0]) || a.index - b.index)
    .map(({ section }) => ({
      label: section[0],
      start: section[1],
      end: section[2],
      trailing: rank(section[0]) === 2,
    }));
};
