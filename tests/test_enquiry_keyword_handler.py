"""ENQUIRY keyword handler — acknowledgement vs how-to prompt."""

from __future__ import annotations

from handlers.new_conv.enquiries_simple import handle_enquiry_keyword


def test_enquiry_keyword_with_question_uses_ack_not_generic_howto():
    ctx = {
        "message": "ENQUIRY do you tour Melbourne",
        "phone_number": "+61400111222",
        "state": {"current_state": "NEW"},
        "state_manager": None,
    }
    out = handle_enquiry_keyword(ctx)
    msg = (out.get("messages") or [""])[0]
    assert "I've received your question" in msg
    assert "Melbourne" in msg or "tour" in msg.lower()
    assert "Example: 'ENQUIRY" not in msg


def test_enquiry_keyword_bare_falls_back_to_howto():
    ctx = {
        "message": "ENQUIRY ",
        "phone_number": "+61400111222",
        "state": {"current_state": "NEW"},
        "state_manager": None,
    }
    out = handle_enquiry_keyword(ctx)
    msg = (out.get("messages") or [""])[0]
    assert "reply with ENQUIRY" in msg


def test_enquiry_question_received_truncates_long_body():
    from templates.enquiry_templates import get_enquiry_question_received_message

    long_q = "word " * 80
    msg = get_enquiry_question_received_message(long_q)
    assert len(msg) < len(long_q) + 200
    assert "..." in msg or len(long_q) <= 200
