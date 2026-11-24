"""Where each integration's source lives. The one hand-kept table of the canary.

An integration SpotNav depends on that is missing here is reported as "not covered" (never silently skipped), and a
test fails when a domain the checks name has neither a source here nor an entry in `NOT_COVERED`.

`core` entries are folders of home-assistant/core (`homeassistant/components/<domain>`); a folder that has gone is
itself a finding (the integration left core). Entries with `libraries` also search the Python library the entity
keys are derived from (register names, sensor ids).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Source:
    #: `owner/name` on GitHub, or `core` for home-assistant/core.
    repo: str
    #: Further repositories searched together (the library behind the entities).
    libraries: tuple[str, ...] = ()
    #: A subfolder of the repository (a monorepo); `""`: the whole repository.
    path: str = ""


CORE = Source("core")


def _hacs(repo: str, *libraries: str) -> Source:
    return Source(repo, tuple(libraries))


UPSTREAMS: dict[str, Source] = {
    # Chargers, core.
    "wallbox": CORE,
    "peblar": CORE,
    "lektrico": CORE,
    "nrgkick": CORE,
    "nexblue": CORE,
    "ohme": CORE,
    "openevse": CORE,
    "v2c": CORE,
    "tesla_wall_connector": CORE,
    # Chargers, custom integrations.
    "easee": _hacs("nordicopen/easee_hass", "nordicopen/pyeasee"),
    "zaptec": _hacs("custom-components/zaptec"),
    "ocpp": _hacs("lbbrhzn/ocpp"),
    "goecharger_api2": _hacs("marq24/ha-goecharger-api2"),
    "goecharger_mqtt": _hacs("syssi/homeassistant-goecharger-mqtt"),
    "goecharger": _hacs("cathiele/homeassistant-goecharger"),
    "wattpilot": _hacs("mk-maddin/wattpilot-HA"),
    "hypervolt_charger": _hacs("gndean/home-assistant-hypervolt-charger"),
    "chargeamps": _hacs("hass-chargeamps/hass-chargeamps"),
    "alfen_wallbox": _hacs("leeyuentuen/alfen_wallbox"),
    "alfen_modbus": _hacs("ThaStealth/alfen_modbus"),
    "heidelberg_energy_control": _hacs("Schrolli91/heidelberg_energy_control"),
    "garo_wallbox": _hacs("sockless-coding/garo_wallbox"),
    "smaev": _hacs("alengwenus/ha-sma-ev-charger"),
    "smartevse": _hacs("dingo35/ha-SmartEVSEv3"),
    "defa_power": _hacs("Bebbssos/ha-defa-power"),
    "webasto_next_modbus": _hacs("tomwellnitz/Webasto-Next-Modbus"),
    "abb_terra_ac": _hacs("JernejHren/ABB-Terra-AC"),
    "myenergi": _hacs("CJNE/ha-myenergi", "CJNE/pymyenergi"),
    "monta": _hacs("nickknissen/hass-monta"),
    "pod_point": _hacs("mattrayner/pod-point-home-assistant-component"),
    "chargepoint": _hacs("mbillow/ha-chargepoint"),
    # Batteries, inverters.
    "sigen": _hacs("TypQxQ/Sigenergy-Local-Modbus"),
    "huawei_solar": _hacs("wlcrs/huawei_solar", "wlcrs/huawei-solar-lib"),
    "solaredge_modbus_multi": _hacs("WillCodeForCats/solaredge-modbus-multi"),
    "solax_modbus": _hacs("wills106/homeassistant-solax-modbus"),
    "victron_gx": Source("core", ("tomer-w/victron_mqtt",)),
    "victron_mqtt": _hacs("tomer-w/ha-victron-mqtt", "tomer-w/victron_mqtt"),
    "goodwe": Source("core", ("marcelblijleven/goodwe",)),
    "enphase_envoy": Source("core", ("pyenphase/pyenphase",)),
    "ferroamp": _hacs("henricm/ha-ferroamp"),
    "solarman": _hacs("davidrapan/ha-solarman"),
    "solis_modbus": _hacs("Pho3niX90/solis_modbus"),
    "sungrow": _hacs("KRoperUK/sungrow-hass"),
    "foxess_modbus": _hacs("nathanmarlor/foxess_modbus"),
    "zendure_ha": _hacs("Zendure/Zendure-HA"),
    "anker_solix": _hacs("thomluther/ha-anker-solix"),
    "ecoflow_cloud": _hacs("tolwi/hassio-ecoflow-cloud"),
    "marstek_local_api": _hacs("jaapp/ha-marstek-local-api"),
    "marstek_modbus": _hacs("ViperRNMC/marstek_venus_modbus"),
    "tesla_custom": _hacs("alandtse/tesla"),
    "powerwall": CORE,
    "fronius": CORE,
    "sma": Source("core", ("kellerza/pysma",)),
    "kostal_plenticore": CORE,
    "homevolt": CORE,
    "kostal_modbus_control": _hacs("mbuhansen/kostal_plenticore_battery_control"),
    "anker_solix_official": _hacs("anker-charging/ha-anker-solix-official"),
    "ef_powerocean_tcpmodbus": _hacs("MaxGrmm/EF-PowerOcean-TcpModbus"),
    "ecoflow_energy": _hacs("shuette42/ecoflow-energy-ha"),
    "omnibattery": _hacs("ffunes/Omnibattery"),
    "sonnenbatterie": _hacs("weltmeyer/ha_sonnenbatterie"),
    "alphaess_modbus": _hacs("senalse/ha-alphaess-modbus"),
    "alphaess": _hacs("CharlesGillanders/homeassistant-alphaESS"),
    "e3dc_rscp": _hacs("torbennehmer/hacs-e3dc"),
    "growatt_modbus": _hacs("0xAHA/Growatt_ModbusTCP"),
    "senec": _hacs("marq24/ha-senec-v3"),
    "saj_h2_modbus": _hacs("stanus74/home-assistant-saj-h2-modbus"),
    "givenergy_local": _hacs("cdpuk/givenergy-local", "dewet22/givenergy-modbus"),
    "pysmaplus": _hacs("littleyoda/ha-pysmaplus", "littleyoda/pysma"),
    "e3dc_rscp_connect": _hacs("tobias-terhaar/e3dc_rscp_connect"),
    "e3dc_maestro": _hacs("TommiG1/e3dc-maestro"),
    # Vehicles.
    "kia_uvo": _hacs("Hyundai-Kia-Connect/kia_uvo", "Hyundai-Kia-Connect/hyundai_kia_connect_api"),
    "tesla_fleet": CORE,
    "teslemetry": CORE,
    "tessie": CORE,
    # Pool heat pumps and spas (`pool/detect.py`, `spa/detect.py`; repositories from the research of 2026-11-23).
    "tuya": CORE,
    "tuya_local": _hacs("make-all/tuya-local"),
    "fairland": _hacs("siedi/ha-fairland"),
    "aqua_temp": _hacs("radical-squared/aquatemp"),
    "warmlink": _hacs("srbjessen/ha-warmlink"),
    "poolex_silverline": _hacs("christianreiss/ha-silverline"),
    "poolsync_custom": _hacs("ccpk1/AP_PoolSync"),
    "intellicenter": _hacs("jlvaillant/intellicenter"),
    "localtuya": _hacs("rospogrigio/localtuya"),
    "iaqualink": CORE,
    "screenlogic": CORE,
    "balboa": CORE,
    "smarttub": CORE,
    "gecko": _hacs("gazoodle/gecko-home-assistant"),
    "bestway": _hacs("cdpuk/ha-bestway"),
    "intex_spa": _hacs("mathieu-mp/homeassistant-intex-spa"),
    "mspa": _hacs("DTekNO/mspa-homeassistant"),
    "arctic_spa": _hacs("jensenbox/ha-arctic-spa"),
    "joyonway": _hacs("alexbde/ha-joyonway"),
    # Saunas (`sauna/detect.py`; read 2026-11-23).
    "huum": CORE,
    "saunum": CORE,
    "harvia_sauna": _hacs("WiesiDeluxe/ha-harvia-sauna"),
    "tylo_sauna": _hacs("skyer/home-assistant-tylo-sauna"),
    "sentio": _hacs("astrandb/sentio"),
    # Meters.
    "shelly": CORE,
    "homewizard": CORE,
    "tibber": CORE,
    "dsmr": CORE,
    "dsmr_reader": CORE,
    "p1_monitor": CORE,
    "edl21": CORE,
    "zha": Source("core", ("zigpy/zha",)),
    "amshan": _hacs("toreamun/amshan-homeassistant"),
    "cozify_han": _hacs("cozify/han-home-assistant"),
}

#: Integrations SpotNav names that the canary does not read, and why. Shared platforms hold unrelated devices, and
#: some have no public source to clone.
NOT_COVERED: dict[str, str] = {
    "mqtt": "shared platform (devices of any maker)",
    "esphome": "shared platform (devices of any maker)",
    "modbus": "shared platform (devices of any maker)",
    "evcc_intg": "external controller: SpotNav only warns about it",
    "openwb2mqtt": "external controller: SpotNav only warns about it",
    "andersen_ev": "no command SpotNav uses",
    "ctek_nanogrid_air": "no command SpotNav uses",
    "elli_charger_ha": "no command SpotNav uses",
    "blue_current": "no command SpotNav uses",
    "webastoconnect": "excluded from charger detection",
    "webel_gctrl": "excluded from charger detection",
    "juicenet": "excluded from charger detection",
    "smappee": "excluded from charger detection",
    "bitvis": "no public source known",
    "victron": "no public source known",
    "solaredge_modbus": "name shared by a core and a custom integration, see solaredge_modbus_multi",
    "onep1": "no public source known",
    "perific": "no public source known",
    "discovergy": "update interval only (no key or service)",
    "foxess": "update interval only (no key or service)",
    "growatt_server": "update interval only (no key or service)",
    "sma_ennexos": "update interval only (no key or service)",
    "solaredge": "update interval only (no key or service)",
    "solis": "update interval only (no key or service)",
    "control_my_spa": "spa found by its domain only, watched; repository not verified",
}
