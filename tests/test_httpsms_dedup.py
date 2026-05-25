"""Unit tests for inbound SMS dedup key + claim behaviour."""

from unittest.mock import MagicMock

import pytest


def test_build_inbound_dedup_key_prefers_message_id():
    from services.httpsms_dedup import build_inbound_dedup_key

    assert build_inbound_dedup_key({"message_id": " mid-1 "}, {}) == "mid-1"


def test_build_inbound_dedup_key_nested_data_and_payload_id():
    from services.httpsms_dedup import build_inbound_dedup_key

    payload = {"data": {"contact": "+61", "id": "from-nested"}}
    msg = payload["data"]
    assert build_inbound_dedup_key(msg, payload) == "from-nested"
    assert build_inbound_dedup_key({}, {"id": "top-level"}) == "top-level"


def test_build_inbound_dedup_key_timestamp_fallback_stable():
    """When no message id, use phone + provider timestamp + body (not body alone)."""
    from services.httpsms_dedup import build_inbound_dedup_key

    msg = {"contact": "+61", "content": "hi", "received_at": "2026-01-15T10:00:00Z"}
    k1 = build_inbound_dedup_key(msg, {}, phone_number="+61400111222", message_body="hi")
    k2 = build_inbound_dedup_key(msg, {}, phone_number="+61400111222", message_body="hi")
    assert k1.startswith("tsfb:")
    assert k1 == k2


def test_build_inbound_dedup_key_no_body_only_without_timestamp():
    from services.httpsms_dedup import build_inbound_dedup_key

    assert (
        build_inbound_dedup_key(
            {"content": "same text"},
            {},
            phone_number="+61400111222",
            message_body="same text",
        )
        == ""
    )


def test_try_claim_unique_violation_is_duplicate():
    from services.httpsms_dedup import try_claim_httpsms_message_id
    from psycopg2 import errors as pg_errors

    db = MagicMock()
    db.execute_query.side_effect = pg_errors.UniqueViolation("dup")
    assert try_claim_httpsms_message_id(db, "x") is False


def test_try_claim_operational_error_propagates():
    from services.httpsms_dedup import try_claim_httpsms_message_id
    from psycopg2 import OperationalError

    db = MagicMock()
    db.execute_query.side_effect = OperationalError("conn")
    with pytest.raises(OperationalError):
        try_claim_httpsms_message_id(db, "x")
