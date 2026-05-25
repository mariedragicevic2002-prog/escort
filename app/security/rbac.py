from __future__ import annotations

from collections.abc import Iterable


class PermissionDeniedError(PermissionError):
    def __init__(self, required_permission: str, actor: str | None = None) -> None:
        who = f" for {actor}" if actor else ""
        super().__init__(f"Missing required permission '{required_permission}'{who}")
        self.required_permission = required_permission
        self.actor = actor


def _normalize_permissions(granted_permissions: Iterable[str] | None) -> set[str]:
    if not granted_permissions:
        return set()
    return {str(permission).strip() for permission in granted_permissions if str(permission).strip()}


def has_permission(granted_permissions: Iterable[str] | None, required_permission: str) -> bool:
    required = str(required_permission or "").strip()
    if not required:
        return True

    granted = _normalize_permissions(granted_permissions)
    if "*" in granted or required in granted:
        return True

    if ":" in required:
        namespace = required.split(":", 1)[0]
        if f"{namespace}:*" in granted:
            return True

    return False


def require_permission(
    granted_permissions: Iterable[str] | None,
    required_permission: str,
    *,
    actor: str | None = None,
) -> None:
    if has_permission(granted_permissions, required_permission):
        return
    raise PermissionDeniedError(required_permission=required_permission, actor=actor)
