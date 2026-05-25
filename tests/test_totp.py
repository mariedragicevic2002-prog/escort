"""
Phase 3 — Admin 2FA (TOTP + backup codes) unit tests.

Covers admin/totp.py end-to-end without touching a real database: we patch
core.settings_manager.get_setting / set_setting to use an in-memory dict.

Golden rules under test:
  - A valid TOTP verifies; a wrong TOTP does not.
  - Backup codes are one-use only — the same code cannot be redeemed twice.
  - Disabling 2FA wipes the secret AND the stored backup code hashes.
  - ADMIN_2FA_DISABLED env bypass makes is_enabled() return False even when
    the DB flag is "true" (recovery path for a lost phone).
"""

import json

import pyotp
import pytest

from admin import totp as admin_totp


@pytest.fixture
def settings_store(monkeypatch):
    """In-memory replacement for core.settings_manager used by admin.totp."""
    store: dict[str, str] = {}

    def _get(key, default=None):
        v = store.get(key, default)
        return v if v is not None else default

    def _set(key, value):
        store[key] = value

    monkeypatch.setattr("core.settings_manager.get_setting", _get)
    monkeypatch.setattr("core.settings_manager.set_setting", _set)
    # Ensure the env bypass isn't accidentally active from the test runner shell.
    monkeypatch.delenv("ADMIN_2FA_DISABLED", raising=False)
    return store


# ---------------------------------------------------------------------------
# Secret + TOTP verification
# ---------------------------------------------------------------------------

class TestTotpRoundTrip:
    def test_generated_secret_is_base32_of_expected_length(self, settings_store):
        secret = admin_totp.generate_new_secret()
        # pyotp default is 32 base32 chars (160 bits)
        assert len(secret) == 32
        assert set(secret).issubset(set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"))

    def test_verify_accepts_current_code(self, settings_store):
        secret = admin_totp.generate_new_secret()
        current = pyotp.TOTP(secret).now()
        assert admin_totp.verify_totp(current, secret=secret) is True

    def test_verify_rejects_wrong_code(self, settings_store):
        secret = admin_totp.generate_new_secret()
        assert admin_totp.verify_totp("000000", secret=secret) is False

    def test_verify_rejects_non_numeric(self, settings_store):
        secret = admin_totp.generate_new_secret()
        assert admin_totp.verify_totp("abcdef", secret=secret) is False

    def test_verify_rejects_wrong_length(self, settings_store):
        secret = admin_totp.generate_new_secret()
        current = pyotp.TOTP(secret).now()
        assert admin_totp.verify_totp(current[:5], secret=secret) is False

    def test_verify_uses_stored_secret_when_not_supplied(self, settings_store):
        secret = admin_totp.generate_new_secret()
        settings_store["admin_totp_secret"] = secret
        current = pyotp.TOTP(secret).now()
        assert admin_totp.verify_totp(current) is True

    def test_verify_with_no_stored_secret_returns_false(self, settings_store):
        assert admin_totp.verify_totp("123456") is False


# ---------------------------------------------------------------------------
# Enrollment lifecycle
# ---------------------------------------------------------------------------

class TestEnrollment:
    def test_is_enabled_false_when_no_secret(self, settings_store):
        settings_store["admin_totp_enabled"] = "true"
        assert admin_totp.is_enabled() is False

    def test_is_enabled_false_when_flag_off(self, settings_store):
        settings_store["admin_totp_secret"] = "JBSWY3DPEHPK3PXP"
        assert admin_totp.is_enabled() is False

    def test_is_enabled_true_when_flag_and_secret(self, settings_store):
        settings_store["admin_totp_enabled"] = "true"
        settings_store["admin_totp_secret"] = "JBSWY3DPEHPK3PXP"
        assert admin_totp.is_enabled() is True

    def test_finalize_enrollment_persists_secret_and_issues_codes(self, settings_store):
        secret = admin_totp.generate_new_secret()
        current = pyotp.TOTP(secret).now()
        ok, codes = admin_totp.finalize_enrollment(secret, current)
        assert ok is True
        assert codes is not None
        assert len(codes) == admin_totp.BACKUP_CODE_COUNT
        assert settings_store["admin_totp_secret"] == secret
        assert settings_store["admin_totp_enabled"] == "true"
        assert settings_store.get("admin_2fa_delivery") == "totp"
        assert settings_store["admin_backup_codes_hashed"]  # non-empty JSON

    def test_finalize_enrollment_rejects_bad_code(self, settings_store):
        secret = admin_totp.generate_new_secret()
        ok, codes = admin_totp.finalize_enrollment(secret, "000000")
        assert ok is False
        assert codes is None
        # Must NOT have persisted anything
        assert "admin_totp_secret" not in settings_store
        assert "admin_totp_enabled" not in settings_store

    def test_has_pending_setup(self, settings_store):
        settings_store["admin_totp_enabled"] = "true"
        assert admin_totp.has_pending_setup() is True
        settings_store["admin_totp_secret"] = "X"
        assert admin_totp.has_pending_setup() is False


# ---------------------------------------------------------------------------
# Backup codes — one-use enforcement
# ---------------------------------------------------------------------------

class TestBackupCodes:
    def test_stored_hashes_not_plaintext(self, settings_store):
        codes = ["abc1234567", "def7654321"]
        admin_totp.store_backup_codes(codes)
        stored = json.loads(settings_store["admin_backup_codes_hashed"])
        # Hashes must not be equal to the plaintext codes
        for h, plain in zip(stored, codes):
            assert h != plain
            assert len(h) > 20  # argon2 or werkzeug pbkdf2

    def test_verify_and_consume_valid_code(self, settings_store):
        codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(codes)
        assert admin_totp.verify_and_consume_backup_code(codes[0]) is True

    def test_same_code_cannot_be_used_twice(self, settings_store):
        """GOLDEN RULE: backup codes are strictly one-use."""
        codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(codes)
        assert admin_totp.verify_and_consume_backup_code(codes[0]) is True
        assert admin_totp.verify_and_consume_backup_code(codes[0]) is False

    def test_consumed_code_removed_from_store(self, settings_store):
        codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(codes)
        admin_totp.verify_and_consume_backup_code(codes[0])
        remaining = json.loads(settings_store["admin_backup_codes_hashed"])
        assert len(remaining) == len(codes) - 1

    def test_wrong_code_does_not_decrement_remaining(self, settings_store):
        codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(codes)
        assert admin_totp.verify_and_consume_backup_code("wrongwrong") is False
        remaining = json.loads(settings_store["admin_backup_codes_hashed"])
        assert len(remaining) == len(codes)

    def test_code_is_case_insensitive(self, settings_store):
        codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(codes)
        # Hex codes are lowercase — verify uppercase still works via _normalize_code
        assert admin_totp.verify_and_consume_backup_code(codes[0].upper()) is True

    def test_code_ignores_whitespace_and_dashes(self, settings_store):
        codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(codes)
        code = codes[0]
        # Format like "abc12 34567" or "abc1-234567" should still verify
        spaced = f"{code[:5]} {code[5:]}"
        assert admin_totp.verify_and_consume_backup_code(spaced) is True

    def test_backup_codes_remaining_count(self, settings_store):
        codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(codes)
        assert admin_totp.backup_codes_remaining() == len(codes)
        admin_totp.verify_and_consume_backup_code(codes[0])
        assert admin_totp.backup_codes_remaining() == len(codes) - 1

    def test_regenerate_invalidates_old_codes(self, settings_store):
        old_codes = admin_totp.generate_backup_codes()
        admin_totp.store_backup_codes(old_codes)
        new_codes = admin_totp.regenerate_backup_codes()
        # Old codes no longer work
        assert admin_totp.verify_and_consume_backup_code(old_codes[0]) is False
        # New ones do
        assert admin_totp.verify_and_consume_backup_code(new_codes[0]) is True


# ---------------------------------------------------------------------------
# SMS delivery flag (gateway + phone mocked)
# ---------------------------------------------------------------------------

class TestSmsDelivery:
    def test_is_enabled_true_when_sms_ready(self, settings_store, monkeypatch):
        settings_store["admin_totp_enabled"] = "true"
        settings_store["admin_2fa_delivery"] = "sms"
        monkeypatch.setattr(admin_totp, "sms_2fa_ready", lambda: True)
        assert admin_totp.is_enabled() is True

    def test_is_enabled_false_when_sms_not_ready(self, settings_store, monkeypatch):
        settings_store["admin_totp_enabled"] = "true"
        settings_store["admin_2fa_delivery"] = "sms"
        monkeypatch.setattr(admin_totp, "sms_2fa_ready", lambda: False)
        assert admin_totp.is_enabled() is False


# ---------------------------------------------------------------------------
# Disable + env bypass (recovery paths)
# ---------------------------------------------------------------------------

class TestDisableAnd2FABypass:
    def test_disable_wipes_secret_and_codes(self, settings_store):
        settings_store["admin_totp_secret"] = "JBSWY3DPEHPK3PXP"
        settings_store["admin_totp_enabled"] = "true"
        settings_store["admin_2fa_delivery"] = "sms"
        settings_store["admin_2fa_sms_phone"] = "+61400000000"
        admin_totp.store_backup_codes(admin_totp.generate_backup_codes())
        admin_totp.disable_2fa()
        assert settings_store["admin_totp_enabled"] == "false"
        assert settings_store["admin_totp_secret"] == ""
        assert settings_store["admin_backup_codes_hashed"] == ""
        assert settings_store.get("admin_2fa_delivery") == "totp"
        assert settings_store.get("admin_2fa_sms_phone") == ""

    def test_env_bypass_disables_2fa_check(self, settings_store, monkeypatch):
        settings_store["admin_totp_enabled"] = "true"
        settings_store["admin_totp_secret"] = "JBSWY3DPEHPK3PXP"
        monkeypatch.setenv("ADMIN_2FA_DISABLED", "1")
        assert admin_totp.is_enabled() is False

    def test_env_bypass_value_variants(self, settings_store, monkeypatch):
        settings_store["admin_totp_enabled"] = "true"
        settings_store["admin_totp_secret"] = "JBSWY3DPEHPK3PXP"
        for val in ("1", "true", "yes"):
            monkeypatch.setenv("ADMIN_2FA_DISABLED", val)
            assert admin_totp.is_enabled() is False, f"value {val!r} should disable"
