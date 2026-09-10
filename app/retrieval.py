"""Retrieval helpers: split_identifiers() for full-text search. Pure functions, no I/O."""


def split_identifiers(text: str) -> str:
    """Each whitespace token followed by its identifier parts, when it has more than one."""
    out: list[str] = []
    for token in text.split():
        out.append(token)
        parts = _identifier_parts(token)
        if len(parts) > 1:
            out.extend(parts)
    return " ".join(out)


def _identifier_parts(token: str) -> list[str]:
    """Split on non-alphanumerics and at camelCase, acronym and letter/digit boundaries."""
    parts: list[str] = []
    current = ""
    for i, ch in enumerate(token):
        if not ch.isalnum():
            if current:
                parts.append(current)
            current = ""
            continue
        if current:
            prev = current[-1]
            following = token[i + 1] if i + 1 < len(token) else ""
            if (
                ch.isdigit() != prev.isdigit()
                or (ch.isupper() and prev.islower())
                or (ch.isupper() and prev.isupper() and following.islower())
            ):
                parts.append(current)
                current = ""
        current += ch
    if current:
        parts.append(current)
    return parts
