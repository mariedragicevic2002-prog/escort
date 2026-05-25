from __future__ import annotations

from typing import Any, Mapping

from app.config_governance.contracts import (
    ConfigFieldContract,
    ConfigGovernanceReport,
    ConfigIssue,
    ConfigRegistryContract,
    ConfigResolution,
    ConfigValidationError,
)
from app.config_governance.drift import detect_config_drift
from app.config_governance.registry import TypedConfigRegistry
from app.config_governance.validation import validate_value

_MISSING = object()


def _fallback_value(contract: ConfigFieldContract[Any], raw_value: Any) -> Any:
    if contract.fallback_resolver is None:
        return contract.default
    try:
        return contract.fallback_resolver(raw_value, contract.default)
    except Exception:
        return contract.default


def resolve_contract(
    *,
    contract: ConfigRegistryContract,
    raw_values: Mapping[str, Any],
    strict: bool = False,
) -> ConfigResolution:
    resolved: dict[str, Any] = {}
    expected_for_drift: dict[str, Any] = {}
    issues: list[ConfigIssue] = []
    defaults_applied: list[str] = []

    for field_name, field_contract in contract.fields.items():
        raw_value = raw_values.get(field_name, _MISSING)
        if raw_value is _MISSING or raw_value is None:
            resolved[field_name] = field_contract.default
            defaults_applied.append(field_name)
            continue

        parsed_value: Any = None
        parse_failed = False
        try:
            parsed_value = field_contract.parser(raw_value)
        except Exception as exc:
            parse_failed = True
            fallback_value = _fallback_value(field_contract, raw_value)
            resolved[field_name] = fallback_value
            defaults_applied.append(field_name)
            issues.append(
                ConfigIssue(
                    namespace=contract.namespace,
                    field=field_name,
                    code="parse_error",
                    message=f"parse failed ({type(exc).__name__})",
                    raw_value=raw_value,
                    fallback_value=fallback_value,
                )
            )
            expected_for_drift[field_name] = raw_value

        if parse_failed:
            continue

        validation_errors = validate_value(field_contract, parsed_value)
        if validation_errors:
            fallback_value = _fallback_value(field_contract, raw_value)
            resolved[field_name] = fallback_value
            defaults_applied.append(field_name)
            issues.append(
                ConfigIssue(
                    namespace=contract.namespace,
                    field=field_name,
                    code="constraint_violation",
                    message="; ".join(validation_errors),
                    raw_value=raw_value,
                    fallback_value=fallback_value,
                )
            )
            expected_for_drift[field_name] = parsed_value
            continue

        resolved[field_name] = parsed_value
        expected_for_drift[field_name] = parsed_value

    drift = detect_config_drift(
        namespace=contract.namespace,
        expected=expected_for_drift,
        runtime={key: resolved.get(key) for key in expected_for_drift},
    )
    report = ConfigGovernanceReport(
        namespace=contract.namespace,
        issues=tuple(issues),
        defaults_applied=tuple(dict.fromkeys(defaults_applied)),
        drift=drift,
    )
    if strict and report.has_issues:
        raise ConfigValidationError(namespace=contract.namespace, issues=report.issues)
    return ConfigResolution(values=resolved, report=report)


def resolve_registered_contract(
    *,
    registry: TypedConfigRegistry,
    namespace: str,
    raw_values: Mapping[str, Any],
    strict: bool = False,
) -> ConfigResolution:
    return resolve_contract(
        contract=registry.get(namespace),
        raw_values=raw_values,
        strict=strict,
    )
