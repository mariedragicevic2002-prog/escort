from __future__ import annotations

from services.hybrid_nlp_detector import HybridNLPDetector


class _FakeAI:
    def __init__(self, response: str):
        self.response = response

    def chat(self, *_args, **_kwargs):
        return self.response


def test_hybrid_detector_disabled_by_default():
    detector = HybridNLPDetector(ai_service=_FakeAI('{"booking_type":"filming","confidence":0.95}'))
    result = detector.detect_special_booking(message="video shoot booking")
    assert result.accepted is False
    assert result.fallback_reason == "feature_disabled"


def test_hybrid_detector_accepts_valid_payload_above_threshold(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    detector = HybridNLPDetector(
        ai_service=_FakeAI('{"route":"special_booking","booking_type":"filming","confidence":0.91}')
    )
    result = detector.detect_special_booking(message="keen for a video shoot")
    assert result.accepted is True
    assert result.hint is not None
    assert result.hint.booking_type == "filming"
    assert result.confidence >= 0.91


def test_hybrid_detector_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    detector = HybridNLPDetector(ai_service=_FakeAI("not json"))
    result = detector.detect_doubles(message="keen for threesome")
    assert result.accepted is False
    assert result.fallback_reason == "invalid_json"


def test_hybrid_detector_rejects_low_confidence(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.85")
    detector = HybridNLPDetector(
        ai_service=_FakeAI(
            '{"route":"loop_break","shift_label":"cancel","confidence":0.51}'
        )
    )
    result = detector.detect_loop_break(message="nah leave it")
    assert result.accepted is False
    assert result.fallback_reason == "below_threshold"


def test_hybrid_detector_accepts_outcall_venue(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    detector = HybridNLPDetector(
        ai_service=_FakeAI(
            '{"route":"outcall_venue","location_mode":"outcall","venue_type":"hotel","confidence":0.93}'
        )
    )
    result = detector.detect_outcall_venue(message="can you come to my hotel?")
    assert result.accepted is True
    assert result.hint is not None
    assert result.hint.location_mode == "outcall"
    assert result.hint.venue_type == "hotel"


def test_hybrid_detector_accepts_temporal_intent(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    detector = HybridNLPDetector(
        ai_service=_FakeAI(
            '{"route":"temporal_intent","urgency":"tomorrow","window_token":"tomorrow-evening","confidence":0.90}'
        )
    )
    result = detector.detect_temporal_intent(message="tomorrow evening works")
    assert result.accepted is True
    assert result.hint is not None
    assert result.hint.urgency == "tomorrow"


def test_hybrid_detector_accepts_flow_shift(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    detector = HybridNLPDetector(
        ai_service=_FakeAI('{"route":"flow_shift","shift_label":"modify","confidence":0.92}')
    )
    result = detector.detect_flow_shift(message="change it to later")
    assert result.accepted is True
    assert result.hint is not None
    assert result.hint.shift_label == "modify"


def test_hybrid_detector_accepts_doubles_supply_clarity(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    detector = HybridNLPDetector(
        ai_service=_FakeAI(
            '{"route":"doubles_supply_clarity","escort_supply_source":"client","confidence":0.94}'
        )
    )
    result = detector.detect_doubles_supply_clarity(message="i will bring my friend")
    assert result.accepted is True
    assert result.hint is not None
    assert result.hint.escort_supply_source == "client"


def test_hybrid_detector_accepts_deposit_intent(monkeypatch):
    monkeypatch.setenv("HYBRID_NLP_ENABLED", "true")
    monkeypatch.setenv("HYBRID_NLP_CONFIDENCE_THRESHOLD", "0.70")
    detector = HybridNLPDetector(
        ai_service=_FakeAI('{"route":"deposit_intent","intent":"question","confidence":0.91}')
    )
    result = detector.detect_deposit_intent(message="why is deposit needed?")
    assert result.accepted is True
    assert result.hint is not None
    assert result.hint.intent == "question"
