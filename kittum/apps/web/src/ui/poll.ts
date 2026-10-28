// Re-reads a job's state while it runs: the server has no push, and a run takes seconds to
// minutes, so once a second is enough to show progress without loading the server.
import { useEffect } from "react";

export function usePoll(active: boolean, tick: () => void, ms = 1000) {
  useEffect(() => {
    if (!active) return;
    const id = setInterval(tick, ms);
    return () => clearInterval(id);
  }, [active, tick, ms]);
}
