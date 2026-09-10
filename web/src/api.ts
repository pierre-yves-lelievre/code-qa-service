/** The JSON API: response types mirroring app/schemas.py, and a fetch wrapper for {detail, code}. */

// ── Repositories ──────────────────────────────────────────────────────────────

export interface RepoHit {
  full_name: string;
  owner: string;
  name: string;
  description: string | null;
  stars: number;
  language: string | null;
  size_kb: number;
  default_branch: string;
  url: string;
  clone_url: string;
}

export interface RepoSearchResponse {
  items: RepoHit[];
}

export interface LanguageStats {
  files: number;
  symbols: number;
  chunks: number;
}

export interface EmbeddingStats {
  model: string;
  embedded: number;
  reused: number;
  tokens_estimated: number;
  tokens: number;
  cost_usd: number;
}

/** `SnapshotInfo.stats`, a dict on the server; the keys app/indexing.py writes. */
export interface IndexStats {
  files?: number;
  chunks?: number;
  seconds?: number;
  by_mode?: Record<string, number>;
  by_language?: Record<string, LanguageStats>;
  skipped?: Record<string, number>;
  files_with_parse_errors?: number;
  embedding?: EmbeddingStats;
  summary?: { status: "ok" | "failed"; input_tokens?: number; output_tokens?: number };
}

export interface SnapshotInfo {
  snapshot_id: number;
  commit_sha: string | null;
  branch: string;
  indexed_at: string | null;
  stats: IndexStats;
}

export interface RepoResponse {
  repo_id: number;
  owner: string;
  name: string;
  url: string;
  summary: string | null;
  suggested_questions: string[] | null;
  snapshot: SnapshotInfo | null;
}

// ── Ask ───────────────────────────────────────────────────────────────────────

export interface AskRequest {
  repo_id: number;
  question: string;
  conversation_id?: string | null;
}

export type Tier = "symbol" | "fts" | "vector";

export interface Source {
  path: string;
  start_line: number;
  end_line: number;
  kind: string;
  qualname: string | null;
  tier: Tier | null;
  excerpt: string;
  github_url: string;
}

export interface LegTrace {
  status: "ok" | "or_fallback" | "empty" | "skipped" | "unavailable";
  hits: number;
  ms: number;
}

export interface Retrievers {
  symbol: LegTrace;
  fts: LegTrace;
  vector: LegTrace;
  planner: "ok" | "fallback";
}

export interface Timings {
  plan_ms: number;
  embed_ms: number;
  retrieve_ms: number;
  llm_ms: number;
  total_ms: number;
}

export interface Tokens {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
}

export interface SnapshotRef {
  commit_sha: string;
  indexed_at: string | null;
}

export interface AskResponse {
  conversation_id: string;
  answer: string;
  not_found: boolean;
  sources: Source[];
  retrievers: Retrievers;
  timings: Timings;
  tokens: Tokens;
  snapshot: SnapshotRef;
  notes: string[];
}

// ── Index ─────────────────────────────────────────────────────────────────────

export interface IndexRequest {
  url: string;
}

export interface IndexAccepted {
  job_id: string;
  repo_id: number;
  owner: string;
  name: string;
  branch: string;
  status: "pending";
}

/** `JobResponse.progress`, a dict on the server; the keys each stage of app/indexing.py writes. */
export interface JobProgress {
  stage?: "cloning" | "walking" | "parsing" | "embedding" | "summarizing" | "done";
  files_done?: number;
  files_total?: number;
  chunks?: number;
  chunks_done?: number;
  chunks_total?: number;
  tokens?: number;
  files?: number;
  already_indexed?: boolean;
}

export interface JobResponse {
  job_id: string;
  repo_id: number;
  status: "pending" | "running" | "succeeded" | "failed";
  progress: JobProgress;
  error: string | null;
  snapshot_id: number | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

// ── Client ────────────────────────────────────────────────────────────────────

export interface ErrorResponse {
  detail: string;
  code: string;
}

/** A failed call: the HTTP status (0 when unreachable) and the server's detail and code. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly code: string;

  constructor(status: number, detail: string, code: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

/** Call the API and return its JSON; a non-2xx answer throws ApiError from `{detail, code}`. */
export async function api<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(0, "The service is unreachable.", "network_error");
  }
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = (data ?? {}) as Partial<ErrorResponse>;
    throw new ApiError(
      response.status,
      error.detail ?? `HTTP ${response.status}`,
      error.code ?? "http_error",
    );
  }
  return data as T;
}

/** One line for an inline error: "detail (code)". */
export function errorText(error: unknown): string {
  return error instanceof ApiError ? `${error.detail} (${error.code})` : "Unexpected error.";
}
