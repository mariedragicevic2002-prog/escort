"""
Embedding service — Gemini text-embedding-004.

Feature-flagged via EMBEDDINGS_ENABLED=true.  When disabled, all methods
return ``None`` / empty so callers can fall back to text search gracefully.

Usage:
    svc = EmbeddingService()
    if svc.enabled:
        vec = svc.embed("I'd like to book a GFE for tomorrow")
        # vec is list[float] of length 768, or None on error
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("adella_chatbot.embedding_service")

_EMBED_MODEL = "text-embedding-004"
_EMBED_DIM = 768


def _is_enabled() -> bool:
    return os.environ.get("EMBEDDINGS_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _get_gemini_key() -> str:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    # Lazy fallback: try reading from admin DB config (same pattern as AIService)
    try:
        import config as _cfg
        return (getattr(_cfg, "GEMINI_API_KEY", None) or "").strip()
    except Exception:
        return ""


class EmbeddingService:
    """Generates text embeddings via Gemini text-embedding-004.

    All public methods are safe to call even when embeddings are disabled or the
    API key is missing — they simply return ``None``.
    """

    def __init__(self, api_key: str | None = None):
        self._enabled = _is_enabled()
        self._api_key: str = (api_key or "").strip()
        self._client: Any = None  # lazy google.genai.Client

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def dim(self) -> int:
        return _EMBED_DIM

    def embed(self, text: str) -> list[float] | None:
        """Return a 768-dimensional embedding vector, or ``None`` if unavailable."""
        if not self._enabled:
            return None
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        try:
            client = self._get_client()
            if client is None:
                return None
            from google.genai import types as _gtypes

            result = client.models.embed_content(
                model=_EMBED_MODEL,
                contents=cleaned,
                config=_gtypes.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
            )
            values = getattr(result, "embeddings", None)
            if values and hasattr(values[0], "values"):
                return list(values[0].values)
            logger.warning("embed_content returned unexpected shape: %r", result)
            return None
        except Exception as exc:
            logger.warning("embedding generation failed: %s", exc)
            return None

    def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Embed multiple texts.  Falls back to per-item calls for safety."""
        return [self.embed(t) for t in texts]

    def _get_client(self):
        if self._client is not None:
            return self._client
        key = self._api_key or _get_gemini_key()
        if not key:
            logger.warning("EmbeddingService: no Gemini API key found; embeddings disabled")
            return None
        try:
            from google import genai

            self._client = genai.Client(api_key=key)
            return self._client
        except ImportError:
            logger.warning("google-genai package not installed; embeddings unavailable")
            return None
        except Exception as exc:
            logger.warning("EmbeddingService client init failed: %s", exc)
            return None
