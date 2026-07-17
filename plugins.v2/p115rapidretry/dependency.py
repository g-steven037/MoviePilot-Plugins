from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable


REQUIRED_ASYNCTOOLS_EXPORTS = (
    "ensure_async",
    "async_collect",
    "async_chain_from_iterable",
)


def ensure_asynctools_compatible(
    module: ModuleType | None = None,
    reload_module: Callable[[ModuleType], ModuleType] | None = None,
) -> ModuleType:
    """Reload an already-cached legacy module after dependency installation."""
    if module is None:
        module = importlib.import_module("asynctools")
    missing = [name for name in REQUIRED_ASYNCTOOLS_EXPORTS if not hasattr(module, name)]
    if missing:
        importlib.invalidate_caches()
        module = (reload_module or importlib.reload)(module)
        missing = [name for name in REQUIRED_ASYNCTOOLS_EXPORTS if not hasattr(module, name)]
    if missing:
        raise ImportError("python-asynctools>=0.2.1 is required")
    return module
