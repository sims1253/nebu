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
      setError(failure(e, "Kittum could not build the corpus."));
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
          ? `Kittum merges the ${searches} ${searches === 1 ? "search" : "searches"} into records and ${plural(labels.study).toLowerCase()}. It proposes records with the same DOI or PMID as one record, and records with the same trial ID as one ${labels.study.toLowerCase()}. You confirm each link afterwards. Build the corpus once every search is in, because Kittum cannot add a later search to it yet.`
          : "Run or import the searches first. Kittum builds the corpus from them."}
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
