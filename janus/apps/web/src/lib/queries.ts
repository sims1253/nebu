import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReviewAnnotationRequest, ReviewStatus } from "@janus/contracts";
import { api } from "./api";

export const queryKeys = {
  health: ["health"] as const,
  reviews: ["reviews"] as const,
  review: (id: string) => ["review", id] as const,
  result: (id: string) => ["reviewResult", id] as const,
};

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: api.getHealth,
    retry: 2,
  });
}

export function useReviews() {
  return useQuery({
    queryKey: queryKeys.reviews,
    queryFn: api.getReviews,
    // Poll only while reviews are actually processing; when idle the list is
    // refreshed by invalidations and window focus instead of a forever timer.
    refetchInterval: (query) =>
      query.state.data?.some(
        (item) => !["ready", "error"].includes(item.status),
      )
        ? 2000
        : false,
  });
}

export function useReview(id: string) {
  return useQuery({
    queryKey: queryKeys.review(id),
    queryFn: () => api.getReview(id),
    refetchInterval: (query) =>
      ["ready", "error"].includes(query.state.data?.status ?? "")
        ? false
        : 1000,
  });
}

export function useReviewResult(id: string, reviewStatus?: ReviewStatus) {
  return useQuery({
    queryKey: queryKeys.result(id),
    queryFn: () => api.getReviewResult(id),
    enabled: reviewStatus === "ready",
    staleTime: 0,
  });
}

export function useCreateReview() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      document,
      specification,
      reference,
    }: {
      document: File;
      specification: File;
      reference: File;
    }) => api.createReview(document, specification, reference),
    onSuccess: (review) => {
      client.setQueryData(queryKeys.review(review.id), review);
      void client.invalidateQueries({ queryKey: queryKeys.reviews });
    },
    meta: { errorTitle: "Could not start the review" },
  });
}

export function useDeleteReview() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.deleteReview,
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: queryKeys.reviews }),
    meta: { errorTitle: "Could not delete the review" },
  });
}

export function useSubmitReviewAnnotation(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: ReviewAnnotationRequest) =>
      api.submitReviewAnnotation(id, request),
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: queryKeys.result(id) }),
    meta: { errorTitle: "Could not save the annotation" },
  });
}
