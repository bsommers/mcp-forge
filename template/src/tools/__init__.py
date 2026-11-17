"""Stable bootstrap (locked): auto-registers every sibling module exposing register(mcp).

Generic by design -- add tools by dropping new modules next to this file; never edit this one.
"""
import importlib
import pkgutil
from typing import Any


def register(mcp: Any) -> None:
    for mod in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        fn = getattr(module, "register", None)
        if callable(fn):
            fn(mcp)
