from __future__ import annotations

import hmac
from dataclasses import dataclass

from app.security.rotation.config import SecretRotationConfig


@dataclass(frozen=True)
class VersionedSecret:
    version: str
    value: str


@dataclass(frozen=True)
class SecretValidationWindow:
    accepted: tuple[VersionedSecret, ...]
    deprecated: tuple[VersionedSecret, ...]
    cutover_state: str


@dataclass(frozen=True)
class SecretMatchResult:
    matched: bool
    version: str
    cutover_state: str
    deprecated_match: bool = False


def build_secret_validation_window(config: SecretRotationConfig) -> SecretValidationWindow:
    accepted: list[VersionedSecret] = []
    if config.active_key:
        accepted.append(VersionedSecret("active", config.active_key))
    if config.dual_window_enabled and config.next_key:
        accepted.append(VersionedSecret("next", config.next_key))

    deprecated: list[VersionedSecret] = []
    if config.deprecated_key:
        deprecated.append(VersionedSecret("deprecated", config.deprecated_key))
    return SecretValidationWindow(tuple(accepted), tuple(deprecated), config.cutover_state)


def match_secret(candidate: str, window: SecretValidationWindow) -> SecretMatchResult:
    incoming = str(candidate or "").strip()
    if not incoming:
        return SecretMatchResult(False, "none", window.cutover_state, False)

    for option in window.accepted:
        if hmac.compare_digest(incoming, option.value):
            return SecretMatchResult(True, option.version, window.cutover_state, False)
    for option in window.deprecated:
        if hmac.compare_digest(incoming, option.value):
            return SecretMatchResult(False, option.version, window.cutover_state, True)
    if not window.accepted:
        return SecretMatchResult(False, "unconfigured", window.cutover_state, False)
    return SecretMatchResult(False, "none", window.cutover_state, False)
