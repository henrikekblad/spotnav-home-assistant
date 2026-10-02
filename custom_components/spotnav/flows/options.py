"""The options flows: editing a charger's entities and a site's capacity settings."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import selector

from ..api.entity_fields import duplicate_placeholders, MAX_AGE_MIN_S, validate_charger_entities
from ..const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CHARGER_PLATFORM,
    CONF_CONTROL_PATH,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_IDLE_POWER_W,
    CONF_POWER_ENTITY,
    DEFAULT_IDLE_POWER_W,
    CONF_GRID_POWER_INVERTED,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_MEASUREMENT_MODE,
    CONF_MODE,
    CONF_PHASE_WIRING,
    CONF_REGULATOR_DEADBAND_A,
    CONF_REGULATOR_DWELL_S,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SITE_CURRENT_SOURCE,
    CONF_SITE_ENABLED,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_SOLAR_PRIORITY,
    CONF_YIELD_CEILING_A,
    CONF_YIELD_STEPPING_ENABLED,
    DEFAULT_MAX_AGE_S,
    DEFAULT_REGULATOR_DEADBAND_A,
    DEFAULT_REGULATOR_DWELL_S,
    DEFAULT_SOLAR_PRIORITY,
    default_yield_ceiling_a,
    DEFAULT_YIELD_STEPPING_ENABLED,
    DOMAIN,
    max_yield_ceiling_a,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_EASEE,
    CURRENT_CONTROL_NUMBER,
    MEASUREMENT_MODE_DIRECT,
    MODE_DETECTED,
    MODE_OCPP,
    SOLAR_PRIORITY_CHOICES,
)
from ..execution.charger_profiles import profile_for
from ..execution.controller import RESTORE_FAILED
from ..planning.hybrid_forecast import async_forecast_capable_domains
from ..runtime import site_controller_for
from ..site.site_capacity_controller import SiteCapacityController, SiteControllerClosed
from ..site.site_membership import site_membership_errors
from ..vehicles.choices import entity_option, flow_language
from ..vehicles.discovery import async_discover_site_current_sources, DiscoveryCandidate
from ..vehicles.ocpp_identity import apply_target, discover_controls, resolve_target
from ..execution.charger_entities import control_path_for_entity
from .charger_wiring import ChargerWiringSteps
from .labels import (
    current_control_selector,
    MANUAL_ATTRIBUTES_CHOICE,
    power_sensor_entity_options,
    SKIP_CHOICE,
)
from .measured_source import (
    async_charger_measured_candidates,
    async_manual_source_step,
    charger_device_ids,
    site_device_scope,
)
from .labels import PHASES
from .site_form import (
    parse_site_details,
    parse_site_flags,
    PendingSiteDetails,
    resolve_site_current_choice,
    site_basic_schema,
    site_current_suggestions_schema,
    site_default_current_choice,
    site_details_schema,
    site_details_unit_errors,
)


_LOGGER = logging.getLogger(__name__)


class SpotNavChargingOptionsFlow(config_entries.OptionsFlow):
    """Edit charger entity choices."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        detected = self._entry.data.get(CONF_MODE) == MODE_DETECTED
        current_limit_field: Any = vol.Optional(CONF_CURRENT_LIMIT)
        if current_limit_default := self._entry.data.get(CONF_CURRENT_LIMIT) or None:
            # A suggested value pre-fills the UI but does not restore a cleared entity as a schema default would.
            current_limit_field = vol.Optional(
                CONF_CURRENT_LIMIT,
                description={"suggested_value": current_limit_default},
            )
        # Optional per charger: a cumulative energy-register sensor, so `manual_kwh`'s remaining
        # need is reduced by energy already delivered. Absent by default.
        energy_register_field: Any = vol.Optional(CONF_ENERGY_REGISTER_ENTITY)
        if energy_register_default := self._entry.data.get(CONF_ENERGY_REGISTER_ENTITY) or None:
            energy_register_field = vol.Optional(
                CONF_ENERGY_REGISTER_ENTITY,
                description={"suggested_value": energy_register_default},
            )
        # Optional per charger: the power sensor of a charger behind a smart plug, and the power
        # below which it counts as not drawing.
        power_field: Any = vol.Optional(CONF_POWER_ENTITY)
        if power_default := self._entry.data.get(CONF_POWER_ENTITY) or None:
            power_field = vol.Optional(CONF_POWER_ENTITY, description={"suggested_value": power_default})
        idle_power_field: Any = vol.Optional(
            CONF_IDLE_POWER_W,
            description={"suggested_value": self._entry.data.get(CONF_IDLE_POWER_W) or DEFAULT_IDLE_POWER_W},
        )
        current_control_field: Any = vol.Optional(
            CONF_CURRENT_CONTROL,
            description={
                "suggested_value": self._entry.data.get(CONF_CURRENT_CONTROL) or ""
            },
        )
        schema_fields: dict[Any, Any] = {
            vol.Required(
                CONF_CHARGE_CONTROL,
                default=self._entry.data[CONF_CHARGE_CONTROL],
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["switch", "select", "button", "sensor"] if detected else "switch"
                )
            ),
            current_limit_field: selector.EntitySelector(
                selector.EntitySelectorConfig(domain="number")
            ),
            energy_register_field: selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="energy")
            ),
        }
        if self._entry.data.get(CONF_MODE) != MODE_OCPP:
            schema_fields[power_field] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            )
            schema_fields[idle_power_field] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=5000, step=1, unit_of_measurement="W", mode=selector.NumberSelectorMode.BOX
                )
            )
        if self._entry.data.get(CONF_MODE) == MODE_OCPP:
            # The same choice the OCPP config step offers; a generic charger has no OCPP integration.
            # The number kind is offered when the entry already uses it or the connector has a
            # session-current number, so a stored choice is never missing from its own dropdown.
            ocpp_kinds: tuple[str, ...] = (CURRENT_CONTROL_CHANGE_CONFIGURATION,)
            if self._entry.data.get(CONF_CURRENT_CONTROL) == CURRENT_CONTROL_NUMBER:
                ocpp_kinds += (CURRENT_CONTROL_NUMBER,)
            else:
                resolution = resolve_target(
                    self.hass, charge_control=self._entry.data[CONF_CHARGE_CONTROL]
                )
                if (
                    resolution.target is not None
                    and discover_controls(self.hass, resolution.target).session_limit_entity
                ):
                    ocpp_kinds += (CURRENT_CONTROL_NUMBER,)
            schema_fields[current_control_field] = current_control_selector(self.hass, ocpp_kinds)
        elif detected:
            # A detected charger sets its current through what its platform supports.
            profile = profile_for(self._entry.data.get(CONF_CHARGER_PLATFORM))
            kinds = tuple(
                kind
                for kind, applies in (
                    (CURRENT_CONTROL_NUMBER, profile is not None and bool(profile.current_keys)),
                    (CURRENT_CONTROL_EASEE, profile is not None and profile.easee_current),
                )
                if applies
            )
            if kinds:
                schema_fields[current_control_field] = current_control_selector(self.hass, kinds)
        schema = vol.Schema(schema_fields)
        if user_input is not None:
            # Exclude this entry itself so re-saving its own entities never conflicts with itself.
            errors = validate_charger_entities(
                self.hass,
                charge_control=user_input[CONF_CHARGE_CONTROL],
                current_limit=user_input.get(CONF_CURRENT_LIMIT) or None,
                exclude_entry_id=self._entry.entry_id,
            )
            path: dict[str, Any] | None = None
            if detected and not errors:
                stored_path = self._entry.data.get(CONF_CONTROL_PATH)
                path = control_path_for_entity(
                    self.hass,
                    user_input[CONF_CHARGE_CONTROL],
                    detected_path=stored_path if isinstance(stored_path, dict) else None,
                    charge_control_is_identity=(
                        user_input[CONF_CHARGE_CONTROL] == self._entry.data[CONF_CHARGE_CONTROL]
                    ),
                )
                if path is None:
                    errors = {CONF_CHARGE_CONTROL: "control_path_unknown"}
                elif (user_input.get(CONF_CURRENT_CONTROL) or "") == CURRENT_CONTROL_NUMBER and not user_input.get(
                    CONF_CURRENT_LIMIT
                ):
                    errors = {CONF_CURRENT_CONTROL: "current_limit_required"}
            if not errors:
                updated_data = {
                    **self._entry.data,
                    CONF_CHARGE_CONTROL: user_input[CONF_CHARGE_CONTROL],
                    CONF_CURRENT_LIMIT: user_input.get(CONF_CURRENT_LIMIT) or "",
                    CONF_CURRENT_CONTROL: user_input.get(CONF_CURRENT_CONTROL) or "",
                    CONF_ENERGY_REGISTER_ENTITY: user_input.get(CONF_ENERGY_REGISTER_ENTITY) or "",
                }
                for key in (CONF_POWER_ENTITY, CONF_IDLE_POWER_W):
                    # Stored only while set, so a charger without them keeps exactly its old data.
                    if user_input.get(key):
                        updated_data[key] = user_input[key]
                    else:
                        updated_data.pop(key, None)
                if updated_data.get(CONF_IDLE_POWER_W) == DEFAULT_IDLE_POWER_W:
                    updated_data.pop(CONF_IDLE_POWER_W)
                if detected:
                    updated_data[CONF_CONTROL_PATH] = path
                if self._entry.data.get(CONF_MODE) == MODE_OCPP:
                    # The charge control may have been re-pointed, so re-resolve the stored identity
                    # rather than keep commanding the connector the old entity named; an unresolved
                    # entity keeps what was stored.
                    apply_target(
                        updated_data,
                        resolve_target(self.hass, charge_control=updated_data[CONF_CHARGE_CONTROL]),
                    )
                self.hass.config_entries.async_update_entry(
                    self._entry, data=updated_data
                )
                await self.hass.config_entries.async_reload(self._entry.entry_id)
                return self.async_create_entry(title="", data={})
            return self.async_show_form(
                step_id="init",
                data_schema=schema,
                errors=errors,
                description_placeholders=duplicate_placeholders(
                    self.hass,
                    errors,
                    charge_control=user_input[CONF_CHARGE_CONTROL],
                    current_limit=user_input.get(CONF_CURRENT_LIMIT) or None,
                    exclude_entry_id=self._entry.entry_id,
                ),
            )
        return self.async_show_form(step_id="init", data_schema=schema)


CHANGE_MEASUREMENT = "change_measurement"

_DETAILS_REASON_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "mode": "The measurement mode changed, so the measurement needs to be set up. ",
        "chargers": "The chargers changed, so their phase wiring needs to be set up. ",
        "measurement": "The stored measurement is incomplete, so it needs to be set up. ",
    },
    "sv": {
        "mode": "M\u00e4tl\u00e4get \u00e4ndrades, s\u00e5 m\u00e4tningen beh\u00f6ver st\u00e4llas in. ",
        "chargers": "Laddarna \u00e4ndrades, s\u00e5 deras fasinkoppling beh\u00f6ver st\u00e4llas in. ",
        "measurement": "Den sparade m\u00e4tningen \u00e4r ofullst\u00e4ndig, s\u00e5 den beh\u00f6ver st\u00e4llas in. ",
    },
}


class SiteCapacityOptionsFlow(ChargerWiringSteps, config_entries.OptionsFlow):
    """Edit every structural choice a site was created with.

    `CONF_SITE_ENABLED` gates the site calculation; `CONF_ACTIVE_CONTROL_ENABLED` is this
    installation's half of the active-control gate and writes nothing unless
    `site/site_capacity.py`'s `ACTIVE_CONTROL_READY` is also set. Uses the create flow's schema and
    parsing functions (`site_basic_schema`, `site_current_suggestions_schema`, `site_details_schema`,
    `parse_site_details`, the manual-entry helpers) and `site_membership_errors`.

    The site's current source is editable too: `async_step_site_current_suggestions` (direct mode)
    preselects the stored source; the manual-attributes shape is collected in
    `async_step_site_manual_source`.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._pending_basic: dict[str, Any] = {}
        # Why the details step is shown although it was not asked for (`_measurement_change_reason`).
        self._details_reason: str | None = None
        # The `site_details` submission while each charger's wiring is collected (`ChargerWiringSteps`).
        self._init_charger_wiring()
        # What this run resolved the site's current source to; `None` means the suggestions step
        # did not run (derived mode) or is unanswered (see `_site_current_source_for_save`).
        self._site_current_source_choice: str | None = None
        self._site_current_source: dict[str, Any] | None = None
        self._site_current_candidates: list[DiscoveryCandidate] = []
        # Whether `async_step_site_init` genuinely customized the yield ceiling, as opposed to
        # carrying its suggested value forward; `_save_site_details` persists an explicit value
        # only then (or when one was already stored).
        self._pending_yield_ceiling_customized = False
        # Config entries able to answer the Energy dashboard's solar-forecast platform, discovered
        # once per flow instance rather than on every render.
        self._forecast_entry_options: list[dict[str, str]] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Home Assistant's fixed options-flow entry point; forwards to `async_step_site_init`.

        A distinctly named method (not just another `step_id`) so its translations do not collide with
        `SpotNavChargingOptionsFlow`'s "init"; Home Assistant requires a method for every declared step_id.
        """
        return await self.async_step_site_init(user_input)

    def _site_init_schema(self) -> vol.Schema:
        """The site_init schema, defaulted from `self._pending_basic` (a submission re-shown after an error)
        else the stored values. Shared by the first render and the race-conflict re-render.
        """
        main_fuse_a = float(self._entry.data[CONF_MAIN_FUSE_A])
        defaults = {
            "name": self._entry.title,
            CONF_MAIN_FUSE_A: self._entry.data[CONF_MAIN_FUSE_A],
            CONF_SAFETY_MARGIN_A: self._entry.data.get(CONF_SAFETY_MARGIN_A, 0.0),
            CONF_MEASUREMENT_MODE: self._entry.data.get(
                CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DIRECT
            ),
            CONF_CHARGER_ENTRY_IDS: self._entry.data.get(CONF_CHARGER_ENTRY_IDS, []),
            CONF_SITE_ENABLED: self._entry.data.get(CONF_SITE_ENABLED, False),
            # Off unless the site opted in; suggested from the stored value, never defaulted on.
            CONF_ACTIVE_CONTROL_ENABLED: self._entry.data.get(
                CONF_ACTIVE_CONTROL_ENABLED, False
            ),
            CONF_MAX_AGE_S: self._entry.data.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S),
            CONF_REGULATOR_DEADBAND_A: self._entry.data.get(
                CONF_REGULATOR_DEADBAND_A, DEFAULT_REGULATOR_DEADBAND_A
            ),
            CONF_REGULATOR_DWELL_S: self._entry.data.get(
                CONF_REGULATOR_DWELL_S, DEFAULT_REGULATOR_DWELL_S
            ),
            # Yield-verified stepping: off unless opted in. The ceiling defaults to 1.15x the
            # currently stored main fuse.
            CONF_YIELD_STEPPING_ENABLED: self._entry.data.get(
                CONF_YIELD_STEPPING_ENABLED, DEFAULT_YIELD_STEPPING_ENABLED
            ),
            CONF_YIELD_CEILING_A: self._entry.data.get(
                CONF_YIELD_CEILING_A, default_yield_ceiling_a(main_fuse_a)
            ),
            # Solar surplus: which claim on the sun wins, car or house battery; `car_first` unless stored.
            CONF_SOLAR_PRIORITY: self._entry.data.get(
                CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY
            ),
            # Hybrid's forecast sources: empty unless named, and hybrid is then exactly cheapest.
            CONF_SOLAR_FORECAST_ENTRIES: self._entry.data.get(CONF_SOLAR_FORECAST_ENTRIES, []),
            **self._pending_basic,
        }
        battery_entity_field: Any = vol.Optional(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
        existing_battery_entity = self._entry.data.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
        if existing_battery_entity:
            battery_entity_field = vol.Optional(
                CONF_BATTERY_AGGREGATE_POWER_ENTITY,
                description={"suggested_value": existing_battery_entity},
            )
        # Optional, purely diagnostic (feeds the "estimated headroom if the battery yields"
        # attribute); never a safe limit. A `SelectSelector` so each option shows its entity id
        # (identically named "Battery Power" sensors are common). The stored entity stays an option
        # even if it no longer matches the `power` filter; with no power sensors, fall back to the
        # plain entity selector.
        battery_options = power_sensor_entity_options(self.hass)
        if existing_battery_entity and not any(
            option["value"] == existing_battery_entity for option in battery_options
        ):
            battery_options.append(entity_option(self.hass, existing_battery_entity))
        battery_selector: Any = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=battery_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            if battery_options
            else selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            )
        )
        return site_basic_schema(
            self.hass, defaults=defaults, exclude_entry_id=self._entry.entry_id
        ).extend(
            {
                vol.Required(CONF_SITE_ENABLED, default=defaults[CONF_SITE_ENABLED]): bool,
                # Off: save these basics and keep the stored measurement and phase wiring.
                vol.Optional(CHANGE_MEASUREMENT, default=False): bool,
                # The human's opt-in half of the active-control gate, default off.
                vol.Required(
                    CONF_ACTIVE_CONTROL_ENABLED,
                    default=defaults[CONF_ACTIVE_CONTROL_ENABLED],
                ): bool,
                vol.Optional(CONF_MAX_AGE_S, default=defaults[CONF_MAX_AGE_S]): vol.All(
                    vol.Coerce(float), vol.Range(min=MAX_AGE_MIN_S)
                ),
                # Active control's damping (see `site/regulator_damping.py`). Zero is valid: no
                # deadband, write as soon as decided.
                vol.Optional(
                    CONF_REGULATOR_DEADBAND_A, default=defaults[CONF_REGULATOR_DEADBAND_A]
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Optional(
                    CONF_REGULATOR_DWELL_S, default=defaults[CONF_REGULATOR_DWELL_S]
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                # Yield-verified stepping: off by default. The ceiling must exceed the main fuse,
                # which depends on another field, so `async_step_site_init` checks it after this
                # schema and re-renders with a translated error.
                vol.Required(
                    CONF_YIELD_STEPPING_ENABLED,
                    default=defaults[CONF_YIELD_STEPPING_ENABLED],
                ): bool,
                vol.Optional(
                    CONF_YIELD_CEILING_A, default=defaults[CONF_YIELD_CEILING_A]
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                # Solar priority: a plain choice, valid beside every fuse or mode choice.
                vol.Required(
                    CONF_SOLAR_PRIORITY, default=defaults[CONF_SOLAR_PRIORITY]
                ): vol.In(SOLAR_PRIORITY_CHOICES),
                battery_entity_field: battery_selector,
                # Hybrid's forecast sources: a multi-select of entries able to answer the Energy
                # dashboard's solar-forecast platform (`_async_ensure_forecast_entry_options`).
                # Empty is a safe choice: hybrid is then exactly cheapest.
                vol.Optional(
                    CONF_SOLAR_FORECAST_ENTRIES,
                    default=defaults[CONF_SOLAR_FORECAST_ENTRIES],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=option["value"], label=option["label"])
                            for option in (self._forecast_entry_options or [])
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )

    async def _async_ensure_forecast_entry_options(self) -> None:
        """Discover `self._forecast_entry_options` once per flow instance: the entries whose integration
        implements the Energy dashboard's solar-forecast platform, as `{"value": entry_id, "label": title}`,
        plus every entry already stored in `CONF_SOLAR_FORECAST_ENTRIES` even if it no longer qualifies.
        """
        if self._forecast_entry_options is not None:
            return
        domains = await async_forecast_capable_domains(self.hass)
        options = [
            {"value": entry.entry_id, "label": entry.title or entry.entry_id}
            for entry in self.hass.config_entries.async_entries()
            if entry.domain in domains
        ]
        known = {option["value"] for option in options}
        for entry_id in self._entry.data.get(CONF_SOLAR_FORECAST_ENTRIES, []) or []:
            if entry_id in known:
                continue
            stored_entry = self.hass.config_entries.async_get_entry(entry_id)
            options.append(
                {"value": entry_id, "label": stored_entry.title if stored_entry else entry_id}
            )
        self._forecast_entry_options = options

    async def async_step_site_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enabled state, main fuse, safety margin, measurement mode, chargers."""
        await self._async_ensure_forecast_entry_options()
        if user_input is not None:
            charger_entry_ids: list[str] = user_input.get(CONF_CHARGER_ENTRY_IDS, [])
            errors = site_membership_errors(
                self.hass,
                charger_entry_ids=charger_entry_ids,
                exclude_entry_id=self._entry.entry_id,
            )
            # The yield ceiling must exceed the main fuse, or `execution/yield_stepping.py`'s
            # hard-ceiling check would license steps at or past the site's limit (see
            # `default_yield_ceiling_a`). Not a `vol.Range` because it depends on this
            # submission's main fuse.
            #
            # The schema default follows the main fuse as currently stored, so raising the fuse
            # without touching the ceiling would carry forward a now-too-low number. Comparing the
            # submission with that old default detects "untouched": the ceiling then follows the
            # new fuse, and only a genuinely different, too-low value is rejected. The comparison
            # uses the generic formula's old value, never the stored one, so an already-stored
            # explicit ceiling is kept when the field is carried forward untouched.
            old_generic_default_ceiling_a = default_yield_ceiling_a(
                float(self._entry.data[CONF_MAIN_FUSE_A])
            )
            submitted_ceiling_a = float(user_input[CONF_YIELD_CEILING_A])
            new_main_fuse_a = float(user_input[CONF_MAIN_FUSE_A])
            # Whether this submission genuinely customized the ceiling; `_save_site_details` uses
            # it to decide whether to persist an explicit value at all.
            self._pending_yield_ceiling_customized = (
                abs(submitted_ceiling_a - old_generic_default_ceiling_a) >= 1e-9
            )
            if not self._pending_yield_ceiling_customized:
                user_input[CONF_YIELD_CEILING_A] = default_yield_ceiling_a(new_main_fuse_a)
            elif submitted_ceiling_a <= new_main_fuse_a:
                errors[CONF_YIELD_CEILING_A] = "yield_ceiling_below_fuse"
            elif submitted_ceiling_a >= max_yield_ceiling_a(new_main_fuse_a):
                errors[CONF_YIELD_CEILING_A] = "yield_ceiling_above_limit"
            if errors:
                self._pending_basic = user_input
                return self.async_show_form(
                    step_id="site_init", data_schema=self._site_init_schema(), errors=errors
                )
            self._pending_basic = user_input
            reason = self._measurement_change_reason(user_input)
            if not user_input.get(CHANGE_MEASUREMENT) and reason is None:
                return await self._save_basics_only()
            self._details_reason = reason
            if user_input[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DIRECT:
                # As in the create flow: direct mode asks for the site's current source first.
                return await self.async_step_site_current_suggestions()
            return await self.async_step_site_details()
        return self.async_show_form(step_id="site_init", data_schema=self._site_init_schema())

    async def async_step_site_current_suggestions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the create flow's site-current choices (candidates, both manual shapes, "skip") with this
        site's stored source preselected (`site_default_current_choice`). Shown whenever direct mode is
        pending, even when discovery finds nothing.
        """
        charger_entry_ids: list[str] = self._pending_basic.get(CONF_CHARGER_ENTRY_IDS, [])
        self._site_current_candidates = await async_discover_site_current_sources(
            self.hass, excluded_device_ids=charger_device_ids(self.hass, charger_entry_ids)
        )
        schema = site_current_suggestions_schema(
            self.hass,
            self._site_current_candidates,
            default_choice=site_default_current_choice(
                self._site_current_candidates, self._entry.data.get(CONF_SITE_CURRENT_SOURCE)
            ),
        )
        if user_input is not None:
            choice = user_input["choice"]
            if choice == MANUAL_ATTRIBUTES_CHOICE:
                return await self.async_step_site_manual_source()
            (
                self._site_current_source_choice,
                self._site_current_source,
            ) = resolve_site_current_choice(self._site_current_candidates, choice)
            return await self.async_step_site_details()
        return self.async_show_form(step_id="site_current_suggestions", data_schema=schema)

    async def async_step_site_manual_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the site's total current as one entity plus its per-phase attribute names, pre-filled
        from the stored source (`async_manual_source_step`, shared with the create flow).
        """
        charger_entry_ids: list[str] = self._pending_basic.get(CONF_CHARGER_ENTRY_IDS, [])
        outcome = await async_manual_source_step(
            self.hass,
            scope=site_device_scope(self.hass, charger_entry_ids),
            stored_source=self._entry.data.get(CONF_SITE_CURRENT_SOURCE),
            user_input=user_input,
        )
        if outcome.source is None:
            return self.async_show_form(
                step_id="site_manual_source",
                data_schema=outcome.schema,
                errors=outcome.errors,
                description_placeholders=outcome.description_placeholders,
            )
        self._site_current_source_choice = MANUAL_ATTRIBUTES_CHOICE
        self._site_current_source = outcome.source
        return await self.async_step_site_details()

    async def async_step_site_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Per-charger phase wiring and measurement entities, pre-filled with the site's current values;
        only fields relevant to the (possibly just-changed) mode are shown.
        """
        charger_entry_ids: list[str] = self._pending_basic.get(CONF_CHARGER_ENTRY_IDS, [])
        mode = self._pending_basic[CONF_MEASUREMENT_MODE]
        skip_direct_fields = self._skip_direct_fields(mode)
        schema = site_details_schema(
            self.hass,
            mode,
            direct_defaults=self._entry.data.get(CONF_DIRECT_ENTITIES, {}),
            derived_defaults=self._entry.data.get(CONF_DERIVED_ENTITIES, {}),
            skip_direct_fields=skip_direct_fields,
            flag_defaults={
                CONF_SITE_CURRENT_SIGNED: bool(self._entry.data.get(CONF_SITE_CURRENT_SIGNED, False)),
                CONF_GRID_POWER_INVERTED: bool(self._entry.data.get(CONF_GRID_POWER_INVERTED, False)),
            },
        )
        if user_input is not None:
            # Authoritative re-check at save time: another site may have claimed a selected
            # charger since the picker on the previous step.
            errors = site_membership_errors(
                self.hass,
                charger_entry_ids=charger_entry_ids,
                exclude_entry_id=self._entry.entry_id,
            )
            if errors:
                # `self._pending_basic` already holds this step's inputs, so only the charger
                # selection needs correcting.
                return self.async_show_form(
                    step_id="site_init", data_schema=self._site_init_schema(), errors=errors
                )
            errors = site_details_unit_errors(
                self.hass, user_input, mode, skip_direct_fields=skip_direct_fields
            )
            if errors:
                return self.async_show_form(
                    step_id="site_details",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors=errors,
                    description_placeholders=self._details_placeholders(),
                )
            pending = PendingSiteDetails(
                details=user_input,
                charger_entry_ids=charger_entry_ids,
                mode=mode,
                skip_direct_fields=skip_direct_fields,
                charger_candidates=await async_charger_measured_candidates(
                    self.hass, charger_entry_ids
                ),
            )
            if charger_entry_ids:
                # Each charger's wiring is its own step; the site is saved after the last.
                return await self._begin_charger_wiring(pending)
            return await self._save_site_details(pending, {})
        return self.async_show_form(
            step_id="site_details",
            data_schema=schema,
            description_placeholders=self._details_placeholders(),
        )

    def _details_placeholders(self) -> dict[str, str]:
        return {
            "reason": _DETAILS_REASON_TEXT[flow_language(self.hass)].get(self._details_reason or "", "")
        }

    def _stored_wiring(self, charger_entry_id: str) -> dict[str, Any]:
        """This site's stored wiring for one charger, so its forms pre-fill it."""
        return dict((self._entry.data.get(CONF_PHASE_WIRING, {}) or {}).get(charger_entry_id) or {})

    async def _finish_pending_site(self) -> ConfigFlowResult:
        """Save the site whose `site_details` submission started this run of manual entries (the options
        flow's half of the shared step).
        """
        if self._pending_site is None:
            # Unreachable from the UI (`_pending_site` is stored first); abort if that ever fails.
            return self.async_abort(reason="no_pending_site")
        # The chargers' wiring steps took time: another site may have claimed one meanwhile.
        errors = site_membership_errors(
            self.hass,
            charger_entry_ids=self._pending_site.charger_entry_ids,
            exclude_entry_id=self._entry.entry_id,
        )
        if errors:
            return self.async_show_form(
                step_id="site_init", data_schema=self._site_init_schema(), errors=errors
            )
        return await self._save_site_details(self._pending_site, self._manual_measured_sources)

    async def _save_site_details(
        self, pending: PendingSiteDetails, manual_sources: dict[str, dict[str, Any]]
    ) -> ConfigFlowResult:
        """Write one `site_details` submission into this site's entry, reload it and finish the flow,
        directly or once every "manual" charger has been collected.
        """
        phase_wiring, direct_entities, derived_entities = parse_site_details(
            self.hass,
            pending.details,
            pending.charger_entry_ids,
            pending.mode,
            phase_wiring_defaults=self._entry.data.get(CONF_PHASE_WIRING, {}),
            skip_direct_fields=pending.skip_direct_fields,
            charger_candidates=pending.charger_candidates,
            manual_sources=manual_sources,
            charger_inputs=pending.charger_inputs,
        )
        updated_data = self._updated_site_data(
            mode=pending.mode,
            charger_entry_ids=pending.charger_entry_ids,
            phase_wiring=phase_wiring,
            direct_entities=direct_entities,
            derived_entities=derived_entities,
            flags=parse_site_flags(pending.details, pending.mode, self._entry.data),
        )
        await self._async_persist_and_reload(
            updated_data, title=self._pending_basic.get("name") or self._entry.title
        )
        return self.async_create_entry(title="", data={})

    async def _save_basics_only(self) -> ConfigFlowResult:
        """Save the basics with the stored measurement, wiring and entities exactly as they are: what
        finishing `site_details` on its current defaults would write, without parsing anything."""
        mode = self._pending_basic[CONF_MEASUREMENT_MODE]
        updated_data = self._updated_site_data(
            mode=mode,
            charger_entry_ids=list(self._pending_basic.get(CONF_CHARGER_ENTRY_IDS, [])),
            phase_wiring=self._entry.data.get(CONF_PHASE_WIRING, {}),
            direct_entities=self._entry.data.get(CONF_DIRECT_ENTITIES, {}),
            derived_entities=self._entry.data.get(CONF_DERIVED_ENTITIES, {}),
            flags=parse_site_flags({}, mode, self._entry.data),
        )
        await self._async_persist_and_reload(
            updated_data, title=self._pending_basic.get("name") or self._entry.title
        )
        return self.async_create_entry(title="", data={})

    def _measurement_change_reason(self, basic: dict[str, Any]) -> str | None:
        """Why the stored measurement and wiring cannot be kept under these basics, or `None`."""
        data = self._entry.data
        mode = basic[CONF_MEASUREMENT_MODE]
        if mode != data.get(CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DIRECT):
            return "mode"
        chargers = list(basic.get(CONF_CHARGER_ENTRY_IDS, []))
        wiring = data.get(CONF_PHASE_WIRING) or {}
        if set(chargers) != set(data.get(CONF_CHARGER_ENTRY_IDS, [])) or any(
            charger not in wiring for charger in chargers
        ):
            return "chargers"
        if mode == MEASUREMENT_MODE_DIRECT:
            direct = data.get(CONF_DIRECT_ENTITIES) or {}
            if not (all(direct.get(phase) for phase in PHASES) or data.get(CONF_SITE_CURRENT_SOURCE)):
                return "measurement"
        else:
            derived = data.get(CONF_DERIVED_ENTITIES) or {}
            if not all(
                (derived.get(phase) or {}).get("power") and (derived.get(phase) or {}).get("voltage")
                for phase in PHASES
            ):
                return "measurement"
        return None

    def _updated_site_data(
        self,
        *,
        mode: str,
        charger_entry_ids: list[str],
        phase_wiring: dict[str, Any],
        direct_entities: dict[str, Any],
        derived_entities: dict[str, Any],
        flags: dict[str, bool],
    ) -> dict[str, Any]:
        """The entry data a save writes: the basics from `self._pending_basic`, the measurement parts
        handed in (parsed from the details step, or the stored ones when it is skipped)."""
        updated_data = {
            **self._entry.data,
            CONF_SITE_ENABLED: self._pending_basic[CONF_SITE_ENABLED],
            CONF_MAIN_FUSE_A: self._pending_basic[CONF_MAIN_FUSE_A],
            CONF_SAFETY_MARGIN_A: self._pending_basic.get(CONF_SAFETY_MARGIN_A, 0.0),
            CONF_MEASUREMENT_MODE: mode,
            CONF_CHARGER_ENTRY_IDS: charger_entry_ids,
            CONF_PHASE_WIRING: phase_wiring,
            CONF_DIRECT_ENTITIES: direct_entities,
            CONF_DERIVED_ENTITIES: derived_entities,
            **flags,
            CONF_MAX_AGE_S: self._pending_basic.get(
                CONF_MAX_AGE_S, self._entry.data.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S)
            ),
            CONF_REGULATOR_DEADBAND_A: self._pending_basic.get(
                CONF_REGULATOR_DEADBAND_A,
                self._entry.data.get(CONF_REGULATOR_DEADBAND_A, DEFAULT_REGULATOR_DEADBAND_A),
            ),
            CONF_REGULATOR_DWELL_S: self._pending_basic.get(
                CONF_REGULATOR_DWELL_S,
                self._entry.data.get(CONF_REGULATOR_DWELL_S, DEFAULT_REGULATOR_DWELL_S),
            ),
            CONF_BATTERY_AGGREGATE_POWER_ENTITY: self._pending_basic.get(
                CONF_BATTERY_AGGREGATE_POWER_ENTITY
            )
            or "",
        }
        # Active control is persisted only when it carries information: written when opted in,
        # and as False only for a site that already had the key. Absent already means off to both
        # gates, so a round-trip save of an untouched entry stays a no-op.
        submitted_active_control = bool(
            self._pending_basic.get(CONF_ACTIVE_CONTROL_ENABLED, False)
        )
        if submitted_active_control or CONF_ACTIVE_CONTROL_ENABLED in self._entry.data:
            updated_data[CONF_ACTIVE_CONTROL_ENABLED] = submitted_active_control
        # Yield-verified stepping's opt-in, persisted the same way (a round-trip save is a no-op).
        submitted_yield_stepping = bool(
            self._pending_basic.get(CONF_YIELD_STEPPING_ENABLED, False)
        )
        if submitted_yield_stepping or CONF_YIELD_STEPPING_ENABLED in self._entry.data:
            updated_data[CONF_YIELD_STEPPING_ENABLED] = submitted_yield_stepping
        # The yield ceiling is persisted only when this submission customized it (see
        # `async_step_site_init`) or one was already stored, so an untouched entry round-trips
        # unchanged. `self._pending_basic[CONF_YIELD_CEILING_A]` is always present here: either
        # the customized value or the fresh default for the submitted main fuse.
        if (
            self._pending_yield_ceiling_customized
            or CONF_YIELD_CEILING_A in self._entry.data
        ):
            updated_data[CONF_YIELD_CEILING_A] = self._pending_basic[CONF_YIELD_CEILING_A]
        else:
            updated_data.pop(CONF_YIELD_CEILING_A, None)
        # Solar priority, persisted the same way: written when it differs from the default or a
        # value was already stored.
        submitted_solar_priority = self._pending_basic.get(
            CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY
        )
        if (
            submitted_solar_priority != DEFAULT_SOLAR_PRIORITY
            or CONF_SOLAR_PRIORITY in self._entry.data
        ):
            updated_data[CONF_SOLAR_PRIORITY] = submitted_solar_priority
        # Hybrid's forecast sources, persisted the same way: written when at least one entry is
        # named or a value was already stored.
        submitted_forecast_entries = list(self._pending_basic.get(CONF_SOLAR_FORECAST_ENTRIES, []))
        if submitted_forecast_entries or CONF_SOLAR_FORECAST_ENTRIES in self._entry.data:
            updated_data[CONF_SOLAR_FORECAST_ENTRIES] = submitted_forecast_entries
        # The site's current source is written from what `async_step_site_current_suggestions` or
        # its manual sub-step resolved (`_site_current_source_for_save`): a candidate or manual
        # source replaces the stored one and "skip" removes it. Anything else ("manual" guided
        # pickers write their own keys, or the suggestions step never ran) leaves the value
        # carried in by `**self._entry.data` untouched.
        resolved_site_source = self._site_current_source_for_save()
        if resolved_site_source is not None:
            updated_data[CONF_SITE_CURRENT_SOURCE] = resolved_site_source
        elif self._site_current_source_choice == SKIP_CHOICE:
            updated_data.pop(CONF_SITE_CURRENT_SOURCE, None)
        return updated_data

    async def _async_persist_and_reload(self, updated_data: dict[str, Any], *, title: str) -> None:
        """Persist the saved site data and reload the entry, through the same transition the card's switch
        uses (`site_settings_api._change_active_control`) when balancing goes off.

        A reload alone discards the running controller and its memory of what it wrote, so a lowered
        current would stay lowered with the option "off". With the controller's `transition_lock` held:

        * balancing running and the save turns it off (or disables the whole site): the controller's
          `async_disable_active_control` runs first, restoring lowered currents through the charger's
          serialized write path, and only then is the entry persisted and reloaded;
        * anything else writes nothing to any charger here. Turning on stays the reload path: the rebuilt
          controller passes every gate the apply path has, and no write happens inside the flow.

        The options flow cannot show a rich answer, so a restore that did not fully succeed is logged
        at warning level and raised as a persistent notification; the flow still saves.
        """
        entry_id = self._entry.entry_id
        for _attempt in range(3):
            controller = site_controller_for(self.hass, entry_id)
            if controller is None:
                break  # not loaded: nothing running, nothing to restore
            async with controller.transition_lock:
                if site_controller_for(self.hass, entry_id) is not controller:
                    continue  # replaced while waiting for the lock: look again
                turning_off = controller.active_control_enabled and not (
                    bool(updated_data.get(CONF_ACTIVE_CONTROL_ENABLED, False))
                    and bool(updated_data.get(CONF_SITE_ENABLED, True))
                )
                if turning_off:
                    await self._async_disable_before_save(controller)
                self.hass.config_entries.async_update_entry(
                    self._entry, data=updated_data, title=title
                )
                await self.hass.config_entries.async_reload(entry_id)
            return
        self.hass.config_entries.async_update_entry(self._entry, data=updated_data, title=title)
        await self.hass.config_entries.async_reload(entry_id)

    async def _async_disable_before_save(self, controller: SiteCapacityController) -> None:
        """Run the off transition and report anything but a clean restore (`transition_lock` held)."""
        try:
            report = await controller.async_disable_active_control()
        except SiteControllerClosed:
            _LOGGER.warning(
                "SpotNav site %s: active control was turned off in options but the site was "
                "unloaded first; lowered chargers could not be restored",
                self._entry.entry_id,
            )
            return
        failed = [item for item in report.chargers if item.restore.outcome == RESTORE_FAILED]
        if not failed:
            _LOGGER.info(
                "SpotNav site %s: active control turned off in options, restore %s",
                self._entry.entry_id,
                report.outcome,
            )
            return
        for item in failed:
            _LOGGER.warning(
                "SpotNav site %s: active control turned off in options but charger %s could not "
                "be restored (from %s A to %s A, code %s); it may still be limited",
                self._entry.entry_id,
                item.charger_entry_id,
                item.restore.from_a,
                item.restore.to_a,
                item.restore.code,
            )
        persistent_notification.async_create(
            self.hass,
            "Active load balancing was turned off, but SpotNav could not restore every "
            "charger's current: "
            + ", ".join(item.charger_entry_id for item in failed)
            + ". The charger may still be limited; check its current setting.",
            title="SpotNav: charger may still be limited",
            notification_id=f"{DOMAIN}_restore_failed_{self._entry.entry_id}",
        )

    def _site_current_source_for_save(self) -> dict[str, Any] | None:
        """What this run resolved the site's current source to, or `None` when it decided nothing.

        `SKIP_CHOICE` resolves to `None` ("configure later": no generic source). A candidate or the
        manual-attributes shape resolves to the source the step built. "manual" (guided per-phase pickers)
        and a run where the suggestions step never ran also give `None` but mean "leave the stored value
        alone" (see `_save_site_details`). A stored generic source only coexists with "manual" when the
        guided pickers were hidden (`_skip_direct_fields`), so preserving it discards nothing chosen.
        """
        if self._site_current_source_choice in ("candidate", MANUAL_ATTRIBUTES_CHOICE):
            return self._site_current_source
        return None

    def _skip_direct_fields(self, mode: str) -> bool:
        """Whether `site_details` should omit the guided per-phase pickers.

        True when the site's current is or will be a single generic source (stored, or just resolved), and
        when this run chose "skip" (showing pickers would quietly configure one anyway). An empty
        `CONF_DIRECT_ENTITIES` also means there is nothing to re-ask.
        """
        return mode == MEASUREMENT_MODE_DIRECT and (
            self._entry.data.get(CONF_SITE_CURRENT_SOURCE) is not None
            or self._site_current_source_for_save() is not None
            or self._site_current_source_choice == SKIP_CHOICE
            or not self._entry.data.get(CONF_DIRECT_ENTITIES)
        )
