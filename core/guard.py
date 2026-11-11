"""Guard wrapper: a hook adapter must never raise into Hermes."""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable

log = logging.getLogger("thumbs-fix")


def guard(fn: Callable[..., Any], *, default: Any = None, name: str | None = None) -> Callable[..., Any]:
    """Wrap fn so every exception is logged and `default` is returned instead."""
    label = name or getattr(fn, "__name__", "hook")

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - the whole point is to catch everything
            log.exception("thumbs-fix: %s failed; ignoring", label)
            return default

    return wrapper
