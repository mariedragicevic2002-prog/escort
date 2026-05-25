"""
Simple feature-flag helpers backed by admin settings.
"""

from core.settings_manager import get_setting


def _setting_bool(key: str, default: bool = False) -> bool:
    raw_default = "true" if default else "false"
    value = (get_setting(key, raw_default) or raw_default).strip().lower()
    return value in ("true", "1", "yes", "on")


def optional_deposit_enabled() -> bool:
    return _setting_bool("optional_deposit_enabled", False)


def touring_auto_notify_enabled() -> bool:
    return _setting_bool("touring_auto_notify_enabled", False)


def analytics_api_enabled() -> bool:
    return _setting_bool("analytics_api_enabled", False)

