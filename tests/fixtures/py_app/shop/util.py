"""Helpers shared across the shop."""

import functools


def slugify(text: str) -> str:
    """Lower-case and hyphenate a title.

        Extra indentation is removed.
    """
    return "-".join(text.lower().split())


@functools.cache
def tax_rate(country: str) -> float:
    return 0.2 if country == "FR" else 0.0


def banner(name: str) -> str:
    f"""Not a docstring: {name}."""
    return name
