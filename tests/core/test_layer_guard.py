"""
Layer guard: enforces Clean Architecture dependency rules.

Fails loudly if any file violates the inward-only dependency rule.
Run with: pytest tests/core/test_layer_guard.py -v
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).parent.parent.parent

VIOLATIONS: dict[str, list[str]] = {
    "core": ["adapters", "infrastructure", "application"],
    "application": ["infrastructure", "adapters"],
}


def _get_imports(filepath: pathlib.Path) -> list[str]:
    source = filepath.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def _collect_violations() -> list[tuple[str, str, str]]:
    found = []
    for layer, forbidden in VIOLATIONS.items():
        layer_dir = ROOT / layer
        if not layer_dir.exists():
            continue
        for pyfile in layer_dir.rglob("*.py"):
            for imp in _get_imports(pyfile):
                for banned in forbidden:
                    if imp == banned or imp.startswith(banned + "."):
                        found.append((str(pyfile.relative_to(ROOT)), imp, layer))
    return found


def test_no_layer_violations() -> None:
    violations = _collect_violations()
    if violations:
        msg = "\n".join(
            f"  [{layer}] {filepath} imports '{imp}' (FORBIDDEN)"
            for filepath, imp, layer in violations
        )
        pytest.fail(f"Clean Architecture layer violations detected:\n{msg}")
