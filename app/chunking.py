"""Chunking: symbol rows and plain files -> chunks, with parts, windows, manifests and a hash."""

import hashlib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from math import ceil
from pathlib import PurePosixPath
from typing import Literal

from app.parsing import Kind, SymbolRow, language_for
from app.retrieval import split_identifiers

ChunkKind = Kind | Literal["window", "manifest"]
Line = tuple[int, str]  # (1-based line number, text without the newline)

BYTES_PER_TOKEN = 3
PART_TOKENS, PART_OVERLAP = 1_200, 150
WINDOW_LINES, WINDOW_OVERLAP = 80, 15
CAP_TOKENS = 4_000
MANIFESTS = ("pyproject.toml", "package.json", "requirements*.txt")  # lower-case basenames
READMES = ("readme.md", "readme.markdown")
FENCES = ("```", "~~~")


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


@dataclass(frozen=True)
class _Meta:
    """The fields every chunk cut from one symbol or section shares."""

    path: str
    kind: ChunkKind
    name: str | None = None
    qualname: str | None = None
    signature: str | None = None
    doc: str | None = None


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
        meta = _Meta(path, row.kind, row.name, row.qualname, row.signature, row.doc)
        chunks.extend(
            _sized(meta, lead, [doc] if doc else [], body, (row.start_line, row.end_line))
        )
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


# ── Plain files ───────────────────────────────────────────────────────────────


def window_file(path: str, text: str) -> list[Chunk]:
    """Chunks of an unparsed file: one manifest chunk, README sections, or 80/15 line windows."""
    lines = _lines(text)
    if not any(line.strip() for line in lines):
        return []
    body = _span(lines, 1, len(lines))
    basename = PurePosixPath(path).name
    if any(fnmatchcase(basename.lower(), pattern) for pattern in MANIFESTS):
        return [_chunk(_Meta(path, "manifest", basename), [], body, (1, len(lines)))]
    if basename.lower() in READMES:
        chunks: list[Chunk] = []
        for name, raw in _sections(body):
            if section := _collapse(raw):
                span = (section[0][0], section[-1][0])
                chunks.extend(_sized(_Meta(path, "window", name), [], [], section, span))
        return chunks
    return [
        _chunk(_Meta(path, "window"), [], body[start:end], (body[start][0], body[end - 1][0]))
        for start, end in _windows([1] * len(body), WINDOW_LINES, WINDOW_OVERLAP)
        if any(line.strip() for _, line in body[start:end])
    ]


def _sections(body: list[Line]) -> list[tuple[str | None, list[Line]]]:
    """Markdown split at ATX headings outside code fences; the preamble has no name."""
    sections: list[tuple[str | None, list[Line]]] = [(None, [])]
    fence: str | None = None
    for number, line in body:
        marker = line.lstrip()[:3]
        if marker in FENCES:
            fence = marker if fence is None else (None if marker == fence else fence)
        elif fence is None and (title := _heading(line)) is not None:
            sections.append((title or None, []))
        sections[-1][1].append((number, line))
    return sections


def _heading(line: str) -> str | None:
    """The title of an ATX heading line (`#` to `######`), or None when it is not one."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return None
    level = len(stripped) - len(stripped.lstrip("#"))
    rest = stripped[level:]
    if not 1 <= level <= 6 or (rest and rest[0] not in " \t"):
        return None
    return rest.strip().rstrip("#").strip()


# ── Parts and windows ─────────────────────────────────────────────────────────


def _sized(
    meta: _Meta, lead: list[str], first_lead: list[str], body: list[Line], span: tuple[int, int]
) -> list[Chunk]:
    """One chunk, or overlapping parts when it is over PART_TOKENS; nothing when body is blank.

    `lead` is repeated on every part; `first_lead` (an outside doc) goes on part 1 only.
    """
    if not any(line.strip() for _, line in body):
        return []
    whole = "\n".join([_header(meta), *lead, *first_lead, *(line for _, line in body)])
    if estimate_tokens(whole) <= PART_TOKENS:
        return [_chunk(meta, [*lead, *first_lead], body, span)]
    weights = [estimate_tokens(line + "\n") for _, line in body]
    windows = _windows(weights, PART_TOKENS, PART_OVERLAP)
    return [
        _chunk(
            meta,
            [*lead, *(first_lead if i == 1 else [])],
            body[start:end],
            (body[start][0], body[end - 1][0]),
            part=(i, len(windows)),
        )
        for i, (start, end) in enumerate(windows, start=1)
    ]


def _windows(weights: list[int], size: int, overlap: int) -> list[tuple[int, int]]:
    """Greedy [start, end) windows of at most `size` weight (at least one item each).

    Each next window starts as late as possible while its leading items, shared with the previous
    window, still weigh at least `overlap`; it always starts after the previous start.
    """
    windows: list[tuple[int, int]] = []
    start = 0
    while start < len(weights):
        end, total = start, 0
        while end < len(weights) and (end == start or total + weights[end] <= size):
            total += weights[end]
            end += 1
        windows.append((start, end))
        if end == len(weights):
            break
        next_start, shared = end, 0
        while next_start > start + 1 and shared < overlap:
            next_start -= 1
            shared += weights[next_start]
        start = next_start
    return windows


# ── Lines ─────────────────────────────────────────────────────────────────────


def _lines(text: str) -> list[str]:
    """A file's lines, CRLF normalised; a trailing newline does not start a new line."""
    text = text.replace("\r\n", "\n")
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


def _header(meta: _Meta, part: tuple[int, int] = (0, 0)) -> str:
    """`path :: qualname (kind)`, falling back to the name, then to the bare path."""
    label = meta.qualname or meta.name
    kind = f"{meta.kind}, part {part[0]}/{part[1]}" if part[0] else meta.kind
    return f"{meta.path} :: {label} ({kind})" if label else f"{meta.path} ({kind})"


def _chunk(
    meta: _Meta,
    lead: list[str],
    body: list[Line],
    span: tuple[int, int],
    part: tuple[int, int] = (0, 0),
) -> Chunk:
    """Assemble header, lead lines and body into a chunk, capped at CAP_TOKENS, and hash it."""
    prefix = "\n".join([_header(meta, part), *lead])
    body, truncated = _cap(body, CAP_TOKENS * BYTES_PER_TOKEN - len(prefix.encode()) - 1)
    code = "\n".join(line for _, line in body)
    text = f"{prefix}\n{code}"
    end_line = body[-1][0] if truncated else span[1]
    return Chunk(
        path=meta.path,
        kind=meta.kind,
        name=meta.name,
        qualname=meta.qualname,
        part=part[0],
        start_line=span[0],
        end_line=end_line,
        signature=meta.signature,
        doc=meta.doc,
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        tokens=estimate_tokens(text),
        truncated=truncated,
        search_a=split_identifiers(" ".join(filter(None, (meta.name, meta.qualname)))),
        search_b=split_identifiers(" ".join(filter(None, (meta.signature, meta.doc)))),
        search_c=split_identifiers(code),
    )


def _cap(body: list[Line], budget: int) -> tuple[list[Line], bool]:
    """The longest whole-line prefix of body within `budget` bytes, or its first line cut short."""
    if len("\n".join(line for _, line in body).encode()) <= budget:
        return body, False
    kept: list[Line] = []
    used = -1  # the first line has no newline before it
    for number, line in body:
        used += len(line.encode()) + 1
        if used > budget:
            break
        kept.append((number, line))
    if not kept:
        number, line = body[0]
        kept = [(number, line.encode()[: max(budget, 0)].decode("utf-8", errors="ignore"))]
    return kept, True
