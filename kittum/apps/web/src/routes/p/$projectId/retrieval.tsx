import { createFileRoute } from "@tanstack/react-router";

import { RetrievalScreen } from "../../../screens/retrieval/Retrieval.tsx";

export const Route = createFileRoute("/p/$projectId/retrieval")({ component: RetrievalScreen });
