"""Misses a person has looked at and found to be no drift.

A key SpotNav reads may be built at run time upstream (`f"{home}_rt_{key}"`, `f"current_{phase}"`) or a state may
come from the charger's firmware or a library that is not among the sources, so the plain text search cannot see
it. Each entry says why, and may name `evidence` (all of it must still be found, as keys, fragments or states) so
that the thing vanishing is still caught. An entry SpotNav no longer relies on is reported, to be removed.

Never put a real miss here to quiet the canary: it stays red until SpotNav follows the integration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Reviewed:
    reason: str
    evidence: tuple[str, ...] = ()

    def holds(self, index: object) -> bool:
        return all(
            index.has_key(item) or index.has_fragment(item) or index.has_value(item)  # type: ignore[attr-defined]
            for item in self.evidence
        )


def _entry(domain: str, kind: str, name: str, reason: str, *evidence: str) -> tuple[tuple[str, str, str], Reviewed]:
    return (domain, kind, name), Reviewed(reason, evidence)


REVIEWED: dict[tuple[str, str, str], Reviewed] = dict(
    (
        _entry("tibber", "key", "rt_power", "unique id is f'{home_id}_rt_{description.key}'", "power"),
        _entry("tibber", "key", "rt_powerproduction", "unique id is f'{home_id}_rt_{description.key}'", "powerproduction"),
        _entry("tibber", "fragment", "rt_currentl", "unique id is f'{home_id}_rt_{description.key}'", "currentl1"),
        _entry("tibber", "fragment", "rt_voltagephase", "unique id is f'{home_id}_rt_{description.key}'", "voltagephase1"),
        _entry("enphase_envoy", "fragment", "net_consumption_l", "phase suffix added at run time", "net_consumption"),
        _entry("enphase_envoy", "fragment", "net_ct_current_l", "phase suffix added at run time", "net_ct_current"),
        _entry("enphase_envoy", "fragment", "voltage_l", "phase suffix added at run time", "voltage"),
        _entry("nexblue", "key", "current_1|current_2|current_3", "key is f'current_{phase}'", "current_{phase}"),
        _entry(
            "solaredge_modbus_multi", "key", "b1_dc_power",
            "unique id is the battery's f'{model}_{serial}_B{battery_id}' plus '_dc_power'",
            "_b{self.battery_id}", "_dc_power",
        ),
        _entry("solarman", "fragment", "external_ct", "entity names come from the inverter definitions (YAML)", "external ct"),
        _entry("solarman", "fragment", "grid_l", "entity names come from the inverter definitions (YAML)", "grid"),
        _entry(
            "myenergi", "key", "power-ct_internal_load", "key is f'power-{key}' for the CT groups of pymyenergi",
            "power-{key}", "internal load",
        ),
        _entry("goecharger", "value", "waiting for vehicle", "the status text comes from the go-e library, not the integration"),
        _entry("smartevse", "value", "charging", "states come from the charger's firmware over MQTT/JSON"),
        _entry("smartevse", "value", "connected to ev", "states come from the charger's firmware over MQTT/JSON"),
    )
)
