"""One read path for ownership: the charge session.

The owner, load balancing's memory, the safety hold, C7's stops, the car-ended record, the hold's memory and the known
connection are the core's session's alone. The controller keeps no field of its own for them: it reads them through a
small set of read-only accessors (`_owner_origin`, `_balancing`, ...), and only the core's decisions change them. This
test reads the controller's source and fails on a field that would hold one of them again, on an accessor that could
be written, and on any other module reaching into the accessors.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "spotnav"
CONTROLLER = PACKAGE / "execution" / "controller.py"

#: The controller's fields that mirrored the session before it was the truth: none may come back.
GONE: frozenset[str] = frozenset(
    {
        "_charge_origin",
        "_plan_charge",
        "_paused_by_balancing",
        "_paused_charge",
        "_held_for_safety",
        "_safety_stopped_at",
        "_person_hold_stop_pending",
        "_person_hold_stop_times",
        "_person_hold_tried_at",
        "_person_hold_gave_up",
        "_car_ended_at",
        "_car_ended_soc",
        "_car_ended_vehicle",
        "_car_ended_wait",
        "_known_connected",
        "_core_drives",
    }
)
#: The read-only accessors, each a view of the session.
ACCESSORS: frozenset[str] = frozenset(
    {"_owner_origin", "_is_plan_charge", "_balancing", "_safety", "_c7", "_car_ended", "_hold_state", "_plugged"}
)
#: What the hold (`window_hold.WindowHold`) no longer remembers: the hold's memory is the session's.
HOLD_MEMORY = frozenset({"held", "overridden", "owned"})


def _is_self(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def violations(source: str) -> list[str]:
    """Every use of a field that mirrored the session, every write through an accessor, and every use of the hold's
    old memory, as `line: what`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        if _is_self(node.value) and node.attr in GONE:
            found.append(f"{node.lineno}: self.{node.attr} mirrors the session")
        elif _is_self(node.value) and node.attr in ACCESSORS and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append(f"{node.lineno}: write of self.{node.attr}, a view of the session")
        elif (
            isinstance(node.value, ast.Attribute)
            and _is_self(node.value.value)
            and node.value.attr == "_hold"
            and node.attr in HOLD_MEMORY
        ):
            found.append(f"{node.lineno}: self._hold.{node.attr} is the session's")
    return found


def test_the_controller_keeps_no_ownership_of_its_own() -> None:
    assert violations(CONTROLLER.read_text(encoding="utf-8")) == []


def test_every_accessor_is_a_read_only_property() -> None:
    tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))
    roles: dict[str, list[str]] = {name: [] for name in ACCESSORS}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ACCESSORS:
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Name) and decorator.id == "property":
                    roles[node.name].append("getter")
                elif isinstance(decorator, ast.Attribute) and decorator.attr in ("setter", "deleter"):
                    roles[node.name].append(decorator.attr)
    assert roles == {name: ["getter"] for name in ACCESSORS}


def test_no_other_module_reaches_into_the_accessors() -> None:
    """Outside the controller, ownership is read through its public properties (`charge_origin`,
    `paused_by_balancing`, `paused_charge_origin`, ...) or the session itself, never through these accessors."""
    names = GONE | ACCESSORS
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
        ("self._charge_origin = 'manual'", "self._charge_origin mirrors the session"),
        ("return self._plan_charge", "self._plan_charge mirrors the session"),
        ("self._person_hold_stop_times.append(now)", "self._person_hold_stop_times mirrors the session"),
        ("self._owner_origin = 'manual'", "write of self._owner_origin, a view of the session"),
        ("self._c7 = None", "write of self._c7, a view of the session"),
        ("self._hold.owned = True", "self._hold.owned is the session's"),
        ("return self._hold.held", "self._hold.held is the session's"),
    ],
)
def test_the_check_finds_ownership_kept_beside_the_session(body: str, expected: str) -> None:
    source = f"class C:\n    def elsewhere(self, now):\n        {body}\n"
    assert any(expected in item for item in violations(source))


def test_the_check_lets_the_accessors_and_the_holds_observation_be() -> None:
    source = (
        "class C:\n"
        "    @property\n"
        "    def _owner_origin(self):\n"
        "        return self._shadow.session.owner\n"
        "    def elsewhere(self):\n"
        "        self._hold.observe(True)\n"
        "        return self._owner_origin, self._hold.was_on\n"
    )
    assert violations(source) == []
