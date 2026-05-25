"""Helpers for safely accessing DB rows that may be dicts (RealDictCursor) or tuples.

Provide a single utility used across the codebase to avoid repeated try/except patterns
and IndexError/KeyError surprises when callers assume tuple shapes.
"""
from typing import Any


def row_get(row: Any, key_or_index, default=None):
    """Safely get a value from a DB row.

    - If ``row`` is a mapping (dict-like), return row.get(key_or_index, default).
    - Otherwise, try to index by integer and return default on IndexError/TypeError.
    """
    if isinstance(row, dict):
        return row.get(key_or_index, default)
    try:
        return row[key_or_index]
    except (KeyError, IndexError, TypeError):
        return default
