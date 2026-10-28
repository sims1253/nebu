// Light and dark. The reviewer's choice is remembered; until then the system setting decides.
import { useCallback, useSyncExternalStore } from "react";

type Theme = "light" | "dark";
const KEY = "kittum.theme";
const listeners = new Set<() => void>();

const current = (): Theme =>
  document.documentElement.dataset["theme"] === "dark" ? "dark" : "light";

export const applyStoredTheme = () => {
  const stored = localStorage.getItem(KEY);
  const theme =
    stored === "dark" || stored === "light"
      ? stored
      : window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
  document.documentElement.dataset["theme"] = theme;
};

export function useTheme() {
  const theme = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    current,
    () => "light" as Theme,
  );
  const toggle = useCallback(() => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.dataset["theme"] = next;
    localStorage.setItem(KEY, next);
    for (const listener of listeners) listener();
  }, []);
  return { theme, toggle };
}
