"""What SpotNav expects of other integrations, read from SpotNav's own definitions.

Nothing here is a second copy of a key list: each expectation is taken from the module that uses it
(`execution/charger_profiles.py`, `execution/chargers/*.py`, `battery/adapters.py`, `battery/commands.py`,
`vehicles/vehicle_wake.py`, `site/site_detection.py`), so a key added there is checked from the next run on.

The spotnav modules are imported under a stub package, so the integration's own `__init__` (setup code, the card)
is not run; the modules read here still import Home Assistant's types, so it must be installed.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
import types
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from re import _parser as sre_parse  # type: ignore[attr-defined]

KIND_KEY = "key"  # an entity key or unique-id suffix: must be found whole
KIND_SERVICE = "service"  # a service name of the integration
KIND_VALUE = "value"  # a state or option the integration reports or accepts
KIND_FRAGMENT = "fragment"  # a literal run of a detection pattern (looser: patterns carry groups)

#: Entity domains and services that belong to Home Assistant itself: never an upstream expectation.
GENERIC_DOMAINS = frozenset(
    {
        "homeassistant", "persistent_notification", "number", "select", "switch", "button", "sensor",
        "binary_sensor", "input_boolean", "input_number", "calendar", "weather", "notify", "script", "automation",
        "logger", "system_log", "recorder", "frontend", "light", "lock", "climate", "water_heater",
    }
)


@dataclass(frozen=True, slots=True)
class Expectation:
    domain: str  # the integration's domain (registry platform)
    kind: str
    name: str
    origin: str  # where in SpotNav it is relied on, for the report
    #: Other names that serve as well: a profile lists keys "in order of preference", one per integration version or
    #: translation, and only one of them has to exist. Includes `name`.
    alternatives: tuple[str, ...] = ()

    @property
    def ident(self) -> tuple[str, str, str]:
        return (self.domain, self.kind, "|".join(self.alternatives) or self.name)

    @property
    def names(self) -> tuple[str, ...]:
        return self.alternatives or (self.name,)


def load_package(repo: Path) -> types.ModuleType:
    """The spotnav package without running its `__init__` (which imports Home Assistant). A package that is already
    imported (the test suite's) is used as it is."""
    if "custom_components.spotnav" in sys.modules:
        return sys.modules["custom_components.spotnav"]
    for name, path in (("custom_components", repo / "custom_components"), ("custom_components.spotnav", repo / "custom_components" / "spotnav")):
        module = types.ModuleType(name)
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = module
    return sys.modules["custom_components.spotnav"]


def _import(name: str) -> types.ModuleType:
    return importlib.import_module(f"custom_components.spotnav.{name}")


def literal_runs(pattern: re.Pattern[str] | str) -> list[str]:
    """The literal text of a regular expression, in runs: what must appear in anything it can match.

    `(?:^|_)plant_ess_soc$` gives `plant_ess_soc`; `-em:\\d+-(?P<a>[abc])_current$` gives `-em:`, `-`, `_current`.
    Alternations and optional parts break a run and contribute nothing (they need not be there).
    """
    source = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    runs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            runs.append("".join(current))
            current.clear()

    def walk(items: Iterable[tuple[object, object]]) -> None:
        for op, arg in items:
            name = str(op)
            if name == "LITERAL":
                current.append(chr(arg).lower())  # type: ignore[arg-type]
            elif name == "SUBPATTERN":
                flush()
                walk(arg[3])  # type: ignore[index]
                flush()
            elif name in ("MAX_REPEAT", "MIN_REPEAT"):
                low, _high, sub = arg  # type: ignore[misc]
                flush()
                if low >= 1:
                    walk(sub)
                    flush()
            else:
                flush()

    walk(sre_parse.parse(source))
    flush()
    return runs


def _tokens(run: str, *, minimum: int) -> Iterator[str]:
    for piece in re.split(r"[^a-z0-9_]+", run):
        piece = piece.strip("_")
        if len(piece) >= minimum:
            yield piece


_PLAIN_PREFIX = re.compile(r"^\^?(?:\(\?:\^\|(?:_|\[_-\]|\[-_\])\))?")


def _is_plain_key(pattern: re.Pattern[str], run: str) -> bool:
    """The pattern is only a key (with an anchor or a `_` before it and `$` after): no groups, classes or repeats,
    so the key is checked whole. Anything else (`current_l` before a phase digit) is a fragment."""
    body = _PLAIN_PREFIX.sub("", pattern.pattern).removesuffix("$")
    return re.fullmatch(r"[a-z0-9_]+", run.strip("_")) is not None and body.lower() == re.escape(run).lower().replace("\\_", "_")


def match_expectations(domains: Iterable[str], pattern: re.Pattern[str], origin: str) -> Iterator[Expectation]:
    """A unique-id `Match` or detection pattern: one expectation per literal piece of it."""
    runs = literal_runs(pattern)
    for domain in domains:
        for run in runs:
            # A pure key (`plant_ess_soc`, the `_key` helper's output) is checked whole.
            for token in _tokens(run, minimum=6):
                if token == domain or token.startswith(f"{domain}-"):
                    continue
                whole = _is_plain_key(pattern, run)
                yield Expectation(domain, KIND_KEY if whole else KIND_FRAGMENT, token, origin)


# ---- chargers ------------------------------------------------------------------------------------------------


def _numeric(value: str) -> bool:
    return value.strip().lstrip("-").isdigit()


#: Value lists whose members are all different states (the others list spellings of one).
_EACH_VALUE = frozenset({"vehicle_idle_values", "disconnected_values", "held_values"})


def charger_profile_expectations() -> Iterator[Expectation]:
    profiles = _import("execution.charger_profiles")
    for profile in profiles.PROFILES.values():
        if profile.role in (profiles.ROLE_EXCLUDED, profiles.ROLE_EXTERNAL_CONTROLLER):
            continue
        platform = profile.platform
        where = f"execution/charger_profiles.py: {platform}"
        start = profile.start_stop
        key_fields: dict[str, Iterable[str]] = {
            "current_keys": profile.current_keys,
            "energy_keys": profile.energy_keys,
            "session_energy_keys": profile.session_energy_keys,
            "energy_power_keys": profile.energy_power_keys,
            "status_keys": profile.status_keys,
            "current_sensor_keys": profile.current_sensor_keys,
            "enable_switch_keys": profile.enable_switch_keys,
            "current_attribute_sensor_key": (profile.current_attribute_sensor_key,),
        }
        value_fields: dict[str, Iterable[str]] = {
            "charging_values": profile.charging_values,
            "vehicle_idle_values": profile.vehicle_idle_values,
            "disconnected_values": profile.disconnected_values,
            "held_values": profile.held_values,
        }
        if start is not None:
            key_fields["start_stop.keys"] = start.keys
            key_fields["start_stop.start_keys"] = start.start_keys
            key_fields["start_stop.stop_keys"] = start.stop_keys
            value_fields["start_stop.start_options"] = start.start_options
            value_fields["start_stop.stop_options"] = start.stop_options
        for rule in profile.own_modes:
            key_fields[f"own_modes[{rule.label}]"] = rule.keys
        for field_name, keys in key_fields.items():
            names = tuple(dict.fromkeys(key.lower() for key in keys if key))
            if names:
                yield Expectation(platform, KIND_KEY, names[0], f"{where}.{field_name}", names)
        for field_name, values in value_fields.items():
            names = tuple(dict.fromkeys(v.lower() for v in values if v and not _numeric(v)))
            if not names:
                continue
            if field_name in _EACH_VALUE:
                for name in names:
                    yield Expectation(platform, KIND_VALUE, name, f"{where}.{field_name}")
            else:
                yield Expectation(platform, KIND_VALUE, names[0], f"{where}.{field_name}", names)
        for attribute in profile.current_attributes + profile.current_attributes_it:
            yield Expectation(platform, KIND_VALUE, attribute.lower(), f"{where}.current_attributes")


def _module_strings(tree: ast.Module) -> dict[str, object]:
    """Module-level `NAME = "text"` / `NAME = ("a", "b")` constants of a parsed module."""
    found: dict[str, object] = {}
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target, value = node.target.id, node.value
        if target is None:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
        if isinstance(literal, str) or (isinstance(literal, tuple) and all(isinstance(i, str) for i in literal)):
            found[target] = literal
    return found


#: What `phase_switches.py` looks up for each helper a vendor module calls: the constants it reads.
_PHASE_HELPER_CONSTANTS = {
    "force_single_phase": ("FORCE_SINGLE_PHASE_KEYS",),
    "phase_mode_select": ("PSM_KEYS", "MODEL_STATUS_KEYS"),
}


def charger_module_expectations(package_dir: Path) -> Iterator[Expectation]:
    """Keys that a charger module names itself (`*_KEYS` / `*_KEY` constants beside its `*_DOMAIN`), and the
    phase-switch helper keys for the platforms that call a helper."""
    chargers = package_dir / "execution" / "chargers"
    shared = _module_strings(ast.parse((chargers / "phase_switches.py").read_text(encoding="utf-8")))
    for path in sorted(chargers.glob("*.py")):
        if path.name == "phase_switches.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        strings = _module_strings(tree)
        domains = {v for k, v in strings.items() if k.endswith("_DOMAIN") and isinstance(v, str)}
        for domain in domains:
            for name, value in strings.items():
                if name.endswith(("_KEYS", "_KEY")) and not name.startswith("PRE_") and name not in _NOT_ENTITY_KEYS:
                    names = tuple(k.lower() for k in ((value,) if isinstance(value, str) else value))  # type: ignore[union-attr]
                    yield Expectation(domain, KIND_KEY, names[0], f"execution/chargers/{path.name}: {name}", names)
            called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            for helper, constants in _PHASE_HELPER_CONSTANTS.items():
                if helper in called:
                    for constant in constants:
                        names = tuple(k.lower() for k in shared.get(constant, ()))  # type: ignore[union-attr]
                        if names:
                            yield Expectation(
                                domain, KIND_KEY, names[0], f"execution/chargers/phase_switches.py: {constant}", names
                            )


#: Constants named like an entity key that are not one (an OCPP configuration key, not an entity).
_NOT_ENTITY_KEYS = frozenset({"_OCPP_ASSIGNED_CURRENT_KEY"})


def _resolve(node: ast.expr, strings: dict[str, object]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and isinstance(strings.get(node.id), str):
        return strings[node.id]  # type: ignore[return-value]
    return None


def service_expectations(package_dir: Path, known_domains: frozenset[str]) -> Iterator[Expectation]:
    """Every `(domain, service)` of an integration SpotNav calls or checks for, found in the code as the two string
    arguments that follow each other in `call_service(...)`, `async_call(...)` and `has_service(...)`."""
    callers = {"call_service", "async_call", "has_service"}
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        strings = _module_strings(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in callers:
                continue
            resolved = [_resolve(arg, strings) for arg in node.args]
            for domain, service in zip(resolved, resolved[1:]):
                if domain in known_domains and service and domain not in GENERIC_DOMAINS:
                    yield Expectation(
                        domain, KIND_SERVICE, service, f"{path.relative_to(package_dir).as_posix()}:{node.lineno}"
                    )


# ---- batteries, vehicles, site detection ---------------------------------------------------------------------


def battery_expectations() -> Iterator[Expectation]:
    adapters = _import("battery.adapters")
    commands = _import("battery.commands")

    def of(match: object, domains: tuple[str, ...], origin: str) -> Iterator[Expectation]:
        yield from match_expectations(domains, match.pattern, origin)  # type: ignore[attr-defined]

    for adapter in adapters.ADAPTERS:
        platforms = adapter.platforms
        where = f"battery/adapters.py: {adapter.brand}"
        for role, match in adapter.facts.items():
            yield from of(match, platforms, f"{where}.facts[{role}]")
        for rule in adapter.modes:
            yield from of(rule.match, platforms, f"{where}.modes[{rule.label}]")
            for option in rule.free:
                yield from _option(platforms, option, f"{where}.modes[{rule.label}].free")
        if adapter.grid_status:
            yield from of(adapter.grid_status[0], platforms, f"{where}.grid_status")
        if adapter.plant_max:
            yield from of(adapter.plant_max, platforms, f"{where}.plant_max")
        for source in adapter.reserves:
            yield from of(source.match, platforms, f"{where}.reserves")
        for name in ("hold", "grid_charge"):
            spec = getattr(adapter, name)
            if spec is None:
                continue
            if spec.match is not None:
                yield from of(spec.match, platforms, f"{where}.{name}")
            if spec.method == "select" and isinstance(spec.value, str):
                yield from _option(platforms, spec.value, f"{where}.{name}.value")
            if spec.service:
                domain, service = spec.service.split(".", 1)
                yield Expectation(domain, KIND_SERVICE, service, f"{where}.{name}.service")
    for control in vars(commands).values():
        if not isinstance(control, commands.BrandControl):
            continue
        platforms = control.adapter.platforms
        where = f"battery/commands.py: {control.brand}"
        for role in control.roles:
            yield from of(role.match, platforms, f"{where}.{role.name}")
            for full in role.services.values():
                domain, service = full.split(".", 1)
                yield Expectation(domain, KIND_SERVICE, service, f"{where}.{role.name}.services")


def _option(domains: tuple[str, ...], option: str, origin: str) -> Iterator[Expectation]:
    for domain in domains:
        yield Expectation(domain, KIND_VALUE, option.lower(), origin)


def vehicle_expectations() -> Iterator[Expectation]:
    wake = _import("vehicles.vehicle_wake")
    for domain, method in wake.WAKE_METHODS.items():
        if method.service:
            yield Expectation(method.service[0], KIND_SERVICE, method.service[1], "vehicles/vehicle_wake.py")
        if method.button_key:
            yield Expectation(domain, KIND_KEY, method.button_key, "vehicles/vehicle_wake.py")


def site_expectations() -> Iterator[Expectation]:
    detection = _import("site.site_detection")
    for row in detection.METER_ROWS:
        for pattern in (*row.patterns, *row.totals):
            yield from match_expectations(row.platforms, pattern.regex, f"site/site_detection.py: METER_ROWS {row.platforms[0]}")
    for row in detection.BATTERY_ROWS:
        where = f"site/site_detection.py: BATTERY_ROWS {row.platforms[0]}"
        yield from match_expectations(row.platforms, row.regex, where)
        if row.discharge_regex is not None:
            yield from match_expectations(row.platforms, row.discharge_regex, where)
    for platform, _needle in detection._OWN_BALANCING:
        yield Expectation(platform, "integration", "", "site/site_detection.py: _OWN_BALANCING")
    for platform in detection.UPDATE_BEHAVIOUR:
        yield Expectation(platform, "integration", "", "site/site_detection.py: UPDATE_BEHAVIOUR")


def collect(repo: Path, known_domains: frozenset[str]) -> list[Expectation]:
    """Every expectation, without duplicates (the first origin of each is kept)."""
    package_dir = repo / "custom_components" / "spotnav"
    load_package(repo)
    gathered: dict[tuple[str, str, str], Expectation] = {}
    for expectation in (
        *charger_profile_expectations(),
        *charger_module_expectations(package_dir),
        *battery_expectations(),
        *vehicle_expectations(),
        *site_expectations(),
        *service_expectations(package_dir, known_domains),
    ):
        gathered.setdefault(expectation.ident, expectation)
    return list(gathered.values())
