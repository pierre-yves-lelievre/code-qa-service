-- 0001_init: repositories, snapshots, files, chunks, embeddings, index jobs, query log.
-- schema_migrations is created by the runner in app/db.py, not here.

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Repositories and snapshots ──────────────────────────────────────────────

CREATE TABLE repos (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner               text NOT NULL,
    name                text NOT NULL,
    url                 text NOT NULL,
    summary             text,
    suggested_questions jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, name)
);

CREATE TABLE snapshots (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_id     bigint NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    commit_sha  text,  -- null while building, until the clone reports it
    branch      text NOT NULL,
    status      text NOT NULL DEFAULT 'building'
                CHECK (status IN ('building', 'active', 'retired', 'failed')),
    stats       jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    indexed_at  timestamptz
);

-- One active snapshot per repo; the activation flip retires the old one first.
CREATE UNIQUE INDEX snapshots_one_active_per_repo ON snapshots (repo_id) WHERE status = 'active';

-- ── Files and chunks ────────────────────────────────────────────────────────

CREATE TABLE files (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id  bigint NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    path         text NOT NULL,
    language     text,
    size_bytes   integer NOT NULL,
    mode         text NOT NULL CHECK (mode IN ('symbols', 'windows', 'skipped')),
    skip_reason  text,
    parse_errors integer NOT NULL DEFAULT 0,  -- -1 means the parse timed out
    UNIQUE (snapshot_id, path)
);

-- kind is validated in Python; its set is not final until Phase 3.
CREATE TABLE chunks (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id  bigint NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    file_id      bigint NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    kind         text NOT NULL,
    name         text,
    qualname     text,
    part         integer NOT NULL DEFAULT 0,
    start_line   integer NOT NULL,
    end_line     integer NOT NULL,
    signature    text,
    doc          text,
    text         text NOT NULL,
    content_hash text NOT NULL,
    tokens       integer NOT NULL,
    truncated    boolean NOT NULL DEFAULT false,
    search_a     text NOT NULL DEFAULT '',  -- name + qualname
    search_b     text NOT NULL DEFAULT '',  -- signature + doc
    search_c     text NOT NULL DEFAULT '',  -- body
    tsv          tsvector GENERATED ALWAYS AS (
                     setweight(to_tsvector('simple', search_a), 'A')
                     || setweight(to_tsvector('simple', search_b), 'B')
                     || setweight(to_tsvector('simple', search_c), 'C')
                 ) STORED
);

CREATE UNIQUE INDEX chunks_key
    ON chunks (file_id, kind, coalesce(qualname, ''), start_line, part);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX chunks_snapshot_name ON chunks (snapshot_id, name);
CREATE INDEX chunks_snapshot_qualname ON chunks (snapshot_id, qualname);
CREATE INDEX chunks_content_hash ON chunks (content_hash);

-- ── Embeddings ──────────────────────────────────────────────────────────────

-- Keyed by content, not by chunk, so identical text is embedded once per model.
CREATE TABLE embeddings (
    content_hash text NOT NULL,
    model        text NOT NULL,
    embedding    vector(1024) NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (content_hash, model)
);

CREATE INDEX embeddings_hnsw ON embeddings USING hnsw (embedding vector_cosine_ops);

-- ── Index jobs ──────────────────────────────────────────────────────────────

CREATE TABLE index_jobs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id      bigint NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    snapshot_id  bigint REFERENCES snapshots (id) ON DELETE SET NULL,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    progress     jsonb NOT NULL DEFAULT '{}',
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    completed_at timestamptz
);

-- Pending counts as active, so a duplicate is rejected at create, before any work starts.
CREATE UNIQUE INDEX index_jobs_one_active_per_repo
    ON index_jobs (repo_id) WHERE status IN ('pending', 'running');

-- ── Query log ───────────────────────────────────────────────────────────────

CREATE TABLE queries (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id  text NOT NULL,
    repo_id     bigint NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    snapshot_id bigint REFERENCES snapshots (id) ON DELETE SET NULL,
    question    text NOT NULL,
    answer      text,
    sources     jsonb NOT NULL DEFAULT '[]',
    retrievers  jsonb NOT NULL DEFAULT '{}',
    timings     jsonb NOT NULL DEFAULT '{}',
    tokens      jsonb NOT NULL DEFAULT '{}',
    not_found   boolean NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
