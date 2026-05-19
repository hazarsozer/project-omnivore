import type {
  APIResponse,
  DocumentDetail,
  DocumentListItem,
  Entity,
  Handler,
  Readiness,
  SearchMode,
  SearchResult,
  UploadResponse,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class APIError extends Error {
  status: number;
  detail?: string;
  constructor(status: number, message: string, detail?: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });

  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!res.ok) {
    const detail =
      (body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : undefined) ??
      (body && typeof body === "object" && "error" in body
        ? String(
            (body as { error: { message?: string } }).error?.message ?? ""
          )
        : undefined);
    throw new APIError(res.status, `HTTP ${res.status} ${res.statusText}`, detail);
  }

  const env = body as APIResponse<T>;
  if (env && typeof env === "object" && "success" in env) {
    if (!env.success) {
      throw new APIError(
        res.status,
        env.error?.message ?? "Unknown error",
        env.error?.code
      );
    }
    return env.data as T;
  }
  return body as T;
}

export const api = {
  health: () => request<{ status: string; service: string }>("/v1/health"),
  ready: () => request<Readiness>("/v1/ready"),
  handlers: () => request<Handler[]>("/v1/handlers"),

  listDocuments: (params: { status?: string; limit?: number; cursor?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.cursor) qs.set("cursor", params.cursor);
    const q = qs.toString();
    return request<DocumentListItem[]>(`/v1/documents${q ? `?${q}` : ""}`);
  },

  getDocument: (id: string) => request<DocumentDetail>(`/v1/documents/${id}`),

  getEntities: (id: string) =>
    request<Entity[]>(`/v1/documents/${id}/entities`),

  retryDocument: (id: string) =>
    request<UploadResponse>(`/v1/documents/${id}/retry`, { method: "POST" }),

  uploadDocument: async (
    file: File,
    onProgress?: (loaded: number, total: number) => void
  ): Promise<UploadResponse> => {
    // XHR for progress events — fetch doesn't expose upload progress yet.
    return await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_BASE}/v1/documents`);

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
      };

      xhr.onload = () => {
        let parsed: unknown = null;
        try {
          parsed = JSON.parse(xhr.responseText);
        } catch {
          /* keep null */
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          const env = parsed as APIResponse<UploadResponse> | null;
          if (env?.success && env.data) {
            resolve(env.data);
          } else {
            reject(new APIError(xhr.status, env?.error?.message ?? "Upload failed"));
          }
        } else {
          const detail =
            (parsed && typeof parsed === "object" && "detail" in parsed
              ? String((parsed as { detail: unknown }).detail)
              : undefined) ?? xhr.statusText;
          reject(new APIError(xhr.status, `Upload failed: ${detail}`, detail));
        }
      };
      xhr.onerror = () => reject(new APIError(0, "Network error"));
      xhr.onabort = () => reject(new APIError(0, "Upload aborted"));

      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });
  },

  search: (body: { query: string; mode: SearchMode; top_k: number }) =>
    request<SearchResult[]>("/v1/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};
