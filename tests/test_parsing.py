"""Parsing tests over fixture files. Unit only: no database, no network."""

from pathlib import Path

import pytest

from app.parsing import SymbolRow, file_symbols, language_for, module_qualname

FIXTURES = Path(__file__).parent / "fixtures"


def _parse(repo: str, path: str) -> list[SymbolRow]:
    """Parse one fixture file, with `path` relative to the fixture repo's root."""
    text = (FIXTURES / repo / path).read_text()
    return file_symbols(path, text, language_for(path)).rows


def _row(rows: list[SymbolRow], qualname: str) -> SymbolRow:
    """The first row with the given qualname."""
    return next(r for r in rows if r.qualname == qualname)


# ── Language table ────────────────────────────────────────────────────────────


def test_language_for_maps_extensions_and_rejects_unknown():
    assert language_for("shop/models.py") == "python"
    assert language_for("SHOP/MODELS.PY") == "python"
    assert language_for("README.md") is None
    assert language_for("Makefile") is None


@pytest.mark.parametrize(
    ("path", "dotted"),
    [
        ("backend/app/crud.py", "backend.app.crud"),
        ("shop/__init__.py", "shop"),
        ("src/index.ts", "src"),
        ("setup.py", "setup"),
    ],
)
def test_module_qualname_strips_extension_and_package_entry_files(path: str, dotted: str):
    assert module_qualname(path) == dotted


# ── Python ────────────────────────────────────────────────────────────────────


def test_python_file_yields_module_class_methods_and_nested_function_in_order():
    rows = _parse("py_app", "shop/models.py")
    assert [(r.kind, r.qualname, r.start_line, r.end_line) for r in rows] == [
        ("module", "shop.models", 1, 31),
        ("class", "shop.models.Product", 6, 31),
        ("method", "shop.models.Product.label", 13, 16),
        ("method", "shop.models.Product.label", 18, 20),
        ("method", "shop.models.Product.reserve", 22, 31),
        ("function", "shop.models.Product.reserve.available", 28, 29),
    ]
    assert {r.path for r in rows} == {"shop/models.py"}


def test_decorated_definition_starts_at_decorator_but_signature_at_keyword():
    models = _parse("py_app", "shop/models.py")
    product = _row(models, "shop.models.Product")
    assert (product.start_line, product.signature) == (6, "class Product")
    label = _row(models, "shop.models.Product.label")
    assert (label.start_line, label.signature) == (13, "def label(self) -> str")

    tax_rate = _row(_parse("py_app", "shop/util.py"), "shop.util.tax_rate")
    assert (tax_rate.kind, tax_rate.start_line, tax_rate.end_line) == ("function", 14, 16)
    assert tax_rate.signature == "def tax_rate(country: str) -> float"


def test_async_def_is_a_method_not_a_separate_kind_with_a_one_line_signature():
    reserve = _row(_parse("py_app", "shop/models.py"), "shop.models.Product.reserve")
    assert reserve.kind == "method"
    assert reserve.signature == "async def reserve( self, quantity: int = 1, ) -> bool"


def test_nested_function_is_a_function_qualified_by_its_enclosing_method():
    available = _row(_parse("py_app", "shop/models.py"), "shop.models.Product.reserve.available")
    assert (available.kind, available.name) == ("function", "available")


def test_python_docstrings_are_captured_and_cleaned():
    models = _parse("py_app", "shop/models.py")
    docs = {(r.qualname, r.start_line): r.doc for r in models}
    assert docs == {
        ("shop.models", 1): "Domain models for the shop.",
        ("shop.models.Product", 6): "A product on sale.",
        ("shop.models.Product.label", 13): "Human-readable label.",
        ("shop.models.Product.label", 18): None,
        ("shop.models.Product.reserve", 22): "Reserve stock for this product.",
        ("shop.models.Product.reserve.available", 28): None,
    }
    util = _parse("py_app", "shop/util.py")
    assert _row(util, "shop.util.slugify").doc == (
        "Lower-case and hyphenate a title.\n\nExtra indentation is removed."
    )
    assert _row(util, "shop.util.banner").doc is None  # an f-string is not a docstring


def test_package_init_module_row_takes_the_package_qualname():
    assert _parse("py_app", "shop/__init__.py") == [
        SymbolRow(
            path="shop/__init__.py",
            kind="module",
            name="__init__",
            qualname="shop",
            start_line=1,
            end_line=1,
            signature=None,
            doc="The shop package.",
        )
    ]
