"""Detect a site's grid meter and home battery from the entity registry.

The registry is scanned, not only the state machine, because most integrations ship their per-phase
current and voltage entities disabled by default: a candidate lists the disabled ones so the card can
offer to enable them. Everything here is a *suggestion*; nothing is applied until a person confirms
(`apply_candidate` turns a confirmed candidate into config data).

Identification follows one rule: find the integration by `platform` (the config-entry domain), then
match entities by `unique_id` suffix or `translation_key`, never by entity id or friendly name, and
carry each integration's sign conventions in its catalogue row:

* `signed_current`: the integration reports export as a negative current (read |I| for the fuse);
* `invert_power`: grid power is export-positive (negate it);
* `power_export`: import and export are two entities, both >= 0 (P = import - export).

Integrations not in the catalogue fall through to `_generic_candidates`: device class plus an
extended phase token (L1/L2/L3, `phase a`, `ph_b`, R/S/T, and an unsuffixed phase A whose `_ph_b`
and `_ph_c` siblings sit on the same device). Inverter-output entities are rejected in both paths.

The functions take plain snapshots (`RegistryEntity`, `RegistryDevice`) so the catalogue is testable
from recorded registry shapes; `snapshot_registry` reads them from Home Assistant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_BATTERY_DISCHARGE_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERTED,
    CONF_CHARGE_CONTROL,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_GRID_POWER_INVERTED,
    CONF_GRID_POWER_SOURCE,
    CONF_MEASUREMENT_MODE,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SITE_CURRENT_SOURCE,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
)
from .measurement_source import (
    GridPowerSource,
    grid_power_source_to_dict,
    PhaseMeasurementSource,
    source_to_dict,
)
from .site_capacity import PHASES, PhaseName

Role = Literal[
    "current",
    "power",
    "power_export",
    "voltage",
    "reactive_power",
    "apparent_power",
    # The meter's total grid power, not per phase: one signed entity, or with `grid_power_export` an
    # import/export pair. Solar and hybrid read it on a site whose phases report current only.
    "grid_power",
    "grid_power_export",
]
Confidence = Literal["high", "medium", "low"]

# Warning codes (stable, translated by the card).
WARNING_OWN_LOAD_BALANCING: Final = "own_load_balancing"
WARNING_SLOW_UPDATE: Final = "update_interval_exceeds_max_age"
WARNING_ON_CHANGE_ONLY: Final = "reports_on_change_only"
WARNING_SIGN_UNVERIFIED: Final = "sign_unverified"
WARNING_VOLTAGE_OTHER_DEVICE: Final = "voltage_from_other_device"
WARNING_MAY_MEASURE_SUBCIRCUIT: Final = "may_measure_subcircuit"

# Registry `disabled_by` value of an entity its integration ships disabled: the only kind this module
# offers to enable (a person's own choice to disable an entity is never overridden).
_DISABLED_BY_INTEGRATION: Final = "integration"

_ROLE_ORDER: Final[tuple[Role, ...]] = (
    "power",
    "power_export",
    "voltage",
    "current",
    "apparent_power",
    "reactive_power",
    "grid_power",
    "grid_power_export",
)

_LETTER_PHASE: Final[dict[str, PhaseName]] = {"a": "L1", "b": "L2", "c": "L3", "r": "L1", "s": "L2", "t": "L3"}
_NUMBER_PHASE: Final[dict[str, PhaseName]] = {"1": "L1", "2": "L2", "3": "L3"}
_ZERO_BASED_PHASE: Final[dict[str, PhaseName]] = {"0": "L1", "1": "L2", "2": "L3"}
# EDL21 / IEC 62056 OBIS phase codes: current 31/51/71, power 21/41/61, voltage 32/52/72.
_OBIS_PHASE: Final[dict[str, PhaseName]] = {
    "31": "L1",
    "51": "L2",
    "71": "L3",
    "21": "L1",
    "41": "L2",
    "61": "L3",
    "32": "L1",
    "52": "L2",
    "72": "L3",
}


@dataclass(frozen=True, slots=True)
class RegistryEntity:
    """The part of an entity-registry entry detection reads."""

    entity_id: str
    platform: str
    unique_id: str = ""
    translation_key: str | None = None
    original_name: str | None = None
    device_class: str | None = None
    unit: str | None = None
    device_id: str | None = None
    config_entry_id: str | None = None
    disabled_by: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def object_id(self) -> str:
        return self.entity_id.split(".", 1)[-1]


@dataclass(frozen=True, slots=True)
class RegistryDevice:
    """The part of a device-registry entry detection reads."""

    id: str
    manufacturer: str | None = None
    model: str | None = None
    name: str | None = None
    config_entry_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Pattern:
    """One entity shape: `regex` (lower-case, `search`ed) names a `role`'s entity, and its named
    groups say which phase: `a` (letter a-c or r-t), `n` (1-3), `z` (0-2) or `o` (an OBIS code)."""

    role: Role
    regex: re.Pattern[str]
    # The phase of an entity whose name carries none: ZHA's `rms_current` is phase A and has `_ph_b` and
    # `_ph_c` siblings. Only used when the regex names no phase group.
    phase: PhaseName | None = None


def _p(role: Role, pattern: str, phase: PhaseName | None = None) -> Pattern:
    return Pattern(role, re.compile(pattern), phase)


@dataclass(frozen=True, slots=True)
class MeterRow:
    """One integration's grid-meter entity shapes and sign conventions."""

    platforms: tuple[str, ...]
    patterns: tuple[Pattern, ...]
    signed_current: bool = False
    invert_power: bool = False
    reject: re.Pattern[str] | None = None
    device_manufacturer: str | None = None
    device_model: str | None = None
    # Sigen reports no grid-meter voltage: the inverter's phase voltage (another device of the same
    # config entry) completes the set, and the candidate says so.
    voltage_any_device: bool = False
    warnings: tuple[str, ...] = ()
    # The integration's total grid power entities (roles `grid_power` and, for an integration that
    # reports import and export as two entities, `grid_power_export`). They only complete a meter that
    # already has its per-phase entities, never make one. A pair needs both halves, or none is used:
    # an import without its export would read as never exporting.
    totals: tuple[Pattern, ...] = ()
    # A device whose model contains this is not a grid meter for the total (HomeWizard's battery
    # reports the same `active_power_w` for its own power).
    totals_reject_model: str | None = None
    # The row's name in `docs/supported.md` where the platform names alone do not tell two rows apart
    # (the HACS and the core `solaredge_modbus`).
    label: str | None = None


@dataclass(frozen=True, slots=True)
class BatteryRow:
    """One integration's home-battery aggregate power entity."""

    platforms: tuple[str, ...]
    regex: re.Pattern[str]
    inverted: bool = False
    # For a battery that reports charge and discharge as two entities: `regex` is the charge power
    # and `discharge_regex` the discharge power.
    discharge_regex: re.Pattern[str] | None = None
    device_model: str | None = None
    reject: re.Pattern[str] | None = None
    # As `MeterRow.label`.
    label: str | None = None


def _re(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


METER_ROWS: Final[tuple[MeterRow, ...]] = (
    # Shelly EM / 3EM / Pro 3EM (Gen2 triphase, Gen1 3EM and the monophase `em1` channels).
    MeterRow(
        platforms=("shelly",),
        patterns=(
            _p("current", r"-em:\d+-(?P<a>[abc])_current$"),
            _p("current", r"-emeter_(?P<z>[012])-current$"),
            _p("current", r"-em1:(?P<z>[012])-current_em1$"),
            _p("power", r"-em:\d+-(?P<a>[abc])_act_power$"),
            _p("power", r"-emeter_(?P<z>[012])-power$"),
            _p("power", r"-em1:(?P<z>[012])-power_em1$"),
            _p("voltage", r"-em:\d+-(?P<a>[abc])_voltage$"),
            _p("voltage", r"-emeter_(?P<z>[012])-voltage$"),
            _p("voltage", r"-em1:(?P<z>[012])-voltage_em1$"),
            _p("apparent_power", r"-em:\d+-(?P<a>[abc])_aprt_power$"),
        ),
        # Gen2 `em` component's `total_act_power`: the sum of the phases' active power, signed as they are.
        totals=(_p("grid_power", r"-em:\d+-total_act_power$"),),
        warnings=(WARNING_MAY_MEASURE_SUBCIRCUIT,),
    ),
    MeterRow(
        platforms=("homewizard",),
        patterns=(
            _p("current", r"_active_current_l(?P<n>[123])_a$"),
            _p("power", r"_active_power_l(?P<n>[123])_w$"),
            _p("voltage", r"_active_voltage_l(?P<n>[123])_v$"),
            _p("reactive_power", r"_active_reactive_power_l(?P<n>[123])_var$"),
            _p("apparent_power", r"_active_apparent_power_l(?P<n>[123])_va$"),
        ),
        # `active_power_w`: import positive (the integration's own production sensor is it negated).
        totals=(_p("grid_power", r"_active_power_w$"),),
        totals_reject_model="bat",
        signed_current=True,
    ),
    MeterRow(
        platforms=("tibber",),
        patterns=(
            _p("current", r"_rt_currentl(?P<n>[123])$"),
            _p("voltage", r"_rt_voltagephase(?P<n>[123])$"),
        ),
        # The Pulse's real-time `power` (consumption) and `powerProduction` (export), both >= 0.
        totals=(
            _p("grid_power", r"_rt_power$"),
            _p("grid_power_export", r"_rt_powerproduction$"),
        ),
    ),
    MeterRow(
        platforms=("dsmr",),
        patterns=(
            _p("current", r"(?:^|_)(?:instantaneous_)?current_l(?P<n>[123])$"),
            _p("power", r"(?:^|_)(?:instantaneous_)?active_power_l(?P<n>[123])_positive$"),
            _p("power_export", r"(?:^|_)(?:instantaneous_)?active_power_l(?P<n>[123])_negative$"),
            _p("voltage", r"(?:^|_)(?:instantaneous_)?voltage_l(?P<n>[123])$"),
        ),
        # `current_electricity_usage` and `current_electricity_delivery` (OBIS 1.7.0 and 2.7.0), both >= 0.
        totals=(
            _p("grid_power", r"(?:^|_)current_electricity_usage$"),
            _p("grid_power_export", r"(?:^|_)current_electricity_delivery$"),
        ),
    ),
    MeterRow(
        platforms=("dsmr_reader",),
        patterns=(
            _p("current", r"phase_power_current_l(?P<n>[123])$"),
            _p("power", r"phase_currently_delivered_l(?P<n>[123])$"),
            _p("power_export", r"phase_currently_returned_l(?P<n>[123])$"),
            _p("voltage", r"phase_voltage_l(?P<n>[123])$"),
        ),
        # `electricity_currently_delivered` is what the grid delivers (import), `..._returned` the export.
        totals=(
            _p("grid_power", r"electricity_currently_delivered$"),
            _p("grid_power_export", r"electricity_currently_returned$"),
        ),
    ),
    MeterRow(
        platforms=("p1_monitor",),
        patterns=(
            _p("current", r"current_phase_l(?P<n>[123])$"),
            _p("power", r"power_consumed_phase_l(?P<n>[123])$"),
            _p("power_export", r"power_produced_phase_l(?P<n>[123])$"),
            _p("voltage", r"voltage_phase_l(?P<n>[123])$"),
        ),
        # The smart meter service's `power_consumption` and `power_production`, both >= 0.
        totals=(
            _p("grid_power", r"(?:^|_)power_consumption$"),
            _p("grid_power_export", r"(?:^|_)power_production$"),
        ),
    ),
    MeterRow(
        platforms=("amshan",),
        patterns=(
            _p("current", r"[-_]current_l(?P<n>[123])$"),
            _p("voltage", r"[-_]voltage_l(?P<n>[123])$"),
        ),
    ),
    MeterRow(
        platforms=("edl21",),
        patterns=(
            _p("current", r"1-0:(?P<o>31|51|71)\.7\.0"),
            _p("power", r"1-0:(?P<o>21|41|61)\.7\.0"),
            _p("voltage", r"1-0:(?P<o>32|52|72)\.7\.0"),
        ),
        warnings=(WARNING_ON_CHANGE_ONLY,),
    ),
    MeterRow(
        platforms=("huawei_solar",),
        patterns=(
            _p("current", r"active_grid_(?P<a>[abc])_current$"),
            _p("power", r"active_grid_(?P<a>[abc])_power$"),
            _p("voltage", r"(?:^|[_-])grid_(?P<a>[abc])_voltage$"),
        ),
        signed_current=True,
        invert_power=True,
    ),
    MeterRow(
        platforms=("solarman",),
        patterns=(
            _p("current", r"external_ct(?P<n>[123])_current$"),
            _p("power", r"(?:^|[_-])grid_l(?P<n>[123])_power$"),
            _p("voltage", r"(?:^|[_-])grid_l(?P<n>[123])_voltage$"),
        ),
        signed_current=True,
    ),
    MeterRow(
        platforms=("fronius",),
        patterns=(
            _p("current", r"current_ac_phase_(?P<n>[123])$"),
            _p("power", r"power_real_phase_(?P<n>[123])$"),
            _p("reactive_power", r"power_reactive_phase_(?P<n>[123])$"),
            _p("voltage", r"voltage_ac_phase_(?P<n>[123])$"),
        ),
        signed_current=True,
        device_model="meter",
    ),
    MeterRow(
        platforms=("enphase_envoy",),
        patterns=(
            _p("current", r"net_ct_current_l(?P<n>[123])$"),
            _p("power", r"net_consumption_l(?P<n>[123])$"),
            _p("voltage", r"(?:^|[_-])voltage_l(?P<n>[123])$"),
        ),
        # The installer override's per-phase values come from the production CT, not the grid.
        reject=_re(r"production|(?:^|[_-])prod(?:[_-]|$)|generation"),
    ),
    MeterRow(
        platforms=("sma", "pysmaplus"),
        patterns=(
            _p("current", r"metering_current_l(?P<n>[123])$"),
            _p("power", r"metering_active_power_draw_l(?P<n>[123])$"),
            _p("power_export", r"metering_active_power_feed_l(?P<n>[123])$"),
            _p("voltage", r"metering_voltage_l(?P<n>[123])$"),
        ),
        # The inverter's own output, not the grid meter.
        reject=_re(r"grid_power|grid_reactive"),
    ),
    MeterRow(
        platforms=("solaredge_modbus_multi", "solaredge_modbus"),
        patterns=(
            _p("current", r"(?P<m>m\d)_ac_current_(?P<a>[abc])$"),
            _p("power", r"(?P<m>m\d)_ac_power_(?P<a>[abc])$"),
            _p("voltage", r"(?P<m>m\d)_ac_voltage_(?P<a>[abc])n$"),
            _p("reactive_power", r"(?P<m>m\d)_ac_var_(?P<a>[abc])$"),
        ),
        signed_current=True,
        invert_power=True,
        label="SolarEdge Modbus Multi / SolarEdge Modbus (HACS)",
    ),
    # Home Assistant core's `solaredge_modbus` (2026.10, library `solaredged` 0.4.0), which shares the
    # platform name with the HACS integration above. The meter's entities are `<serial>_meter_<id>_<key>`
    # on a "Meter n" sub-device (the inverter's own `<serial>_ac_current_phase_a` has no `_meter_`, so the
    # inverter is never read as a meter). The library hands the SunSpec meter registers on unchanged, as
    # the HACS integration does, so the signs are the same: current and power signed with export
    # positive, and the meter's total `ac_power` likewise. Only a wye meter has the neutral voltages.
    MeterRow(
        platforms=("solaredge_modbus",),
        patterns=(
            _p("current", r"_meter_(?P<m>.+?)_ac_current_phase_(?P<a>[abc])$"),
            _p("power", r"_meter_(?P<m>.+?)_ac_power_phase_(?P<a>[abc])$"),
            _p("voltage", r"_meter_(?P<m>.+?)_ac_voltage_phase_(?P<a>[abc])n$"),
        ),
        totals=(_p("grid_power", r"_meter_(?P<m>.+?)_ac_power$"),),
        signed_current=True,
        invert_power=True,
        label="SolarEdge Modbus (Home Assistant core)",
    ),
    # Bitvis Power Hub (core `bitvis`, 2026.10), a Swedish HAN reader. Unique ids are `<mac>_<key>`. Active
    # power is two non-negative kW floats, `delivered_to_client` (the grid delivers: import) and
    # `delivered_by_client` (export), per phase (disabled by default) and in total (`bitvis-protobuf`
    # `han_port.proto`, both `optional float`, named for the direction rather than signed); current
    # is the per-phase magnitude. The per-phase reactive power is left out: a pair that needs a sign.
    MeterRow(
        platforms=("bitvis",),
        patterns=(
            _p("current", r"(?:^|_)phase_current_l(?P<n>[123])$"),
            _p("voltage", r"(?:^|_)phase_voltage_l(?P<n>[123])$"),
            _p("power", r"(?:^|_)power_active_l(?P<n>[123])_delivered_to_client$"),
            _p("power_export", r"(?:^|_)power_active_l(?P<n>[123])_delivered_by_client$"),
        ),
        totals=(
            _p("grid_power", r"(?:^|_)power_active_delivered_to_client$"),
            _p("grid_power_export", r"(?:^|_)power_active_delivered_by_client$"),
        ),
    ),
    # Solis Modbus (Pho3niX90, `3bcee0e`). Unique ids are `solis_modbus_<serial>_solis_modbus_inverter_<key>`;
    # the meter's three-phase block is `inverter_meter_ac_current_a`, `..._ac_voltage_a`,
    # `..._active_power_a` and `inverter_meter_total_active_power` (a second meter is `inverter_meter2_`
    # and left out). The integration's own "Grid Power Net" is the same register 33263 negated, and its
    # net grid energy is from-grid minus to-grid, so the raw meter power is export-positive: negated. The
    # current has no sign, and the meter may be placed on the load or the PV side (its "Type and Location"
    # register), which the candidate asks to be checked.
    MeterRow(
        platforms=("solis_modbus",),
        patterns=(
            _p("current", r"inverter_meter_ac_current_(?P<a>[abc])$"),
            _p("power", r"inverter_meter_active_power_(?P<a>[abc])$"),
            _p("voltage", r"inverter_meter_ac_voltage_(?P<a>[abc])$"),
        ),
        totals=(_p("grid_power", r"inverter_meter_total_active_power$"),),
        invert_power=True,
        warnings=(WARNING_MAY_MEASURE_SUBCIRCUIT,),
    ),
    # Cozify HAN (Finland, `0d27a23`). Unique ids are `<entry_id>_<key>_<index>`: `i_0..2` current and
    # `u_0..2` voltage per phase, `pi_1..3` import and `pe_1..3` export power per phase, `pi_0` and `pe_0`
    # the totals. Import and export are two entities (the integration is replacing its signed `p` with
    # them). The daily maximum `max_i_<n>` has the same ending as a current and is left out.
    MeterRow(
        platforms=("cozify_han",),
        patterns=(
            _p("current", r"(?<!max)_i_(?P<z>[012])$"),
            _p("voltage", r"_u_(?P<z>[012])$"),
            _p("power", r"_pi_(?P<n>[123])$"),
            _p("power_export", r"_pe_(?P<n>[123])$"),
        ),
        totals=(
            _p("grid_power", r"_pi_0$"),
            _p("grid_power_export", r"_pe_0$"),
        ),
    ),
    # Ferroamp EnergyHub (henricm, `1842058`). `<slug>_ehub-iext-L1..L3` is the grid current. The grid power
    # (`pext`) and the battery power (`pbat`) are passed on from the hub unchanged and nothing in the
    # integration says which way they count, so neither is used: the current alone protects the fuse.
    MeterRow(
        platforms=("ferroamp",),
        patterns=(_p("current", r"-iext-l(?P<n>[123])$"),),
        signed_current=True,
    ),
    # frient Electricity Meter Interface (EMIZB-132) through ZHA. The electrical measurement cluster gives
    # the current of phase A as `rms_current` and of B and C as `rms_current_ph_b` and `_ph_c`, and the
    # total as `total_active_power`, a signed int32 that ZHA passes on unchanged; what the device reports
    # while the household exports is not in the source, so the total's sign is unverified. No power per
    # phase exists, so this is a direct-current meter.
    MeterRow(
        platforms=("zha",),
        device_model="emizb-132",
        patterns=(
            _p("current", r"(?:^|[-_])rms_current$", "L1"),
            _p("current", r"(?:^|[-_])rms_current_ph_(?P<a>[bc])$"),
        ),
        totals=(_p("grid_power", r"(?:^|[-_])total_active_power$"),),
        warnings=(WARNING_SIGN_UNVERIFIED,),
    ),
    MeterRow(
        platforms=("victron_gx", "victron_mqtt", "victron"),
        patterns=(
            _p("current", r"grid_current_(?:phase_)?l?(?P<n>[123])$"),
            _p("power", r"grid_power_(?:phase_)?l?(?P<n>[123])$"),
            _p("voltage", r"grid_voltage_(?:phase_)?l?(?P<n>[123])$"),
        ),
        signed_current=True,
    ),
    MeterRow(
        platforms=("goodwe",),
        patterns=(
            _p("current", r"meter_current(?P<n>[123])$"),
            _p("power", r"meter_active_power(?P<n>[123])$"),
            _p("reactive_power", r"meter_reactive_power(?P<n>[123])$"),
            _p("voltage", r"meter_voltage(?P<n>[123])$"),
        ),
        invert_power=True,
    ),
    MeterRow(
        platforms=("sigen",),
        patterns=(
            _p("power", r"plant_grid_sensor_phase_(?P<a>[abc])_active_power$"),
            _p("reactive_power", r"plant_grid_sensor_phase_(?P<a>[abc])_reactive_power$"),
            _p("voltage", r"phase_(?P<a>[abc])_voltage$"),
        ),
        voltage_any_device=True,
    ),
    MeterRow(
        platforms=("mqtt",),
        device_manufacturer="amsleser.no",
        patterns=(
            _p("current", r"_i(?P<n>[123])$"),
            _p("voltage", r"_u(?P<n>[123])$"),
            _p("power", r"_p(?P<n>[123])$"),
            _p("power_export", r"_po(?P<n>[123])$"),
        ),
    ),
    MeterRow(
        # Any ESPHome device: a DIY or off-the-shelf P1 reader reports its board as the model. The
        # per-phase patterns (a digit 1-3, complete for current, or for power and voltage) are the
        # shape requirement, so a device with a single `current` sensor is not matched.
        platforms=("esphome",),
        patterns=(
            _p("current", r"(?:^|[_-])current_(?:phase_|l)?(?P<n>[123])$"),
            _p("power", r"(?:^|[_-])power_delivered_(?:phase_|l)?(?P<n>[123])$"),
            _p("power_export", r"(?:^|[_-])power_returned_(?:phase_|l)?(?P<n>[123])$"),
            _p("voltage", r"(?:^|[_-])voltage_(?:phase_|l)?(?P<n>[123])$"),
        ),
        reject=_re(r"reactive"),
    ),
    # Sungrow, KRoperUK's `sungrow` integration (local Modbus through a WiNet-S, or iSolarCloud). The
    # smart meter behind the inverter reports power per phase with the sign of the grid: positive is
    # import, so the power is read as it is. The current is a magnitude with no direction, so it is
    # never taken as signed; power and voltage carry the measurement and the current completes it.
    MeterRow(
        platforms=("sungrow",),
        patterns=(
            _p("power", r"meter_phase_(?P<a>[abc])_active_power$"),
            _p("voltage", r"meter_phase_(?P<a>[abc])_voltage$"),
            _p("current", r"meter_phase_(?P<a>[abc])_current$"),
        ),
    ),
    # The community Sungrow Modbus package (mkaiser): plain `modbus` sensors with `sg_` unique ids and no
    # device. Only those ids are read, since the `modbus` platform hosts everyone's sensors; a second
    # inverter's set carries an `_inv_N` suffix and is its own meter.
    MeterRow(
        platforms=("modbus",),
        patterns=(
            _p("power", r"^sg_meter_phase_(?P<a>[abc])_active_power(?:_(?P<m>inv_\d))?$"),
            _p("voltage", r"^sg_meter_phase_(?P<a>[abc])_voltage(?:_(?P<m>inv_\d))?$"),
            _p("current", r"^sg_meter_phase_(?P<a>[abc])_current(?:_(?P<m>inv_\d))?$"),
        ),
    ),
    # SolaX's own plugin of the `solax_modbus` integration (other plugins of it have their own
    # conventions). `measured_power_l1..3` is the grid meter's power per phase; the integration's own
    # computed Grid Export reads it as export when positive, and its house load is the inverter's power
    # minus it, so the power is negated to import-positive. The sign is read from the code, not
    # confirmed on a device, which the candidate says. The meter 2 entities end the same way and are
    # left out.
    MeterRow(
        platforms=("solax_modbus",),
        device_manufacturer="solax",
        patterns=(
            _p("power", r"measured_power_l(?P<n>[123])$"),
            _p("voltage", r"grid_voltage_l(?P<n>[123])$"),
        ),
        invert_power=True,
        reject=_re(r"meter_2"),
        warnings=(WARNING_SIGN_UNVERIFIED,),
    ),
)

# Integrations whose rows are device-filtered because the platform hosts unrelated devices.
_SHARED_PLATFORMS: Final = frozenset({"mqtt", "esphome", "modbus", "zha"})
_CATALOGUED_PLATFORMS: Final = frozenset(
    platform for row in METER_ROWS for platform in row.platforms if platform not in _SHARED_PLATFORMS
) | {"easee"}

BATTERY_ROWS: Final[tuple[BatteryRow, ...]] = (
    # Charge-positive, matching SpotNav's convention.
    BatteryRow(("huawei_solar",), _re(r"storage_charge_discharge_power$")),
    BatteryRow(("sigen",), _re(r"plant_ess_power$")),
    BatteryRow(("victron_gx", "victron_mqtt", "victron"), _re(r"system_dc_battery_power$")),
    BatteryRow(
        ("solaredge_modbus_multi", "solaredge_modbus"),
        _re(r"(?:^|_)b1_dc_power$"),
        label="SolarEdge Modbus Multi / SolarEdge Modbus (HACS)",
    ),
    # Core `solaredge_modbus`: the battery's `dc_power` on its own "Battery n" sub-device
    # (`<serial>_battery_<id>_dc_power`; the inverter's `<serial>_dc_power` is the PV side). The library
    # reads the StorageEdge register unchanged, charge positive as the HACS integration's is.
    BatteryRow(
        ("solaredge_modbus",), _re(r"_battery_.+_dc_power$"), label="SolarEdge Modbus (Home Assistant core)"
    ),
    BatteryRow(("homewizard",), _re(r"_active_power_w$"), device_model="bat"),
    # Discharge-positive: negate.
    BatteryRow(("powerwall",), _re(r"battery_instant_power$"), inverted=True),
    BatteryRow(("tesla_fleet", "teslemetry", "tesla_custom"), _re(r"battery_power$"), inverted=True),
    BatteryRow(("fronius",), _re(r"power_battery$"), inverted=True),
    BatteryRow(("goodwe",), _re(r"pbattery1$"), inverted=True),
    BatteryRow(("solarman",), _re(r"battery_power$"), inverted=True),
    BatteryRow(("foxess_modbus",), _re(r"invbatpower$"), inverted=True),
    BatteryRow(("enphase_envoy",), _re(r"battery_discharge$"), inverted=True),
    # Sungrow reports the battery in two conventions. Local Modbus (the `battery_power` register of
    # KRoperUK's integration and of the community Modbus package) is discharge-positive. The iSolarCloud
    # transport names `total_field_energy_storage_active_power` "Battery Power" and documents it as
    # charge-positive, so it is read as it is, by its code and never by that name; its ESS devices also
    # report charge and discharge as two non-negative entities.
    BatteryRow(
        ("sungrow",),
        _re(r"(?:^|_)battery_power$"),
        inverted=True,
        reject=_re(r"total_field_energy_storage_active_power"),
    ),
    BatteryRow(("modbus",), _re(r"^sg_battery_power$"), inverted=True),
    BatteryRow(("sungrow",), _re(r"total_field_energy_storage_active_power$")),
    BatteryRow(("sungrow",), _re(r"battery_charge_power$"), discharge_regex=_re(r"battery_discharge_power$")),
    BatteryRow(("solis_modbus",), _re(r"battery_power_net$"), inverted=True),
    # Charge and discharge as two non-negative entities.
    BatteryRow(("sma",), _re(r"battery_power_charge$"), discharge_regex=_re(r"battery_power_discharge$")),
)


@dataclass(frozen=True, slots=True)
class DetectedEntity:
    """One entity a candidate uses, for display and for enabling."""

    role: str
    phase: PhaseName | None
    entity_id: str
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class MeterCandidate:
    """A suggested grid-meter setup.

    Either direct (`direct_entities` or an attribute `current_source`) or derived
    (`derived_entities`, per phase `power` and `voltage` plus whichever of `power_export`,
    `reactive_power`, `apparent_power` and `current` the meter has for all three phases).
    """

    candidate_id: str
    integration: str
    title: str
    mode: str
    confidence: Confidence
    direct_entities: Mapping[PhaseName, str] | None = None
    current_source: PhaseMeasurementSource | None = None
    derived_entities: Mapping[PhaseName, Mapping[str, str]] | None = None
    signed_current: bool = False
    power_inverted: bool = False
    # A derived candidate with no current, apparent or reactive power: the fuse check will be
    # estimated from active power (`site_capacity.estimate_current_from_power`).
    estimated: bool = False
    entities: tuple[DetectedEntity, ...] = ()
    warnings: tuple[str, ...] = ()
    # The meter's total grid power where the integration reports one (`MeterRow.totals`).
    grid_power: GridPowerSource | None = None

    @property
    def disabled_entity_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.entity_id for item in self.entities if item.disabled))


@dataclass(frozen=True, slots=True)
class BatteryCandidate:
    """A suggested home-battery power entity and its sign convention."""

    candidate_id: str
    integration: str
    title: str
    entity_id: str
    inverted: bool = False
    discharge_entity_id: str | None = None
    disabled_entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Detection:
    meters: tuple[MeterCandidate, ...] = ()
    batteries: tuple[BatteryCandidate, ...] = ()
    warnings: tuple[str, ...] = field(default=())


def snapshot_registry(hass: HomeAssistant) -> tuple[list[RegistryEntity], list[RegistryDevice]]:
    """The sensor entities (enabled, or disabled by their integration) and all devices."""
    entities: list[RegistryEntity] = []
    for entry in er.async_get(hass).entities.values():
        if entry.domain != "sensor":
            continue
        disabled_by = entry.disabled_by.value if entry.disabled_by is not None else None
        entities.append(
            RegistryEntity(
                entity_id=entry.entity_id,
                platform=entry.platform,
                unique_id=str(entry.unique_id or ""),
                translation_key=entry.translation_key,
                original_name=entry.original_name,
                device_class=entry.device_class or entry.original_device_class,
                unit=entry.unit_of_measurement,
                device_id=entry.device_id,
                config_entry_id=entry.config_entry_id,
                disabled_by=disabled_by,
            )
        )
    devices = [
        RegistryDevice(
            id=device.id,
            manufacturer=device.manufacturer,
            model=device.model,
            name=device.name_by_user or device.name,
            config_entry_ids=tuple(device.config_entries),
        )
        for device in dr.async_get(hass).devices
    ]
    return entities, devices


def _texts(entity: RegistryEntity) -> list[str]:
    texts = [entity.unique_id.lower()]
    if entity.translation_key:
        texts.append(entity.translation_key.lower())
    texts.append(entity.object_id.lower())
    if entity.original_name:
        texts.append(re.sub(r"[^a-z0-9]+", "_", entity.original_name.lower()).strip("_"))
    return [text for text in texts if text]


def _phase_of(match: re.Match[str]) -> PhaseName | None:
    groups = match.groupdict()
    if groups.get("a"):
        return _LETTER_PHASE.get(groups["a"])
    if groups.get("n"):
        return _NUMBER_PHASE.get(groups["n"])
    if groups.get("z"):
        return _ZERO_BASED_PHASE.get(groups["z"])
    if groups.get("o"):
        return _OBIS_PHASE.get(groups["o"])
    return None


def _usable(entity: RegistryEntity, excluded_device_ids: frozenset[str]) -> bool:
    if entity.disabled_by not in (None, _DISABLED_BY_INTEGRATION):
        return False
    return entity.device_id is None or entity.device_id not in excluded_device_ids


def _device_ok(row_manufacturer: str | None, row_model: str | None, device: RegistryDevice | None) -> bool:
    if row_manufacturer is None and row_model is None:
        return True
    if device is None:
        return False
    if row_manufacturer is not None and row_manufacturer not in (device.manufacturer or "").lower():
        return False
    if row_model is not None and row_model not in (device.model or "").lower():
        return False
    return True


def _title(devices: Mapping[str, RegistryDevice], entities: Iterable[RegistryEntity], fallback: str) -> str:
    for entity in entities:
        device = devices.get(entity.device_id or "")
        if device is not None and device.name:
            return device.name
    return fallback


_IncompleteKey = tuple[str, str]


def _catalogue_candidates(
    entities: list[RegistryEntity],
    devices: Mapping[str, RegistryDevice],
    excluded_device_ids: frozenset[str],
) -> tuple[list[MeterCandidate], set[str]]:
    """Candidates from `METER_ROWS`, and the entity ids of every entity a row matched (so the
    generic pass leaves them alone)."""
    candidates: list[MeterCandidate] = []
    claimed: set[str] = set()
    by_platform: dict[str, list[RegistryEntity]] = {}
    for entity in entities:
        by_platform.setdefault(entity.platform, []).append(entity)

    for row in METER_ROWS:
        for platform in row.platforms:
            groups: dict[tuple[str, str], dict[Role, dict[PhaseName, RegistryEntity]]] = {}
            totals: dict[tuple[str, str], dict[Role, RegistryEntity]] = {}
            for entity in by_platform.get(platform, []):
                if not _usable(entity, excluded_device_ids):
                    continue
                if not _device_ok(row.device_manufacturer, row.device_model, devices.get(entity.device_id or "")):
                    continue
                texts = _texts(entity)
                if row.reject is not None and any(row.reject.search(text) for text in texts):
                    continue
                matched = _match_row(row, texts)
                if matched is None:
                    total = _match_total(row, texts)
                    if total is not None and _total_device_ok(row, devices.get(entity.device_id or "")):
                        total_role, total_meter = total
                        totals.setdefault(
                            (entity.config_entry_id or entity.device_id or "", total_meter), {}
                        ).setdefault(total_role, entity)
                    continue
                role, phase, meter = matched
                key = (entity.config_entry_id or entity.device_id or "", meter)
                groups.setdefault(key, {}).setdefault(role, {})[phase] = entity
            for (entry_key, meter), roles in groups.items():
                candidate = _build_row_candidate(
                    row, platform, entry_key, meter, roles, devices, totals.get((entry_key, meter), {})
                )
                if candidate is None:
                    continue
                candidates.append(candidate)
                claimed.update(item.entity_id for item in candidate.entities)
    return candidates, claimed


def _match_row(row: MeterRow, texts: list[str]) -> tuple[Role, PhaseName, str] | None:
    for pattern in row.patterns:
        for text in texts:
            match = pattern.regex.search(text)
            if match is None:
                continue
            phase = _phase_of(match) or pattern.phase
            if phase is None:
                continue
            return pattern.role, phase, match.groupdict().get("m") or ""
    return None


def _match_total(row: MeterRow, texts: list[str]) -> tuple[Role, str] | None:
    """A total's role and, for a row whose meters are told apart (a `m` group), which meter's it is."""
    for pattern in row.totals:
        for text in texts:
            match = pattern.regex.search(text)
            if match is not None:
                return pattern.role, match.groupdict().get("m") or ""
    return None


def _total_device_ok(row: MeterRow, device: RegistryDevice | None) -> bool:
    if row.totals_reject_model is None or device is None:
        return True
    return row.totals_reject_model not in (device.model or "").lower()


def _grid_power_of(
    row: MeterRow, totals: Mapping[Role, RegistryEntity]
) -> tuple[GridPowerSource, list[tuple[Role, PhaseName | None, RegistryEntity]]] | None:
    """The total grid power a meter's `totals` complete, with the entities it uses: the one signed
    entity, or for a row that reports import and export separately both halves (never one alone).
    """
    power = totals.get("grid_power")
    if power is None:
        return None
    if any(pattern.role == "grid_power_export" for pattern in row.totals):
        export = totals.get("grid_power_export")
        if export is None:
            return None
        return (
            GridPowerSource(power=power.entity_id, power_export=export.entity_id),
            [("grid_power", None, power), ("grid_power_export", None, export)],
        )
    return GridPowerSource(power=power.entity_id), [("grid_power", None, power)]


def _complete(roles: Mapping[Role, Mapping[PhaseName, RegistryEntity]], role: Role) -> bool:
    return all(phase in roles.get(role, {}) for phase in PHASES)


def _build_row_candidate(
    row: MeterRow,
    platform: str,
    entry_key: str,
    meter: str,
    roles: dict[Role, dict[PhaseName, RegistryEntity]],
    devices: Mapping[str, RegistryDevice],
    totals: Mapping[Role, RegistryEntity] | None = None,
) -> MeterCandidate | None:
    candidate_id = ":".join(part for part in (platform, entry_key, meter) if part)
    used: list[tuple[Role, PhaseName | None, RegistryEntity]] = []

    def take(role: Role) -> dict[PhaseName, RegistryEntity] | None:
        if not _complete(roles, role):
            return None
        for phase in PHASES:
            used.append((role, phase, roles[role][phase]))
        return {phase: roles[role][phase] for phase in PHASES}

    derived_possible = _complete(roles, "power") and _complete(roles, "voltage")
    flat = [entity for by_phase in roles.values() for entity in by_phase.values()]
    title = _title(devices, flat, platform)
    warnings = list(row.warnings)
    if row.voltage_any_device and {e.device_id for e in roles.get("voltage", {}).values()} != {
        e.device_id for e in roles.get("power", {}).values()
    }:
        warnings.append(WARNING_VOLTAGE_OTHER_DEVICE)

    grid_power = _grid_power_of(row, totals or {})

    def total_entities() -> GridPowerSource | None:
        # Only once the per-phase entities have made this a candidate at all.
        if grid_power is None:
            return None
        used.extend(grid_power[1])
        return grid_power[0]

    if derived_possible:
        power = take("power")
        voltage = take("voltage")
        assert power is not None and voltage is not None
        extras = {role: take(role) for role in ("power_export", "current", "apparent_power", "reactive_power")}
        derived: dict[PhaseName, dict[str, str]] = {}
        for phase in PHASES:
            entry: dict[str, str] = {"power": power[phase].entity_id, "voltage": voltage[phase].entity_id}
            for role, by_phase in extras.items():
                if by_phase is not None:
                    entry[role] = by_phase[phase].entity_id
            derived[phase] = entry
        estimated = not any(extras[role] is not None for role in ("current", "apparent_power", "reactive_power"))
        grid_source = total_entities()
        return MeterCandidate(
            candidate_id=candidate_id,
            integration=platform,
            title=title,
            mode=MEASUREMENT_MODE_DERIVED,
            confidence="high",
            derived_entities=derived,
            signed_current=row.signed_current,
            power_inverted=row.invert_power,
            estimated=estimated,
            entities=_display(used),
            warnings=tuple(warnings),
            grid_power=grid_source,
        )
    current = take("current")
    if current is None:
        return None
    grid_source = total_entities()
    return MeterCandidate(
        candidate_id=candidate_id,
        integration=platform,
        title=title,
        mode=MEASUREMENT_MODE_DIRECT,
        confidence="high",
        direct_entities={phase: current[phase].entity_id for phase in PHASES},
        signed_current=row.signed_current,
        # The sign of the meter's total grid power; there is no per-phase power in direct mode.
        power_inverted=row.invert_power and grid_source is not None,
        entities=_display(used),
        warnings=tuple(warnings),
        grid_power=grid_source,
    )


def _display(used: list[tuple[Role, PhaseName | None, RegistryEntity]]) -> tuple[DetectedEntity, ...]:
    return tuple(
        DetectedEntity(
            role=role,
            phase=phase,
            entity_id=entity.entity_id,
            disabled=entity.disabled_by == _DISABLED_BY_INTEGRATION,
        )
        for role, phase, entity in sorted(
            used,
            key=lambda item: (
                _ROLE_ORDER.index(item[0]),
                -1 if item[1] is None else PHASES.index(item[1]),
            ),
        )
    )


# ---- Easee Equalizer ---------------------------------------------------------------------------

_EASEE_CURRENT: Final = re.compile(r"(?:^|_)current$")
# The Equalizer's `current` entity states the highest phase and carries each phase as an attribute.
_EASEE_ATTRIBUTES: Final[dict[PhaseName, str]] = {
    "L1": "state_currentL1",
    "L2": "state_currentL2",
    "L3": "state_currentL3",
}


def _easee_candidates(
    entities: list[RegistryEntity],
    devices: Mapping[str, RegistryDevice],
    excluded_device_ids: frozenset[str],
) -> tuple[list[MeterCandidate], set[str]]:
    """The Easee Equalizer's grid current, an attributes source on its one `current` entity (disabled
    by default). The Equalizer balances load by itself, which the candidate warns about."""
    found: list[MeterCandidate] = []
    claimed: set[str] = set()
    for entity in entities:
        if entity.platform != "easee" or not _usable(entity, excluded_device_ids):
            continue
        device = devices.get(entity.device_id or "")
        if not _device_ok(None, "equalizer", device):
            continue
        if not any(_EASEE_CURRENT.search(text) for text in (entity.unique_id.lower(), entity.object_id)):
            continue
        found.append(
            MeterCandidate(
                candidate_id=f"easee:{entity.config_entry_id or entity.device_id}:equalizer",
                integration="easee",
                title=(device.name if device and device.name else "Easee Equalizer"),
                mode=MEASUREMENT_MODE_DIRECT,
                confidence="high",
                current_source=PhaseMeasurementSource(
                    kind="attributes",
                    entity_id=entity.entity_id,
                    attributes=dict(_EASEE_ATTRIBUTES),
                    attribute_unit_override="A",
                ),
                entities=(
                    DetectedEntity(
                        "current",
                        None,
                        entity.entity_id,
                        disabled=entity.disabled_by == _DISABLED_BY_INTEGRATION,
                    ),
                ),
                warnings=(WARNING_OWN_LOAD_BALANCING,),
            )
        )
        claimed.add(entity.entity_id)
    return found, claimed


# ---- Generic fallback -------------------------------------------------------------------------

_GENERIC_ROLES: Final[dict[str, Role]] = {
    "current": "current",
    "power": "power",
    "voltage": "voltage",
    "reactive_power": "reactive_power",
    "apparent_power": "apparent_power",
}
_GENERIC_UNITS: Final[dict[Role, tuple[str, ...]]] = {
    "current": ("a", "ma"),
    "power": ("w", "kw"),
    "voltage": ("v",),
    "reactive_power": ("var", "kvar"),
    "apparent_power": ("va", "kva"),
}

# Words marking an entity as something other than the grid connection.
_INVERTER_WORDS: Final = re.compile(
    r"(?:^|_)(?:inverter|output|production|generation|pv|solar|battery|backup|eps|load|evse|charger)(?:_|$)"
    r"|ac_current_[rst]$|ac_power_[rst]$"
)
_GRID_WORDS: Final = re.compile(r"(?:^|_)(?:grid|meter|mains|p1)(?:_|$)")

_PHASE_MARKERS: Final = re.compile(
    r"(?:^|_)(?:"
    r"l(?P<l>[123])"
    r"|(?:phase|ph)_?(?P<pl>[abc123])"
    r"|(?:phase|ph)_(?P<rst>[rst])"
    r")(?=_|$)"
)
_TRAILING_RST: Final = re.compile(r"(?:^|_)(?P<rst>[rst])$")


def _generic_key(entity: RegistryEntity) -> tuple[str, PhaseName | None, bool] | None:
    """`(stem, phase, is_rst)` for the entity's best text, or `None` without a phase token."""
    for text in _texts(entity)[::-1]:
        marker = None
        for candidate in _PHASE_MARKERS.finditer(text):
            marker = candidate
        phase: PhaseName | None = None
        is_rst = False
        stem = text
        if marker is not None:
            groups = marker.groupdict()
            if groups.get("l"):
                phase = _NUMBER_PHASE[groups["l"]]
            elif groups.get("pl"):
                letter = groups["pl"]
                phase = _NUMBER_PHASE[letter] if letter in _NUMBER_PHASE else _LETTER_PHASE[letter]
            elif groups.get("rst"):
                phase = _LETTER_PHASE[groups["rst"]]
                is_rst = True
            stem = (text[: marker.start()] + text[marker.end():]).strip("_")
        else:
            trailing = _TRAILING_RST.search(text)
            if trailing is not None:
                phase = _LETTER_PHASE[trailing.group("rst")]
                is_rst = True
                stem = text[: trailing.start()].strip("_")
        if phase is not None or marker is not None:
            return stem, phase, is_rst
    return None


def _generic_candidates(
    entities: list[RegistryEntity],
    devices: Mapping[str, RegistryDevice],
    excluded_device_ids: frozenset[str],
    claimed: set[str],
) -> list[MeterCandidate]:
    """Candidates for integrations the catalogue does not know.

    Entities are grouped by device (else config entry) and stem (the name minus its phase token); a
    group needs all three phases. R/S/T tokens count only as a complete trio. A bare name with
    `_ph_b`/`_ph_c` (or `_phase_b`/`_phase_c`) siblings on the same device is phase A.
    """
    # (scope, role, stem) -> phase -> entity
    groups: dict[tuple[str, Role, str], dict[PhaseName, RegistryEntity]] = {}
    rst_groups: dict[tuple[str, Role, str], dict[PhaseName, RegistryEntity]] = {}
    bare: dict[tuple[str, Role, str], RegistryEntity] = {}
    for entity in entities:
        if entity.entity_id in claimed or not _usable(entity, excluded_device_ids):
            continue
        if entity.platform in _CATALOGUED_PLATFORMS:
            # The catalogue row is authoritative for its integration: what it refused (the inverter,
            # a production CT) is not a grid meter just because the generic rules would accept it.
            continue
        role = _GENERIC_ROLES.get(entity.device_class or "")
        if role is None:
            continue
        if entity.unit and entity.unit.lower() not in _GENERIC_UNITS[role]:
            continue
        texts = _texts(entity)
        joined = " ".join(texts)
        if any(_INVERTER_WORDS.search(text) for text in texts) and not any(
            _GRID_WORDS.search(text) for text in texts
        ):
            continue
        scope = entity.device_id or entity.config_entry_id or ""
        key = _generic_key(entity)
        if key is not None:
            stem, phase, is_rst = key
            if phase is not None:
                target = rst_groups if is_rst else groups
                target.setdefault((scope, role, stem), {}).setdefault(phase, entity)
                continue
        # An unsuffixed name could be phase A; only valid beside `_ph_b`/`_ph_c` siblings.
        if scope and "reactive" not in joined:
            bare.setdefault((scope, role, entity.object_id.lower()), entity)

    # Phase A: a bare entity whose stem equals a complete-but-A sibling group's stem.
    for (scope, role, stem), by_phase in list(groups.items()):
        if "L1" in by_phase or "L2" not in by_phase or "L3" not in by_phase:
            continue
        for (bare_scope, bare_role, _bare_id), entity in bare.items():
            if bare_scope != scope or bare_role != role:
                continue
            texts = _texts(entity)
            if any(text == stem or text.endswith(stem) for text in texts if stem):
                by_phase["L1"] = entity
                break
    for key, by_phase in rst_groups.items():
        if all(phase in by_phase for phase in PHASES):
            groups.setdefault(key, by_phase)

    complete = {key: by_phase for key, by_phase in groups.items() if all(p in by_phase for p in PHASES)}
    by_scope: dict[str, dict[Role, list[tuple[str, dict[PhaseName, RegistryEntity]]]]] = {}
    for (scope, role, stem), by_phase in complete.items():
        by_scope.setdefault(scope, {}).setdefault(role, []).append((stem, by_phase))

    candidates: list[MeterCandidate] = []
    for scope, roles in sorted(by_scope.items()):
        flat = [entity for families in roles.values() for _stem, by_phase in families for entity in by_phase.values()]
        title = _title(devices, flat, scope or "meter")
        platform = flat[0].platform
        currents = roles.get("current", [])
        powers = roles.get("power", [])
        volts = roles.get("voltage", [])
        if len(powers) == 1 and len(volts) == 1:
            _stem, power = powers[0]
            _vstem, voltage = volts[0]
            used: list[tuple[Role, PhaseName, RegistryEntity]] = [
                ("power", p, power[p]) for p in PHASES
            ] + [("voltage", p, voltage[p]) for p in PHASES]
            derived = {
                phase: {"power": power[phase].entity_id, "voltage": voltage[phase].entity_id}
                for phase in PHASES
            }
            for role in ("current", "apparent_power", "reactive_power"):
                families = roles.get(role, [])
                if len(families) == 1:
                    _s, by_phase = families[0]
                    for phase in PHASES:
                        derived[phase][role] = by_phase[phase].entity_id
                        used.append((role, phase, by_phase[phase]))
            estimated = not any(
                role in derived["L1"] for role in ("current", "apparent_power", "reactive_power")
            )
            candidates.append(
                MeterCandidate(
                    candidate_id=f"generic:{scope}:derived",
                    integration=platform,
                    title=title,
                    mode=MEASUREMENT_MODE_DERIVED,
                    confidence="low",
                    derived_entities=derived,
                    estimated=estimated,
                    entities=_display(used),
                    warnings=(WARNING_SIGN_UNVERIFIED,),
                )
            )
            continue
        for index, (stem, by_phase) in enumerate(currents):
            used = [("current", p, by_phase[p]) for p in PHASES]
            candidates.append(
                MeterCandidate(
                    candidate_id=f"generic:{scope}:current:{stem or index}",
                    integration=platform,
                    title=title,
                    mode=MEASUREMENT_MODE_DIRECT,
                    confidence="medium" if len(currents) == 1 else "low",
                    direct_entities={p: by_phase[p].entity_id for p in PHASES},
                    entities=_display(used),
                )
            )
    return candidates


def _battery_candidates(
    entities: list[RegistryEntity],
    devices: Mapping[str, RegistryDevice],
    excluded_device_ids: frozenset[str],
) -> list[BatteryCandidate]:
    found: list[BatteryCandidate] = []
    taken: set[str] = set()
    for row in BATTERY_ROWS:
        charge: dict[str, RegistryEntity] = {}
        discharge: dict[str, RegistryEntity] = {}
        for entity in entities:
            if entity.platform not in row.platforms or not _usable(entity, excluded_device_ids):
                continue
            device = devices.get(entity.device_id or "")
            if not _device_ok(None, row.device_model, device):
                continue
            texts = _texts(entity)
            scope = entity.config_entry_id or entity.device_id or ""
            if row.reject is not None and any(row.reject.search(text) for text in texts):
                continue
            if any(row.regex.search(text) for text in texts):
                charge.setdefault(scope, entity)
            elif row.discharge_regex is not None and any(row.discharge_regex.search(t) for t in texts):
                discharge.setdefault(scope, entity)
        for scope, entity in charge.items():
            other = discharge.get(scope)
            if row.discharge_regex is not None and other is None:
                continue
            candidate_id = f"battery:{entity.platform}:{scope}"
            if candidate_id in taken:
                continue  # an earlier row already found this integration's battery
            taken.add(candidate_id)
            used = [entity] + ([other] if other is not None else [])
            found.append(
                BatteryCandidate(
                    candidate_id=candidate_id,
                    integration=entity.platform,
                    title=_title(devices, used, entity.platform),
                    entity_id=entity.entity_id,
                    inverted=row.inverted,
                    discharge_entity_id=other.entity_id if other is not None else None,
                    disabled_entity_ids=tuple(
                        item.entity_id for item in used if item.disabled_by == _DISABLED_BY_INTEGRATION
                    ),
                )
            )
    return found


def detect_site(
    entities: list[RegistryEntity],
    devices: list[RegistryDevice],
    *,
    excluded_device_ids: Iterable[str] = (),
) -> Detection:
    """Every grid-meter and battery candidate in the snapshot, best first.

    `excluded_device_ids` should be each charger's device, so a charger is never suggested as the
    site meter.
    """
    excluded = frozenset(excluded_device_ids)
    device_map = {device.id: device for device in devices}
    meters, claimed = _catalogue_candidates(entities, device_map, excluded)
    easee, easee_claimed = _easee_candidates(entities, device_map, excluded)
    meters += easee
    claimed |= easee_claimed
    meters += _generic_candidates(entities, device_map, excluded, claimed)
    rank = {"high": 0, "medium": 1, "low": 2}
    # Within a confidence tier a meter with its own per-phase entities (direct or derived) comes
    # before an attributes source such as the Easee Equalizer, which reports a derived figure and
    # balances load by itself; then by integration and id for a stable order.
    meters.sort(
        key=lambda c: (rank[c.confidence], c.current_source is not None, c.integration, c.candidate_id)
    )
    batteries = _battery_candidates(entities, device_map, excluded)
    batteries.sort(key=lambda c: (c.integration, c.candidate_id))
    return Detection(meters=tuple(meters), batteries=tuple(batteries))


def enable_disabled_entities(hass: HomeAssistant, entity_ids: Iterable[str]) -> None:
    """Enable the entities their integration ships disabled. An entity a person disabled is left
    as it is."""
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is not None and entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity_id, disabled_by=None)


def excluded_charger_devices(hass: HomeAssistant, charger_entry_ids: list[str]) -> set[str]:
    """Every listed charger's device that resolves from its charge-control entity, so a charger
    is never suggested as the site meter."""
    registry = er.async_get(hass)
    excluded: set[str] = set()
    for charger_entry_id in charger_entry_ids:
        charger_entry = hass.config_entries.async_get_entry(charger_entry_id)
        charge_control = charger_entry.data.get(CONF_CHARGE_CONTROL) if charger_entry else None
        entity_entry = registry.async_get(charge_control) if charge_control else None
        if entity_entry is not None and entity_entry.device_id:
            excluded.add(entity_entry.device_id)
    return excluded


def detect_site_from_hass(hass: HomeAssistant, *, excluded_device_ids: Iterable[str] = ()) -> Detection:
    entities, devices = snapshot_registry(hass)
    return detect_site(entities, devices, excluded_device_ids=excluded_device_ids)


# ---- Devices with their own load balancing -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnBalancing:
    """A device that balances load by itself and may fight SpotNav's active control."""

    integration: str
    device_name: str


# (platform, lower-case substring of the device model or name, or `None` for any device).
_OWN_BALANCING: Final[tuple[tuple[str, str | None], ...]] = (
    ("easee", "equalizer"),
    ("zaptec", "sense"),
    ("zaptec", "apm"),
    ("ferroamp", None),
    ("onep1", None),
    # Perific/Enegic's reporter balances a Zaptec (or Easee) installation through the vendor cloud, on
    # any of its models.
    ("perific", None),
)


def find_own_load_balancing(
    devices: list[RegistryDevice], platform_of_entry: Mapping[str, str]
) -> list[OwnBalancing]:
    """Devices of an integration known to balance load by itself (Easee Equalizer, Zaptec Sense,
    Ferroamp ACE, ONEp1). `platform_of_entry` maps a config entry id to its domain."""
    found: list[OwnBalancing] = []
    for device in devices:
        domains = {platform_of_entry.get(entry_id) for entry_id in device.config_entry_ids}
        label = f"{device.model or ''} {device.name or ''}".lower()
        for platform, needle in _OWN_BALANCING:
            if platform in domains and (needle is None or needle in label):
                found.append(OwnBalancing(platform, device.name or device.model or platform))
                break
    return found


def find_own_load_balancing_from_hass(hass: HomeAssistant) -> list[OwnBalancing]:
    _entities, devices = snapshot_registry(hass)
    platform_of_entry = {entry.entry_id: entry.domain for entry in hass.config_entries.async_entries()}
    return find_own_load_balancing(devices, platform_of_entry)


# ---- Update-interval knowledge -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdateBehaviour:
    """What the catalogue knows about how often an integration writes a meter value."""

    interval_s: float
    # The integration's own setting that shortens it, named as the user sees it; `None` when the
    # integration offers none.
    option: str | None = None
    on_change_only: bool = False


UPDATE_BEHAVIOUR: Final[dict[str, UpdateBehaviour]] = {
    "solaredge_modbus_multi": UpdateBehaviour(300.0, "Polling frequency (Modbus poll interval, 1-10 s)"),
    "solaredge_modbus": UpdateBehaviour(30.0, "Polling interval"),
    "dsmr": UpdateBehaviour(30.0, "Minimum time between updates"),
    "huawei_solar": UpdateBehaviour(30.0, None),
    "fronius": UpdateBehaviour(60.0, None),
    "enphase_envoy": UpdateBehaviour(60.0, None),
    "ferroamp": UpdateBehaviour(30.0, None),
    "solax_modbus": UpdateBehaviour(15.0, "Polling interval"),
    "solis_modbus": UpdateBehaviour(10.0, "Scan interval"),
    # `sungrow` is not listed: its local Modbus entries poll every 30 s but its iSolarCloud entries every
    # 300 s, and nothing in the registry tells the two apart.
    "tesla_custom": UpdateBehaviour(660.0, "Scan interval"),
    "tesla_fleet": UpdateBehaviour(60.0, None),
    "powerwall": UpdateBehaviour(30.0, None),
    "discovergy": UpdateBehaviour(30.0, None),
    "sma_ennexos": UpdateBehaviour(60.0, None),
    "growatt_server": UpdateBehaviour(300.0, None),
    "solaredge": UpdateBehaviour(900.0, None),
    "solis": UpdateBehaviour(300.0, None),
    "foxess": UpdateBehaviour(300.0, None),
    "edl21": UpdateBehaviour(0.0, None, on_change_only=True),
    "zha": UpdateBehaviour(900.0, None, on_change_only=True),
}


@dataclass(frozen=True, slots=True)
class FreshnessWarning:
    """One integration whose updates can be older than the site's maximum measurement age."""

    code: str
    integration: str
    interval_s: float | None
    option: str | None
    entity_id: str


def freshness_warnings(
    entity_platforms: Mapping[str, str], max_age_s: float
) -> list[FreshnessWarning]:
    """Warnings for the measurement entities (`entity id -> platform`) whose integration updates
    more slowly than `max_age_s`, or only on change. One per integration, naming the integration
    option that shortens the interval where the catalogue knows one."""
    warnings: list[FreshnessWarning] = []
    seen: set[str] = set()
    for entity_id, platform in sorted(entity_platforms.items()):
        behaviour = UPDATE_BEHAVIOUR.get(platform)
        if behaviour is None or platform in seen:
            continue
        if behaviour.on_change_only:
            seen.add(platform)
            warnings.append(FreshnessWarning(WARNING_ON_CHANGE_ONLY, platform, None, None, entity_id))
        elif behaviour.interval_s > max_age_s:
            seen.add(platform)
            warnings.append(
                FreshnessWarning(WARNING_SLOW_UPDATE, platform, behaviour.interval_s, behaviour.option, entity_id)
            )
    return warnings


# ---- Applying a confirmed candidate ------------------------------------------------------------


def apply_meter_candidate(data: Mapping[str, Any], candidate: MeterCandidate) -> dict[str, Any]:
    """`data` with the candidate's meter choices written: measurement mode, entities, source, total grid
    power and sign flags. Only those keys change; the inactive mode's stored entities are kept."""
    updated = dict(data)
    updated[CONF_MEASUREMENT_MODE] = candidate.mode
    updated[CONF_SITE_CURRENT_SIGNED] = candidate.signed_current
    updated[CONF_GRID_POWER_INVERTED] = candidate.power_inverted
    # The meter's total comes with the meter: a total left from the previous meter would be read as
    # this one's.
    if candidate.grid_power is not None:
        updated[CONF_GRID_POWER_SOURCE] = grid_power_source_to_dict(candidate.grid_power)
    else:
        updated.pop(CONF_GRID_POWER_SOURCE, None)
    if candidate.mode == MEASUREMENT_MODE_DERIVED:
        updated[CONF_DERIVED_ENTITIES] = {
            phase: dict(values) for phase, values in (candidate.derived_entities or {}).items()
        }
    else:
        if candidate.current_source is not None:
            updated[CONF_SITE_CURRENT_SOURCE] = source_to_dict(candidate.current_source)
            updated[CONF_DIRECT_ENTITIES] = {}
        else:
            updated[CONF_DIRECT_ENTITIES] = dict(candidate.direct_entities or {})
            updated[CONF_SITE_CURRENT_SOURCE] = None
    return updated


def apply_battery_candidate(data: Mapping[str, Any], candidate: BatteryCandidate) -> dict[str, Any]:
    updated = dict(data)
    updated[CONF_BATTERY_AGGREGATE_POWER_ENTITY] = candidate.entity_id
    updated[CONF_BATTERY_DISCHARGE_POWER_ENTITY] = candidate.discharge_entity_id or ""
    updated[CONF_BATTERY_POWER_INVERTED] = candidate.inverted
    return updated
