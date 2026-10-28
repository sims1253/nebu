// Keycaps. Every action shows its key; a hint is the key plus the action's name.
import type { ReactNode } from "react";

// Plex has no erase-left glyph, so Backspace is drawn.
const backspace = (
  <svg width="15" height="10" viewBox="0 0 15 10" aria-label="Backspace" role="img">
    <path
      d="M4.5 0.75h9.25v8.5H4.5L0.9 5z"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
    />
    <path
      d="M7.4 3l4 4M11.4 3l-4 4"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
    />
  </svg>
);

const GLYPHS: Record<string, ReactNode> = {
  " ": "Space",
  Backspace: backspace,
  Enter: "Enter",
  Escape: "Esc",
  ArrowUp: "↑",
  ArrowDown: "↓",
  Tab: "Tab",
};

export function Key({ k, on, className }: { k: string; on?: boolean; className?: string }) {
  return (
    <kbd className={`key ${className ?? ""}`} data-on={on || undefined}>
      <span>{GLYPHS[k] ?? k}</span>
    </kbd>
  );
}

/** Keys pressed in sequence ("e" then "1") render with a thin "then" between them. */
export function KeyHint({
  keys,
  children,
  on,
  muted,
}: {
  keys: ReadonlyArray<string>;
  children: ReactNode;
  on?: boolean;
  muted?: boolean;
}) {
  return (
    <span className={`inline-flex items-center gap-1.5 ${muted ? "text-ink-3" : "text-ink-2"}`}>
      <span className="inline-flex items-center gap-0.5">
        {keys.map((k, i) => (
          <Key key={`${k}-${i}`} k={k} on={on ?? false} />
        ))}
      </span>
      <span>{children}</span>
    </span>
  );
}
