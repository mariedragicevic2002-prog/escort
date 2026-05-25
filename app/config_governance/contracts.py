from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, TypeVar

T = TypeVar("T")
ConfigParser = Callable[[Any], T]
ConfigValidator = Callable[[T], str | None]
ConfigFallbackResolver = Callable[[Any, T], T]


@dataclass(frozen=True)
class ConfigFieldContract(Generic[T]):
    name: str
    default: T
    parser: ConfigParser[T]
    validators: tuple[ConfigValidator[T], ...] = ()
    fallback_resolver: ConfigFallbackResolver[T] | None = None


@dataclass(frozen=True)
class ConfigRegistryContract:
    namespace: str
    fields: Mapping[str, ConfigFieldContract[Any]]


@dataclass(frozen=True)
class ConfigIssue:
    namespace: str
    field: str
    code: str
    message: str
    raw_value: Any = None
    fallback_value: Any = None


@dataclass(frozen=True)
class ConfigDriftEntry:
    field: str
    expected: Any
    runtime: Any
    reason: str = "value_mismatch"


@dataclass(frozen=True)
class ConfigDriftSignal:
    namespace: str
    entries: tuple[ConfigDriftEntry, ...] = ()

    @property
    def drifted(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class ConfigGovernanceReport:
    namespace: str
    issues: tuple[ConfigIssue, ...] = ()
    defaults_applied: tuple[str, ...] = ()
    drift: ConfigDriftSignal | None = None

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


@dataclass(frozen=True)
class ConfigResolution:
    values: Mapping[str, Any]
    report: ConfigGovernanceReport


class ConfigValidationError(ValueError):
    def __init__(self, *, namespace: str, issues: tuple[ConfigIssue, ...]) -> None:
        self.namespace = namespace
        self.issues = issues
        summary = "; ".join(f"{issue.field}: {issue.message}" for issue in issues)
        super().__init__(f"{namespace} configuration invalid: {summary}")
