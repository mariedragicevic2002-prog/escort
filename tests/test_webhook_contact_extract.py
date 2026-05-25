"""Webhook helper: caller phone extraction for error-path SMS."""

from flask import Flask


def test_extract_webhook_contact_phone_json_data_contact():
    from flask import request

    from main_v2.webhook_helpers import extract_webhook_contact_phone

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"data": {"contact": "+61400111222", "content": "hi"}},
        content_type="application/json",
    ):
        assert extract_webhook_contact_phone(request) == "+61400111222"


def test_extract_webhook_contact_phone_flat_json_contact():
    from flask import request

    from main_v2.webhook_helpers import extract_webhook_contact_phone

    app = Flask(__name__)
    with app.test_request_context(
        "/webhook",
        method="POST",
        json={"contact": "+61400999333", "content": "yo"},
        content_type="application/json",
    ):
        assert extract_webhook_contact_phone(request) == "+61400999333"


def test_is_goodbye_false_when_hard_frustration_present():
    from main_v2.helpers import _is_goodbye

    assert _is_goodbye("this is stupid bye") is False
