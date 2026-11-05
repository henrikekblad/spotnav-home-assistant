"""The step test for a grid meter's sign (`site/grid_sign.py`), on synthetic sites: a charger steps, the site's grid
reading moves (or moves the wrong way), and the tally raises and clears the warning."""

from __future__ import annotations

from dataclasses import dataclass, field

from custom_components.spotnav.site.grid_sign import (
    Change,
    GridSignCheck,
    KIND_CURRENT,
    KIND_POWER,
    Reading,
    Sample,
)


@dataclass
class Site:
    """A site whose grid is the house plus the chargers less the sun, sampled every tick. `inverted`: the grid reading
    as SpotNav takes it has the wrong sign. `charger_interval`: the chargers report their own draw this often (an OCPP
    charger once a minute), while the grid meter reports on every tick."""

    house_w: float = 600.0
    solar_w: float = 0.0
    battery_w: float | None = None
    inverted: bool = False
    charger_interval: float = 0.0
    kinds: tuple[str, ...] = (KIND_POWER,)
    t: float = 1_000_000.0
    draws: dict[str, float] = field(default_factory=lambda: {"car": 0.0})
    reported: dict[str, Reading] = field(default_factory=dict)

    def grid_w(self) -> float:
        total = self.house_w + sum(self.draws.values()) - self.solar_w + (self.battery_w or 0.0)
        return -total if self.inverted else total

    def sample(self) -> Sample:
        for charger, draw in self.draws.items():
            last = self.reported.get(charger)
            if last is None or self.t - last.at >= self.charger_interval:
                self.reported[charger] = Reading(draw, self.t)
        grid = {kind: Reading(self.grid_w(), self.t) for kind in self.kinds}
        battery = None if self.battery_w is None else Reading(self.battery_w, self.t)
        return Sample(self.t, dict(self.reported), grid, battery, self.battery_w is not None)

    def set_draw(self, charger: str, watts: float) -> None:
        self.draws[charger] = watts


def run(check: GridSignCheck, site: Site, seconds: float, tick: float = 5.0) -> list[Change]:
    changes: list[Change] = []
    end = site.t + seconds
    while site.t < end:
        changes += check.observe(site.sample())
        site.t += tick
    return changes


def steps(check: GridSignCheck, site: Site, count: int, *, watts: float = 7_400.0, charger: str = "car") -> list[Change]:
    """`count` steps of the charger, each switching it on or off, held for two minutes."""
    changes = run(check, site, 60.0)
    for _ in range(count):
        site.set_draw(charger, watts if site.draws[charger] == 0.0 else 0.0)
        changes += run(check, site, 120.0)
    return changes


def test_a_correct_sign_never_warns() -> None:
    check = GridSignCheck({KIND_POWER: False})
    changes = steps(check, Site(), 8)
    assert changes == []
    assert not check.warned(KIND_POWER)
    assert check.as_dict()[KIND_POWER]["recent"] == ["consistent"] * 5


def test_an_inverted_grid_power_warns_after_three_inverted_steps() -> None:
    check = GridSignCheck({KIND_POWER: True})
    site = Site(inverted=True, solar_w=2_500.0)
    assert steps(check, site, 2) == []
    assert not check.warned(KIND_POWER)
    changes = steps(check, site, 1)
    assert changes == [Change(KIND_POWER, True, 3, 0)]
    assert check.warned(KIND_POWER)


def test_a_charger_that_reports_once_a_minute_still_counts() -> None:
    """An OCPP charger shows its draw up to a minute after the meter: the grid's value before is the one from before
    the charger's last report of its old value."""
    check = GridSignCheck({KIND_POWER: False})
    site = Site(inverted=True, charger_interval=60.0)
    steps(check, site, 3)
    assert check.warned(KIND_POWER)


def test_mixed_steps_never_warn() -> None:
    check = GridSignCheck({KIND_POWER: False})
    site = Site()
    changes = steps(check, site, 2)
    site.inverted = True
    changes += steps(check, site, 3)
    site.inverted = False
    changes += steps(check, site, 1)
    site.inverted = True
    changes += steps(check, site, 2)
    assert changes == []
    assert not check.warned(KIND_POWER)


def test_noise_and_small_steps_are_not_counted() -> None:
    check = GridSignCheck({KIND_POWER: False})
    site = Site(inverted=True)
    # Under 6 A on one phase: no step.
    assert steps(check, site, 4, watts=1_200.0) == []
    # A step the grid answers with far more than the charger (something else switched with it): not counted.
    run(check, site, 60.0)
    site.set_draw("car", 2_000.0)
    site.house_w += 6_000.0
    run(check, site, 120.0)
    # A step the grid hardly answers: not counted.
    site.set_draw("car", 0.0)
    site.house_w += 1_500.0
    run(check, site, 120.0)
    assert check.as_dict()[KIND_POWER]["recent"] == []


def test_a_battery_that_moves_in_the_window_keeps_the_step_out() -> None:
    check = GridSignCheck({KIND_POWER: True})
    site = Site(inverted=True, battery_w=0.0)
    run(check, site, 60.0)
    for index in range(4):
        on = index % 2 == 0
        site.set_draw("car", 7_400.0 if on else 0.0)
        # The battery holds the grid: it discharges by what the car draws.
        site.battery_w = -5_000.0 if on else 0.0
        run(check, site, 120.0)
    assert not check.warned(KIND_POWER)
    assert check.as_dict()[KIND_POWER]["recent"] == []


def test_a_battery_without_a_fresh_reading_keeps_the_step_out() -> None:
    check = GridSignCheck({KIND_POWER: True})
    site = Site(inverted=True)
    run(check, site, 60.0)
    site.set_draw("car", 7_400.0)
    end = site.t + 120.0
    while site.t < end:
        sample = site.sample()
        check.observe(Sample(sample.at, sample.chargers, sample.grid, None, battery_known=True))
        site.t += 5.0
    assert check.as_dict()[KIND_POWER]["recent"] == []


def test_another_charger_stepping_with_it_keeps_the_step_out() -> None:
    check = GridSignCheck({KIND_POWER: True})
    site = Site(inverted=True, draws={"car": 0.0, "other": 0.0})
    run(check, site, 60.0)
    site.set_draw("car", 7_400.0)
    site.set_draw("other", 3_700.0)
    run(check, site, 120.0)
    assert check.as_dict()[KIND_POWER]["recent"] == []


def test_the_warning_clears_after_three_consistent_steps() -> None:
    check = GridSignCheck({KIND_POWER: True})
    site = Site(inverted=True)
    steps(check, site, 3)
    assert check.warned(KIND_POWER)
    site.inverted = False
    changes = steps(check, site, 2)
    assert changes == []
    assert check.warned(KIND_POWER)
    changes = steps(check, site, 1)
    assert changes == [Change(KIND_POWER, False, 2, 3)]
    assert not check.warned(KIND_POWER)


def test_the_tally_survives_a_restart_and_is_dropped_when_the_setting_changed() -> None:
    check = GridSignCheck({KIND_POWER: True})
    steps(check, Site(inverted=True), 3)
    kept = check.as_dict()

    again = GridSignCheck({KIND_POWER: True})
    assert again.load(kept) == []
    assert again.warned(KIND_POWER)

    changed = GridSignCheck({KIND_POWER: False})
    assert changed.load(kept) == [Change(KIND_POWER, False, 3, 0, "setting")]
    assert not changed.warned(KIND_POWER)
    assert changed.as_dict()[KIND_POWER]["recent"] == []


def test_a_malformed_tally_is_ignored() -> None:
    check = GridSignCheck({KIND_POWER: True})
    assert check.load({KIND_POWER: {"flag": True, "recent": ["maybe"], "warned": True, "consistent_since": 0}}) == []
    assert check.load(["garbage"]) == []
    assert not check.warned(KIND_POWER)


def test_the_phase_currents_are_tallied_on_their_own() -> None:
    """A direct site with a correct grid power but phase currents of the other sign: only the currents warn."""

    @dataclass
    class TwoReadings(Site):
        def sample(self) -> Sample:
            sample = super().sample()
            grid = dict(sample.grid)
            grid[KIND_CURRENT] = Reading(-grid[KIND_POWER].value, self.t)
            return Sample(sample.at, sample.chargers, grid, sample.battery, sample.battery_known)

    check = GridSignCheck({KIND_POWER: False, KIND_CURRENT: True})
    changes = steps(check, TwoReadings(kinds=(KIND_POWER,)), 3)
    assert changes == [Change(KIND_CURRENT, True, 3, 0)]
    assert check.warned(KIND_CURRENT)
    assert not check.warned(KIND_POWER)
    # Turning "Grid current is signed" off drops the currents' tally, and they are no longer tested.
    off = GridSignCheck({KIND_POWER: False})
    assert off.load(check.as_dict()) == [Change(KIND_CURRENT, False, 3, 0, "setting")]
    assert not off.warned(KIND_CURRENT)
