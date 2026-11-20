"""Per-brand heat pump adapters: where each integration keeps the pump's readings and which controls it has.

Read only. Nothing here or in `facts.py` writes to a heat pump; the controls are listed so the diagnostics show what
a pump offers (plan `plans/v3_heating_plan.md` section 4). The heat control (`heat/control.py`, behind the person's
opt-in) steers through the SG Ready API and the curve offsets among them, as `heat/paths.py` says.

Every entity is found by its integration (the registry's platform) and the key at the fixed place of its unique id,
never by its entity id or name, which a person may change. A pump is found by its anchor (`Adapter.anchor`, the
outdoor temperature every supported pump reads); its other entities are looked for in the anchor's config entry,
the anchor's own device first.

Sources, read 2026-11-15 (background: `plans/research_integrations/heat_pump_survey_2026-11-15.md`):

* NIBE local, core `nibe_heatpump` (core 5aea6a2, library nibe 2.26.0, `nibe/data/*.json`): unique id
  `<entry unique id or entry id>-<coil address>`; climate systems `-s1`. A coil becomes a `sensor`, `binary_sensor`,
  `number`, `select` or `switch` by its write flag and mappings. **Every coil entity is disabled by default**: what
  the person has not enabled reads as `disabled`, never as a value. The F series (F730, F750, F1155, SMO 40,
  VVM 225/320/325, ...) and the S series (S1155, S320, VVM S, ...) number their registers apart, and the same
  address can mean different things (40012: return temperature on F, degree minutes on S), so each series is an
  adapter of its own, told apart by the anchor (F: 40004 outdoor BT1, S: 30002 outdoor BT1).
* NIBE cloud, core `myuplink` (myuplink 0.7.0, the core test fixtures for an F730 and an SMO 20): unique id
  `<device id>-<parameter id>`; the F series' parameter ids are its register addresses. Enum sensors show their
  text ("Heating", "Hot water"). An S series pump through myUplink is not mapped (its parameter ids are not in any
  source read); it is found by the F anchor only if it has a parameter 40004.
* CTC, `ctc_bms` (dklemm/home-assistant-ctc-bms 0.6.0, `582dff8`): unique id `<entry id>_<register>` for the BMS
  map (62000 block read, 61500 block stored settings, shown only with the integration's setpoints option) and
  `<entry id>_<control>` for the volatile 1000 block (forgotten by the controller five minutes after the last
  write; 1100 is eight virtual digital inputs `_1100_di<n>`, SmartGrid among the functions they can be given).
* CTC, `ctc_ecozenith` (beolink/HA-CTC-Local-Easyconf 0.30.0, `8d2f348`): unique id
  `ctc_ecozenith_<device key>_<key>` with named keys; its volatile controls start `ctl_`. The device key can hold
  underscores, so a key is matched at the end and a longer key that ends the same is rejected (`hp1_outdoor_temp`).
* Panasonic Aquarea through HeishaMon, `aquarea` (kamaradclimber/heishamon-homeassistant 2.14.0, `6c91321`; topics
  from heishamon/HeishaMon `MQTT-Topics.md`): unique id `<entry id>-<topic id>`, `TOP<n>` for readings, `SET<n>`
  or a command name for the writable ones. The HeishaMon YAML package has no unique ids and cannot be read so.
* Thermia Genesis, `thermiagenesis` (CJNE/thermiagenesis 0.0.12, library pythermiagenesis 0.1.8; Thermia's "Modbus
  protocol for Genesis platform" v10.0, read 2026-11-20; plan `plans/v3_thermia_genesis.md`): unique id
  `thermiagenesis_<register key>` with no entry id, one pump per Home Assistant. Every number and sensor is disabled by
  default, and the integration polls only the registers of enabled entities, every 30 s. Every holding register is
  treated as a stored setting (the manual says nothing of flash wear), and the only single-value shift of the heat
  curve is the comfort wheel (holding 5). The demand in progress is a number (`PRIORITY_CODES_THERMIA`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Literal

from ..battery.adapters import Match

#: The readings an adapter may know where to find.
FactRole = Literal[
    "outdoor_temp",
    "supply_temp",
    # The supply temperature the pump's heat curve asks for now: it falls at once when a lowering input closes, so
    # the heat control's check reads it where the pump reports it (`heat/relay_control.py`).
    "calc_supply_temp",
    "return_temp",
    "room_temp",
    "hot_water_temp",
    "degree_minutes",
    "compressor_speed",
    "compressor_state",
    "immersion_power",
    "immersion_state",
    "priority",
]

#: The kinds of control a pump may offer (plan section 4): a heat-curve offset, a room or flow setpoint, SG Ready
#: written through the integration, a hot water boost, something that blocks or caps the compressor, and the
#: pump's own price mode.
CapabilityKind = Literal["offset", "setpoint", "sg_ready", "hot_water_boost", "blocking", "price_mode"]

#: How a price-control entity tells that the pump runs its own price control: `on` (a switch that is on),
#: `level` (a price level the pump was given: 1-3 or low/normal/high; 0 or unknown is none), `nonzero` (a price
#: offset or a spot price that is not 0), `not_normal` (a SmartGrid mode other than normal).
PriceRuleKind = Literal["on", "level", "nonzero", "not_normal"]


@dataclass(frozen=True, slots=True)
class Capability:
    """A control path a pump has. `volatile`: the pump forgets a written value by itself (CTC's 1000 block);
    `stored`: a stored setting (write rarely).

    A hot-water boost's recipe (the Boost button and the planned boost, `heat/dhw_boost.py`): `self_ending` when a
    start ends by itself (a one-time run, a timer), `duration` the timer's length where it has one (`None`: a
    one-time run, its length learned), `start` and `stop` what is written: a select's option (matched by these
    words, any case) or a number's value; a switch is turned on and off and needs neither. `stop` `None` on a select
    or number: no way to end it early. `verified`: that a start ends by itself is confirmed on a real pump; an
    unverified boost is offered to the Boost button only, and SpotNav stops it after its expected run."""

    kind: CapabilityKind
    match: Match
    note: str = ""
    volatile: bool = False
    self_ending: bool = False
    duration: timedelta | None = None
    start: str | None = None
    stop: str | None = None
    verified: bool = True


@dataclass(frozen=True, slots=True)
class PriceRule:
    """An entity that shows the pump's own price control, and how it is read."""

    match: Match
    kind: PriceRuleKind
    label: str


@dataclass(frozen=True, slots=True)
class Adapter:
    brand: str
    label: str
    platforms: tuple[str, ...]
    #: The entity that marks one pump of this adapter (its outdoor temperature).
    anchor: Match
    #: Each role's entities in order of preference: the first that reads counts; for `immersion_power` all that
    #: read are summed (CTC's upper and lower heaters).
    facts: Mapping[FactRole, tuple[Match, ...]] = field(default_factory=dict)
    capabilities: tuple[Capability, ...] = ()
    #: The pump's own price control (NIBE Smart Price Adaption, CTC's price mode / SmartGrid), empty when it has
    #: none SpotNav knows of.
    price_control: tuple[PriceRule, ...] = ()
    #: Read through the vendor's cloud (a slow, rate-limited path: read only, plan section 4).
    cloud: bool = False
    #: Where the pump states what it serves as a number (Thermia's demand in progress): the number, as text, to the
    #: priority it means (`facts.Priority`); a number not listed reads as none.
    priority_codes: Mapping[str, str] = field(default_factory=dict)


def _dash(key: str, *domains: str) -> Match:
    """`<prefix>-<key>` (nibe_heatpump, myuplink, aquarea); a unique id is matched lower-cased."""
    return Match(domains or ("sensor",), re.compile(rf"-{re.escape(key.lower())}$"))


def _under(key: str, *domains: str, reject: str | None = None) -> Match:
    """`<prefix>_<key>` (the CTC integrations)."""
    return Match(
        domains or ("sensor",), re.compile(rf"_{re.escape(key.lower())}$"), None if reject is None else re.compile(reject)
    )


#: NIBE's degree minutes are a writable coil: a `number`, and a `sensor` too where the coil names a nature.
_DM_DOMAINS: Final = ("sensor", "number")

NIBE_F: Final = Adapter(
    brand="nibe_f",
    label="NIBE F series (local)",
    platforms=("nibe_heatpump",),
    anchor=_dash("40004"),
    facts={
        "outdoor_temp": (_dash("40004"),),
        # BT2 supply S1 on a pump with its own circuit, BT25 external supply on an SMO (which has no 40008).
        "supply_temp": (_dash("40008"), _dash("40071")),
        "calc_supply_temp": (_dash("43009"),),
        "return_temp": (_dash("40012"),),
        "room_temp": (_dash("40033"),),
        "hot_water_temp": (_dash("40013"),),
        # 16 bit first (the Modbus one EffektGuard reads), then 32 bit.
        "degree_minutes": (_dash("43005", *_DM_DOMAINS), _dash("40940", *_DM_DOMAINS)),
        "compressor_speed": (_dash("43136"),),
        "compressor_state": (_dash("43427"),),
        "immersion_power": (_dash("43084"),),
        "priority": (_dash("43086"),),
    },
    capabilities=(
        Capability("offset", _dash("47011", "number"), "Heat Offset S1, -10..10 in whole steps; a stored setting."),
        Capability("setpoint", _dash("s1", "climate"), "Climate system 1; acts only with the room sensor in use."),
        Capability("setpoint", _dash("47398", "number"), "Room sensor setpoint S1; a stored setting."),
        Capability(
            "hot_water_boost",
            _dash("48132", "select"),
            "Temporary lux: off, 3 h, 6 h, 12 h, one time.",
            self_ending=True,
            start="one time",
            stop="off",
        ),
    ),
    price_control=(
        PriceRule(_dash("41929"), "level", "Smart Price Adaption price level"),
        PriceRule(_dash("44896"), "nonzero", "Smart Price Adaption heating offset"),
    ),
)

NIBE_S: Final = Adapter(
    brand="nibe_s",
    label="NIBE S series (local)",
    platforms=("nibe_heatpump",),
    anchor=_dash("30002"),
    facts={
        "outdoor_temp": (_dash("30002"),),
        "supply_temp": (_dash("30006"), _dash("30040")),
        # Calculated supply climate system 1 (`s1155_s1255.json`): what the heat control's check reads.
        "calc_supply_temp": (_dash("31018"),),
        "return_temp": (_dash("30008"),),
        "room_temp": (_dash("30027"),),
        "hot_water_temp": (_dash("30009"),),
        "degree_minutes": (_dash("40019", *_DM_DOMAINS), _dash("40012", *_DM_DOMAINS)),
        "compressor_speed": (_dash("31047"),),
        "immersion_power": (_dash("31028"),),
        "priority": (_dash("31029"),),
    },
    capabilities=(
        Capability("offset", _dash("40031", "number"), "Heating offset climate system 1, -10..10; a stored setting."),
        Capability("setpoint", _dash("s1", "climate"), "Climate system 1; acts only with the room sensor in use."),
        Capability("setpoint", _dash("40207", "number"), "Room sensor set point climate system 1; stored."),
        Capability("sg_ready", _dash("43033", "switch"), "Activate SG Ready via API."),
        Capability("sg_ready", _dash("46009", "number"), "Requested operating mode (SG Ready), 0..3."),
        Capability("hot_water_boost", _dash("40698", "number"), "More hot water (its values are not documented)."),
    ),
    price_control=(PriceRule(_dash("40844", "switch"), "on", "Smart Price Adaption activated"),),
)

MYUPLINK: Final = Adapter(
    brand="myuplink",
    label="NIBE myUplink (cloud)",
    platforms=("myuplink",),
    anchor=_dash("40004"),
    cloud=True,
    facts={
        "outdoor_temp": (_dash("40004"),),
        "supply_temp": (_dash("40008"), _dash("40071")),
        "calc_supply_temp": (_dash("43009"),),
        "return_temp": (_dash("40012"), _dash("40152")),
        "room_temp": (_dash("40033"),),
        "hot_water_temp": (_dash("40013"), _dash("40014")),
        "degree_minutes": (_dash("40940", *_DM_DOMAINS),),
        # Not in either fixture; F-series parameter ids are the register addresses, so these are expected there.
        "compressor_speed": (_dash("43136"),),
        "immersion_power": (_dash("43084"),),
        "immersion_state": (_dash("49993"),),
        "priority": (_dash("49994"),),
    },
    capabilities=(
        Capability("offset", _dash("47011", "number"), "Heating offset climate system 1, -10..10; through the cloud."),
        Capability("setpoint", _dash("47398", "number"), "Room sensor set point climate system 1; through the cloud."),
        Capability("hot_water_boost", _dash("50004", "switch"), "Temporary lux (one run).", self_ending=True),
        Capability(
            "hot_water_boost",
            _dash("48132", "select"),
            "Temporary lux with a duration (SMO 20).",
            self_ending=True,
            start="one time",
            stop="off",
        ),
    ),
    price_control=(
        PriceRule(_dash("41929"), "level", "Smart Price Adaption mode"),
        PriceRule(_dash("44896"), "nonzero", "Smart Price Adaption heating offset"),
    ),
)

CTC_BMS: Final = Adapter(
    brand="ctc_bms",
    label="CTC (BMS Modbus)",
    platforms=("ctc_bms",),
    anchor=_under("62000"),
    facts={
        "outdoor_temp": (_under("62000"),),
        # Heating system 1's primary flow and room temperature (systems 2-4 follow at +1).
        "supply_temp": (_under("62011"),),
        # Heating system 1's primary flow setpoint (`registers.py` "Temp", the stored shape of control 1029): what
        # the curve asks for now.
        "calc_supply_temp": (_under("62007"),),
        "return_temp": (_under("62015"),),
        "room_temp": (_under("62203"),),
        # The upper tank first: 62003 reads a constant 0 on some units (shown as unknown there).
        "hot_water_temp": (_under("62276"), _under("62003")),
        "degree_minutes": (_under("62167"),),
        # Heat pump 1 (A1): its speed (rps) and its status.
        "compressor_speed": (_under("62193"),),
        "compressor_state": (_under("62017"),),
        # Upper and lower immersion heater, summed; both disabled by default in the integration.
        "immersion_power": (_under("62168"), _under("62169")),
        "priority": (_under("62017"),),
    },
    capabilities=(
        Capability("offset", _under("1023", "number"), "Curve adjustment override, -10..10 °C.", volatile=True),
        Capability("setpoint", _under("1010", "number"), "Room temperature setpoint override.", volatile=True),
        Capability("setpoint", _under("61509", "number"), "Room setpoint (stored, EEPROM: limited write cycles)."),
        Capability(
            "sg_ready",
            Match(("switch",), re.compile(r"_1100_di[0-7]$")),
            "Virtual digital inputs; SmartGrid A/B only where the controller's menu assigns them.",
            volatile=True,
        ),
        Capability(
            "hot_water_boost",
            _under("1006", "number"),
            "Extra hot water timer override (h).",
            volatile=True,
            self_ending=True,
            duration=timedelta(hours=1),
            start="1",
            stop="0",
        ),
        Capability("blocking", _under("1002", "number"), "Max compressor speed override (rps).", volatile=True),
        Capability("blocking", _under("61521", "switch"), "Heat pump 1 blocked (stored, EEPROM)."),
        Capability("price_mode", _under("1005", "select"), "Electricity price mode override.", volatile=True),
    ),
    price_control=(
        PriceRule(_under("62301"), "not_normal", "SmartGrid mode"),
        PriceRule(_under("62302"), "nonzero", "Spot price received"),
    ),
)

#: A longer key that ends in the same words is another reading (`hp1_outdoor_temp` is the outdoor unit's sensor).
_CTC_LOCAL_REJECT: Final = r"_hp\d+_outdoor_temp$"

CTC_LOCAL: Final = Adapter(
    brand="ctc_local",
    label="CTC (Local Easyconf)",
    platforms=("ctc_ecozenith",),
    anchor=_under("outdoor_temp", reject=_CTC_LOCAL_REJECT),
    facts={
        "outdoor_temp": (_under("outdoor_temp", reject=_CTC_LOCAL_REJECT),),
        "supply_temp": (_under("hs1_flow"),),
        "return_temp": (_under("return_temp"),),
        "room_temp": (_under("room_temp_1"),),
        "hot_water_temp": (_under("dhw_temp"), _under("dhw_temp_raw")),
        "degree_minutes": (_under("degree_minutes"),),
        "compressor_speed": (_under("hp1_rps"),),
        "compressor_state": (_under("hp1_status"),),
        "immersion_power": (_under("immersion_upper_kw"), _under("immersion_lower_kw")),
        "priority": (_under("hp1_status"),),
    },
    capabilities=(
        Capability("setpoint", _under("ctl_room_setpoint_1", "number"), "Room setpoint override.", volatile=True),
        Capability(
            "hot_water_boost",
            _under("ctl_extra_dhw", "number"),
            "Extra hot water (h).",
            volatile=True,
            self_ending=True,
            duration=timedelta(hours=1),
            start="1",
            stop="0",
        ),
        Capability("blocking", _under("ctl_max_rps", "number"), "Max compressor speed override (rps).", volatile=True),
        Capability("price_mode", _under("ctl_price_mode", "select"), "Electricity price mode override.", volatile=True),
    ),
    price_control=(PriceRule(_under("sg_mode"), "not_normal", "SmartGrid mode"),),
)

HEISHAMON: Final = Adapter(
    brand="heishamon",
    label="Panasonic Aquarea (HeishaMon)",
    platforms=("aquarea",),
    anchor=_dash("TOP14"),
    facts={
        "outdoor_temp": (_dash("TOP14"),),
        "supply_temp": (_dash("TOP6"),),
        "return_temp": (_dash("TOP5"),),
        "hot_water_temp": (_dash("TOP10"),),
        "compressor_speed": (_dash("TOP8"),),
        "immersion_state": (_dash("TOP60", "binary_sensor"),),
        # The 3-way valve: Room (heating) or Tank (hot water).
        "priority": (_dash("TOP20"),),
    },
    capabilities=(
        Capability(
            "offset",
            _dash("SET5", "number"),
            "Zone 1 heat request: a -5..5 shift only with the compensation curve; a direct temperature otherwise.",
        ),
        Capability("setpoint", _dash("1", "climate"), "Zone 1 climate."),
        # Whether Force DHW ends by itself once the tank is full is not verified on a real pump.
        Capability(
            "hot_water_boost",
            _dash("SET10", "switch"),
            "Force DHW (ending by itself unverified).",
            self_ending=True,
            verified=False,
        ),
        Capability("sg_ready", _dash("SetSmartGridMode", "select"), "Needs HeishaMon's optional PCB."),
    ),
)

#: Thermia's "first prioritised demand" (input register 1): 1 manual operation, 2 defrost, 3 hot water, 4 heat,
#: 5 cool, 6 pool, 7 anti-legionella, 98 standby, 99 no demand, 100 off. Manual operation and defrost name no
#: priority SpotNav knows and read as none.
PRIORITY_CODES_THERMIA: Final = {
    "3": "hot_water",
    "4": "heating",
    "5": "cooling",
    "6": "pool",
    "7": "hot_water",
    "98": "off",
    "99": "off",
    "100": "off",
}

THERMIA_GENESIS: Final = Adapter(
    brand="thermia_genesis",
    label="Thermia Genesis (Modbus)",
    platforms=("thermiagenesis",),
    anchor=_under("input_outdoor_temperature"),
    facts={
        "outdoor_temp": (_under("input_outdoor_temperature"),),
        "supply_temp": (_under("input_system_supply_line_temperature"),),
        # The supply temperature the curve asks for now (input 18): the check's proof that the comfort wheel moves
        # the curve.
        "calc_supply_temp": (_under("input_system_supply_line_calculated_set_point"),),
        # The pump's own return (condenser in); the base map has no system return line.
        "return_temp": (_under("input_condenser_in_temperature"),),
        "room_temp": (_under("input_room_temperature_sensor"),),
        "hot_water_temp": (_under("input_tap_water_weighted_temperature"), _under("input_tap_water_top_temperature")),
        # In per cent of the compressor's range (the rpm register is outside the role's plausible band).
        "compressor_speed": (_under("input_compressor_speed_percent"),),
        "priority": (_under("input_first_prioritised_demand"),),
    },
    capabilities=(
        Capability(
            "offset",
            _under("holding_comfort_wheel_setting", "number"),
            "Comfort wheel, whole steps; a stored setting (wear unknown).",
        ),
    ),
    priority_codes=PRIORITY_CODES_THERMIA,
)

ADAPTERS: Final[tuple[Adapter, ...]] = (NIBE_F, NIBE_S, MYUPLINK, CTC_BMS, CTC_LOCAL, HEISHAMON, THERMIA_GENESIS)

#: The integrations SpotNav reads heat pumps from.
PLATFORMS: Final = frozenset(platform for adapter in ADAPTERS for platform in adapter.platforms)


def adapters_for(platform: str) -> tuple[Adapter, ...]:
    """The adapters of an integration, in the order they are tried."""
    return tuple(adapter for adapter in ADAPTERS if platform in adapter.platforms)


__all__ = [
    "ADAPTERS",
    "PLATFORMS",
    "Adapter",
    "Capability",
    "CapabilityKind",
    "FactRole",
    "Match",
    "PriceRule",
    "adapters_for",
]
