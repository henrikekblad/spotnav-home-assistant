"""SpotNav charging control integration.

Two kinds of config entry (a charger and a site) plus an installation-wide layer. Everything an
entry starts is stopped by `entry.async_on_unload` where it is started; live objects are on
`entry.runtime_data` (see `runtime.py`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started

from .api.dashboard import async_setup_dashboard_api
from .api.debug import async_setup_debug_api
from .api.entity_config import async_setup_entity_config_api
from .api.camera import async_setup_camera_api
from .api.identification import async_setup_identification_api
from .api.manual_action import async_setup_manual_action_api
from .api.market import async_setup_market_api
from .api.region import async_setup_region_api
from .api.pairing import async_register_pairing
from .api.sessions import async_setup_sessions_api
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
from .energy_register import async_store_detected_register, async_watch_for_register
from .entity_renames import async_setup_entity_renames
from .log_buffer import attach_log_buffer
from .execution.auto_execution import AutoExecutor, pause_blocks_execution
from .execution.controller import ChargingController
from .execution.min_soc_floor import FloorInputs, MinSocFloor
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
from .planning.phases import async_migrate_settings_phases
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
from .notifications.notifier import ChargerNotifier
from .notifications.push import ChargerPush
from .execution.ownership_coverage import OwnershipCoverage
from .vehicles.camera_identification import CameraIdentification
from .vehicles.camera_pictures import ReferenceStore
from .vehicles.identification import VehicleIdentifier
from .vehicles.vehicle_target import async_adopt as async_adopt_vehicle_target, async_setup_vehicle_targets
from .sessions.inputs import current_fiscal, price_book_for, session_facts, soc_percent_now
from .sessions.history_import import HistoryImporter, START_DELAY_S as HISTORY_IMPORT_DELAY_S
from .sessions.recorder import SessionRecorder
from .sessions.store import SessionStore
from .site.site_join import (
    async_apply_site_join,
    async_leave_sites,
    backfill_profile_measured_sources,
    prune_missing_members,
)
from .site.site_capacity_controller import SiteCapacityController
from .texts import async_load as async_load_texts
from .vehicles.discovery_decisions import async_setup_decisions
from .vehicles.soc_estimate import SocReader
from .vehicles.vehicle_refresh import async_ask_vehicle_update
from .vehicles.vehicle_discovery import charger_vehicle_ids, resolve_target_vehicle, vehicle_soc_entity_id
from .vehicles.vehicle_properties import consumption_kwh_per_10km, stored_capacity_kwh, stored_properties


_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Integration-wide setup: the singletons, their services, and the reads.

    Runs once, before any entry; `async_setup_entry` runs per entry and would register services
    repeatedly.
    """
    data = domain_data(hass)
    # SpotNav's own words (`i18n/<lang>.json`), read once off the event loop.
    await async_load_texts(hass)
    # Keep SpotNav's own recent log records for the debug bundle; changes no log level.
    data.log_buffer = attach_log_buffer()
    await async_setup_decisions(hass)
    # Every outstanding decision becomes one fixable Repairs issue (see repairs.py), re-synced on
    # every mutation. Domain setup is never unloaded, so the cancel callable is kept, not called.
    await async_sync_resolution_repairs(hass)
    data.resync_cancel = await async_arm_resolution_sync(hass)
    # An entity renamed in Home Assistant is renamed in the entries that store it.
    async_setup_entity_renames(hass)
    # One price repository and one refresh manager for the installation. Setup only constructs them;
    # the manager polls nothing until an area has a subscriber and stops with Home Assistant.
    repository = await async_setup_price_repository(hass)
    await async_setup_price_refresh(hass, repository)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, partial(_async_stop, hass))
    await async_setup_auto_settings(hass)
    # A car's target is the car's at every charger (`vehicles/vehicle_target.py`).
    async_setup_vehicle_targets(hass)
    # The charge sessions' record, loaded before any charger entry starts recording into it.
    data.session_store = SessionStore(hass)
    data.session_store.set_fiscal_resolver(lambda charger_id, area_id: current_fiscal(hass, charger_id, area_id))
    await data.session_store.async_load()
    async_register_services(hass)
    async_setup_dashboard_api(hass)
    async_setup_sessions_api(hass)
    async_setup_market_api(hass)
    async_setup_region_api(hass)
    async_setup_settings_api(hass)
    async_setup_manual_action_api(hass)
    async_setup_site_settings_api(hass)
    async_setup_entity_config_api(hass)
    async_setup_identification_api(hass)
    async_setup_camera_api(hass)
    async_setup_debug_api(hass)
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
        # No entry is unloaded, so no controller's shutdown saves what the charge-ownership core still has unsaved.
        await data.controller.async_flush_session()
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

    # A detected charger stored without its lifetime register gets the one detection finds now, before
    # the controller reads its config (`energy_register.py`).
    async_store_detected_register(hass, entry)
    # The one state-of-charge source both the planner and the stop decision read.
    soc_reader = SocReader(
        hass,
        entry.entry_id,
        raw_reader=_read_soc,
        register_entity_id=lambda: controller.energy_register_entity_id,
        charge_control=lambda: controller.charge_control,
        remembered_capacity=lambda vehicle_id: stored_capacity_kwh(hass, vehicle_id),
        plugged_in_since=lambda: controller.plugged_in_since,
    )
    await soc_reader.async_load()
    entry.async_on_unload(soc_reader.async_shutdown)
    controller = ChargingController(
        hass,
        entry.entry_id,
        dict(entry.data),
        soc_reader=soc_reader.read,
        vehicle_limit_reader=soc_reader.vehicle_max_percent,
    )
    data = entry.runtime_data = ChargerData(controller=controller, soc_reader=soc_reader)
    entry.async_on_unload(controller.async_shutdown)

    settings_store = domain_data(hass).auto_store
    price_manager = domain_data(hass).price_refresh
    if settings_store is not None:
        # An older release's settings `phases` moves to the vehicle or the charger before anything plans.
        await async_migrate_settings_phases(hass, entry.entry_id)
        # The boundary comes first so the preview can apply on the first calculation of a reload.
        executor = data.executor = AutoExecutor(hass, controller, settings_store)
        entry.async_on_unload(executor.async_shutdown)
        await executor.async_start()
        # Solar execution needs only the executor, controller and settings store.
        solar = data.solar = async_setup_solar_execution(
            hass, entry.entry_id, controller, executor, settings_store
        )
        entry.async_on_unload(solar.async_stop)
        # A strategy change to `solar` hands a charge the sun keeps over to it instead of stopping it.
        executor.set_sun_keeps_probe(solar.sun_keeps_charge)
        entry.async_on_unload(lambda: executor.set_sun_keeps_probe(None))
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
        # The same guard's sun's part on its own, for the charge-ownership core's shadow (which decides the pause's).
        controller.set_solar_hold_probe(
            lambda: (state := solar_execution_state(hass, entry.entry_id)) is not None
            and state.state in ("on", "arming", "disarming")
        )
        if price_manager is not None:
            await _async_setup_auto_preview(hass, entry, data, settings_store, price_manager)

    # Before the webhook, whose `push_register` writes it.
    push = data.push = ChargerPush(hass, entry.entry_id)
    await push.async_load()
    async_register_charger_webhook(hass, entry)
    await controller.async_initialize()
    if data.executor is not None:
        # The restored record may carry an older release's person's Stop, and a manual Start's charge is
        # watched again.
        await data.executor.async_after_restore()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # After the platforms, so a smart plug's integrated-energy sensor already stands in for the register.
    _async_start_session_recorder(hass, entry, data, controller)
    # Still no register: take the charger's own as soon as its integration registers or reports one.
    async_watch_for_register(hass, entry, controller)
    await _async_start_notifier(hass, entry, data, controller)
    await _async_start_identifier(hass, entry, data, controller)
    # A charger the flow was asked to add to the site joins it now that its entry id exists.
    await async_apply_site_join(hass, entry)
    # After the join, so the site's wiring and fuse are known to the defaults.
    await async_seed_first_run(hass, entry, controller, data.preview)
    # A target stored before targets were the car's becomes its car's.
    await async_adopt_vehicle_target(hass, entry.entry_id)
    if (
        price_manager is not None
        and settings_store is not None
        and settings_store.settings(entry.entry_id).area_id is None
        and settings_store.suggested(entry.entry_id)
    ):
        # The relay was unreachable when the defaults were written, so the area is empty: suggest it
        # when the relay first answers, then stop listening.
        remove_listener: list[Any] = []

        @callback
        def _on_catalogue(snapshot) -> None:
            if snapshot.catalogue is None:
                return
            for remove in remove_listener:
                remove()
            remove_listener.clear()
            hass.async_create_task(async_seed_first_run(hass, entry, controller, data.preview))

        remove_listener.append(price_manager.add_catalogue_listener(_on_catalogue))
        entry.async_on_unload(lambda: [remove() for remove in remove_listener])
    return True


def _async_start_session_recorder(
    hass: HomeAssistant, entry: ChargerConfigEntry, data: ChargerData, controller: ChargingController
) -> None:
    """The charger's charge-session recorder: it watches the charge and keeps the record."""
    session_store = domain_data(hass).session_store
    if session_store is None:
        return
    recorder = data.sessions = SessionRecorder(
        hass,
        entry.entry_id,
        session_store,
        facts=lambda: session_facts(hass, controller),
        prices=lambda now: price_book_for(hass, entry.entry_id, now),
        consume_cause=controller.consume_start_cause,
        subscribe=controller.add_charge_state_listener,
        start_soc=lambda: soc_percent_now(hass, entry.entry_id),
    )
    entry.async_on_unload(recorder.async_shutdown)
    recorder.async_start()
    _async_schedule_history_import(hass, entry, data, controller, session_store)


async def _async_start_notifier(
    hass: HomeAssistant, entry: ChargerConfigEntry, data: ChargerData, controller: ChargingController
) -> None:
    """The charger's notifier: the chosen events to the chosen phones (`notifications/notifier.py`)."""
    store = domain_data(hass).auto_store
    if store is None:
        return

    def _currency() -> str | None:
        area_id = store.settings(entry.entry_id).area_id
        manager = domain_data(hass).price_refresh
        area = None if manager is None or area_id is None else manager.catalogue_snapshot().area(area_id)
        return None if area is None else area.currency

    notifier = data.notifier = ChargerNotifier(
        hass,
        entry.entry_id,
        name=lambda: entry.title,
        controller=controller,
        store=store,
        sessions=domain_data(hass).session_store,
        currency=_currency,
        push=data.push,
    )
    await notifier.async_load()
    entry.async_on_unload(notifier.async_shutdown)
    notifier.async_start(data.preview)


async def _async_start_identifier(
    hass: HomeAssistant, entry: ChargerConfigEntry, data: ChargerData, controller: ChargingController
) -> None:
    """Which car is plugged in, when more than one can charge here (`vehicles/identification.py`)."""
    store = domain_data(hass).auto_store
    if store is None:
        return
    camera = data.camera = CameraIdentification(hass, entry.entry_id, store)
    await camera.async_load()
    identifier = data.identifier = VehicleIdentifier(
        hass, entry.entry_id, controller=controller, store=store, notifier=data.notifier, camera=camera
    )
    entry.async_on_unload(identifier.async_shutdown)
    await identifier.async_load()
    identifier.async_start(data.preview)


def _async_schedule_history_import(
    hass: HomeAssistant,
    entry: ChargerConfigEntry,
    data: ChargerData,
    controller: ChargingController,
    session_store: SessionStore,
) -> None:
    """Import the charges from before the sessions feature, once, in the background after start-up."""
    importer = data.history_import = HistoryImporter(
        hass,
        entry.entry_id,
        session_store,
        register_entity=lambda: controller.energy_register_entity_id,
        energy_from_power=lambda: controller.energy_from_power,
    )

    async def _import(_now: datetime) -> None:
        await importer.async_run()

    @callback
    def _begin(_hass: HomeAssistant) -> None:
        entry.async_on_unload(async_call_later(hass, HISTORY_IMPORT_DELAY_S, _import))

    entry.async_on_unload(async_at_started(hass, _begin))
    # The same re-price once a night, so a long-running HA prices late-arriving history without a restart.
    entry.async_on_unload(importer.schedule_daily())


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
        return live_vehicle_facts(hass, data.soc_reader, vehicle_id, charger_vehicle_ids(hass, entry_id))

    def _vehicle_update(vehicle_id: str, ask: bool) -> None:
        # A target waits for the car's new level: hear of the next reading, and ask the car's integration
        # to read it again once per charge (`refresh_vehicle`'s own call and interval, never a wake-up).
        data.soc_reader.notify_next_reading()
        if ask:
            entry.async_create_background_task(
                hass, async_ask_vehicle_update(hass, vehicle_id), f"{entry_id} vehicle refresh"
            )

    def _consumption(vehicle_id: str) -> float | None:
        return consumption_kwh_per_10km(
            hass, resolve_target_vehicle(hass, vehicle_id or None, charger_vehicle_ids(hass, entry_id))[0]
        )

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
        vehicle_update=_vehicle_update,
    )
    entry.async_on_unload(preview.async_shutdown)
    await preview.async_start()

    def _soc_moved() -> None:
        # New session or real progress: plan again from the current state of charge.
        if settings_store.settings(entry_id).driver == DRIVER_TARGET_SOC:
            entry.async_create_task(hass, preview.async_recalculate(), f"{entry_id} recalculate")

    data.soc_reader.set_on_reading(_soc_moved)

    def _floor_inputs() -> FloorInputs | None:
        # The car the charger plans for, read as the planner reads it, with no side effect on the planner's wait.
        settings = settings_store.settings(entry_id)
        vehicle_id, _ = resolve_target_vehicle(hass, settings.target.vehicle_id, charger_vehicle_ids(hass, entry_id))
        if vehicle_id is None:
            return None
        floor = stored_properties(hass, vehicle_id).min_percent
        if floor is None:
            return FloorInputs(min_percent=None, target_percent=None, vehicle_max_percent=None, soc_percent=None)
        data.soc_reader.ensure_watch(vehicle_id)
        reading = data.soc_reader.read(vehicle_id)
        return FloorInputs(
            min_percent=floor,
            target_percent=settings.target.target_percent if settings.driver == DRIVER_TARGET_SOC else None,
            vehicle_max_percent=data.soc_reader.vehicle_max_percent(vehicle_id),
            soc_percent=None if reading is None else reading.soc_percent,
            soc_estimated=bool(reading is not None and reading.estimated),
            soc_age_s=None if reading is None else reading.age_s,
        )

    # The car's minimum charge level: decided again at every change of the charger, every calculation (a new
    # reading plans again) and every half minute.
    floor = data.min_soc = MinSocFloor(hass, entry_id, data.executor, settings_store, _floor_inputs)
    floor.async_start()
    entry.async_on_unload(floor.async_stop)
    entry.async_on_unload(preview.add_listener(floor.async_poke))

    def _on_connection(event: str) -> bool:
        # A plug-in or an unplug: plan again (the need counted afresh for a new plug-in), and let Auto
        # start a window open now once it has, for a plan it owns.
        handled = preview.note_connection(event)
        plan = data.controller.plan
        return handled and (plan is None or plan.auto_owned)

    data.controller.set_connection_handler(_on_connection)
    entry.async_on_unload(lambda: data.controller.set_connection_handler(None))
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
    # A member charger whose platform names its own measured current (Easee) gets that source without
    # redoing the site; what a person set stays.
    filled = backfill_profile_measured_sources(hass, entry)
    if filled is not None:
        hass.config_entries.async_update_entry(entry, data=filled)
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
    await ChargerPush.async_remove_stored(hass, entry.entry_id)
    await VehicleIdentifier.async_remove_stored(hass, entry.entry_id)
    await ReferenceStore.async_remove_stored(hass, entry.entry_id)
    await ChargerNotifier.async_remove_stored(hass, entry.entry_id)
    await OwnershipCoverage.async_remove_stored(hass, entry.entry_id)
    session_store = domain_data(hass).session_store
    if session_store is not None:
        await session_store.async_remove_charger(entry.entry_id)
