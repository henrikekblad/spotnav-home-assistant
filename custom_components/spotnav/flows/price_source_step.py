"""The first setup's one step about where prices come from.

When the installation's first charger or site is added, its electricity price is about to be made from the area Home
Assistant's location suggests (`pricing/price_setup.py`). When that area has no source of its own yet
(`pricing/providers/notices.py`), the flow shows one step before the entry is created, saying what gives it one:

* `prices_nordpool`: an area Nord Pool trades. A link that opens Home Assistant's own flow for adding the Nord
  Pool integration (a config flow cannot start another integration's flow), or the integration's page when it is
  there but did not start; and, as the alternative, the optional ENTSO-E key with its short how-to;
* `prices_entsoe`: an area only ENTSO-E publishes. The optional key and the how-to;
* `prices_pvpc`: Spain's PVPC. A link that adds Home Assistant's PVPC integration (or opens it);
* `prices_octopus`: a Great Britain region. A link to the Octopus Energy integration's HACS repository (or its page).

Every field is optional: the step is passed without acting, and the card's notice stays until a source is there. A
key that is entered is checked and stored as the card's would be (`pricing/entsoe_key.py`); one the platform
rejects keeps the step open with that error.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from ..const import DOMAIN, ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE
from ..planning.first_run import suggest_area
from ..pricing.entsoe_key import async_save_entsoe_key, ERROR_KEY_REJECTED
from ..pricing.price_setup import any_price_entry
from ..pricing.providers.notices import ProviderNotice
from ..runtime import domain_data, entry_kind
from .price import async_price_catalogue, CONF_ENTSOE_KEY

#: Home Assistant's frontend routes (frontend 20260826, the release Home Assistant 2026.9 ships): the add-integration
#: flow for a domain, and an integration's own page.
ADD_INTEGRATION_PATH: Final = "/config/integrations/dashboard/add?domain={domain}"
INTEGRATION_PATH: Final = "/config/integrations/integration/{domain}"
#: Where an ENTSO-E key is registered for (the how-to's link).
ENTSOE_URL: Final = "https://transparency.entsoe.eu/"
#: The Octopus Energy integration is not part of Home Assistant: it is added from HACS.
OCTOPUS_REPOSITORY_URL: Final = "https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy"

#: Each notice the step is shown for, with its step and the link it offers (`None`: no link).
_STEPS: Final[dict[str, tuple[str, str | None]]] = {
    "add_nordpool": ("prices_nordpool", ADD_INTEGRATION_PATH.format(domain="nordpool")),
    "nordpool_not_loaded": ("prices_nordpool", INTEGRATION_PATH.format(domain="nordpool")),
    "add_entsoe_key": ("prices_entsoe", None),
    "entsoe_key_rejected": ("prices_entsoe", None),
    "add_pvpc": ("prices_pvpc", ADD_INTEGRATION_PATH.format(domain="pvpc_hourly_pricing")),
    "pvpc_not_loaded": ("prices_pvpc", INTEGRATION_PATH.format(domain="pvpc_hourly_pricing")),
    "add_octopus": ("prices_octopus", OCTOPUS_REPOSITORY_URL),
    "octopus_not_loaded": ("prices_octopus", INTEGRATION_PATH.format(domain="octopus_energy")),
    "octopus_no_agile": ("prices_octopus", INTEGRATION_PATH.format(domain="octopus_energy")),
}
#: The steps that offer the ENTSO-E key.
_KEY_STEPS: Final = frozenset({"prices_nordpool", "prices_entsoe"})


def first_setup(hass: HomeAssistant) -> bool:
    """Whether a charger or site added now is the installation's first, with no electricity price yet."""
    if any_price_entry(hass):
        return False
    return not any(
        entry_kind(entry) in (ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE) for entry in hass.config_entries.async_entries(DOMAIN)
    )


async def async_first_setup_notice(hass: HomeAssistant) -> tuple[str, ProviderNotice] | None:
    """`(area id, notice)` when the first charger or site is being added and the area Home Assistant's location
    suggests has no source of its own that this step can help with; else `None`."""
    config = hass.config
    if not config.country or not first_setup(hass):
        return None
    catalogue = await async_price_catalogue(hass)
    area_id = suggest_area(catalogue, config.country, config.latitude, config.longitude)
    repository = domain_data(hass).price_repository
    if area_id is None or repository is None:
        return None
    notice = repository.source_status(area_id).notice
    if notice is None or notice.code not in _STEPS:
        return None
    return area_id, notice


class PriceSourceSteps:
    """The step itself, mixed into the integration's config flow; [_async_create_with_prices] replaces the charger's
    and the site's `async_create_entry`."""

    hass: HomeAssistant
    _prices_pending: tuple[str, dict[str, Any]] | None = None
    _prices_notice: tuple[str, ProviderNotice] | None = None

    async def _async_create_with_prices(self, title: str, data: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry, after the prices step when the first setup's area has no source of its own."""
        found = await async_first_setup_notice(self.hass)
        if found is None:
            return self.async_create_entry(title=title, data=data)  # type: ignore[attr-defined]
        self._prices_pending = (title, data)
        self._prices_notice = found
        return await self._async_prices_step(None)

    async def async_step_prices_nordpool(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_prices_step(user_input)

    async def async_step_prices_entsoe(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_prices_step(user_input)

    async def async_step_prices_pvpc(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_prices_step(user_input)

    async def async_step_prices_octopus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_prices_step(user_input)

    async def _async_prices_step(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        if self._prices_pending is None or self._prices_notice is None:
            return self.async_abort(reason="no_pending_entry")  # type: ignore[attr-defined]
        title, data = self._prices_pending
        area_id, notice = self._prices_notice
        step_id, link = _STEPS[notice.code]
        errors: dict[str, str] = {}
        if user_input is not None:
            value = user_input.get(CONF_ENTSOE_KEY)
            if step_id in _KEY_STEPS and isinstance(value, str) and value.strip():
                saved = await async_save_entsoe_key(self.hass, value, area_id=area_id)
                if saved.error == ERROR_KEY_REJECTED:
                    errors[CONF_ENTSOE_KEY] = ERROR_KEY_REJECTED
            if not errors:
                return self.async_create_entry(title=title, data=data)  # type: ignore[attr-defined]
        schema: dict[Any, Any] = {}
        if step_id in _KEY_STEPS:
            schema[vol.Optional(CONF_ENTSOE_KEY)] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD, autocomplete="off")
            )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id,
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"area": area_id, "link": link or "", "entsoe_url": ENTSOE_URL},
        )
