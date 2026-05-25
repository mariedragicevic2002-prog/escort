"""

AI Service - Simple wrapper for Claude/Gemini API calls.
Primary: Claude. Fallback: Gemini.
Enhanced with structured extraction for booking fields.
"""



import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime
from typing import Any

from core.ai_policy_boundary import AI_DECISION_BOUNDARY_PROMPT
from core.client_profile import profile_to_prompt_snippet
from core.policy_retrieval import get_rates_summary_snippet
from core.prompt_registry import (
    PROMPT_VERSION as _PROMPT_VERSION,
    append_prompt_metadata,
    get_runtime_persona_prompt,
)
from utils.api_resilience import (
    AI_HTTP_TIMEOUT_SECONDS,
    GEMINI_HTTP_TIMEOUT_SECONDS,
    call_with_retry_anthropic,
    call_with_retry_gemini,
    chat_fallback_template_message,
)
from utils.circuit_breaker import CircuitBreakerOpenError, get_circuit_breaker

logger = logging.getLogger("adella_chatbot.ai_service")

_AI_CALL_LOG_SERVICE = None
_AI_CALL_LOG_DB_URL = ""


def _get_call_log_service():
    """Lazy import to avoid circular deps and startup cost."""
    global _AI_CALL_LOG_DB_URL, _AI_CALL_LOG_SERVICE
    try:
        from services.ai_call_log_service import AICallLogService
        from services.database_service import DatabaseService

        db_url = (os.environ.get("DATABASE_URL") or "").strip()
        if not db_url:
            return None
        if _AI_CALL_LOG_SERVICE is None or _AI_CALL_LOG_DB_URL != db_url:
            _AI_CALL_LOG_SERVICE = AICallLogService(DatabaseService(db_url))
            _AI_CALL_LOG_DB_URL = db_url
        return _AI_CALL_LOG_SERVICE
    except Exception:
        return None


def _usage_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except Exception:
        return 0


def _usage_value(payload: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
        try:
            value = getattr(payload, key)
        except Exception:
            value = None
        if value is not None:
            return value
    return None


def _extract_gemini_usage_counts(response: Any) -> tuple[int, int]:
    usage = (
        getattr(response, "usage_metadata", None)
        or getattr(response, "usageMetadata", None)
        or getattr(response, "usage", None)
        or {}
    )
    input_tokens = _usage_int(
        _usage_value(
            usage,
            "prompt_token_count",
            "promptTokenCount",
            "input_tokens",
            "inputTokens",
        )
    )
    output_tokens = _usage_int(
        _usage_value(
            usage,
            "candidates_token_count",
            "candidatesTokenCount",
            "output_tokens",
            "outputTokens",
        )
    )
    return input_tokens, output_tokens


# Gemini model: use config if set, else gemini-2.5-flash
def _get_gemini_model() -> str:
    try:
        import config
        return (getattr(config, "AI_MODEL_GEMINI", None) or "").strip() or "gemini-2.5-flash"
    except Exception as e:
        logger.warning("Gemini model from config failed, using default: %s", e)
        return "gemini-2.5-flash"


def _google_genai_client(api_key: str):
    """Gemini Developer API client (``google-genai`` unified SDK, replaces ``google-generativeai``)."""
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=int(GEMINI_HTTP_TIMEOUT_SECONDS * 1000)  # SDK expects ms
        ),
    )


def _get_effective_provider(provider: str | None) -> str:
    """Resolve provider: None = read from admin_settings; 'random' = claude or gemini per call."""
    if provider is not None and provider != "random":
        return provider
    try:
        from core.settings_manager import get_setting

        p = (get_setting("ai_provider") or "claude").strip()
    except Exception as e:
        logger.warning("Could not read ai_provider setting, using claude: %s", e)
        p = "claude"
    if p == "random":
        return random.choice(["claude", "gemini"])
    return p or "claude"


_PERSONALITY_DESCRIPTIONS = {
    "Flirty":       "Be warm, playful and lightly flirtatious. Use a friendly, inviting tone with light teasing.",
    "Sensual":      "Be intimate and alluring. Speak with quiet confidence, warmth and a hint of seduction.",
    "Playful":      "Be fun, light-hearted and cheeky. Use humour and keep the energy upbeat.",
    "Professional": "Be polished and concise. Maintain a respectful, business-like tone at all times.",
    "Luxurious":    "Be sophisticated and elegant. Use elevated language that evokes exclusivity and indulgence.",
    "Mysterious":   "Be intriguing and elusive. Give just enough to spark curiosity without revealing too much.",
    "Friendly":     "Be warm, approachable and genuine. Use a conversational, welcoming tone.",
    "Sultry":       "Be confident and seductive. Use a slow, deliberate tone with quiet allure.",
    "Sassy":        "Be bold, witty and a little cheeky. Keep it fun with a confident edge.",
    "Sweet":        "Be kind, caring and warm. Use gentle, sincere language that puts clients at ease.",
    "Direct":       "Be clear and to the point. No fluff \u2014 deliver what's needed efficiently.",
}

_TONE_MODIFIERS = {
    1: "Keep a strictly professional tone \u2014 no flirting at all.",
    2: "Lean professional; minimise flirting.",
    # 3 = neutral \u2014 let the personality description handle it
    4: "Be noticeably warm and friendly.",
    5: "Be openly flirtatious and playful.",
}

_LENGTH_INSTRUCTIONS = {
    1: "Reply in a single short sentence (under 60 characters).",
    2: "Keep replies to 1–2 short sentences (under 100 characters).",
    # 3 = default concise \u2014 no extra instruction needed
    4: "Replies can extend to 4–5 sentences when helpful.",
    5: "Provide detailed, thorough responses when appropriate.",
}


def _get_personality_prompt_suffix() -> str:
    """Build a system-prompt suffix from centralized prompt registry settings."""
    persona = get_runtime_persona_prompt()
    return f" {persona}" if persona else ""


def _parse_intent_router_response(raw: str, allowed: list[str]) -> str | None:
    """Parse JSON {intent, confidence} from the model; return intent if it is in allowed."""
    if not raw or not allowed:
        return None
    text = raw.strip()
    # Strip ```json ... ``` fences
    if "```" in text:
        chunks = text.split("```")
        for ch in chunks:
            p = ch.strip()
            if p.lower().startswith("json"):
                p = p[4:].lstrip()
            if p.startswith("{") and "}" in p:
                text = p
                break
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
    intent_val = obj.get("intent")
    if intent_val is None:
        return None
    intent_str = str(intent_val).strip()
    if not intent_str:
        return None
    allowed_lower = {a.lower(): a for a in allowed}
    if intent_str in allowed:
        return intent_str
    return allowed_lower.get(intent_str.lower())


class AIService:
    """AI service wrapper: Claude (primary), Gemini (fallback).
    Provider can be None to read from admin settings at runtime."""

    def __init__(self, provider: str | None = "claude"):
        """Initialize AI service. Pass provider=None to use admin setting ``ai_provider`` at runtime."""
        self.provider = provider  # None = read from settings each time
        self.claude_key = None
        self.gemini_key = None
        self._api_keys_loaded = False
        self._model_router = None
        self._model_router_loaded = False
        self._conversation_summarizer = None
        self._last_usage = {"model": "", "input_tokens": 0, "output_tokens": 0}

    def _reset_last_usage(self) -> None:
        self._last_usage = {"model": "", "input_tokens": 0, "output_tokens": 0}

    def _set_last_usage(self, model: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._last_usage = {
            "model": (model or "").strip(),
            "input_tokens": _usage_int(input_tokens),
            "output_tokens": _usage_int(output_tokens),
        }

    def _provider_default_model(self, provider: str) -> str:
        return _get_gemini_model() if provider == "gemini" else "claude-sonnet-4-6"

    def _get_model_router(self):
        if self._model_router_loaded:
            return self._model_router
        self._model_router_loaded = True
        try:
            from services.model_router import ModelRouter

            self._model_router = ModelRouter()
        except Exception as e:
            logger.warning("Model router unavailable: %s", e)
            self._model_router = None
        return self._model_router

    def _get_conversation_summarizer(self):
        if self._conversation_summarizer is not None:
            return self._conversation_summarizer
        try:
            from services.conversation_summarizer import ConversationSummarizer

            self._conversation_summarizer = ConversationSummarizer(self)
        except Exception as e:
            logger.warning("Conversation summarizer unavailable: %s", e)
            self._conversation_summarizer = None
        return self._conversation_summarizer

    def _log_ai_call(
        self,
        *,
        model: str,
        call_type: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        phone_number: str | None = None,
        prompt_version: str | None = None,
    ) -> None:
        """Fire-and-forget AI call logging. Never raises."""
        if not (model or "").strip():
            return

        def _do() -> None:
            svc = _get_call_log_service()
            if svc is None:
                return
            svc.log_call(
                phone_number=phone_number,
                model=model,
                call_type=call_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                prompt_version=prompt_version,
            )

        try:
            threading.Thread(target=_do, daemon=True).start()
        except Exception:
            pass

    def _maybe_log_last_usage(self, *, call_type: str, latency_ms: int, phone_number: str | None = None) -> None:
        usage = self._last_usage or {}
        model = str(usage.get("model") or "").strip()
        if not model:
            return
        self._log_ai_call(
            model=model,
            call_type=call_type,
            input_tokens=_usage_int(usage.get("input_tokens")),
            output_tokens=_usage_int(usage.get("output_tokens")),
            latency_ms=latency_ms,
            phone_number=phone_number,
            prompt_version=_PROMPT_VERSION,
        )

    def _ensure_api_keys(self) -> None:
        """Load API keys lazily from ``admin_settings`` via config getters."""
        if self._api_keys_loaded:
            return
        try:
            import config as _cfg

            self.claude_key = _cfg.get_anthropic_api_key()
            self.gemini_key = _cfg.get_gemini_api_key()
        except Exception as e:
            logger.warning("API key load failed (keys cleared): %s", e)
            self.claude_key = ""
            self.gemini_key = ""
        self._api_keys_loaded = True

    def chat(
        self,
        prompt: str,
        system_prompt: str | None = None,
        history: list | None = None,
        client_profile: dict[str, Any] | None = None,
        include_policy_context: bool = False,
    ) -> str:
        """
        Send a chat message to AI and get response.
        Provider is resolved from admin settings if initialized with provider=None.
        Fallback order: Claude → Gemini.

        Args:
            history: Optional prior turns as [{"role": "user"/"assistant", "content": "..."}]
                     for multi-turn conversation context (excludes the current prompt).
        """
        started_at = time.perf_counter()
        self._reset_last_usage()
        try:
            self._ensure_api_keys()
            effective = _get_effective_provider(self.provider)
            routed_provider = effective
            model_override = ""
            sys = (system_prompt or "").strip()
            profile_snippet = profile_to_prompt_snippet(client_profile)
            if profile_snippet:
                sys = f"{sys} {profile_snippet}".strip()
            if include_policy_context:
                rates_snippet = get_rates_summary_snippet()
                if rates_snippet:
                    sys = f"{sys} Rates info: {rates_snippet}".strip()
            sys = f"{sys} {AI_DECISION_BOUNDARY_PROMPT}".strip()
            sys = append_prompt_metadata(sys, key="runtime_chat")
            sys = (sys or "") + _get_personality_prompt_suffix()
            if not self.claude_key and not self.gemini_key:
                return (
                    "AI service not configured. Save API keys on the Config page or set "
                    "ANTHROPIC_API_KEY/CLAUDE_API_KEY/GEMINI_API_KEY."
                )
            phone_number = None
            state = None
            if isinstance(client_profile, dict):
                phone_number = (client_profile.get("phone_number") or client_profile.get("phone") or "").strip() or None
                state = client_profile.get("active_state") or client_profile.get("current_state")
            router = self._get_model_router()
            if router is not None:
                try:
                    routed_provider, model_override = router.route(
                        prompt,
                        state=state,
                        history=history,
                        configured_provider=effective,
                    )
                except Exception as router_err:
                    logger.warning("Model routing failed, using configured provider: %s", router_err)
            reply = self._chat_with_fallback_chain(
                prompt,
                sys or None,
                history,
                routed_provider,
                model_override=model_override,
            )
            self._maybe_log_last_usage(
                call_type="chat",
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                phone_number=phone_number,
            )
            return reply
        except Exception as e:
            logger.error("AI chat error: %s", e)
            return chat_fallback_template_message()

    def summarize_text(self, prompt: str, max_tokens: int = 150) -> str:
        """Lightweight AI call for summarization tasks.

        Uses circuit-breaker-protected provider calls but skips the heavy
        ``chat()`` overhead (no client profile, no policy context, no model
        routing, no personality prompt suffix).  Prefers Gemini (cheaper/faster)
        and falls back to Claude.
        """
        try:
            self._ensure_api_keys()
        except Exception as exc:
            logger.warning("summarize_text key load failed: %s", exc)
            return ""
        max_tok = max_tokens
        if self.gemini_key:
            try:
                cb = get_circuit_breaker(
                    "ai_gemini",
                    failure_threshold=5,
                    recovery_timeout=120.0,
                    expected_exception=Exception,
                )
                result = cb.call(lambda: self._chat_gemini(prompt, max_tokens=max_tok))
                return str(result or "").strip()
            except Exception as exc:
                logger.warning("summarize_text gemini failed: %s", exc)
        if self.claude_key:
            try:
                cb = get_circuit_breaker(
                    "ai_claude",
                    failure_threshold=5,
                    recovery_timeout=120.0,
                    expected_exception=Exception,
                )
                result = cb.call(lambda: self._chat_claude(prompt, max_tokens=max_tok))
                return str(result or "").strip()
            except Exception as exc:
                logger.warning("summarize_text claude failed: %s", exc)
        return ""

    def stream_chat(self, prompt: str, system_prompt: str | None = None, model: str | None = None, max_tokens: int = 600):
        """Yield text chunks as they arrive from the AI provider (for SSE / streaming responses).

        Prefers the requested ``model`` provider, falls back to the other.
        Yields plain string chunks (not SSE-formatted).  Raises on complete
        failure so callers can emit an error event.
        """
        self._ensure_api_keys()
        effective = (model or "").strip().lower()
        if effective not in ("claude", "gemini"):
            effective = _get_effective_provider(self.provider)
        sys = (system_prompt or "").strip() or "You are a helpful assistant."
        order = ["gemini", "claude"] if effective == "gemini" else ["claude", "gemini"]
        for provider in order:
            if provider == "gemini" and self.gemini_key:
                try:
                    yield from self._stream_gemini(prompt, sys, max_tokens)
                    return
                except Exception as exc:
                    logger.warning("stream_chat gemini failed: %s", exc)
            elif provider == "claude" and self.claude_key:
                try:
                    yield from self._stream_claude(prompt, sys, max_tokens)
                    return
                except Exception as exc:
                    logger.warning("stream_chat claude failed: %s", exc)
        raise RuntimeError("All AI providers failed or unconfigured for streaming.")

    def _stream_gemini(self, prompt: str, system_prompt: str, max_tokens: int):
        """Yield text chunks from Gemini streaming API."""
        from google import genai
        from google.genai import types

        client = _google_genai_client(self.gemini_key or "")
        selected_model = _get_gemini_model()
        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        for chunk in client.models.generate_content_stream(
            model=selected_model,
            contents=contents,  # type: ignore[arg-type]
            config=types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                system_instruction=system_prompt or None,
            ),
        ):
            text = getattr(chunk, "text", None) or ""
            if text:
                yield text

    def _stream_claude(self, prompt: str, system_prompt: str, max_tokens: int):
        """Yield text chunks from Claude streaming API."""
        import anthropic

        client = anthropic.Anthropic(api_key=self.claude_key, timeout=AI_HTTP_TIMEOUT_SECONDS)
        messages = [{"role": "user", "content": prompt}]
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,  # type: ignore[arg-type]
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text

    def _chat_with_fallback_chain(
        self,
        prompt: str,
        system_prompt: str | None,
        history: list | None,
        effective: str,
        *,
        model_override: str = "",
    ) -> str:
        """Try primary provider, then alternate, then static template (Claude → Gemini → template)."""
        if effective == "claude":
            order = [
                ("claude", lambda: self._try_claude_chat(prompt, system_prompt, history, model=model_override)),
                ("gemini", lambda: self._try_gemini_chat(prompt, system_prompt, history)),
            ]
        else:
            order = [
                ("gemini", lambda: self._try_gemini_chat(prompt, system_prompt, history, model=model_override)),
                ("claude", lambda: self._try_claude_chat(prompt, system_prompt, history)),
            ]
        for name, fn in order:
            try:
                return fn()
            except CircuitBreakerOpenError:
                logger.warning("AI circuit open for %s — trying next provider", name)
                continue
            except Exception as e:
                logger.warning("AI provider %s failed: %s", name, e)
                continue
        return chat_fallback_template_message()

    def _try_claude_chat(
        self,
        prompt: str,
        system_prompt: str | None,
        history: list | None,
        *,
        model: str = "",
    ) -> str:
        if not self.claude_key:
            raise RuntimeError("no claude key")
        cb = get_circuit_breaker(
            "ai_claude",
            failure_threshold=5,
            recovery_timeout=120.0,
            expected_exception=Exception,
        )
        return cb.call(
            lambda: self._chat_claude(
                prompt,
                system_prompt,
                model=(model or "claude-sonnet-4-6"),
                history=history,
            )
        )

    def _try_gemini_chat(
        self,
        prompt: str,
        system_prompt: str | None,
        history: list | None,
        *,
        model: str = "",
    ) -> str:
        if not self.gemini_key:
            raise RuntimeError("no gemini key")
        cb = get_circuit_breaker(
            "ai_gemini",
            failure_threshold=5,
            recovery_timeout=120.0,
            expected_exception=Exception,
        )
        return cb.call(lambda: self._chat_gemini(prompt, system_prompt, history=history, model=model or None))

    @staticmethod
    def _prepare_claude_messages(history: list | None, prompt: str) -> list:
        """
        Build Claude-compatible messages list from history + current prompt.
        Merges consecutive same-role turns (Claude requires strict alternation)
        and ensures the list starts with a 'user' turn.
        """
        messages = []
        for turn in (history or []):
            role = turn.get("role", "user")
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})
        # Drop leading assistant turns (Claude requires starting with 'user')
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        messages.append({"role": "user", "content": prompt})
        return messages

    def _chat_claude(self, prompt: str, system_prompt: str | None = None, max_tokens: int = 400, model: str = "claude-sonnet-4-6", history: list | None = None) -> str:
        """Chat with Claude (tenacity retries + explicit timeout)."""
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self.claude_key, timeout=AI_HTTP_TIMEOUT_SECONDS)
            messages = self._prepare_claude_messages(history, prompt)

            def _do():
                response = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt or "You are a helpful assistant.",
                    messages=messages,
                    timeout=AI_HTTP_TIMEOUT_SECONDS,
                )
                if not response.content:
                    raise ValueError("Empty response from Claude API")
                usage = getattr(response, "usage", None)
                self._set_last_usage(
                    model,
                    getattr(usage, "input_tokens", 0),
                    getattr(usage, "output_tokens", 0),
                )
                return response.content[0].text

            return call_with_retry_anthropic(_do)

        except ImportError:
            return "Anthropic library not installed. Run: pip install anthropic"
        except Exception as e:
            logger.error("Claude API error: %s", e)
            raise

    def _chat_gemini(
        self,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int = 400,
        history: list | None = None,
        model: str | None = None,
    ) -> str:
        """Chat with Gemini (tenacity retries + explicit timeout).

        MED-01 fix: system_prompt is passed via system_instruction so it is
        structurally separated from user content, preventing prompt injection
        via crafted client messages that contain system-role markers.
        """
        try:
            from google.genai import types

            contents: list[types.Content] = []
            for turn in (history or []):
                role = "user" if turn.get("role") == "user" else "model"
                text = (turn.get("content") or "").strip()
                if text:
                    contents.append(
                        types.Content(role=role, parts=[types.Part(text=text)])
                    )
            contents.append(
                types.Content(role="user", parts=[types.Part(text=prompt)])
            )

            client = _google_genai_client(self.gemini_key or "")
            selected_model = (model or _get_gemini_model()).strip() or _get_gemini_model()

            def _do():
                response = client.models.generate_content(
                    model=selected_model,
                    contents=contents,  # type: ignore[arg-type]
                    config=types.GenerateContentConfig(
                        max_output_tokens=max_tokens,
                        system_instruction=system_prompt or None,
                    ),
                )
                if not getattr(response, "text", None):
                    raise ValueError("Empty response from Gemini API")
                input_tokens, output_tokens = _extract_gemini_usage_counts(response)
                self._set_last_usage(selected_model, input_tokens, output_tokens)
                return response.text

            return call_with_retry_gemini(_do)

        except ImportError:
            return "Google GenAI library not installed. Run: pip install \"google-genai>=1.20,<2\""
        except Exception as e:
            logger.error("Gemini API error: %s", e)
            raise

    def get_troubleshoot_advice(self, user_issue: str, system_prompt: str | None = None) -> dict[str, Any]:
        """
        Get troubleshooting advice: Claude first, Gemini as fallback or second opinion.
        Returns dict with claude_response (or None), gemini_response (or None), and which succeeded.
        """
        default_system = (
            "You are an expert troubleshooter for an SMS chatbot system. "
            "The system uses: httpSMS (Android app + httpsms.com cloud relay) for SMS, "
            "Google Calendar for bookings, a database for conversation state, "
            "and Claude/Gemini for AI classification. "
            "Give concise, step-by-step troubleshooting advice. Focus on: config/API keys, httpSMS app connectivity, "
            "httpsms.com dashboard setup, calendar token expiry, database connectivity, and common misconfigurations."
        )
        sys_prompt = system_prompt or default_system
        max_tok = 800
        result: dict[str, str | None] = {"claude_response": None, "gemini_response": None, "claude_error": None, "gemini_error": None}
        self._ensure_api_keys()

        if self.claude_key:
            try:
                result["claude_response"] = self._chat_claude(user_issue, sys_prompt, max_tokens=max_tok)
            except Exception as e:
                result["claude_error"] = str(e)
                logger.warning(f"Claude troubleshoot failed: {e}")

        if self.gemini_key:
            try:
                result["gemini_response"] = self._chat_gemini(user_issue, sys_prompt, max_tokens=max_tok)
            except Exception as e:
                result["gemini_error"] = str(e)
                logger.warning(f"Gemini troubleshoot failed: {e}")

        return result

    def extract_booking_fields(
        self,
        message: str,
        current_date: datetime | None = None,
        history: list | None = None,
        phone_number: str | None = None,
    ) -> dict[str, Any]:
        """
        Extract booking fields from natural language message using AI.
        Falls back gracefully if AI is unavailable.

        Args:
            message: User message text
            current_date: Current datetime for context (defaults to now)

        Returns:
            Dict with extracted fields: {
                'date': datetime or None,
                'time': (hour, minute) tuple or None,
                'duration': int (minutes) or None,
                'experience_type': 'GFE' or 'PSE' or None,
                'incall_outcall': 'incall' or 'outcall' or None,
                'outcall_address': str or None
            }
        """
        started_at = time.perf_counter()
        self._reset_last_usage()
        self._ensure_api_keys()
        if not self.claude_key and not self.gemini_key:
            logger.debug("AI not configured, skipping AI extraction")
            return {}

        if current_date is None:
            from utils.timezone import get_current_datetime

            current_date = get_current_datetime()

        assert current_date is not None
        extraction_prompt = self._build_extraction_prompt(message, current_date, history)

        try:
            import concurrent.futures

            _EXTRACTION_HARD_DEADLINE = 28.0
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(self._extract_with_fallback_chain, extraction_prompt)
                result = _fut.result(timeout=_EXTRACTION_HARD_DEADLINE)
            parsed = self._parse_extraction_result(result, current_date)
            self._maybe_log_last_usage(
                call_type="extraction",
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                phone_number=phone_number,
            )
            return parsed
        except concurrent.futures.TimeoutError:
            logger.warning("AI extraction hard deadline exceeded (%.0fs) — falling back", _EXTRACTION_HARD_DEADLINE)
            return {}
        except Exception as e:
            logger.warning("AI extraction failed: %s — falling back to pattern matching", e)
            return {}

    def _build_extraction_prompt(self, message: str, current_date: datetime, history: list | None = None) -> str:
        """Build prompt for structured field extraction."""
        date_str = current_date.strftime("%A, %B %d, %Y at %I:%M %p")

        # Embed recent conversation turns so AI can resolve references like "same time as before"
        history_section = ""
        if history:
            if len(history) >= 6:
                summarizer = self._get_conversation_summarizer()
                if summarizer is not None:
                    compressed = summarizer.compress_history(history)
                    formatted = summarizer.format_for_prompt(compressed)
                    if formatted:
                        history_section = "\n" + formatted + "\n"
            if not history_section:
                recent = history[-4:]
                lines = []
                for turn in recent:
                    role_label = "user" if turn.get("role") == "user" else "assistant"
                    content = (turn.get("content") or "").strip()
                    if content:
                        # A1: XML delimiters prevent injected history from escaping its role context
                        lines.append(f"<{role_label}_turn>{content}</{role_label}_turn>")
                if lines:
                    history_section = "\nRecent conversation (read-only context):\n" + "\n".join(lines) + "\n"

        system_prompt = f"""You are a booking assistant. Extract booking information from user messages.

Current date/time context: {date_str}

Extract ONLY the following fields if mentioned:
- date: Date for booking (convert relative dates like "tomorrow", "Friday" to actual dates)
- time: Time for booking in 24-hour format as (hour, minute) tuple
- duration: Duration in minutes (e.g., "1 hour" = 60, "2 hours" = 120, "30 min" = 30)
- experience_type: "GFE" or "PSE" if mentioned
- incall_outcall: "incall" or "outcall" if mentioned
- outcall_address: Specific hotel name or street address ONLY. Do NOT extract vague phrases like "my place", "my home", "my apartment", "my house", "my hotel", "here", "home" — set to null if no specific address is given.

Also include confidence scores 0.0-1.0 for each field:
- date_confidence
- time_confidence
- duration_confidence
- experience_type_confidence
- incall_outcall_confidence
- outcall_address_confidence

Return ONLY valid JSON in this exact format:
{{
    "date": "YYYY-MM-DD" or null,
    "time": [hour, minute] or null,
    "duration": minutes or null,
    "experience_type": "GFE" or "PSE" or null,
    "incall_outcall": "incall" or "outcall" or null,
    "outcall_address": "string" or null,
    "date_confidence": 0.0,
    "time_confidence": 0.0,
    "duration_confidence": 0.0,
    "experience_type_confidence": 0.0,
    "incall_outcall_confidence": 0.0,
    "outcall_address_confidence": 0.0
}}

If a field is not mentioned, use null. Be strict - only extract what is clearly stated."""

        return f"{system_prompt}{history_section}\nUser message: {message}\n\nExtracted fields (JSON only):"

    def _extract_with_fallback_chain(self, extraction_prompt: str) -> str:
        """Claude → Gemini with circuit breaker + tenacity (same order as chat)."""
        effective = _get_effective_provider(self.provider)
        if effective == "claude":
            order = [("claude", self._extract_with_claude), ("gemini", self._extract_with_gemini)]
        else:
            order = [("gemini", self._extract_with_gemini), ("claude", self._extract_with_claude)]
        last_err: Exception | None = None
        for name, fn in order:
            try:
                if name == "claude" and not self.claude_key:
                    continue
                if name == "gemini" and not self.gemini_key:
                    continue
                return fn(extraction_prompt)
            except CircuitBreakerOpenError:
                logger.warning("AI extract circuit open for %s — trying next provider", name)
                continue
            except Exception as e:
                last_err = e
                logger.warning("AI extract provider %s failed: %s", name, e)
                continue
        if last_err:
            raise last_err
        raise RuntimeError("no AI keys available for extraction")

    def _extract_with_claude(self, prompt: str) -> str:
        """Extract fields using Claude with structured output."""
        import anthropic

        client = anthropic.Anthropic(api_key=self.claude_key, timeout=AI_HTTP_TIMEOUT_SECONDS)
        cb = get_circuit_breaker(
            "ai_claude",
            failure_threshold=5,
            recovery_timeout=120.0,
            expected_exception=Exception,
        )

        def _do():
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system="You are a helpful assistant that extracts structured data from text. Always return valid JSON only.",
                messages=[{"role": "user", "content": prompt}],
                timeout=AI_HTTP_TIMEOUT_SECONDS,
            )
            if not response.content:
                raise ValueError("Empty response from Claude extraction API")
            usage = getattr(response, "usage", None)
            self._set_last_usage(
                "claude-sonnet-4-6",
                getattr(usage, "input_tokens", 0),
                getattr(usage, "output_tokens", 0),
            )
            return response.content[0].text

        try:
            return cb.call(lambda: call_with_retry_anthropic(_do))
        except Exception as e:
            logger.error("Claude extraction error: %s", e)
            raise

    def _extract_with_gemini(self, prompt: str) -> str:
        """Extract fields using Gemini with structured output."""
        from google.genai import types

        client = _google_genai_client(self.gemini_key or "")
        cb = get_circuit_breaker(
            "ai_gemini",
            failure_threshold=5,
            recovery_timeout=120.0,
            expected_exception=Exception,
        )
        selected_model = _get_gemini_model()

        def _do():
            response = client.models.generate_content(
                model=selected_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=500,
                    response_mime_type="application/json",
                ),
            )
            if not getattr(response, "text", None):
                raise ValueError("Empty response from Gemini extraction API")
            input_tokens, output_tokens = _extract_gemini_usage_counts(response)
            self._set_last_usage(selected_model, input_tokens, output_tokens)
            return response.text

        try:
            return cb.call(lambda: call_with_retry_gemini(_do))
        except Exception as e:
            logger.error("Gemini extraction error: %s", e)
            raise

    def _parse_date_field(self, data: dict, extracted: dict, _current_date) -> None:
        """Parse the date field from extraction data into extracted dict."""
        if not data.get("date"):
            return
        try:
            from datetime import datetime as _dt

            from utils.timezone import get_local_timezone

            tz = get_local_timezone()
            extracted["date"] = tz.localize(_dt.strptime(data["date"], "%Y-%m-%d"))
        except (ValueError, KeyError) as e:
            logger.warning("Could not parse extracted date: %s", e)

    def _parse_time_field(self, data: dict, extracted: dict) -> None:
        """Parse the time field from extraction data into extracted dict."""
        if not (data.get("time") and isinstance(data["time"], list) and len(data["time"]) == 2):
            return
        hour, minute = data["time"]
        if isinstance(hour, int) and isinstance(minute, int) and 0 <= hour <= 23 and 0 <= minute <= 59:
            extracted["time"] = (hour, minute)

    def _parse_duration_field(self, data: dict, extracted: dict) -> None:
        """Parse the duration field from extraction data into extracted dict."""
        if not data.get("duration"):
            return
        try:
            duration = int(data["duration"])
            if duration > 0:
                extracted["duration"] = duration
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse extracted duration: %s", e)

    def _parse_address_field(self, data: dict, extracted: dict) -> None:
        """Parse the outcall_address field, guarding against time-text false positives."""
        if not data.get("outcall_address"):
            return
        address = str(data["outcall_address"]).strip()
        if len(address) <= 3:
            return
        _time_like = bool(
            re.fullmatch(
                r"(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+(?:today|tonight|tomorrow|now|asap))?",
                address.lower(),
                re.IGNORECASE,
            )
        )
        if not _time_like:
            extracted["outcall_address"] = address

    def _append_confidence_metadata(self, data: dict[str, Any], extracted: dict[str, Any]) -> None:
        confidence: dict[str, float] = {}
        low_confidence_fields: list[str] = []
        for field in ["date", "time", "duration", "experience_type", "incall_outcall", "outcall_address"]:
            raw_value = data.get(f"{field}_confidence")
            if raw_value is None:
                continue
            try:
                score = max(0.0, min(float(raw_value), 1.0))
            except (TypeError, ValueError):
                continue
            confidence[field] = score
            if score < 0.7 and field in extracted:
                low_confidence_fields.append(field)
        if confidence:
            extracted["_confidence"] = confidence
        if low_confidence_fields:
            extracted["_low_confidence_fields"] = low_confidence_fields

    def _parse_extraction_result(self, result: str, _current_date) -> dict[str, Any]:
        """Parse AI extraction result into structured fields."""
        extracted = {}

        try:
            result = result.strip()
            if "```json" in result:
                _, _, rest = result.partition("```json")
                result = rest.partition("```")[0].strip()
            elif "```" in result:
                _, _, rest = result.partition("```")
                result = rest.partition("```")[0].strip()

            data = json.loads(result)

            self._parse_date_field(data, extracted, _current_date)
            self._parse_time_field(data, extracted)
            self._parse_duration_field(data, extracted)

            if data.get("experience_type"):
                exp_type = str(data["experience_type"]).upper()
                if exp_type in ["GFE", "PSE"]:
                    extracted["experience_type"] = exp_type

            if data.get("incall_outcall"):
                loc_type = str(data["incall_outcall"]).lower()
                if loc_type in ["incall", "outcall"]:
                    extracted["incall_outcall"] = loc_type

            self._parse_address_field(data, extracted)
            self._append_confidence_metadata(data, extracted)

            logger.info(f"AI extracted fields: {list(extracted.keys())}")

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse AI extraction result as JSON: {e}")
        except Exception as e:
            logger.warning(f"Error parsing extraction result: {e}")

        return extracted

    def classify_intent(
        self,
        message: str,
        possible_intents: list,
        hint: str = "",
        history: list | None = None,
        intent_descriptions: dict[str, str] | None = None,
    ) -> str | None:
        """
        Classify message intent using AI (fallback when pattern matching fails).

        Args:
            message: User message
            possible_intents: List of possible intent strings (machine-facing names)
            hint: Optional context hint (e.g. conversation state, collected fields, bot's last reply)
            history: Optional recent conversation turns for context
            intent_descriptions: Optional intent -> one-line description for the router prompt

        Returns:
            Intent string or None
        """
        started_at = time.perf_counter()
        self._reset_last_usage()
        self._ensure_api_keys()
        if not self.claude_key and not self.gemini_key:
            return None

        allowed = [str(i).strip() for i in possible_intents if str(i).strip()]
        if not allowed:
            return None

        lines = []
        for intent in allowed:
            desc = (intent_descriptions or {}).get(intent, "").strip()
            if desc:
                lines.append(f'- "{intent}": {desc}')
            else:
                lines.append(f'- "{intent}"')
        catalog = "\n".join(lines)
        hint_line = f"\n\nContext:\n{hint}" if hint else ""

        prompt = f"""Choose exactly one intent for the user message below.

Valid intents (use the exact key string in JSON):
{catalog}{hint_line}

User message:
{message}

Respond with a single JSON object only, no markdown, no other text:
{{"intent":"<one of the intent keys above>","confidence":0.0}}

confidence is 0.0–1.0 for how sure you are."""

        sys_prompt = (
            "You are a strict JSON classifier. Output only valid JSON: "
            '{"intent":"<key>","confidence":<number>}. '
            "The intent value must be exactly one key from the list."
        )
        short_history = (history or [])[-4:] or None
        classify_max_tokens = 120
        try:
            effective = _get_effective_provider(self.provider)
            if effective == "claude":
                order = [
                    (
                        "claude",
                        lambda: self._try_classify_claude(
                            prompt, sys_prompt, short_history, max_tokens=classify_max_tokens
                        ),
                    ),
                    (
                        "gemini",
                        lambda: self._try_classify_gemini(
                            prompt, sys_prompt, short_history, max_tokens=classify_max_tokens
                        ),
                    ),
                ]
            else:
                order = [
                    (
                        "gemini",
                        lambda: self._try_classify_gemini(
                            prompt, sys_prompt, short_history, max_tokens=classify_max_tokens
                        ),
                    ),
                    (
                        "claude",
                        lambda: self._try_classify_claude(
                            prompt, sys_prompt, short_history, max_tokens=classify_max_tokens
                        ),
                    ),
                ]
            result: str | None = None
            for name, fn in order:
                try:
                    result = fn()
                    if result is not None:
                        break
                except CircuitBreakerOpenError:
                    logger.warning("AI classify circuit open for %s — trying next provider", name)
                    continue
                except Exception as e:
                    logger.warning("AI classify provider %s failed: %s", name, e)
                    continue
            if not result:
                return None

            self._maybe_log_last_usage(
                call_type="classification",
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
            parsed = _parse_intent_router_response(result, allowed)
            if parsed:
                return parsed

            result_lower = result.strip().lower()
            for intent in allowed:
                if intent.lower() == result_lower or intent.lower() in result_lower:
                    return intent

            return None

        except Exception as e:
            logger.warning("AI intent classification failed: %s", e)
            return None

    def _try_classify_claude(
        self, prompt: str, sys_prompt: str, short_history: list | None, *, max_tokens: int = 120
    ) -> str:
        if not self.claude_key:
            raise RuntimeError("no claude key")
        cb = get_circuit_breaker(
            "ai_claude",
            failure_threshold=5,
            recovery_timeout=120.0,
            expected_exception=Exception,
        )
        return cb.call(
            lambda: self._chat_claude(
                prompt,
                sys_prompt,
                max_tokens=max_tokens,
                model="claude-haiku-4-5-20251001",
                history=short_history,
            )
        )

    def _try_classify_gemini(
        self, prompt: str, sys_prompt: str, short_history: list | None, *, max_tokens: int = 120
    ) -> str:
        if not self.gemini_key:
            raise RuntimeError("no gemini key")
        cb = get_circuit_breaker(
            "ai_gemini",
            failure_threshold=5,
            recovery_timeout=120.0,
            expected_exception=Exception,
        )
        return cb.call(
            lambda: self._chat_gemini(prompt, sys_prompt, max_tokens=max_tokens, history=short_history)
        )
