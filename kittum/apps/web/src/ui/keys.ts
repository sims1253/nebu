// Keyboard handling. Each screen registers one handler; typing in a field or an open dialog
// never triggers screen keys.
import { useEffect, useEffectEvent } from "react";

const typing = (target: EventTarget | null) =>
  target instanceof HTMLElement &&
  (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));

const blocked = (event: KeyboardEvent) =>
  event.defaultPrevented ||
  event.metaKey ||
  event.ctrlKey ||
  event.altKey ||
  typing(event.target) ||
  document.querySelector("[role=dialog][data-open]") !== null;

/** Calls `handler` for keys on the page; returning true marks the key as handled. */
export function useKeys(handler: (event: KeyboardEvent) => boolean | void) {
  const onKey = useEffectEvent((event: KeyboardEvent) => {
    if (blocked(event)) return;
    if (handler(event)) event.preventDefault();
  });
  useEffect(() => {
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
