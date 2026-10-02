"""The config flow: pairing, vehicle resolution, adding a charger and adding a site."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    selector,
)
from homeassistant.util import slugify

from ..api.entity_fields import validate_charger_entities
from ..api.pairing import PairingRegister
from ..const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_CURRENT_ENTITIES,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CHARGER_PLATFORM,
    CONF_CHARGING_STATE,
    CONF_CONTROL_PATH,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENERGY_REGISTER_IS_SESSION,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_MAX_AGE_S,
    CONF_MEASUREMENT_MODE,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CONF_PHASE_WIRING,
    CONF_REGULATOR_DEADBAND_A,
    CONF_REGULATOR_DWELL_S,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SOURCE,
    CONF_SITE_ENABLED,
    CONF_WEBHOOK_ID,
    CONF_YIELD_STEPPING_ENABLED,
    CONFIG_ENTRY_VERSION,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_EASEE,
    CURRENT_CONTROL_NUMBER,
    DEFAULT_MAX_AGE_S,
    DEFAULT_REGULATOR_DEADBAND_A,
    DEFAULT_REGULATOR_DWELL_S,
    DEFAULT_YIELD_STEPPING_ENABLED,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    ENTRY_TYPE_RESOLVE_VEHICLE,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DIRECT,
    MODE_DETECTED,
    MODE_GENERIC,
    MODE_OCPP,
)
from ..execution.charger_profiles import (
    detectable_platforms,
    profile_for,
    ROLE_CHARGER,
    ROLE_EXCLUDED,
)
from ..repairs import async_sync_resolution_repairs
from ..runtime import domain_data
from ..site.measurement_source import source_to_dict
from ..site.site_join import queue_site_join
from ..site.site_detection import (
    apply_battery_candidate,
    BatteryCandidate,
    detect_site_from_hass,
    enable_disabled_entities,
    MeterCandidate,
)
from ..site.site_membership import site_membership_errors
from ..vehicles.choices import (
    ambiguous_vehicle_option,
    DISMISS_VEHICLE_CHOICE,
    entity_option,
    flow_language,
    RESOLVE_VEHICLE_TEXT,
)
from ..vehicles.discovery import (
    async_discover_charger_current_sources,
    async_discover_site_current_sources,
)
from ..vehicles.discovery_decisions import async_setup_decisions, DECISION_DOMAIN_VEHICLE
from ..vehicles.ocpp_identity import (
    apply_target,
    charge_control_target_from_entity_id,
    energy_register_entity_for,
    session_limit_target_from_entity_id,
    is_station_maximum,
    resolve_target,
)
from ..vehicles.vehicle_discovery import discover_ambiguous_vehicles
from ..execution.chargers.ocpp import assigned_amps_for_connector, read_assigned_current_value
from ..execution.charger_entities import control_path_for_entity, own_mode_conflicts
from .charger_wiring import ChargerWiringSteps
from .charger_detection import detect_charger, DetectedCharger, identifier_domains
from .labels import current_control_selector, MANUAL_ATTRIBUTES_CHOICE, SKIP_CHOICE
from .measured_source import (
    async_charger_measured_candidates,
    async_manual_source_step,
    charger_device_ids,
    site_device_scope,
)
from .options import SiteCapacityOptionsFlow, SpotNavChargingOptionsFlow
from .site_confirm import site_confirm_summary, site_join_summary
from .site_form import (
    default_site_name,
    detected_defaults,
    parse_site_details,
    parse_site_flags,
    PendingSiteDetails,
    resolve_site_current_choice,
    site_basic_schema,
    site_current_suggestions_schema,
    site_default_current_choice,
    site_detected_schema,
    site_details_schema,
    site_details_unit_errors,
    charger_wiring_schema,
)


_LOGGER = logging.getLogger(__name__)

#: How long the read-only `AssignedCurrent` probe may take before the charger counts as not supporting it.
PROBE_TIMEOUT_S = 10.0
PROBE_SUPPORTED = "supported"
PROBE_NOT_SUPPORTED = "not_supported"
PROBE_UNREACHABLE = "unreachable"

_DETECTED_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "external": "Another controller (evcc or openWB) is installed and may already control this "
        "charger. Nothing is suggested: choose only what you are sure SpotNav should drive.",
        "disabled": "Disabled by default, and useful (they are enabled if you leave the box below ticked):",
    },
    "sv": {
        "external": "En annan styrning (evcc eller openWB) är installerad och kan redan styra den här "
        "laddaren. Inget föreslås: välj bara det du är säker på att SpotNav ska styra.",
        "disabled": "Avstängda som standard men användbara (de aktiveras om rutan nedan är ikryssad):",
    },
}

#: The form field naming the sensor that says the charger is charging (stored as `CONF_CHARGING_STATE`).
FIELD_CHARGING_STATE_ENTITY = "charging_state_entity"


class SpotNavChargingConfigFlow(ChargerWiringSteps, config_entries.ConfigFlow, domain=DOMAIN):
    """Configure SpotNav charging control."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        self._mode = MODE_OCPP
        self._device_id: str | None = None
        # The devices and connector an OCPP entry is offered from when it was reached through the
        # automatic flow (`_ocpp_scope`); `None` means just `_device_id`, as in the OCPP flow.
        self._ocpp_device_ids: list[str] | None = None
        self._ocpp_connector: int | None = None
        self._current_default_cache: tuple[Any, tuple[str, str | None]] | None = None
        self._probe_unreachable = False
        self._reprobed = False
        self._notice_shown = False
        # What detection found for the device a `MODE_DETECTED` charger is built from.
        self._detected: DetectedCharger | None = None
        self._site_basic: dict[str, Any] = {}
        # Set when a device was picked in `async_step_resolve_vehicle`. This flow records one
        # decision and aborts; it creates no config entry.
        self._resolve_vehicle_device_id: str | None = None
        # Set by `async_step_site_current_suggestions`: "candidate", "manual" (guided pickers) or
        # "skip" (configure later via the options flow).
        self._site_current_source_choice: str | None = None
        self._site_current_source: dict[str, Any] | None = None
        self._site_current_candidates: list = []
        # What registry detection found and which candidate was chosen (`async_step_site_detected`);
        # the chosen one pre-fills the details form, and its disabled entities are enabled only when
        # the site is actually created.
        self._detected_meters: list[MeterCandidate] = []
        self._detected_batteries: list[BatteryCandidate] = []
        self._join_charger: tuple[str, dict[str, Any]] | None = None
        self._join_site_entry: Any = None
        # The battery the confirmation step applies; `None` whenever the full form is used.
        self._site_battery: BatteryCandidate | None = None
        self._site_detected: MeterCandidate | None = None
        self._enable_detected_entities = False
        # The `site_details` submission while each charger's wiring is collected (`ChargerWiringSteps`).
        self._init_charger_wiring()
        # Set from the request that started this flow. It creates no config entry either: it
        # records a person's approval or refusal of one pairing request and aborts.
        self._pairing_request_id: str | None = None

    def _pairing_register(self) -> PairingRegister | None:
        return domain_data(self.hass).pairing

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A phone has asked to be paired: show it, and its code, to a person.

        Started by the pairing webhook with `SOURCE_INTEGRATION_DISCOVERY`, so it appears in Settings ->
        Devices & Services. Every path ends in an abort. The request id passed in is the only one this
        flow can act on, and the record lives in the register.
        """
        self._pairing_request_id = (discovery_info or {}).get("request_id") or self._pairing_request_id
        return self._pairing_menu()

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The menu's own step, for a flow that is resumed. Shows the menu again, re-reading the register so
        an expired request aborts instead of offering a dead approval.
        """
        return self._pairing_menu()

    def _pairing_menu(self) -> ConfigFlowResult:
        register = self._pairing_register()
        record = (
            register.describe(self._pairing_request_id)
            if register is not None and self._pairing_request_id
            else None
        )
        if record is None:
            return self.async_abort(reason="pairing_request_expired")
        return self.async_show_menu(
            step_id="pairing",
            menu_options=["pair_approve", "pair_deny"],
            description_placeholders={"code": record.code, "device": record.device},
        )

    async def async_step_pair_approve(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Approve this request. Single-use: the app is told exactly once."""
        register = self._pairing_register()
        if (
            register is None
            or not self._pairing_request_id
            or not register.approve(self._pairing_request_id)
        ):
            return self.async_abort(reason="pairing_request_expired")
        return self.async_abort(reason="pairing_approved")

    async def async_step_pair_deny(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refuse this request, so the phone is told instead of waiting."""
        register = self._pairing_register()
        if (
            register is None
            or not self._pairing_request_id
            or not register.deny(self._pairing_request_id)
        ):
            return self.async_abort(reason="pairing_request_expired")
        return self.async_abort(reason="pairing_denied")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose what to add: a charger, a site capacity/load-balancing group,
        or a resolution for a vehicle detection could not decide about.
        """
        if user_input is not None:
            if user_input[CONF_ENTRY_TYPE] == ENTRY_TYPE_SITE:
                return await self.async_step_site()
            if user_input[CONF_ENTRY_TYPE] == ENTRY_TYPE_RESOLVE_VEHICLE:
                return await self.async_step_resolve_vehicle()
            return await self.async_step_charger_type()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENTRY_TYPE, default=ENTRY_TYPE_CHARGER
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                ENTRY_TYPE_CHARGER,
                                ENTRY_TYPE_SITE,
                                # Only offered when detection has something it refuses to guess.
                                *(
                                    [ENTRY_TYPE_RESOLVE_VEHICLE]
                                    if discover_ambiguous_vehicles(self.hass)
                                    else []
                                ),
                            ],
                            translation_key="entry_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_resolve_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resolve (or dismiss) a vehicle detection refuses to guess between.

        Creates no config entry: records one decision in the discovery-decision store, then aborts.
        """
        ambiguous = discover_ambiguous_vehicles(self.hass)
        if not ambiguous:
            return self.async_abort(reason="no_ambiguous_vehicles")
        if user_input is not None:
            self._resolve_vehicle_device_id = user_input["device_id"]
            return await self.async_step_resolve_vehicle_soc()
        return self.async_show_form(
            step_id="resolve_vehicle",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                ambiguous_vehicle_option(self.hass, candidate)
                                for candidate in ambiguous
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_resolve_vehicle_soc(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which of a device's battery sensors is its state of charge, or say the device is not a
        vehicle. The dismissal goes through the same store as the `dismiss_vehicle` service.
        """
        device_id = self._resolve_vehicle_device_id
        candidate = next(
            (item for item in discover_ambiguous_vehicles(self.hass) if item.id == device_id),
            None,
        )
        if candidate is None:
            # No longer ambiguous (another resolution landed or its entities changed): start over.
            return self.async_abort(reason="no_ambiguous_vehicles")
        if user_input is not None:
            choice = user_input["soc_entity_id"]
            store = await async_setup_decisions(self.hass)
            if choice == DISMISS_VEHICLE_CHOICE:
                await store.async_dismiss(DECISION_DOMAIN_VEHICLE, device_id)
                # Repairs follows every decision, wherever it was made (see repairs.py).
                await async_sync_resolution_repairs(self.hass)
                return self.async_abort(reason="vehicle_dismissed")
            await store.async_confirm(
                DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": choice}
            )
            await async_sync_resolution_repairs(self.hass)
            return self.async_abort(reason="vehicle_resolved")
        options = [entity_option(self.hass, entity_id) for entity_id in candidate.candidate_entity_ids]
        options.append(
            selector.SelectOptionDict(
                value=DISMISS_VEHICLE_CHOICE,
                label=RESOLVE_VEHICLE_TEXT[flow_language(self.hass)]["dismiss"],
            )
        )
        return self.async_show_form(
            step_id="resolve_vehicle_soc",
            data_schema=vol.Schema(
                {
                    vol.Required("soc_entity_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders={"name": candidate.name},
        )

    async def async_step_charger_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose automatic detection (which covers OCPP) or generic Home Assistant entities."""
        if user_input is not None:
            self._mode = user_input[CONF_MODE]
            if self._mode == MODE_DETECTED:
                return await self.async_step_detect_device()
            return await self.async_step_generic()
        return self.async_show_form(
            step_id="charger_type",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODE, default=MODE_DETECTED): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[MODE_DETECTED, MODE_GENERIC],
                            translation_key="mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_ocpp_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Automatic flow only: one unambiguous answer is confirmed rather than asked for."""
        switches, _ = self._entities_for_device(self._device_id)
        control, current_limit = await self._current_default(switches)
        if len(switches) != 1 or not control or self._probe_unreachable:
            return await self.async_step_ocpp_entities()
        if user_input is not None:
            if user_input.get("adjust"):
                return await self.async_step_ocpp_entities()
            errors = validate_charger_entities(
                self.hass, charge_control=switches[0], current_limit=current_limit
            )
            if not errors:
                return await self._create_entry(
                    {
                        CONF_CHARGE_CONTROL: switches[0],
                        CONF_CURRENT_CONTROL: control,
                        CONF_CURRENT_LIMIT: current_limit or "",
                    }
                )
            return await self.async_step_ocpp_entities()
        name = self._options(switches)[switches[0]]
        register = self._resolved_energy_register(switches)
        register_state = self.hass.states.get(register) if register else None
        current = (
            f"the charger's current number ({self._options([current_limit])[current_limit]})"
            if control == CURRENT_CONTROL_NUMBER and current_limit
            else "OCPP ChangeConfiguration"
        )
        return self.async_show_form(
            step_id="ocpp_confirm",
            data_schema=vol.Schema({vol.Optional("adjust", default=False): bool}),
            description_placeholders={
                "charge_control": name,
                "current": current,
                "energy_meter": (register_state.name if register_state else register) or "none found",
            },
        )

    async def async_step_ocpp_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select only suitable entities belonging to the chosen OCPP device."""
        switches, numbers = self._entities_for_device(self._device_id)
        if not switches:
            return self.async_abort(reason="no_charge_control")
        schema: dict[Any, Any] = {}
        if len(switches) == 1:
            # One candidate: preselected, but still a visible field.
            schema[vol.Required(CONF_CHARGE_CONTROL, default=switches[0])] = vol.In(
                self._options(switches)
            )
        else:
            schema[vol.Required(CONF_CHARGE_CONTROL)] = vol.In(self._options(switches))
        control, current_limit = await self._current_default(switches)
        if numbers:
            # Read for the charger's own setpoint and min/max; written only when "number" is chosen
            # as the way to set the current.
            limit_key: Any = vol.Optional(CONF_CURRENT_LIMIT)
            if current_limit in numbers:
                limit_key = vol.Optional(CONF_CURRENT_LIMIT, default=current_limit)
            schema[limit_key] = vol.In(self._options(numbers))
        # The connector's own `Energy.Active.Import.Register` sensor resolves automatically
        # (`ocpp_identity.energy_register_entity_for`); it is prefilled here and can be overridden
        # for chargers that expose it differently.
        energy_register = self._resolved_energy_register(switches)
        schema[
            vol.Optional(
                CONF_ENERGY_REGISTER_ENTITY,
                description={"suggested_value": energy_register} if energy_register else None,
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        )
        if numbers or [
            entity_id
            for entity_id in switches
            if charge_control_target_from_entity_id(entity_id) is not None
        ]:
            # Offered whenever a connector can be established (a 0.12 charge control entity or a
            # connector-scoped current entity); with none there is nothing to command.
            schema[vol.Optional(CONF_CURRENT_CONTROL, default=control)] = current_control_selector(
                self.hass,
                (CURRENT_CONTROL_CHANGE_CONFIGURATION, CURRENT_CONTROL_NUMBER) if numbers
                else (CURRENT_CONTROL_CHANGE_CONFIGURATION,),
            )
        unanswered = {"base": "charger_no_answer"}
        if user_input is not None and self._notice_shown:
            # The form was shown because the charger did not answer; an untouched resubmit asks again.
            untouched = not user_input.get(CONF_CURRENT_CONTROL) and not user_input.get(CONF_CURRENT_LIMIT)
            if untouched and not self._probe_unreachable and control:
                self._notice_shown = False
                return await self.async_step_ocpp_confirm()
            if untouched and self._probe_unreachable and not self._reprobed:
                self._reprobed = True
                return self.async_show_form(
                    step_id="ocpp_entities", data_schema=vol.Schema(schema), errors=unanswered
                )
        if user_input is None and self._probe_unreachable:
            self._notice_shown = True
            return self.async_show_form(
                step_id="ocpp_entities", data_schema=vol.Schema(schema), errors=unanswered
            )
        if user_input is not None:
            errors = validate_charger_entities(
                self.hass,
                charge_control=user_input[CONF_CHARGE_CONTROL],
                current_limit=user_input.get(CONF_CURRENT_LIMIT) or None,
            )
            if not errors:
                return await self._create_entry(user_input)
            return self.async_show_form(
                step_id="ocpp_entities", data_schema=vol.Schema(schema), errors=errors
            )
        return self.async_show_form(step_id="ocpp_entities", data_schema=vol.Schema(schema))

    def _resolved_energy_register(self, switches: list[str]) -> str | None:
        """The energy register sensor of the one connector on offer, if the integration exposes it."""
        if len(switches) != 1:
            return None
        target = charge_control_target_from_entity_id(switches[0])
        return energy_register_entity_for(self.hass, target) if target is not None else None

    async def _current_default(self, switches: list[str]) -> tuple[str, str | None]:
        """How the current is set by default, decided by what the charger answers, never by its make.

        With one connector on offer, the charge point is asked (read only, bounded) for
        `AssignedCurrent`: a list with this connector means `ChangeConfiguration`; an answer without
        it means not supported, and then the connector's own session-current number is used if there
        is exactly one; otherwise nothing. No answer at all (`self._probe_unreachable`) decides
        nothing and is not remembered: a write path is never guessed. Returns
        `(current_control, current_limit entity or None)`.
        """
        self._probe_unreachable = False
        if len(switches) != 1:
            return "", None
        target = charge_control_target_from_entity_id(switches[0])
        if target is None:
            return "", None
        key = (switches[0], tuple(self._ocpp_device_ids or ()), self._device_id)
        if self._current_default_cache is not None and self._current_default_cache[0] == key:
            return self._current_default_cache[1]
        outcome = await self._probe_assigned_current(switches[0], target)
        if outcome == PROBE_UNREACHABLE:
            self._probe_unreachable = True
            return "", None
        if outcome == PROBE_SUPPORTED:
            answer: tuple[str, str | None] = (CURRENT_CONTROL_CHANGE_CONFIGURATION, None)
        else:
            _, numbers = self._entities_for_device(self._device_id)
            own = [n for n in numbers if session_limit_target_from_entity_id(n) == target]
            answer = (CURRENT_CONTROL_NUMBER, own[0]) if len(own) == 1 else ("", None)
        self._current_default_cache = (key, answer)
        return answer

    async def _probe_assigned_current(self, charge_control: str, target: Any) -> str:
        """Ask the charge point whether it carries an `AssignedCurrent` entry for this connector.

        Three outcomes, never conflated: the charger answered with this connector in the list
        (supported), the charger answered with anything else (not supported), or it could not be asked
        (unreachable: the service raised, timed out, or the charge point is unavailable).
        """
        state = self.hass.states.get(charge_control)
        if state is not None and state.state == STATE_UNAVAILABLE:
            return PROBE_UNREACHABLE
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                value = await read_assigned_current_value(self.hass, target.devid)
        except Exception:  # timeouts included
            return PROBE_UNREACHABLE
        if (
            isinstance(value, str)
            and value
            and assigned_amps_for_connector(value, target.connector_id) is not None
        ):
            return PROBE_SUPPORTED
        return PROBE_NOT_SUPPORTED

    async def async_step_detect_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the charger's device; its controls are then found from its integration's entities.

        Device first, entities second: the entities are matched by platform and key on this one device
        (`charger_detection.detect_charger`), never by name, and nothing is applied before the next
        step shows it.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._is_ocpp_device(user_input["device"]):
                # OCPP has no profile of its own: it keeps its proven path, prefilled from this device.
                self._mode = MODE_OCPP
                self._device_id = user_input["device"]
                self._ocpp_device_ids, self._ocpp_connector = self._ocpp_scope(self._device_id)
                switches, _ = self._entities_for_device(self._device_id)
                if len(switches) == 1 and (await self._current_default(switches))[0]:
                    return await self.async_step_ocpp_confirm()
                return await self.async_step_ocpp_entities()
            detected = detect_charger(self.hass, user_input["device"])
            if detected is None:
                errors["device"] = "device_not_found"
            elif detected.role == ROLE_EXCLUDED:
                return self.async_abort(reason="not_a_charger")
            elif detected.role is None:
                return self.async_abort(reason="charger_not_recognised")
            elif detected.role != ROLE_CHARGER and not detected.external_controller:
                return self.async_abort(reason="charger_not_controllable")
            else:
                self._device_id = user_input["device"]
                self._detected = detected
                if detected.conflicts and not detected.external_controller:
                    return await self.async_step_own_mode()
                return await self.async_step_detected_entities()
        options = self._charger_device_options()
        if not options:
            return self.async_abort(reason="no_supported_charger")
        return self.async_show_form(
            step_id="detect_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_own_mode(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The charger's own smart, solar or load-balancing mode is on: ask for it to be turned off.

        Two controllers on one charger fight every cycle. The modes are read again on submit, so
        turning it off and continuing just works; `continue_anyway` is the person's informed choice to
        keep going with one still on.
        """
        detected = self._detected
        assert detected is not None
        profile = profile_for(detected.platform)
        errors: dict[str, str] = {}
        if user_input is not None and profile is not None:
            registry = er.async_get(self.hass)
            entries = [
                entry
                for entry in er.async_entries_for_device(registry, detected.device_id)
                if entry.platform == profile.platform
            ]
            detected.conflicts = own_mode_conflicts(self.hass, entries, profile)
            if not detected.conflicts or user_input.get("continue_anyway"):
                return await self.async_step_detected_entities()
            errors["base"] = "own_mode_active"
        return self.async_show_form(
            step_id="own_mode",
            data_schema=vol.Schema({vol.Optional("continue_anyway", default=False): bool}),
            description_placeholders={
                "modes": ", ".join(
                    f"{self._entity_name(conflict.entity_id)} ({conflict.label}: {conflict.state})"
                    for conflict in detected.conflicts
                )
            },
            errors=errors,
        )

    async def async_step_detected_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm what detection found: the charge control, the current, the energy register and the
        charging-state sensor, each a suggestion that can be changed or cleared.

        With evcc or openWB present nothing is suggested: they may own this charger, and a choice made
        by a guess here would fight them.
        """
        detected = self._detected
        assert detected is not None
        suggest = not detected.external_controller
        text = _DETECTED_TEXT[flow_language(self.hass)]
        schema = self._detected_schema(detected, suggest=suggest)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._detected_errors(detected, user_input)
            if not errors:
                return await self._create_detected_entry(detected, user_input)
        return self.async_show_form(
            step_id="detected_entities",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device": detected.device_name,
                "warning": f"{text['external']}\n\n" if detected.external_controller else "",
                "disabled": (
                    f"\n\n{text['disabled']} "
                    + ", ".join(self._entity_name(entity_id) for entity_id in detected.disabled_useful)
                    if detected.disabled_useful
                    else ""
                ),
            },
        )

    def _entity_name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return state.name if state is not None else entity_id

    def _detected_schema(self, detected: DetectedCharger, *, suggest: bool) -> vol.Schema:
        def optional(key: str, value: Any) -> Any:
            if suggest and value:
                return vol.Optional(key, description={"suggested_value": value})
            return vol.Optional(key)

        profile = profile_for(detected.platform)
        kinds = tuple(
            kind
            for kind, applies in (
                (CURRENT_CONTROL_NUMBER, profile is not None and bool(profile.current_keys)),
                (CURRENT_CONTROL_EASEE, profile is not None and profile.easee_current),
            )
            if applies
        )
        control_field: Any = (
            vol.Required(CONF_CHARGE_CONTROL, description={"suggested_value": detected.charge_control})
            if suggest and detected.charge_control
            else vol.Required(CONF_CHARGE_CONTROL)
        )
        energy = detected.energy_register
        fields: dict[Any, Any] = {
            control_field: selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "select", "button", "sensor"])
            ),
            optional(CONF_CURRENT_LIMIT, detected.current_limit): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="number")
            ),
        }
        if kinds:
            fields[optional(CONF_CURRENT_CONTROL, detected.current_control)] = current_control_selector(
                self.hass, kinds
            )
        fields[optional(CONF_ENERGY_REGISTER_ENTITY, energy)] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        )
        if detected.session_energy_register and not energy:
            # A register that resets every session is offered only knowingly.
            fields[vol.Optional(CONF_ENERGY_REGISTER_IS_SESSION, default=False)] = bool
        fields[optional(FIELD_CHARGING_STATE_ENTITY, (detected.charging_state or {}).get("entity_id"))] = (
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        )
        if detected.disabled_useful:
            fields[vol.Optional("enable_disabled_entities", default=True)] = bool
        return vol.Schema(fields)

    def _detected_errors(self, detected: DetectedCharger, user_input: dict[str, Any]) -> dict[str, str]:
        charge_control = user_input[CONF_CHARGE_CONTROL]
        errors = dict(
            validate_charger_entities(
                self.hass,
                charge_control=charge_control,
                current_limit=user_input.get(CONF_CURRENT_LIMIT) or None,
            )
        )
        path = control_path_for_entity(
            self.hass,
            charge_control,
            detected_path=detected.control_path,
            charge_control_is_identity=charge_control == detected.charge_control,
        )
        if path is None:
            errors[CONF_CHARGE_CONTROL] = "control_path_unknown"
        control = user_input.get(CONF_CURRENT_CONTROL) or ""
        if control == CURRENT_CONTROL_NUMBER and not user_input.get(CONF_CURRENT_LIMIT):
            errors[CONF_CURRENT_CONTROL] = "current_limit_required"
        if control == CURRENT_CONTROL_EASEE and (path is None or path.get("kind") != "easee"):
            errors[CONF_CURRENT_CONTROL] = "current_control_unsupported"
        return errors

    async def _create_detected_entry(
        self, detected: DetectedCharger, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        charge_control = user_input[CONF_CHARGE_CONTROL]
        path = control_path_for_entity(
            self.hass,
            charge_control,
            detected_path=detected.control_path,
            charge_control_is_identity=charge_control == detected.charge_control,
        )
        energy = user_input.get(CONF_ENERGY_REGISTER_ENTITY) or ""
        is_session = False
        if not energy and user_input.get(CONF_ENERGY_REGISTER_IS_SESSION) and detected.session_energy_register:
            energy, is_session = detected.session_energy_register, True
        state_entity = user_input.get(FIELD_CHARGING_STATE_ENTITY) or ""
        profile = profile_for(detected.platform)
        data: dict[str, Any] = {
            CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_MODE: MODE_DETECTED,
            CONF_CHARGER_PLATFORM: detected.platform,
            CONF_CHARGE_CONTROL: charge_control,
            CONF_CONTROL_PATH: path,
            CONF_CURRENT_LIMIT: user_input.get(CONF_CURRENT_LIMIT) or "",
            CONF_CURRENT_CONTROL: user_input.get(CONF_CURRENT_CONTROL) or "",
            CONF_ENERGY_REGISTER_ENTITY: energy,
            CONF_ENERGY_REGISTER_IS_SESSION: is_session,
            CONF_CHARGING_STATE: (
                {
                    "entity_id": state_entity,
                    "charging_values": list(
                        (detected.charging_state or {}).get("charging_values")
                        or (profile.charging_values if profile is not None else ())
                    ),
                }
                if state_entity and (profile is None or profile.charging_values)
                else None
            ),
            CONF_CHARGER_CURRENT_ENTITIES: list(detected.current_entities),
            CONF_WEBHOOK_ID: secrets.token_urlsafe(32),
        }
        if user_input.get("enable_disabled_entities"):
            registry = er.async_get(self.hass)
            for entity_id in detected.disabled_useful:
                entry = registry.async_get(entity_id)
                if entry is not None and entry.disabled_by is not None:
                    registry.async_update_entity(entity_id, disabled_by=None)
        await self.async_set_unique_id(f"charge_control:{charge_control}")
        self._abort_if_unique_id_configured()
        return await self._finish_charger(detected.device_name, data)

    async def async_step_generic(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select generic Home Assistant switch and number entities."""
        schema = vol.Schema(
            {
                vol.Required(CONF_CHARGE_CONTROL): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional(CONF_CURRENT_LIMIT): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                # A generic charger has no OCPP identity, so this is the only route to
                # delivered-energy accounting for `manual_kwh`.
                vol.Optional(CONF_ENERGY_REGISTER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="energy")
                ),
            }
        )
        if user_input is not None:
            errors = validate_charger_entities(
                self.hass,
                charge_control=user_input[CONF_CHARGE_CONTROL],
                current_limit=user_input.get(CONF_CURRENT_LIMIT) or None,
            )
            if not errors:
                return await self._create_entry(user_input)
            return self.async_show_form(step_id="generic", data_schema=schema, errors=errors)
        return self.async_show_form(step_id="generic", data_schema=schema)

    async def _create_entry(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        charge_control = user_input[CONF_CHARGE_CONTROL]
        state = self.hass.states.get(charge_control)
        title = state.name if state else charge_control
        if self._mode == MODE_OCPP and self._device_id:
            device = dr.async_get(self.hass).async_get(self._device_id)
            if device is not None:
                title = device.name_by_user or device.name or title
        data: dict[str, Any] = {
            CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_MODE: self._mode,
            CONF_CHARGE_CONTROL: charge_control,
            CONF_CURRENT_LIMIT: user_input.get(CONF_CURRENT_LIMIT, ""),
            CONF_CURRENT_CONTROL: user_input.get(CONF_CURRENT_CONTROL) or "",
            CONF_ENERGY_REGISTER_ENTITY: user_input.get(CONF_ENERGY_REGISTER_ENTITY) or "",
            CONF_WEBHOOK_ID: secrets.token_urlsafe(32),
        }
        if self._mode == MODE_OCPP:
            # The explicit connector target from the chosen charge control. An unresolvable entity
            # is marked and the entry still created: the current is recorded but not applied.
            resolution = resolve_target(self.hass, charge_control=charge_control)
            apply_target(data, resolution)
        # One entry per physical charger: an OCPP connector by charge point and connector,
        # anything else by the switch that starts and stops it.
        charge_point = data.get(CONF_OCPP_CHARGE_POINT_ID)
        connector = data.get(CONF_OCPP_CONNECTOR_ID)
        await self.async_set_unique_id(
            f"ocpp:{charge_point}:{connector}"
            if charge_point and connector
            else f"charge_control:{charge_control}"
        )
        self._abort_if_unique_id_configured()
        return await self._finish_charger(title, data)

    async def _finish_charger(self, title: str, data: dict[str, Any]) -> ConfigFlowResult:
        """Create the charger entry, after offering to add it to the installation's one site."""
        sites = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE
        ]
        if len(sites) != 1:
            return self.async_create_entry(title=title, data=data)
        self._join_charger = (title, data)
        self._join_site_entry = sites[0]
        return await self.async_step_join_site()

    async def async_step_join_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer to add the new charger to the one site. Only a wiring detection can determine
        unambiguously (three phases, exactly one discovered measured-current source) is offered; with
        anything less the answer is "no", and the site's settings are where to add it."""
        assert self._join_charger is not None and self._join_site_entry is not None
        title, data = self._join_charger
        site = self._join_site_entry
        entity = er.async_get(self.hass).async_get(data[CONF_CHARGE_CONTROL])
        candidates = await async_discover_charger_current_sources(
            self.hass, charger_device_id=entity.device_id if entity is not None else None
        )
        wiring: dict[str, Any] | None = None
        if len(candidates) == 1:
            wiring = {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(candidates[0].mapping),
            }
        if user_input is not None:
            if wiring is not None and user_input.get("join", True):
                queue_site_join(self.hass, self.unique_id or "", site.entry_id, wiring)
            return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="join_site",
            data_schema=vol.Schema({vol.Optional("join", default=True): bool} if wiring else {}),
            description_placeholders={
                "site": site.title,
                "summary": site_join_summary(
                    self.hass, candidates[0] if wiring is not None else None
                ),
            },
        )

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Main fuse, safety margin, measurement mode and associated chargers: the smallest useful base
        step. Per-charger wiring and measurement entities follow in `async_step_site_details`; the maximum
        measurement age is an advanced option editable only via the options flow.
        """
        if user_input is not None:
            self._site_basic = user_input
            charger_entry_ids: list[str] = user_input.get(CONF_CHARGER_ENTRY_IDS, [])
            detection = detect_site_from_hass(
                self.hass, excluded_device_ids=charger_device_ids(self.hass, charger_entry_ids)
            )
            self._detected_meters = list(detection.meters)
            self._detected_batteries = list(detection.batteries)
            if self._detected_meters:
                return await self.async_step_site_detected()
            if user_input[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DIRECT:
                return await self.async_step_site_current_suggestions()
            return await self.async_step_site_details()
        return self.async_show_form(step_id="site", data_schema=site_basic_schema(self.hass))

    async def async_step_site_detected(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the grid meters found in the entity registry (including entities their integration
        ships disabled), never applied without this confirmation. Choosing one sets the measurement
        mode, pre-fills the next form and, when asked, enables its disabled entities on creation;
        "manual" continues with the mode chosen on the first step.
        """
        offer_enable = any(candidate.disabled_entity_ids for candidate in self._detected_meters)
        schema = site_detected_schema(
            self.hass,
            self._detected_meters,
            default_choice=self._detected_meters[0].candidate_id,
            offer_enable=offer_enable,
        )
        if user_input is not None:
            chosen = next(
                (c for c in self._detected_meters if c.candidate_id == user_input["choice"]), None
            )
            self._site_detected = chosen
            self._enable_detected_entities = bool(
                chosen is not None and offer_enable and user_input.get("enable_disabled", False)
            )
            if chosen is None:
                if self._site_basic[CONF_MEASUREMENT_MODE] == MEASUREMENT_MODE_DIRECT:
                    return await self.async_step_site_current_suggestions()
                return await self.async_step_site_details()
            self._site_basic = {**self._site_basic, CONF_MEASUREMENT_MODE: chosen.mode}
            self._site_current_source_choice = None
            self._site_current_source = None
            if chosen.current_source is not None:
                self._site_current_source_choice = "candidate"
                self._site_current_source = source_to_dict(chosen.current_source)
            return await self.async_step_site_confirm()
        return self.async_show_form(step_id="site_detected", data_schema=schema)

    async def _prepare_site_confirm(
        self,
    ) -> tuple[PendingSiteDetails, str, BatteryCandidate | None] | None:
        """What a defaults-only `site_details` submission would create, completed with what detection
        also found, plus its summary; `None` when any part is missing or ambiguous (the full form then).

        Every charger needs exactly one discovered measured-current source, since the form would
        otherwise start on "skip" and that is not something to confirm.
        """
        chosen = self._site_detected
        if chosen is None:
            return None
        charger_entry_ids: list[str] = self._site_basic.get(CONF_CHARGER_ENTRY_IDS, [])
        mode = self._site_basic[CONF_MEASUREMENT_MODE]
        skip_direct_fields = self._site_current_source_choice in (
            "candidate",
            MANUAL_ATTRIBUTES_CHOICE,
            SKIP_CHOICE,
        )
        charger_candidates = await async_charger_measured_candidates(self.hass, charger_entry_ids)
        direct_defaults, derived_defaults, flag_defaults = detected_defaults(chosen)
        schema = site_details_schema(
            self.hass,
            mode,
            direct_defaults=direct_defaults,
            derived_defaults=derived_defaults,
            skip_direct_fields=skip_direct_fields,
            flag_defaults=flag_defaults,
        )
        try:
            details = dict(schema({}))
        except vol.Invalid:
            return None
        if site_details_unit_errors(self.hass, details, mode, skip_direct_fields=skip_direct_fields):
            return None
        chargers = []
        charger_inputs: dict[str, dict[str, Any]] = {}
        for charger_entry_id in charger_entry_ids:
            found = charger_candidates.get(charger_entry_id) or []
            entry = self.hass.config_entries.async_get_entry(charger_entry_id)
            if len(found) != 1 or entry is None:
                return None
            wiring_defaults = charger_wiring_schema(
                self.hass, charger_entry_id, candidates=found
            )({})
            charger_inputs[charger_entry_id] = {
                **wiring_defaults,
                "measured_source": found[0].candidate_id,
            }
            chargers.append((entry.title, wiring_defaults["phases"], found[0]))
        batteries = [b for b in self._detected_batteries if b.integration == chosen.integration]
        battery = batteries[0] if len(batteries) == 1 else None
        summary = site_confirm_summary(self.hass, candidate=chosen, battery=battery, chargers=chargers)
        pending = PendingSiteDetails(
            details=details,
            charger_entry_ids=charger_entry_ids,
            mode=mode,
            skip_direct_fields=skip_direct_fields,
            charger_candidates=charger_candidates,
            charger_inputs=charger_inputs,
        )
        return pending, summary, battery

    async def async_step_site_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Everything the site needs was found: confirm it instead of showing the full form."""
        self._site_battery = None
        prepared = await self._prepare_site_confirm()
        if prepared is None or (user_input is not None and user_input.get("adjust")):
            return await self.async_step_site_details()
        pending, summary, battery = prepared
        if user_input is not None:
            errors = site_membership_errors(self.hass, charger_entry_ids=pending.charger_entry_ids)
            if errors:
                return self.async_show_form(
                    step_id="site",
                    data_schema=site_basic_schema(self.hass, defaults=self._site_basic),
                    errors=errors,
                )
            self._site_battery = battery
            return await self._create_site_entry(pending, {})
        return self.async_show_form(
            step_id="site_confirm",
            data_schema=vol.Schema({vol.Optional("adjust", default=False): bool}),
            description_placeholders={"summary": summary},
        )

    async def async_step_site_current_suggestions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer ranked suggestions for the site's total current, discovered from existing entities and
        never auto-applied, alongside the two manual shapes and "skip".

        Always shown when reached (direct mode): a site with no candidate is what manual entry is for.
        """
        charger_entry_ids: list[str] = self._site_basic.get(CONF_CHARGER_ENTRY_IDS, [])
        self._site_current_candidates = await async_discover_site_current_sources(
            self.hass, excluded_device_ids=charger_device_ids(self.hass, charger_entry_ids)
        )
        schema = site_current_suggestions_schema(
            self.hass,
            self._site_current_candidates,
            # A brand-new site defaults to "manual" (the guided per-phase pickers).
            default_choice=site_default_current_choice(self._site_current_candidates, None),
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
        """Collect the site's total current as one entity plus its per-phase attribute names.

        Single-shot (unlike the per-charger queue), with the mirror-image device scope: anything except
        entities on an associated charger's own device.
        """
        charger_entry_ids: list[str] = self._site_basic.get(CONF_CHARGER_ENTRY_IDS, [])
        outcome = await async_manual_source_step(
            self.hass,
            scope=site_device_scope(self.hass, charger_entry_ids),
            stored_source=self._site_current_source,
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
        """Per-charger phase wiring, plus the measurement entities for the mode chosen in
        `async_step_site`; never both modes' fields. The direct-mode pickers are skipped when the site's
        current was already resolved as a single generic source or deferred ("configure later").
        """
        charger_entry_ids: list[str] = self._site_basic.get(CONF_CHARGER_ENTRY_IDS, [])
        mode = self._site_basic[CONF_MEASUREMENT_MODE]
        skip_direct_fields = self._site_current_source_choice in (
            "candidate",
            MANUAL_ATTRIBUTES_CHOICE,
            SKIP_CHOICE,
        )
        direct_defaults, derived_defaults, flag_defaults = (
            detected_defaults(self._site_detected) if self._site_detected is not None else ({}, {}, {})
        )
        schema = site_details_schema(
            self.hass,
            mode,
            direct_defaults=direct_defaults,
            derived_defaults=derived_defaults,
            skip_direct_fields=skip_direct_fields,
            flag_defaults=flag_defaults,
        )
        if user_input is not None:
            # Authoritative re-check: another site may have claimed a charger since the picker.
            errors = site_membership_errors(self.hass, charger_entry_ids=charger_entry_ids)
            if errors:
                return self.async_show_form(
                    step_id="site",
                    data_schema=site_basic_schema(self.hass, defaults=self._site_basic),
                    errors=errors,
                )
            errors = site_details_unit_errors(
                self.hass, user_input, mode, skip_direct_fields=skip_direct_fields
            )
            if errors:
                return self.async_show_form(
                    step_id="site_details",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors=errors,
                )
            # Resolved once and reused by every charger's wiring step and the parser: per-charger
            # discovery is asynchronous (Recorder) while the form builders are synchronous.
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
                return await self._begin_charger_wiring(pending)
            return await self._create_site_entry(pending, {})
        return self.async_show_form(step_id="site_details", data_schema=schema)

    async def _finish_pending_site(self) -> ConfigFlowResult:
        """Save the site whose `site_details` submission started this run of manual entries (the create
        flow's half). `async` so both flows' step bodies read identically.
        """
        if self._pending_site is None:
            # Unreachable from the UI (`_pending_site` is stored first); abort if that ever fails.
            return self.async_abort(reason="no_pending_site")
        # The chargers' wiring steps took time: another site may have claimed one meanwhile.
        errors = site_membership_errors(
            self.hass, charger_entry_ids=self._pending_site.charger_entry_ids
        )
        if errors:
            return self.async_show_form(
                step_id="site",
                data_schema=site_basic_schema(self.hass, defaults=self._site_basic),
                errors=errors,
            )
        return await self._create_site_entry(self._pending_site, self._manual_measured_sources)

    async def _create_site_entry(
        self, pending: PendingSiteDetails, manual_sources: dict[str, dict[str, Any]]
    ) -> ConfigFlowResult:
        """Create the site entry from one `site_details` submission, directly or once every "manual"
        charger has been collected.
        """
        phase_wiring, direct_entities, derived_entities = parse_site_details(
            self.hass,
            pending.details,
            pending.charger_entry_ids,
            pending.mode,
            skip_direct_fields=pending.skip_direct_fields,
            charger_candidates=pending.charger_candidates,
            manual_sources=manual_sources,
            charger_inputs=pending.charger_inputs,
        )
        data = {
            CONF_ENTRY_TYPE: ENTRY_TYPE_SITE,
            # Creation needs a complete configuration, so the calculation starts on; it only reads.
            # Active control stays off below.
            CONF_SITE_ENABLED: True,
            # Off, and not a field of this flow: turning active control on is a later decision from
            # the options flow, and also needs the compile-time gate in `site/site_capacity.py`
            # (see `CONF_ACTIVE_CONTROL_ENABLED` in const.py).
            CONF_ACTIVE_CONTROL_ENABLED: False,
            CONF_MAIN_FUSE_A: self._site_basic[CONF_MAIN_FUSE_A],
            CONF_SAFETY_MARGIN_A: self._site_basic.get(CONF_SAFETY_MARGIN_A, 0.0),
            CONF_MEASUREMENT_MODE: pending.mode,
            CONF_CHARGER_ENTRY_IDS: pending.charger_entry_ids,
            CONF_PHASE_WIRING: phase_wiring,
            CONF_DIRECT_ENTITIES: direct_entities,
            CONF_DERIVED_ENTITIES: derived_entities,
            CONF_SITE_CURRENT_SOURCE: self._site_current_source,
            **parse_site_flags(pending.details, pending.mode),
            CONF_MAX_AGE_S: DEFAULT_MAX_AGE_S,
            # Active control's damping at its documented defaults (see const.py); tunable later.
            CONF_REGULATOR_DEADBAND_A: DEFAULT_REGULATOR_DEADBAND_A,
            CONF_REGULATOR_DWELL_S: DEFAULT_REGULATOR_DWELL_S,
            # Yield-verified stepping: off by default and set from the options flow. The ceiling is
            # deliberately not written: absent means "1.15x the main fuse", computed on every read,
            # so a stored copy could only go stale when the fuse is edited.
            CONF_YIELD_STEPPING_ENABLED: DEFAULT_YIELD_STEPPING_ENABLED,
        }
        if self._site_battery is not None:
            data = apply_battery_candidate(data, self._site_battery)
        name = self._site_basic.get("name") or default_site_name(self.hass)
        # A site is identified by the name it was created under, which survives later edits.
        await self.async_set_unique_id(f"site:{slugify(name)}")
        self._abort_if_unique_id_configured()
        if self._enable_detected_entities and self._site_detected is not None:
            enable_disabled_entities(self.hass, self._site_detected.disabled_entity_ids)
            if self._site_battery is not None:
                enable_disabled_entities(self.hass, self._site_battery.disabled_entity_ids)
        return self.async_create_entry(title=name, data=data)

    def _config_entry_domains(self) -> dict[str, str]:
        return {entry.entry_id: entry.domain for entry in self.hass.config_entries.async_entries()}

    def _charger_device_options(self) -> list[selector.SelectOptionDict]:
        """Devices that can be chosen as a charger, labelled by name and area, sorted by label.

        An OCPP device is listed only when it carries a charge-control switch itself, which hides the
        central system and a charge point whose connectors carry the control. Any other detectable
        platform's device is listed when it has at least one entity, which hides bridge and account
        devices.
        """
        domains = self._config_entry_domains()
        detectable = set(detectable_platforms())
        devices = dr.async_get(self.hass)
        entities = er.async_get(self.hass)
        areas = ar.async_get(self.hass)
        options: list[tuple[str, str]] = []
        for device in devices.devices:
            device_domains = {domains.get(entry_id) for entry_id in device.config_entries}
            if self._is_ocpp_device(device.id):
                if not self._carries_charge_control(device.id):
                    continue
            elif not (device_domains & detectable) or not er.async_entries_for_device(
                entities, device.id
            ):
                continue
            label = device.name_by_user or device.name or device.id
            area = areas.async_get_area(device.area_id) if device.area_id else None
            if area is not None:
                label = f"{label} \u2014 {area.name}"
            options.append((label, device.id))
        return [
            selector.SelectOptionDict(value=value, label=label)
            for label, value in sorted(options, key=lambda item: (item[0].casefold(), item[1]))
        ]

    def _carries_charge_control(self, device_id: str) -> bool:
        """Whether the device has an enabled OCPP charge-control switch of its own (named by its
        entity id or, as the integration registers it, by its unique id)."""
        return any(
            entry.domain == "switch"
            and entry.platform == "ocpp"
            and entry.disabled_by is None
            and (
                entry.entity_id.endswith("_charge_control")
                or str(entry.unique_id or "").endswith("charge_control")
            )
            for entry in er.async_entries_for_device(er.async_get(self.hass), device_id)
        )

    def _is_ocpp_device(self, device_id: str) -> bool:
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            return False
        if "ocpp" in identifier_domains(device):
            return True
        domains = self._config_entry_domains()
        return any(domains.get(config_entry_id) == "ocpp" for config_entry_id in device.config_entries)

    def _ocpp_scope(self, device_id: str) -> tuple[list[str], int | None]:
        """Where an OCPP device's charge control is looked for, and the connector it fixes.

        A device that carries a charge control itself is used as it is (a connector device selects
        that connector, a one-connector charge point its only one). A charge point that carries none
        offers the connectors of its child devices. A connector device whose switch sits on the
        charge point gets the charge point's, narrowed to the connector named in its own name.
        """
        if self._entities_for_device(device_id, connector=None, scoped=False)[0]:
            return [device_id], None
        devices = dr.async_get(self.hass)
        children = [
            device.id for device in devices.devices if device.via_device_id == device_id
        ]
        if any(self._entities_for_device(child, connector=None, scoped=False)[0] for child in children):
            return children, None
        device = devices.async_get(device_id)
        if device is not None and device.via_device_id:
            match = re.search(r"(\d+)\s*$", device.name_by_user or device.name or "")
            if match:
                return [device.via_device_id], int(match.group(1))
        return [device_id], None

    def _entities_for_device(
        self, device_id: str | None, *, connector: int | None = None, scoped: bool = True
    ) -> tuple[list[str], list[str]]:
        if scoped:
            connector = self._ocpp_connector
            device_ids = self._ocpp_device_ids or ([device_id] if device_id else [])
        else:
            device_ids = [device_id] if device_id else []
        registry = er.async_get(self.hass)
        entries = [
            entry for one in device_ids for entry in er.async_entries_for_device(registry, one)
        ]
        all_switches = sorted(
            entry.entity_id for entry in entries
            if entry.domain == "switch" and entry.platform == "ocpp" and entry.disabled_by is None
        )
        switches = [
            entity_id for entity_id in all_switches
            if entity_id.endswith("_charge_control")
        ] or all_switches
        numbers = sorted(
            entry.entity_id for entry in entries
            if entry.domain == "number"
            and entry.platform == "ocpp"
            and entry.disabled_by is None
            and self.hass.states.get(entry.entity_id) is not None
            and self.hass.states.get(entry.entity_id).attributes.get("unit_of_measurement") == "A"
            # The station-wide ceiling is never offered as a control: it names no connector, so it
            # could not establish a target, and writing it would change a persistent safety value for
            # the whole charger. Diagnostics report it as the ceiling it is.
            and not is_station_maximum(entry.entity_id)
        )
        if connector is not None:
            switches = [
                entity_id
                for entity_id in switches
                if (target := charge_control_target_from_entity_id(entity_id)) is None
                or target.connector_id == connector
            ]
        return switches, numbers

    def _options(self, entity_ids: list[str]) -> dict[str, str]:
        return {
            entity_id: self.hass.states.get(entity_id).name
            if self.hass.states.get(entity_id)
            else entity_id
            for entity_id in entity_ids
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
            return SiteCapacityOptionsFlow(config_entry)
        return SpotNavChargingOptionsFlow(config_entry)
