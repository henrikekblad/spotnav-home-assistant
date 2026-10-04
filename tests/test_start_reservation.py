"""Two chargers on one site that start in the same tick share the start allowance; one charger's failure
does not abort the regulator's pass for the others.

The report's sequence (I1): two chargers with windows starting at 22:00, a 16 A margin. Each start read
the same measured margin and added nothing for the other's start on its way: both started at 16 A, 32 A
on a 16 A margin until the next regulator pass lowered them. And (S7): an exception in one charger's
step of the pass skipped every later charger's must-lower write.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_MEASURED_CURRENT_SOURCE, MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict

from .helpers import make_site_entry, setup_two_chargers
from .world import PHASES, controller_of, set_charger_delivered_a, set_derived_site_entities, set_site_current_a

pytestmark = pytest.mark.usefixtures("offline_relay")


def _wiring(prefix: str) -> dict[str, Any]:
    return {
        "phases": 3,
        "phase": None,
        "min_current_a": 6.0,
        CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
            PhaseMeasurementSource(
                kind="separate_entities", entity_ids={p: f"sensor.{prefix}_{p.lower()}" for p in PHASES}
            )
        ),
    }


async def _two_charger_site(hass: HomeAssistant, *, site_a: float) -> tuple[Any, Any, Any, list, list]:
    entry_a, entry_b, turn_on, turn_off = await setup_two_chargers(hass)
    for prefix in ("ca", "cb"):
        set_charger_delivered_a(hass, prefix, 0.0)
    derived = set_derived_site_entities(hass, "pair_site", 0.0)
    site = make_site_entry(
        hass,
        entry_id="pair_site",
        main_fuse_a=20.0,
        safety_margin_a=0.0,
        charger_entry_ids=[entry_a.entry_id, entry_b.entry_id],
        phase_wiring={entry_a.entry_id: _wiring("ca"), entry_b.entry_id: _wiring("cb")},
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        active_control_enabled=True,
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    set_site_current_a(hass, "pair_site", site_a)
    await hass.async_block_till_done()
    return controller_of(hass, entry_a.entry_id), controller_of(hass, entry_b.entry_id), controller_of(
        hass, site.entry_id
    ), turn_on, turn_off


async def test_two_starts_in_one_tick_do_not_both_take_the_whole_margin(hass: HomeAssistant) -> None:
    a, b, site, turn_on, _ = await _two_charger_site(hass, site_a=4.0)  # 16 A of margin
    assert site.start_allowance_a(a.entry_id) == pytest.approx(16.0)

    started = await asyncio.gather(a.async_start(16), b.async_start(16))

    assert started.count(True) == 1, "the second start finds the first one's 16 A taken"
    assert len(turn_on) == 1
    late = b if started[0] else a
    assert late.paused_by_balancing, "the start held back waits for headroom as load balancing's pause"


async def test_a_reservation_is_split_by_what_is_left(hass: HomeAssistant) -> None:
    a, b, site, turn_on, _ = await _two_charger_site(hass, site_a=0.0)  # 20 A of margin

    assert await a.async_start(10) is True
    assert site.start_allowance_a(b.entry_id) == pytest.approx(10.0), "20 A less the 10 A on its way"
    set_charger_delivered_a(hass, "ca", 10.0)
    set_site_current_a(hass, "pair_site", 10.0)
    await hass.async_block_till_done()
    assert site.start_allowance_a(b.entry_id) == pytest.approx(10.0), "once drawn, the meter counts it once"


async def test_a_start_that_did_not_go_out_frees_its_reservation(hass: HomeAssistant) -> None:
    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)

    async def not_executed(_amps: Any = None) -> bool:
        return False

    a.adapter.async_start = not_executed  # type: ignore[method-assign]
    assert await a.async_start(16) is False
    assert site.start_allowance_a(b.entry_id) == pytest.approx(16.0)


async def test_one_chargers_failure_does_not_abort_the_pass_for_the_others(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)
    seen: list[str] = []

    async def fails(self, *args: Any, **kwargs: Any) -> Any:
        seen.append(self.entry_id)
        if self is a:
            raise TimeoutError("the charger's cloud did not answer")
        return await original(self, *args, **kwargs)

    from custom_components.spotnav.execution.controller import ChargingController

    original = ChargingController.async_apply_regulated_current
    monkeypatch.setattr(ChargingController, "async_apply_regulated_current", fails)
    monkeypatch.setattr(type(site), "_is_commandable_charger", staticmethod(lambda _controller: True))
    site.regulator_decisions = {
        a.entry_id: _overload(a.entry_id),
        b.entry_id: _overload(b.entry_id),
    }
    monkeypatch.setattr(
        "custom_components.spotnav.site.site_capacity_controller.applyability_failure", lambda **_kwargs: None
    )
    await site._async_apply_active_control()  # noqa: SLF001

    assert seen == [a.entry_id, b.entry_id], "the second charger's write is still made"


def _overload(entry_id: str) -> Any:
    from custom_components.spotnav.site.regulator import RegulatorDecision

    return RegulatorDecision(
        proposed_current_a=8.0,
        reason="reducing_current_due_to_active_import_overload",
        limiting_phase=None,
    )


async def test_an_unread_reservation_ends_after_a_short_while(hass: HomeAssistant) -> None:
    """A charger whose own current reads nothing holds its share at most `START_RESERVATION_UNREAD_S`."""
    from custom_components.spotnav.site.site_capacity_controller import START_RESERVATION_UNREAD_S

    a, b, site, _, _ = await _two_charger_site(hass, site_a=0.0)  # 20 A of margin
    clock = {"now": 1000.0}
    site._yield_now = lambda: clock["now"]  # noqa: SLF001
    assert await a.async_start(10) is True
    for p in PHASES:
        hass.states.async_set(f"sensor.ca_{p.lower()}", "unavailable")
    await hass.async_block_till_done()
    assert site.start_allowance_a(b.entry_id) == pytest.approx(10.0), "the meter does not show it yet"

    clock["now"] += START_RESERVATION_UNREAD_S + 1
    assert site.start_allowance_a(b.entry_id) == pytest.approx(20.0)


async def test_a_start_that_fails_after_reserving_frees_its_reservation(hass: HomeAssistant) -> None:
    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)

    async def broken_save() -> None:
        raise OSError("disk full")

    a._async_save = broken_save  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(OSError):
        await a.async_start(16)
    assert site.start_allowance_a(b.entry_id) == pytest.approx(16.0)
