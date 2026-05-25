from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.security.rotation.state import resolve_cutover_state


def _clean_secret(value: str | None) -> str:
    return str(value or "").strip()


def _unique_secrets(values: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        candidate = _clean_secret(raw)
        if not candidate or candidate in seen:
            continue
        out.append(candidate)
        seen.add(candidate)
    return out


@dataclass(frozen=True)
class SecretRotationConfig:
    active_key: str
    next_key: str
    deprecated_key: str
    cutover_state: str
    dual_window_enabled: bool
    fallback_applied: bool


def resolve_secret_rotation_config(
    *,
    active_key: str | None = None,
    next_key: str | None = None,
    deprecated_key: str | None = None,
    cutover_state: str | None = None,
    fallback_secrets: Sequence[str] | None = None,
) -> SecretRotationConfig:
    fallback = _unique_secrets(fallback_secrets)
    active = _clean_secret(active_key)
    next_secret = _clean_secret(next_key)
    deprecated = _clean_secret(deprecated_key)
    fallback_applied = False

    if not active and fallback:
        active = fallback[0]
        fallback_applied = True
    if not next_secret and len(fallback) > 1:
        next_secret = fallback[1]
        fallback_applied = True
    if not active and next_secret:
        active, next_secret = next_secret, ""
        fallback_applied = True

    if next_secret and next_secret == active:
        next_secret = ""
    if deprecated in {active, next_secret}:
        deprecated = ""

    state_resolution = resolve_cutover_state(
        cutover_state,
        has_active=bool(active),
        has_next=bool(next_secret),
    )
    return SecretRotationConfig(
        active_key=active,
        next_key=next_secret,
        deprecated_key=deprecated,
        cutover_state=state_resolution.cutover_state,
        dual_window_enabled=state_resolution.dual_window_enabled,
        fallback_applied=fallback_applied,
    )
