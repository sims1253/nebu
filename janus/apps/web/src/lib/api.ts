import type {
  ApiError,
  HealthResponse,
  ReviewAnnotationRequest,
  ReviewAnnotationResponse,
  ReviewMetadata,
  ReviewProgress,
  ReviewResult,
  ValidationResponse,
} from "@janus/contracts";

/** Requests without a body (polling, fetches) get a short deadline; uploads
 * and posts get a long one so large documents are not cut off mid-transfer. */
function timeoutFor(init: RequestInit | undefined): number {
  return init?.body ? 300_000 : 15_000;
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    signal: AbortSignal.timeout(timeoutFor(init)),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: ApiError;
    } | null;
    throw new ApiClientError(response.status, body?.detail ?? null);
  }
  return response.json() as Promise<T>;
}

export class ApiClientError extends Error {
  constructor(
    readonly status: number,
    readonly apiError: ApiError | null,
  ) {
    super(apiError?.message ?? `Request failed with status ${status}`);
    this.name = "ApiClientError";
  }
}

function inputs(specification: File, reference: File): FormData {
  const form = new FormData();
  form.append("specification", specification);
  form.append("reference", reference);
  return form;
}

export const api = {
  getHealth: () => fetchJson<HealthResponse>("/health"),
  validateInputs: (specification: File, reference: File) =>
    fetchJson<ValidationResponse>("/api/reviews/validate", {
      method: "POST",
      body: inputs(specification, reference),
    }),
  createReview(document: File, specification: File, reference: File) {
    const form = inputs(specification, reference);
    form.append("document", document);
    return fetchJson<ReviewMetadata>("/api/reviews", {
      method: "POST",
      body: form,
    });
  },
  getReviews: () => fetchJson<ReviewMetadata[]>("/api/reviews"),
  getReview: (id: string) => fetchJson<ReviewMetadata>(`/api/reviews/${id}`),
  deleteReview: (id: string) =>
    fetchJson<{ review_id: string; removed_entries: string[] }>(
      `/api/reviews/${id}`,
      {
        method: "DELETE",
      },
    ),
  getReviewProgress: (id: string) =>
    fetchJson<ReviewProgress>(`/api/reviews/${id}/progress`),
  getReviewResult: (id: string) =>
    fetchJson<ReviewResult>(`/api/reviews/${id}/result`),
  getReviewDocumentUrl: (id: string) => `/api/reviews/${id}/document`,
  submitReviewAnnotation: (id: string, request: ReviewAnnotationRequest) =>
    fetchJson<ReviewAnnotationResponse>(`/api/reviews/${id}/annotations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    }),
};
