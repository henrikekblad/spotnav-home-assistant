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
  from outside), and the Modbus integration's `user_work_mode` "trade_mode" ("AI Optimization") and `manual`
  (jaapp/ha-marstek-local-api `624881c`, ViperRNMC/marstek_venus_modbus 2026.10.1). evcc drives Venus batteries
  over Modbus (its `marstek-venus-e` template), a planner rather than an aggregator [V]; no aggregator is known.
* The integration domains were read from each integration's `manifest.json` on 2026-11-14: core `tibber`,
  `homevolt`, `goodwe`, `enphase_envoy`, `powerwall`, `victron_gx`; custom `checkwatt` (faanskit/ha-checkwatt
  0.3.0), `greenely` (linsvensson/sensor.greenely 2.1.3), `omnibattery` (ffunes/Omnibattery 1.6.1), `ferroamp` (henricm/ha-ferroamp 1.18.0),
  `sonnenbatterie` (weltmeyer/ha_sonnenbatterie 2026.11.1), and the adapters' own (`adapters.py`). No Home
  Assistant integration was found for Pixii; Sungrow is mostly read through YAML Modbus (no config entry).

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
    Coexistence("sonnen", "sonnen", ("sonnenbatterie",), "sonnen app", ("Tibber VPP [V]",)),
    Coexistence("solax", "SolaX", ("solax_modbus",), "SolaX Cloud", ("Tibber [U]",)),
    # The other plugins of `solax_modbus` that have an adapter, after SolaX so that the integration alone still
    # names SolaX. Their own modes are the ones the plugin's mode select lists.
    Coexistence("sofar", "Sofar", ("solax_modbus",), "Sofar: time-of-use, timing and peak-cut modes"),
    Coexistence("growatt", "Growatt", ("solax_modbus", "growatt_server"),
                "Growatt: battery-first and grid-first time slots"),
    Coexistence("solinteg", "Solinteg", ("solax_modbus",), "Solinteg app: economic and time-of-use modes",
                ("CheckWatt [F]", "Samvolt [F]", "Enequi [F]")),
    Coexistence("foxess", "FoxESS", ("foxess_modbus",), "FoxESS: Feed-in First, Back-up and Peak Shaving work modes",
                ("1KOMMA5 Heartbeat (1K5 units) [U]",)),
    Coexistence("solis", "Solis", ("solis_modbus",), "SolisCloud: Solis AI and time-of-use",
                ("Enequi [F]", "Tibber [U]")),
    Coexistence("marstek", "Marstek", ("marstek_local_api", "marstek_modbus"),
                "Marstek app: AI mode and Manual time slots"),
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
)


def outside_integrations(domains: frozenset[str] | set[str]) -> tuple[OutsideIntegration, ...]:
    """The integrations of the table among the set-up config entries' `domains`, in the table's order."""
    return tuple(row for row in OUTSIDE_INTEGRATIONS if row.domain in domains)
