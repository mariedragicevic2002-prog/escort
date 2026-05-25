from services.escalation_service import evaluate_escalation


def test_escalation_service_triggers_manual_review_for_emotional_threat():
    result = evaluate_escalation(
        message="this is a scam and I will report you to police",
        intent="general_question",
        current_state={"profanity_count": 0},
        client_context={"total_bookings": 0},
    )
    assert result["triggered"] is True
    assert "escalate_manual_review" in result["tags"]


def test_escalation_service_flags_vip_context():
    result = evaluate_escalation(
        message="can we book this week",
        intent="book_appointment",
        current_state={"profanity_count": 0},
        client_context={"total_bookings": 12},
    )
    assert result["triggered"] is True
    assert "escalate_vip_context" in result["tags"]
