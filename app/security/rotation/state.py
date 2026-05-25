from __future__ import annotations

from dataclasses import dataclass

CUTOVER_STABLE = "stable"
CUTOVER_DUAL_WINDOW = "dual_window"
CUTOVER_POST_CUTOVER = "post_cutover"
CUTOVER_UNCONFIGURED = "unconfigured"

_DUAL_WINDOW_ALIASES = {"dual", "dual_window", "pre_cutover", "overlap", "warmup", "rotate"}
_POST_CUTOVER_ALIASES = {"post_cutover", "cutover", "cutover_complete", "deprecated", "deprecate"}
_STABLE_ALIASES = {"stable", "active_only", "steady", "single"}


@dataclass(frozen=True)
class CutoverStateResolution:
    cutover_state: str
    dual_window_enabled: bool


def resolve_cutover_state(raw_state: str | None, *, has_active: bool, has_next: bool) -> CutoverStateResolution:
    if not has_active and not has_next:
        return CutoverStateResolution(CUTOVER_UNCONFIGURED, False)

    normalized = str(raw_state or "").strip().lower()
    if normalized in _DUAL_WINDOW_ALIASES:
        return CutoverStateResolution(CUTOVER_DUAL_WINDOW if has_next else CUTOVER_STABLE, bool(has_next))
    if normalized in _POST_CUTOVER_ALIASES:
        return CutoverStateResolution(CUTOVER_POST_CUTOVER, False)
    if normalized in _STABLE_ALIASES:
        return CutoverStateResolution(CUTOVER_STABLE, False)

    if has_next:
        return CutoverStateResolution(CUTOVER_DUAL_WINDOW, True)
    return CutoverStateResolution(CUTOVER_STABLE, False)
