"""
Conversation summarizer.

Compresses older conversation turns into a rolling summary.
Prevents context window overflow in long conversations while preserving key details.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("adella_chatbot.conversation_summarizer")


class ConversationSummarizer:
    """Builds rolling conversation summaries for long booking threads."""

    def __init__(self, ai_service=None):
        self.ai_service = ai_service

    def should_summarize(self, history: list) -> bool:
        """Return True when the conversation is long enough to compress.

        Triggers on turn count (>= 6) OR total character volume (> 3000 chars)
        so that a few very long turns also trigger compression.
        """
        turns = history or []
        if len(turns) >= 6:
            return True
        total_chars = sum(len(str((t or {}).get("content") or "")) for t in turns)
        return total_chars > 3000

    def summarize(self, history: list) -> str:
        """Summarize the supplied conversation turns into a short factual note."""
        if not history:
            return ""
        prompt = (
            "Summarize this booking conversation in under 100 words, focusing on: "
            "what was agreed, key details collected (date, time, type), any issues. "
            "Be factual and concise.\n\n"
            + self._join_turns(history)
        )
        try:
            ai_result = self._summarize_with_ai(prompt)
            if ai_result:
                return self._truncate_words(ai_result.strip(), 100)
        except Exception as exc:
            logger.warning("conversation summary generation failed: %s", exc)
        return self._fallback_summary(history)

    def compress_history(self, history: list) -> dict:
        """Return a summary plus the last two turns kept verbatim."""
        if self.should_summarize(history):
            return {
                "summary": self.summarize((history or [])[:-2]),
                "recent_turns": (history or [])[-2:],
                "compressed": True,
            }
        return {
            "summary": "",
            "recent_turns": history or [],
            "compressed": False,
        }

    def format_for_prompt(self, compressed: dict) -> str:
        """Format compressed history for downstream AI prompts."""
        recent = self._format_recent_turns((compressed or {}).get("recent_turns") or [])
        if (compressed or {}).get("compressed"):
            summary = str((compressed or {}).get("summary") or "").strip()
            if summary:
                return f"Previous conversation summary: {summary}\n\nRecent messages:\n{recent}" if recent else f"Previous conversation summary: {summary}"
        return recent

    def _summarize_with_ai(self, prompt: str) -> str:
        if self.ai_service is None:
            return ""
        # Use the public summarize_text() API which goes through circuit breakers
        # and is provider-agnostic, instead of calling private _chat_* methods directly.
        summarize_fn = getattr(self.ai_service, "summarize_text", None)
        if callable(summarize_fn):
            try:
                return str(summarize_fn(prompt, max_tokens=150) or "").strip()
            except Exception as exc:
                logger.warning("conversation summarizer AI call failed: %s", exc)
                return ""
        # Fallback: minimal shim for legacy AIService instances without summarize_text
        try:
            ensure_keys = getattr(self.ai_service, "_ensure_api_keys", None)
            if callable(ensure_keys):
                ensure_keys()
        except Exception as exc:
            logger.warning("conversation summarizer key load failed: %s", exc)
        try:
            gemini_key = getattr(self.ai_service, "gemini_key", None)
            claude_key = getattr(self.ai_service, "claude_key", None)
            if gemini_key:
                result = self.ai_service._chat_gemini(prompt, max_tokens=150)  # noqa: SLF001
                return str(result or "").strip()
            if claude_key:
                result = self.ai_service._chat_claude(prompt, max_tokens=150)  # noqa: SLF001
                return str(result or "").strip()
        except Exception as exc:
            logger.warning("conversation summarizer AI call failed: %s", exc)
        return ""

    def _join_turns(self, history: list[dict[str, Any]]) -> str:
        lines = []
        for turn in history or []:
            role = "User" if str((turn or {}).get("role") or "").strip().lower() == "user" else "Assistant"
            content = str((turn or {}).get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _format_recent_turns(self, turns: list[dict[str, Any]]) -> str:
        return self._join_turns(turns)

    def _fallback_summary(self, history: list[dict[str, Any]]) -> str:
        keywords = (
            "today",
            "tomorrow",
            "tonight",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "am",
            "pm",
            "hour",
            "minute",
            "gfe",
            "pse",
            "incall",
            "outcall",
            "address",
            "hotel",
            "issue",
            "problem",
            "change",
            "confirm",
        )
        snippets: list[str] = []
        for turn in history or []:
            content = str((turn or {}).get("content") or "").strip()
            if not content:
                continue
            lowered = content.lower()
            if any(keyword in lowered for keyword in keywords):
                snippets.append(content)
            if len(snippets) >= 4:
                break
        if not snippets:
            snippets = [
                str((turn or {}).get("content") or "").strip()
                for turn in (history or [])[:4]
                if str((turn or {}).get("content") or "").strip()
            ]
        if not snippets:
            return ""
        return self._truncate_words(" | ".join(snippets), 100)

    def _truncate_words(self, text: str, max_words: int) -> str:
        words = [word for word in str(text or "").split() if word]
        if len(words) <= max_words:
            return " ".join(words)
        return " ".join(words[:max_words]).strip()
