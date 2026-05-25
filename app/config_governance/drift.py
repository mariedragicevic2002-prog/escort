from __future__ import annotations

from typing import Any, Mapping

from app.config_governance.contracts import ConfigDriftEntry, ConfigDriftSignal


def detect_config_drift(
    *,
    namespace: str,
    expected: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> ConfigDriftSignal:
    entries: list[ConfigDriftEntry] = []
    keys = set(expected.keys()) | set(runtime.keys())
    for key in sorted(keys):
        has_expected = key in expected
        has_runtime = key in runtime
        if not has_expected:
            entries.append(
                ConfigDriftEntry(
                    field=str(key),
                    expected=None,
                    runtime=runtime.get(key),
                    reason="unexpected_runtime",
                )
            )
            continue
        if not has_runtime:
            entries.append(
                ConfigDriftEntry(
                    field=str(key),
                    expected=expected.get(key),
                    runtime=None,
                    reason="missing_runtime",
                )
            )
            continue
        if expected.get(key) != runtime.get(key):
            entries.append(
                ConfigDriftEntry(
                    field=str(key),
                    expected=expected.get(key),
                    runtime=runtime.get(key),
                    reason="value_mismatch",
                )
            )
    return ConfigDriftSignal(namespace=namespace, entries=tuple(entries))
