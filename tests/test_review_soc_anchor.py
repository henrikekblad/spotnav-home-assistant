"""Adversarial review of a435aa6: the restart rule and the same-value rule in `resolve_soc`.

A value whose entity came back (a reload or late load of its integration) is flagged `restored` by the
reader, kept only while the charger saw the car plugged in throughout (`plugged_in_throughout`), and the
register an anchor's baseline came from is named; these cases pass that in as the reader does. The first
case was a Home Assistant restart; a restart never shows the car stayed plugged in (re-review), so it is
now the same sequence after a reload of the car's integration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY, resolve_soc, SocAnchor

CAPACITY = 77.0
T0 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def _reading(value: float, age_s: float, *, restored: bool = False) -> SocReading:
    return SocReading(
        soc_percent=value, source="vehicle", entity_id="sensor.ev", vehicle_id="car", age_s=age_s, restored=restored
    )


def _resolve(reading, anchor, register, now, register_entity_id=None, *, throughout=False):
    return resolve_soc(reading=reading, anchor=anchor, register_kwh=register, capacity_kwh=CAPACITY, now=now,
                       vehicle_id="car", register_entity_id=register_entity_id, plugged_in_throughout=throughout)


def _expected(soc: float, kwh: float) -> float:
    return soc + kwh * CHARGE_EFFICIENCY / CAPACITY * 100.0


def test_restart_rule_lost_when_register_reads_while_restored_value_is_still_fresh() -> None:
    # 60 % read at 08:00 with the register at 1000; 10 kWh delivered by 10:00, the car's cloud silent.
    anchor = SocAnchor(60.0, 1000.0, T0, "vehicle", "car")
    restart = T0 + timedelta(hours=2, minutes=30)
    # The restart re-announces 60 % (last_updated = restart), the register has no state yet: anchor kept.
    first = _resolve(_reading(60.0, 2.0, restored=True), anchor, None, restart + timedelta(seconds=2), throughout=True)
    assert first.anchor == anchor
    # 60 s later the register reads 1010 while the restored value is still "fresh" (< 180 s).
    now = restart + timedelta(seconds=60)
    second = _resolve(_reading(60.0, 60.0, restored=True), first.anchor, 1010.0, now, throughout=True)
    # The 10 kWh must still be carried forward; instead the anchor is re-taken at 60 % / 1010 kWh.
    later = _resolve(_reading(60.0, 3600.0), second.anchor, 1010.0, restart + timedelta(hours=1))
    assert later.reading is not None and later.reading.soc_percent == pytest.approx(_expected(60.0, 10.0))


def test_genuine_same_value_reading_while_register_unreadable_is_masked() -> None:
    # 60 % at 08:00, register 1000. 10 kWh delivered (estimate ~72 %).
    anchor = SocAnchor(60.0, 1000.0, T0, "vehicle", "car")
    # 14:00: the car really reports 60 % again (driven back down, or the energy never reached the battery)
    # while the OCPP charger is offline (register unavailable).
    now = T0 + timedelta(hours=6)
    seen = _resolve(_reading(60.0, 5.0), anchor, None, now)
    # 15:00: register back (1010), the 14:00 reading is now stale. The car is at 60 %, not ~72 %.
    later = _resolve(_reading(60.0, 3605.0), seen.anchor, 1010.0, now + timedelta(hours=1))
    assert later.reading is not None
    assert later.reading.soc_percent == pytest.approx(60.0), "a real new reading was masked: over-count"


def test_anchor_from_another_register_after_redetection_overcounts() -> None:
    # A detected Easee used a separate meter (sensor.ev_meter, 500 kWh) as its register; anchor 50 % at 500.
    # The person picks "Automatic" in the card: `_write_charger` stores "", the reload's
    # `async_store_detected_register` stores the charger's lifetime register (6800 kWh). The stored anchor
    # does not say which register its baseline came from.
    anchor = SocAnchor(50.0, 500.0, T0, "vehicle", "car", register_entity_id="sensor.ev_meter")
    now = T0 + timedelta(hours=3)
    after = _resolve(_reading(50.0, 3 * 3600.0), anchor, 6800.0, now, "sensor.easee_lifetime_energy")
    assert after.reading is not None
    assert after.reading.soc_percent == pytest.approx(50.0), "6300 kWh 'delivered': car read as 100 %, not charged"
