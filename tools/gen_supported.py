"""Write `docs/supported.md` from the tables the integration itself uses.

The page is generated, never edited by hand: `python tools/gen_supported.py` rewrites it, and
`tests/test_supported_doc.py` fails when the committed file differs from what this script writes.

Sources, one per category:

* chargers: `execution/charger_profiles.py` (`_PROFILES`) plus the OCPP path in `config_flow/flow.py`;
* grid meters: `site/site_detection.py` (`METER_ROWS`, the Easee Equalizer, `UPDATE_BEHAVIOUR`);
* house batteries: `site/site_detection.py` (`BATTERY_ROWS`);
* vehicles: the recorded entity shapes in `tests/test_vehicle_catalogue.py`, read as data;
* solar forecast: the Energy dashboard contract read by `planning/hybrid_forecast.py`;
* the overview of inverters and batteries: `OVERVIEW` below for what the market has, with what SpotNav reads,
  controls and has seen on real hardware worked out from the tables above and `battery/adapters.py` and
  `battery/commands.py`.

Display names are the only hand-written data here (`NAMES`); the test fails when a table gains an
integration that has no name.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.spotnav.battery import adapters as ba  # noqa: E402
from custom_components.spotnav.battery import commands as bc  # noqa: E402
from custom_components.spotnav.execution import charger_profiles as cp  # noqa: E402
from custom_components.spotnav.execution import other_controllers as oc  # noqa: E402
from custom_components.spotnav.site import site_detection as sd  # noqa: E402
from custom_components.spotnav.site.import_target import RAISE_LOCK_S  # noqa: E402

OUTPUT = ROOT / "docs" / "supported.md"
VEHICLE_CASES = ROOT / "tests" / "test_vehicle_catalogue.py"
#: Relative to docs/supported.md as GitHub renders it (docs, main, blob, repository), so no owner name.
ISSUES_URL = "../../../issues/new?template=hardware_report.yml"

TESTED = "Tested: charging verified"
SET_UP = "Set up by users: detected and configured, charging not yet confirmed"
DETECTED = "Detected from the integration's source"

#: Hardware confirmed on real equipment, by the owner or a user's report. Everything else is read from the
#: integration's source. Keys: (category, home assistant domain); a row for one maker's devices of an integration
#: that speaks for several (`solax_modbus`) is `<domain>/<maker>` and never takes the bare domain's entry.
TESTED_ON: dict[tuple[str, str], str] = {
    ("charger", "ocpp"): "Charge Amps HALO",
    ("meter", "sigen"): "Sigenergy",
    ("battery", "sigen"): "Sigenergy",
    ("battery", "solax_modbus/solax"): "SolaX",
    ("vehicle", "kia_uvo"): "Kia EV6",
    ("charger", "easee"): "Easee Home",
    ("meter", "tibber"): "Tibber Pulse",
    ("vehicle", "mbapi2020"): "Mercedes-Benz",
    ("vehicle", "mazda_cs"): "Mazda MX-30",
    ("forecast", "solcast_solar"): "Solcast",
    ("charger", "peblar"): "Peblar",
}

#: Chargers whose phase switching for solar has been confirmed on real hardware, by platform. Every other
#: platform that can switch is marked untested.
PHASE_SWITCHING_TESTED_ON: dict[str, str] = {"peblar": "Peblar"}

#: Hardware that users have set up successfully (SpotNav found and configured it) but where nobody
#: has confirmed a charge or a balanced current yet. Same keys as `TESTED_ON`.
SET_UP_ON: dict[tuple[str, str], str] = {
    ("meter", "dsmr"): "ESPHome P1 reader",
    ("meter", "easee"): "Easee Equalizer",
    ("vehicle", "toyota"): "Subaru e-Outback through the Toyota integration",
}

#: What others found on real hardware that a user should know, by (category, Home Assistant domain): appended to the
#: row's notes. Short and plain; each from an upstream report, not from SpotNav's own tests. A row for one maker's
#: devices is keyed `<domain>/<maker>`, as in `TESTED_ON`, and there the note is what the plugin's source says.
HARDWARE_NOTES: dict[tuple[str, str], str] = {
    ("meter", "solax_modbus/sofar"): "The HYD hybrids and the KTL-X inverters with a meter; the older inverters of "
    "the integration's \"Sofar Old\" plugin are not detected.",
    ("meter", "solax_modbus/growatt"): "Only the three-phase SPH (TL3 BH-UP) reports the grid per phase. The TL-XH, "
    "TL3-XH and WIT hybrids and the single-phase SPH report totals only, so the site needs its own grid meter.",
    ("meter", "solax_modbus/solinteg"): "Also M-TEC Energy Butler and Wattsonic. Three-phase units only. The meter's "
    "power per phase is disabled by default and is offered to be enabled. The inverter's own phase current is not "
    "the grid's and is not used. An M2HT-10K-30 needs the integration's release 2026.10.02 or later to be read as a "
    "three-phase hybrid.",
    ("battery", "solax_modbus/sofar"): "The older inverters of the integration's \"Sofar Old\" plugin are not detected.",
    ("battery", "solax_modbus/growatt"): "The off-grid SPF is not detected.",
    ("battery", "solax_modbus/solinteg"): "Also M-TEC Energy Butler and Wattsonic.",
    ("charger", "wallbox"): "Since mid-2025 the Wallbox cloud allows about one request a minute per account, and "
    "Home Assistant's own polling already uses it: keep one charger per account, or use OCPP, which is recommended "
    "for Wallbox.",
    ("charger", "sigen"): "Driven carefully until tested on real hardware: see [Sigenergy DC charger]"
    "(setup.md#sigenergy-dc-charger).",
    ("charger", "smartevse"): "Use firmware 3.10.1 or newer: on 3.9.3 to 3.10.0 some cars do not resume after a pause.",
    ("charger", "zaptec"): "Firmware 2.3 and newer ends a session after 20 restarts plus one per hour of the session, "
    "which solar on a cloudy day can reach. With APM or in stand-alone mode the charger ignores current writes "
    "(Code 527), so there is no current control.",
    ("charger", "goecharger_api2"): "Keep the hidden setting `sua` (simulate unplugging always) off: with it on, a stop "
    "can leave the charger in \"minimum pause duration\" until it is rebooted.",
    ("charger", "goecharger_mqtt"): "Keep the hidden setting `sua` (simulate unplugging always) off: with it on, a stop "
    "can leave the charger in \"minimum pause duration\" until it is rebooted.",
    ("battery", "zendure_ha"): "Each Zendure device has its own battery power and the integration has no total: "
    "with several devices on one account, SpotNav reads one of them.",
    ("battery", "anker_solix"): "Read from Anker's cloud, at most once a minute and often only every five "
    "minutes, which is slow for telling the car's draw from the battery's.",
    ("battery", "ecoflow_cloud"): "PowerOcean and Stream batteries are not detected: which way their power counts "
    "is not known.",
    ("meter", "edl21"): "Home Assistant writes each telegram that differs from the last, which on some meters is "
    "several times a second. Some readers time out with no meter data since Home Assistant 2026.10.4.",
}

#: Display names by Home Assistant domain.
NAMES: dict[str, str] = {
    # chargers: taken from the profiles themselves; see `charger_name`
    "ocpp": "OCPP (Home Assistant OCPP integration)",
    # meters and batteries
    "shelly": "Shelly EM / 3EM",
    "homewizard": "HomeWizard",
    "tibber": "Tibber Pulse",
    "dsmr": "DSMR smart meter",
    "dsmr_reader": "DSMR Reader",
    "p1_monitor": "P1 Monitor",
    "amshan": "AMS HAN",
    "edl21": "EDL21 smart meter",
    "huawei_solar": "Huawei Solar",
    "solarman": "Solarman",
    "fronius": "Fronius",
    "enphase_envoy": "Enphase Envoy",
    "sma": "SMA",
    "pysmaplus": "SMA",
    "solaredge_modbus_multi": "SolarEdge Modbus Multi",
    "solaredge_modbus": "SolarEdge Modbus",
    "victron_gx": "Victron GX",
    "victron_mqtt": "Victron MQTT",
    "victron": "Victron",
    "goodwe": "GoodWe",
    "sigen": "Sigenergy",
    "mqtt": "MQTT",
    "esphome": "ESPHome",
    "easee": "Easee",
    "zaptec": "Zaptec",
    "ferroamp": "Ferroamp",
    "onep1": "ONEp1",
    "powerwall": "Tesla Powerwall",
    "tesla_fleet": "Tesla Fleet",
    "teslemetry": "Teslemetry",
    "tesla_custom": "Tesla Custom Integration",
    "foxess_modbus": "FoxESS Modbus",
    "sungrow": "Sungrow",
    "modbus": "Sungrow Modbus package",
    "solax_modbus": "SolaX Modbus",
    "perific": "Perific",
    "solis_modbus": "Solis Modbus",
    "bitvis": "Bitvis Power Hub",
    "cozify_han": "Cozify HAN",
    "zha": "frient EMIZB-132 (ZHA)",
    "zendure_ha": "Zendure",
    "anker_solix": "Anker Solix",
    "ecoflow_cloud": "EcoFlow",
    "marstek_local_api": "Marstek (local API)",
    # vehicles
    "kia_uvo": "Kia Uvo",
    "ha_kia_hyundai": "Kia and Hyundai (community integration)",
    "tessie": "Tessie",
    "myskoda": "MySkoda",
    "renault": "Renault",
    "volvo": "Volvo",
    "mbapi2020": "Mercedes-Benz",
    "fordpass": "FordPass",
    "stellantis_vehicles": "Stellantis Vehicles",
    "audiconnect": "Audi Connect",
    "cardata": "BMW CarData",
    "volkswagencarnet": "Volkswagen We Connect",
    "toyota": "Toyota",
    "byd_vehicle": "BYD",
    "polestar_api": "Polestar",
    "mg_saic": "MG (SAIC)",
    "rivian": "Rivian",
    "porscheconnect": "Porsche Connect",
    "smarthashtag": "smart #1 and #3",
    "subaru": "Subaru",
    "nissan_connect": "Nissan Connect",
    "leafspy": "Leaf Spy",
    "cupra_we_connect": "Cupra We Connect",
    "toyota_na": "Toyota (North America)",
    "smartcar": "Smartcar",
    "pycupra": "PyCupra (Cupra and Seat)",
    "volkswagen_we_connect_id": "Volkswagen We Connect ID",
    "lynkco": "Lynk & Co",
    "zeekr_ev": "Zeekr",
    "hello_smart": "Hello Smart",
    "abrp": "A Better Route Planner",
    "teslafi": "TeslaFi",
    "lucidmotors": "Lucid Motors",
    "skodaconnect": "Skoda Connect (legacy)",
    "leapmotor": "Leapmotor",
    "nissan_carwings": "Nissan Carwings",
    "uconnect": "Uconnect (Fiat, Jeep, Alfa Romeo)",
    "mazda_cs": "Mazda Connected Services ng",
}

#: Solar forecast integrations: Home Assistant domain, display name. Any integration that feeds the
#: Energy dashboard's solar forecast is accepted; these are the ones named in the forecast adapter's
#: documentation and tests.
FORECAST_SOURCES: tuple[tuple[str, str], ...] = (
    ("forecast_solar", "Forecast.Solar"),
    ("solcast_solar", "Solcast Solar"),
    ("open_meteo_solar_forecast", "Open-Meteo Solar Forecast"),
)

_WARNING_TEXT = {
    sd.WARNING_OWN_LOAD_BALANCING: "Balances load by itself and may fight active control.",
    sd.WARNING_ON_CHANGE_ONLY: "Reports only when a value changes.",
    sd.WARNING_SIGN_UNVERIFIED: "Which way the power counts is read from the source, not confirmed on a device.",
    sd.WARNING_MAY_MEASURE_SUBCIRCUIT: "Check that the meter measures the whole main feed, not a sub-circuit.",
}


OWN_BALANCING_MODELS = {"equalizer": "Equalizer", "sense": "Sense", "apm": "APM"}


def name_of(domain: str) -> str:
    return NAMES.get(domain, domain)


def join_words(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(cell(item) for item in row) + " |" for row in rows)
    return lines


def verified(category: str, *domains: str) -> str:
    for domain in domains:
        hardware = TESTED_ON.get((category, domain))
        if hardware:
            return f"{TESTED} ({hardware})"
    for domain in domains:
        hardware = SET_UP_ON.get((category, domain))
        if hardware:
            return f"{SET_UP} ({hardware})"
    return DETECTED


def makers(row: sd.MeterRow | sd.BatteryRow) -> tuple[str, ...]:
    """The makers a row is for, as the row names them (lower case); empty for a row for any maker."""
    wanted = row.device_manufacturer
    return () if wanted is None else (wanted,) if isinstance(wanted, str) else wanted


def verified_row(category: str, row: sd.MeterRow | sd.BatteryRow) -> str:
    """`verified` for a table row: by its maker where the row is for one maker's devices."""
    return verified(category, *row_keys(row))


def row_keys(row: sd.MeterRow | sd.BatteryRow) -> tuple[str, ...]:
    """The keys of a row in `TESTED_ON` and `HARDWARE_NOTES`: its domains, with its maker where it is for one."""
    named = makers(row)
    return tuple(f"{platform}/{named[0]}" if named else platform for platform in row.platforms)


def maker_note(row: sd.MeterRow | sd.BatteryRow) -> str:
    named = makers(row)
    if not named:
        return ""
    if len(named) == 1:
        return f"Only devices from {named[0]}."
    return f"Only devices from {named[0]} (named \"{'\" or \"'.join(named[1:])}\" by the integration)."


def duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{int(seconds)} s"


# ---- chargers -------------------------------------------------------------------------------------


def start_stop_text(profile: cp.PlatformProfile) -> str:
    text = _start_stop_kind_text(profile)
    return f"{text} (HA {profile.start_stop_since}+)" if profile.start_stop_since else text


def _start_stop_kind_text(profile: cp.PlatformProfile) -> str:
    path = profile.start_stop
    if path is None:
        return "None"
    if path.kind in (cp.PATH_SWITCH, cp.PATH_SWITCH_ASSUMED):
        return "Switch (on means paused)" if path.inverted else "Switch"
    if path.kind == cp.PATH_SELECT:
        return "Mode select"
    if path.kind == cp.PATH_SELECT_RESTORE:
        return "Mode select, the previous mode is put back on a start"
    if path.kind == cp.PATH_SELECT_APPROVE:
        return "Mode select, a charge waiting for approval is approved first"
    if path.kind in (cp.PATH_BUTTONS, cp.PATH_BUTTONS_TOGGLE):
        return "Start and stop buttons"
    if path.kind == cp.PATH_NUMBER_PAUSE:
        return "Current limit (0 A pauses) and a start button"
    if path.kind == cp.PATH_SWITCH_BUDGET:
        return "Switch, at most three pauses in ten minutes"
    if path.kind == cp.PATH_EASEE:
        return "Easee services"
    return path.kind


def current_text(profile: cp.PlatformProfile) -> str:
    if profile.easee_current:
        return "Easee dynamic limit service"
    if profile.power_keys:
        return "Power limit number (kW)"
    if profile.current_keys:
        return "Number entity"
    return "None"


def energy_text(profile: cp.PlatformProfile) -> str:
    parts = []
    if profile.energy_keys:
        parts.append("Lifetime energy")
    elif profile.energy_power_keys:
        parts.append("Energy counted from its power")
    elif profile.session_energy_keys:
        parts.append("Per-session energy")
    if profile.status_keys:
        parts.append("status sensor")
    return ", ".join(parts).capitalize() if parts else "None"


def policy_sentences(policy: cp.WritePolicy) -> list[str]:
    lines = []
    if policy.min_interval_s > 0:
        lines.append(f"At most one current write every {duration(policy.min_interval_s)}.")
    if policy.max_writes_per_minute:
        lines.append(f"At most {policy.max_writes_per_minute} settings changes a minute.")
    if policy.flash_stored:
        lines.append("The current is stored in the charger: written when a charge starts, never while balancing.")
    if policy.zero_pauses:
        lines.append("Zero amps would pause the charge, so SpotNav stops it instead.")
    if policy.ignored_while_paused:
        lines.append("Ignored while paused: written after the charge starts.")
    if policy.installation_wide:
        lines.append("Limits the whole installation: used only with one charger.")
    if policy.resend_after_plug_in:
        lines.append("Sent again after a car is plugged in and after a restart.")
    if policy.state_is_not_setpoint:
        lines.append("The number shows a stored limit, not what is applied, so it is never taken as already set.")
    if policy.max_pauses_per_10min:
        lines.append(f"At most {policy.max_pauses_per_10min} pauses in ten minutes; beyond that it holds at the floor.")
    return lines


def charger_notes(profile: cp.PlatformProfile) -> str:
    lines = [profile.note] if profile.note else policy_sentences(profile.policy)
    if profile.own_modes:
        labels = join_words(sorted({rule.label for rule in profile.own_modes}))
        lines.append(f"Turn off its own {labels}: two controllers on one charger fight each other.")
    if profile.phase_switching:
        lines.append(f"Phase switching for solar through {profile.phase_switching}.")
        tested = PHASE_SWITCHING_TESTED_ON.get(profile.platform)
        lines.append(f"Phase switching tested on {tested}." if tested else "Phase switching: untested on real hardware.")
    if profile.policy.min_interval_s > RAISE_LOCK_S:
        lines.append(
            "Under an import target see [chargers that take a new current only every few minutes]"
            "(site-and-load-balancing.md#holding-the-chargers-under-the-import-target)."
        )
    note = HARDWARE_NOTES.get(("charger", profile.platform))
    if note:
        lines.append(note)
    return " ".join(lines)


def charger_row(profile: cp.PlatformProfile) -> tuple[str, ...]:
    return (
        f"{profile.name} (`{profile.platform}`)",
        start_stop_text(profile),
        current_text(profile),
        energy_text(profile),
        charger_notes(profile),
        verified("charger", profile.platform),
    )


OCPP_ROW: tuple[str, ...] = (
    "OCPP (`ocpp`), any charge point",
    "Charge control switch",
    "AssignedCurrent through ChangeConfiguration, else the connector's session current limit number",
    "Energy.Active.Import.Register sensor",
    "SpotNav asks the charge point which of the two it supports when the charger is added. "
    "A charge point with several connectors is added once per connector. "
    "The session current limit goes out as a relative charging profile, which Easee over OCPP, KEBA firmware 1.18 "
    "and 1.19.0, Autel, and ABB firmware 1.8.37 with the integration's 0.12 versions reject: current control does "
    "not work on those. A charging profile left behind can cap the charger; `ocpp.clear_profile` removes it.",
    verified("charger", "ocpp"),
)

CHARGER_HEADER = ("Integration", "Start and stop", "Current", "Energy and status", "Notes", "Verification")


def chargers_section() -> list[str]:
    profiles = cp._PROFILES
    by_role: dict[str, list[cp.PlatformProfile]] = {}
    for profile in profiles:
        by_role.setdefault(profile.role, []).append(profile)
    lines = [
        "## Supported EV chargers",
        "",
        "Add a charger with **Settings, Devices & services, Add integration, SpotNav, A charger, "
        "Automatic (recommended)**. SpotNav reads the charger's own integration, suggests the charge "
        "control, the current setting, the energy meter and the status sensor, and asks you to confirm. "
        "Any other charger that exposes a switch works through **Manual**, which starts and "
        "stops the charge and can set a current through a number entity.",
        "",
        "### Supported",
        "",
        "These integrations can be started and stopped, and most can have their current set. "
        "A column that says None means SpotNav does that part without the charger's help: "
        "a charger with no current control still follows the plan's start and stop times, and load balancing and "
        "solar switch it on and off whole at the current SpotNav learns it draws "
        "(see [chargers that can only be switched on and off](site-and-load-balancing.md#chargers-that-can-only-be-switched-on-and-off)).",
        "",
    ]
    rows = [OCPP_ROW] + [charger_row(p) for p in by_role.get(cp.ROLE_CHARGER, [])]
    lines += table(CHARGER_HEADER, rows)
    lines.append("")
    measurement = by_role.get(cp.ROLE_MEASUREMENT_ONLY, [])
    lines += [
        "### Measurement only",
        "",
        "SpotNav cannot start or stop these chargers, so the flow refuses them as a charger with the "
        "message that they can only be read. They stay in the catalogue because their energy and "
        "current sensors are recognised as measurements.",
        "",
    ]
    lines += table(
        ("Integration", "Energy and status", "Notes", "Verification"),
        [
            (f"{p.name} (`{p.platform}`)", energy_text(p), p.note, verified("charger", p.platform))
            for p in measurement
        ],
    )
    lines.append("")
    external = by_role.get(cp.ROLE_EXTERNAL_CONTROLLER, [])
    lines += [
        "### Controlled by another system",
        "",
        "These integrations mean another system owns the charger. The flow shows a warning and "
        "suggests nothing, because two controllers on one charger send contradictory commands. "
        "Choose the entities yourself only if SpotNav should really take over.",
        "",
    ]
    lines += [f"- {p.name} (`{p.platform}`)" for p in external]
    lines += [
        "",
        "These integrations switch or limit a charger by themselves and run beside SpotNav. The flow "
        "and the card warn that the charger will be fought over, and still suggest the charger's "
        "entities. Turn the other controller off for the charger SpotNav controls. The warning is for "
        "the charger when the controller's setting names it, and for the installation when the "
        "controller cannot name one.",
        "",
    ]
    lines += [f"- {c.name} (`{c.domain}`)" for c in oc.CONTROLLERS]
    lines += [
        "",
        "Tibber's smart charging is not detected, because Home Assistant cannot see it. The core Tibber "
        "integration only reads a linked charger or vehicle (sensors, no switch, number or service that "
        "charges; its Data API scopes are read-only), so the smart charging that Tibber runs for a charger "
        "linked in the Tibber app happens in Tibber's cloud. If the charger is linked to Tibber (or another "
        "app) for smart charging, turn that off in the app, or SpotNav and the app will fight. The Easee "
        "setup step says so. Sources: [Tibber in Home Assistant]"
        "(https://www.home-assistant.io/integrations/tibber) and the integration's source, "
        "`homeassistant/components/tibber` (Home Assistant 2026.9.2).",
        "",
    ]
    unsupported = by_role.get(cp.ROLE_UNSUPPORTED, [])
    lines += [
        "### Not supported",
        "",
        "SpotNav recognises these chargers but cannot drive them; the flow stops with a message.",
        "",
    ]
    lines += [f"- {p.name} (`{p.platform}`)" + (f": {p.note.rstrip('.')}" if p.note else "") for p in unsupported]
    lines.append("")
    excluded = by_role.get(cp.ROLE_EXCLUDED, [])
    lines += [
        "### Not a charger",
        "",
        "These integrations are recognised as not being EV chargers, so their devices are not offered "
        "when you choose the charger's device.",
        "",
    ]
    lines += [f"- {p.name} (`{p.platform}`)" for p in excluded]
    lines.append("")
    lines += [
        "### A dumb charger behind a smart plug",
        "",
        "A charger with no integration of its own, such as a granny charger or a simple wallbox, can be "
        "switched by a smart plug that reports power. Add it with **Manual**: the plug's switch is the "
        "**Charge control** and the plug's power sensor (device class power, W or kW) is the **Power sensor "
        "(smart plug)**. Both can be changed later in the card's charger settings.",
        "",
        "- SpotNav integrates the power into an energy counter of its own (the \"Energy from power\" sensor, "
        "trapezoid between readings). A gap longer than the maximum measurement age is not integrated, and "
        "the counter is kept across restarts. It stands in for the energy register unless you choose one.",
        "- With no status sensor, SpotNav reads \"the car is finished or not drawing\" from the power: below "
        "100 W for five minutes during a planned charge, the card says the charger draws almost no power. "
        "The limit can be changed under the integration's **Configure** (*Not drawing below (W)*).",
        "- The plug must be rated for the charger's continuous current (a charger at 16 A needs a plug that "
        "carries 16 A for hours, which many cheap plugs do not), and the charger must start charging again "
        "by itself when power returns. SpotNav can switch the plug but cannot tell the charger to start.",
        "- There is no current control: the charger charges at its own current and SpotNav only switches it. "
        "SpotNav learns that current from the plug's power, and load balancing pauses and resumes the charge whole "
        "(see [chargers that can only be switched on and off](site-and-load-balancing.md#chargers-that-can-only-be-switched-on-and-off)).",
        "",
    ]
    return lines


# ---- grid meters ----------------------------------------------------------------------------------


def meter_measurement(row: sd.MeterRow) -> str:
    roles = {pattern.role for pattern in row.patterns}
    if {"power", "voltage"} <= roles:
        extras = roles & {"current", "apparent_power", "reactive_power"}
        return "Derived from power and voltage, current " + ("exact" if extras else "estimated")
    return "Direct phase current"


def meter_signs(row: sd.MeterRow) -> str:
    roles = {pattern.role for pattern in row.patterns}
    parts = []
    if row.signed_current:
        parts.append("Negative current while exporting, read as its size")
    if row.invert_power:
        parts.append("Export-positive power, negated")
    if "power_export" in roles:
        parts.append("Import and export are two entities")
    return "; ".join(parts) if parts else "None needed"


def meter_notes(row: sd.MeterRow) -> str:
    lines = []
    if row.device_manufacturer:
        lines.append(maker_note(row))
    if row.device_model:
        lines.append(f"Only devices whose model contains \"{row.device_model}\".")
    if row.voltage_any_device:
        lines.append("Voltage is taken from the inverter, which is on another device of the same integration.")
    lines.extend(_WARNING_TEXT[code] for code in row.warnings if code in _WARNING_TEXT)
    if row.totals:
        pair = any(pattern.role == "grid_power_export" for pattern in row.totals)
        lines.append(
            "Total grid power for solar and hybrid: "
            + ("an import and an export entity." if pair else "one signed entity.")
        )
    behaviour = [sd.UPDATE_BEHAVIOUR[p] for p in row.platforms if p in sd.UPDATE_BEHAVIOUR]
    if behaviour and behaviour[0].interval_s > 0:
        lines.append(f"Updates about every {duration(behaviour[0].interval_s)}.")
    lines.extend(HARDWARE_NOTES[("meter", p)] for p in row_keys(row) if ("meter", p) in HARDWARE_NOTES)
    return " ".join(lines)


def domains_text(domains: tuple[str, ...]) -> str:
    return ", ".join(f"`{d}`" for d in domains)


def meters_section() -> list[str]:
    rows = []
    for row in sd.METER_ROWS:
        names = row.label or " / ".join(dict.fromkeys(name_of(p) for p in row.platforms))
        rows.append(
            (
                f"{names} ({domains_text(row.platforms)})",
                meter_measurement(row),
                meter_signs(row),
                meter_notes(row),
                verified_row("meter", row),
            )
        )
    rows.append(
        (
            f"Easee Equalizer ({domains_text(('easee',))})",
            "Direct phase current, all phases on one entity",
            "Negative current while exporting, read as its size",
            "The Equalizer balances load by itself and may fight active control. Its values come every 5-10 "
            "minutes and are written only on a change: too seldom for load balancing, enough for solar.",
            verified("meter", "easee"),
        )
    )
    own = "; ".join(
        name_of(platform) + (f" {OWN_BALANCING_MODELS.get(needle, needle)}" if needle else "")
        for platform, needle in sd._OWN_BALANCING
    )
    lines = [
        "## Supported grid meters",
        "",
        "When you add a site, SpotNav scans the entity registry for these meters, including entities "
        "their integration ships disabled, and offers what it found. Nothing is applied until you "
        "confirm, and only entities the integration disabled are ever enabled. A meter that is not "
        "listed is still found when its entities carry a per-phase current or power and voltage, "
        "or you can pick the entities yourself.",
        "",
        "Measurement is either direct (a current per phase) or derived (power and voltage per phase). "
        "Derived current is exact when the meter also reports its own current, apparent power or "
        "reactive power, and estimated from power with a power factor of 0.9 otherwise.",
        "",
        "Solar and hybrid need the grid's signed power. A derived site has it per phase; a direct site "
        "has it only with the meter's total grid power, which the rows below mark where SpotNav detects "
        "it. Without it, pick the meter's total power sensor yourself in the site editor.",
        "",
    ]
    lines += table(("Integration", "Measurement", "Sign handling", "Notes", "Verification"), rows)
    lines += [
        "",
        f"Devices known to balance load by themselves, which SpotNav warns about: {own}.",
        "",
    ]
    return lines


# ---- batteries ------------------------------------------------------------------------------------


def batteries_section() -> list[str]:
    rows = []
    for row in sd.BATTERY_ROWS:
        names = row.label or " / ".join(dict.fromkeys(name_of(p) for p in row.platforms))
        if row.discharge_regex is not None:
            convention = "Charge and discharge are two entities, combined"
        elif row.inverted:
            convention = "Discharge-positive, negated"
        else:
            convention = "Charge-positive, used as it is"
        notes = " ".join(
            line
            for line in (
                maker_note(row),
                f"Only devices whose model contains \"{row.device_model}\"." if row.device_model else "",
                *(HARDWARE_NOTES[("battery", p)] for p in row_keys(row) if ("battery", p) in HARDWARE_NOTES),
            )
            if line
        )
        rows.append((f"{names} ({domains_text(row.platforms)})", convention, notes, verified_row("battery", row)))
    return [
        "## Supported house batteries",
        "",
        "The site uses a house battery's power to tell the car's draw from the house's, and to decide "
        "between the car and the battery when the sun shines. SpotNav reads charging as positive power "
        "and negates the integrations that report the opposite.",
        "",
        *table(("Integration", "Sign convention", "Notes", "Verification"), rows),
        "",
    ]


# ---- inverters and batteries: the overview ----------------------------------------------------------


class Overview(NamedTuple):
    """One maker in the overview. What SpotNav reads, controls and has seen is worked out from `keys` (the rows of
    `METER_ROWS` and `BATTERY_ROWS`, as `row_keys` names them) and `adapter` (a brand of `battery/adapters.py`).
    The rest is hand-written from the sources the module names, and says "unknown" where they say nothing."""

    brand: str
    #: The Home Assistant integrations, with where each comes from.
    integrations: str
    keys: tuple[str, ...] = ()
    adapter: str | None = None
    #: The control column for a maker SpotNav has no commands for.
    control: str = "Unknown"
    note: str = ""


UNKNOWN = "Unknown"
#: The makers `docs/home-battery.md` offers the control for (the first brand); the others with commands are not yet.
CONTROL_IN_TESTING: frozenset[str] = frozenset({"sigenergy"})
CONTROL_OPT_IN = "Opt-in, in testing"
CONTROL_MAPPED = "Mapped, never run on a device"

#: Sources: `battery/adapters.py`, `battery/commands.py`, `battery/coexist.py`, `site/site_detection.py`, and the
#: research notes `plans/research_integrations/` (`battery_local_control_guide_2026-11-14.md`, `site.md`,
#: `commercial_energy_gap_2026-11-14.md`, `solinteg_m2ht_2026-11.md`) and `plans/v3_battery_control_optin.md`.
#: Repository names are the ones `tools/upstream_drift/upstreams.py` or those notes give; none is guessed.
OVERVIEW: tuple[Overview, ...] = (
    Overview(
        "Sigenergy",
        "`sigen`, HACS (TypQxQ/Sigenergy-Local-Modbus)",
        keys=("sigen",),
        adapter="sigenergy",
        note="Keeps the last command: no dead-man, so SpotNav caps a grid charge for the case that Home Assistant "
        "stops. Has its own grid import and export limits; SpotNav uses the export limit. Often run by Sigen AI mode.",
    ),
    Overview(
        "Huawei (SUN2000, LUNA2000)",
        "`huawei_solar`, HACS (wlcrs/huawei_solar)",
        keys=("huawei_solar",),
        adapter="huawei",
        note="Control needs the installer password in the integration. A forced charge ends by itself after its "
        "duration. The cloud integrations (FusionSolar) are too slow and are not read.",
    ),
    Overview(
        "SolarEdge",
        "`solaredge_modbus_multi`, HACS (WillCodeForCats/solaredge-modbus-multi); `solaredge_modbus`, HACS "
        "(binsentsu) and Home Assistant core",
        keys=("solaredge_modbus_multi", "solaredge_modbus"),
        adapter="solaredge",
        note="Control is mapped for Modbus Multi only, and only while the storage is in Remote Control; a command ends "
        "by itself after its timeout. The cloud integration (`solaredge`, every 15 minutes) is not read.",
    ),
    Overview(
        "SolaX (X1 and X3 hybrids)",
        "`solax_modbus`, HACS (wills106/homeassistant-solax-modbus), its SolaX plugin",
        keys=("solax_modbus/solax",),
        adapter="solax",
        note="Remote control ends by itself when it is no longer renewed. One Modbus client at a time.",
    ),
    Overview(
        "Sofar (HYD hybrids, KTL-X)",
        "`solax_modbus`, HACS, its Sofar plugin. Also reachable through `solarman` (see Deye). Not read: the core "
        "`sofar` integration",
        keys=("solax_modbus/sofar",),
        adapter="sofar",
        control="Not mapped. The integration has a passive mode with a grid power target and battery power limits",
        note="The passive mode has its own setting for what the inverter does when commands stop coming.",
    ),
    Overview(
        "Growatt (SPH, SPA, MIN TL-XH, MOD and MID TL3-XH, WIT)",
        "`solax_modbus`, HACS, its Growatt plugin. Not read: core `growatt_server` (cloud, every 5 minutes) and a "
        "HACS Modbus integration by 0xAHA",
        keys=("solax_modbus/growatt",),
        adapter="growatt",
        control="Not mapped. What can be controlled locally depends on the model",
        note="Only the three-phase SPH reports the grid per phase. The off-grid SPF is not read.",
    ),
    Overview(
        "Solinteg (MHT, M2HT; also M-TEC Energy Butler, Wattsonic)",
        "`solax_modbus`, HACS, its Solinteg plugin",
        keys=("solax_modbus/solinteg",),
        adapter="solinteg",
        control="Not mapped. The integration has an EMS mode with a battery power target",
        note="Keeps the last command: no dead-man found, and one owner measured a command standing for 16 minutes, so "
        "SpotNav caps a grid charge for the case that Home Assistant stops, counted as all on one phase. Has its own "
        "site import limit; whether it acts under EMS control is unknown, so SpotNav does not count on it. Charging "
        "per phase is mapped but cannot run: the integration reads no power per phase of the inverter. One Modbus TCP "
        "client at a time, and the dongle stops answering for some seconds about once a minute.",
    ),
    Overview(
        "Sungrow (SH hybrids)",
        "`sungrow`, HACS (KRoperUK/sungrow-hass); the mkaiser Modbus YAML package (plain `modbus` sensors, not an "
        "integration)",
        keys=("sungrow", "modbus"),
        control="Not mapped. Forced charge and discharge exist locally",
        note="No battery level or size is read yet. iSolarCloud entries update only every 5 minutes.",
    ),
    Overview(
        "GoodWe (ET, EH)",
        "`goodwe`, Home Assistant core",
        keys=("goodwe",),
        adapter="goodwe",
        note="Control needs the HACS integration (mletenay/home-assistant-goodwe-inverter): core has no eco charge "
        "mode and no eco power or level numbers, and the status then says so. Nothing stops a command when Home "
        "Assistant stops, so a grid charge is capped for that case. The eco level ceiling is ignored on some "
        "firmwares, so a charge is judged by the battery's power. The hold (the depth of discharge set to the level "
        "now) is not confirmed on a device: one that charges from the grid is handed back.",
    ),
    Overview(
        "Fronius (GEN24 with BYD)",
        "`fronius`, Home Assistant core (reads only). Not read: `fronius_modbus`, HACS (redpomodoro/fronius_modbus)",
        keys=("fronius",),
        control="Not mapped. Modbus control exists (charge and discharge limits, grid charging, reserve)",
        note="Its Modbus control has an optional fall-back if commands stop. The meter updates about once a minute.",
    ),
    Overview(
        "Victron (GX with ESS)",
        "`victron_gx`, Home Assistant core; `victron_mqtt`, HACS (tomer-w/ha-victron-mqtt); `victron`, HACS "
        "(sfstar/hass-victron)",
        keys=("victron_gx", "victron_mqtt", "victron"),
        adapter="victron",
        control="Not mapped. ESS mode, grid setpoint and minimum SoC exist locally",
        note="Control is for `victron_gx` and `victron_mqtt` (a Multi or Quattro with ESS); the Modbus integration "
        "is read only. The Multi's own one-minute fall-back is not counted on until a plant has shown it. The limits "
        "are settings stored on the GX, written rarely.",
    ),
    Overview(
        "Enphase",
        "`enphase_envoy`, Home Assistant core",
        keys=("enphase_envoy",),
        adapter="enphase",
        control="Not possible locally on Envoy firmware 8.2.4225 and later: the battery's mode is set in the "
        "Enphase app. Planned later through its cloud, for slow actions only",
        note="The local API needs a token from Enphase's cloud, renewed yearly.",
    ),
    Overview(
        "Tesla Powerwall",
        "`powerwall`, `tesla_fleet` and `teslemetry`, Home Assistant core; `tesla_custom`, HACS (alandtse/tesla)",
        keys=("powerwall", "tesla_fleet", "teslemetry", "tesla_custom"),
        adapter="tesla",
        note="Control is mapped for `tesla_fleet` and `teslemetry` only, through Tesla's cloud: slow, planned "
        "actions only (backup reserve and grid charging, no peak cover, few writes a day). The charge power is the "
        "Powerwall's own, so a charge that does not fit under the fuse on one phase is refused, not lowered, and none "
        "runs while the battery's size is unknown. No dead-man: a grid charge runs in 30-minute blocks, so a lost "
        "connection leaves one block's charge. Only while the operation mode is self-powered. Powerwall 3 has a more "
        "restricted local API.",
    ),
    Overview(
        "Deye, Sunsynk, Sol-Ark",
        "`solarman`, HACS (davidrapan/ha-solarman). Not read: the sunsynk add-on (kellerza/sunsynk) and the cloud "
        "integrations",
        keys=("solarman",),
        control="Not mapped. Work mode and programme slots are written in community setups, not confirmed",
        note="No battery level or size is read yet.",
    ),
    Overview(
        "Solis",
        "`solis_modbus`, HACS (Pho3niX90/solis_modbus). Not read: the Solis plugins of `solax_modbus` and the cloud "
        "integration (`solis`)",
        keys=("solis_modbus",),
        note="The meter may sit on the load or the PV side: check that it measures the main feed.",
    ),
    Overview(
        "FoxESS (H1, H3, KH, AC1, AC3)",
        "`foxess_modbus`, HACS (nathanmarlor/foxess_modbus). The cloud integration (`foxess`) is not read",
        keys=("foxess_modbus",),
        control="Not mapped. Work mode, charge periods and SoC limits exist locally, depending on the model",
        note="Only the battery's power is read, not the grid.",
    ),
    Overview(
        "SMA",
        "`sma`, Home Assistant core; `pysmaplus`, HACS",
        keys=("sma", "pysmaplus"),
        control="Unknown: Modbus setpoints exist, but no Home Assistant integration for them was found",
        note="Core does not support ennexOS devices such as the Tripower X.",
    ),
    Overview(
        "Kostal (Plenticore)",
        "`kostal_plenticore`, Home Assistant core: not read",
        control="Not mapped. Power control is Modbus \"external battery control\", set up by the installer; no Home "
        "Assistant integration for it was found",
        note="External control falls back to the inverter's own after a timeout.",
    ),
    Overview("SAJ", UNKNOWN, note="Named among the batteries Tibber runs (not verified)."),
    Overview(
        "Alpha ESS",
        "Not read: `alphaess`, HACS (cloud), and an AlphaESS plugin of `solax_modbus`",
    ),
    Overview(
        "sonnen",
        "`sonnenbatterie`, HACS (weltmeyer/ha_sonnenbatterie): not read",
        control="Not mapped. Operating mode, charge and discharge power and reserve exist locally",
        note="Joins Tibber's virtual power plant.",
    ),
    Overview(
        "Emaldo",
        "ha-emaldo (cloud and local UDP): not read",
        control="Not possible locally. Planned later through its cloud, for slow actions that end by themselves",
        note="Emaldo steers its batteries from its own cloud for grid services.",
    ),
    Overview(
        "Pixii (Pixii Home)",
        "None found",
        control="Unknown: its command documentation is not public",
        note="Greenely runs Pixii Home in Sweden. An owner reports local MQTT readings.",
    ),
    Overview(
        "Ferroamp (EnergyHub)",
        "`ferroamp`, HACS (henricm/ha-ferroamp)",
        keys=("ferroamp",),
        adapter="ferroamp",
        note="Control sends the integration's charge, discharge and auto requests (whole-system power, no timeout "
        "documented; a request counts once the hub has acked it). The charge cap counts it on one phase: how the hub "
        "spreads it is not documented, and SpotNav does not count on ACE. MQTT is enabled by Ferroamp's support. Pools batteries for grid services with Ntricity.",
    ),
    Overview(
        "Homevolt (Tibber)",
        "`homevolt`, Home Assistant core: not read",
        control="Not mapped. Battery mode, a power setpoint and grid limits exist locally",
        note="The local API is activated by Tibber's support. Usually run by Tibber Grid Rewards.",
    ),
    Overview("HomeWizard (its battery)", "`homewizard`, Home Assistant core", keys=("homewizard",)),
    Overview(
        "Zendure (SolarFlow, Hyper, AIO)",
        "`zendure_ha`, HACS (Zendure/Zendure-HA)",
        keys=("zendure_ha",),
        note="With several devices on one account only one is read.",
    ),
    Overview(
        "Anker Solix (Solarbank)",
        "`anker_solix`, HACS (thomluther/ha-anker-solix)",
        keys=("anker_solix",),
        note="Read from Anker's cloud, a minute or more between values.",
    ),
    Overview(
        "EcoFlow (PowerStream, Ocean Pro)",
        "`ecoflow_cloud`, HACS (tolwi/hassio-ecoflow-cloud)",
        keys=("ecoflow_cloud",),
        note="PowerOcean and Stream batteries are not read.",
    ),
    Overview(
        "Marstek",
        "`marstek_local_api`, HACS (jaapp/ha-marstek-local-api)",
        keys=("marstek_local_api",),
    ),
)


def _overview_reads(entry: Overview) -> tuple[list[str], bool]:
    """What SpotNav reads of a maker, and whether any of it was confirmed on real hardware."""
    wanted = set(entry.keys)
    parts = []
    if any(wanted & set(row_keys(row)) for row in sd.METER_ROWS):
        parts.append("grid meter")
    if any(wanted & set(row_keys(row)) for row in sd.BATTERY_ROWS):
        parts.append("battery power")
    if entry.adapter is not None:
        adapter = next(adapter for adapter in ba.ADAPTERS if adapter.brand == entry.adapter)
        parts.append("battery level" + (", size" if "capacity" in adapter.facts or "capacity_ah" in adapter.facts else ""))
    tested = any((category, key) in TESTED_ON for category in ("meter", "battery") for key in entry.keys)
    return parts, tested


def _overview_row(entry: Overview) -> tuple[str, ...]:
    parts, tested = _overview_reads(entry)
    reads = "No" if not parts else ("Yes" if tested else "Yes, untested") + ": " + ", ".join(parts)
    control = entry.control
    if any(control.brand == entry.adapter for control in bc.CONTROLS):
        control = CONTROL_OPT_IN if entry.adapter in CONTROL_IN_TESTING else CONTROL_MAPPED
    seen = [
        f"{name} read ({TESTED_ON[(category, key)]})"
        for category, name in (("meter", "Grid meter"), ("battery", "Battery power"))
        for key in entry.keys
        if (category, key) in TESTED_ON
    ]
    return (entry.brand, entry.integrations, reads, control, "; ".join(seen) or "Nothing yet", entry.note)


def overview_section() -> list[str]:
    return [
        "## Hybrid inverters and home batteries: overview",
        "",
        "Every hybrid inverter and home battery we know of in the Nordics and Europe, and how far SpotNav gets with "
        "each. The detailed rows are under [grid meters](#supported-grid-meters) and "
        "[house batteries](#supported-house-batteries); what the battery plan needs from each is in "
        "[The battery plan](battery-plan.md#what-the-plan-needs).",
        "",
        "- **SpotNav reads**: what it finds by itself through the integration. *Untested* means read from the "
        "integration's source and not yet confirmed on real hardware. A battery that is not read can still be used "
        "by picking its power sensor yourself on the site.",
        f"- **SpotNav controls**: *{CONTROL_OPT_IN}* is [SpotNav's own battery control]"
        "(home-battery.md#spotnavs-own-battery-control-off-by-default-in-testing), off by default. "
        f"*{CONTROL_MAPPED}* means SpotNav has the commands for that integration behind the same switch and the same "
        "check of the battery, but nobody has run them on a battery yet. *Not mapped* means SpotNav only reads, "
        "with what the integration itself could do locally. Without control SpotNav still plans in shadow where it "
        "reads the battery's level, and still signals a grid charge to your own planner or automation.",
        "- **Unknown** is where neither SpotNav's tables nor our notes say. Corrections are welcome: "
        f"[Hardware tested]({ISSUES_URL}).",
        "",
        *table(
            ("Brand", "Home Assistant integration", "SpotNav reads", "SpotNav controls", "Seen on real hardware", "Notes"),
            [_overview_row(entry) for entry in OVERVIEW],
        ),
        "",
    ]


# ---- heat pumps -----------------------------------------------------------------------------------

#: One row per pump and Home Assistant route: (brand, integration, local or cloud, what SpotNav controls of the
#: heating, hot water on command, notes). Hand-written from `plans/research_integrations/` (the heat pump survey,
#: the cloud note and the hot-water note) and from `heat/adapters.py` and `heat/paths.py`; an integration is named
#: as the sources name it, never linked from memory. "Unverified" means no source read says either way.
HEAT_PUMP_HEADER = ("Brand", "Integration", "Local or cloud", "SpotNav controls", "Hot water on command", "Notes")
HEAT_PUMPS: tuple[tuple[str, ...], ...] = (
    (
        "NIBE F-series (F730, F750, F1155, F1255, SMO, VVM)",
        "`nibe_heatpump`, Home Assistant core",
        "Local",
        "Relay or SG Ready contact; offset (47011, stored), untested",
        "Boost: temporary lux, select 48132 (off, 3 h, 6 h, 12 h, one time). SpotNav uses \"one time\" for the "
        "hot water button; planned boosts in shadow first, untested",
        "The lux run ends by itself. Comfort mode and the start and stop temperatures are stored settings.",
    ),
    (
        "NIBE F-series",
        "`myuplink`, Home Assistant core",
        "Cloud",
        "Read only; an offset number if you pick one, untested",
        "Boost: switch temporary lux (50004), select 48132 on SMO 20. SpotNav uses it for the hot water button; "
        "planned boosts in shadow first, at most two a day, untested",
        "Needs your own myUplink API app. The lux run ends by itself. Request limits unverified.",
    ),
    (
        "NIBE S-series (S1155, S1255, S320, S735)",
        "`nibe_heatpump`, Home Assistant core",
        "Local",
        "SG Ready through the API (43033 and 46009), offset (40031, stored): both untested",
        "Unproven: \"more hot water\" (40698), values undocumented. No temporary lux register",
        "SG Ready is the clean route here. Stop temperatures (40059-40065) and the water heater mode are stored settings.",
    ),
    (
        "CTC EcoLogic, EcoZenith, EcoAir",
        "`ctc_bms`, HACS (dklemm/home-assistant-ctc-bms); `ctc_ecozenith`, HACS (beolink/HA-CTC-Local-Easyconf)",
        "Local (Modbus TCP)",
        "Offset (`ctc_bms` only), untested; the Local Easyconf integration is read only",
        "Boost: extra hot water in hours; hot water mode and setpoint. SpotNav uses extra hot water for one hour "
        "for the hot water button; planned boosts in shadow first, untested",
        "These controls are volatile: the pump forgets them about five minutes after the last write, so a lost "
        "connection hands back by itself. Stored parameters are not written.",
    ),
    (
        "Panasonic Aquarea",
        "HeishaMon with `kamaradclimber/heishamon-homeassistant`, HACS",
        "Local (MQTT, needs the HeishaMon board)",
        "Read; relay or SG Ready contact; an offset number if you pick one, untested",
        "Boost: force hot water (switch); hot water setpoint 40-75. SpotNav uses force hot water for the hot water "
        "button only and turns it off after the expected run: whether it ends by itself is unverified",
        "Whether HeishaMon's commands survive a power cut is unverified.",
    ),
    (
        "Panasonic Aquarea",
        "`cjaliaga/home-assistant-aquarea`, HACS",
        "Cloud",
        "Not yet",
        "Setpoint and on/off (unverified)",
        "",
    ),
    (
        "Daikin Altherma 4",
        "`joklee/ha_daikin_altherma4_modbus`, HACS",
        "Local (Modbus TCP)",
        "Not yet",
        "Boost: DHW booster and single heat-up switches with setpoints 30-85; reheat setpoint; mode select",
        "Whether the values are stored is unverified. The README advises refreshing state regularly.",
    ),
    (
        "Daikin Altherma",
        "`jwillemsen/daikin_onecta`, HACS",
        "Cloud (200 calls a day)",
        "Not yet",
        "Setpoint, on/off and powerful mode",
        "The call limit leaves room for a few planned actions a day (a morning and an evening run), never a loop.",
    ),
    (
        "Mitsubishi Ecodan",
        "`gekkekoe/esphome-ecodan-hp`, ESPHome",
        "Local (CN105 and an ESP)",
        "Not yet",
        "Boost: forced hot water; block or allow hot water and heating (server control)",
        "Whether the values are stored is unverified.",
    ),
    (
        "Mitsubishi Ecodan",
        "`melcloud`, Home Assistant core",
        "Cloud (throttled)",
        "Not yet",
        "Setpoint and mode (auto, force hot water)",
        "",
    ),
    (
        "Thermia Genesis",
        "`CJNE/thermiagenesis`, HACS",
        "Local (Modbus TCP)",
        "Not yet",
        "Setpoint: start and stop temperatures as numbers (disabled by default). No boost entity seen",
        "A field tester is needed to check the registers.",
    ),
    (
        "Thermia Online",
        "`klejejs/ha-thermia-heat-pump-integration`, HACS",
        "Cloud",
        "Not yet",
        "Boost: hot water and hot water boost switches; water heater",
        "Unverified.",
    ),
    (
        "IVT Greenline, older Bosch Compress (Rego 6xx)",
        "`crnjan/regoheatpump-ha`, HACS",
        "Local (serial)",
        "Not yet",
        "Setpoint: DHW target 35-54 degrees. No boost",
        "A stored Rego setting.",
    ),
    (
        "IVT, Bosch with K30, K40 or iCom",
        "`serbanb11/bosch-homecom-hass`, HACS",
        "Cloud",
        "Not yet",
        "Boost: extra hot water service; setpoint and mode",
        "The cloud answers 429 quickly: not a route for feedback.",
    ),
    (
        "Vaillant (aroTHERM, flexoTHERM)",
        "`signalkraft/mypyllant-component`, HACS",
        "Cloud (strict quotas)",
        "Not yet",
        "A hot water boost entity is listed; the documented services only set time programmes (unverified)",
        "Low scan intervals can get a temporary ban.",
    ),
    (
        "Viessmann (Vitocal)",
        "`vicare`, Home Assistant core",
        "Cloud (daily quota)",
        "Not yet",
        "Setpoint only; no boost or mode write",
        "",
    ),
    (
        "Stiebel Eltron, Tecalor",
        "`pail23/stiebel_eltron_isg_component`, HACS",
        "Local (ISG Modbus)",
        "Not yet; its SG Ready switches fit the SG Ready path",
        "Comfort and eco targets (unverified)",
        "",
    ),
    (
        "Samsung EHS",
        "`echoDaveD/EHS-Sentinel`",
        "Local (RS485 and MQTT)",
        "Not yet",
        "Hot water mode and target with control allowed (unverified)",
        "Writes field settings whose wear is unknown.",
    ),
    (
        "LG Therma V",
        "`lg-heatpump/lg_heatpump`, HACS",
        "Local (Modbus through PI485)",
        "Not yet",
        "On/off and a disinfection switch. No setpoint or boost",
        "",
    ),
    (
        "Hitachi Yutaki",
        "`alepee/hass-hitachi_yutaki`, HACS",
        "Local (Modbus gateway)",
        "Not yet",
        "Boost (unverified)",
        "",
    ),
    (
        "Qvantum",
        "`perosb/qvantum_custom_component`, HACS",
        "Local (Modbus, writing is opt-in) or cloud",
        "Not yet",
        "Modes Eco, Normal, Extra, Smart, Off (unverified)",
        "",
    ),
    (
        "Any pump with an SG Ready input",
        "A switch entity for each of the two inputs",
        "Local",
        "SG Ready (never the forced state)",
        "Only if the installer wired the input to extra hot water",
        "",
    ),
    (
        "Any pump with an input for a relay",
        "A switch entity",
        "Local",
        "Relay: lowers the curve",
        "A contact on an external hot water input, if the pump has one. Chosen as the hot water entity, SpotNav "
        "pulses it for the hot water button; planned boosts in shadow first",
        "",
    ),
    (
        "Any water heater with an immersion element",
        "A switch or smart plug",
        "Local",
        "No: use the price automation blueprint",
        "Allow heating in the cheap hours only; the thermostat stays as the safety",
        "Do not hold the tank cold for days.",
    ),
)


def heat_pumps_section() -> list[str]:
    lines = [
        "## Supported heat pumps",
        "",
        "What SpotNav reads of a heat pump is in [Heat pumps](heat-pumps.md). This table says what it can control, "
        "and which pumps can make hot water on command through Home Assistant.",
        "",
        "- **SpotNav controls** is the heating: *relay* and *SG Ready* switch a contact you wired, *SG Ready API* "
        "writes the pump's own SG Ready mode, *offset* writes the heating curve offset, *Not yet* means SpotNav reads "
        "at most. A stored offset is written rarely, in whole steps, and put back when the control ends.",
        "- **The NIBE S, the CTC and every stored-offset path are untested in the field.** None of them has run on "
        "a real heat pump yet; they are read from the integrations' sources and the makers' manuals.",
        "- **Hot water on command** is what the integration can do, as the sources describe it. *Boost* is a run "
        "that ends by itself, *setpoint* is a stored setting, and *block* holds the pump back. *Unverified* means no "
        "source we read says. Where a boost ends by itself, SpotNav uses it for the **Varmvatten nu** button on the "
        "heat card (your own start, any user). SpotNav also plans boosts before the household's hot water peaks, in "
        "shadow first: they are written only with **Styr värmen** on, after a week of shadow that hit the peaks "
        "(see [Heat pumps](heat-pumps.md#hot-water)). Setpoints are not written.",
        "- Cloud routes are rate limited. They get at most two planned hot water actions a day (before the morning and the evening showers), never a loop; a local route is bounded only by wear.",
        "",
        *table(HEAT_PUMP_HEADER, [row for row in HEAT_PUMPS]),
        "",
        f"A pump or route missing or wrong here? [Hardware tested]({ISSUES_URL}).",
        "",
    ]
    return lines


# ---- vehicles -------------------------------------------------------------------------------------


def vehicle_cases() -> list[dict[str, str | None]]:
    """The recorded vehicle shapes of the catalogue test, read as data without importing it."""
    tree = ast.parse(VEHICLE_CASES.read_text(encoding="utf-8"))
    cases = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Case":
            text = ast.literal_eval(node.args[0])
            keywords = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
            domain = text.split(" ")[0].rstrip(":")
            note = text.split(": ", 1)[1] if ": " in text else ""
            cases.append(
                {
                    "domain": domain,
                    "text": text,
                    "note": note,
                    "soc": keywords.get("soc"),
                    "limit": keywords.get("limit"),
                    "ceiling": keywords.get("ceiling"),
                }
            )
    cases.sort(key=lambda case: (str(case["domain"]), str(case["text"])))
    return cases


def sentence(text: str) -> str:
    """A note as a sentence: capitalised unless it starts with an identifier such as `max_soc`."""
    if not text:
        return ""
    first = text.split(" ")[0]
    if first.isalpha() and first.islower():
        text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def vehicles_section() -> list[str]:
    cases = vehicle_cases()
    rows = []
    seen: set[str] = set()
    misses = []
    for case in cases:
        domain = str(case["domain"])
        if case["soc"] is None:
            misses.append(case)
            continue
        if case["soc"] == "ambiguous" or domain in seen:
            continue
        seen.add(domain)
        if case["limit"]:
            limit = "Can be set"
        elif case["ceiling"]:
            limit = "Read only, used as the ceiling"
        else:
            limit = "None found"
        rows.append(
            (
                f"{name_of(domain)} (`{domain}`)",
                limit,
                sentence(str(case["note"])),
                verified("vehicle", domain),
            )
        )
    lines = [
        "## Supported vehicles",
        "",
        "SpotNav needs no vehicle integration to charge. A vehicle adds the target state of charge, "
        "the charge-level estimate and the charge-limit display. Any integration works when one of its "
        "devices has a battery percentage sensor and a distance (range) sensor: SpotNav detects the "
        "vehicle from those shapes, not from the brand. A Subaru e-Outback, for example, is detected "
        "through the Toyota integration (`pytoyoda`, Home Assistant domain `toyota`) from its battery "
        "level sensor and range sensor, although the list below has no Subaru-specific entry for it.",
        "",
        "The integrations below are the ones the detection was checked against, from each integration's "
        "recorded entity shapes. A vehicle that reports several battery readings is ranked by what the "
        "integration calls them, and when that does not leave exactly one, Home Assistant shows a repair "
        "asking which reading is the state of charge.",
        "",
        *table(("Integration", "Charge limit", "Notes", "Verification"), rows),
        "",
        "### Known pitfalls",
        "",
        "- **Health is not charge.** State-of-health, target, limit, minimum, arrival and predicted "
        "readings are never taken as the state of charge.",
        "- **The 12 V battery.** Many integrations give the car's 12 V battery the battery device class. "
        "A device with only that sensor is not a vehicle.",
        "- **Sleeping cars.** Cloud readings can be an hour old in the middle of a charge. SpotNav "
        "estimates the level forward from the charger's energy meter and marks it as estimated. The app's "
        "refresh, like SpotNav's own re-reads, only asks Home Assistant to re-read the vehicle's entities; SpotNav "
        "never calls an integration's own force-update. Most integrations answer that from their cloud's copy, but "
        "Kia Uvo (`kia_uvo`) asks the car itself when its last report is older than the integration's force-refresh "
        "interval (in the Australian region at every update), which can wake the car.",
        "- **No range sensor, no vehicle.** A device without a distance-class range sensor, or with no "
        "device classes at all, is not detected. Nor is an integration that registers no device: Nissan Leaf "
        "(`nissan_leaf`) has no device and no device class on its ranges, and Volkswagen GoConnect "
        "(`volkswagen_goconnect`) none on its charge level or range.",
        "- **A charge limit that is writable only in one mode.** The unofficial Polestar integration (`polestar`) "
        "accepts its charge target only while its own charging mode is custom. In daily or long-trip mode "
        "SpotNav's write fails and is reported as a failed charge limit.",
        "- **The car's own solar charging.** Teslemetry's (`teslemetry`) charge on solar and its lower limit drive "
        "Tesla's own solar charging. Turn it off for a car SpotNav charges: two controllers on one charge fight "
        "each other, as with a charger's built-in PV mode.",
    ]
    lines.append("- Shapes the detection was checked to leave alone:")
    for case in misses:
        label, _, note = str(case["text"]).partition(": ")
        lines.append(f"  - `{label}`: {sentence(note)}" if note else f"  - `{label}`")
    lines.append("")
    return lines


# ---- solar forecast -------------------------------------------------------------------------------


#: SpotNav's own solar forecast, as `docs/forecasts.md` describes it (its section "SpotNav's own solar forecast
#: (background only)"), with the same attribution of the weather data.
OWN_FORECAST_LINK = "forecasts.md#spotnavs-own-solar-forecast-background-only"
OWN_FORECAST_ROW: tuple[str, str, str] = (
    f"[SpotNav's own solar forecast]({OWN_FORECAST_LINK})",
    "Built in, no integration to add: an irradiance forecast from [Open-Meteo](https://open-meteo.com/) (weather "
    "data by Open-Meteo.com, CC BY 4.0), fetched by your own Home Assistant, and your own measured solar production.",
    "Runs in the background only: logged and scored each night beside the source you chose, and not yet used by "
    "Hybrid or any other plan",
)


def forecast_section() -> list[str]:
    rows = [
        (f"{name} (`{domain}`)", "Chosen on the site; read through the Energy dashboard.", verified("forecast", domain))
        for domain, name in FORECAST_SOURCES
    ]
    rows.append(OWN_FORECAST_ROW)
    return [
        "## Supported solar forecast",
        "",
        "The Hybrid strategy holds back grid energy when a solar forecast promises sun. It reads the "
        "forecast through the Energy dashboard's solar forecast contract, so any integration that "
        "feeds the Energy dashboard's solar forecast can be chosen on the site. SpotNav only reads the "
        "estimate an integration already has and never asks it to fetch. Forecasts in 15, 30 or 60 "
        "minute slots are summed per hour. Without a source, Hybrid plans exactly like Cheapest.",
        "",
        *table(("Source", "What it is", "Verification"), rows),
        "",
        "Whichever source you choose, Solcast included, SpotNav learns a correction for it from your own production "
        "(a factor for clear hours and one for cloudy hours). The battery plan reads the corrected forecast once the "
        "correction has learnt; Hybrid keeps reading the forecast as the source gives it. See "
        "[the solar forecast correction](forecasts.md#the-solar-forecast-correction).",
        "",
    ]


# ---- price areas ----------------------------------------------------------------------------------


def prices_section() -> list[str]:
    return [
        "## Supported price areas",
        "",
        "Your Home Assistant fetches the prices itself, from a source you add once "
        "([Prices](prices.md#where-prices-come-from)):",
        "",
        *table(
            ("Areas", "Source"),
            [
                (
                    "Sweden, Norway, Denmark, Finland, Estonia, Latvia, Lithuania, Germany/Luxembourg, the "
                    "Netherlands, Belgium, France, Austria, Poland, Romania, Bulgaria",
                    "Home Assistant's built-in Nord Pool integration (or your own ENTSO-E key)",
                ),
                (
                    "Switzerland, Czechia, Slovakia, Hungary, Slovenia, Croatia, Greece, Italy (all zones), "
                    "Spain (day-ahead), Portugal",
                    "Your own free ENTSO-E key (the ENTSO-E Transparency Platform)",
                ),
                ("Spain – PVPC (regulated)", "Home Assistant's built-in PVPC integration (Red Eléctrica)"),
                ("Great Britain – Octopus Agile, 14 regions", "The Octopus Energy integration (HACS), your Agile meter"),
                ("Netherlands – Zonneplan", "The Zonneplan integration (HACS), Zonneplan's dynamic tariff"),
            ],
        ),
        "",
        "The card's area picker lists every area and names, in the price settings, where its prices come from "
        "or what to add. The SpotNav relay no longer delivers prices (since SpotNav 2.0).",
        "",
        "### Great Britain (Octopus Agile)",
        "",
        "- **For Agile customers.** The prices are Octopus Energy's Agile import unit rates, one per region "
        "(`GB-A` to `GB-P`, the GSP groups), in half-hours that SpotNav plans on quarter-hours. On any other "
        "tariff they are not your prices.",
        "- **All-in.** The Agile unit rate already contains VAT, levies and network charges, so VAT, energy "
        "tax and grid transfer are locked as *Included in the price* and nothing is added. The daily "
        "standing charge is not part of a plan or a session's cost.",
        "- **Your region.** The picker groups the regions under Great Britain (\"GB C – London\"). *Find my "
        "region* takes a postcode: Home Assistant asks Octopus Energy's public lookup directly, and the "
        "postcode is not sent anywhere else, stored or logged. A new charger in Great Britain starts "
        "with no area until you choose one.",
        "- **Your rates.** They are read from your own meter through the [Octopus Energy]"
        "(https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) integration, installed through HACS. "
        "Without an Agile meter there are no prices.",
        "- **When.** Agile is published around 16:00 UK time for 23:00 to 23:00, so tomorrow's last hour "
        "(23:00 to midnight) arrives with the next day's publication. Amounts are in pounds and pence and "
        "distances in miles.",
        "- **Not with Intelligent Octopus Go.** Octopus does not allow a third party to control the charging "
        "of a car on Intelligent Octopus Go: its customers must opt out of other apps' smart charging. Do not "
        "use SpotNav to control charging on that tariff. Source: [Octopus Energy]"
        "(https://octopus.energy/help-and-faqs/articles/"
        "why-can-t-i-have-both-intelligent-octopus-go-and-a-third-party-controlling-my-charging-/).",
        "",
        "Prices: Octopus Energy (Agile). SpotNav is not affiliated with Octopus Energy.",
        "",
        "### Netherlands (Zonneplan)",
        "",
        "- **For Zonneplan customers.** *Netherlands – Zonneplan* (`NL-ZONNEPLAN`) is Zonneplan's dynamic "
        "tariff per kWh, in quarter-hours. The area *Netherlands* (`NL`) stays the day-ahead spot price with "
        "your own fees; the two never share days or a price history.",
        "- **All-in.** The tariff already contains Zonneplan's purchase fee, the energy tax and VAT, so VAT and "
        "energy tax are locked as *Included in the price* and nothing is added. Grid transfer stays a normal "
        "setting, off by default: Dutch network costs are per connection, not per kWh.",
        "- **Your prices.** They are read from the [Zonneplan](https://github.com/fsaris/home-assistant-zonneplan-one) "
        "integration, installed through HACS (its sensor *Current quarter hourly electricity tariff* must be "
        "on). SpotNav does not contact Zonneplan itself. While that integration is loaded and the "
        "area *Netherlands* has prices of its own, it shows a hint to choose *Netherlands – Zonneplan*. When it is down there is no fallback "
        "to the spot price: the days already held keep planning, and tomorrow waits.",
        "- **When.** Tomorrow's tariff appears in the afternoon, after the day-ahead auction. If the "
        "integration's forecast right after Zonneplan is added starts at the current quarter-hour rather than "
        "at midnight, that first day is not used (SpotNav never fills in prices it does not have): tomorrow's "
        "tariff is fetched in the afternoon, and charging is planned from midnight, when it becomes today's.",
        "",
        "Prices: Zonneplan. SpotNav is not affiliated with Zonneplan.",
        "",
    ]


# ---- the page -------------------------------------------------------------------------------------


def render() -> str:
    lines = [
        "<!-- Generated by tools/gen_supported.py from the integration's tables. Do not edit by hand. -->",
        "",
        "# Supported EV chargers, meters, batteries, heat pumps and cars",
        "",
        "SpotNav for Home Assistant works with the Home Assistant integrations listed here. The lists are "
        "generated from the tables the integration itself uses, so they always match the release.",
        "",
        "Each row says how it is known to work:",
        "",
        f"- **{TESTED}**: charging confirmed on real hardware.",
        f"- **{SET_UP}**: users have added it and SpotNav detected and configured it correctly, "
        "but nobody has yet confirmed that charging or current control works.",
        f"- **{DETECTED}**: SpotNav recognises the integration's entities and follows its documented "
        "behaviour, but nobody has confirmed it on real hardware yet.",
        "",
        f"Using one of these? Tell us whether it works: [Hardware tested]({ISSUES_URL}).",
        "",
        "Jump to: [inverters and batteries, the overview](#hybrid-inverters-and-home-batteries-overview), "
        "[chargers](#supported-ev-chargers), [grid meters](#supported-grid-meters), "
        "[house batteries](#supported-house-batteries), [heat pumps](#supported-heat-pumps), [vehicles](#supported-vehicles), "
        "[solar forecast](#supported-solar-forecast), [price areas](#supported-price-areas).",
        "",
        *overview_section(),
        *chargers_section(),
        *meters_section(),
        *batteries_section(),
        *heat_pumps_section(),
        *vehicles_section(),
        *forecast_section(),
        *prices_section(),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> None:
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
