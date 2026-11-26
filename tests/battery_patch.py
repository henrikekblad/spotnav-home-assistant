"""The one way a test replaces a module global of the battery control's shell (`battery/control`): a timing constant
(`HAND_BACK_DEADLINE_S`), a recipe lookup (`control_for`), the store's class.

A function reads a global from the module it is defined in. Once the shell is more than one module, a
`monkeypatch.setattr(control_module, name, ...)` on the module the code was moved out of would change nothing and the
test would go on passing or failing for another reason. So every such patch goes through `patch`: it sets the name in
each module whose code reads it (`READ_IN`), and refuses a name that is not listed, not defined there, or not read by
that module's code. `test_battery_patch.py` proves the list is exactly where the shell's code reads each name. A test
that needs such a global's value reads it with `read`.

A move of code between the shell's modules changes `READ_IN` in the same commit."""

from __future__ import annotations

import dis
import importlib
import pkgutil
from functools import cache
from types import CodeType, ModuleType
from typing import Any, Final

import pytest

from custom_components.spotnav.battery import control

#: Each patched name -> the shell modules whose code reads it as a global, by name under `battery`.
READ_IN: Final[dict[str, tuple[str, ...]]] = {
    "CALL_TIMEOUT_S": ("control.io",),
    "GATED_AFTER_RUNNING": ("control.core",),
    "HAND_BACK_DEADLINE_S": ("control.core", "control.life", "control.stored",),
    "HAND_BACK_LOOK_S": ("control.core", "control.stored",),
    "IMPORT_LIMIT_READ_BACK_S": ("control.core",),
    "IMPORT_LIMIT_READ_BACK_STEP_S": ("control.core",),
    "START_RETRY_S": ("control.life",),
    "STARTUP_LOOK_S": ("control.life",),
    "STARTUP_WAIT_S": ("control.life",),
    "Store": ("control.core", "control.life",),
    "blocked": ("control.core",),
    "control_for": ("control.core", "control.life", "control.stored",),
    "export_control_for": ("control.life", "control.stored",),
    "learning_gate_open": ("control.core",),
    "plant_entity_id": ("control.core",),
    "read_export_facts": ("control.core", "control.life", "control.stored",),
    "read_plant_entity": ("control.core",),
    "recipe_field_tested": ("control.core",),
}


def module(name: str) -> ModuleType:
    """A shell module by its name under `battery` (`control`, `control.core`)."""
    return importlib.import_module(f"{control.__name__.rsplit('.', 1)[0]}.{name}")


def listed(name: str) -> tuple[ModuleType, ...]:
    """The modules `READ_IN` names for the global `name`."""
    return tuple(module(short) for short in READ_IN[name])


def family() -> tuple[ModuleType, ...]:
    """The shell's modules: `battery.control` itself and, once it is a package, every module in it."""
    path = getattr(control, "__path__", None)
    if path is None:
        return (control,)
    return (control, *(
        importlib.import_module(f"{control.__name__}.{info.name}") for info in pkgutil.iter_modules(path)
    ))


def _globals_read(code: CodeType) -> set[str]:
    names = {instruction.argval for instruction in dis.get_instructions(code) if instruction.opname == "LOAD_GLOBAL"}
    for const in code.co_consts:
        if isinstance(const, CodeType):
            names |= _globals_read(const)
    return names


@cache
def globals_read(module: ModuleType) -> frozenset[str]:
    """The globals the module's functions and methods read when they run (never what its own top level reads once
    at import: a patch cannot reach that)."""
    file = module.__file__
    assert file is not None
    with open(file, encoding="utf-8") as source:
        return frozenset(_globals_read(compile(source.read(), file, "exec")))


def readers(name: str) -> tuple[ModuleType, ...]:
    """The shell's modules whose code reads the global `name`."""
    return tuple(member for member in family() if name in globals_read(member))


def patch(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Replace the shell's global `name` with `value` where the shell's code reads it, for the test."""
    if name not in READ_IN:
        raise LookupError(f"{name}: not a listed global of the battery control's shell (tests/battery_patch.py)")
    for member in listed(name):
        if name not in vars(member):
            raise LookupError(f"{name}: not defined in {member.__name__}")
        if name not in globals_read(member):
            raise LookupError(f"{name}: no code in {member.__name__} reads it")
        monkeypatch.setattr(member, name, value)


def read(name: str) -> Any:
    """The shell's global `name` as its code reads it now (a test's replacement, if one was made): never read it from
    the package, which only names the constants and would show the value before the replacement."""
    values = [vars(member)[name] for member in listed(name)]
    assert all(value is values[0] for value in values), f"{name}: the shell's modules read different values"
    return values[0]
