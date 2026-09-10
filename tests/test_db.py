"""Database tests: migrations runner, initial schema, and pool, against the compose Postgres."""

from pathlib import Path

import pytest
from psycopg.errors import UniqueViolation

from app.db import close_pool, connection, run_migrations

# Test migrations use 9xxx versions so they never collide with real ones already recorded.


def _columns(db, table: str) -> list[str]:
    """Column names of a table in declaration order."""
    rows = db.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [name for (name,) in rows]


def _file(db) -> tuple[int, int]:
    """Insert a repo, a building snapshot, and one file; return (snapshot_id, file_id)."""
    (repo_id,) = db.execute(
        "INSERT INTO repos (owner, name, url) VALUES (%s, %s, %s) RETURNING id",
        ("octo", "demo", "https://github.com/octo/demo"),
    ).fetchone()
    (snapshot_id,) = db.execute(
        "INSERT INTO snapshots (repo_id, branch) VALUES (%s, %s) RETURNING id", (repo_id, "main")
    ).fetchone()
    (file_id,) = db.execute(
        "INSERT INTO files (snapshot_id, path, size_bytes, mode) VALUES (%s, %s, %s, %s)"
        " RETURNING id",
        (snapshot_id, "app/auth.py", 120, "symbols"),
    ).fetchone()
    return snapshot_id, file_id


def _chunk(db, snapshot_id: int, file_id: int, **cols) -> int:
    """Insert a module chunk at line 1, overridden by `cols`; return its id."""
    row = {"kind": "module", "qualname": None, "start_line": 1, "end_line": 5, **cols}
    (chunk_id,) = db.execute(
        "INSERT INTO chunks (snapshot_id, file_id, kind, qualname, start_line, end_line, text,"
        " content_hash, tokens, search_a, search_b, search_c)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'x', 'h', 1, %s, %s, %s) RETURNING id",
        (
            snapshot_id,
            file_id,
            row["kind"],
            row["qualname"],
            row["start_line"],
            row["end_line"],
            row.get("search_a", ""),
            row.get("search_b", ""),
            row.get("search_c", ""),
        ),
    ).fetchone()
    return chunk_id


# ── Migrations ────────────────────────────────────────────────────────────────


def test_migrations_apply_in_order_and_are_recorded(db, tmp_path: Path):
    (tmp_path / "9002_alter.sql").write_text("ALTER TABLE mig_a ADD COLUMN note text;")
    (tmp_path / "9001_create.sql").write_text(
        "CREATE TABLE mig_a (id int); CREATE TABLE mig_b (id int);"
    )
    (tmp_path / "notes.txt").write_text("not a migration")

    assert run_migrations(db, tmp_path) == ["9001_create.sql", "9002_alter.sql"]
    assert _columns(db, "mig_a") == ["id", "note"]
    assert _columns(db, "mig_b") == ["id"]
    recorded = db.execute(
        "SELECT version, name FROM schema_migrations WHERE version >= 9000 ORDER BY version"
    ).fetchall()
    assert recorded == [(9001, "9001_create.sql"), (9002, "9002_alter.sql")]


def test_second_migration_run_applies_nothing(db, tmp_path: Path):
    (tmp_path / "9001_create.sql").write_text("CREATE TABLE mig_a (id int);")
    run_migrations(db, tmp_path)
    assert run_migrations(db, tmp_path) == []


def test_duplicate_migration_version_is_rejected(db, tmp_path: Path):
    (tmp_path / "9001_one.sql").write_text("SELECT 1;")
    (tmp_path / "9001_two.sql").write_text("SELECT 2;")
    with pytest.raises(RuntimeError, match="Duplicate migration version"):
        run_migrations(db, tmp_path)


# ── Initial schema ────────────────────────────────────────────────────────────


def test_initial_schema_creates_tables_and_vector_extension(db):
    tables = {
        name
        for (name,) in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    expected = {"repos", "snapshots", "files", "chunks", "embeddings", "index_jobs", "queries"}
    assert expected | {"schema_migrations"} <= tables
    assert db.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 1").fetchone() == (
        "0001_init.sql",
    )
    assert run_migrations(db) == []


def test_chunk_tsv_weights_name_signature_and_body(db):
    snapshot_id, file_id = _file(db)
    chunk_id = _chunk(
        db,
        snapshot_id,
        file_id,
        search_a="login access token",
        search_b="def login form",
        search_c="return token",
    )

    def matches(query: str) -> bool:
        """Whether the chunk's tsv matches a weighted tsquery."""
        (hit,) = db.execute(
            "SELECT tsv @@ to_tsquery('simple', %s) FROM chunks WHERE id = %s", (query, chunk_id)
        ).fetchone()
        return hit

    assert matches("access:A")
    assert matches("form:B")
    assert matches("return:C")
    assert not matches("return:A")


def test_chunk_key_is_unique_even_when_qualname_is_null(db):
    snapshot_id, file_id = _file(db)
    _chunk(db, snapshot_id, file_id)
    _chunk(db, snapshot_id, file_id, start_line=10)
    with pytest.raises(UniqueViolation), db.transaction():
        _chunk(db, snapshot_id, file_id)


# ── Pool ──────────────────────────────────────────────────────────────────────


def test_pooled_connection_works_again_after_the_pool_is_closed(migrated):
    try:
        with connection() as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        close_pool()
        with connection() as conn:
            assert conn.execute("SELECT 2").fetchone() == (2,)
    finally:
        close_pool()
