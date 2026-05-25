"""
Unified configuration entry point for refactor2.

ALL code must import config through this module.
Direct imports from legacy config modules are forbidden.

Infrastructure layer — may import from config_governance internals.
"""
from __future__ import annotations

import os
from importlib import import_module
from typing import Any, Callable, Protocol


class ConfigGetter(Protocol):
    def __call__(self, key: str, default: Any = None) -> Any: ...


def _load_callable(module_name: str, attribute: str) -> Callable[..., Any] | None:
    try:
        module = import_module(module_name)
    except Exception:
        return None
    candidate = getattr(module, attribute, None)
    return candidate if callable(candidate) else None


def _governance_getter() -> Callable[[str], Any] | None:
    for module_name in (
        "app.config_governance.registry",
        "app.config_governance.registry",
    ):
        getter = _load_callable(module_name, "get_config_value")
        if getter is not None:
            return getter
    return None


def _settings_getter() -> ConfigGetter | None:
    for module_name in (
        "core.settings_manager",
        "refactor.core.settings_manager",
    ):
        getter = _load_callable(module_name, "get_setting")
        if getter is not None:
            return getter
    return None


def _module_value(module_name: str, key: str) -> Any:
    try:
        module = import_module(module_name)
    except Exception:
        return None
    return getattr(module, key, None)


def get(key: str, default: Any = None) -> Any:
    """Retrieve a config value by key."""
    if not key:
        return default

    governance_getter = _governance_getter()
    if governance_getter is not None:
        try:
            value = governance_getter(key)
        except Exception:
            value = None
        if value is not None:
            return value

    settings_getter = _settings_getter()
    if settings_getter is not None:
        try:
            value = settings_getter(key, None)
        except TypeError:
            try:
                value = settings_getter(key)
            except Exception:
                value = None
        except Exception:
            value = None
        if value is not None:
            return value

    for module_name in ("config", "app.config"):
        value = _module_value(module_name, key)
        if value is not None:
            return value

    return os.environ.get(key, default)


def get_config(key: str, default: Any = None) -> Any:
    """Backward-compatible alias for the unified config getter."""
    return get(key, default)


def require(key: str) -> str:
    """Retrieve a required config value. Raises RuntimeError if missing."""
    value = get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RuntimeError(f"Required config key missing: {key!r}")
    return str(value)


__all__ = ["get", "get_config", "require"]
