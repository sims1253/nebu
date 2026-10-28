// The status vocabulary. A status shows as the key letter that sets it, in its colour, with a
// word beside it wherever there is room. Colour never carries the meaning alone.
import type { ReactNode } from "react";

import type { CriterionStatus, Outcome, ProposalStatus } from "../data/schema.ts";

type Tone = "include" | "exclude" | "unclear" | "insufficient" | "neutral" | "petrol";

const TONES: Record<Tone, string> = {
  include: "bg-include-wash text-include",
  exclude: "bg-exclude-wash text-exclude",
  unclear: "bg-unclear-wash text-unclear",
  insufficient: "bg-insufficient-wash text-insufficient",
  neutral: "bg-sheet-2 text-ink-2 shadow-[inset_0_0_0_1px_var(--rule-strong)]",
  petrol: "bg-petrol-wash text-petrol",
};

const TONE_TEXT: Record<Tone, string> = {
  include: "text-include",
  exclude: "text-exclude",
  unclear: "text-unclear",
  insufficient: "text-insufficient",
  neutral: "text-ink-2",
  petrol: "text-petrol",
};

interface Term {
  readonly glyph: ReactNode;
  readonly label: string;
  readonly tone: Tone;
}

export const OUTCOMES: Record<Outcome, Term & { readonly glyph: string; readonly past: string }> = {
  include: { glyph: "i", label: "Include", past: "Included", tone: "include" },
  exclude: { glyph: "e", label: "Exclude", past: "Excluded", tone: "exclude" },
  unclear: { glyph: "u", label: "Unclear", past: "Marked unclear", tone: "unclear" },
  insufficient_information: {
    glyph: "x",
    label: "Insufficient information",
    past: "Marked insufficient information",
    tone: "insufficient",
  },
};

export const VERDICTS: Record<CriterionStatus, Term> = {
  met: { glyph: "y", label: "Met", tone: "include" },
  not_met: { glyph: "n", label: "Not met", tone: "exclude" },
  unclear: { glyph: "?", label: "Unclear", tone: "unclear" },
};

export const PROPOSAL_STATUS: Record<ProposalStatus, Term> = {
  extracted: { glyph: "·", label: "Extracted", tone: "neutral" },
  missing: { glyph: "–", label: "Missing", tone: "insufficient" },
  ambiguous: { glyph: "?", label: "Ambiguous", tone: "unclear" },
  invalid: { glyph: "!", label: "Invalid", tone: "exclude" },
  error: { glyph: "!", label: "Error", tone: "exclude" },
};

// Plex has no check mark, so it is drawn.
const check = (
  <svg width="10" height="8" viewBox="0 0 10 8" aria-hidden="true">
    <path
      d="M1 4.2l2.6 2.4L9 1"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export const ADOPTED: Term = { glyph: check, label: "Adopted as evidence", tone: "petrol" };
export const ACCEPTED: Term = { glyph: check, label: "Accepted", tone: "include" };

export function Mark({ term, title }: { term: Term; title?: string }) {
  return (
    <span className={`mark ${TONES[term.tone]}`} title={title ?? term.label} aria-hidden="true">
      {term.glyph}
    </span>
  );
}

/** A mark with its word, for places with room. */
export function Status({ term, children }: { term: Term; children?: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${TONE_TEXT[term.tone]}`}>
      <Mark term={term} />
      <span>{children ?? term.label}</span>
    </span>
  );
}
