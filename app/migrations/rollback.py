from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from typing import Any

from app.migrations.plan_validation import MigrationPlanStep, build_plan_hash, normalize_sql


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(key): inner for key, inner in sorted(dict(value or {}).items(), key=lambda item: str(item[0]))}


@dataclass(frozen=True)
class RollbackMetadataEntry:
    operation_id: str
    rollback_sql: str
    forward_sql_hash: str
    description: str
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": str(self.operation_id or ""),
            "rollback_sql": str(self.rollback_sql or ""),
            "forward_sql_hash": str(self.forward_sql_hash or ""),
            "description": str(self.description or ""),
            "metadata": _canonical_metadata(self.metadata),
        }


@dataclass(frozen=True)
class RollbackMetadataBundle:
    contract_version: str
    generated_at: str
    dry_run: bool
    plan_hash: str
    entries: tuple[RollbackMetadataEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": str(self.contract_version or "migration-rollback.v1"),
            "generated_at": str(self.generated_at or ""),
            "dry_run": bool(self.dry_run),
            "plan_hash": str(self.plan_hash or ""),
            "entry_count": len(self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _forward_sql_hash(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode("utf-8")).hexdigest()


def capture_rollback_metadata(
    steps: Iterable[MigrationPlanStep],
    *,
    dry_run: bool,
    plan_hash: str | None = None,
    actor: str | None = None,
    run_id: str | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> RollbackMetadataBundle:
    normalized_steps = list(steps)
    generated_at = (now_provider or _utc_now)().astimezone(UTC).isoformat()
    resolved_plan_hash = str(plan_hash or build_plan_hash(normalized_steps))
    entries: list[RollbackMetadataEntry] = []

    for step in normalized_steps:
        if not step.reversible:
            continue
        rollback_sql = normalize_sql(step.rollback_sql)
        if not rollback_sql:
            continue
        metadata = _canonical_metadata(step.metadata)
        if actor:
            metadata.setdefault("captured_by", str(actor))
        if run_id:
            metadata.setdefault("run_id", str(run_id))
        entries.append(
            RollbackMetadataEntry(
                operation_id=str(step.operation_id or "").strip(),
                rollback_sql=rollback_sql,
                forward_sql_hash=_forward_sql_hash(step.forward_sql),
                description=str(step.description or ""),
                metadata=metadata,
            )
        )

    entries.sort(key=lambda item: item.operation_id)
    return RollbackMetadataBundle(
        contract_version="migration-rollback.v1",
        generated_at=generated_at,
        dry_run=bool(dry_run),
        plan_hash=resolved_plan_hash,
        entries=tuple(entries),
    )
