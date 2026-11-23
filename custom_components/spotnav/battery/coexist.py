"""Who else usually runs a home battery: the per-brand coexistence table and the integrations that run batteries.

SpotNav is one controller among several. In Sweden most home batteries are steered from a cloud: the maker's own
optimiser (Sigen AI mode, FusionSolar time-of-use, iSolarCloud, SEMS, Enlighten), or an aggregator that sells the
battery's flexibility (CheckWatt, Greenely, Tibber Grid Rewards, Ferroamp with Ntricity). Two writers on one battery
fight each other, so SpotNav's battery parts default to **watch-only** for every brand: they plan in shadow as a
benchmark and write nothing. The person's opt-in (`gate.py`, off and "shadow only" by default) lifts that for one
site's battery after its verification passed; this table says what usually runs each brand, so a reason or a notice
can name it.

Sources (read 2026-11-14, `plans/research_integrations/commercial_energy_gap_2026-11-14.md` sections 1-2, its own
markers kept: [V] verified on the vendor's or the aggregator's page, [F] forum or third party, [U] unverified):

* CheckWatt takes "most common brands" through its own CM10 gateway [F]; Greenely's partner pilot (June 2025) names
  Huawei, Sungrow, GoodWe and Sigenergy, Pixii first [V]; Tibber Grid Rewards runs Homevolt first and "all
  batteries supported by Tibber" (Huawei, KOSTAL, SAJ, Solis, SolaX per a LinkedIn post [U]); sonnen joins Tibber's
  VPP [V]; Ferroamp pools with Ntricity and Varberg Energi for FCR-D [V]; a Sigenergy plant "can be taken over by a
  VPP operator" instead of AI mode [F].
* Sofar's, Growatt's and Solinteg's own modes are the options of each plugin's mode select in `solax_modbus`
  (wills106 `4acd644`: "Energy Storage Mode", "Priority", "Working Mode"), not an app's names. Solinteg's
  aggregators are the third-party EMS a Swedish retailer lists as compatible (KP Energy [F],
  `plans/research_integrations/solinteg_m2ht_2026-11.md`).
* FoxESS's own modes are the options of `foxess_modbus`'s `work_mode` select (nathanmarlor `94252f0`). That
  1KOMMA5's Heartbeat steers its `1K5-HI` units is an assumption of `plans/v3_foxess.md` [U].
* Solis's own optimiser is SolisCloud's "Solis AI" / "Smart Strategy" (evcc's `solis-hybrid-s` template asks for it
  to be turned off for local control [F]); its local modes are the options of `solis_modbus`'s "Storage Mode"
  select (Pho3niX90 v4.5.0). Enequi (QuiPower) running a Solis S6-EH3P is a field report (2026-11-22) [F].
* Marstek's own modes are the local API's "AI" (its optimiser), "Manual" (time slots) and "Passive" (a setpoint
  from outside; the official `marstek` has `device_mode` `auto`, `ai`, `manual`, `passive` and `ups`, MarstekEnergy/ha_marstek
  `6394e6a`), and the Modbus integration's `user_work_mode` "trade_mode" ("AI Optimization") and `manual`
  (jaapp/ha-marstek-local-api `624881c`, ViperRNMC/marstek_venus_modbus 2026.10.1). evcc drives Venus batteries
  over Modbus (its `marstek-venus-e` template), a planner rather than an aggregator [V]; no aggregator is known.
* EcoFlow's own modes are the options of `ef_powerocean_tcpmodbus`'s `operating_mode` (`self_consumption`, `ai`) and of
  `ecoflow_energy`'s `work_mode` (`self_use`, `ai_schedule`; MaxGrmm `56331de`, shuette42 `771b646`). EcoFlow's planned
  Tibber integration and CheckWatt readiness (Elmia Solar 2026) are announcements, not live [U], so no aggregator is
  named (`plans/research_integrations/ecoflow_control_2026-11-23.md`).
* Kostal's own modes are the options of core `kostal_plenticore`'s `devices:local` select (`SmartBatteryControl`,
  `TimeControl`) and its strategy switch (automatic, automatic economical); Tibber's bridge for Kostal (November
  2025) needs the inverter's "External via protocol (Modbus TCP)" setting, which only one writer may use [U]
  (`plans/research_integrations/kostal_control_2026-11-23.md`). `kostal_modbus_control` (mbuhansen 4.3.0) is the
  battery-control integration for it, set up for control but also for readings.
* Fronius's own control is what the inverter's web UI holds and Home Assistant cannot see: battery time-of-use
  schedules, the Energy Cost Assistant in Solar.web and a grid operator's dynamic power reduction, all of which outrank or
  precede Modbus limits ([EVCC] requirement text in `plans/research_integrations/fronius_control_2026-11-23.md`). No
  aggregator is named: no Nordic one was found.
* Homevolt's Smart Control and Grid Rewards are Tibber's cloud schedules, which the unit replaces its local ones with
  while its `local_mode` is off (tibber/homevolt-local-api-doc `58af3c4`, core `homevolt` `42f4a32`); the unit has no
  self-consumption mode of its own, so the battery is SpotNav's to read as free only while `local_mode` is on.
* Deye's and Sunsynk's own control is the inverter's six time-of-use programmes, which the vendor's app and cloud
  write and which `solarman`'s `Time of Use` select and kellerza's `Use Timer` switch show as on or off
  (davidrapan `84982c5`, kellerza `49efe3f`); no aggregator is named: none was found in the Nordics
  (`plans/research_integrations/deye_sunsynk_control_2026-11-23.md`).
* Anker's own modes are the options of `anker_solix_official`'s `operating_mode` select (`tou_mode`, `custom_mode`,
  `smart_mode`, `dynamic_pricing`, `socket_overlay_mode`: what the Anker app sets, anker-charging `a9930d7`). Below its
  backup reserve a Max AC grid-charges at its configured input power in any mode (Anker's own answer in the
  integration's issue 152). No aggregator is named: none was found (`plans/research_integrations/anker_solix_control_2026-11-23.md`).
* Zendure's own manager is the app's HEMS (`hems_state`: the device takes its energy reports and its own loop overrides
  local writes after about ten seconds) and, in `zendure_ha`, the Zendure Manager (`zendure_manager_operation`, every
  option but `off` writes the limits from the P1 meter), Zendure-HA `e67b04e`. Ffunes/Omnibattery drives Zendure too and is
  in the outside integrations. No aggregator is named: none was found
  (`plans/research_integrations/zendure_control_2026-11-23.md`).
* sonnen's own modes are the options of `sonnenbatterie`'s `operating_mode` (`2` automatic, `10` time-of-use, `11`
  optimization; weltmeyer `9441b4a`); a mode change is refused while a virtual power plant is active, which is the
  Tibber VPP (sonnenVPP, since 2024 in Sweden [V]) running the battery
  (`plans/research_integrations/sonnen_control_2026-11-23.md`).
* Alpha ESS's own control is the app's two time-window sets (`charging_time_period_control`) and the vendor's dispatch block,
  whose registers a supplier's cloud writes too: evcc issue 36201 reads a dispatch nobody at the house started on a Frank
  Energie battery (start 1, mode 2, 1800 s) [F]; no Nordic aggregator was found. The integration domains are senalse's
  `alphaess_modbus` (v1.15.0) and CharlesGillanders' cloud `alphaess` (v0.9.0)
  (`plans/research_integrations/alphaess_control_2026-11-23.md`).
* E3/DC's own control is the portal's AI360 and Smart-Charge (price-driven charging that "overrides external charge
  commands", the E3DC Maestro README and evcc's template note), the idle periods and the weather-regulated charge;
  none of them is readable from Home Assistant. `e3dc_maestro` (TommiG1 `72c9754`) is a controller that calls
  `e3dc_rscp`'s services and exists only to drive the battery: an outside integration that runs it. No aggregator is
  named: no Nordic E3/DC owner was found (`plans/research_integrations/e3dc_control_2026-11-23.md`).
* Growatt's own control through `growatt_modbus` (0xAHA `29d6b2b`) is the VPP remote power control, whose registers stay on after
  its timed command ends, the time-of-use slots (newer firmware reverts a slot write) and ShinePhone's or the portal's
  schedules, which "take effect regardless" and are invisible over Modbus (issues 373, 448, 460); the ARK and APX
  batteries also pull a little from the grid by themselves (issue 492). No aggregator is named
  (`plans/research_integrations/growatt_control_2026-11-23.md`).
* SENEC's own control is the cloud's (SENEC.Cloud tariffs, the Backup Power Pro reserve, the peak-shaving helper) and the unit's
  own states, which grid-charge by themselves (initial, maintenance, equalisation, desulfation, safety and emergency charge;
  `system_state`); one owner's charge switch came back on ten seconds after it was turned off (marq24/ha-senec-v3 issue 115).
  No aggregator is named: none was found, and no Nordic SENEC owner either
  (`plans/research_integrations/senec_control_2026-11-23.md`).
* SAJ's own control is the app's: the `AppMode` options `12` (AI saving) and `10` (peak shaving) are the vendor's optimisers,
  `1` the time-of-use slots, and the app's limits stay in force after Home Assistant has written the passive entities (stanus74
  issue 162); EKD's Ampere.IQ rewrites the same registers on an Ampere.StoragePro (issue 18). Tibber lists SAJ among the batteries
  it runs, from a LinkedIn post only [U]; no other aggregator was found
  (`plans/research_integrations/saj_control_2026-11-23.md`).
* SMA's own control is the Sunny Portal's Forecast-based Charging, the Sunny Home Manager's energy management (both must be
  off for any outside control, evcc's template and SMA's Modbus document say) and the inverter's own protective grid charge at
  a very low level (10-15 minutes, the SMA manual); none shows in a Home Assistant entity. No aggregator was found, and no
  Nordic SMA battery owner either (`plans/research_integrations/sma_control_2026-11-23.md`).
* The integration domains were read from each integration's `manifest.json` on 2026-11-14: core `tibber`,
  `homevolt`, `goodwe`, `enphase_envoy`, `powerwall`, `victron_gx`; custom `checkwatt` (faanskit/ha-checkwatt
  0.3.0), `greenely` (linsvensson/sensor.greenely 2.1.3), `omnibattery` (ffunes/Omnibattery 1.6.1), `ferroamp` (henricm/ha-ferroamp 1.18.0),
  `sonnenbatterie` (weltmeyer/ha_sonnenbatterie 2026.11.1, read on 2026-11-23 at `9441b4a`, 2026.11.6), and the adapters' own (`adapters.py`). No Home
  Assistant integration was found for Pixii; Sungrow is mostly read through YAML Modbus (no config entry): its row has no platform, as `modbus`
  is every Modbus device's, and is found by the brand of the facts (`adapters.SUNGROW`, chosen by unique id).

Pure: no Home Assistant import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal


@dataclass(frozen=True, slots=True)
class Coexistence:
    """One brand: its integrations, what usually runs it besides SpotNav, and SpotNav's default."""

    brand: str
    label: str
    #: The Home Assistant integration domains the brand is read through (empty: none found).
    platforms: tuple[str, ...]
    #: Its own optimiser, as the maker's app names it.
    own_optimiser: str
    #: Aggregators known to run this brand in Sweden, with the research's marker.
    aggregators: tuple[str, ...] = ()
    #: SpotNav only watches it unless a later opt-in says otherwise (every brand in this release).
    watch_only: bool = True


COEXISTENCE: Final[tuple[Coexistence, ...]] = (
    Coexistence("sigenergy", "Sigenergy", ("sigen",), "Sigen app: AI mode, time-of-use",
                ("Greenely (pilot) [V]", "a VPP operator [F]")),
    Coexistence("huawei", "Huawei", ("huawei_solar",), "FusionSolar: time-of-use",
                ("CheckWatt [F]", "Greenely (pilot) [V]", "Tibber [U]")),
    Coexistence("sungrow", "Sungrow", (), "iSolarCloud", ("CheckWatt [F]", "Greenely (pilot) [V]")),
    Coexistence("goodwe", "GoodWe", ("goodwe",), "SEMS: eco modes", ("CheckWatt [F]", "Greenely (pilot) [V]")),
    Coexistence("pixii", "Pixii", (), "Pixii Home", ("Greenely [V]",)),
    # Emaldo has no Home Assistant integration (a gh search found none, 2026-11-23): its own hardware is run from its
    # cloud, by its own optimiser and its Grid Rewards guarantee plan, sold with Bixia (since July 2026, [V] in
    # `plans/research_integrations/commercial_energy_gap_2026-11-14.md`).
    Coexistence("emaldo", "Emaldo", (), "Emaldo app: its own optimiser and Grid Rewards plan",
                ("Bixia, Grid Rewards guarantee plan [V]",)),
    Coexistence("ferroamp", "Ferroamp", ("ferroamp",), "EnergyHub", ("Ntricity / Varberg Energi, FCR-D [V]",)),
    Coexistence("homevolt", "Homevolt", ("homevolt",), "Tibber app: Smart Control", ("Tibber Grid Rewards [V]",)),
    Coexistence("sonnen", "sonnen", ("sonnenbatterie",), "sonnen app: time-of-use and its own optimization mode", ("Tibber VPP [V]",)),
    Coexistence("solax", "SolaX", ("solax_modbus",), "SolaX Cloud", ("Tibber [U]",)),
    # The other plugins of `solax_modbus` that have an adapter, after SolaX so that the integration alone still
    # names SolaX. Their own modes are the ones the plugin's mode select lists.
    Coexistence("sofar", "Sofar", ("solax_modbus",), "Sofar: time-of-use, timing and peak-cut modes"),
    Coexistence("growatt", "Growatt", ("solax_modbus", "growatt_server", "growatt_modbus"),
                "Growatt: battery-first and grid-first time slots"),
    Coexistence("solinteg", "Solinteg", ("solax_modbus",), "Solinteg app: economic and time-of-use modes",
                ("CheckWatt [F]", "Samvolt [F]", "Enequi [F]")),
    Coexistence("foxess", "FoxESS", ("foxess_modbus",), "FoxESS: Feed-in First, Back-up and Peak Shaving work modes",
                ("1KOMMA5 Heartbeat (1K5 units) [U]",)),
    Coexistence("solis", "Solis", ("solis_modbus",), "SolisCloud: Solis AI and time-of-use",
                ("Enequi [F]", "Tibber [U]")),
    Coexistence("marstek", "Marstek", ("marstek_local_api", "marstek_modbus", "marstek"),
                "Marstek app: AI mode and Manual time slots"),
    Coexistence("ecoflow", "EcoFlow", ("ef_powerocean_tcpmodbus", "ecoflow_energy"),
                "EcoFlow app: Self-use, AI schedule and backup reserve"),
    Coexistence("kostal", "Kostal", ("kostal_plenticore", "kostal_modbus_control"),
                "Kostal: smart battery control, time control and the automatic strategies",
                ("Tibber, over its bridge [U]",)),
    Coexistence("fronius", "Fronius", ("fronius",),
                "Fronius web UI: time-of-use schedules, Energy Cost Assistant and dynamic power reduction"),
    # Not "mqtt", the platform kellerza's add-on is read through: it is every MQTT device's, and the row is found by the
    # brand of the facts there.
    Coexistence("deye", "Deye / Sunsynk", ("solarman",),
                "Deye Cloud or SunSynk Connect: time-of-use programmes and work modes"),
    Coexistence("anker", "Anker SOLIX", ("anker_solix_official",),
                "Anker app: time-of-use, smart, custom and dynamic-pricing modes, and the backup reserve"),
    Coexistence("alphaess", "Alpha ESS", ("alphaess_modbus", "alphaess"),
                "AlphaCloud app: grid-charge and discharge time windows, and supplier dispatch over the same registers",
                ("Frank Energie VPP (Netherlands) [F]",)),
    Coexistence("e3dc", "E3/DC", ("e3dc_rscp", "e3dc_rscp_connect"),
                "E3/DC portal: AI360, Smart-Charge, idle periods and weather-regulated charge"),
    Coexistence("senec", "SENEC", ("senec",),
                "SENEC app and SENEC.Cloud: tariff charging, the backup reserve, peak shaving and the unit's own maintenance charges"),
    Coexistence("saj", "SAJ", ("saj_h2_modbus",),
                "SAJ app (Elekeeper, eSolar): AI saving, peak shaving, time-of-use and the app's charge and discharge limits",
                ("Tibber, named for SAJ [U]",)),
    Coexistence("sma", "SMA", ("sma", "pysmaplus"),
                "Sunny Portal Forecast-based Charging, the Sunny Home Manager's energy management and the inverter's protective grid charge"),
    Coexistence("zendure", "Zendure", ("zendure_ha",),
                "Zendure app: HEMS, and the integration's own Zendure Manager"),
    Coexistence("solaredge", "SolarEdge", ("solaredge_modbus_multi", "solaredge_modbus"), "mySolarEdge: storage profiles"),
    Coexistence("victron", "Victron", ("victron_gx",), "VRM: Dynamic ESS"),
    Coexistence("enphase", "Enphase", ("enphase_envoy",), "Enlighten: storage modes"),
    Coexistence("tesla", "Tesla Powerwall", ("powerwall",), "Tesla app: time-based control, Storm Watch"),
)


def coexistence_for(brand: str | None = None, platform: str | None = None) -> Coexistence | None:
    """The row of a brand (`facts.BatteryFacts.brand`) or, failing that, of an integration domain."""
    for row in COEXISTENCE:
        if brand is not None and row.brand == brand:
            return row
    for row in COEXISTENCE:
        if platform is not None and platform in row.platforms:
            return row
    return None


#: How much an integration's presence says. `runs`: it exists for a service that drives the battery, so its
#: presence alone makes the battery watch-only. `hint`: it is set up by many who use it for prices or readings
#: only, so it never marks a battery by itself; it is named as the likely source when the battery's own settings
#: or behaviour mark it.
Strength = Literal["runs", "hint"]


@dataclass(frozen=True, slots=True)
class OutsideIntegration:
    domain: str
    label: str
    strength: Strength


OUTSIDE_INTEGRATIONS: Final[tuple[OutsideIntegration, ...]] = (
    OutsideIntegration("checkwatt", "CheckWatt", "runs"),
    OutsideIntegration("tibber", "Tibber", "hint"),
    OutsideIntegration("greenely", "Greenely", "hint"),
    # ffunes/Omnibattery (manifest `domain`, 1.6.1, `d74591b`): a controller for Marstek, Zendure, Anker Solix, Sessy
    # and Hoymiles batteries (a PD zero-export loop, grid charging, time slots). It exists only to drive a battery, so
    # its being set up marks the battery as run by someone else, as CheckWatt's does.
    OutsideIntegration("omnibattery", "Omnibattery", "runs"),
    # mbuhansen/kostal_plenticore_battery_control (manifest `domain`, 4.3.0, `3bd84ac`): the Modbus control integration for
    # a Kostal Plenticore, which also reads the inverter, so its presence is only a hint.
    OutsideIntegration("kostal_modbus_control", "Kostal Modbus Control", "hint"),
    # senalse/ha-alphaess-modbus (`b256f55`, v1.15.0): reads the inverter and also has the force charge, force export and
    # dispatch switches, so its presence is only a hint (the `dispatch_start` mode names it when it runs).
    OutsideIntegration("alphaess_modbus", "Alpha ESS Modbus", "hint"),
    # TommiG1/e3dc-maestro (manifest `domain`, 0.6.0-rc.2, `72c9754`): a price-driven controller on top of `e3dc_rscp`, which
    # writes its power limits and charge modes. It exists to drive the battery, so its being set up marks it as run by
    # someone else.
    OutsideIntegration("e3dc_maestro", "E3DC Maestro", "runs"),
)


def outside_integrations(domains: frozenset[str] | set[str]) -> tuple[OutsideIntegration, ...]:
    """The integrations of the table among the set-up config entries' `domains`, in the table's order."""
    return tuple(row for row in OUTSIDE_INTEGRATIONS if row.domain in domains)
