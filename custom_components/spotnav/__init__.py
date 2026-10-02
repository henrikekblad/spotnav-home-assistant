"""SpotNav charging control integration.

Two kinds of config entry (a charger and a site) plus an installation-wide layer. Everything an
entry starts is stopped by `entry.async_on_unload` where it is started; live objects are on
`entry.runtime_data` (see `runtime.py`).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, Event, HomeAssistant

from .api.dashboard import async_setup_dashboard_api
from .api.entity_config import async_setup_entity_config_api
from .api.manual_action import async_setup_manual_action_api
from .api.market import async_setup_market_api
from .api.pairing import async_register_pairing
from .api.settings import async_setup_settings_api
from .api.site_settings import async_setup_site_settings_api
from .api.webhook import async_register_charger_webhook
from .card_asset import async_setup_card_asset
from .const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_SITE,
    PLATFORMS,
    SITE_PLATFORMS,
)
from .execution.auto_execution import AutoExecutor, pause_blocks_execution
from .execution.controller import ChargingController
from .execution.solar_execution import (
    async_rebind_solar_execution,
    async_setup_solar_execution,
    solar_execution_state,
)
from .execution.target_stop import resolve_charger_soc_reading, resolve_soc_reading, SocReading
from .planning.auto_controller import (
    async_remove_auto_state,
    AutoPlannerController,
    live_vehicle_facts,
    LiveVehicleFacts,
)
from .planning.auto_settings import async_setup_auto_settings, DRIVER_TARGET_SOC
from .planning.first_run import async_seed_first_run
from .pricing.market_observation import MarketObservation
from .pricing.price_refresh import async_setup_price_refresh
from .pricing.price_repository import async_setup_price_repository
from .repairs import async_arm_resolution_sync, async_sync_resolution_repairs
from .runtime import (
    charger_data,
    ChargerConfigEntry,
    ChargerData,
    domain_data,
    SiteConfigEntry,
    SiteData,
)
from .services import async_register_services
from .site.site_join import async_apply_site_join, async_leave_sites, prune_missing_members
from .site.site_capacity_controller import SiteCapacityController
from .vehicles.discovery_decisions import async_setup_decisions
from .vehicles.soc_estimate import SocReader
from .vehicles.vehicle_discovery import resolve_target_vehicle, vehicle_soc_entity_id
from .vehicles.vehicle_properties import consumption_kwh_per_10km, stored_capacity_kwh


_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Integration-wide setup: the singletons, their services, and the reads.

    Runs once, before any entry; `async_setup_entry` runs per entry and would register services
    repeatedly.
    """
    data = domain_data(hass)
    await async_setup_decisions(hass)
    # Every outstanding decision becomes one fixable Repairs issue (see repairs.py), re-synced on
    # every mutation. Domain setup is never unloaded, so the cancel callable is kept, not called.
    await async_sync_resolution_repairs(hass)
    data.resync_cancel = await async_arm_resolution_sync(hass)
    # One price repository and one refresh manager for the installation. Setup only constructs them;
    # the manager polls nothing until an area has a subscriber and stops with Home Assistant.
    repository = await async_setup_price_repository(hass)
    await async_setup_price_refresh(hass, repository)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, partial(_async_stop, hass))
    await async_setup_auto_settings(hass)
    async_register_services(hass)
    async_setup_dashboard_api(hass)
    async_setup_market_api(hass)
    async_setup_settings_api(hass)
    async_setup_manual_action_api(hass)
    async_setup_site_settings_api(hass)
    async_setup_entity_config_api(hass)
    # Static route for the bundled card asset, versioned by the manifest.
    await async_setup_card_asset(hass)
    async_register_pairing(hass)
    return True


async def _async_stop(hass: HomeAssistant, _event: Event | None = None) -> None:
    """Home Assistant is stopping: silence the boundaries and previews, then the price manager.

    Settings and installed plans are left alone; stopping Home Assistant is not an instruction to
    stop charging.
    """
    loaded = [
        data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if (data := charger_data(hass, entry.entry_id)) is not None
    ]
    for data in loaded:
        if data.executor is not None:
            await data.executor.async_shutdown()
    for data in loaded:
        if data.preview is not None:
            await data.preview.async_shutdown()
    manager = domain_data(hass).price_refresh
    if manager is not None:
        await manager.async_shutdown()
        domain_data(hass).price_refresh = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        loaded = await _async_setup_site_entry(hass, entry)
    else:
        loaded = await _async_setup_charger_entry(hass, entry)
    if loaded:
        # An entry that arrives or changes can make two chargers one, or give a site its first charger.
        await async_sync_resolution_repairs(hass)
    return loaded


async def _async_setup_charger_entry(hass: HomeAssistant, entry: ChargerConfigEntry) -> bool:
    """One charger: its controller, its authority boundary, solar, Auto, its webhook, its entities."""

    def _read_soc(vehicle_id: str | None) -> SocReading | None:
        """Read the state of charge a plan's target is enforced against.

        The charger-side source is preferred while live; the vehicle reading is the fallback, resolved
        by `vehicle_discovery` so the reported and the enforced value are the same number. A sleeping
        car yields a reading with no usable value, not "no vehicle".
        """
        charger_reading = resolve_charger_soc_reading(hass, controller.charge_control)
        if charger_reading is not None and charger_reading.soc_percent is not None:
            return charger_reading
        return resolve_soc_reading(
            hass,
            vehicle_id=vehicle_id,
            entity_id=vehicle_soc_entity_id(hass, vehicle_id),
        )

    # The one state-of-charge source both the planner and the stop decision read.
    soc_reader = SocReader(
        hass,
        entry.entry_id,
        raw_reader=_read_soc,
        register_entity_id=lambda: controller.energy_register_entity_id,
        charge_control=lambda: controller.charge_control,
        remembered_capacity=lambda vehicle_id: stored_capacity_kwh(hass, vehicle_id),
    )
    await soc_reader.async_load()
    entry.async_on_unload(soc_reader.async_shutdown)
    controller = ChargingController(hass, entry.entry_id, dict(entry.data), soc_reader=soc_reader.read)
    data = entry.runtime_data = ChargerData(controller=controller, soc_reader=soc_reader)
    entry.async_on_unload(controller.async_shutdown)

    settings_store = domain_data(hass).auto_store
    price_manager = domain_data(hass).price_refresh
    if settings_store is not None:
        # The boundary comes first so the preview can apply on the first calculation of a reload.
        executor = data.executor = AutoExecutor(hass, controller, settings_store)
        entry.async_on_unload(executor.async_shutdown)
        await executor.async_start()
        # Solar execution needs only the executor, controller and settings store.
        solar = data.solar = async_setup_solar_execution(
            hass, entry.entry_id, controller, executor, settings_store
        )
        entry.async_on_unload(solar.async_stop)
        # Hybrid window-end handoff: inert for a charger not on `solar`/`hybrid`
        # (the coordinator reports `state=None`).
        controller.set_end_window_guard(
            lambda: (state := solar_execution_state(hass, entry.entry_id)) is not None
            and state.state in ("on", "disarming")
        )
        # A charge that starts by itself outside a window is held back, except while Auto is paused
        # by the person (an Auto plan only) or solar execution is running the charger.
        controller.set_hold_guard(
            lambda: (
                (plan := controller.plan) is not None
                and plan.auto_owned
                and pause_blocks_execution(settings_store.settings(entry.entry_id))
            )
            or (
                (state := solar_execution_state(hass, entry.entry_id)) is not None
                and state.state in ("on", "arming", "disarming")
            )
        )
        if price_manager is not None:
            await _async_setup_auto_preview(hass, entry, data, settings_store, price_manager)

    async_register_charger_webhook(hass, entry)
    await controller.async_initialize()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # A charger the flow was asked to add to the site joins it now that its entry id exists.
    await async_apply_site_join(hass, entry)
    # After the join, so the site's wiring and fuse are known to the defaults.
    await async_seed_first_run(hass, entry, controller, data.preview)
    return True


async def _async_setup_auto_preview(
    hass: HomeAssistant,
    entry: ChargerConfigEntry,
    data: ChargerData,
    settings_store,
    price_manager,
) -> None:
    """The charger's market display observation and Auto preview, beside its controller."""
    entry_id = entry.entry_id
    assert data.executor is not None

    def _live_vehicle_facts(vehicle_id: str) -> LiveVehicleFacts | None:
        return live_vehicle_facts(hass, data.soc_reader, vehicle_id)

    def _consumption(vehicle_id: str) -> float | None:
        return consumption_kwh_per_10km(hass, resolve_target_vehicle(hass, vehicle_id or None)[0])

    # The display need for the market is independent of execution; shared with the preview's
    # per-area stream and released on this entry's unload.
    observation = data.observation = MarketObservation(hass, entry_id, settings_store, price_manager)
    entry.async_on_unload(observation.async_close)
    preview = data.preview = AutoPlannerController(
        hass,
        entry_id,
        settings_store,
        price_manager,
        executor=data.executor,
        vehicle_reader=_live_vehicle_facts,
        consumption_reader=_consumption,
        observation=observation,
    )
    entry.async_on_unload(preview.async_shutdown)
    await preview.async_start()

    def _soc_moved() -> None:
        # New session or real progress: plan again from the current state of charge.
        if settings_store.settings(entry_id).driver == DRIVER_TARGET_SOC:
            entry.async_create_task(hass, preview.async_recalculate(), f"{entry_id} recalculate")

    data.soc_reader.set_on_reading(_soc_moved)
    # One shared market fetch per installation; awaited so entities exist when setup returns.
    await price_manager.async_ensure_catalogue()
    # The boundary reads the live price identity from the preview and publishes through it.
    data.executor.attach_preview(preview)


async def _async_setup_site_entry(hass: HomeAssistant, entry: SiteConfigEntry) -> bool:
    """Set up a site capacity (dynamic load balancing) entry. Registers no webhook."""
    if hass.state is not CoreState.running:
        # At boot every entry is known once Home Assistant has started: then drop members a former
        # version left behind when their charger was deleted, and reload if any were.
        # Not tied to the entry's unload: a once-listener that has fired cannot be removed again.
        async def _prune(_event: Event) -> None:
            if entry.state is ConfigEntryState.LOADED and prune_missing_members(hass, entry):
                await hass.config_entries.async_reload(entry.entry_id)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _prune)
    controller = SiteCapacityController(hass, entry.entry_id, dict(entry.data))
    entry.runtime_data = SiteData(controller)
    entry.async_on_unload(controller.async_shutdown)
    await controller.async_initialize()
    # Entry load order is not guaranteed: rebind every member charger's solar coordinator now
    # that this controller exists (also covers this site reloading).
    async_rebind_solar_execution(hass, list(entry.data.get(CONF_CHARGER_ENTRY_IDS) or []))
    await hass.config_entries.async_forward_entry_setups(entry, SITE_PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a SpotNav entry.

    Only the platforms are unloaded here; everything else stops through `async_on_unload`. A failed
    platform unload leaves the entry loaded and running. An unload is not an instruction to stop
    charging.
    """
    platforms = SITE_PLATFORMS if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE else PLATFORMS
    return await hass.config_entries.async_unload_platforms(entry, platforms)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """An entry was deleted for good: take a charger out of its site and remove its own Auto state.

    Unload does not come here (a reload must find its settings). Charger entries only; a site entry
    never writes settings.
    """
    await async_sync_resolution_repairs(hass, (entry.entry_id,))
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return
    async_leave_sites(hass, entry.entry_id)
    await async_remove_auto_state(hass, entry.entry_id)
    await SocReader.async_remove_stored(hass, entry.entry_id)
