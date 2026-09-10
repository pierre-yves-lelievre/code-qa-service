"""ChunkStore tests against codeqa_test, one rolled-back transaction per test."""

from pathlib import Path

import pytest

from app.chunking import chunk_file, window_file
from app.config import settings
from app.embeddings import FakeEmbeddings
from app.github import walk_files
from app.indexing import _index_file
from app.parsing import file_symbols
from app.retrieval import or_terms, split_identifiers
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


# ── Retrieval queries ─────────────────────────────────────────────────────────


def _py_snapshot(store, sha: str = "a" * 40) -> int:
    """Index tests/fixtures/py_app as an active snapshot with fake vectors; return its id."""
    repo_id = store.upsert_repo("octo", "py_app", "https://github.com/octo/py_app")
    snapshot_id = store.create_snapshot(repo_id, "main", sha)
    batch = [_index_file(entry)[:2] for entry in walk_files(FIXTURES / "py_app")]
    store.add_files(snapshot_id, batch)
    fake = FakeEmbeddings(settings.embedding_dims)
    pending, _ = store.pending_embeddings(snapshot_id, fake.model)
    hashes = [content_hash for content_hash, _ in pending]
    texts = store.chunk_texts(snapshot_id, hashes)
    vectors = fake.embed_documents([texts[h] for h in hashes]).vectors
    store.add_embeddings(fake.model, list(zip(hashes, vectors, strict=True)))
    store.activate(snapshot_id, {})
    return snapshot_id


def _chunk(db, snapshot_id: int, qualname: str) -> tuple[int, str]:
    """The id and text of the snapshot's first chunk with this qualname."""
    return db.execute(
        "SELECT id, text FROM chunks WHERE snapshot_id = %s AND qualname = %s ORDER BY id LIMIT 1",
        (snapshot_id, qualname),
    ).fetchone()


def test_membership_keeps_names_qualnames_and_dotted_suffixes_in_order(store):
    snapshot_id = _py_snapshot(store)
    candidates = ["Stripe", "Product.label", "slugify", "shop.util.tax_rate", "product", "total"]
    assert store.resolve_identifiers(snapshot_id, candidates) == [
        "Product.label",
        "slugify",
        "shop.util.tax_rate",
        "total",
    ]


def test_symbol_ladder_stops_at_each_identifiers_first_rung(store):
    snapshot_id = _py_snapshot(store)

    def qualnames(*identifiers: str) -> list[str | None]:
        """Qualnames of the ladder's hits for these identifiers."""
        return [h.qualname for h in store.symbol_search(snapshot_id, list(identifiers), 50)]

    assert qualnames("shop.models.Product") == ["shop.models.Product"]  # qualname rung
    assert qualnames("slugify") == ["shop.util.slugify"]  # name rung
    assert qualnames("Product.label") == ["shop.models.Product.label"] * 2  # suffix: getter, setter
    assert qualnames("slugify", "shop.models.Product") == [
        "shop.util.slugify",
        "shop.models.Product",
    ]  # in identifier order


def test_full_text_uses_and_first_then_falls_back_to_or(store):
    snapshot_id = _py_snapshot(store)
    hits, mode = store.text_search(snapshot_id, split_identifiers("tax_rate"), or_terms("x"), 50)
    assert (mode, hits[0].qualname) == ("and", "shop.util.tax_rate")

    question = "how is the tax rate for a country computed"
    hits, mode = store.text_search(snapshot_id, split_identifiers(question), or_terms(question), 50)
    assert mode == "or"
    assert "shop.util.tax_rate" in [h.qualname for h in hits]

    assert store.text_search(snapshot_id, "zebra quantum", or_terms("zebra quantum"), 50)[0] == []


def test_vector_leg_returns_the_chunk_whose_text_is_the_query_first(store, db):
    snapshot_id = _py_snapshot(store)
    chunk_id, text = _chunk(db, snapshot_id, "shop.util.slugify")
    vector = FakeEmbeddings(settings.embedding_dims).embed_query(text).vectors[0]
    hits = store.vector_search(snapshot_id, "fake", vector, 20)
    assert len({h.id for h in hits}) == len(hits) > 1
    assert hits[0].id == chunk_id
    assert hits[0].score == pytest.approx(1.0)
    assert all(h.score < 0.35 for h in hits[1:])  # hash-seeded noise: mechanics, not meaning
