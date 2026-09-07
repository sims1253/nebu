import type {
  CheckResult,
  ExtractionSummary,
  SavedExtraction,
} from "@janus/contracts";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/extractions${path}`, init);
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    const detail = body?.detail;
    if (Array.isArray(detail)) {
      throw new Error(
        detail
          .map(
            (entry: { path?: string; message?: string; msg?: string }) =>
              `${entry.path ?? "Input"}: ${entry.message ?? entry.msg}`,
          )
          .join("\n"),
      );
    }
    throw new Error(
      typeof detail === "string"
        ? detail
        : `Request failed (${response.status}).`,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}

export const extractions = {
  list: () => request<ExtractionSummary[]>(""),
  get: (id: string) => request<SavedExtraction>(`/${id}`),
  specification: (id: string) =>
    request<Record<string, unknown>>(`/${id}/specification`),
  create(document: File, specification: File) {
    const body = new FormData();
    body.append("document", document);
    body.append("specification", specification);
    return request<SavedExtraction>("", { method: "POST", body });
  },
  remove: (id: string) => request<void>(`/${id}`, { method: "DELETE" }),
  compare(id: string, reference: File, rules: string) {
    const body = new FormData();
    body.append("reference", reference);
    body.append("rules", rules);
    return request<CheckResult[]>(`/${id}/compare`, { method: "POST", body });
  },
};

export function atPointer(data: unknown, pointer: string): unknown {
  return pointer
    .split("/")
    .slice(1)
    .reduce<unknown>((value, key) => {
      const token = key.replaceAll("~1", "/").replaceAll("~0", "~");
      return value != null && typeof value === "object"
        ? (value as Record<string, unknown>)[token]
        : undefined;
    }, data);
}

export function saveJson(value: unknown, filename: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
