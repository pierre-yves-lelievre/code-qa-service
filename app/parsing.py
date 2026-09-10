"""tree-sitter parsing: the language table, definition queries, and file_symbols() -> rows."""

import inspect
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Literal

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser, Query, QueryCursor

QUERIES_DIR = Path(__file__).parent / "queries"

Kind = Literal["module", "class", "function", "method"]

# Extension → (grammar name, query file). JavaScript and TSX share the TypeScript query.
LANGUAGES: dict[str, tuple[str, str]] = {
    ".py": ("python", "python.scm"),
    ".ts": ("typescript", "typescript.scm"),
    ".mts": ("typescript", "typescript.scm"),
    ".cts": ("typescript", "typescript.scm"),
    ".tsx": ("tsx", "typescript.scm"),
    ".js": ("javascript", "typescript.scm"),
    ".jsx": ("javascript", "typescript.scm"),
    ".mjs": ("javascript", "typescript.scm"),
    ".cjs": ("javascript", "typescript.scm"),
}
_GRAMMARS = {
    "python": tree_sitter_python.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "javascript": tree_sitter_javascript.language,
}
_QUERY_FILES = {grammar: query for grammar, query in LANGUAGES.values()}
_MODULE_SUFFIXES = (".__init__", ".index")


@dataclass(frozen=True)
class SymbolRow:
    """One definition, or the whole module, found in a source file."""

    path: str
    kind: Kind
    name: str
    qualname: str
    start_line: int
    end_line: int
    signature: str | None
    doc: str | None


@dataclass(frozen=True)
class ParseResult:
    """The rows of one file and the number of ERROR and MISSING nodes in its tree."""

    rows: list[SymbolRow]
    error_nodes: int


# ── Language table ────────────────────────────────────────────────────────────


def language_for(path: str) -> str | None:
    """Return the grammar name for a file path, or None when its extension is not parsed."""
    spec = LANGUAGES.get(PurePosixPath(path).suffix.lower())
    return spec[0] if spec else None


def module_qualname(path: str) -> str:
    """Dotted module path: extension stripped, '/' to '.', trailing __init__ or index dropped."""
    dotted = ".".join(PurePosixPath(path).with_suffix("").parts)
    for suffix in _MODULE_SUFFIXES:
        if dotted.endswith(suffix):
            return dotted.removesuffix(suffix)
    return dotted


@cache
def _language(grammar: str) -> Language:
    """Load a grammar from its wheel, once."""
    return Language(_GRAMMARS[grammar]())


@cache
def _query(grammar: str) -> Query:
    """Compile a grammar's definition query, once."""
    return Query(_language(grammar), (QUERIES_DIR / _QUERY_FILES[grammar]).read_text())


# ── Parsing ───────────────────────────────────────────────────────────────────


def file_symbols(path: str, text: str, language: str) -> ParseResult:
    """Parse one file into its module row followed by one row per definition, in source order."""
    source = text.encode()
    root = Parser(_language(language)).parse(source).root_node
    module = module_qualname(path)

    # node.id → (definition node, kind, name, body); ids identify nodes across lookups.
    found: dict[int, tuple[Node, str, str, Node]] = {}
    for _, captures in QueryCursor(_query(language)).matches(root):
        for capture, nodes in captures.items():
            if capture.startswith("definition."):
                node = nodes[0]
                name = _text(source, captures["name"][0])
                found[node.id] = (
                    node,
                    capture.removeprefix("definition."),
                    name,
                    captures["body"][0],
                )

    rows = [
        SymbolRow(
            path=path,
            kind="module",
            name=PurePosixPath(path).stem,
            qualname=module,
            start_line=1,
            end_line=text.count("\n") + (0 if text.endswith("\n") else 1),
            signature=None,
            doc=_module_doc(language, source, root),
        )
    ]
    for node, kind, name, body in sorted(found.values(), key=lambda f: f[0].start_byte):
        chain = _enclosing(node, found)
        if kind == "function" and chain and chain[-1][0] == "class":
            kind = "method"
        outer = node.parent if node.parent and node.parent.type == "decorated_definition" else node
        rows.append(
            SymbolRow(
                path=path,
                kind=kind,
                name=name,
                qualname=".".join([module, *(n for _, n in chain), name]),
                start_line=outer.start_point.row + 1,
                end_line=_last_line(node),
                signature=_signature(source, node, body),
                doc=_doc(language, source, node, body),
            )
        )
    return ParseResult(rows=rows, error_nodes=_count_errors(root))


def _enclosing(node: Node, found: dict[int, tuple[Node, str, str, Node]]) -> list[tuple[str, str]]:
    """(kind, name) of each definition enclosing a node, outermost first."""
    chain = []
    parent = node.parent
    while parent is not None:
        if parent.id in found:
            _, kind, name, _ = found[parent.id]
            chain.append((kind, name))
        parent = parent.parent
    return chain[::-1]


def _text(source: bytes, node: Node) -> str:
    """Source text of a node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _last_line(node: Node) -> int:
    """1-based last line of a node, not counting a trailing newline."""
    row, column = node.end_point
    return row if column == 0 and row > node.start_point.row else row + 1


def _signature(source: bytes, node: Node, body: Node) -> str:
    """Source from the def/class keyword up to the body, on one line, without a trailing ':'."""
    start = next((c for c in node.children if c.type not in ("decorator", "comment")), node)
    head = source[start.start_byte : body.start_byte].decode("utf-8", errors="replace")
    if node.type == "variable_declarator" and node.parent is not None:
        head = f"{_text(source, node.parent.children[0])} {head}"  # const / let / var
    return " ".join(head.split()).removesuffix(":").rstrip()


def _count_errors(root: Node) -> int:
    """Count ERROR and MISSING nodes, descending only into subtrees that contain one."""
    count, stack = 0, [root] if root.has_error else []
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
        stack.extend(child for child in node.children if child.has_error)
    return count


# ── Docs ──────────────────────────────────────────────────────────────────────


def _doc(language: str, source: bytes, node: Node, body: Node) -> str | None:
    """A definition's doc: the Python docstring in its body, or the JSDoc just above it."""
    if language == "python":
        return _python_doc(source, body)
    return _jsdoc(source, _statement(node))


def _module_doc(language: str, source: bytes, root: Node) -> str | None:
    """A file's doc: the Python module docstring, or a detached JSDoc heading the file."""
    if language == "python":
        return _python_doc(source, root)
    for child in root.named_children:
        if child.type == "hash_bang_line" or (child.type == "comment" and not _is_jsdoc(child)):
            continue
        if not _is_jsdoc(child):
            return None
        # A JSDoc directly above the first statement documents that statement, not the file.
        following = child.next_named_sibling
        if following is None or following.start_point.row > child.end_point.row + 1:
            return _clean_jsdoc(_text(source, child))
        return None
    return None


def _python_doc(source: bytes, block: Node) -> str | None:
    """First statement of a block when it is a plain string literal, cleaned like a docstring."""
    first = next((c for c in block.named_children if c.type != "comment"), None)
    if first is None or first.type != "expression_statement" or first.named_child_count != 1:
        return None
    string = first.named_children[0]
    if string.type != "string" or "f" in _text(source, string.children[0]).lower():
        return None
    raw = source[string.children[0].end_byte : string.children[-1].start_byte]
    return inspect.cleandoc(raw.decode("utf-8", errors="replace")) or None


def _statement(node: Node) -> Node:
    """The statement a definition's JSDoc sits above: through its declaration and export."""
    if node.type == "variable_declarator" and node.parent is not None:
        node = node.parent
    if node.parent is not None and node.parent.type == "export_statement":
        node = node.parent
    return node


def _jsdoc(source: bytes, statement: Node) -> str | None:
    """The `/** */` comment ending on the line before a statement, or on its first line."""
    comment = statement.prev_named_sibling
    if comment is None or not _is_jsdoc(comment):
        return None
    if comment.end_point.row < statement.start_point.row - 1:
        return None
    return _clean_jsdoc(_text(source, comment))


def _is_jsdoc(node: Node) -> bool:
    """Whether a node is a `/** ... */` block comment."""
    return node.type == "comment" and node.text.startswith(b"/**") and node.text != b"/**/"


def _clean_jsdoc(comment: str) -> str | None:
    """Comment text without the `/** */` markers or each line's leading '*'."""
    body = comment.removeprefix("/**").removesuffix("*/")
    lines = [line.strip().removeprefix("*").strip() for line in body.splitlines()]
    return "\n".join(lines).strip() or None
