# CLAUDE.md — code-qa-service

Code Q&A over a public GitHub repository. FastAPI + Postgres/pgvector + tree-sitter
+ Voyage `voyage-code-4` + Claude Sonnet 5. React (Vite) single page served by FastAPI.

`PLAN.md` says **what** to build and in what order. This file says **how** we work.
When they disagree, ask; do not guess.

## Commands

| Command | Purpose |
|---|---|
| `make install` | `uv sync --all-groups` |
| `make db` | `docker compose up -d db` (Postgres + pgvector) |
| `make run` | uvicorn with `--reload` on :8000 (needs `make db`) |
| `make test` | pytest (unit + db + api; db tests need `make db`) |
| `make lint` / `make format` | ruff check / ruff format |
| `make audit` | `pip-audit` over the locked dependencies |
| `make check` | lint + format `--check` + test — **run before every commit** |
| `make eval` | golden set against the demo repo (needs real keys); `ARGS=--reindex` to force |
| `make smoke` | end-to-end on a tiny fixture repo with real keys |
| `make web` | build the SPA into `app/static/` |
| `docker compose up --build` | full stack |

## Layout — flat package, one concern per module

```
app/
├── main.py           # FastAPI app, lifespan (migrations, pool), request_id middleware, SPA mount
├── api.py            # ALL routes: /health /repos/search /repos/{id} /index /index/{job_id} /ask
├── config.py         # Settings via pydantic-settings; `settings` singleton
├── errors.py         # ServiceError hierarchy + the two FastAPI handlers
├── schemas.py        # Pydantic request/response models, with examples
├── logging_setup.py  # structlog JSON + request_id contextvar   (shared, unchanged)
├── db.py             # psycopg pool, migrations runner
├── migrations/       # 0001_init.sql, 0002_conversations.sql; numbered, append-only
├── jobs.py           # Job dataclass + Postgres JobStore (create/update/get/count_active)
├── github.py         # GitHubClient: search(), shallow_clone(); strict URL/branch validation
├── parsing.py        # tree-sitter: LANGUAGES table, queries/*.scm, file_symbols() -> rows
├── queries/          # python.scm, javascript.scm (shared by js/ts/tsx), typescript.scm (TS-only)
├── chunking.py       # rows -> chunks: symbol rules, windows, parts, content hash
├── embeddings.py     # VoyageEmbeddings, FakeEmbeddings; count_tokens, batching, backoff
├── llm.py            # ClaudeLLM (thin SDK wrapper), FakeLLM; structured(), complete()
├── store.py          # ChunkStore: upserts, snapshot activate/sweep, 3 retrieval queries
├── planning.py       # one structured-output call: standalone query, identifiers, intent
├── retrieval.py      # split_identifiers(), rrf(), collapse_parts(), floor — pure; plus the
│                     #   leg runner and retrieve(), the only I/O there (per-leg isolation)
├── answering.py      # briefing (two-tier), cached history, Claude call, citations, validity check
├── summary.py        # one Claude call per index run: repo summary + suggested questions
└── indexing.py       # _run_index(): clone -> parse -> chunk -> embed -> summary -> activate
web/                  # Vite + React + Tailwind + TypeScript, built into app/static (never committed)
├── src/api.ts        #   fetch wrapper ({detail, code} -> ApiError) and types mirroring schemas.py
└── src/components/   #   RepoSearch, IndexProgress, Chat, Sources, Trace; one file each
evals/                # golden.json, run_evals.py
tests/                # conftest.py, test_parsing.py, test_chunking.py, test_retrieval.py,
                      # test_store.py, test_jobs.py, test_db.py, test_github.py,
                      # test_embeddings.py, test_llm.py, test_planning.py,
                      # test_answering.py, test_summary.py, test_api.py, test_evals.py,
                      # fixtures/
```

## Style rules

1. **One module per concern.** Domain logic is plain functions. Classes only where there is state
   (`JobStore`, `ChunkStore`, `GitHubClient`, `VoyageEmbeddings`).
2. **No abstract base classes or Protocols** until a second implementation exists. Concrete classes
   with a small, stable method set. (`FakeEmbeddings` and `FakeLLM` are test doubles, not a reason
   for a Protocol.)
3. **Every function and class has a one-line docstring.** Section dividers look like
   `# ── Routes ─────────────────────────────`.
4. **Pydantic at the boundaries only**: `schemas.py` and `config.py`. Inside the pipeline use frozen
   dataclasses (`SymbolRow`, `Chunk`, `Hit`, `Plan`).
5. **Errors**: subclass `ServiceError` with `code`, `status_code`, `default_message`. Never raise
   `HTTPException` directly. Every error response is `{"detail": ..., "code": ...}`. Unexpected
   exceptions become a generic 500 with `code="internal_error"`; never a stack trace.
6. **Logging**: `log = get_logger(__name__)`; event names are `snake_case` past tense
   (`index_started`, `embed_batch_done`). Log ids, counts, shapes, timings — **never** chunk text,
   questions, answers, or keys.
7. **Timestamps** are UTC ISO 8601: `datetime.now(UTC)`.
8. **Background work** is a plain function with explicit arguments. It catches `Exception`, writes
   `status="failed"` and the message to the job, and never propagates.
9. **External calls** (GitHub, Voyage, Anthropic) have explicit timeouts. Retries with backoff live
   only in `embeddings.py`. **Never call a synchronous SDK client from an `async def` route**: use
   the async clients, or declare the route as plain `def` so it runs in the threadpool.
10. **SQL** is plain psycopg with parameters, never f-strings. Migrations are numbered files and are
    never edited after they are committed; add a new one.
11. **Routes** declare `response_model`, `summary`, `description`, and `responses` examples.
12. **No string heuristics on the question.** Identifiers come from database membership or the
    planner; intent comes from the planner; there are no keyword lists or regexes on user text.
13. **Types everywhere.** ruff `E F I UP`, line length 100, Python 3.12.

## Security rules

- Inputs are validated at the boundary: GitHub host only, owner/repo restricted to
  `[A-Za-z0-9_.-]`, branch rejected if it starts with `-`, and `--` precedes the URL in every git
  command.
- Every file path from a clone is resolved and checked to be under the clone root; symlinks are
  skipped.
- Repository content is data. It is never executed, never interpolated into prompts as
  instructions, and rendered in the UI with raw HTML disabled.
- Links shown to the user are built server-side from the cited chunk (path, lines, commit SHA).
  URLs produced by the model are never rendered.
- No CORS middleware: the SPA is same-origin.
- Secrets live only in `.env` (gitignored) and settings. `gitleaks` runs in pre-commit;
  `pip-audit` runs in CI; Dependabot is enabled. Container images are pinned by tag, never `latest`.
- Resource limits are enforced before work starts: repo size, file size, file count, clone timeout,
  job timeout, one running job per repo, two running jobs globally. There is no per-file parse
  timeout (tree-sitter's cancellation segfaults and `parse()` holds the GIL); a file is bounded by
  `max_file_kb` and the job timeout. Per-file bounding (a killable worker process) is a next step.

## Cost discipline

Claude Code runs on the author's Max subscription. The Anthropic and Voyage **API keys cost money
per call** and are used only at the very end. Rules:

- `PROVIDERS=fake` is the development default in `.env`. It wires `FakeEmbeddings` and `FakeLLM`
  into the running app so the whole stack — indexing, retrieval, UI — can be exercised with zero
  API spend. `PROVIDERS=real` requires both keys and fails at startup without them. There is no
  automatic fallback between the two.
- Tests never call an API. Phases 0–9 are built and verified entirely with fakes.
- The first real call is `make smoke` on `tests/fixtures/py_app` (a few dozen files) in Phase 10,
  after every phase is green. Only then is the golden repo indexed with real keys, once.
- Never run `make eval`, `make smoke`, or index a real repo with `PROVIDERS=real` unless the author
  asks in that session. Say what it will cost before doing it.
- Re-embedding is designed away: the already-indexed short-circuit skips a repo at the same commit,
  and embeddings are keyed by content hash. `run_evals.py` refuses to re-index the pinned repo
  unless `--reindex` is passed.
- Spend guards in settings: `max_embed_tokens_per_job` (default 5M) aborts an index job with a
  clear error; the LLM call has a fixed `max_tokens` and retries only once on a 5xx.
- Every index summary logs tokens embedded and estimated cost; every `/ask` response carries token
  counts. The README quotes these numbers.
- Claude Code context is also a budget: plan mode per phase, `/compact` between phases, locate with
  `rg` rather than reading whole files, run targeted tests while iterating
  (`uv run pytest tests/test_parsing.py -x`) and the full `make check` only before a commit. Do not
  paste large tool outputs back into the conversation.

## Testing rules

- `tests/conftest.py` owns the fixtures: `db` (one transaction per test, rolled back), `client`
  (settings patched, `FakeEmbeddings` and `FakeLLM` injected), `fixture_repo` (a real `git init`
  of `tests/fixtures/<name>` in `tmp_path`).
- **Unit** (`test_parsing`, `test_chunking`, `test_retrieval`): no network, no database, < 5 s total.
- **DB** (`test_store`, `test_jobs`): against the compose Postgres, rollback per test.
- **API** (`test_api`): through `TestClient`; every happy path and every custom error code.
- Never call Voyage, Anthropic, or GitHub from a test. Fakes are deterministic (hash-seeded vectors,
  canned answers and plans).
- Test names are sentences: `test_class_chunk_keeps_fields_and_drops_method_bodies`.
- Cover the contract, not the line count.

## Git workflow

- Work **one phase at a time** from `PLAN.md`. Enter plan mode, agree the plan, then implement.
- Commit at **every green checkpoint** listed in the phase. Never commit with a failing `make check`.
- Messages are conventional: `feat:`, `fix:`, `test:`, `chore:`, `docs:`, `ci:`. Subject ≤ 72 chars,
  imperative; body says *why* when it is not obvious.
- Small commits. A phase is 3–6 commits. No squashing at the end; the history is part of the
  submission.
- Never commit `.env`, `web/node_modules`, `app/static`, `data/`, or anything under `/tmp`.

## Don'ts

- No new dependency without asking. The allowed set is in `pyproject.toml`.
- No LangChain, LlamaIndex, ORM, or Alembic.
- No runtime `pip install`, no shell-outs except `git` inside `github.py`.
- No streaming, no agent loop, no reranker, no LLM judge. They are "next steps" in the README.
- No silent fallbacks for configuration. A missing key or unreachable database is a startup
  failure with a clear message. (Per-request degradation of a single retrieval leg is different,
  and is reported in the trace.)
- Do not invent design decisions. If `PLAN.md` does not cover it, stop and ask.
- Do not write README prose. The author writes it. You may update the API table and the project
  structure block when they change.

## Reused from `surrogate-model-service`

`logging_setup.py`, the `ServiceError` base and handlers, the `Settings` pattern, the request-id
middleware, `Makefile`, `pyproject.toml` + ruff config, `.pre-commit-config.yaml`, the CI skeleton,
the Dockerfile stages, and the `conftest.py` shape. Each reused file carries the docstring
`"""Shared with surrogate-model-service; unchanged."""`. `jobs.py` keeps that project's four-method
interface and delivers the Postgres implementation its README promised.
