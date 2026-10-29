// The AI judge's reading of one criterion, shown only after the person has decided: which
// criterion, its verdict, why, and the quote it relied on. A quote the judge verified is marked
// in the record; one it did not verify is the model's own words and is never marked.
import type { CriterionStatus } from "../../data/schema.ts";
import { Status, VERDICTS } from "../../ui/Status.tsx";

export interface CriterionName {
  /** The criterion's number on the screen, 1-based, or null if the screen has no such criterion. */
  readonly number: number | null;
  readonly label: string;
  readonly text: string | null;
}

export function AiReading({
  criterion,
  status,
  downgradedFrom,
  rationale,
  quote,
  verified,
  markedAt,
  marked = true,
  compact,
}: {
  criterion: CriterionName;
  status: CriterionStatus;
  downgradedFrom: CriterionStatus | null | undefined;
  rationale: string;
  quote: string | null;
  verified: boolean;
  /** Where the verified quote is marked, as a sentence ("Marked in the abstract."), or null if
   * Kittum could not place it on screen. */
  markedAt: string | null;
  /** The quote is the one marked in the record now, so it shares the marker. */
  marked?: boolean;
  compact?: boolean;
}) {
  return (
    <div
      className={compact ? "grid gap-0.5 text-xs" : "grid gap-1 text-sm"}
      data-testid="ai-reading"
    >
      <p className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-ink-2" data-testid="ai-criterion">
          {criterion.number !== null ? `${criterion.number} ` : ""}
          {criterion.label}
        </span>
        <Status term={VERDICTS[status]} />
        {downgradedFrom ? (
          <span className="text-ink-3">
            The model said {VERDICTS[downgradedFrom].label.toLowerCase()}, but its quote failed
            Kittum&rsquo;s check. Kittum counts the reading as unclear.
          </span>
        ) : null}
      </p>
      {criterion.text && !compact ? (
        <p className="max-w-[80ch] text-xs text-ink-3">{criterion.text}</p>
      ) : null}
      <p className="max-w-[80ch] text-ink" data-testid="ai-rationale">
        {rationale}
      </p>
      <QuoteLine quote={quote} verified={verified} markedAt={markedAt} marked={marked} />
    </div>
  );
}

function QuoteLine({
  quote,
  verified,
  markedAt,
  marked,
}: {
  quote: string | null;
  verified: boolean;
  markedAt: string | null;
  marked: boolean;
}) {
  if (!quote) {
    return (
      <p className="text-ink-3" data-testid="ai-quote" data-verified="none">
        The AI gave no quote.
      </p>
    );
  }
  const flat = quote.replace(/\s+/g, " ");
  if (!verified) {
    return (
      <p className="max-w-[80ch] text-ink-3" data-testid="ai-quote" data-verified="false">
        The AI quoted &ldquo;{flat}&rdquo;. This text is not in the record, so Kittum does not mark
        it.
      </p>
    );
  }
  return (
    <p className="max-w-[80ch] text-ink-2" data-testid="ai-quote" data-verified="true">
      <span className={marked && markedAt ? "evidence-text" : "text-ink"}>
        &ldquo;{flat}&rdquo;
      </span>{" "}
      {markedAt ?? "Kittum could not find this quote on screen."}
    </p>
  );
}
