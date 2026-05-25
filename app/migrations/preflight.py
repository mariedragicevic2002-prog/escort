from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip().strip('"').lower()


def _normalize_identifiers(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        candidate = _normalize_identifier(value)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    normalized.sort()
    return tuple(normalized)


class MigrationSchemaExecutor(Protocol):
    def execute_query(
        self,
        query: str,
        params: Sequence[Any] = (),
        *,
        fetch: bool | None = True,
        conn: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        ...


@dataclass(frozen=True)
class SchemaRequirement:
    table: str
    required_columns: tuple[str, ...] = ()
    required_indexes: tuple[str, ...] = ()

    def normalized(self) -> "SchemaRequirement":
        return SchemaRequirement(
            table=_normalize_identifier(self.table),
            required_columns=_normalize_identifiers(self.required_columns),
            required_indexes=_normalize_identifiers(self.required_indexes),
        )


@dataclass(frozen=True)
class PreflightIssue:
    check_type: str
    table: str
    identifier: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "check_type": str(self.check_type or "unknown"),
            "table": str(self.table or ""),
            "identifier": str(self.identifier or ""),
            "detail": str(self.detail or ""),
        }


@dataclass(frozen=True)
class PreflightReport:
    contract_version: str
    generated_at: str
    passed: bool
    requirements: tuple[SchemaRequirement, ...]
    missing_tables: tuple[str, ...]
    missing_columns: tuple[tuple[str, str], ...]
    missing_indexes: tuple[tuple[str, str], ...]
    issues: tuple[PreflightIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": str(self.contract_version or "migration-preflight.v1"),
            "generated_at": str(self.generated_at or ""),
            "passed": bool(self.passed),
            "requirements": [
                {
                    "table": requirement.table,
                    "required_columns": list(requirement.required_columns),
                    "required_indexes": list(requirement.required_indexes),
                }
                for requirement in self.requirements
            ],
            "missing_tables": list(self.missing_tables),
            "missing_columns": [
                {"table": table, "column": column}
                for table, column in self.missing_columns
            ],
            "missing_indexes": [
                {"table": table, "index": index_name}
                for table, index_name in self.missing_indexes
            ],
            "issues": [item.to_dict() for item in self.issues],
        }


_TABLE_EXISTS_SQL = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = current_schema()
  AND table_name = %s
LIMIT 1
"""

_COLUMN_EXISTS_SQL = """
SELECT column_name
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND table_name = %s
  AND column_name = %s
LIMIT 1
"""

_TABLE_INDEXES_SQL = """
SELECT indexname
FROM pg_indexes
WHERE schemaname = current_schema()
  AND tablename = %s
"""


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    if isinstance(row, Sequence) and row and not isinstance(row, (str, bytes, bytearray)):
        return row[0]
    return None


def _query_rows(
    executor: MigrationSchemaExecutor,
    query: str,
    params: Sequence[Any],
    *,
    conn: Any | None = None,
) -> list[Any]:
    rows = executor.execute_query(query, params, fetch=True, conn=conn)
    if rows is None:
        return []
    return list(rows)


def run_preflight_checks(
    executor: MigrationSchemaExecutor,
    requirements: Iterable[SchemaRequirement],
    *,
    conn: Any | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> PreflightReport:
    generated_at = (now_provider or _utc_now)().astimezone(UTC).isoformat()
    normalized_requirements = tuple(
        requirement.normalized()
        for requirement in requirements
        if _normalize_identifier(requirement.table)
    )
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []
    missing_indexes: list[tuple[str, str]] = []
    issues: list[PreflightIssue] = []

    for requirement in normalized_requirements:
        table_name = requirement.table
        try:
            table_rows = _query_rows(executor, _TABLE_EXISTS_SQL, (table_name,), conn=conn)
        except Exception as exc:
            issues.append(
                PreflightIssue(
                    check_type="table_check_error",
                    table=table_name,
                    identifier=table_name,
                    detail=f"failed to query table metadata: {exc}",
                )
            )
            continue
        if not table_rows:
            missing_tables.append(table_name)
            issues.append(
                PreflightIssue(
                    check_type="missing_table",
                    table=table_name,
                    identifier=table_name,
                    detail="required table is missing",
                )
            )
            continue

        for column_name in requirement.required_columns:
            try:
                column_rows = _query_rows(
                    executor,
                    _COLUMN_EXISTS_SQL,
                    (table_name, column_name),
                    conn=conn,
                )
            except Exception as exc:
                issues.append(
                    PreflightIssue(
                        check_type="column_check_error",
                        table=table_name,
                        identifier=column_name,
                        detail=f"failed to query column metadata: {exc}",
                    )
                )
                continue
            if column_rows:
                continue
            missing_columns.append((table_name, column_name))
            issues.append(
                PreflightIssue(
                    check_type="missing_column",
                    table=table_name,
                    identifier=column_name,
                    detail="required column is missing",
                )
            )

        try:
            index_rows = _query_rows(executor, _TABLE_INDEXES_SQL, (table_name,), conn=conn)
        except Exception as exc:
            issues.append(
                PreflightIssue(
                    check_type="index_check_error",
                    table=table_name,
                    identifier=table_name,
                    detail=f"failed to query index metadata: {exc}",
                )
            )
            index_rows = []
        available_indexes = {
            _normalize_identifier(_row_value(row, "indexname"))
            for row in index_rows
            if _normalize_identifier(_row_value(row, "indexname"))
        }
        for index_name in requirement.required_indexes:
            if index_name in available_indexes:
                continue
            missing_indexes.append((table_name, index_name))
            issues.append(
                PreflightIssue(
                    check_type="missing_index",
                    table=table_name,
                    identifier=index_name,
                    detail="required index is missing",
                )
            )

    dedup_missing_tables = tuple(sorted({item for item in missing_tables}))
    dedup_missing_columns = tuple(sorted({item for item in missing_columns}))
    dedup_missing_indexes = tuple(sorted({item for item in missing_indexes}))
    ordered_issues = tuple(
        sorted(
            issues,
            key=lambda item: (item.check_type, item.table, item.identifier),
        )
    )

    return PreflightReport(
        contract_version="migration-preflight.v1",
        generated_at=generated_at,
        passed=not dedup_missing_tables and not dedup_missing_columns and not dedup_missing_indexes,
        requirements=normalized_requirements,
        missing_tables=dedup_missing_tables,
        missing_columns=dedup_missing_columns,
        missing_indexes=dedup_missing_indexes,
        issues=ordered_issues,
    )
