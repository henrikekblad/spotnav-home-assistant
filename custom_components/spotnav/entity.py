"""Base entities for SpotNav charging control."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .execution.auto_execution import AutoExecutor
from .execution.controller import ChargingController
from .planning.auto_controller import AutoPlannerController, AutoSnapshot, SettingsReconcileError
from .planning.auto_settings import (
    AreaAutoSettings,
    AutoSettings,
    AutoSettingsError,
    AutoSettingsStore,
    FiscalOverride,
)
from .pricing.price_refresh import PriceRefreshManager
from .pricing.price_repository import CatalogueSnapshot
from .pricing.relay_contract import AreaEntry
from .runtime import domain_data, executor_for, preview_for
from .site.site_capacity_controller import SiteCapacityController


class SpotNavChargingEntity(Entity):
    """Entity backed by the SpotNav charging controller."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, controller: ChargingController) -> None:
        self._entry = entry
        self.controller = controller
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Sensnology",
            model="SpotNav charging control",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.controller.add_listener(self.async_write_ha_state))


#: The three states a fiscal component can be in: `off` is `enabled=False`, `suggested` is
#: `enabled=True` with no value, `manual` is `enabled=True` with one.
FISCAL_OFF: Final = "off"
FISCAL_SUGGESTED: Final = "suggested"
FISCAL_MANUAL: Final = "manual"
FISCAL_OPTIONS: Final = (FISCAL_OFF, FISCAL_SUGGESTED, FISCAL_MANUAL)

#: The three components, in entity creation order: value added tax, energy tax, grid transfer fee.
FISCAL_COMPONENTS: Final = ("vat", "tax", "transfer")


def fiscal_policy(component: FiscalOverride) -> str:
    """Which of the three states this component is in, as an entity option id."""
    if not component.enabled:
        return FISCAL_OFF
    return FISCAL_SUGGESTED if component.value is None else FISCAL_MANUAL


def with_fiscal_policy(component: FiscalOverride, policy: str) -> FiscalOverride:
    """This component in that policy, or a refusal, never an invented figure.

    `manual` with nothing stored is refused rather than read as zero or copied from the suggestion.
    """
    if policy == FISCAL_OFF:
        return FiscalOverride(enabled=False, value=component.value)
    if policy == FISCAL_SUGGESTED:
        return FiscalOverride(enabled=True, value=None)
    if policy == FISCAL_MANUAL:
        if component.value is None:
            raise AutoSettingsError(
                "invalid_fiscal",
                "a manual fiscal policy needs an explicit value; set the value first",
            )
        return FiscalOverride(enabled=True, value=component.value)
    raise AutoSettingsError("invalid_fiscal", f"{policy!r} is not a fiscal policy")


def fiscal_view(
    component: FiscalOverride, suggestion: float | None
) -> tuple[str, float | None, str]:
    """One component's policy, the figure actually applied, and where that figure comes from.

    The single implementation of this reading, shared by the entities and the dashboard contract.
    `suggestion` is the market's catalogue value, or `None` when it publishes none (not a zero).
    """
    policy = fiscal_policy(component)
    if policy == FISCAL_MANUAL:
        return (policy, component.value, "manual")
    if policy == FISCAL_SUGGESTED:
        if suggestion is None:
            return (policy, None, "suggestion_unavailable")
        return (policy, suggestion, "suggested")
    return (policy, None, "off")


class AutoSurface:
    """Whatever of the Auto backend one charger currently has.

    Resolved once per platform setup: settings store and price manager are installation-global,
    preview and authority boundary are per entry. An absent backend makes an entity unavailable
    with a stable reason rather than raising.
    """

    __slots__ = ("store", "manager", "preview", "executor")

    def __init__(
        self,
        store: AutoSettingsStore | None,
        manager: PriceRefreshManager | None,
        preview: AutoPlannerController | None,
        executor: AutoExecutor | None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.preview = preview
        self.executor = executor

    @classmethod
    def resolve(cls, hass: HomeAssistant, entry_id: str) -> AutoSurface:
        """Find this charger's backend, without creating, subscribing to or fetching anything."""
        return cls(
            store=domain_data(hass).auto_store,
            manager=domain_data(hass).price_refresh,
            preview=preview_for(hass, entry_id),
            executor=executor_for(hass, entry_id),
        )

    @property
    def available(self) -> bool:
        """Whether the settings and the preview are both there."""
        return self.store is not None and self.preview is not None

    @property
    def unavailable_reason(self) -> str | None:
        """Why this backend is unusable, as a stable code, or `None` when it is usable."""
        if self.store is None:
            return "auto_store_absent"
        if self.preview is None:
            return "auto_preview_absent"
        return None

    def settings(self, entry_id: str) -> AutoSettings | None:
        """This charger's stored settings, or `None` when there is no store to read."""
        return None if self.store is None else self.store.settings(entry_id)

    def catalogue(self) -> CatalogueSnapshot | None:
        """The relay's area list as it stands, or `None` when there is no price layer."""
        return None if self.manager is None else self.manager.catalogue_snapshot()

    def catalogue_entry(self, area_id: str | None) -> AreaEntry | None:
        """One area's catalogue entry, or `None` when it cannot be read."""
        if area_id is None:
            return None
        catalogue = self.catalogue()
        return None if catalogue is None else catalogue.area(area_id)


class SpotNavSiteEntity(Entity):
    """Entity backed by the SpotNav site capacity controller."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, controller: SiteCapacityController) -> None:
        self._entry = entry
        self.controller = controller
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Sensnology",
            model="SpotNav site",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.controller.add_listener(self.async_write_ha_state))


class SpotNavAutoEntity(Entity):
    """Base for the Auto entity surface: one charger's settings, preview and boundary.

    An Auto entity owns no timer, requests no price and keeps no copy of the settings; it listens
    to the preview and writes only through the reviewed controller methods.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        controller: ChargingController,
        auto: AutoSurface,
        *,
        key: str,
    ) -> None:
        self._entry = entry
        self.controller = controller
        self._auto = auto
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Sensnology",
            model="SpotNav charging control",
        )
        #: The settings revision this entity last showed; a write carries it as
        #: `expected_revision`, so an edit made elsewhere is refused, not overwritten.
        self._displayed_revision: int | None = None

    @property
    def available(self) -> bool:
        """Whether this charger's Auto backend is there at all."""
        return self._auto.available

    @property
    def unavailable_reason(self) -> str | None:
        """The stable code explaining an unavailable entity, or `None` when it is available."""
        return self._auto.unavailable_reason

    @property
    def settings(self) -> AutoSettings | None:
        """The stored settings, read fresh; `None` only when there is no store to read."""
        return self._auto.settings(self._entry.entry_id)

    @property
    def snapshot(self) -> AutoSnapshot | None:
        """The preview's current immutable snapshot, or `None` without a preview."""
        return None if self._auto.preview is None else self._auto.preview.snapshot()

    @property
    def area(self) -> AreaEntry | None:
        """The selected area's catalogue entry, or `None` when there is none to read."""
        settings = self.settings
        return self._auto.catalogue_entry(None if settings is None else settings.area_id)

    @property
    def override(self) -> AreaAutoSettings | None:
        """The selected area's fiscal overrides, or `None` when no area is selected."""
        settings = self.settings
        if settings is None or settings.area_id is None:
            return None
        return settings.override_for(settings.area_id)

    async def async_added_to_hass(self) -> None:
        """One listener, on the preview, for as long as HA keeps this entity."""
        preview = self._auto.preview
        if preview is not None:
            self.async_on_remove(preview.add_listener(self._on_auto_snapshot))
            self._displayed_revision = preview.snapshot().settings_revision

    @callback
    def _on_auto_snapshot(self, snapshot: AutoSnapshot) -> None:
        """The preview published a meaningful change: re-render, on the event loop."""
        self._displayed_revision = snapshot.settings_revision
        self.async_write_ha_state()

    async def async_write_settings(
        self, mutate: Callable[[AutoSettings], AutoSettings]
    ) -> None:
        """Write one settings change, compare-and-set against what this entity displayed.

        A refused write makes the entity re-render from what is actually stored; nothing is optimistic.
        """
        preview = self._auto.preview
        if preview is None:
            raise HomeAssistantError(
                f"Auto settings are not available ({self.unavailable_reason})"
            )
        expected = self._displayed_revision
        if expected is None:
            settings = self.settings
            expected = None if settings is None else settings.revision
        try:
            await preview.async_apply_settings(mutate=mutate, expected_revision=expected)
        except SettingsReconcileError:
            # Settings were committed but the reconcile failed: re-render from the store and
            # re-raise the stable error unchanged ("saved, but applying failed").
            self.async_write_ha_state()
            raise
        except AutoSettingsError as err:
            # Refused by the backend: re-render so the stored truth shows beside the message.
            self.async_write_ha_state()
            raise ServiceValidationError(
                str(err),
                translation_domain=DOMAIN,
                translation_key=err.code,
            ) from err
