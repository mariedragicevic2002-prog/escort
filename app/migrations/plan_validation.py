from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any


_DESTRUCTIVE_SQL_RE = re.compile(
    r"(?i)\b(drop\s+table|drop\s+column|truncate\s+table|delete\s+from)\b"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_operation_id(value: Any, *, index: int) -> str:
    candidate = str(value or "").strip()
    return candidate or f"migration-step-{index:03d}"


def normalize_sql(sql: Any) -> str:
    return " ".join(str(sql or "").split())


def _canonical_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_json(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def is_destructive_sql(sql: Any) -> bool:
    return bool(_DESTRUCTIVE_SQL_RE.search(normalize_sql(sql)))


@dataclass(frozen=True)
class MigrationPlanStep:
    operation_id: str
    forward_sql: str
    description: str = ""
    reversible: bool = False
    rollback_sql: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_contract_dict(self) -> dict[str, Any]:
        normalized_forward_sql = normalize_sql(self.forward_sql)
        normalized_rollback_sql = normalize_sql(self.rollback_sql) if self.rollback_sql else None
        return {
            "operation_id": str(self.operation_id or "").strip(),
            "description": str(self.description or ""),
            "forward_sql": normalized_forward_sql,
            "reversible": bool(self.reversible),
            "rollback_sql": normalized_rollback_sql,
            "destructive": is_destructive_sql(normalized_forward_sql),
            "metadata": _canonical_json(dict(self.metadata or {})),
        }


def build_plan_hash(steps: Iterable[MigrationPlanStep]) -> str:
    payload = json.dumps(
        [step.to_contract_dict() for step in steps],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationPlanValidationReport:
    contract_version: str
    generated_at: str
    dry_run: bool
    valid: bool
    plan_hash: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": str(self.contract_version or "migration-plan.v1"),
            "generated_at": str(self.generated_at or ""),
            "dry_run": bool(self.dry_run),
            "valid": bool(self.valid),
            "plan_hash": str(self.plan_hash or ""),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "steps": list(self.steps),
        }


def validate_migration_plan(
    steps: Iterable[MigrationPlanStep],
    *,
    allow_destructive: bool = False,
    dry_run: bool = True,
    now_provider: Callable[[], datetime] | None = None,
) -> MigrationPlanValidationReport:
    normalized_steps: list[MigrationPlanStep] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen_operation_ids: set[str] = set()

    for index, raw_step in enumerate(list(steps), start=1):
        operation_id = _normalize_operation_id(raw_step.operation_id, index=index)
        normalized_step = MigrationPlanStep(
            operation_id=operation_id,
            forward_sql=normalize_sql(raw_step.forward_sql),
            description=str(raw_step.description or "").strip(),
            reversible=bool(raw_step.reversible),
            rollback_sql=(normalize_sql(raw_step.rollback_sql) if raw_step.rollback_sql else None),
            metadata=_canonical_json(dict(raw_step.metadata or {})),
        )
        normalized_steps.append(normalized_step)

        if operation_id in seen_operation_ids:
            errors.append(f"duplicate operation_id: {operation_id}")
        seen_operation_ids.add(operation_id)

        if not normalized_step.forward_sql:
            errors.append(f"{operation_id}: forward_sql is required")

        if is_destructive_sql(normalized_step.forward_sql) and not allow_destructive:
            errors.append(f"{operation_id}: destructive SQL detected but allow_destructive is false")

        if normalized_step.reversible and not normalized_step.rollback_sql:
            errors.append(f"{operation_id}: reversible operation requires rollback_sql")
        if not normalized_step.reversible and normalized_step.rollback_sql:
            warnings.append(f"{operation_id}: rollback_sql provided for non-reversible operation")

    generated_at = (now_provider or _utc_now)().astimezone(UTC).isoformat()
    plan_hash = build_plan_hash(normalized_steps)
    step_payload = tuple(step.to_contract_dict() for step in normalized_steps)

    return MigrationPlanValidationReport(
        contract_version="migration-plan.v1",
        generated_at=generated_at,
        dry_run=bool(dry_run),
        valid=not errors,
        plan_hash=plan_hash,
        errors=tuple(errors),
        warnings=tuple(warnings),
        steps=step_payload,
    )
