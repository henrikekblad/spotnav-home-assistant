"""Constants for SpotNav charging control."""

DOMAIN = "spotnav"
CONF_MODE = "mode"
CONF_CHARGE_CONTROL = "charge_control"
CONF_CURRENT_LIMIT = "current_limit"
# Optional per charger: how its current may be set (see CURRENT_CONTROL_CHANGE_CONFIGURATION).
CONF_CURRENT_CONTROL = "current_control"
# Optional per charger: a `sensor` with the charger's cumulative energy register (kWh). When set,
# energy already delivered toward the departure is subtracted from `requested_kwh`, so a replan
# does not buy it twice (baseline: `auto_settings.EnergyBaseline`).
CONF_ENERGY_REGISTER_ENTITY = "energy_register_entity"
CONF_WEBHOOK_ID = "webhook_id"
MODE_OCPP = "ocpp"
MODE_GENERIC = "generic"
# A charger found from its device: the integration's entities are matched by platform and key
# (`execution/charger_profiles.py`, `config_flow/charger_detection.py`).
MODE_DETECTED = "detected"

# Per charger, written by the detected flow and editable later: the integration (`platform`) the
# charger belongs to, how it is started and stopped, which sensor says it is charging and with which
# values, and the sensors that measure its current. All optional; a charger without them behaves
# exactly as before (the switch is the charging state, nothing is read from the charger).
CONF_CHARGER_PLATFORM = "charger_platform"
# `{"kind": "switch"|"select"|"buttons"|"easee", ...}`, see `execution/charger_adapter.py`.
CONF_CONTROL_PATH = "control_path"
# `{"entity_id": "sensor....", "charging_values": ["charging", ...]}`.
CONF_CHARGING_STATE = "charging_state"
# One entity with L1/L2/L3 attributes, or up to three phase sensors, in A or mA.
CONF_CHARGER_CURRENT_ENTITIES = "charger_current_entities"
# Whether the energy register is a per-session one (it resets), chosen knowingly.
CONF_ENERGY_REGISTER_IS_SESSION = "energy_register_is_session"

# OCPP control identity (charge point id and connector number), derived once from the Charge
# Control entity (`ocpp_identity.resolve_target`) and persisted, so a renamed entity cannot
# silently redirect writes. Nothing else may parse an entity id to pick a connector.
CONF_OCPP_CHARGE_POINT_ID = "ocpp_charge_point_id"
CONF_OCPP_CONNECTOR_ID = "ocpp_connector_id"
# Set when no unique target could be established: data is kept, the current is recorded but
# not applied, nothing is guessed.
CONF_OCPP_TARGET_UNRESOLVED = "ocpp_target_unresolved"

CONFIG_ENTRY_VERSION = 1
# The one `CONF_CURRENT_CONTROL` value that changes behaviour: set the current through the OCPP
# `configure` service (`ChangeConfiguration` on `AssignedCurrent`), with no charging profile.
# Chosen explicitly per charger, never inferred; absent means record the request, never apply it.
CURRENT_CONTROL_CHANGE_CONFIGURATION = "change_configuration"
# Set the current through `number.set_value` on the charger's current-limit number, under the
# platform's write policy (`execution/charger_profiles.py`). Explicit opt-in, like the one above.
CURRENT_CONTROL_NUMBER = "number"
# Set the current through Easee's own services (`easee.set_charger_dynamic_limit`).
CURRENT_CONTROL_EASEE = "easee_dynamic_limit"
PLATFORMS = ["binary_sensor", "button", "date", "number", "select", "sensor", "switch", "time"]

MAX_SCHEDULE_PERIODS = 8

# Every config entry is either a charger (the default kind) or a site capacity group.
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_CHARGER = "charger"
ENTRY_TYPE_SITE = "site"
# Not a stored entry kind: resolves or dismisses a vehicle whose state of charge detection
# refuses to guess, records the decision in the discovery-decision store, and finishes.
ENTRY_TYPE_RESOLVE_VEHICLE = "resolve_vehicle"

# Site config keys (config/options flow only).
CONF_SITE_ENABLED = "site_enabled"
# Per-site opt-in for writing a charger from the regulator's decisions. Off by default and
# separate from `ACTIVE_CONTROL_READY` in `site/site_capacity.py`; both must hold before any write.
CONF_ACTIVE_CONTROL_ENABLED = "active_control_enabled"
CONF_MAIN_FUSE_A = "main_fuse_a"
CONF_SAFETY_MARGIN_A = "safety_margin_a"
CONF_MEASUREMENT_MODE = "measurement_mode"
CONF_CHARGER_ENTRY_IDS = "charger_entry_ids"
CONF_PHASE_WIRING = "phase_wiring"  # {charger_entry_id: {"phases": 1|3, "phase": "L1"|"L2"|"L3"|None}}
CONF_DIRECT_ENTITIES = "direct_entities"  # {"L1": entity_id, "L2": ..., "L3": ...}
# {"L1": {"power": id, "voltage": id, optional "power_export": id (P = power - power_export, for a
# meter that reports import and export as two entities), optional "reactive_power", "apparent_power"
# and "current": id}, ...}. Without any of the last three the phase current is estimated from power.
CONF_DERIVED_ENTITIES = "derived_entities"
# The site's grid current is reported signed (export negative): the fuse carries |I|, so read it as
# a magnitude instead of rejecting it. Applies to `CONF_DIRECT_ENTITIES`, the stored current source
# and derived mode's optional current entities.
CONF_SITE_CURRENT_SIGNED = "site_current_signed"
# The grid power entities are export-positive (Huawei, SolarEdge Modbus, GoodWe ...): negate them so
# the site sees import positive.
CONF_GRID_POWER_INVERTED = "grid_power_inverted"
# The site's total current as a serialized `measurement_source.PhaseMeasurementSource`; takes
# precedence over `CONF_DIRECT_ENTITIES` (a plain {"L1": entity_id, ...} dict the entity picker writes).
CONF_SITE_CURRENT_SOURCE = "site_current_source"
CONF_MAX_AGE_S = "max_age_s"

# Per-charger sub-key of `CONF_PHASE_WIRING`: an optional measured per-phase current source,
# serialized like `CONF_SITE_CURRENT_SOURCE`. Absent is normal and credits nothing to headroom.
CONF_MEASURED_CURRENT_SOURCE = "measured_current_source"

# Optional, purely diagnostic home-battery input (`BatteryYieldEstimate`), never a safe limit.
# The aggregate power entity has a verified sign (positive = charging); the per-phase source has
# only a magnitude, so it is used only alongside the aggregate as a direction confirmation.
CONF_BATTERY_PER_PHASE_SOURCE = "battery_per_phase_source"
CONF_BATTERY_AGGREGATE_POWER_ENTITY = "battery_aggregate_power_entity"
# A second, non-negative entity for a battery that reports charge and discharge separately (SMA,
# Growatt cloud): the aggregate entity is then the charge power and this one the discharge power.
CONF_BATTERY_DISCHARGE_POWER_ENTITY = "battery_discharge_power_entity"
# The battery's aggregate power is discharge-positive (Tesla, Fronius, Enphase, GoodWe ...): negate it
# so positive means charging.
CONF_BATTERY_POWER_INVERTED = "battery_power_inverted"

MEASUREMENT_MODE_DIRECT = "direct_phase_current"
MEASUREMENT_MODE_DERIVED = "derived_phase_current"

# Minimum change, in amps, before the charger is rewritten; wider than one step to stop dithering
# (see `site/regulator_damping.py`).
CONF_REGULATOR_DEADBAND_A = "regulator_deadband_a"
DEFAULT_REGULATOR_DEADBAND_A = 2.0

# Seconds a change must be continuously justified before it is written; longer than the 30 s
# recompute interval, so a value surviving one recompute is never written.
CONF_REGULATOR_DWELL_S = "regulator_dwell_s"
DEFAULT_REGULATOR_DWELL_S = 60.0

DEFAULT_MAX_AGE_S = 120.0

DEFAULT_MIN_CURRENT_A = 6.0

SITE_RECOMPUTE_INTERVAL_S = 30

SITE_PLATFORMS = ["binary_sensor", "sensor"]

# Opt-in per site: whether `execution/yield_stepping.py`'s `YieldStepper` is consulted when applying.
CONF_YIELD_STEPPING_ENABLED = "yield_stepping_enabled"
DEFAULT_YIELD_STEPPING_ENABLED = False

# Hard ceiling in amps for a yield step or probe. Defaults to 1.15x the main fuse; must exceed it.
CONF_YIELD_CEILING_A = "yield_ceiling_a"
YIELD_CEILING_FUSE_FACTOR = 1.15
# Largest yield ceiling as a multiple of the main fuse (1.25 x In is a gG fuse's conventional
# non-fusing current), enforced at run time as well as in the options flow.
YIELD_CEILING_MAX_FUSE_FACTOR = 1.25


# Per-site solar priority: `car_first` (default) or `battery_first`.
CONF_SOLAR_PRIORITY = "solar_priority"
SOLAR_PRIORITY_CAR_FIRST = "car_first"
SOLAR_PRIORITY_BATTERY_FIRST = "battery_first"
SOLAR_PRIORITY_CHOICES = (SOLAR_PRIORITY_CAR_FIRST, SOLAR_PRIORITY_BATTERY_FIRST)
DEFAULT_SOLAR_PRIORITY = SOLAR_PRIORITY_CAR_FIRST


# Per-site config-entry ids that hybrid's forecast adapter (`planning/hybrid_forecast.py`) reads an
# hourly PV forecast from (entries implementing the Energy dashboard's `async_get_solar_forecast`).
# Absent or empty means no forecast, and hybrid is exactly cheapest.
CONF_SOLAR_FORECAST_ENTRIES = "solar_forecast_entries"


def max_yield_ceiling_a(main_fuse_a: float) -> float:
    """The ceiling no configuration may exceed; see `YIELD_CEILING_MAX_FUSE_FACTOR`."""
    return main_fuse_a * YIELD_CEILING_MAX_FUSE_FACTOR


def default_yield_ceiling_a(main_fuse_a: float) -> float:
    """The yield ceiling's default, used when a site has not configured one."""
    return main_fuse_a * YIELD_CEILING_FUSE_FACTOR
