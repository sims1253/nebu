// Builds the corpus: every search, deduplicated into the first identity revision. Screening
// opens on this revision, so the lead builds it once the searches are in.
import { useRouteContext, useRouter } from "@tanstack/react-router";
import { useState } from "react";

import { run } from "../../data/workbench.ts";
import { Button, failure } from "../../ui/Form.tsx";
import { useKeys } from "../../ui/keys.ts";
import { plural, useProject } from "../project.tsx";

export function BuildCorpus({
  searches,
  onBuilt,
}: {
  /** How many searches there are; null while unknown. */
  searches: number | null;
  onBuilt: () => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const router = useRouter();
  const { project, me, labels } = useProject();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = (searches ?? 0) > 0;

  const build = async () => {
    setBusy(true);
    setError(null);
    try {
      await run(workbench.ingestCorpus({ projectId: project.id, as: me.actor_id }));
      await router.invalidate();
      onBuilt();
    } catch (e) {
      setError(failure(e, "The corpus was not built."));
    } finally {
      setBusy(false);
    }
  };

  useKeys((event) => {
    if (event.key !== "b" || !ready || busy) return false;
    void build();
    return true;
  });

  return (
    <div className="mt-4 max-w-[64ch]">
      <p className="text-md text-ink-2">
        {ready
          ? `Kittum deduplicates the ${searches} ${searches === 1 ? "search" : "searches"} into records and ${plural(labels.study).toLowerCase()}: identical DOIs and PMIDs are proposed as one record, shared trial IDs as one ${labels.study.toLowerCase()}. You confirm each link afterwards. Build it once every search is in: later searches need the update diff, which is not built yet.`
          : "Run or import the searches first; the corpus is built from them."}
      </p>
      <div className="mt-4 flex items-center gap-4">
        <Button primary keys="b" disabled={!ready || busy} onClick={() => void build()}>
          {busy ? "Building the corpus" : "Build the corpus"}
        </Button>
      </div>
      {error ? (
        <p role="alert" className="mt-3 text-sm text-exclude">
          {error}
        </p>
      ) : null}
    </div>
  );
}
