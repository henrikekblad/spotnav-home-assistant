"""Characterisation of `YieldStepper.observe`: randomized observation sequences, each verdict and the stepper's whole
state after it, digested and compared with the digests recorded from the code as it stood before `observe` was split
into phases. A pure refactor of `observe` keeps every digest; a change of behaviour shows which chunk of cases moved.

The sequences are seeded, so they are the same on every run. Values are drawn near the thresholds the rules compare
against (the limit, the held band, the ceiling, the floor, the follow ratio, the settle and probe times) so every
rule is reached; `test_every_reason_is_reached` keeps it so.
"""

from __future__ import annotations

import hashlib
import random
from typing import get_args

from custom_components.spotnav.execution.yield_stepping import (
    YieldConfig,
    YieldObservation,
    YieldReason,
    YieldStepper,
)

CASES = 600
STEPS = 60
CHUNK = 50


def _config(rng: random.Random) -> YieldConfig:
    ceiling = rng.choice([20.0, 23.0, 25.0])
    return YieldConfig(
        ceiling_a=ceiling,
        unverified_ceiling_a=rng.choice([None, None, 20.0, ceiling - 2.0]),
        min_current_a=rng.choice([6.0, 6.0, 8.0]),
        settle_s=rng.choice([30.0, 10.0]),
    )


def _maybe(rng: random.Random, value, p_none: float = 0.01):
    return None if rng.random() < p_none else value


def _run_case(seed: int) -> tuple[list[str], list[str]]:
    rng = random.Random(seed)
    stepper = YieldStepper(_config(rng))
    phases = rng.choice([("L1",), ("L2",), ("L1", "L2", "L3")])
    limit_base = {p: rng.choice([16.0, 18.0, 20.0]) for p in phases}
    # How much of the car's current reaches the grid (0: a battery takes it all).
    grid_share = rng.choice([0.0, 0.1, 0.5, 1.0])
    house = {p: rng.uniform(5.0, 18.0) for p in phases}
    assigned: float | None = rng.choice([6.0, 8.0, 10.0, 16.0])
    requested = rng.choice([6.0, 10.0, 16.0, 32.0])
    now = rng.uniform(0.0, 1000.0)
    delivered_prev: dict[str, float | None] | None = None
    lines: list[str] = []
    reasons: list[str] = []
    for _ in range(STEPS):
        now += rng.choice([1.0, 5.0, 10.0, 20.0, 31.0, 45.0, 61.0, 95.0, 400.0, 700.0, 4000.0])
        # The assignment: usually what the stepper last wrote, sometimes moved by someone else.
        r = rng.random()
        if r < 0.01:
            assigned = None
        elif r < 0.08:
            assigned = rng.choice([6.0, 7.0, 8.0, 10.0, 13.0, 16.0])
        elif r < 0.18 and assigned is not None:
            assigned = max(0.0, assigned - rng.choice([1.0, 2.0, 3.0]))
        if assigned is not None and rng.random() < 0.05:
            assigned = assigned - 0.005
        # The car's reading.
        if delivered_prev is not None and rng.random() < 0.3:
            delivered = dict(delivered_prev)
        else:
            ratio = rng.choice([0.0, 0.5, 0.79, 0.85, 0.89, 0.98, 1.0, 1.0])
            base = assigned if assigned is not None else rng.choice([0.0, 8.0])
            delivered = {p: _maybe(rng, round(base * ratio + rng.uniform(-0.2, 0.2), 3)) for p in phases}
        delivered_prev = delivered
        # The house load wanders.
        for p in phases:
            house[p] = min(25.0, max(0.0, house[p] + rng.choice([-2.0, -0.5, 0.0, 0.0, 0.5, 2.0])))
        limit = {p: limit_base[p] for p in phases}
        site: dict[str, float | None] = {}
        for p in phases:
            if rng.random() < 0.5:
                offset = rng.choice([-4.0, -1.0, -0.4, -0.2, 0.0, 0.03, 0.2, 0.45, 0.8, 1.2, 2.0, 2.6, 3.5, 6.0])
                site[p] = _maybe(rng, round(limit[p] + offset, 3))
            else:
                d = delivered.get(p) or 0.0
                site[p] = _maybe(rng, round(house[p] + grid_share * d, 3))
        lim_choice = rng.random()
        limit_a = None if lim_choice < 0.1 else ({p: None for p in phases} if lim_choice < 0.13 else limit)
        voltage = rng.random()
        voltage_v = (
            None
            if voltage < 0.5
            else {p: rng.choice([None, 205.0, 215.0, 222.0, 230.0, 238.0]) for p in phases}
        )
        observation = YieldObservation(
            now=now,
            site_current_a=site,
            delivered_current_a=delivered,
            phases=phases,
            battery_charge_a=rng.choice([None, 0.0, 1.0, 2.5, 4.0, 9.0, 14.0]),
            credit_allowed=rng.random() < 0.6,
            battery_power_w=rng.choice([None, -500.0, 0.0, 250.0, 400.0, 1500.0, 3000.0, 6000.0, 12000.0]),
            limit_a=limit_a,
            battery_fresh=rng.random() < 0.7,
            voltage_v=voltage_v,
        )
        if rng.random() < 0.03:
            stepper.note_battery_yielded()
        raw_choice = rng.random()
        if raw_choice < 0.04 or assigned is None:
            raw = None if raw_choice < 0.04 else rng.choice([6.0, 10.0])
        else:
            raw = max(0.0, assigned + rng.choice([-6.0, -3.0, -2.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 2.0]))
        verdict = stepper.observe(raw, requested, assigned, observation)
        reasons.append(verdict.reason)
        state = {k: v for k, v in sorted(vars(stepper).items()) if k != "_config"}
        lines.append(f"{verdict!r}|{state!r}|{stepper.yield_verified}")
        if verdict.action == "write" and verdict.current_a is not None and rng.random() < 0.85:
            assigned = verdict.current_a
    return lines, reasons


def _chunk_digests() -> tuple[list[str], set[str]]:
    digests: list[str] = []
    reached: set[str] = set()
    for start in range(0, CASES, CHUNK):
        h = hashlib.sha256()
        for seed in range(start, start + CHUNK):
            lines, reasons = _run_case(seed)
            reached.update(reasons)
            for line in lines:
                h.update(line.encode())
                h.update(b"\n")
        digests.append(h.hexdigest()[:16])
    return digests, reached


# Recorded from `observe` before it was split into phases.
EXPECTED = [
    "73e05dccad7ff90d",
    "1c40e20c8fde86b6",
    "7f146de0f4024cd4",
    "9b96e33ef5597d5b",
    "1165f172fde9b8e4",
    "1d07272b97f7f484",
    "157569518f6abd53",
    "c88ccfa561530dc3",
    "bd7bd44e2ab53afd",
    "f20d6c7d1db55527",
    "e049394bb70dd40a",
    "ab9bea0bdf9c56a9",
]


def test_observe_matches_recorded_behaviour() -> None:
    digests, _ = _chunk_digests()
    assert digests == EXPECTED


# Step 5 records `held_for_battery_probe` only where step 6 then always proposes the step into the battery's share
# (`probe_step`): state unknown or stale confirmed, the car following, below its request, and room in the battery's
# share. So it is never a final verdict.
UNREACHED = {"held_for_battery_probe"}


def test_every_reason_is_reached() -> None:
    _, reached = _chunk_digests()
    assert set(get_args(YieldReason)) - reached == UNREACHED
