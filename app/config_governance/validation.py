from __future__ import annotations

from typing import Any, Iterable

from app.config_governance.contracts import ConfigFieldContract


def allowed_values(values: Iterable[Any]):
    allowed = tuple(values)

    def _validator(value: Any) -> str | None:
        if value not in allowed:
            return f"must be one of {allowed}"
        return None

    return _validator


def numeric_bounds(*, minimum: float | None = None, maximum: float | None = None):
    def _validator(value: Any) -> str | None:
        try:
            parsed = float(value)
        except Exception:
            return "must be numeric"
        if minimum is not None and parsed < minimum:
            return f"must be >= {minimum:g}"
        if maximum is not None and parsed > maximum:
            return f"must be <= {maximum:g}"
        return None

    return _validator


def validate_value(contract: ConfigFieldContract[Any], value: Any) -> tuple[str, ...]:
    errors: list[str] = []
    for validator in contract.validators:
        message = validator(value)
        if message:
            errors.append(str(message))
    return tuple(errors)
