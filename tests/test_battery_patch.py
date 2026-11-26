"""The guard on `tests/battery_patch.py`: every global of the battery control's shell that a test replaces is replaced
in exactly the modules whose code reads it, and no test goes around the helper."""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests import battery_patch

_SENTINEL = object()


@pytest.mark.parametrize("name", sorted(battery_patch.READ_IN))
def test_a_listed_global_is_patched_in_every_shell_module_whose_code_reads_it_and_nowhere_else(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = battery_patch.listed(name)
    assert listed, name
    assert {module.__name__ for module in listed} == {module.__name__ for module in battery_patch.readers(name)}
    battery_patch.patch(monkeypatch, name, _SENTINEL)
    for module in listed:
        # What the module's own functions and methods read from now on is the test's value.
        functions = _functions(module)
        assert functions and all(function.__globals__[name] is _SENTINEL for function in functions), module


def _functions(module: ModuleType) -> list[Any]:
    """The functions defined in the module, its classes' methods among them."""
    found: list[Any] = []
    for value in vars(module).values():
        if getattr(value, "__module__", None) != module.__name__:
            continue
        members = vars(value).values() if isinstance(value, type) else (value,)
        for member in members:
            member = getattr(member, "__func__", member)
            member = getattr(member, "fget", member)
            # Written in the module (a dataclass's generated methods are not).
            if getattr(member, "__globals__", None) is vars(module):
                found.append(member)
    return found


def test_a_name_no_shell_code_reads_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LookupError):
        battery_patch.patch(monkeypatch, "NO_SUCH_GLOBAL", 1)
    # Defined in the module, but only read at import (a constant no function reads): a patch would change nothing.
    module = battery_patch.family()[0]
    short = module.__name__.split(".battery.", 1)[1]
    unread = next(
        name for name in vars(module)
        if name.isupper() and not any(name in battery_patch.globals_read(member) for member in battery_patch.family())
    )
    monkeypatch.setitem(battery_patch.READ_IN, unread, (short,))
    with pytest.raises(LookupError):
        battery_patch.patch(monkeypatch, unread, 1)
    # Listed for a module that does not define it.
    monkeypatch.setitem(battery_patch.READ_IN, "NO_SUCH_GLOBAL", (short,))
    with pytest.raises(LookupError):
        battery_patch.patch(monkeypatch, "NO_SUCH_GLOBAL", 1)


def test_no_test_replaces_a_shell_global_without_the_helper() -> None:
    names = {module.__name__ for module in battery_patch.family()}
    aliases = re.compile(
        r"^\s*(?:from (custom_components\.spotnav\.battery(?:\.control)?) import (\([^)]*\)|.+)"
        r"|import (custom_components\.spotnav\.battery\.control\S*) as (\w+))$",
        re.MULTILINE,
    )
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.name == "battery_patch.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "spotnav.battery.control" not in "".join(re.findall(r"setattr\(\s*\"[^\"]*\"", text)), path.name
        modules: set[str] = set()
        for package, imported, dotted, alias in aliases.findall(text):
            if dotted:
                modules.add(alias)
                continue
            for part in imported.strip("()").split(","):
                source, _, alias = part.split("#")[0].strip().partition(" as ")
                if f"{package}.{source}" in names:
                    modules.add(alias or source)
        for module in modules:
            hits = re.findall(rf"setattr\(\s*{re.escape(module)}\s*,\s*\"(\w+)\"", text)
            assert hits == [], f"{path.name}: {hits} replaced on the module, not through tests/battery_patch.py"
