"""Shared callable introspection helpers.

Unifies the copy-pasted ``_accepts_keyword_arg`` function that existed in
four different files with conflicting fallback behavior (GH #1201).
"""

from __future__ import annotations

import inspect
from typing import Any


def accepts_keyword_arg(func: Any, name: str) -> bool:
    """Return True when *func* accepts *name* as a keyword argument.

    Checks both explicit parameter names and ``**kwargs``-style var-keyword
    parameters.  Falls back to ``False`` on introspection failure so the
    caller never accidentally enables a feature it cannot verify.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )
