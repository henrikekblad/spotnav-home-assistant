"""One read path for ownership (the core end state plan, step 2).

The controller keeps the owner, load balancing's memory, the safety hold, C7's stops, the car-ended record, the
window hold's memory and the known connection in fields of its own, but reads and writes them only through a small
set of accessors. A later step moves a group to the charge session by changing that group's accessor alone, so a
read or write that goes around it would keep a stale field. This test reads the controller's source and fails on
any such read or write, and on any other module reaching into those fields.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "spotnav"
CONTROLLER = PACKAGE / "execution" / "controller.py"

#: Each field behind an accessor -> the accessor (a property whose getter alone reads it, whose setter alone
#: writes it).
FIELDS: dict[str, str] = {
    "_charge_origin": "_owner_origin",
    "_plan_charge": "_is_plan_charge",
    "_paused_by_balancing": "_balancing",
    "_paused_charge": "_balancing",
    "_held_for_safety": "_safety",
    "_safety_stopped_at": "_safety",
    "_person_hold_stop_pending": "_c7",
    "_person_hold_stop_times": "_c7",
    "_person_hold_tried_at": "_c7",
    "_person_hold_gave_up": "_c7",
    "_car_ended_at": "_car_ended",
    "_car_ended_soc": "_car_ended",
    "_car_ended_vehicle": "_car_ended",
    "_car_ended_wait": "_car_ended",
    "_known_connected": "_plugged",
}
#: The window hold's memory of ownership, read and written only through `_hold_state`.
HOLD_FIELDS = frozenset({"held", "overridden", "owned"})
HOLD_ACCESSOR = "_hold_state"
ACCESSORS = frozenset(FIELDS.values()) | {HOLD_ACCESSOR}


def _is_self(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _role(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """`getter` or `setter` for an accessor's own two functions, else `None`."""
    if function.name not in ACCESSORS:
        return None
    for decorator in function.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "property":
            return "getter"
        if isinstance(decorator, ast.Attribute) and decorator.attr == "setter":
            return "setter"
    return None


def _stores(target: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether the attribute is written: assigned, deleted, or changed in place (`.append(...)`, `x[i] = ...`)."""
    if isinstance(target.ctx, (ast.Store, ast.Del)):
        return True
    parent = parents.get(target)
    if isinstance(parent, ast.Subscript) and isinstance(parent.ctx, (ast.Store, ast.Del)):
        return True
    if isinstance(parent, ast.Attribute) and isinstance(parents.get(parent), ast.Call):
        return parent.attr in {"append", "remove", "extend", "clear", "pop", "insert", "update", "setdefault"}
    return False


def violations(source: str) -> list[str]:
    """Every read or write of a field behind an accessor outside that accessor, as `line: what`."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[str] = []

    def enclosing(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        function = enclosing(node)
        role = None if function is None else _role(function)
        if _is_self(node.value) and node.attr in FIELDS:
            write = _stores(node, parents)
            if function is not None and function.name == "__init__" and isinstance(node.ctx, ast.Store):
                continue  # its declaration
            if function is not None and function.name == FIELDS[node.attr] and role == ("setter" if write else "getter"):
                continue
            found.append(f"{node.lineno}: {'write' if write else 'read'} of self.{node.attr} outside {FIELDS[node.attr]}")
        elif isinstance(node.value, ast.Attribute) and _is_self(node.value.value) and node.value.attr == "_hold":
            if node.attr not in HOLD_FIELDS:
                continue  # the hold's own observation (`observe`, `held_now`, ...), not its memory of ownership
            write = _stores(node, parents)
            if function is not None and function.name == HOLD_ACCESSOR and role == ("setter" if write else "getter"):
                continue
            found.append(f"{node.lineno}: {'write' if write else 'read'} of self._hold.{node.attr} outside {HOLD_ACCESSOR}")
        elif _is_self(node.value) and node.attr == "_hold":
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute):
                continue  # judged above, as `self._hold.<name>`
            if function is not None and function.name == "__init__" and isinstance(node.ctx, ast.Store):
                continue
            if function is not None and function.name == HOLD_ACCESSOR and role is not None:
                continue
            found.append(f"{node.lineno}: self._hold taken whole outside {HOLD_ACCESSOR}")
    return found


def test_the_controller_reads_and_writes_ownership_only_through_its_accessors() -> None:
    assert violations(CONTROLLER.read_text(encoding="utf-8")) == []


def test_every_accessor_is_a_property_with_a_setter() -> None:
    tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))
    roles: dict[str, set[str]] = {name: set() for name in ACCESSORS}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (role := _role(node)) is not None:
            roles[node.name].add(role)
    assert roles == {name: {"getter", "setter"} for name in ACCESSORS}


def test_no_other_module_reaches_into_the_fields() -> None:
    """Outside the controller, ownership is read through its public properties (`charge_origin`,
    `paused_by_balancing`, `paused_charge_origin`, ...), never through these fields."""
    names = set(FIELDS) | ACCESSORS | {"_hold"}
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == CONTROLLER:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in names and not _is_self(node.value):
                found.append(f"{path.relative_to(PACKAGE)}:{node.lineno}: .{node.attr}")
    assert found == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("self._charge_origin = 'manual'", "write of self._charge_origin outside _owner_origin"),
        ("return self._plan_charge", "read of self._plan_charge outside _is_plan_charge"),
        ("self._person_hold_stop_times.append(now)", "write of self._person_hold_stop_times outside _c7"),
        ("self._car_ended_at, self._car_ended_soc = None, None", "write of self._car_ended_at outside _car_ended"),
        ("self._hold.owned = True", "write of self._hold.owned outside _hold_state"),
        ("hold = self._hold", "self._hold taken whole outside _hold_state"),
    ],
)
def test_the_check_finds_a_read_or_write_around_an_accessor(body: str, expected: str) -> None:
    source = f"class C:\n    def elsewhere(self, now):\n        {body}\n"
    assert any(expected in item for item in violations(source))


def test_the_check_lets_the_accessors_and_the_hold_s_own_methods_be() -> None:
    source = (
        "class C:\n"
        "    def __init__(self):\n"
        "        self._charge_origin = None\n"
        "        self._hold = object()\n"
        "    @property\n"
        "    def _owner_origin(self):\n"
        "        return self._charge_origin\n"
        "    @_owner_origin.setter\n"
        "    def _owner_origin(self, origin):\n"
        "        self._charge_origin = origin\n"
        "    def elsewhere(self):\n"
        "        self._hold.spotnav_started()\n"
        "        return self._owner_origin\n"
    )
    assert violations(source) == []


def test_a_getter_does_not_write_and_a_setter_does_not_read() -> None:
    source = (
        "class C:\n"
        "    @property\n"
        "    def _owner_origin(self):\n"
        "        self._charge_origin = None\n"
        "    @_owner_origin.setter\n"
        "    def _owner_origin(self, origin):\n"
        "        return self._charge_origin\n"
    )
    assert len(violations(source)) == 2
