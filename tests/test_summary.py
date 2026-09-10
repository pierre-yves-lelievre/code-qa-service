"""Repo summary: inputs from the walked tree, the call, validation and the best-effort fallback."""

from pathlib import Path

from app.config import settings
from app.errors import ProviderError
from app.github import WalkEntry
from app.llm import FakeLLM
from app.summary import (
    README_BYTES,
    SUMMARY_MAX_TOKENS,
    SUMMARY_SCHEMA,
    SYSTEM,
    TREE_LINES,
    Summary,
    inputs,
    material,
    summarize,
)


def _entries(root: Path, files: dict[str, str]) -> list[WalkEntry]:
    """Write files under root and return them as walked entries."""
    entries = []
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        entries.append(WalkEntry(rel, path, len(text.encode()), None))
    return entries


def test_inputs_are_the_top_level_readme_and_tree_with_directory_counts(tmp_path: Path):
    files = {
        "README.md": "# Shop\n" + "x" * (README_BYTES * 2),
        "docs/README.md": "nested readme is not the repo's",
        "shop/cart.py": "",
        "shop/tax.py": "",
        "pyproject.toml": "",
    }
    readme, tree = inputs(_entries(tmp_path, files))
    assert readme is not None and readme.startswith("# Shop") and len(readme) == README_BYTES
    assert tree == ["docs/ (1 files)", "shop/ (2 files)", "README.md", "pyproject.toml"]


def test_the_tree_is_capped_and_a_missing_readme_is_said(tmp_path: Path):
    entries = _entries(tmp_path, {f"f{i:03}.py": "" for i in range(TREE_LINES + 20)})
    readme, tree = inputs(entries)
    assert readme is None and len(tree) == TREE_LINES
    assert material(None, ["a.py"]) == "File tree, top level:\na.py\n\nREADME: none"


def test_a_valid_reply_is_trimmed_to_four_questions():
    llm = FakeLLM(
        replies=[{"summary": " A shop. ", "questions": ["a?", " ", "b?", "c?", "d?", "e?"]}]
    )
    result = summarize("# Shop", ["shop/ (2 files)"], llm)
    assert isinstance(result, Summary)
    assert (result.text, result.questions) == ("A shop.", ("a?", "b?", "c?", "d?"))
    assert result.usage.input > 0
    (request,) = llm.requests
    assert (request["system"], request["schema"]) == (SYSTEM, SUMMARY_SCHEMA)
    assert (request["max_tokens"], request["timeout"]) == (
        SUMMARY_MAX_TOKENS,
        settings.summary_timeout_s,
    )
    assert request["messages"] == [
        {"role": "user", "content": "File tree, top level:\nshop/ (2 files)\n\nREADME:\n# Shop"}
    ]


def test_too_few_questions_or_a_failed_call_leave_no_summary():
    short = FakeLLM(replies=[{"summary": "A shop.", "questions": ["a?", "b?", "c?"]}])
    assert summarize(None, [], short) is None
    assert summarize(None, [], FakeLLM(replies=[{"summary": "", "questions": []}])) is None
    assert summarize(None, [], FakeLLM(replies=[ProviderError("down")])) is None


def test_the_fake_answers_a_summary_schema_with_a_canned_summary():
    result = summarize(None, [], FakeLLM())
    assert result is not None and len(result.questions) == 4
