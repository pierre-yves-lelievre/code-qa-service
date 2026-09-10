"""Chunking: symbol rows and plain files -> chunks, with parts, windows, manifests and a hash."""

import hashlib
from dataclasses import dataclass
from math import ceil
from typing import Literal

from app.parsing import Kind, SymbolRow, language_for
from app.retrieval import split_identifiers

ChunkKind = Kind | Literal["window", "manifest"]
Line = tuple[int, str]  # (1-based line number, text without the newline)

BYTES_PER_TOKEN = 3


@dataclass(frozen=True)
class Chunk:
    """One unit of retrieval: a symbol, a part of one, a window, or a manifest."""

    path: str
    kind: ChunkKind
    name: str | None
    qualname: str | None
    part: int  # 0 when not split, else 1..n
    start_line: int
    end_line: int
    signature: str | None
    doc: str | None
    text: str
    content_hash: str
    tokens: int
    truncated: bool
    search_a: str  # name + qualname
    search_b: str  # signature + doc
    search_c: str  # the source lines


def estimate_tokens(text: str) -> int:
    """Token estimate: UTF-8 bytes / 3, rounded up; it runs high for code, on purpose."""
    return ceil(len(text.encode()) / BYTES_PER_TOKEN)


# ── Symbols ───────────────────────────────────────────────────────────────────


def chunk_file(path: str, text: str, rows: list[SymbolRow]) -> list[Chunk]:
    """Chunks of a parsed file: one per symbol row, in row order; an empty module is dropped."""
    lines = _lines(text)
    doc_outside = language_for(path) != "python"  # a JSDoc sits above its span
    parents = _parents(rows)
    chunks: list[Chunk] = []
    for i, row in enumerate(rows):
        children = [child for child, parent in zip(rows, parents, strict=True) if parent == i]
        doc = row.doc if doc_outside else None
        if row.kind == "module":
            # The module doc is always among the uncovered lines, so it gets no prefix.
            body, lead, doc = _collapse(_uncovered(lines, children)), [], None
        elif row.kind in ("class", "type"):
            body, lead = _collapse(_own_lines(lines, row, children)), []
        else:
            body, lead = _span(lines, row.start_line, row.end_line), [row.signature or ""]
        chunks.extend(_symbol_chunks(path, row, lead, doc, body))
    return chunks


def _parents(rows: list[SymbolRow]) -> list[int | None]:
    """Index of each row's enclosing row: the innermost one whose qualname and span contain it."""
    parents: list[int | None] = [None]
    for i, row in enumerate(rows[1:], start=1):
        parent = 0
        for j in range(i - 1, 0, -1):
            outer = rows[j]
            if (
                f"{outer.qualname}.{row.name}" == row.qualname
                and outer.start_line <= row.start_line
                and row.end_line <= outer.end_line
            ):
                parent = j
                break
        parents.append(parent)
    return parents


def _own_lines(lines: list[str], row: SymbolRow, children: list[SymbolRow]) -> list[Line]:
    """A class's lines with each child's span replaced by its signature, at its indentation."""
    replaced = {_first_line(c): c for c in children if c.start_line > row.start_line}
    own: list[Line] = []
    skip_to = 0
    for number, line in _span(lines, row.start_line, row.end_line):
        if number <= skip_to:
            continue
        child = replaced.get(number)
        if child is None:
            own.append((number, line))
            continue
        indent = line[: len(line) - len(line.lstrip())]
        own.append((number, f"{indent}{child.signature}"))
        skip_to = child.end_line
    return own


def _uncovered(lines: list[str], children: list[SymbolRow]) -> list[Line]:
    """A module's lines outside every top-level definition and its JSDoc."""
    covered = {n for c in children for n in range(_first_line(c), c.end_line + 1)}
    return [(n, line) for n, line in _span(lines, 1, len(lines)) if n not in covered]


def _first_line(row: SymbolRow) -> int:
    """First line a definition covers: its JSDoc when there is one above it, else start_line."""
    return min(row.doc_start_line or row.start_line, row.start_line)


def _symbol_chunks(
    path: str, row: SymbolRow, lead: list[str], doc: str | None, body: list[Line]
) -> list[Chunk]:
    """The chunk of one symbol, or nothing when its body has no text."""
    if not any(line.strip() for _, line in body):
        return []
    return [
        _chunk(
            path=path,
            kind=row.kind,
            name=row.name,
            qualname=row.qualname,
            signature=row.signature,
            doc=row.doc,
            lead=[*lead, *([doc] if doc else [])],
            body=body,
            span=(row.start_line, row.end_line),
        )
    ]


# ── Lines ─────────────────────────────────────────────────────────────────────


def _lines(text: str) -> list[str]:
    """A file's lines, without newlines; a trailing newline does not start a new line."""
    return text.removesuffix("\n").split("\n") if text else []


def _span(lines: list[str], start: int, end: int) -> list[Line]:
    """Numbered lines start..end, 1-based and inclusive."""
    return [(n, lines[n - 1]) for n in range(start, min(end, len(lines)) + 1)]


def _collapse(body: list[Line]) -> list[Line]:
    """Drop leading and trailing blank lines and collapse each run of blank lines to one."""
    out: list[Line] = []
    for number, line in body:
        if line.strip() or (out and out[-1][1].strip()):
            out.append((number, line))
    while out and not out[-1][1].strip():
        out.pop()
    return out


# ── Chunk ─────────────────────────────────────────────────────────────────────


def _header(path: str, kind: str, name: str | None, qualname: str | None) -> str:
    """`path :: qualname (kind)`, falling back to the name, then to the bare path."""
    label = qualname or name
    return f"{path} :: {label} ({kind})" if label else f"{path} ({kind})"


def _chunk(
    *,
    path: str,
    kind: ChunkKind,
    name: str | None,
    qualname: str | None,
    signature: str | None,
    doc: str | None,
    lead: list[str],
    body: list[Line],
    span: tuple[int, int],
) -> Chunk:
    """Assemble header, lead lines and body into a chunk with its hash and search fields."""
    code = "\n".join(line for _, line in body)
    text = "\n".join([_header(path, kind, name, qualname), *lead, code])
    return Chunk(
        path=path,
        kind=kind,
        name=name,
        qualname=qualname,
        part=0,
        start_line=span[0],
        end_line=span[1],
        signature=signature,
        doc=doc,
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        tokens=estimate_tokens(text),
        truncated=False,
        search_a=split_identifiers(" ".join(filter(None, (name, qualname)))),
        search_b=split_identifiers(" ".join(filter(None, (signature, doc)))),
        search_c=split_identifiers(code),
    )
