"""What a loaded SpotNav installation holds, typed, and how the rest of the code reaches it.

* A charger entry owns a `ChargerData` on `entry.runtime_data` (controller, authority boundary,
  Auto preview, market display observation, solar coordinator, state-of-charge reader ...).
* A site entry owns a `SiteData` (its capacity controller).
* The installation owns one `SpotNavData` under `DATA_KEY`: everything that belongs to no entry
  (settings store, price repository and manager, decision store, pairing register ...). Created
  by `async_setup`, before any entry, and outlives every entry.

Everything an entry starts is stopped with `entry.async_on_unload` where it is started.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE


if TYPE_CHECKING:
    from .log_buffer import SpotNavLogBuffer
    from .planning.auto_controller import AutoPlannerController
    from .execution.auto_execution import AutoExecutor
    from .planning.auto_settings import AutoSettingsStore
    from .execution.controller import ChargingController
    from .vehicles.discovery_decisions import DiscoveryDecisionStore
    from .execution.hybrid_execution import HybridChargerState, ReplanMemory
    from .pricing.market_observation import MarketObservation
    from .api.pairing import PairingRegister
    from .pricing.price_refresh import PriceRefreshManager
    from .pricing.price_repository import PriceRepository
    from .site.site_capacity_controller import SiteCapacityController
    from .vehicles.soc_estimate import SocReader
    from .execution.solar_execution import SolarExecutionCoordinator
    from .vehicles.vehicle_charge_limit import VehicleChargeLimitLimiter
    from .vehicles.vehicle_refresh import VehicleRefreshLimiter
    from .sessions.history_import import HistoryImporter
    from .sessions.recorder import SessionRecorder
    from .sessions.store import SessionStore
    from .notifications.notifier import ChargerNotifier
    from .notifications.push import ChargerPush
    from .vehicles.identification import VehicleIdentifier
    from .vehicles.camera_identification import CameraIdentification


@dataclass
class ChargerData:
    """One charger entry's live objects. Filled as setup progresses; `None` until then."""

    controller: ChargingController
    soc_reader: SocReader
    executor: AutoExecutor | None = None
    preview: AutoPlannerController | None = None
    observation: MarketObservation | None = None
    solar: SolarExecutionCoordinator | None = None
    hybrid_state: HybridChargerState | None = None
    hybrid_memory: ReplanMemory | None = None
    sessions: SessionRecorder | None = None
    history_import: HistoryImporter | None = None
    notifier: ChargerNotifier | None = None
    #: Which car is plugged in, at a charger more than one vehicle can charge at.
    identifier: VehicleIdentifier | None = None
    #: The charger's camera for identification: its reference pictures and the AI Task query.
    camera: CameraIdentification | None = None
    #: The paired app's instant-notification registration (`notifications/push.py`).
    push: ChargerPush | None = None


@dataclass
class SiteData:
    """One site entry's live objects."""

    controller: SiteCapacityController


@dataclass
class SpotNavData:
    """The installation-wide singletons: nothing here belongs to one config entry."""

    auto_store: AutoSettingsStore | None = None
    price_repository: PriceRepository | None = None
    price_refresh: PriceRefreshManager | None = None
    decision_store: DiscoveryDecisionStore | None = None
    pairing: PairingRegister | None = None
    refresh_limiter: VehicleRefreshLimiter | None = None
    charge_limit_limiter: VehicleChargeLimitLimiter | None = None
    forecast_platforms: dict[str, Callable] | None = None
    session_store: SessionStore | None = None
    resync_cancel: Callable[[], None] | None = None
    card_served: bool = False
    #: The bundle hash in the card URL Home Assistant hands the browsers (`card_asset.py`), `None` until served.
    card_served_digest: str | None = None
    log_buffer: SpotNavLogBuffer | None = None
    #: When the integration loaded; the start-up grace (`startup.py`) counts from here.
    started_at: datetime = field(default_factory=dt_util.utcnow)


DATA_KEY: HassKey[SpotNavData] = HassKey(DOMAIN)

type ChargerConfigEntry = ConfigEntry[ChargerData]
type SiteConfigEntry = ConfigEntry[SiteData]


def domain_data(hass: HomeAssistant) -> SpotNavData:
    """The installation's singletons. Created by `async_setup`; created here if a caller runs first."""
    data = hass.data.get(DATA_KEY)
    if data is None:
        data = hass.data[DATA_KEY] = SpotNavData()
    return data


def _entry_data(hass: HomeAssistant, entry_id: str, *, site: bool) -> ChargerData | SiteData | None:
    """The runtime data of a loaded entry of the wanted kind, or `None`."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    if (entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE) is not site:
        return None
    return getattr(entry, "runtime_data", None)


def charger_data(hass: HomeAssistant, entry_id: str) -> ChargerData | None:
    """A loaded charger entry's runtime data, or `None` (unknown id, a site entry, not set up yet)."""
    return _entry_data(hass, entry_id, site=False)  # type: ignore[return-value]


def site_data(hass: HomeAssistant, entry_id: str) -> SiteData | None:
    """A loaded site entry's runtime data, or `None`."""
    return _entry_data(hass, entry_id, site=True)  # type: ignore[return-value]


def controller_for(hass: HomeAssistant, entry_id: str) -> ChargingController | None:
    """A charger entry's controller, if that entry has one."""
    data = charger_data(hass, entry_id)
    return None if data is None else data.controller


def site_controller_for(hass: HomeAssistant, entry_id: str) -> SiteCapacityController | None:
    """A site entry's controller, if that entry has one."""
    data = site_data(hass, entry_id)
    return None if data is None else data.controller


def preview_for(hass: HomeAssistant, entry_id: str) -> AutoPlannerController | None:
    """A charger entry's Auto preview controller, if it has one."""
    data = charger_data(hass, entry_id)
    return None if data is None else data.preview


def executor_for(hass: HomeAssistant, entry_id: str) -> AutoExecutor | None:
    """A charger entry's authority boundary, if it has one."""
    data = charger_data(hass, entry_id)
    return None if data is None else data.executor
