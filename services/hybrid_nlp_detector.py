"""
Hybrid NLP detector for route-scoped, detection-only hints.

This module is intentionally narrow: it only supports
1) doubles disambiguation
2) special-booking classification
3) loop-break intent-shift detection

It never writes state directly. Callers must keep rule-based handlers authoritative.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Literal

from utils.structured_logging import log_quality_metric

logger = logging.getLogger("adella_chatbot.hybrid_nlp")

_ENV_ENABLED = "HYBRID_NLP_ENABLED"
_ENV_THRESHOLD = "HYBRID_NLP_CONFIDENCE_THRESHOLD"
_ENV_CALL_TIMEOUT = "HYBRID_NLP_CALL_TIMEOUT_SECONDS"
_DEFAULT_THRESHOLD = 0.72
_DEFAULT_CALL_TIMEOUT_SECONDS = 5.0

_DOUBLES_TYPES = frozenset({"mmf", "mff", "unknown"})
_SUPPLY_SOURCES = frozenset({"client", "escort", "unknown"})
_SPECIAL_BOOKING_TYPES = frozenset({"overnight", "dirty_weekend", "fly_me_to_you", "filming"})
_LOOP_BREAK_LABELS = frozenset(
    {"cancel", "modify", "deposit_resistance", "frustration", "continue_collecting"}
)
_OUTCALL_LOCATION_MODES = frozenset({"incall", "outcall", "unknown"})
_OUTCALL_VENUE_TYPES = frozenset({"hotel", "apartment", "home", "unknown"})
_TEMPORAL_URGENCY = frozenset({"asap", "today", "tomorrow", "scheduled", "unknown"})
_FLOW_SHIFT_LABELS = frozenset({"confirm", "modify", "cancel", "continue"})
_DEPOSIT_INTENT_LABELS = frozenset({"resistance", "question", "acceptance", "unknown"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_threshold(default: float = _DEFAULT_THRESHOLD) -> float:
    raw = os.getenv(_ENV_THRESHOLD, "")
    if raw == "":
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return max(0.0, min(1.0, val))


def _env_call_timeout(default: float = _DEFAULT_CALL_TIMEOUT_SECONDS) -> float:
    raw = os.getenv(_ENV_CALL_TIMEOUT, "")
    if raw == "":
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return max(1.0, min(20.0, val))


def _strip_json_fences(raw: str) -> str:
    text = (raw or "").strip()
    if "```" not in text:
        return text
    chunks = text.split("```")
    for ch in chunks:
        part = ch.strip()
        if part.lower().startswith("json"):
            part = part[4:].lstrip()
        if part.startswith("{") and "}" in part:
            return part
    return text


def _parse_json_obj(raw: str) -> dict[str, Any] | None:
    text = _strip_json_fences(raw)
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    return obj


def _state_snippet(state: dict[str, Any] | None) -> str:
    if not state:
        return ""
    keys = (
        "current_state",
        "booking_type",
        "experience_type",
        "doubles_type",
        "escort_supply_source",
        "booking_status",
        "deposit_required",
        "incall_outcall",
    )
    parts = []
    for k in keys:
        if state.get(k) is not None and str(state.get(k)).strip() != "":
            parts.append(f"{k}={state.get(k)}")
    return ", ".join(parts)


@dataclass(frozen=True)
class HybridDetectionResult:
    accepted: bool
    confidence: float
    hint: Any | None
    fallback_reason: str | None


@dataclass(frozen=True)
class DoublesHint:
    doubles_type: Literal["mmf", "mff", "unknown"]
    escort_supply_source: Literal["client", "escort", "unknown"]


@dataclass(frozen=True)
class SpecialBookingHint:
    booking_type: Literal["overnight", "dirty_weekend", "fly_me_to_you", "filming"]


@dataclass(frozen=True)
class LoopBreakHint:
    shift_label: Literal["cancel", "modify", "deposit_resistance", "frustration", "continue_collecting"]


@dataclass(frozen=True)
class OutcallVenueHint:
    location_mode: Literal["incall", "outcall", "unknown"]
    venue_type: Literal["hotel", "apartment", "home", "unknown"]


@dataclass(frozen=True)
class TemporalHint:
    urgency: Literal["asap", "today", "tomorrow", "scheduled", "unknown"]
    window_token: str


@dataclass(frozen=True)
class FlowShiftHint:
    shift_label: Literal["confirm", "modify", "cancel", "continue"]


@dataclass(frozen=True)
class DoublesSupplyHint:
    escort_supply_source: Literal["client", "escort", "unknown"]


@dataclass(frozen=True)
class DepositIntentHint:
    intent: Literal["resistance", "question", "acceptance", "unknown"]


class HybridNLPDetector:
    """Route-scoped hybrid detector with strict JSON validation + confidence gate."""

    def __init__(self, ai_service=None):
        self.ai_service = ai_service

    @staticmethod
    def enabled() -> bool:
        return _env_bool(_ENV_ENABLED, default=False)

    @staticmethod
    def confidence_threshold() -> float:
        return _env_threshold(default=_DEFAULT_THRESHOLD)

    def detect_doubles(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify this message for doubles booking disambiguation.
Return JSON only:
{{
  "route": "doubles",
  "doubles_type": "mmf|mff|unknown",
  "escort_supply_source": "client|escort|unknown",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""

        return self._detect(
            route="doubles",
            prompt=prompt,
            history=history,
            validator=self._validate_doubles,
        )

    def detect_special_booking(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify this message for special booking type.
Return JSON only:
{{
  "route": "special_booking",
  "booking_type": "overnight|dirty_weekend|fly_me_to_you|filming",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""

        return self._detect(
            route="special_booking",
            prompt=prompt,
            history=history,
            validator=self._validate_special_booking,
        )

    def detect_loop_break(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify this message for loop-break intent shift while collecting/checking booking details.
Return JSON only:
{{
  "route": "loop_break",
  "shift_label": "cancel|modify|deposit_resistance|frustration|continue_collecting",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""

        return self._detect(
            route="loop_break",
            prompt=prompt,
            history=history,
            validator=self._validate_loop_break,
        )

    def detect_outcall_venue(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify outcall vs incall intent and likely venue type.
Return JSON only:
{{
  "route": "outcall_venue",
  "location_mode": "incall|outcall|unknown",
  "venue_type": "hotel|apartment|home|unknown",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""
        return self._detect(
            route="outcall_venue",
            prompt=prompt,
            history=history,
            validator=self._validate_outcall_venue,
        )

    def detect_temporal_intent(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify temporal booking intent from this message.
Return JSON only:
{{
  "route": "temporal_intent",
  "urgency": "asap|today|tomorrow|scheduled|unknown",
  "window_token": "short normalized token or empty string",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""
        return self._detect(
            route="temporal_intent",
            prompt=prompt,
            history=history,
            validator=self._validate_temporal_intent,
        )

    def detect_flow_shift(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify whether this message is a flow pivot.
Return JSON only:
{{
  "route": "flow_shift",
  "shift_label": "confirm|modify|cancel|continue",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""
        return self._detect(
            route="flow_shift",
            prompt=prompt,
            history=history,
            validator=self._validate_flow_shift,
        )

    def detect_doubles_supply_clarity(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify who likely supplies the second person in a doubles booking.
Return JSON only:
{{
  "route": "doubles_supply_clarity",
  "escort_supply_source": "client|escort|unknown",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""
        return self._detect(
            route="doubles_supply_clarity",
            prompt=prompt,
            history=history,
            validator=self._validate_doubles_supply_clarity,
        )

    def detect_deposit_intent(
        self,
        *,
        message: str,
        state: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> HybridDetectionResult:
        prompt = f"""Classify the client's deposit intent.
Return JSON only:
{{
  "route": "deposit_intent",
  "intent": "resistance|question|acceptance|unknown",
  "confidence": 0.0
}}

Current booking state: {_state_snippet(state) or "none"}
Message: {message}
"""
        return self._detect(
            route="deposit_intent",
            prompt=prompt,
            history=history,
            validator=self._validate_deposit_intent,
        )

    def _detect(
        self,
        *,
        route: str,
        prompt: str,
        history: list[dict[str, str]] | None,
        validator,
    ) -> HybridDetectionResult:
        if not self.enabled():
            return self._reject(route, "feature_disabled")
        if not self.ai_service:
            return self._reject(route, "ai_service_unavailable")

        ai_service = self.ai_service
        short_history = (history or [])[-4:] or None
        raw: str | None = None
        call_error: Exception | None = None
        timeout_seconds = _env_call_timeout()

        def _invoke_model() -> None:
            nonlocal raw, call_error
            try:
                raw = ai_service.chat(
                    prompt,
                    system_prompt=(
                        "You are a strict JSON classifier. Output one JSON object only, "
                        "no markdown, no prose."
                    ),
                    history=short_history,
                    include_policy_context=False,
                )
            except Exception as e:
                call_error = e

        worker = threading.Thread(target=_invoke_model, daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            logger.warning(
                "Hybrid NLP route=%s timed out after %.2fs",
                route,
                timeout_seconds,
            )
            return self._reject(route, "ai_timeout")
        try:
            if call_error is not None:
                raise call_error
        except Exception as e:
            logger.warning("Hybrid NLP route=%s model call failed: %s", route, e)
            return self._reject(route, "ai_error")

        obj = _parse_json_obj(raw or "")
        if obj is None:
            return self._reject(route, "invalid_json")

        parsed_hint, confidence, reason = validator(obj)
        if reason:
            return self._reject(route, reason, confidence=confidence)
        if parsed_hint is None:
            return self._reject(route, "invalid_payload", confidence=confidence)

        threshold = self.confidence_threshold()
        if confidence < threshold:
            return self._reject(route, "below_threshold", confidence=confidence)

        self._log_decision(route=route, accepted=True, confidence=confidence, fallback_reason=None)
        return HybridDetectionResult(
            accepted=True,
            confidence=confidence,
            hint=parsed_hint,
            fallback_reason=None,
        )

    def _validate_doubles(self, obj: dict[str, Any]) -> tuple[DoublesHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        doubles_type = str(obj.get("doubles_type") or "").strip().lower()
        source = str(obj.get("escort_supply_source") or "").strip().lower()
        if doubles_type not in _DOUBLES_TYPES:
            return None, confidence, "invalid_doubles_type"
        if source not in _SUPPLY_SOURCES:
            return None, confidence, "invalid_supply_source"
        return DoublesHint(doubles_type=doubles_type, escort_supply_source=source), confidence, None  # type: ignore[arg-type]

    def _validate_special_booking(
        self, obj: dict[str, Any]
    ) -> tuple[SpecialBookingHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        booking_type = str(obj.get("booking_type") or "").strip().lower()
        if booking_type not in _SPECIAL_BOOKING_TYPES:
            return None, confidence, "invalid_booking_type"
        return SpecialBookingHint(booking_type=booking_type), confidence, None  # type: ignore[arg-type]

    def _validate_loop_break(self, obj: dict[str, Any]) -> tuple[LoopBreakHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        shift_label = str(obj.get("shift_label") or "").strip().lower()
        if shift_label not in _LOOP_BREAK_LABELS:
            return None, confidence, "invalid_shift_label"
        return LoopBreakHint(shift_label=shift_label), confidence, None  # type: ignore[arg-type]

    def _validate_outcall_venue(self, obj: dict[str, Any]) -> tuple[OutcallVenueHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        location_mode = str(obj.get("location_mode") or "").strip().lower()
        venue_type = str(obj.get("venue_type") or "").strip().lower()
        if location_mode not in _OUTCALL_LOCATION_MODES:
            return None, confidence, "invalid_location_mode"
        if venue_type not in _OUTCALL_VENUE_TYPES:
            return None, confidence, "invalid_venue_type"
        return OutcallVenueHint(location_mode=location_mode, venue_type=venue_type), confidence, None  # type: ignore[arg-type]

    def _validate_temporal_intent(self, obj: dict[str, Any]) -> tuple[TemporalHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        urgency = str(obj.get("urgency") or "").strip().lower()
        if urgency not in _TEMPORAL_URGENCY:
            return None, confidence, "invalid_temporal_urgency"
        window_token = str(obj.get("window_token") or "").strip().lower()
        return TemporalHint(urgency=urgency, window_token=window_token), confidence, None  # type: ignore[arg-type]

    def _validate_flow_shift(self, obj: dict[str, Any]) -> tuple[FlowShiftHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        shift_label = str(obj.get("shift_label") or "").strip().lower()
        if shift_label not in _FLOW_SHIFT_LABELS:
            return None, confidence, "invalid_flow_shift_label"
        return FlowShiftHint(shift_label=shift_label), confidence, None  # type: ignore[arg-type]

    def _validate_doubles_supply_clarity(
        self, obj: dict[str, Any]
    ) -> tuple[DoublesSupplyHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        source = str(obj.get("escort_supply_source") or "").strip().lower()
        if source not in _SUPPLY_SOURCES:
            return None, confidence, "invalid_supply_source"
        return DoublesSupplyHint(escort_supply_source=source), confidence, None  # type: ignore[arg-type]

    def _validate_deposit_intent(self, obj: dict[str, Any]) -> tuple[DepositIntentHint | None, float, str | None]:
        confidence = self._parse_confidence(obj.get("confidence"))
        intent = str(obj.get("intent") or "").strip().lower()
        if intent not in _DEPOSIT_INTENT_LABELS:
            return None, confidence, "invalid_deposit_intent"
        return DepositIntentHint(intent=intent), confidence, None  # type: ignore[arg-type]

    @staticmethod
    def _parse_confidence(value: Any) -> float:
        try:
            conf = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, conf))

    def _reject(self, route: str, reason: str, *, confidence: float = 0.0) -> HybridDetectionResult:
        self._log_decision(route=route, accepted=False, confidence=confidence, fallback_reason=reason)
        return HybridDetectionResult(
            accepted=False,
            confidence=confidence,
            hint=None,
            fallback_reason=reason,
        )

    @staticmethod
    def _log_decision(
        *,
        route: str,
        accepted: bool,
        confidence: float,
        fallback_reason: str | None,
    ) -> None:
        status = "accepted" if accepted else "rejected"
        logger.info(
            "Hybrid NLP route=%s status=%s confidence=%.3f fallback_reason=%s threshold=%.3f",
            route,
            status,
            confidence,
            fallback_reason or "",
            _env_threshold(default=_DEFAULT_THRESHOLD),
        )
        log_quality_metric(
            "hybrid_nlp_detector_decision",
            route=route,
            status=status,
            confidence=round(confidence, 4),
            fallback_reason=fallback_reason or "",
        )
