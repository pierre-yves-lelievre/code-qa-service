"""Chunking tests over fixture files and inline text. Unit only: no database, no network."""

from pathlib import Path

import pytest

from app.chunking import Chunk, chunk_file, estimate_tokens, window_file
from app.parsing import file_symbols, language_for
from app.retrieval import split_identifiers

FIXTURES = Path(__file__).parent / "fixtures"


def _chunk_text(path: str, text: str) -> list[Chunk]:
    """Parse and chunk one file's text."""
    return chunk_file(path, text, file_symbols(path, text, language_for(path)).rows)


def _chunks(repo: str, path: str) -> list[Chunk]:
    """Parse and chunk one fixture file, with `path` relative to the fixture repo's root."""
    return _chunk_text(path, (FIXTURES / repo / path).read_text())


def _find(chunks: list[Chunk], qualname: str, start_line: int | None = None) -> Chunk:
    """The first chunk with the given qualname (and start line, when given)."""
    return next(c for c in chunks if c.qualname == qualname and start_line in (None, c.start_line))


# ── Symbol rules ──────────────────────────────────────────────────────────────


def test_module_chunk_has_imports_and_top_level_code_but_no_bodies():
    module = _find(_chunks("py_app", "shop/models.py"), "shop.models")
    assert module.text == (
        "shop/models.py :: shop.models (module)\n"
        '"""Domain models for the shop."""\n'
        "\n"
        "from dataclasses import dataclass"
    )
    service = _find(_chunks("ts_app", "src/service.ts"), "src.service")
    assert 'import { Base } from "./base";' in service.text
    assert "return a + b" not in service.text
    assert "Adds two numbers." not in service.text  # a JSDoc goes with its definition


def test_file_of_documented_definitions_has_no_module_chunk():
    assert "module" not in {c.kind for c in _chunks("ts_app", "src/types.ts")}


def test_class_chunk_keeps_fields_drops_method_bodies_and_lists_child_signatures():
    chunks = _chunks("py_app", "shop/models.py")
    product = _find(chunks, "shop.models.Product")
    assert product.text == (
        "shop/models.py :: shop.models.Product (class)\n"
        "@dataclass\n"
        "class Product:\n"
        '    """A product on sale."""\n'
        "\n"
        "    sku: str\n"
        "    price: int\n"
        "\n"
        "    def label(self) -> str\n"
        "\n"
        "    def label(self, value: str) -> None\n"
        "\n"
        "    async def reserve( self, quantity: int = 1, ) -> bool"
    )
    # The first method is decorated: its decorator line belongs to the method chunk only.
    assert "@property" not in product.text
    assert (
        "    @property\n    def label(self) -> str:"
        in _find(chunks, "shop.models.Product.label", 13).text
    )


def test_type_chunk_follows_the_class_rule():
    chunks = _chunks("ts_app", "src/types.ts")
    assert _find(chunks, "src.types.Shape").text == (
        "src/types.ts :: src.types.Shape (class)\n"
        "A shape.\n"
        "export abstract class Shape implements HasArea {\n"
        "  abstract area(): number;\n"
        "\n"
        "  describe(): string\n"
        "}"
    )
    has_area = _find(chunks, "src.types.HasArea")
    assert (has_area.kind, has_area.text) == (
        "type",
        "src/types.ts :: src.types.HasArea (type)\n"
        "Something with an area.\n"
        "export interface HasArea {\n"
        "  area(): number;\n"
        "}",
    )


def test_child_jsdoc_is_dropped_from_the_class_chunk_with_the_child_body():
    service = _find(_chunks("ts_app", "src/service.ts"), "src.service.OrderService")
    assert "Place an order." not in service.text
    assert "  async place(id: string): Promise<void>\n" in service.text


def test_nested_def_appears_in_parent_body_and_as_its_own_chunk():
    chunks = _chunks("py_app", "shop/models.py")
    reserve = _find(chunks, "shop.models.Product.reserve")
    assert "        def available() -> int:\n            return quantity" in reserve.text
    available = _find(chunks, "shop.models.Product.reserve.available")
    assert (available.kind, available.start_line, available.end_line) == ("function", 28, 29)


def test_property_getter_and_setter_give_two_chunks_with_distinct_keys():
    chunks = [c for c in _chunks("py_app", "shop/models.py") if c.name == "label"]
    keys = {(c.kind, c.qualname, c.start_line, c.part) for c in chunks}
    assert keys == {
        ("method", "shop.models.Product.label", 13, 0),
        ("method", "shop.models.Product.label", 18, 0),
    }


def test_python_docstring_appears_once_and_jsdoc_is_prefixed():
    getter = _find(_chunks("py_app", "shop/models.py"), "shop.models.Product.label", 13)
    assert getter.text.count("Human-readable label.") == 1
    assert _find(_chunks("ts_app", "src/service.ts"), "src.service.add").text == (
        "src/service.ts :: src.service.add (function)\n"
        "function add(a: number, b: number): number\n"
        "Adds two numbers.\n"
        "export function add(a: number, b: number): number {\n"
        "  return a + b;\n"
        "}"
    )


# ── Parts, windows, manifests, cap ────────────────────────────────────────────


def test_long_function_splits_into_parts_with_expected_overlap():
    body = "".join(f"    value_{i} = x + {i}  # padding padding padding\n" for i in range(300))
    parts = _chunk_text("m.py", f"def big(x):\n{body}")  # no module chunk: nothing is uncovered
    n = len(parts)
    assert n > 1
    assert [c.part for c in parts] == list(range(1, n + 1))
    assert (parts[0].start_line, parts[-1].end_line) == (1, 301)
    lines = f"def big(x):\n{body}".splitlines()
    for i, chunk in enumerate(parts, start=1):
        head, signature, *code = chunk.text.split("\n")
        assert (head, signature) == (f"m.py :: m.big (function, part {i}/{n})", "def big(x)")
        assert code == lines[chunk.start_line - 1 : chunk.end_line]
        assert sum(estimate_tokens(line + "\n") for line in code) <= 1_200
    for before, after in zip(parts, parts[1:], strict=False):
        shared = lines[after.start_line - 1 : before.end_line]
        assert shared and sum(estimate_tokens(line + "\n") for line in shared) >= 150


def test_files_without_symbols_window_at_80_lines_with_15_overlap():
    windows = window_file("data.txt", "".join(f"line {i}\n" for i in range(1, 201)))
    assert [(c.kind, c.part, c.start_line, c.end_line) for c in windows] == [
        ("window", 0, 1, 80),
        ("window", 0, 66, 145),
        ("window", 0, 131, 200),
    ]
    assert windows[1].text.split("\n")[:2] == ["data.txt (window)", "line 66"]


def test_readme_splits_by_heading():
    readme = "intro\n\n# Title\ntext\n```\n# not a heading\n```\n## Install ##\npip install\n"
    sections = window_file("docs/README.md", readme)
    assert [(c.kind, c.name, c.start_line, c.end_line) for c in sections] == [
        ("window", None, 1, 1),
        ("window", "Title", 3, 7),
        ("window", "Install", 8, 9),
    ]
    assert sections[1].text.startswith("docs/README.md :: Title (window)\n# Title\n")


@pytest.mark.parametrize(
    "path", ["backend/pyproject.toml", "requirements-dev.txt", "web/package.json"]
)
def test_manifests_are_single_chunks(path: str):
    text = "".join(f"entry-{i} = 1\n" for i in range(300))
    chunks = window_file(path, text)
    assert [(c.kind, c.name, c.start_line, c.end_line) for c in chunks] == [
        ("manifest", path.rsplit("/", 1)[-1], 1, 300)
    ]


def test_oversized_chunk_is_truncated_at_4000_tokens_on_a_whole_line():
    line = "x" * 999
    (window,) = window_file("data.txt", f"{line}\n" * 80)
    assert window.truncated
    assert window.tokens <= 4_000
    assert window.end_line < 80
    assert window.text.split("\n")[1:] == [line] * window.end_line

    (manifest,) = window_file("package.json", "y" * 20_000)
    assert (manifest.truncated, manifest.end_line) == (True, 1)
    assert manifest.tokens <= 4_000


# ── Stability ─────────────────────────────────────────────────────────────────


def test_crlf_file_chunks_equal_lf():
    for repo, path in [("py_app", "shop/models.py"), ("ts_app", "src/service.ts")]:
        text = (FIXTURES / repo / path).read_text()
        assert _chunk_text(path, text.replace("\n", "\r\n")) == _chunk_text(path, text)
    assert window_file("x.txt", "a\r\nb\r\n") == window_file("x.txt", "a\nb\n")


def test_empty_file_yields_no_chunk():
    assert _chunk_text("empty.py", "") == []
    assert _chunk_text("empty.ts", "\n\n") == []
    assert window_file("empty.txt", "") == []
    assert window_file("README.md", "\n  \n") == []


def test_unchanged_chunk_keeps_its_hash_when_another_function_changes():
    text = (FIXTURES / "py_app" / "shop" / "models.py").read_text()
    edited = text.replace("self.sku = value\n", "value = value.strip()\n        self.sku = value\n")
    before = {(c.qualname, c.signature): c for c in _chunk_text("shop/models.py", text)}
    after = {(c.qualname, c.signature): c for c in _chunk_text("shop/models.py", edited)}
    assert before.keys() == after.keys()
    changed = {key for key in after if after[key].content_hash != before[key].content_hash}
    assert changed == {("shop.models.Product.label", "def label(self, value: str) -> None")}
    reserve = (
        "shop.models.Product.reserve",
        "async def reserve( self, quantity: int = 1, ) -> bool",
    )
    assert (before[reserve].start_line, after[reserve].start_line) == (22, 23)  # moved, same text


# ── Tokens and search fields ──────────────────────────────────────────────────


def test_estimate_tokens_is_utf8_bytes_over_three_rounded_up():
    assert [estimate_tokens(t) for t in ("", "abc", "abcd", "é")] == [0, 1, 2, 1]


def test_split_identifiers_adds_camel_dotted_and_path_parts():
    assert split_identifiers("getPasswordHash HTTPServer backend.app.crud.authenticate") == (
        "getPasswordHash get Password Hash HTTPServer HTTP Server "
        "backend.app.crud.authenticate backend app crud authenticate"
    )
    assert split_identifiers("src/util.js plain") == "src/util.js src util js plain"
    getter = _find(_chunks("py_app", "shop/models.py"), "shop.models.Product.label", 13)
    assert getter.search_a == "label shop.models.Product.label shop models Product label"
    assert getter.search_b == (
        "def label(self) label self -> str Human-readable Human readable label."
    )
