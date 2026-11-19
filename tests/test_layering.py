"""The control code does not import the API or the config flows: those layers import it, never the other way round.

Every import in the domain packages is checked, a function's local one and a `TYPE_CHECKING` one included: a rule
the control code needs lives in the domain layer (or `const.py`), and the API and the flows import it from there.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "spotnav"
DOMAIN_PACKAGES = ("battery", "heat", "site", "planning", "execution", "core")
UPPER_LAYERS = ("api", "flows")


def _imported_modules(source: str, package: tuple[str, ...]) -> list[tuple[int, str]]:
    """Each import in a module of `package` as `(line, module path inside the integration)`, relative ones
    resolved."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                parts = (node.module or "").split(".")
                if parts[:2] != ["custom_components", "spotnav"]:
                    continue
                base = parts[2:]
            else:
                base = [*package[: len(package) - (node.level - 1)], *((node.module or "").split(".") if node.module else [])]
            if not base:
                found.extend((node.lineno, alias.name) for alias in node.names)
            else:
                found.append((node.lineno, ".".join(base)))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[:2] == ["custom_components", "spotnav"] and len(parts) > 2:
                    found.append((node.lineno, ".".join(parts[2:])))
    return found


def test_domain_packages_do_not_import_the_api_or_the_flows() -> None:
    offenders = [
        f"{path.relative_to(PACKAGE)}:{line} imports {module}"
        for name in DOMAIN_PACKAGES
        for path in sorted((PACKAGE / name).rglob("*.py"))
        for line, module in _imported_modules(path.read_text(encoding="utf-8"), path.relative_to(PACKAGE).parent.parts)
        if module.split(".")[0] in UPPER_LAYERS
    ]
    assert not offenders, "\n".join(offenders)


def test_the_check_sees_a_local_import() -> None:
    """The walk finds a function's local import, the shape the old inversions had, at any depth."""
    source = (
        "def f():\n"
        "    from ..api.entity_fields import x\n"
        "    from .. import flows\n"
        "    from custom_components.spotnav.api import house\n"
    )
    assert [module for _, module in _imported_modules(source, ("site",))] == ["api.entity_fields", "flows", "api"]
    deeper = "def f():\n    from ...flows.measured_source import y\n"
    assert [module for _, module in _imported_modules(deeper, ("execution", "chargers"))] == ["flows.measured_source"]
