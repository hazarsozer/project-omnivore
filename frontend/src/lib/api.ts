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
import {
  clearAuth,
  getStoredJWT,
  setStoredJWT,
} from "./auth";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Request timeout in ms — overridable via NEXT_PUBLIC_API_TIMEOUT_MS (default 30s). */
const REQUEST_TIMEOUT_MS = (() => {
  const parsed = Number(process.env.NEXT_PUBLIC_API_TIMEOUT_MS);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 30_000;
})();

/** fetch() wrapper that aborts after REQUEST_TIMEOUT_MS and surfaces a clear timeout error. */
async function fetchWithTimeout(
  input: string,
  init?: RequestInit
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new APIError(
        0,
        `Request timed out after ${REQUEST_TIMEOUT_MS}ms`
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/** Build the auth header using priority: sessionStorage JWT > env API key > none */
function buildAuthHeader(): Record<string, string> {
  const jwt = getStoredJWT();
  if (jwt) return { Authorization: `Bearer ${jwt}` };

  const envKey = process.env.NEXT_PUBLIC_API_KEY;
  if (envKey) return { "X-API-Key": envKey };

  return {};
}

export class APIError extends Error {
  status: number;
  detail?: string;
  constructor(status: number, message: string, detail?: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

/** Runtime guard for the token-exchange payload — the API contract is untrusted at the boundary. */
function isTokenResponse(value: unknown): value is TokenResponse {
  return (
    value != null &&
    typeof value === "object" &&
    typeof (value as Record<string, unknown>).access_token === "string" &&
    typeof (value as Record<string, unknown>).token_type === "string" &&
    typeof (value as Record<string, unknown>).expires_in === "number"
  );
}

/** Exchange an API key for a JWT. Does NOT modify stored auth state. */
async function fetchToken(apiKey: string): Promise<TokenResponse> {
  const res = await fetchWithTimeout(`${API_BASE}/v1/auth/token`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ api_key: apiKey }),
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
      body && typeof body === "object" && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : undefined;
    throw new APIError(
      res.status,
      `HTTP ${res.status} ${res.statusText}`,
      detail
    );
  }

  const env = body as APIResponse<TokenResponse>;
  if (env && typeof env === "object" && "success" in env) {
    if (!env.success || !env.data) {
      throw new APIError(
        res.status,
        env.error?.message ?? "Token exchange failed",
        env.error?.code
      );
    }
    if (!isTokenResponse(env.data)) {
      throw new APIError(res.status, "Malformed token response from server");
    }
    return env.data;
  }

  // Direct (non-enveloped) response from the token endpoint
  if (!isTokenResponse(body)) {
    throw new APIError(res.status, "Malformed token response from server");
  }
  return body;
}

/** Silent JWT refresh is disabled — raw API key is never persisted in the browser.
 *  On 401, the user is redirected to the login page to re-enter their API key. */
async function tryRefreshJWT(): Promise<boolean> {
  return false;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const doRequest = async (): Promise<Response> => {
    return fetchWithTimeout(`${API_BASE}${path}`, {
      ...init,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...buildAuthHeader(),
        ...(init?.headers ?? {}),
      },
    });
  };

  let res = await doRequest();

  // Auto-refresh: if 401 and we have a stored API key, try getting a fresh JWT
  if (res.status === 401) {
    const refreshed = await tryRefreshJWT();
    if (refreshed) {
      res = await doRequest();
    }
    if (res.status === 401) {
      clearAuth();
      throw new APIError(401, "Unauthorized — please sign in again");
    }
  }

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
    if (env.data == null) {
      throw new APIError(res.status, "Unexpected empty response data");
    }
    return env.data as T;
  }
  return body as T;
}

export const api = {
  health: () => request<{ status: string; service: string }>("/v1/health"),
  ready: () => request<Readiness>("/v1/ready"),
  handlers: () => request<Handler[]>("/v1/handlers"),

  /** Exchange an API key for a short-lived JWT. */
  exchangeToken: (apiKey: string): Promise<TokenResponse> =>
    fetchToken(apiKey),

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

      // Apply auth header with same priority as request()
      const authHeader = buildAuthHeader();
      for (const [key, value] of Object.entries(authHeader)) {
        xhr.setRequestHeader(key, value);
      }

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
      };

      xhr.onload = async () => {
        try {
          // Handle 401 with refresh on XHR path
          if (xhr.status === 401) {
            const refreshed = await tryRefreshJWT();
            if (refreshed) {
              // Re-issue request with fresh token
              api
                .uploadDocument(file, onProgress)
                .then(resolve)
                .catch(reject);
              return;
            }
            clearAuth();
            reject(new APIError(401, "Unauthorized — please sign in again"));
            return;
          }

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
        } catch (err) {
          reject(err instanceof Error ? err : new APIError(0, String(err)));
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
