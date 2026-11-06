"""The import target with a slow charger: a current written at most every 15 minutes (Zaptec's write policy).

The HALO world of `tests/test_import_target_hold.py`, its current path given Zaptec's interval (`min_interval_s` 900):
a lowering the interval holds back leaves the car at what it draws meanwhile, so the hold sizes the lowering by energy
and pauses the car where no current can bring the hour home in time; no raise comes that its lowering could not follow
within the hour.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.charger_profiles import policy_for, WritePolicy
from custom_components.spotnav.execution.chargers.base import WriteRateLimiter
from custom_components.spotnav.site.import_target import (
    ACTION_FREE,
    ACTION_HELD,
    ACTION_PAUSED,
    ACTION_UNHELD,
    PAUSE_SLOW_CHARGER,
    raise_lock_s,
    share_budget,
    TargetMember,
    TargetShare,
)
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

from .test_import_target_hold import _charging, _HOUR, _V, _world, _World

#: Zaptec's own write policy: a current at most every 15 minutes.
_ZAPTEC = policy_for("zaptec")


def _slow(world: _World, policy: WritePolicy = _ZAPTEC) -> WriteRateLimiter:
    """Give the charger's current path a write policy's interval; returns its limiter."""
    current = world.halo.cc.adapter.current
    limiter = WriteRateLimiter(policy)
    current.policy = policy
    current._limiter = limiter  # noqa: SLF001
    return limiter


def _row(world: _World) -> str:
    controller = world.halo.controller
    budget = controller._target_budget  # noqa: SLF001
    share = controller.import_target_snapshot()["chargers"].get(world.halo.charger.entry_id) or {}
    so_far = None if budget is None else round(budget.so_far_kwh, 3)
    return (
        f"{dt_util.utcnow():%M:%S} house {world.house_a * 3 * _V:5.0f} W  car {world.drawn_a:4.1f} A  "
        f"hour {so_far} kWh  {share.get('action')} {share.get('cap_a')} {share.get('reason')} "
        f"wait {share.get('write_wait_s')}"
    )


async def _replay(world: _World, minutes: int, rows: list[str]) -> None:
    for _ in range(minutes):
        await world.run(1)
        rows.append(_row(world))


async def _slow_world(hass: HomeAssistant, monkeypatch, frozen, name: str, policy: WritePolicy = _ZAPTEC):
    """A 9 kW target (held under 8.82 kW) and a 1 kW house: the car is held at about 11 A, its current written at
    most once an interval."""
    world = await _world(hass, monkeypatch, frozen, name, target_kw=9.0)
    limiter = _slow(world, policy)
    await world.step()
    await _charging(world)
    return world, limiter


def _share(world: _World) -> dict:
    return world.halo.controller.import_target_snapshot()["chargers"][world.halo.charger.entry_id]


async def test_a_lowering_that_cannot_arrive_before_the_hours_end_pauses_the_car(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world, limiter = await _slow_world(hass, monkeypatch, frozen, "slow50")
        rows: list[str] = []
        await _replay(world, 47, rows)
        limiter.record()  # the current was written at :48: the next write may go out at :03
        await _replay(world, 2, rows)
        assert world.car_a == 12.0 and world.turn_offs == 0
        # The house takes 2 kW more at :50: a fast charger would be held at 8 A, this one would draw 12 A to :03.
        world.house_a = 3000.0 / (3 * _V)
        await _replay(world, 1, rows)
        assert _share(world)["action"] == "paused"
        assert _share(world)["reason"] == "slow_charger"
        assert _share(world)["write_wait_s"] > 600
        await _replay(world, 1, rows)
        assert world.turn_offs == 1 and world.drawn_a == 0.0, "\n".join(rows)
        assert _share(world)["reason"] == "slow_charger"
        log = world.halo.controller.import_target_snapshot()["log"]
        assert any(entry["action"] == "paused" and entry.get("reason") == "slow_charger" for entry in log)
        await _replay(world, 10, rows)
        # The hour ends under the level it is held to (9 kW less its 0.18 kW margin).
        assert world.hour_kw(_HOUR) <= 8.82, "\n".join(rows)


async def test_a_lowering_early_in_the_hour_is_held_by_energy_and_written_at_the_interval(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world, limiter = await _slow_world(hass, monkeypatch, frozen, "slow20")
        rows: list[str] = []
        await _replay(world, 17, rows)
        limiter.record()  # the current was written at :18: the next write may go out at :33
        await _replay(world, 2, rows)
        assert world.car_a == 11.0
        world.house_a = 3000.0 / (3 * _V)
        await _replay(world, 1, rows)
        # Held below what the budget alone would leave (8 A), for the quarter it keeps drawing 11 A meanwhile.
        assert _share(world)["action"] == "held" and _share(world)["cap_a"] == 7.0
        assert _share(world)["reason"] is None
        await _replay(world, 11, rows)
        assert world.drawn_a == 11.0, "the charger takes no new current before its interval"
        await _replay(world, 2, rows)
        assert world.drawn_a == 7.0, "\n".join(rows)
        await _replay(world, 11, rows)
        # From the last interval and a dwell before the hour's end the car is kept where it is set.
        while dt_util.utcnow() < _HOUR + timedelta(minutes=59, seconds=30):
            await world.run(1)
            assert world.drawn_a == 7.0, f"{dt_util.utcnow():%M:%S}: {world.drawn_a} A"
        await world.run(1)
        assert world.turn_offs == 0
        assert world.hour_kw(_HOUR) <= 8.82, "\n".join(rows)


async def test_a_charger_written_every_90_s_is_held_at_the_hours_end_not_paused(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world, limiter = await _slow_world(
            hass, monkeypatch, frozen, "cloud50", WritePolicy(min_interval_s=90.0, call_timeout_s=45.0)
        )
        rows: list[str] = []
        await _replay(world, 49, rows)
        limiter.record()
        world.house_a = 3000.0 / (3 * _V)
        await _replay(world, 3, rows)
        assert _share(world)["action"] == "held", "\n".join(rows)
        await _replay(world, 9, rows)
        assert world.turn_offs == 0
        assert world.hour_kw(_HOUR) <= 8.82, "\n".join(rows)


# -- the share itself (`share_budget`)

_A3 = 3 * _V  # one amp on three phases (W)


def _member(name: str = "a", **fields) -> TargetMember:
    values = {"phases": 3, "min_a": 6.0, "wants_a": 16.0, "draws_a": 12.0, "set_a": 12.0}
    values.update(fields)
    return TargetMember(charger_entry_id=name, **values)


def test_a_slow_charger_whose_write_comes_after_the_hours_end_is_paused() -> None:
    zaptec = _member(write_wait_s=720.0, write_interval_s=900.0)
    assert share_budget(8 * _A3, [zaptec], remaining_s=565.0)["a"] == TargetShare(
        ACTION_PAUSED, 0.0, 0.0, PAUSE_SLOW_CHARGER
    )
    # Its write may go out now: held at what the budget leaves, as any charger.
    ready = replace(zaptec, write_wait_s=0.0)
    assert share_budget(8 * _A3, [ready], remaining_s=565.0)["a"] == TargetShare(ACTION_HELD, 8.0, 8.0)
    # Without the hour's time left (the old call) no slow rule applies.
    assert share_budget(8 * _A3, [zaptec])["a"] == TargetShare(ACTION_HELD, 8.0, 8.0)


def test_a_slow_charger_is_held_by_energy_while_its_write_waits() -> None:
    zaptec = _member(draws_a=11.0, write_wait_s=720.0, write_interval_s=900.0)
    share = share_budget(8 * _A3, [zaptec], remaining_s=2365.0)["a"]
    # 8 A over 2365 s less 11 A for 720 s leaves 6.6 A for the 1645 s after the write.
    assert share == TargetShare(ACTION_HELD, 6.0, 6.0)
    # A charger drawing within its share meanwhile is held as before.
    within = replace(zaptec, draws_a=7.0, wants_a=7.0)
    assert share_budget(8 * _A3, [within], remaining_s=2365.0)["a"] == TargetShare(ACTION_FREE, None, 8.0)
    # Too little left for its minimum after the wait: paused, and said why.
    tight = share_budget(7 * _A3, [zaptec], remaining_s=2365.0)["a"]
    assert tight == TargetShare(ACTION_PAUSED, 0.0, 0.0, PAUSE_SLOW_CHARGER)


def test_a_slow_charger_counts_what_it_draws_meanwhile_against_the_next_charger() -> None:
    zaptec = _member("a", draws_a=11.0, wants_a=11.0, write_wait_s=720.0, write_interval_s=900.0)
    other = _member("b", draws_a=0.0, wants_a=16.0, set_a=None)
    shares = share_budget(20 * _A3, [zaptec, other], remaining_s=1200.0)
    assert shares["a"].action == ACTION_FREE
    # The first takes 11 A for the whole of the rest of the hour: 9 A left, as without the wait.
    assert shares["b"] == TargetShare(ACTION_HELD, 9.0, 9.0)
    over = share_budget(12 * _A3, [replace(zaptec, draws_a=16.0, wants_a=8.0), other], remaining_s=1200.0)
    # 16 A for 720 s leaves 6 A for the 480 s after it, 12 A over the hour's rest: nothing left for the second.
    assert over["a"] == TargetShare(ACTION_HELD, 6.0, 6.0)
    assert over["b"] == TargetShare(ACTION_PAUSED, 0.0, 0.0)


def test_a_charger_the_target_may_not_hold_is_never_paused_for_its_write() -> None:
    started = _member(holdable=False, draws_a=16.0, write_wait_s=900.0, write_interval_s=900.0)
    assert share_budget(0.0, [started], remaining_s=300.0)["a"] == TargetShare(ACTION_UNHELD)


def test_fast_chargers_share_as_before() -> None:
    """A charger written every 10 s or every minute: the slow rules never apply, whatever its wait."""
    cases = [(allowed, remaining) for allowed in (0.0, 4000.0, 6 * _A3, 9 * _A3, 30 * _A3) for remaining in (60.0, 600.0, 3000.0)]
    for interval, wait in ((10.0, 8.0), (60.0, 55.0), (0.0, 30.0)):
        members = [
            _member("a", write_wait_s=wait, write_interval_s=interval),
            _member("b", draws_a=16.0, wants_a=10.0, write_wait_s=wait, write_interval_s=interval),
            _member("c", switch_only=True, wants_a=10.0, draws_a=10.0),
        ]
        for allowed, remaining in cases:
            assert share_budget(allowed, members, remaining_s=remaining) == share_budget(allowed, members)


def test_the_hour_end_lock_starts_an_interval_and_a_dwell_before_the_end_for_a_slow_charger() -> None:
    assert raise_lock_s(0.0) == 600.0
    assert raise_lock_s(90.0) == 600.0
    assert raise_lock_s(900.0) == 960.0


def test_a_resume_at_the_hours_end_is_a_raise() -> None:
    """The hour's end keeps a charger the target held where it keeps it; a resume or a probe gives it no more."""
    held = SimpleNamespace(
        _target_shares={"a": TargetShare(ACTION_HELD, 10.0, 10.0), "b": TargetShare(ACTION_FREE, None, 12.0)},
        _target_raise_lock={"a": 8.0, "b": 9.0},
    )
    assert SiteCapacityController._target_start_cap(held, "a") == 8.0  # noqa: SLF001
    assert SiteCapacityController._target_start_cap(held, "b") == 9.0  # noqa: SLF001
    held._target_raise_lock = {}  # noqa: SLF001
    assert SiteCapacityController._target_start_cap(held, "a") == 10.0  # noqa: SLF001
    assert SiteCapacityController._target_start_cap(held, "b") is None  # noqa: SLF001


def test_zaptec_is_the_slow_charger_these_rules_are_for() -> None:
    assert _ZAPTEC.min_interval_s == 900.0
    assert raise_lock_s(_ZAPTEC.min_interval_s) > 600.0
