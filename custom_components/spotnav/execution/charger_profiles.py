"""What is known, per Home Assistant integration, about driving a charger from outside.

Pure data, read from each integration's own source: which
entities start and stop it, which one takes a current, which sensor says it is charging and with
which values, where its lifetime energy register and its measured current are, which of its own
modes fight an external controller, and the **write policy** that keeps SpotNav from harming it.

Entities are matched by platform plus `translation_key` or the tail of the `unique_id`, never by
entity id or friendly name (`config_flow/charger_detection.py`). An integration that is not listed is
still usable through the generic flow; it then gets `DEFAULT_POLICY`, the conservative one.

`WritePolicy` is what the adapter enforces (`execution/charger_adapter.py`):

* `min_interval_s`: the least time between two writes of the current (cloud APIs answer 429 or get
  abused; Zaptec asks for minutes);
* `max_writes_per_minute`: a sliding window on top of it (Easee: 20 setting changes a minute);
* `flash_stored`: the setting lives in flash (or a schedule/parameter store), so it is written at a
  session start at most and **never from the regulator loop**;
* `zero_pauses`: writing 0 A pauses the charge, so SpotNav never writes below the 6 A floor and uses
  its stop instead;
* `ignored_while_paused`: a write while the charger is paused only stores the value (Peblar) or, for
  Easee, a limit above 0 would resume it, so the current is written after the start, never before,
  and not while paused;
* `installation_wide`: the number caps every charger of an installation (Zaptec), so it is used only
  when the installation has a single charger;
* `resend_after_plug_in`: the charger forgets the limit on plug-in and reboot (Easee), so it is sent
  again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

#: What the platform is to SpotNav.
ROLE_CHARGER: Final = "charger"
#: Another controller owns the charger (evcc, openWB): warn, never pre-select.
ROLE_EXTERNAL_CONTROLLER: Final = "external_controller"
#: Not a charger at all.
ROLE_EXCLUDED: Final = "excluded"
#: A charger with nothing to command; usable only as a measurement and energy source.
ROLE_MEASUREMENT_ONLY: Final = "measurement_only"
#: A charger SpotNav cannot drive (read only, lock only).
ROLE_UNSUPPORTED: Final = "unsupported"

PATH_SWITCH: Final = "switch"
PATH_SELECT: Final = "select"
PATH_BUTTONS: Final = "buttons"
PATH_EASEE: Final = "easee"
PATH_KINDS: Final = (PATH_SWITCH, PATH_SELECT, PATH_BUTTONS, PATH_EASEE)


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """How often and when a charger's current may be written. See the module docstring."""

    min_interval_s: float = 0.0
    max_writes_per_minute: int | None = None
    flash_stored: bool = False
    zero_pauses: bool = False
    ignored_while_paused: bool = False
    installation_wide: bool = False
    resend_after_plug_in: bool = False

    @property
    def regulator_writes(self) -> bool:
        """Whether the regulator loop may write the current during a session."""
        return not self.flash_stored

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_interval_s": self.min_interval_s,
            "max_writes_per_minute": self.max_writes_per_minute,
            "flash_stored": self.flash_stored,
            "regulator_writes": self.regulator_writes,
            "zero_pauses": self.zero_pauses,
            "ignored_while_paused": self.ignored_while_paused,
            "installation_wide": self.installation_wide,
            "resend_after_plug_in": self.resend_after_plug_in,
        }


#: A platform nobody described: assume a cloud round trip, and that 0 A may pause it.
DEFAULT_POLICY: Final = WritePolicy(min_interval_s=90.0, zero_pauses=True)
#: OCPP's `ChangeConfiguration` path: no limit of its own, exactly as it has always been.
OCPP_POLICY: Final = WritePolicy()

#: An OCPP charger whose current is set through its session-limit number: like other local chargers,
#: and a zero does not pause it (the pilot floor is 6 A).
OCPP_NUMBER_POLICY: Final = WritePolicy(min_interval_s=10.0)

_LOCAL = WritePolicy(min_interval_s=10.0)
_LOCAL_ZERO_PAUSES = WritePolicy(min_interval_s=10.0, zero_pauses=True)
_CLOUD = WritePolicy(min_interval_s=60.0)


@dataclass(frozen=True, slots=True)
class OwnModeRule:
    """One of the charger's own controllers: active while the entity reads anything but one of
    `inactive_values` (compared lower-case; an unreadable state never counts as active).
    """

    domain: str
    keys: tuple[str, ...]
    inactive_values: tuple[str, ...]
    #: Short name shown to the person ("Eco-Smart", "load balancing").
    label: str


@dataclass(frozen=True, slots=True)
class StartStop:
    """How a platform starts and stops a charge."""

    kind: str
    #: switch: the entity keys; `inverted` when on means paused (V2C).
    keys: tuple[str, ...] = ()
    inverted: bool = False
    #: select: acceptable option names for start and stop, matched lower-case against the entity's
    #: `options`.
    start_options: tuple[str, ...] = ()
    stop_options: tuple[str, ...] = ()
    #: buttons: the entity keys of the start and the stop button.
    start_keys: tuple[str, ...] = ()
    stop_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """Everything one integration's detection and write policy needs."""

    platform: str
    name: str
    role: str = ROLE_CHARGER
    start_stop: StartStop | None = None
    #: The number that takes the current (`number.set_value`), or `None`: a current cannot be set
    #: through a number. Easee sets it through its own services instead (`easee_current`).
    current_keys: tuple[str, ...] = ()
    easee_current: bool = False
    policy: WritePolicy = DEFAULT_POLICY
    #: Lifetime (`total_increasing`) register keys, in order of preference, and per-session ones.
    energy_keys: tuple[str, ...] = ()
    session_energy_keys: tuple[str, ...] = ()
    #: The sensor that says what the charger is doing, the values meaning "charging" and the values
    #: meaning "a vehicle is connected but asks for no current" (the `SuspendedEV` of OCPP).
    status_keys: tuple[str, ...] = ()
    charging_values: tuple[str, ...] = ()
    vehicle_idle_values: tuple[str, ...] = ()
    #: Sensors measuring the current, per phase (A or mA).
    current_sensor_keys: tuple[str, ...] = ()
    own_modes: tuple[OwnModeRule, ...] = ()
    #: The integration puts the entity key first in the `unique_id` (`<key>-<serial>`).
    key_first: bool = False
    note: str = ""

    @property
    def can_set_current(self) -> bool:
        return bool(self.current_keys) or self.easee_current


def _switch(*keys: str, inverted: bool = False) -> StartStop:
    return StartStop(PATH_SWITCH, keys=keys, inverted=inverted)


def _select(keys: tuple[str, ...], start: tuple[str, ...], stop: tuple[str, ...]) -> StartStop:
    return StartStop(PATH_SELECT, keys=keys, start_options=start, stop_options=stop)


def _buttons(start: tuple[str, ...], stop: tuple[str, ...]) -> StartStop:
    return StartStop(PATH_BUTTONS, start_keys=start, stop_keys=stop)


def _rule(domain: str, keys: tuple[str, ...], inactive: tuple[str, ...], label: str) -> OwnModeRule:
    return OwnModeRule(domain, keys, inactive, label)


_PROFILES: Final[tuple[PlatformProfile, ...]] = (
    PlatformProfile(
        platform="easee",
        name="Easee",
        start_stop=StartStop(PATH_EASEE),
        easee_current=True,
        policy=WritePolicy(max_writes_per_minute=20, resend_after_plug_in=True, ignored_while_paused=True),
        energy_keys=("lifetime_energy",),
        session_energy_keys=("session_energy",),
        status_keys=("status", "easee_status"),
        charging_values=("charging",),
        vehicle_idle_values=("ready_to_charge", "completed"),
        current_sensor_keys=("current",),
        own_modes=(
            _rule("switch", ("smart_charging",), ("off",), "smart charging"),
        ),
        note="Start and stop (resume and pause) and the dynamic limit go through Easee's own services; "
        "the max-limit services are never used (flash).",
    ),
    PlatformProfile(
        platform="wallbox",
        name="Wallbox",
        start_stop=_switch("pause_resume"),
        current_keys=("maximum_charging_current",),
        policy=WritePolicy(min_interval_s=90.0),
        session_energy_keys=("added_energy",),
        status_keys=("status_description",),
        charging_values=("charging",),
        vehicle_idle_values=("waiting for car demand",),
        own_modes=(_rule("select", ("ecosmart", "eco_smart"), ("off",), "Eco-Smart"),),
        key_first=True,
        note="Cloud: at most one current write every 90 s.",
    ),
    PlatformProfile(
        platform="zaptec",
        name="Zaptec",
        start_stop=_switch("charger_operation_mode", "charging"),
        current_keys=("available_current",),
        policy=WritePolicy(min_interval_s=900.0, zero_pauses=True, installation_wide=True),
        energy_keys=("signed_meter_value_kwh", "signed_meter_value"),
        status_keys=("charger_operation_mode", "charger_mode"),
        charging_values=("connected_charging",),
        vehicle_idle_values=("connected_finished",),
        current_sensor_keys=("current_phase1", "current_phase2", "current_phase3"),
        note="The limit is installation-wide: used only for a single-charger installation, "
        "and at most every 15 minutes.",
    ),
    PlatformProfile(
        platform="goecharger_api2",
        name="go-e Charger (API v2)",
        start_stop=_select(("frc",), ("charge", "2"), ("don't charge", "dont_charge", "dont charge", "1")),
        current_keys=("amp",),
        policy=_LOCAL,
        energy_keys=("eto",),
        status_keys=("car", "car_value"),
        charging_values=("charging", "2"),
        current_sensor_keys=("nrg_4", "nrg_5", "nrg_6"),
        own_modes=(
            _rule("select", ("lmo",), ("3", "default", "default mode"), "charging mode"),
            _rule("switch", ("fup",), ("off",), "PV surplus"),
        ),
    ),
    PlatformProfile(
        platform="goecharger_mqtt",
        name="go-e Charger (MQTT)",
        start_stop=_select(("frc",), ("charge", "2"), ("dont_charge", "don't charge", "dont charge", "1")),
        current_keys=("amp",),
        policy=_LOCAL,
        energy_keys=("eto",),
        status_keys=("car",),
        charging_values=("charging", "2"),
        current_sensor_keys=("nrg_4", "nrg_5", "nrg_6"),
        own_modes=(
            _rule("select", ("lmo",), ("3", "default", "default mode"), "charging mode"),
            _rule("switch", ("fup",), ("off",), "PV surplus"),
        ),
    ),
    PlatformProfile(
        platform="goecharger",
        name="go-e Charger (legacy)",
        start_stop=_switch("allow_charging", "alw"),
        note="Current is only reachable through a service: start and stop only.",
    ),
    PlatformProfile(
        platform="wattpilot",
        name="Fronius Wattpilot",
        start_stop=_buttons(("frc2",), ("frc1",)),
        current_keys=("amp",),
        policy=_LOCAL,
        own_modes=(_rule("select", ("lmo",), ("3", "default", "default mode"), "charging mode"),),
    ),
    PlatformProfile(
        platform="peblar",
        name="Peblar",
        start_stop=_switch("charge"),
        current_keys=("charge_current_limit",),
        policy=WritePolicy(min_interval_s=10.0, zero_pauses=True, ignored_while_paused=True),
        energy_keys=("energy_total",),
        session_energy_keys=("energy_session",),
        status_keys=("cp_state",),
        charging_values=("charging",),
        current_sensor_keys=("current_phase_1", "current_phase_2", "current_phase_3"),
        own_modes=(_rule("select", ("smart_charging",), ("default",), "smart charging"),),
        note="The switch and the current share one register: the current is not written while paused.",
    ),
    PlatformProfile(
        platform="nrgkick",
        name="NRGkick",
        start_stop=_switch("charging_enabled"),
        current_keys=("current_set",),
        policy=WritePolicy(min_interval_s=30.0),
        energy_keys=("total_charged_energy",),
        session_energy_keys=("charged_energy",),
        status_keys=("status",),
        charging_values=("charging",),
        current_sensor_keys=("l1_current", "l2_current", "l3_current"),
    ),
    PlatformProfile(
        platform="v2c",
        name="V2C Trydan",
        start_stop=_switch("paused", inverted=True),
        current_keys=("intensity",),
        policy=_LOCAL,
        session_energy_keys=("charge_energy",),
        own_modes=(_rule("switch", ("dynamic",), ("off",), "dynamic mode"),),
        note="The switch means paused: on stops the charge.",
    ),
    PlatformProfile(
        platform="hypervolt_charger",
        name="Hypervolt",
        start_stop=_switch("charging"),
        current_keys=("max_current",),
        policy=_CLOUD,
    ),
    PlatformProfile(
        platform="chargeamps",
        name="Charge Amps",
        start_stop=_switch("enable"),
        current_keys=("max_current",),
        policy=_CLOUD,
        energy_keys=("total_energy",),
        status_keys=("status", "charge_point_status"),
        charging_values=("charging",),
        vehicle_idle_values=("suspendedev",),
        current_sensor_keys=("l1_current", "l2_current", "l3_current"),
    ),
    PlatformProfile(
        platform="lektrico",
        name="Lektrico",
        start_stop=_buttons(("charge_start",), ("charge_stop",)),
        current_keys=("dynamic_limit",),
        policy=_LOCAL_ZERO_PAUSES,
        energy_keys=("lifetime_energy",),
        session_energy_keys=("energy",),
        status_keys=("state",),
        charging_values=("charging",),
        current_sensor_keys=("current_l1", "current_l2", "current_l3"),
        own_modes=(_rule("select", ("load_balancing_mode",), ("disabled", "none", "off"), "load balancing"),),
    ),
    PlatformProfile(
        platform="openevse",
        name="OpenEVSE",
        start_stop=_select(("override_state",), ("active",), ("disabled",)),
        current_keys=("charge_rate",),
        policy=_LOCAL,
        energy_keys=("usage_total",),
        session_energy_keys=("usage_session",),
        status_keys=("state",),
        charging_values=("charging",),
        own_modes=(
            _rule("switch", ("solar_pv_divert", "divert"), ("off",), "solar divert"),
            _rule("switch", ("current_shaper",), ("off",), "current shaper"),
        ),
    ),
    PlatformProfile(
        platform="alfen_wallbox",
        name="Alfen (Wallbox)",
        current_keys=("main_normal_max_current_socket_1", "main_normal_max_current"),
        policy=WritePolicy(min_interval_s=60.0, flash_stored=True, zero_pauses=True),
        note="The current is a stored setting: written at a session start, never from the regulator.",
    ),
    PlatformProfile(
        platform="alfen_modbus",
        name="Alfen (Modbus)",
        start_stop=_switch("charger_enabled"),
        current_keys=("max_current_limit", "max_current_limit_s1"),
        policy=_LOCAL_ZERO_PAUSES,
    ),
    PlatformProfile(
        platform="heidelberg_energy_control",
        name="Heidelberg Energy Control",
        start_stop=_switch("virtual_enable"),
        current_keys=("virtual_current",),
        policy=_LOCAL_ZERO_PAUSES,
    ),
    PlatformProfile(
        platform="garo_wallbox",
        name="GARO",
        start_stop=_select(("mode",), ("always_on",), ("always_off",)),
        current_keys=("current_limit",),
        policy=WritePolicy(min_interval_s=60.0, flash_stored=True),
        session_energy_keys=("acc_energy",),
        note="The limit is stored with the charger's schedule: written at a session start only.",
    ),
    PlatformProfile(
        platform="smaev",
        name="SMA EV Charger",
        start_stop=_select(("operating_mode_of_charge_session",), ("boost",), ("off", "stop", "optimized")),
        current_keys=("charge_current_limit",),
        policy=WritePolicy(min_interval_s=60.0, flash_stored=True),
        note="The limit is a stored parameter: written at a session start only.",
    ),
    PlatformProfile(
        platform="smartevse",
        name="SmartEVSE",
        start_stop=_select(("mode",), ("normal",), ("off", "pause")),
        current_keys=("override_current",),
        policy=_LOCAL,
    ),
    PlatformProfile(
        platform="defa_power",
        name="DEFA Power",
        start_stop=_buttons(("start_charging",), ("stop_charging",)),
        current_keys=("ampere",),
        policy=_CLOUD,
        energy_keys=("meter_value",),
    ),
    PlatformProfile(
        platform="webasto_next_modbus",
        name="Webasto Next",
        start_stop=_buttons(("start_session",), ("stop_session",)),
        current_keys=("set_current_a",),
        policy=_LOCAL,
    ),
    PlatformProfile(
        platform="abb_terra_ac",
        name="ABB Terra AC",
        start_stop=_buttons(("start_charging",), ("stop_charging",)),
        current_keys=("current_limit",),
        policy=_LOCAL_ZERO_PAUSES,
    ),
    PlatformProfile(
        platform="myenergi",
        name="myenergi zappi",
        start_stop=_select(("charge_mode",), ("fast",), ("stopped",)),
        note="No current control: start and stop only.",
    ),
    PlatformProfile(
        platform="ohme",
        name="Ohme",
        start_stop=_select(("charge_mode",), ("max_charge",), ("paused",)),
        status_keys=("status",),
        own_modes=(),
        note="No current control; Ohme's own smart schedule may fight external control.",
    ),
    PlatformProfile(
        platform="monta",
        name="Monta",
        start_stop=_switch("start_stop", "charger"),
        energy_keys=("charger_lastmeterreadingkwh",),
        status_keys=("charger_state",),
        charging_values=("busy-charging",),
        vehicle_idle_values=("busy-non-charging",),
        note="No current control: start and stop only.",
    ),
    PlatformProfile(
        platform="pod_point",
        name="Pod Point",
        start_stop=_switch("charging_allowed"),
        note="No current control: start and stop only.",
    ),
    PlatformProfile(
        platform="chargepoint",
        name="ChargePoint",
        start_stop=_buttons(("start_charging",), ("stop_charging",)),
        note="Its current limit is a select: start and stop only.",
    ),
    PlatformProfile(
        platform="tesla_wall_connector",
        name="Tesla Wall Connector",
        role=ROLE_MEASUREMENT_ONLY,
        energy_keys=("energy_kwh",),
        current_sensor_keys=("current_a_a", "current_b_a", "current_c_a"),
        note="Read only: the car is the actuator.",
    ),
    PlatformProfile(platform="evcc_intg", name="evcc", role=ROLE_EXTERNAL_CONTROLLER),
    PlatformProfile(platform="openwb2mqtt", name="openWB", role=ROLE_EXTERNAL_CONTROLLER),
    PlatformProfile(platform="andersen_ev", name="Andersen", role=ROLE_UNSUPPORTED, note="Lock only."),
    PlatformProfile(platform="ctek_nanogrid_air", name="CTEK Nanogrid Air", role=ROLE_UNSUPPORTED),
    PlatformProfile(platform="elli_charger_ha", name="Elli", role=ROLE_UNSUPPORTED),
    PlatformProfile(platform="blue_current", name="Blue Current", role=ROLE_UNSUPPORTED, note="Stop only."),
    PlatformProfile(platform="webastoconnect", name="Webasto ThermoConnect", role=ROLE_EXCLUDED),
    PlatformProfile(platform="webel_gctrl", name="GARO G-CTRL", role=ROLE_EXCLUDED),
    PlatformProfile(platform="juicenet", name="JuiceNet", role=ROLE_EXCLUDED),
    PlatformProfile(platform="smappee", name="Smappee", role=ROLE_EXCLUDED),
)

PROFILES: Final[dict[str, PlatformProfile]] = {profile.platform: profile for profile in _PROFILES}

def detectable_platforms() -> tuple[str, ...]:
    """Platforms the detected flow offers a device of: everything that is not excluded."""
    return tuple(p.platform for p in _PROFILES if p.role != ROLE_EXCLUDED)


def profile_for(platform: str | None) -> PlatformProfile | None:
    return PROFILES.get(platform or "")


def policy_for(platform: str | None) -> WritePolicy:
    """The write policy of a platform; `DEFAULT_POLICY` for one nobody described."""
    profile = profile_for(platform)
    return profile.policy if profile is not None else DEFAULT_POLICY


def entity_matches_keys(
    *,
    translation_key: str | None,
    unique_id: str | None,
    keys: tuple[str, ...],
    key_first: bool = False,
) -> bool:
    """Whether a registry entry is one of `keys`: by `translation_key`, else by the tail of its
    `unique_id` (`<serial>_<key>`, `<serial>-<key>`, `<platform>.<...>_<key>`), or its head for an
    integration that puts the key first (`<key>-<serial>`). Never the entity id.
    """
    if not keys:
        return False
    wanted = tuple(key.lower() for key in keys)
    if translation_key and translation_key.lower() in wanted:
        return True
    if not unique_id:
        return False
    uid = unique_id.lower()
    for key in wanted:
        if uid == key or uid.endswith((f"_{key}", f"-{key}", f".{key}", f":{key}")):
            return True
        if key_first and uid.startswith((f"{key}-", f"{key}_")):
            return True
    return False

