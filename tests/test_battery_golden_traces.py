"""Every recipe's write path, pinned look by look before the executor was split into modules: each brand in
`commands.CONTROLS` and each export control in `commands.EXPORT_CONTROLS`, run on the fake plant of its own test
file through the same scenarios (a start of each action, renewals, changes of action, the expiry test, a restart's
hand-back, someone else's value, the fuse, failing writes and the day's cap), and compared byte for byte with the
traces recorded on the engine as it was (`fixtures/battery/golden/<rig>.json`).

Per look: the writes (role, method, entity, value, restore, device), the events, `persist_first`, and a digest of
everything the executor keeps (its state, ledger and monitor as stored, and the attributes that are not stored).
Per scenario end: those in full. A refactor that moves code must leave every fixture as it is; if it renames an
attribute that is not stored, change `_unstored` here, never a fixture.

Regenerate only on purpose, on the engine whose behaviour is wanted:
`python -m tests.test_battery_golden_traces`."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.battery.commands import (
    ACTION_CURTAIL_EXPORT,
    CONTROLS,
    DEYE_CONTROL,
    ECOFLOW_CONTROL,
    EXPORT_CONTROLS,
    FERROAMP_CONTROL,
    FOXESS_CONTROL,
    FRONIUS_CONTROL,
    GOODWE_CONTROL,
    GOODWE_EXPORT_CONTROL,
    HUAWEI_CONTROL,
    KOSTAL_CONTROL,
    MARSTEK_CONTROL,
    SIGENERGY_CONTROL,
    SIGENERGY_EXPORT_CONTROL,
    SOLAREDGE_CONTROL,
    SOLAX_CONTROL,
    SOLAX_EXPORT_CONTROL,
    SOLINTEG_CONTROL,
    SOLIS_CONTROL,
    SOLIS_EXPORT_CONTROL,
    SUNGROW_EXPORT_CONTROL,
    TESLA_CONTROL,
    VICTRON_CONTROL,
    VICTRON_EXPORT_CONTROL,
    BrandControl,
)
from custom_components.spotnav.battery.executor import ExecInputs, ExecState, Executor, Op, StepResult
from custom_components.spotnav.battery.ledger import Ledger
from custom_components.spotnav.battery.monitor import Monitor
from tests import (
    test_battery_deye_control as deye,
    test_battery_ecoflow as ecoflow,
    test_battery_export_brands as export_brands,
    test_battery_foxess as foxess,
    test_battery_fronius_control as fronius,
    test_battery_goodwe_tesla as goodwe_tesla,
    test_battery_kostal_control as kostal,
    test_battery_marstek_control as marstek,
    test_battery_solinteg as solinteg,
    test_battery_solis_control as solis,
    test_battery_victron as victron,
)
from tests.battery_control_fakes import (
    FERROAMP_STATES,
    HUAWEI_STATES,
    SIGENERGY_STATES,
    SOLAREDGE_STATES,
    SOLAX_STATES,
    T0,
    TICK,
    FakeBattery,
    inputs,
)

FIXTURES = Path(__file__).parent / "fixtures" / "battery" / "golden"
#: The shared watchdog's renewal interval where the recipe names none of its own (s).
SHARED_RENEW_S = 900.0
ACTIONS = (("hold", 0.0), ("grid_charge", 3.0), ("discharge_house", 0.0), ("export_discharge", 2.0))

Look = Callable[[Executor, Any, datetime, "str | None", float, dict[str, Any]], ExecInputs]


def _fake_look(*, takeover: bool = False, own_phases: bool = False, clocked: bool = False) -> Look:
    """A look at a `FakeBattery`, with what each brand's own test run adds to it."""

    def look(executor: Executor, plant: Any, now: datetime, action: str | None, rate: float, more: dict[str, Any]) -> ExecInputs:
        extra: dict[str, Any] = {}
        if takeover:
            extra["takeover"] = True
        if own_phases:
            extra["charge_phases"] = executor.control.grid_charge_phases
        if clocked:
            plant.at(now)
            extra["power_kw"] = plant.power_kw()
        return inputs(plant, now, action, rate, **(extra | more))

    return look


def _solinteg_look(_executor: Executor, plant: Any, now: datetime, action: str | None, rate: float, more: dict[str, Any]) -> ExecInputs:
    return plant.look(now, action, rate, **more)


def _victron_look(executor: Executor, plant: Any, now: datetime, action: str | None, rate: float, more: dict[str, Any]) -> ExecInputs:
    return plant.inputs(executor, now, action, rate, **more)


def _fake_apply(plant: Any, _now: datetime, op: Op) -> bool:
    return bool(plant.apply(op))


def _victron_apply(plant: Any, now: datetime, op: Op) -> bool:
    plant.apply(now, op)
    return True


@dataclass(frozen=True)
class Rig:
    """One recipe on its fake plant."""

    name: str
    control: BrandControl
    make: Callable[[], Any]
    look: Look = field(default_factory=_fake_look)
    apply: Callable[[Any, datetime, Op], bool] = _fake_apply
    #: The fake follows the executor's action (`battery_control_fakes.run`).
    follows: bool = True
    #: Run on the control's targets per phase.
    phased: bool = False
    #: What every look of this rig is given unless a scenario says otherwise (a fuse with room for the recipe).
    base: tuple[tuple[str, Any], ...] = ()


def _plain(control: BrandControl, states: dict[str, str]) -> Callable[[], Any]:
    return lambda: FakeBattery(control, dict(states))


def _export(control: BrandControl, states: dict[str, str]) -> Callable[[], Any]:
    return lambda: export_brands.Plant(control, dict(states))


RIGS: tuple[Rig, ...] = (
    Rig("sigenergy", SIGENERGY_CONTROL, _plain(SIGENERGY_CONTROL, SIGENERGY_STATES)),
    Rig("solaredge", SOLAREDGE_CONTROL, _plain(SOLAREDGE_CONTROL, SOLAREDGE_STATES)),
    Rig("huawei", HUAWEI_CONTROL, _plain(HUAWEI_CONTROL, HUAWEI_STATES)),
    Rig("solax", SOLAX_CONTROL, _plain(SOLAX_CONTROL, SOLAX_STATES)),
    Rig("ferroamp", FERROAMP_CONTROL, _plain(FERROAMP_CONTROL, FERROAMP_STATES), _fake_look(takeover=True)),
    Rig("solis", SOLIS_CONTROL, lambda: solis._plant()[1], _fake_look(clocked=True), follows=False),
    Rig("deye", DEYE_CONTROL, lambda: deye._plant()[1]),
    Rig("fronius", FRONIUS_CONTROL, lambda: fronius._plant()[1], _fake_look(takeover=True)),
    Rig("kostal", KOSTAL_CONTROL, lambda: kostal._plant()[1], _fake_look(takeover=True)),
    Rig("goodwe", GOODWE_CONTROL, lambda: goodwe_tesla._goodwe()[1], _fake_look(own_phases=True)),
    Rig(
        "tesla", TESLA_CONTROL, lambda: goodwe_tesla._tesla(idle_kw=0.0, capacity_kwh=13.5, rated_kw=None)[1],
        _fake_look(own_phases=True), base=(("headroom_a", 30.0),),
    ),
    Rig("ecoflow", ECOFLOW_CONTROL, lambda: ecoflow._ocean()[1], _fake_look(clocked=True), follows=False),
    Rig("foxess", FOXESS_CONTROL, foxess._inverter),
    Rig("marstek", MARSTEK_CONTROL, lambda: marstek._plant()[1]),
    Rig("solinteg", SOLINTEG_CONTROL, lambda: solinteg._plant()[1], _solinteg_look, follows=False),
    Rig(
        "solinteg_phased", SOLINTEG_CONTROL, lambda: solinteg._plant(solinteg.PHASED)[1], _solinteg_look,
        follows=False, phased=True,
    ),
    Rig("victron", VICTRON_CONTROL, victron.Plant, _victron_look, _victron_apply, follows=False),
    Rig("victron_phased", VICTRON_CONTROL, victron.Plant, _victron_look, _victron_apply, follows=False, phased=True),
    Rig("export_sigenergy", SIGENERGY_EXPORT_CONTROL, _export(SIGENERGY_EXPORT_CONTROL, {"export_limit": "11.5"})),
    Rig("export_victron", VICTRON_EXPORT_CONTROL, _export(VICTRON_EXPORT_CONTROL, {"export_limit": "-1"})),
    Rig(
        "export_sungrow", SUNGROW_EXPORT_CONTROL,
        _export(SUNGROW_EXPORT_CONTROL, {"export_limit": "5000", "export_enable": "off", "limit_min": "0"}),
    ),
    Rig("export_solis", SOLIS_EXPORT_CONTROL, _export(SOLIS_EXPORT_CONTROL, {"export_limit": "8000", "export_enable": "on"})),
    Rig(
        "export_goodwe", GOODWE_EXPORT_CONTROL,
        _export(GOODWE_EXPORT_CONTROL, {"export_limit": "10000", "export_enable": "off"}),
    ),
    Rig("export_solax", SOLAX_EXPORT_CONTROL, _export(SOLAX_EXPORT_CONTROL, {"export_limit": "6000"})),
)


def _unstored(executor: Executor) -> dict[str, Any]:
    """The executor's attributes that are behaviour but not stored: the adapter between the engine and the fixtures."""
    return {
        "renewal_owned": dict(executor.renewal_owned),
        "silenced": executor.silenced,
        "expired": list(executor.expired),
        "diverged": list(executor.diverged),
        "command_wait_since": executor.command_wait_since,
        "restore_wait_since": dict(executor.restore_wait_since),
        "surplus_raised_at": executor._surplus_raised_at,
        "variant": executor.control.variant,
        "phased": executor.control.phased,
    }


def _kept(executor: Executor) -> dict[str, Any]:
    return {**executor.as_dict(), "unstored": _unstored(executor)}


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class Session:
    """One scenario: an executor on its rig's plant, looked at `TICK` apart, every look recorded."""

    def __init__(self, rig: Rig, *, variant: str | None = None) -> None:
        self.rig = rig
        self.plant = rig.make()
        self.executor = Executor(rig.control)
        if variant is not None:
            assert self.executor.use_variant(variant)
        if rig.phased:
            assert self.executor.use_phases(True)
        self.now = T0
        self.failed: tuple[str, ...] = ()
        self.timed_out: tuple[str, ...] = ()
        #: What each of these roles reads instead of the plant's own value (someone else's value, an unreadable one).
        self.mask: dict[str, str | None] = {}
        #: Writes that raise (`raises`), are given up on (`times_out`) or are dropped without an error (`drops`):
        #: role names, or `*` for every role.
        self.raises: set[str] = set()
        self.times_out: set[str] = set()
        self.drops: set[str] = set()
        self.looks: list[dict[str, Any]] = []

    # --- driving ---

    def _hit(self, names: set[str], role: str) -> bool:
        return "*" in names or role in names

    def _write(self, op: Op) -> bool:
        if self._hit(self.raises, op.role) or self._hit(self.times_out, op.role):
            return False
        if self._hit(self.drops, op.role):
            return True
        return self.rig.apply(self.plant, self.now, op)

    def run(self, looks: int, action: str | None, rate: float = 0.0, **more: Any) -> None:
        for _ in range(looks):
            given = self.rig.look(
                self.executor, self.plant, self.now, action, rate,
                {"failed": self.failed, "timed_out": self.timed_out} | dict(self.rig.base) | more,
            )
            if self.mask:
                reads = dict(given.reads)
                for name, state in self.mask.items():
                    if name in reads:
                        reads[name] = replace(reads[name], state=state)
                given = replace(given, reads=reads)
            result = self.executor.step(given)
            self._record(result)
            failed = tuple(op.role for op in result.ops if not self._write(op))
            self.failed = failed
            self.timed_out = tuple(role for role in failed if self._hit(self.times_out, role))
            if self.rig.follows:
                state = self.executor.state
                self.plant.action = state.action if state.phase == "active" else None
                self.plant.rate_kw = state.rate_kw
            self.now += TICK

    def wait(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def restart(self) -> None:
        """Home Assistant restarts: the executor is rebuilt from what it stored, and hands back first."""
        stored = json.loads(_dump(self.executor.as_dict()))
        self.executor = Executor(
            self.rig.control, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
            ExecState.from_dict(stored["state"]),
        )
        self.failed = self.timed_out = ()
        events = self.executor.restart(self.now)
        self.looks.append({"restart": events, "digest": self._digest()})

    def seed(self, **changes: Any) -> None:
        self.executor.state = replace(self.executor.state, day=self.now.date().isoformat(), **changes)

    # --- recording ---

    def _digest(self) -> str:
        return hashlib.sha256(_dump(_kept(self.executor)).encode()).hexdigest()[:16]

    def _record(self, result: StepResult) -> None:
        self.looks.append({
            "ops": [[op.role, op.method, op.entity_id, op.value, op.restore, op.device_id] for op in result.ops],
            "events": list(result.events),
            "persist_first": result.persist_first,
            "digest": self._digest(),
        })

    def trace(self) -> dict[str, Any]:
        return {"looks": self.looks, "end": _kept(self.executor)}


# --- the scenarios ---------------------------------------------------------------------------------------------------


def _actions(control: BrandControl) -> list[tuple[str, float]]:
    if control.kind == "export":
        return [(ACTION_CURTAIL_EXPORT, 0.0)]
    return [(action, rate) for action, rate in ACTIONS if control.supports(action)]


def _main(control: BrandControl) -> tuple[str, float]:
    """The action a scenario about one running action runs: a grid charge where the recipe has one."""
    actions = dict(_actions(control))
    for action in ("grid_charge", "hold", ACTION_CURTAIL_EXPORT):
        if action in actions:
            return action, actions[action]
    return next(iter(actions.items()))


def _more(control: BrandControl) -> dict[str, Any]:
    """What every look of an export control needs: the site exports."""
    return {"grid_import_kw": -3.0} if control.kind == "export" else {}


def _renew_s(control: BrandControl) -> float:
    return float(control.expiry.renew_s or control.charge_block_s or SHARED_RENEW_S)


def _foreign_value(session: Session, role: str) -> str:
    method = session.rig.control.role(role).method
    if method == "switch":
        read = session.executor.state.target.get(role)
        return "off" if read is True else "on"
    if method == "number":
        return "7777"
    return "Someone Else"


def _starts(rig: Rig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    more = _more(rig.control)
    variants = rig.control.variant_names() if len(rig.control.variant_names()) > 1 else (None,)
    for action, rate in _actions(rig.control):
        for variant in variants if action == "hold" else (None,):
            session = Session(rig, variant=variant)
            session.run(8, action, rate, **more)
            session.run(12, "self_consume", **more)
            out[f"start_{action}" + (f"_{variant}" if variant else "")] = session.trace()
    return out


def _renewal(rig: Rig) -> dict[str, Any]:
    control = rig.control
    action, rate = _main(control)
    more = _more(control)
    session = Session(rig)
    session.run(8, action, rate, **more)
    session.wait(_renew_s(control))
    session.run(6, action, rate, **more)
    session.mask = dict.fromkeys(control.expiry.renewed_roles, "unavailable")
    session.wait(_renew_s(control))
    session.run(6, action, rate, **more)
    session.mask = {}
    session.run(4, action, rate, **more)
    if control.renewal_cap:
        session.seed(renewals_today=control.renewal_cap)
        session.wait(_renew_s(control))
        session.run(6, action, rate, **more)
    session.run(10, "self_consume", **more)
    return {"renewal": session.trace()}


def _mode_change(rig: Rig) -> dict[str, Any]:
    control = rig.control
    if control.kind == "export":
        return {}
    rates = dict(_actions(control))
    session = Session(rig)
    for action in ("hold", "grid_charge", "export_discharge", "hold", "grid_charge", "discharge_house", "hold"):
        if action in rates:
            session.run(4, action, rates[action])
    # Changes closer than a renewal on change waits for.
    for action in ("grid_charge", "hold", "grid_charge", "hold"):
        if action in rates:
            session.run(1, action, rates[action])
    session.run(4, "hold" if "hold" in rates else next(iter(rates)), 0.0)
    session.run(10, "self_consume")
    return {"mode_change": session.trace()}


def _expiry(rig: Rig) -> dict[str, Any]:
    control = rig.control
    if control.kind == "export":
        return {}
    action = "hold" if control.supports("hold") else _main(control)[0]
    rate = dict(_actions(control))[action]
    session = Session(rig)
    session.run(8, action, rate)
    # A renewal falls due in the test: it is not written.
    session.wait(_renew_s(control))
    session.run(6, action, rate, expiry_probe=True)
    fallback = dict(control.expiry.fallback_values)
    session.mask = {name: fallback.get(name, "unknown") for name in control.expiry.renewed_roles}
    session.run(6, action, rate, expiry_probe=True)
    session.mask = dict.fromkeys(control.expiry.renewed_roles, "Someone Else")
    session.run(6, action, rate, expiry_probe=True)
    session.mask = {}
    session.run(8, action, rate)
    session.run(10, "self_consume")
    out = {"expiry": session.trace()}
    if control.expiry.silence:
        # A restart in the test's silence: the stored `retake` writes the control back.
        session = Session(rig)
        session.run(8, action, rate)
        session.run(4, action, rate, expiry_probe=True)
        session.restart()
        session.run(10, action, rate)
        session.run(10, "self_consume")
        out["expiry_restart"] = session.trace()
    return out


def _restarts(rig: Rig) -> dict[str, Any]:
    control = rig.control
    action, rate = _main(control)
    more = _more(control)
    out: dict[str, Any] = {}

    session = Session(rig)
    session.run(8, action, rate, **more)
    session.wait(300)
    session.restart()
    session.run(10, action, rate, **more)
    out["restart"] = session.trace()

    # A write in flight when Home Assistant stopped.
    session = Session(rig)
    session.run(2, action, rate, **more)
    session.restart()
    session.run(12, action, rate, **more)
    out["restart_in_flight"] = session.trace()

    # The plant is not read yet after the restart.
    session = Session(rig)
    session.run(8, action, rate, **more)
    session.restart()
    session.mask = {role.name: None for role in control.roles}
    session.run(4, action, rate, **more)
    session.wait(200)
    session.run(4, action, rate, **more)
    session.mask = {}
    session.run(10, action, rate, **more)
    out["restart_unread"] = session.trace()

    # The restore is not taken: every retry, the block, the attempt again, and at last a plant that takes it.
    session = Session(rig)
    session.run(8, action, rate, **more)
    session.restart()
    session.drops = {"*"}
    for _ in range(14):
        session.run(2, "self_consume", **more)
        session.wait(120)
    session.wait(4000)
    session.run(4, "self_consume", **more)
    session.drops = set()
    session.wait(4000)
    session.run(10, "self_consume", **more)
    session.wait(4000)
    session.run(10, "self_consume", **more)
    out["restart_not_taken"] = session.trace()
    return out


def _foreign(rig: Rig) -> dict[str, Any]:
    control = rig.control
    action, rate = _main(control)
    more = _more(control)
    out: dict[str, Any] = {}
    session = Session(rig)
    session.run(8, action, rate, **more)
    written = session.executor.state.written
    if written:
        session.mask = {written[0]: _foreign_value(session, written[0])}
    # Longer than a reading must differ for before it is someone else's (`MIN_DIVERGENCE_S`).
    session.run(30, action, rate, **more)
    session.mask = {}
    session.run(8, "self_consume", **more)
    session.wait(7 * 3600)
    session.run(8, action, rate, **more)
    session.run(8, "self_consume", **more)
    out["foreign"] = session.trace()

    if control.command_role:
        name = control.command_role
        fallback = dict(control.expiry.fallback_values).get(name, "unknown")
        for label, value in (("kind", "Someone Else"), ("altered", None), ("lapsed", fallback)):
            session = Session(rig)
            session.run(8, action, rate, **more)
            read = session.rig.look(session.executor, session.plant, session.now, action, rate, dict(more)).reads.get(name)
            own = (read.state if read is not None else None) or ""
            session.mask = {name: value if value is not None else f"{own.partition(':')[0]}:1"}
            session.run(6, action, rate, **more)
            session.mask = {name: fallback}
            session.run(4, action, rate, **more)
            session.mask = {}
            session.run(8, "self_consume", **more)
            out[f"foreign_command_{label}"] = session.trace()
    return out


def _fuse(rig: Rig) -> dict[str, Any]:
    control = rig.control
    if control.kind == "export":
        return {}
    out: dict[str, Any] = {}
    if control.supports("grid_charge"):
        session = Session(rig)
        session.run(8, "grid_charge", 3.0)
        session.run(6, "grid_charge", 3.0, headroom_a=3.0)
        session.run(6, "grid_charge", 3.0, headroom_a=6.0)
        session.run(8, "grid_charge", 3.0, headroom_a=0.5)
        session.run(10, "self_consume")
        out["fuse_charge"] = session.trace()
    if control.supports("hold"):
        session = Session(rig)
        session.run(6, "hold")
        session.run(6, "hold", headroom_a=1.2)
        session.run(6, "hold", headroom_a=16.0)
        session.wait(900)
        session.run(6, "hold", headroom_a=16.0)
        session.run(8, "self_consume")
        out["fuse_hold"] = session.trace()
    if rig.phased:
        caps = (1.5, 1.5, 1.5)
        session = Session(rig)
        session.run(12, "grid_charge", 4.0, phase_caps_kw=caps)
        session.run(8, "grid_charge", 4.0, phase_caps_kw=caps, phase_headroom_a=(2.0, 10.0, 10.0), headroom_a=2.0)
        session.run(8, "grid_charge", 4.0, phase_caps_kw=caps, phase_headroom_a=(0.5, 10.0, 10.0), headroom_a=0.5)
        session.run(10, "self_consume")
        out["fuse_phases"] = session.trace()
    return out


def _failures(rig: Rig) -> dict[str, Any]:
    control = rig.control
    action, rate = _main(control)
    more = _more(control)
    out: dict[str, Any] = {}
    for label in ("raises", "times_out", "drops"):
        session = Session(rig)
        getattr(session, label).add("*")
        session.run(8, action, rate, **more)
        session.wait(200)
        session.run(6, action, rate, **more)
        getattr(session, label).clear()
        session.run(8, action, rate, **more)
        session.wait(1000)
        session.run(8, action, rate, **more)
        session.run(10, "self_consume", **more)
        out[f"write_{label}"] = session.trace()

    # A write that fails mid-action, on the role written last.
    session = Session(rig)
    session.run(8, action, rate, **more)
    written = session.executor.state.written
    session.raises = set(written[-1:])
    session.run(4, "self_consume", **more)
    session.raises = set()
    session.run(10, "self_consume", **more)
    out["write_raises_on_hand_back"] = session.trace()

    # The day's cap, one write short of it, and the day after.
    cap = min(96, control.write_cap("setting"))
    session = Session(rig)
    session.seed(writes_today=cap - 1)
    session.run(8, action, rate, **more)
    session.run(6, "self_consume", **more)
    session.wait(24 * 3600)
    session.run(8, action, rate, **more)
    session.run(10, "self_consume", **more)
    out["day_cap"] = session.trace()
    return out


SCENARIOS = (_starts, _renewal, _mode_change, _expiry, _restarts, _foreign, _fuse, _failures)


def trace_rig(rig: Rig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for scenario in SCENARIOS:
        out |= scenario(rig)
    return out


def _text(trace: dict[str, Any]) -> str:
    return json.dumps(trace, indent=1, sort_keys=True, default=str) + "\n"


def test_every_recipe_has_a_rig() -> None:
    traced = {rig.control.brand for rig in RIGS if rig.control.kind != "export"}
    assert traced == {control.brand for control in CONTROLS}
    exports = {rig.control.brand for rig in RIGS if rig.control.kind == "export"}
    assert exports == {control.brand for control in EXPORT_CONTROLS}


@pytest.mark.parametrize("rig", RIGS, ids=lambda rig: rig.name)
def test_the_recipe_writes_and_reports_look_by_look_as_recorded(rig: Rig) -> None:
    expected = (FIXTURES / f"{rig.name}.json").read_text()
    got = json.loads(_text(trace_rig(rig)))
    want = json.loads(expected)
    assert set(got) == set(want)
    for name in want:
        for index, (a, b) in enumerate(zip(got[name]["looks"], want[name]["looks"], strict=True)):
            assert a == b, f"{rig.name} {name} look {index}"
        assert got[name]["end"] == want[name]["end"], f"{rig.name} {name}"
    assert _text(trace_rig(rig)) == expected, "byte for byte"


def test_the_traces_exercise_what_they_pin() -> None:
    """Each event kind and hand-back reason the scenarios are there for is in some trace."""
    kinds: set[str] = set()
    reasons: set[str] = set()
    for rig in RIGS:
        for scenario in json.loads((FIXTURES / f"{rig.name}.json").read_text()).values():
            for look in scenario["looks"]:
                for event in [*look.get("events", ()), *look.get("restart", ())]:
                    kinds.add(event["kind"])
                    reasons.add(str(event["reason"]).partition(":")[0])
    assert {"enter", "change", "rate", "renew", "write", "hand_back", "not_started", "peak_cover"} <= kinds, kinds
    assert {"restart", "fuse_headroom", "restore_again"} <= reasons, reasons


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for rig in RIGS:
        (FIXTURES / f"{rig.name}.json").write_text(_text(trace_rig(rig)))
        print(f"wrote {rig.name}")
