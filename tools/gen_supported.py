"""Write `docs/supported.md` from the tables the integration itself uses.

The page is generated, never edited by hand: `python tools/gen_supported.py` rewrites it, and
`tests/test_supported_doc.py` fails when the committed file differs from what this script writes.

Sources, one per category:

* chargers: `execution/charger_profiles.py` (`_PROFILES`) plus the OCPP path in `config_flow/flow.py`;
* grid meters: `site/site_detection.py` (`METER_ROWS`, the Easee Equalizer, `UPDATE_BEHAVIOUR`);
* house batteries: `site/site_detection.py` (`BATTERY_ROWS`);
* vehicles: the recorded entity shapes in `tests/test_vehicle_catalogue.py`, read as data;
* solar forecast: the Energy dashboard contract read by `planning/hybrid_forecast.py`.

Display names are the only hand-written data here (`NAMES`); the test fails when a table gains an
integration that has no name.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.spotnav.execution import charger_profiles as cp  # noqa: E402
from custom_components.spotnav.site import site_detection as sd  # noqa: E402

OUTPUT = ROOT / "docs" / "supported.md"
VEHICLE_CASES = ROOT / "tests" / "test_vehicle_catalogue.py"
#: Relative to docs/supported.md as GitHub renders it (docs, main, blob, repository), so no owner name.
ISSUES_URL = "../../../issues/new?template=hardware_report.yml"

TESTED = "Tested: charging verified"
SET_UP = "Set up by users: detected and configured, charging not yet confirmed"
DETECTED = "Detected from the integration's source"

#: Hardware confirmed by the owner on real equipment. Everything else is read from the
#: integration's source. Keys: (category, home assistant domain).
TESTED_ON: dict[tuple[str, str], str] = {
    ("charger", "ocpp"): "Charge Amps HALO",
    ("meter", "sigen"): "Sigenergy",
    ("battery", "sigen"): "Sigenergy",
    ("vehicle", "kia_uvo"): "Kia EV6",
}

#: Hardware that users have set up successfully (SpotNav found and configured it) but where nobody
#: has confirmed a charge or a balanced current yet. Same keys as `TESTED_ON`.
SET_UP_ON: dict[tuple[str, str], str] = {
    ("meter", "dsmr"): "ESPHome P1 reader",
    ("charger", "easee"): "Easee Home",
    ("meter", "easee"): "Easee Equalizer",
    ("vehicle", "toyota"): "Subaru e-Outback through the Toyota integration",
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
    "sungrow_sungrow": "Sungrow",
    "modbus": "Sungrow Modbus package",
    "solax_modbus": "SolaX Modbus",
    "perific": "Perific",
    "solis_modbus": "Solis Modbus",
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


def duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{int(seconds)} s"


# ---- chargers -------------------------------------------------------------------------------------


def start_stop_text(profile: cp.PlatformProfile) -> str:
    path = profile.start_stop
    if path is None:
        return "None"
    if path.kind == cp.PATH_SWITCH:
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
    if profile.current_keys:
        return "Number entity"
    return "None"


def energy_text(profile: cp.PlatformProfile) -> str:
    parts = []
    if profile.energy_keys:
        parts.append("Lifetime energy")
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
    "A charge point with several connectors is added once per connector.",
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
        "a charger with no current control still follows the plan's start and stop times.",
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
    lines.append("")
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
        lines.append(f"Only devices from {row.device_manufacturer}.")
    if row.device_model:
        lines.append(f"Only devices whose model contains \"{row.device_model}\".")
    if row.voltage_any_device:
        lines.append("Voltage is taken from the inverter, which is on another device of the same integration.")
    lines.extend(_WARNING_TEXT[code] for code in row.warnings if code in _WARNING_TEXT)
    behaviour = [sd.UPDATE_BEHAVIOUR[p] for p in row.platforms if p in sd.UPDATE_BEHAVIOUR]
    if behaviour and behaviour[0].interval_s > 0:
        lines.append(f"Updates about every {duration(behaviour[0].interval_s)}.")
    return " ".join(lines)


def domains_text(domains: tuple[str, ...]) -> str:
    return ", ".join(f"`{d}`" for d in domains)


def meters_section() -> list[str]:
    rows = []
    for row in sd.METER_ROWS:
        names = " / ".join(dict.fromkeys(name_of(p) for p in row.platforms))
        rows.append(
            (
                f"{names} ({domains_text(row.platforms)})",
                meter_measurement(row),
                meter_signs(row),
                meter_notes(row),
                verified("meter", *row.platforms),
            )
        )
    rows.append(
        (
            f"Easee Equalizer ({domains_text(('easee',))})",
            "Direct phase current, all phases on one entity",
            "None needed",
            "The Equalizer balances load by itself and may fight active control.",
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
        names = " / ".join(dict.fromkeys(name_of(p) for p in row.platforms))
        if row.discharge_regex is not None:
            convention = "Charge and discharge are two entities, combined"
        elif row.inverted:
            convention = "Discharge-positive, negated"
        else:
            convention = "Charge-positive, used as it is"
        notes = f"Only devices whose model contains \"{row.device_model}\"." if row.device_model else ""
        rows.append((f"{names} ({domains_text(row.platforms)})", convention, notes, verified("battery", *row.platforms)))
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
        "refresh only asks Home Assistant to re-read the vehicle's entities; SpotNav never calls an "
        "integration's own force-update, so it does not wake the car.",
        "- **No range sensor, no vehicle.** A device without a distance-class range sensor, or with no "
        "device classes at all, is not detected.",
    ]
    lines.append("- Shapes the detection was checked to leave alone:")
    for case in misses:
        label, _, note = str(case["text"]).partition(": ")
        lines.append(f"  - `{label}`: {sentence(note)}" if note else f"  - `{label}`")
    lines.append("")
    return lines


# ---- solar forecast -------------------------------------------------------------------------------


def forecast_section() -> list[str]:
    rows = [
        (f"{name} (`{domain}`)", DETECTED) for domain, name in FORECAST_SOURCES
    ]
    return [
        "## Supported solar forecast",
        "",
        "The Hybrid strategy holds back grid energy when a solar forecast promises sun. It reads the "
        "forecast through the Energy dashboard's solar forecast contract, so any integration that "
        "feeds the Energy dashboard's solar forecast can be chosen on the site. SpotNav only reads the "
        "estimate an integration already has and never asks it to fetch. Forecasts in 15, 30 or 60 "
        "minute slots are summed per hour. Without a source, Hybrid plans exactly like Cheapest.",
        "",
        *table(("Integration", "Verification"), rows),
        "",
    ]


# ---- the page -------------------------------------------------------------------------------------


def render() -> str:
    lines = [
        "<!-- Generated by tools/gen_supported.py from the integration's tables. Do not edit by hand. -->",
        "",
        "# Supported EV chargers, meters, batteries and cars",
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
        f"Using one of these? Tell us whether it works: [Works with my hardware or does not]({ISSUES_URL}).",
        "",
        "Jump to: [chargers](#supported-ev-chargers), [grid meters](#supported-grid-meters), "
        "[house batteries](#supported-house-batteries), [vehicles](#supported-vehicles), "
        "[solar forecast](#supported-solar-forecast).",
        "",
        *chargers_section(),
        *meters_section(),
        *batteries_section(),
        *vehicles_section(),
        *forecast_section(),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> None:
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
