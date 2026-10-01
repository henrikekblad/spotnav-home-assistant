"""The bundled card's one static route: a compiled, versioned frontend asset.

The compiled file is committed next to the Python that serves it. It is served with
`StaticPathConfig` and `hass.http.async_register_static_paths`, registered once for the whole
domain during domain setup (a per-entry registration would fail as a duplicate route on reload)
and never unregistered. The URL carries the manifest version and a short hash of the bundle as a query
(`card_asset_url`), so an upgrade or a rebuild is a new URL and the file may be cached hard. A missing compiled file raises rather
than registering a route that answers 404.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Final

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .runtime import domain_data


_LOGGER = logging.getLogger(__name__)

#: The stable URL of the card module, owned by this integration (not `/local`).
CARD_URL_PATH: Final = f"/{DOMAIN}/spotnav-card.js"

#: The compiled asset inside the package; the route serves this file only.
CARD_ASSET_PATH: Final = Path(__file__).parent / "www" / "spotnav-card.js"

CARD_ELEMENT: Final = "spotnav-card"


class CardAssetMissing(HomeAssistantError):
    """The compiled card asset is not where the integration package must carry it."""


def card_asset_url(version: str, digest: str | None = None) -> str:
    """The resource URL for one integration version and, when known, one bundle content.

    Pure, so docs, tests and logs agree. The digest makes a rebuilt bundle a new URL even when the
    version did not change (the Home Assistant Android app keeps the old module otherwise).
    """
    token = f"{version}-{digest}" if digest else version
    return f"{CARD_URL_PATH}?v={token}"


def read_bundle_digest(path: Path = CARD_ASSET_PATH) -> str | None:
    """The first 8 hex characters of the bundle's SHA-256, or None if it cannot be read.

    Blocking file I/O: call it from an executor job, never on the event loop.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    except OSError:
        return None


async def async_manifest_version(hass: HomeAssistant) -> str | None:
    """The version Home Assistant loaded for this integration, read from `manifest.json`."""
    integration = await async_get_integration(hass, DOMAIN)
    return integration.version


async def async_setup_card_asset(hass: HomeAssistant) -> None:
    """Register the card's one route for the whole domain, once, during domain setup."""
    data = domain_data(hass)
    if data.card_served:
        return

    if not await hass.async_add_executor_job(CARD_ASSET_PATH.is_file):
        raise CardAssetMissing(
            f"The bundled card asset is missing at {CARD_ASSET_PATH}. Build it with "
            "`npm ci && npm run build` in frontend/ and ship the compiled file inside "
            "custom_components/spotnav/www/, or remove the resource URL from the "
            "dashboard that references it."
        )

    if hass.http is None:
        # No HTTP component (a headless test host) is a skip, not a failure, but is logged.
        _LOGGER.warning(
            "The HTTP component is not set up, so the bundled SpotNav card is not served at %s",
            CARD_URL_PATH,
        )
        return

    await hass.http.async_register_static_paths(
        # A versioned URL may be cached hard; the changed query is the upgrade guarantee.
        [StaticPathConfig(CARD_URL_PATH, str(CARD_ASSET_PATH), cache_headers=True)]
    )
    data.card_served = True
    digest = await hass.async_add_executor_job(read_bundle_digest)
    url = card_asset_url(await async_manifest_version(hass) or "unknown", digest)
    _LOGGER.debug("Serving the bundled SpotNav card at %s", url)
    async_load_card_in_frontend(hass, url)


def async_load_card_in_frontend(hass: HomeAssistant, url: str) -> None:
    """Have the frontend load the card on every page via `frontend.add_extra_js_url`.

    The URL carries the manifest version, so an upgrade is a new URL. A dashboard resource added by
    hand does no harm: the bundle guards its own second evaluation (`frontend/src/index.ts`). The
    frontend is a manifest dependency, so it is set up before this runs.
    """
    add_extra_js_url(hass, url)
