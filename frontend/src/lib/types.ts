export type DocStatus =
  | "queued"
  | "routing"
  | "extracting"
  | "enriching"
  | "indexed"
  | "failed"
  | "duplicate";

export interface APIResponse<T> {
  success: boolean;
  data: T | null;
  error: { code: string; message: string } | null;
  meta: Record<string, unknown> | null;
}

export interface DocumentListItem {
  document_id: string;
  filename: string;
  mime_type: string;
  status: DocStatus;
  created_at: string | null;
}

export interface DocumentSummary {
  title?: string | null;
  abstract?: string | null;
  key_points?: string[] | null;
  topics?: string[] | null;
  [k: string]: unknown;
}

export interface DocumentDetail {
  document_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  status: DocStatus;
  handler: string | null;
  created_at: string | null;
  indexed_at: string | null;
  error: { reason?: string; retry_payload?: unknown } | null;
  metadata: Record<string, unknown> | null;
  summary: DocumentSummary | null;
  routing_decision: { policy?: string; sink_counts?: Record<string, number> } | null;
}

export interface Entity {
  label: string;
  value: string;
  normalized: string | null;
  confidence: number | null;
}

export interface UploadResponse {
  document_id: string;
  status: DocStatus;
  poll_url: string;
}

export type SearchMode = "bm25" | "vector" | "hybrid";

export interface SearchResult {
  chunk_id: string;
  document_id: string;
  content: string;
  heading_path: string[] | null;
  score: number;
  token_count: number;
}

export interface Handler {
  name: string;
  version: string;
  accepts: string[];
  cost_class: "io" | "cpu" | "gpu";
  timeout_seconds: number;
}

export interface Readiness {
  postgres: string;
  redis: string;
  minio: string;
}
