"""The dry run: what the executor would write for an action asked for now, and what handing back would write, on a
throw-away executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from ..commands import BrandControl, Native
from .core import Executor
from .state import ExecInputs, ExecState, Op
from .values import restore_order


@dataclass(frozen=True, slots=True)
class Preview:
    """What the executor would do if the action were asked for now, from a fresh state: why it would not start, or
    the writes it would make (in the brand's order, each read back in turn) and what handing back would write."""

    blocker: str | None
    rate_kw: float = 0.0
    writes: tuple[Op, ...] = ()
    restores: tuple[Op, ...] = ()
    #: The value found per restored role (as read) and the native value a restore would write.
    snapshot: Mapping[str, str] = field(default_factory=dict)
    restore_values: Mapping[str, Native] = field(default_factory=dict)


def preview(
    control: BrandControl, inputs: ExecInputs, *, writes_today: int = 0, watchdog_s: int | None = None
) -> Preview:
    """The executor's own start rules and commands for `inputs.action`, run on a throw-away executor: nothing is
    written, nothing is kept. The shadow parts (`hold.py`) use it so that what they log as "would write" is exactly
    what the write path would write, with the same refusals (management, controls found and enabled, fuse, stale
    data, the day's cap) and the same snapshot rules (a never-set limit is restored as the rating)."""
    executor = Executor(
        control, state=ExecState(day=inputs.now.date().isoformat(), writes_today=writes_today), watchdog_s=watchdog_s
    )
    started, events = executor._try_start(inputs)
    if not started:
        return Preview(events[0]["reason"] if events else "nothing_to_do")
    state = executor.state
    writes: list[Op] = []
    for name in control.enter_order:
        if name not in state.target:
            continue
        role = control.role(name)
        if not executor._satisfied(role, state.target[name], inputs.reads.get(name), restoring=False):
            writes.append(Op(name, role.method, state.entities.get(name), state.target[name], device_id=state.device_id))
    written = tuple(op.role for op in writes)
    executor.state = replace(state, written=written)
    executor._begin_restore(inputs.now, "preview")
    back = executor.state.target
    restores = tuple(
        Op(name, control.role(name).method, state.entities.get(name), back[name], device_id=state.device_id, restore=True)
        for name in restore_order(control, state.action)
        if name in back and name in written
    )
    return Preview(None, state.rate_kw, tuple(writes), restores, dict(state.snapshot), dict(state.restore_values))


def start_rate(control: BrandControl, inputs: ExecInputs) -> tuple[float, str | None]:
    """The rate the fuse rules alone would start `inputs.action` at now (kW: `inputs.rate_kw`, lowered to a grid
    charge's live room with the battery's cover now counted out, `Executor._aim` from a fresh state), and why they
    would refuse it (`fuse_headroom` and the like; the rate is then 0). Who manages the plant, its controls and the
    day's caps are not asked (`preview` asks them): for a question put while this path's own executor has the plant,
    whose own command a fresh executor would read as someone else's."""
    rate, _phases, why = Executor(control)._aim(inputs, True)
    return rate, why
