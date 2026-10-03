"""What is known, per Home Assistant integration, about driving a charger from outside.

Pure data, read from each integration's own source: which
entities start and stop it, which one takes a current, which sensor says it is charging and with
which values, where its lifetime energy register and its measured current are, which of its own
modes fight an external controller, and the **write policy** that keeps SpotNav from harming it.

Entities are matched by platform plus `translation_key` or the tail of the `unique_id`, never by
entity id or friendly name (`config_flow/charger_detection.py`). An integration that is not listed is
still usable through the generic flow; it then gets `DEFAULT_POLICY`, the conservative one.

`WritePolicy` is what the adapter enforces (`execution/chargers/`):

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
  again;
* `state_is_not_setpoint`: the number's state is not what the charger applies (OpenEVSE reads its stored
  soft maximum while a write is a per-session claim), so "the number already shows the target" never
  skips a write, and the setpoint is what was last written;
* `max_pauses_per_10min`: the vendor's relay-protection budget for pausing (Peblar: three in a rolling
  ten minutes); a stop beyond it lowers the current to the floor instead;
* `session_bound`: the number exists only for a running session (OCPP's session limit): it is
  unavailable before the transaction and `unknown` after it starts until the first write, so `unknown`
  is writable and a write that found it unavailable is retried once the session is there.
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
#: A select whose Start puts back the mode the person had before SpotNav's Stop (SmartEVSE).
PATH_SELECT_RESTORE: Final = "select_restore"
#: A charge is paused by writing 0 A to the current number, and started by writing the current (ABB).
PATH_NUMBER_PAUSE: Final = "number_pause"
#: Buttons whose Start needs the register toggled 0 then 1 (Webasto Next).
PATH_BUTTONS_TOGGLE: Final = "buttons_toggle"
#: A select whose Start first presses the integration's approve button while the charger waits for
#: an approval (Ohme).
PATH_SELECT_APPROVE: Final = "select_approve"
#: A switch whose pauses are counted against the vendor's relay-protection budget (Peblar).
PATH_SWITCH_BUDGET: Final = "switch_budget"
PATH_KINDS: Final = (
    PATH_SWITCH,
    PATH_SELECT,
    PATH_BUTTONS,
    PATH_EASEE,
    PATH_SELECT_RESTORE,
    PATH_NUMBER_PAUSE,
    PATH_BUTTONS_TOGGLE,
    PATH_SELECT_APPROVE,
    PATH_SWITCH_BUDGET,
)


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
    session_bound: bool = False
    state_is_not_setpoint: bool = False
    max_pauses_per_10min: int | None = None
    #: How long one service call to the charger may take before it is given up on (a cloud
    #: round trip is allowed longer).
    call_timeout_s: float = 30.0

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
OCPP_NUMBER_POLICY: Final = WritePolicy(min_interval_s=10.0, session_bound=True)

_LOCAL = WritePolicy(min_interval_s=10.0)
_LOCAL_ZERO_PAUSES = WritePolicy(min_interval_s=10.0, zero_pauses=True)
_CLOUD = WritePolicy(min_interval_s=60.0, call_timeout_s=45.0)


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
    #: Entities whose `unique_id` contains any of these are other entities that end in the same key
    #: (Hypervolt's per-schedule-session charge modes) and are not this rule's.
    exclude: tuple[str, ...] = ()


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
    #: The state classes a lifetime register named in `energy_keys` may have. Only `total_increasing`
    #: is accepted for one that is merely found on the device; a register the profile names itself may
    #: be `total` (DEFA's meter reading), which is still a lifetime counter.
    energy_state_classes: tuple[str, ...] = ("total_increasing",)
    #: The sensor that says what the charger is doing, the values meaning "charging" and the values
    #: meaning "a vehicle is connected but asks for no current" (the `SuspendedEV` of OCPP).
    status_keys: tuple[str, ...] = ()
    charging_values: tuple[str, ...] = ()
    vehicle_idle_values: tuple[str, ...] = ()
    #: Status values meaning no vehicle is connected, so a move out of one is a plug-in (the point after
    #: which a charger that forgets its limit is told it again, `WritePolicy.resend_after_plug_in`).
    disconnected_values: tuple[str, ...] = ()
    #: Sensors measuring the current, per phase (A or mA).
    current_sensor_keys: tuple[str, ...] = ()
    #: One sensor that carries the three phase currents as attributes (Easee's `current`): its entity
    #: key, the attribute names of L1, L2 and L3 on a TN network, and on an IT network (230 V between
    #: phases) when the terminals map differently. The unit of those attributes is amperes.
    current_attribute_sensor_key: str = ""
    current_attributes: tuple[str, ...] = ()
    current_attributes_it: tuple[str, ...] = ()
    own_modes: tuple[OwnModeRule, ...] = ()
    #: The charger's own "enabled" switch when SpotNav does not use it as its start/stop control (Easee's
    #: `is_enabled`: a stored setting that is never written). While it reads one of
    #: `enable_switch_off_values` the charger cannot start, whatever SpotNav sends.
    enable_switch_keys: tuple[str, ...] = ()
    enable_switch_off_values: tuple[str, ...] = ("off",)
    #: The lowest current a charge is started (and re-sent after a plug-in) at, when that is above the
    #: 6 A floor the regulator may still go down to while it runs. `None`: no such minimum.
    min_start_current_a: float | None = None
    #: Statuses in which the charger's own scheduler or load balancer holds the charge: a Start was
    #: taken, and nothing SpotNav sends will release it.
    held_values: tuple[str, ...] = ()
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


def _rule(
    domain: str,
    keys: tuple[str, ...],
    inactive: tuple[str, ...],
    label: str,
    exclude: tuple[str, ...] = (),
) -> OwnModeRule:
    return OwnModeRule(domain, keys, inactive, label, exclude)


_PROFILES: Final[tuple[PlatformProfile, ...]] = (
    PlatformProfile(
        platform="easee",
        name="Easee",
        start_stop=StartStop(PATH_EASEE),
        easee_current=True,
        policy=WritePolicy(
            max_writes_per_minute=20, resend_after_plug_in=True, ignored_while_paused=True, call_timeout_s=45.0
        ),
        energy_keys=("lifetime_energy",),
        session_energy_keys=("session_energy",),
        status_keys=("status", "easee_status"),
        charging_values=("charging",),
        vehicle_idle_values=("ready_to_charge", "completed"),
        disconnected_values=("disconnected",),
        current_sensor_keys=("current",),
        # Easee reports the input terminals, not phases: T2 is the neutral on a TN network and T3, T4,
        # T5 are L1, L2, L3 (evcc reads them so); on an IT network there is no neutral and T2, T3, T4
        # are L1, L2, L3 (Easee's output phase configurations, e.g. P2_T2_T3_T4_IT = L1+L2, L2+L3).
        current_attribute_sensor_key="current",
        current_attributes=("state_inCurrentT3", "state_inCurrentT4", "state_inCurrentT5"),
        current_attributes_it=("state_inCurrentT2", "state_inCurrentT3", "state_inCurrentT4"),
        own_modes=(
            _rule("switch", ("smart_charging",), ("off",), "smart charging"),
        ),
        enable_switch_keys=("is_enabled",),
        min_start_current_a=7.0,
        held_values=(
            "awaiting_scheduled_start",
            "awaiting_smart_start",
            "awaiting_load_balancing",
            "paused_due_to_equalizer",
        ),
        note="Start and stop (resume and pause) and the dynamic limit go through Easee's own services; "
        "the max-limit services are never used (flash). A charge starts at 7 A or more, since the "
        "firmware delays a 6 A start by about five minutes. Enable the disabled \"Dynamic charger limit\" "
        "sensor for confirmed writes, and the disabled \"Current\" sensor: its terminal attributes are the "
        "charger's measured current per phase, which the site reads for the regulator and solar (T3, T4, T5 "
        "are L1, L2, L3 on a TN network; T2, T3, T4 on an IT network).",
    ),
    PlatformProfile(
        platform="wallbox",
        name="Wallbox",
        start_stop=_switch("pause_resume"),
        current_keys=("maximum_charging_current",),
        policy=WritePolicy(min_interval_s=90.0, call_timeout_s=45.0),
        session_energy_keys=("added_energy",),
        status_keys=("status_description",),
        charging_values=("charging",),
        vehicle_idle_values=("waiting for car demand",),
        disconnected_values=("disconnected",),
        own_modes=(_rule("select", ("ecosmart", "eco_smart"), ("off", "disabled"), "Eco-Smart"),),
        held_values=(
            "waiting in queue by power sharing",
            "waiting in queue by power boost",
            "waiting in queue by eco-smart",
        ),
        key_first=True,
        note="Cloud: at most one current write every 90 s. Pause and resume end the charger's own schedule "
        "until its Resume schedule button is pressed; a charger in Ready cannot be started from Home "
        "Assistant. The integration has no entity for Power Boost, Power Sharing or the app's schedule, so "
        "only Eco-Smart is shown as a conflict, and a charge held in a Power Sharing, Power Boost or "
        "Eco-Smart queue is reported as held by the charger.",
    ),
    PlatformProfile(
        platform="zaptec",
        name="Zaptec",
        start_stop=_switch("charger_operation_mode", "charging"),
        current_keys=("available_current",),
        policy=WritePolicy(
            min_interval_s=900.0, zero_pauses=True, installation_wide=True, call_timeout_s=45.0
        ),
        energy_keys=("signed_meter_value_kwh", "signed_meter_value"),
        status_keys=("charger_operation_mode", "charger_mode"),
        charging_values=("connected_charging",),
        vehicle_idle_values=("connected_finished",),
        disconnected_values=("disconnected",),
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
        charging_values=("charging", "2", "laden"),
        current_sensor_keys=("nrg_4", "nrg_5", "nrg_6"),
        own_modes=(
            _rule("select", ("lmo",), ("3", "default", "default mode"), "charging mode"),
            _rule("switch", ("fup",), ("off",), "PV surplus"),
        ),
        note="The raw `car` sensor is disabled by default; the enabled `car_value` sensor reports the "
        "state in Home Assistant's language, so the German word is accepted as well. The charger "
        "returns the force state to neutral on unplug.",
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
        status_keys=("car_status",),
        charging_values=("charging",),
        vehicle_idle_values=("waiting for vehicle",),
        note="Current is only reachable through a service: start and stop only. The switch means "
        "charging is allowed, so the status sensor decides whether it is charging.",
    ),
    PlatformProfile(
        platform="wattpilot",
        name="Fronius Wattpilot",
        start_stop=_buttons(("frc2",), ("frc1",)),
        current_keys=("amp", "amp_22kw"),
        policy=_LOCAL,
        status_keys=("car", "car_state"),
        charging_values=("charging",),
        vehicle_idle_values=("wait car",),
        own_modes=(_rule("select", ("lmo",), ("3", "default", "default mode"), "charging mode"),),
    ),
    PlatformProfile(
        platform="peblar",
        name="Peblar",
        start_stop=StartStop(PATH_SWITCH_BUDGET, keys=("charge",)),
        current_keys=("charge_current_limit",),
        policy=WritePolicy(min_interval_s=10.0, zero_pauses=True, max_pauses_per_10min=3),
        energy_keys=("energy_total",),
        session_energy_keys=("energy_session",),
        status_keys=("cp_state",),
        charging_values=("charging",),
        vehicle_idle_values=("suspended",),
        current_sensor_keys=("current_phase_1", "current_phase_2", "current_phase_3"),
        own_modes=(_rule("select", ("smart_charging",), ("default",), "smart charging"),),
        note="The switch and the current share one register. While paused a current written to the number "
        "is only stored, so the current is written before the switch is turned on, never after. Peblar "
        "allows at most three pauses in ten minutes (relay protection): beyond that a stop holds the "
        "charge at 6 A instead.",
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
        own_modes=(
            _rule("select", ("activation_mode",), ("plug and charge",), "activation mode"),
            _rule(
                "select",
                ("charge_mode",),
                ("boost",),
                "charge mode",
                exclude=("schedule_session",),
            ),
        ),
        note="Stop sets the charger's force-stop flag and Start only clears it, so a Start does nothing "
        "while the activation mode is Schedule or Octopus or the charge mode is Eco or Super Eco.",
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
        own_modes=(_rule("switch", ("schedule",), ("off",), "schedule"),),
        note="The enable switch writes the connector's stored mode: Stop sets Off and replaces the "
        "schedule, and Start does nothing while the mode is Schedule. Stop may not end a running "
        "session on a unit with OCPP enabled.",
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
        policy=WritePolicy(min_interval_s=10.0, state_is_not_setpoint=True, resend_after_plug_in=True),
        energy_keys=("usage_total",),
        session_energy_keys=("usage_session",),
        status_keys=("status", "state"),
        charging_values=("charging",),
        vehicle_idle_values=("connected",),
        disconnected_values=("not_connected",),
        own_modes=(
            _rule("switch", ("solar_pv_divert", "divert"), ("off",), "solar divert"),
            _rule("switch", ("current_shaper",), ("off",), "current shaper"),
        ),
        note="A current is written as a claim for the running session (firmware 4.1.2 or newer), not as "
        "the stored limit. The number shows the stored maximum, which a claim never changes, so it is "
        "never trusted as what is applied, and the claim is sent again after a plug-in.",
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
        start_stop=_switch("charger_enabled", "charger_enabled_socket"),
        current_keys=("max_current_limit", "max_current_limit_socket"),
        policy=WritePolicy(min_interval_s=10.0, zero_pauses=True, ignored_while_paused=True),
        status_keys=("mode_3_state", "mode_3_state_socket"),
        charging_values=("c2", "d2"),
        vehicle_idle_values=("b1", "b2", "c1", "d1"),
        note="The switch and the current share one register: a current written while the charge is "
        "stopped would start it again, so the current is written after a start only.",
    ),
    PlatformProfile(
        platform="heidelberg_energy_control",
        name="Heidelberg Energy Control",
        start_stop=_switch("virtual_enable"),
        current_keys=("virtual_current",),
        policy=_LOCAL_ZERO_PAUSES,
        status_keys=("charging_state",),
        charging_values=("c",),
        vehicle_idle_values=("b",),
    ),
    PlatformProfile(
        platform="garo_wallbox",
        name="GARO",
        start_stop=_select(("sensor", "mode"), ("always_on",), ("always_off",)),
        current_keys=("current_limit",),
        policy=WritePolicy(min_interval_s=60.0, flash_stored=True),
        energy_keys=("acc_energy",),
        session_energy_keys=("acc_session_energy",),
        status_keys=("status",),
        charging_values=("charging",),
        vehicle_idle_values=("charging_paused", "charging_finished"),
        own_modes=(_rule("select", ("sensor", "mode"), ("always_on", "always_off"), "schedule"),),
        note="Writing the current replaces the user's reduced-current schedule with one all-day interval "
        "and needs the Charge Limiter switch on, so it is written at a session start only. Start "
        "replaces the Schedule mode with Always on and Stop leaves Always off.",
    ),
    PlatformProfile(
        platform="smaev",
        name="SMA EV Charger",
        start_stop=_select(("operating_mode_of_charge_session",), ("boost_charging",), ("charge_stop",)),
        current_keys=("charge_current_limit",),
        policy=WritePolicy(min_interval_s=60.0, flash_stored=True),
        energy_keys=("charging_station_meter_reading",),
        session_energy_keys=("charging_session_energy",),
        status_keys=("charging_session_status",),
        charging_values=("active_mode",),
        vehicle_idle_values=("sleep_mode",),
        note="Optimised charging is PV-surplus charging and is never taken as a stop. The limit is a "
        "stored parameter that needs an installer login and is disabled by default: written at a "
        "session start only.",
    ),
    PlatformProfile(
        platform="smartevse",
        name="SmartEVSE",
        start_stop=StartStop(
            PATH_SELECT_RESTORE,
            keys=("smartevse_mode_id",),
            start_options=("normal",),
            stop_options=("pause", "off"),
        ),
        current_keys=("override_current",),
        policy=_LOCAL,
        energy_keys=("smartevse_ev_total_kwh",),
        session_energy_keys=("smartevse_ev_charged_kwh",),
        status_keys=("smartevse_state", "state"),
        charging_values=("charging",),
        vehicle_idle_values=("connected to ev",),
        note="Start puts back the mode the charger was in before SpotNav's Stop (Smart or Solar), and "
        "Normal when that is not known. The charger keeps a written current in RAM only, accepts it in "
        "Normal and Smart mode only, and the Home Assistant number stops at 16 A.",
    ),
    PlatformProfile(
        platform="defa_power",
        name="DEFA Power",
        start_stop=_buttons(("start_charging",), ("stop_charging",)),
        current_keys=("ampere",),
        policy=_CLOUD,
        energy_keys=("meter_value",),
        session_energy_keys=("transaction_meter_value",),
        energy_state_classes=("total", "total_increasing"),
        status_keys=("charging_state",),
        charging_values=("charging",),
        vehicle_idle_values=("suspended_ev", "ev_connected"),
        own_modes=(_rule("switch", ("eco_mode_active",), ("off",), "eco mode"),),
        note="The buttons exist only in some states: Start while the vehicle is connected, Stop while "
        "charging, and neither while the charger's eco mode or a manual schedule has paused it.",
    ),
    PlatformProfile(
        platform="webasto_next_modbus",
        name="Webasto Next",
        start_stop=StartStop(PATH_BUTTONS_TOGGLE, start_keys=("start_session",), stop_keys=("stop_session",)),
        current_keys=("set_current_a",),
        policy=_LOCAL,
        status_keys=("charging_state", "charge_point_state"),
        charging_values=("charging",),
        note="Register 5006 starts a session only when its value changes, so Start sends a cancel first "
        "unless the last command was one. Stop cancels the session. The current number is write-only "
        "and shows the last value written.",
    ),
    PlatformProfile(
        platform="abb_terra_ac",
        name="ABB Terra AC",
        start_stop=StartStop(PATH_NUMBER_PAUSE, keys=("current_limit",), start_keys=("start_charging",)),
        current_keys=("current_limit",),
        policy=WritePolicy(min_interval_s=10.0, ignored_while_paused=True),
        status_keys=("charging_state",),
        charging_values=("state c2 - charging",),
        vehicle_idle_values=("state c1 - ev ready for charge", "state b2 - ev plug in, charging complete"),
        note="ABB's Stop button ends the session and asks for a new badge, so a pause is 0 A written to "
        "the current limit (below 6 A pauses and keeps the session) and a start writes the current. "
        "The start button is pressed only while the charger waits for authorization.",
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
        start_stop=StartStop(
            PATH_SELECT_APPROVE, keys=("charge_mode",), start_options=("max_charge",), stop_options=("paused",)
        ),
        status_keys=("status",),
        charging_values=("charging",),
        vehicle_idle_values=("plugged_in", "finished", "pending_approval"),
        own_modes=(
            _rule("switch", ("price_cap",), ("off",), "price cap"),
            _rule("switch", ("solar_boost",), ("off",), "solar boost"),
        ),
        note="No current control. The integration offers no resume call: a charge waiting for approval "
        "is approved with its button before max charge is selected. Ohme's own smart schedule may fight "
        "external control and its cloud has blocked pausing before.",
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
        role=ROLE_UNSUPPORTED,
        note="Its switch replaces the stored weekly schedule with one-second windows and is unavailable "
        "in manual mode, and the integration no longer works with the new Pod Home app.",
    ),
    PlatformProfile(
        platform="chargepoint",
        name="ChargePoint",
        start_stop=_buttons(("start_charging_session",), ("stop_charging_session",)),
        note="Its current limit is a select: start and stop only. Stop ends the session and Start may stay "
        "refused until the cable is replugged.",
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

