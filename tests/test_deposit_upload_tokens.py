"""Tests for deposit upload token reuse, rotation, and payment reference stability."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def patch_token_deps(monkeypatch, mock_db):
    monkeypatch.setattr("core.deposit_upload_tokens.get_shared_db", lambda _url: mock_db)
    monkeypatch.setattr("core.deposit_upload_tokens._supports_payment_reference_column", lambda _db: True)
    monkeypatch.setattr("core.deposit_upload_tokens.config.get_base_url", lambda: "https://example.test")


def test_reuses_token_when_attempts_remaining(patch_token_deps, mock_db):
    from core.deposit_upload_tokens import generate_deposit_upload_token

    mock_db.execute_query.return_value = [
        {"short_code": "REUSE1", "payment_reference": "12345"},
    ]
    r = generate_deposit_upload_token("+61400111222", 100, force_new=False)
    assert r == {
        "short_code": "REUSE1",
        "upload_url": "https://example.test/d/REUSE1",
        "payment_reference": "12345",
    }
    mock_db.execute_query.assert_called_once()
    sql = mock_db.execute_query.call_args[0][0]
    assert "SELECT short_code, payment_reference" in sql
    assert "upload_tokens" in sql


def test_no_reuse_inserts_new_token(patch_token_deps, mock_db, monkeypatch):
    from core.deposit_upload_tokens import generate_deposit_upload_token

    monkeypatch.setattr("core.deposit_upload_tokens.generate_short_code", lambda: "ROTATE")

    mock_db.execute_query.side_effect = [
        [],  # reuse: no row
        [],  # short_code ROTATE unused
        [],  # payment_reference candidate unused
        None,  # INSERT
    ]
    r = generate_deposit_upload_token("+61400111222", 100, force_new=False)
    assert r is not None
    assert r["short_code"] == "ROTATE"
    assert r["upload_url"] == "https://example.test/d/ROTATE"
    assert r["payment_reference"] is not None
    assert len(r["payment_reference"]) == 5
    assert mock_db.execute_query.call_count == 4
    insert_sql = mock_db.execute_query.call_args_list[3][0][0]
    assert "INSERT INTO upload_tokens" in insert_sql


def test_force_new_skips_reuse_goes_to_insert(patch_token_deps, mock_db, monkeypatch):
    from core.deposit_upload_tokens import generate_deposit_upload_token

    monkeypatch.setattr("core.deposit_upload_tokens.generate_short_code", lambda: "FORC01")

    mock_db.execute_query.side_effect = [
        [],  # short_code FORC01 unused
        [],  # payment ref unused
        None,  # INSERT
    ]
    r = generate_deposit_upload_token("+61400999888", 200, force_new=True)
    assert r is not None
    assert r["short_code"] == "FORC01"
    assert r["payment_reference"] is not None
    first_sql = mock_db.execute_query.call_args_list[0][0][0]
    assert "short_code" in first_sql
    assert "FROM upload_tokens" in first_sql


def test_resolve_deposit_upload_and_reference_returns_reused_token(patch_token_deps, mock_db):
    from core.deposit_upload_tokens import resolve_deposit_upload_and_reference

    mock_db.execute_query.return_value = [
        {"short_code": "REUSE1", "payment_reference": "12345"},
    ]
    u, ref = resolve_deposit_upload_and_reference("+61400111222", 100)
    assert u == "https://example.test/d/REUSE1"
    assert ref == "12345"
