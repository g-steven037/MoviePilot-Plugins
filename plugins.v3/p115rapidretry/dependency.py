from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable


REQUIRED_ASYNCTOOLS_EXPORTS = (
    "ensure_async",
    "async_collect",
    "async_chain_from_iterable",
)
MINIMUM_ASYNCTOOLS_VERSION = (0, 2, 2)


def _version_tuple(module: ModuleType) -> tuple[int, ...]:
    value = getattr(module, "__version__", ())
    if isinstance(value, tuple) and all(isinstance(part, int) for part in value):
        return value
    if isinstance(value, str):
        try:
            return tuple(int(part) for part in value.split(".") if part.isdigit())
        except (TypeError, ValueError):
            return ()
    return ()


def _is_compatible(module: ModuleType) -> bool:
    return (
        _version_tuple(module) >= MINIMUM_ASYNCTOOLS_VERSION
        and all(hasattr(module, name) for name in REQUIRED_ASYNCTOOLS_EXPORTS)
    )


def ensure_asynctools_compatible(
    module: ModuleType | None = None,
    reload_module: Callable[[ModuleType], ModuleType] | None = None,
) -> ModuleType:
    """Require the Python 3.12-compatible release and refresh stale module caches."""
    if module is None:
        try:
            module = importlib.import_module("asynctools")
        except ImportError as exc:
            raise ImportError("python-asynctools>=0.2.2 is required") from exc
    if not _is_compatible(module):
        importlib.invalidate_caches()
        try:
            module = (reload_module or importlib.reload)(module)
        except ImportError as exc:
            raise ImportError("python-asynctools>=0.2.2 is required") from exc
    if not _is_compatible(module):
        raise ImportError("python-asynctools>=0.2.2 is required")
    return module
