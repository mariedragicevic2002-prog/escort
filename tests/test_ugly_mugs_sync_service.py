from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import ugly_mugs_sync_service as svc


def _make_settings_store(initial: dict[str, str] | None = None) -> tuple[dict[str, str], list[tuple[str, str]]]:
    settings = dict(initial or {})
    writes: list[tuple[str, str]] = []
    return settings, writes


def _install_settings_mocks(monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], writes: list[tuple[str, str]]) -> None:
    monkeypatch.setattr(svc, "get_setting", lambda key: settings.get(key))

    def _set_setting(key: str, value: str) -> bool:
        settings[key] = value
        writes.append((key, value))
        return True

    monkeypatch.setattr(svc, "set_setting", _set_setting)


def test_run_ugly_mugs_sync_skips_when_disabled(monkeypatch: pytest.MonkeyPatch):
    settings, writes = _make_settings_store(
        {
            svc.SETTING_SYNC_ENABLED: "false",
        }
    )
    _install_settings_mocks(monkeypatch, settings, writes)
    monkeypatch.setattr(svc, "_create_scraper", lambda: (_ for _ in ()).throw(AssertionError("should not run scraper")))

    result = svc.run_ugly_mugs_sync()

    assert result["status"] == "skipped"
    assert result["reason"] == "disabled"
    assert settings[svc.META_LAST_STATUS] == "skipped"
    assert "Sync disabled" in settings[svc.META_LAST_ERROR]
    assert writes


def test_run_ugly_mugs_sync_skips_when_credentials_missing(monkeypatch: pytest.MonkeyPatch):
    settings, writes = _make_settings_store(
        {
            svc.SETTING_SYNC_ENABLED: "true",
            svc.SETTING_USERNAME: "",
            svc.SETTING_PASSWORD: "",
        }
    )
    _install_settings_mocks(monkeypatch, settings, writes)

    result = svc.run_ugly_mugs_sync()

    assert result["status"] == "skipped"
    assert result["reason"] == "missing_credentials"
    assert settings[svc.META_LAST_STATUS] == "skipped"
    assert settings[svc.META_LAST_ERROR] == "Missing credentials"
    assert writes


def test_run_ugly_mugs_sync_success(monkeypatch: pytest.MonkeyPatch, tmp_path):
    export_path = tmp_path / "ugly_mugs_export.xlsx"
    settings, writes = _make_settings_store(
        {
            svc.SETTING_SYNC_ENABLED: "true",
            svc.SETTING_USERNAME: "user",
            svc.SETTING_PASSWORD: "pass",
            svc.SETTING_START_PAGE: "1",
            svc.SETTING_TOTAL_PAGES: "2",
            svc.SETTING_EXPORT_PATH: str(export_path),
            svc.SETTING_PAGE_DELAY_SECONDS: "0",
        }
    )
    _install_settings_mocks(monkeypatch, settings, writes)

    fake_scraper = object()
    monkeypatch.setattr(svc, "_create_scraper", lambda: fake_scraper)
    monkeypatch.setattr(svc, "_login", lambda scraper, username, password: None)
    monkeypatch.setattr(
        svc,
        "_scrape_lookup_numbers",
        lambda scraper, **kwargs: (
            {
                "61400111222": {"raw": "0400 111 222", "first_page": 1, "occurrences": 2},
                "61400999888": {"raw": "+61 400 999 888", "first_page": 2, "occurrences": 1},
            },
            [(2, "temporary timeout")],
        ),
    )

    workbook_calls: list[dict[str, object]] = []

    def _fake_write_export_workbook(**kwargs):
        workbook_calls.append(kwargs)

    monkeypatch.setattr(svc, "_write_export_workbook", _fake_write_export_workbook)

    watchlist_calls: list[tuple[list[tuple[str, str]], str]] = []

    def _fake_replace_watchlist(numbers, filename=""):
        watchlist_calls.append((numbers, filename))
        return {"inserted": len(numbers)}

    monkeypatch.setattr(svc, "replace_watchlist", _fake_replace_watchlist)

    result = svc.run_ugly_mugs_sync()

    assert result["status"] == "success"
    assert result["inserted"] == 2
    assert result["unique_count"] == 2
    assert result["failed_pages_count"] == 1
    assert settings[svc.META_LAST_STATUS] == "success"
    assert settings[svc.META_LAST_ERROR] == ""
    assert int(settings[svc.META_LAST_COUNT]) == 2
    assert workbook_calls
    assert watchlist_calls
    assert watchlist_calls[0][1] == export_path.name
    assert writes


def test_run_ugly_mugs_sync_failure_sets_failed_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path):
    settings, writes = _make_settings_store(
        {
            svc.SETTING_SYNC_ENABLED: "true",
            svc.SETTING_USERNAME: "user",
            svc.SETTING_PASSWORD: "pass",
            svc.SETTING_EXPORT_PATH: str(tmp_path / "ugly_mugs_export.xlsx"),
            svc.SETTING_PAGE_DELAY_SECONDS: "0",
        }
    )
    _install_settings_mocks(monkeypatch, settings, writes)
    monkeypatch.setattr(svc, "_create_scraper", lambda: object())
    monkeypatch.setattr(svc, "_login", lambda scraper, username, password: (_ for _ in ()).throw(RuntimeError("login failed")))

    with pytest.raises(RuntimeError, match="login failed"):
        svc.run_ugly_mugs_sync()

    assert settings[svc.META_LAST_STATUS] == "failed"
    assert "login failed" in settings[svc.META_LAST_ERROR]
    assert settings[svc.META_LAST_RUN_AT]
    datetime.fromisoformat(settings[svc.META_LAST_RUN_AT].replace("Z", "+00:00")).astimezone(timezone.utc)
    assert writes


def test_get_ugly_mugs_sync_config_reads_admin_legacy_keys(monkeypatch: pytest.MonkeyPatch, tmp_path):
    export_path = tmp_path / "ugly_mugs_admin.xlsx"
    settings, writes = _make_settings_store(
        {
            svc.LEGACY_SETTING_SYNC_ENABLED: "true",
            svc.LEGACY_SETTING_USERNAME: "legacy-user",
            svc.LEGACY_SETTING_PASSWORD: "legacy-pass",
            svc.LEGACY_SETTING_TOTAL_PAGES: "42",
            svc.LEGACY_SETTING_EXPORT_PATH: str(export_path),
            svc.LEGACY_SETTING_SCHEDULE_HOUR: "10",
            svc.LEGACY_SETTING_SCHEDULE_MINUTE: "35",
            svc.LEGACY_SETTING_SCHEDULE_TIMEZONE: "Australia/Adelaide",
        }
    )
    _install_settings_mocks(monkeypatch, settings, writes)

    cfg = svc.get_ugly_mugs_sync_config()
    schedule = svc.get_ugly_mugs_sync_schedule()

    assert cfg.enabled is True
    assert cfg.username == "legacy-user"
    assert cfg.password == "legacy-pass"
    assert cfg.total_pages == 42
    assert cfg.export_path == str(export_path)
    assert schedule.hour == 10
    assert schedule.minute == 35
    assert schedule.timezone == "Australia/Adelaide"


def test_get_ugly_mugs_sync_config_reads_lookup_env_aliases(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(svc, "get_setting", lambda key: None)
    monkeypatch.setenv("UGLY_MUGS_SYNC_ENABLED", "true")
    monkeypatch.setenv("UGLY_MUGS_LOOKUP_USERNAME", "env-user")
    monkeypatch.setenv("UGLY_MUGS_LOOKUP_PASSWORD", "env-pass")
    monkeypatch.setenv("UGLY_MUGS_LOOKUP_TOTAL_PAGES", "11")
    monkeypatch.setenv("UGLY_MUGS_SYNC_HOUR_LOCAL", "8")
    monkeypatch.setenv("UGLY_MUGS_SYNC_MINUTE_LOCAL", "45")

    cfg = svc.get_ugly_mugs_sync_config()
    schedule = svc.get_ugly_mugs_sync_schedule()

    assert cfg.enabled is True
    assert cfg.username == "env-user"
    assert cfg.password == "env-pass"
    assert cfg.total_pages == 11
    assert schedule.hour == 8
    assert schedule.minute == 45


def test_run_ugly_mugs_sync_mirrors_legacy_metadata(monkeypatch: pytest.MonkeyPatch):
    settings, writes = _make_settings_store({svc.SETTING_SYNC_ENABLED: "false"})
    _install_settings_mocks(monkeypatch, settings, writes)

    result = svc.run_ugly_mugs_sync()

    assert result["status"] == "skipped"
    assert settings[svc.LEGACY_META_LAST_STATUS] == "skipped"
    assert settings[svc.LEGACY_META_LAST_COUNT] == "0"
    assert settings[svc.LEGACY_META_LAST_ERROR] == "Sync disabled"
    assert settings[svc.LEGACY_META_LAST_RUN_AT]
