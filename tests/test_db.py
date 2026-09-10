"""Database tests: migrations runner and pool, against the compose Postgres."""

from pathlib import Path

import pytest

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
