"""ChunkStore tests against codeqa_test, one rolled-back transaction per test."""

from pathlib import Path

import pytest

from app.chunking import chunk_file, window_file
from app.parsing import file_symbols
from app.store import FileRow

FIXTURES = Path(__file__).parent / "fixtures"


def _statuses(db, repo_id: int) -> dict[int, str]:
    """Snapshot id → status for one repo."""
    rows = db.execute("SELECT id, status FROM snapshots WHERE repo_id = %s", (repo_id,))
    return dict(rows.fetchall())


def _indexed(store, repo_id: int, sha: str) -> int:
    """Create a snapshot with one windowed file and activate it; return its id."""
    snapshot_id = store.create_snapshot(repo_id, "main", sha)
    notes = FileRow(path="notes.txt", language=None, size_bytes=6, mode="windows")
    store.add_files(snapshot_id, [(notes, window_file("notes.txt", f"{sha}\n"))])
    store.activate(snapshot_id, {"files": 1})
    return snapshot_id


# ── Repos ─────────────────────────────────────────────────────────────────────


def test_upsert_repo_is_idempotent_and_refreshes_the_url(store):
    first = store.upsert_repo("octo", "demo", "https://github.com/octo/demo")
    again = store.upsert_repo("octo", "demo", "https://github.com/octo/demo/")
    assert first == again
    assert store.upsert_repo("octo", "other", "https://github.com/octo/other") != first


# ── Snapshots ─────────────────────────────────────────────────────────────────


def test_activation_retires_the_previous_snapshot(store, db):
    repo_id = store.upsert_repo("octo", "demo", "https://github.com/octo/demo")
    assert store.active_snapshot(repo_id) is None
    first = _indexed(store, repo_id, "a" * 40)
    second = _indexed(store, repo_id, "b" * 40)
    assert _statuses(db, repo_id) == {first: "retired", second: "active"}
    active = store.active_snapshot(repo_id)
    assert (active.id, active.commit_sha, active.branch) == (second, "b" * 40, "main")
    stats, indexed_at = db.execute(
        "SELECT stats, indexed_at FROM snapshots WHERE id = %s", (second,)
    ).fetchone()
    assert stats == {"files": 1}
    assert indexed_at is not None


def test_failed_snapshot_leaves_the_active_one_alone(store, db):
    repo_id = store.upsert_repo("octo", "demo", "https://github.com/octo/demo")
    active = _indexed(store, repo_id, "a" * 40)
    building = store.create_snapshot(repo_id, "main", "b" * 40)
    store.fail_snapshot(building)
    store.fail_snapshot(active)  # only a building snapshot can fail
    assert _statuses(db, repo_id) == {active: "active", building: "failed"}
    with pytest.raises(ValueError, match="not building"):
        store.activate(building, {})


def test_sweep_keeps_the_newest_two_and_never_the_building_one(store, db):
    repo_id = store.upsert_repo("octo", "demo", "https://github.com/octo/demo")
    oldest, retired, active = (_indexed(store, repo_id, c * 40) for c in "abc")
    building = store.create_snapshot(repo_id, "main", "d" * 40)
    assert store.sweep(repo_id) == 1
    assert _statuses(db, repo_id) == {retired: "retired", active: "active", building: "building"}
    orphans = db.execute("SELECT count(*) FROM chunks WHERE snapshot_id = %s", (oldest,))
    assert orphans.fetchone() == (0,)
    assert store.sweep(repo_id) == 0


# ── Files and chunks ──────────────────────────────────────────────────────────


def test_files_and_chunks_round_trip_with_search_fields(store, db):
    repo_id = store.upsert_repo("octo", "demo", "https://github.com/octo/demo")
    snapshot_id = store.create_snapshot(repo_id, "main", "a" * 40)
    text = (FIXTURES / "py_app" / "shop" / "models.py").read_text()
    chunks = chunk_file("shop/models.py", text, file_symbols("shop/models.py", text, "python").rows)
    models = FileRow("shop/models.py", "python", len(text.encode()), "symbols")
    logo = FileRow("logo.png", None, 10, "skipped", skip_reason="binary")
    assert store.add_files(snapshot_id, [(models, chunks), (logo, [])]) == len(chunks)

    files = db.execute(
        "SELECT path, language, mode, skip_reason FROM files WHERE snapshot_id = %s ORDER BY path",
        (snapshot_id,),
    ).fetchall()
    assert files == [
        ("logo.png", None, "skipped", "binary"),
        ("shop/models.py", "python", "symbols", None),
    ]
    rows = db.execute(
        "SELECT kind, name, qualname, part, start_line, end_line, signature, doc, text,"
        " content_hash, tokens, truncated, search_a, search_b, search_c"
        " FROM chunks WHERE snapshot_id = %s ORDER BY id",
        (snapshot_id,),
    ).fetchall()
    assert rows == [
        (
            c.kind, c.name, c.qualname, c.part, c.start_line, c.end_line, c.signature, c.doc,
            c.text, c.content_hash, c.tokens, c.truncated, c.search_a, c.search_b, c.search_c,
        )
        for c in chunks
    ]  # fmt: skip
    (hit,) = db.execute(
        "SELECT qualname FROM chunks WHERE snapshot_id = %s AND kind = 'class'"
        " AND tsv @@ to_tsquery('simple', 'Product:A')",
        (snapshot_id,),
    ).fetchone()
    assert hit == "shop.models.Product"
