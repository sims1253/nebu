// A screen that is designed but not built yet: what it will show, its keys, and what it waits on.
import type { ProfileLabels } from "../data/schema.ts";
import { KeyHint } from "../ui/Key.tsx";
import { useProject } from "./project.tsx";

export function Placeholder({
  title,
  shows,
  keys,
  waitsOn,
}: {
  title: string;
  /** What the screen will show, in the words of the project's profile. */
  shows: (labels: ProfileLabels) => ReadonlyArray<string>;
  keys: ReadonlyArray<readonly [ReadonlyArray<string>, string]>;
  waitsOn: string;
}) {
  const { labels } = useProject();
  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[720px] px-8 py-14">
        <p className="text-sm text-ink-3">Not built yet</p>
        <h1 className="mt-1 text-xl font-semibold tracking-[-0.01em]">{title}</h1>
        <h2 className="mt-8 text-sm font-semibold text-ink-2">It will show</h2>
        <ul className="mt-2 grid gap-1.5 text-md text-ink-2">
          {shows(labels).map((line) => (
            <li key={line} className="border-l-2 border-rule pl-3">
              {line}
            </li>
          ))}
        </ul>
        {keys.length > 0 ? (
          <>
            <h2 className="mt-8 text-sm font-semibold text-ink-2">Keys</h2>
            <div className="mt-2 flex flex-wrap gap-x-5 gap-y-2">
              {keys.map(([k, label]) => (
                <KeyHint key={label} keys={k}>
                  {label}
                </KeyHint>
              ))}
            </div>
          </>
        ) : null}
        <p className="mt-10 border-t border-rule pt-4 text-xs text-ink-3">{waitsOn}</p>
      </div>
    </section>
  );
}
