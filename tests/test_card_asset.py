"""The bundled card's static route: one URL, registered once, served from the package.

These tests are about the *delivery* half of the card: that Home Assistant really serves the
compiled file over its own supported static-path mechanism, at the integration's own URL, with
the version query the documentation names, and that nothing about Lovelace storage is touched on
the way. The card's own behaviour is tested in `frontend/` (TypeScript, against a faked
`hass.callWS`); the WebSocket contract it reads is tested in `tests/test_dashboard_api.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from homeassistant.setup import async_setup_component

from custom_components import spotnav as integration
from custom_components.spotnav import card_asset

from .world import setup_charger
from custom_components.spotnav.runtime import domain_data

pytestmark = pytest.mark.usefixtures("offline_relay")

#: The integration package's own directory: the route must serve a file inside it, so that what
#: HACS installs is what the route answers with.
PACKAGE = Path(integration.__file__).resolve().parent
MANIFEST = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
README = Path(integration.__file__).resolve().parents[2] / "README.md"


class Registrations:
    """Every static path this domain has asked Home Assistant to serve, in order."""

    def __init__(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
        assert hass.http is not None, "the HTTP component must be set up for this test"
        self.configs: list[Any] = []
        original = hass.http.async_register_static_paths

        async def spy(configs: list[Any]) -> None:
            self.configs.extend(configs)
            await original(configs)

        monkeypatch.setattr(hass.http, "async_register_static_paths", spy)

    @property
    def paths(self) -> list[str]:
        return [config.url_path for config in self.configs]


def test_the_route_is_the_integration_s_own_path() -> None:
    """An integration-owned path, not `/local`, and not a directory prefix."""
    assert card_asset.CARD_URL_PATH == "/spotnav/spotnav-card.js"
    assert card_asset.CARD_ELEMENT == "spotnav-card"
    assert card_asset.CARD_ASSET_PATH == PACKAGE / "www" / "spotnav-card.js"
    # The compiled asset is committed inside the package, which is what HACS installs.
    assert card_asset.CARD_ASSET_PATH.is_file(), card_asset.CARD_ASSET_PATH
    assert card_asset.CARD_URL_PATH + "?v=1" == card_asset.card_asset_url("1")


def test_the_documented_url_derives_from_the_manifest_version() -> None:
    """One rule for the URL and one source for the version; the README asks for no resource."""
    url = card_asset.card_asset_url(MANIFEST["version"])
    assert url == f"/spotnav/spotnav-card.js?v={MANIFEST['version']}"
    text = README.read_text(encoding="utf-8")
    # The integration loads the card itself (`async_load_card_in_frontend`), so the README must not
    # tell anyone to paste a versioned resource URL that the next release would outdate.
    assert "/spotnav/spotnav-card.js?v=" not in text
    assert "no dashboard resource to add" in text


def test_the_url_carries_the_bundle_hash_and_falls_back_to_the_version(tmp_path: Path) -> None:
    import hashlib

    bundle = tmp_path / "card.js"
    bundle.write_bytes(b"console.log(1)")
    digest = card_asset.read_bundle_digest(bundle)
    assert digest == hashlib.sha256(b"console.log(1)").hexdigest()[:8]
    assert card_asset.card_asset_url("1.2.3", digest) == f"/spotnav/spotnav-card.js?v=1.2.3-{digest}"
    bundle.write_bytes(b"console.log(2)")
    assert card_asset.read_bundle_digest(bundle) != digest, "a rebuilt bundle is a new URL"
    assert card_asset.read_bundle_digest(tmp_path / "missing.js") is None
    assert card_asset.card_asset_url("1.2.3", None) == "/spotnav/spotnav-card.js?v=1.2.3"


async def test_setup_registers_the_hashed_url_computed_off_the_loop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    seen: dict[str, object] = {}
    loop_thread = threading.get_ident()
    real = card_asset.read_bundle_digest

    def spy(*args: object) -> str | None:
        seen["off_loop"] = threading.get_ident() != loop_thread
        return real(*args)  # type: ignore[arg-type]

    urls: list[str] = []
    monkeypatch.setattr(card_asset, "read_bundle_digest", spy)
    monkeypatch.setattr(card_asset, "async_load_card_in_frontend", lambda _h, url: urls.append(url))
    monkeypatch.setattr(card_asset, "domain_data", lambda _h: type("D", (), {"card_served": False})())
    monkeypatch.setattr(hass, "http", type("H", (), {"async_register_static_paths": staticmethod(lambda *_: _noop())})(), raising=False)
    await card_asset.async_setup_card_asset(hass)
    assert seen["off_loop"] is True
    assert urls == [card_asset.card_asset_url(MANIFEST["version"], real())]


async def _noop() -> None:
    return None


async def test_the_manifest_version_is_read_from_the_loader(hass: HomeAssistant) -> None:
    """What Home Assistant itself loaded, not a second hand-kept constant."""
    assert await card_asset.async_manifest_version(hass) == MANIFEST["version"]


async def test_one_route_for_two_entries_a_reload_and_a_second_setup(
    hass: HomeAssistant, hass_client_no_auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registered once for the domain, never per entry and never twice."""
    assert await async_setup_component(hass, "http", {})
    registrations = Registrations(hass, monkeypatch)
    first = await setup_charger(hass, entry_id="entry_a", webhook_id="webhook-a", charge_control="switch.entry_a")
    await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.entry_b")

    assert await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()
    # A second setup call is what a reloaded domain or a second setup path would do.
    await card_asset.async_setup_card_asset(hass)

    assert registrations.paths == [card_asset.CARD_URL_PATH]

    config = registrations.configs[0]
    served = Path(config.path).resolve()
    assert served.is_relative_to(PACKAGE), served
    assert served == card_asset.CARD_ASSET_PATH
    assert config.cache_headers is True, "a versioned URL may be cached hard"

    # The test client starts the HTTP server, which freezes the router: registration has to be
    # finished by the time it exists.
    client = await hass_client_no_auth()
    response = await client.get(card_asset.card_asset_url(MANIFEST["version"]))
    assert response.status == 200
    assert "javascript" in response.headers["Content-Type"]
    assert "max-age" in response.headers.get("Cache-Control", ""), "cached on purpose"
    body = await response.text()
    assert card_asset.CARD_ELEMENT in body, "the compiled asset defines the element"


async def test_the_route_survives_another_entry_unloading(
    hass: HomeAssistant, hass_client_no_auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry going away must not take the shared asset away from the others."""
    assert await async_setup_component(hass, "http", {})
    registrations = Registrations(hass, monkeypatch)
    first = await setup_charger(hass, entry_id="entry_a", webhook_id="webhook-a", charge_control="switch.entry_a")
    second = await setup_charger(hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.entry_b")
    assert first.entry_id != second.entry_id

    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()

    assert registrations.paths == [card_asset.CARD_URL_PATH], "still registered once"
    # The test client starts the HTTP server, which freezes the router: registration has to be
    # finished by the time it exists.
    client = await hass_client_no_auth()
    response = await client.get(card_asset.CARD_URL_PATH)
    assert response.status == 200, "and still served after another entry unloaded"
    assert await hass.config_entries.async_reload(second.entry_id)
    await hass.async_block_till_done()
    assert registrations.paths == [card_asset.CARD_URL_PATH], "and a reload adds nothing"


async def test_a_missing_asset_fails_loudly_and_registers_nothing(
    hass: HomeAssistant, hass_client_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken package must not become a registered URL that answers 404."""
    assert await async_setup_component(hass, "http", {})
    registrations = Registrations(hass, monkeypatch)
    missing = tmp_path / "spotnav-card.js"
    monkeypatch.setattr(card_asset, "CARD_ASSET_PATH", missing)
    domain_data(hass).card_served = False

    with pytest.raises(card_asset.CardAssetMissing) as failure:
        await card_asset.async_setup_card_asset(hass)

    assert str(missing) in str(failure.value)
    assert "npm run build" in str(failure.value), "and says how to fix it"
    assert registrations.paths == [], "no half-registered route"
    assert domain_data(hass).card_served is False, "not marked as done"


async def test_a_headless_host_is_a_logged_skip_not_a_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """No HTTP component: say so, register nothing, and do not fail the whole integration."""
    assert hass.http is None, "this test deliberately runs without the HTTP component"
    domain_data(hass).card_served = False

    with caplog.at_level("WARNING"):
        await card_asset.async_setup_card_asset(hass)

    assert card_asset.CARD_URL_PATH in caplog.text
    assert domain_data(hass).card_served is False, "nothing was served"


async def test_nothing_writes_lovelace_or_dashboard_storage(
    hass: HomeAssistant, hass_client_no_auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one manual resource step is the user's; this integration writes no Lovelace state."""
    assert await async_setup_component(hass, "http", {})
    keys: list[str] = []
    original_save = storage.Store.async_save
    original_delay = storage.Store.async_delay_save

    async def save(store: Any, data: Any) -> None:
        keys.append(store.key)
        await original_save(store, data)

    def delay_save(store: Any, data_func: Any, delay: float) -> None:
        keys.append(store.key)
        original_delay(store, data_func, delay)

    monkeypatch.setattr(storage.Store, "async_save", save)
    monkeypatch.setattr(storage.Store, "async_delay_save", delay_save)

    await setup_charger(hass, entry_id="entry_a", webhook_id="webhook-a", charge_control="switch.entry_a")
    # The test client starts the HTTP server, which freezes the router: registration has to be
    # finished by the time it exists.
    client = await hass_client_no_auth()
    response = await client.get(card_asset.CARD_URL_PATH)
    assert response.status == 200

    lovelace = [key for key in keys if "lovelace" in key or "dashboard" in key]
    assert lovelace == [], lovelace
    assert keys, "the integration does write its own stores, so the spy is real"


def test_the_frontend_is_asked_to_load_the_versioned_card(hass: HomeAssistant) -> None:
    """No dashboard resource is needed: the frontend loads the card, at the manifest-versioned URL."""
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    from custom_components.spotnav.card_asset import (
        async_load_card_in_frontend,
        card_asset_url,
    )

    url = card_asset_url("1.2.3")

    async_load_card_in_frontend(hass, url)
    assert url in hass.data[DATA_EXTRA_MODULE_URL].urls
    assert url.endswith("?v=1.2.3"), "an upgrade is a new URL, so no browser keeps the old bundle"


async def test_the_asset_is_looked_up_off_the_event_loop(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_file` is a blocking filesystem call: it must run in the executor, not on the loop."""
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class Probe:
        def is_file(self) -> bool:
            seen.append(threading.get_ident())
            return False

        def __str__(self) -> str:
            return "probe"

    monkeypatch.setattr(card_asset, "CARD_ASSET_PATH", Probe())
    domain_data(hass).card_served = False

    with pytest.raises(card_asset.CardAssetMissing):
        await card_asset.async_setup_card_asset(hass)

    assert seen and seen[0] != loop_thread
