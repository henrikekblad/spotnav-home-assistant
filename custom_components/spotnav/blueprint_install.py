"""SpotNav's automation blueprints, kept installed in Home Assistant.

The blueprints ship inside the integration (`blueprints/automation/*.yaml`, so the release zip carries them) and are
copied to `<config>/blueprints/automation/spotnav/` once per Home Assistant start, off the event loop. A copy is
replaced only while it is still the one SpotNav wrote: the SHA-256 of what was written is kept per file (`Store`
`spotnav.blueprints`), and a copy that matches it, or, with nothing kept yet (an install from before it was kept),
matches a version SpotNav shipped before (`SHIPPED_BEFORE`), is untouched and gets the shipped content. A missing copy
is written; a copy equal to the shipped one is only adopted. Any other copy was edited by the user and is kept as it
is, and no longer updated: said once per start in the log, nothing to act on in the card (whose blueprint list may
still offer the import). Each file says so in its first lines. Nothing outside that folder is written, no other file
in it is touched, and the automations made from the blueprints are never edited: when a file was written while
automations were loaded, the automation blueprints' cache is cleared and, if an automation uses a written blueprint,
the automations are reloaded (what Home Assistant does itself when a blueprint is saved), so they run the new content.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

#: The blueprints, by file name, in the order the card lists them.
BLUEPRINT_FILES: Final = ("price_setpoint.yaml", "solar_surplus_switch.yaml")
#: Where they ship, inside the integration.
SOURCE_FOLDER: Final = Path(__file__).parent / "blueprints" / "automation"
#: The folder under Home Assistant's `blueprints/automation/` that SpotNav owns.
INSTALL_FOLDER: Final = "spotnav"
#: Where the SHA-256 of what SpotNav last wrote, per file, is kept.
STORE_KEY: Final = "spotnav.blueprints"
STORE_VERSION: Final = 1
#: The SHA-256 of every content SpotNav installed before the written hash was kept (the blueprints as shipped from
#: 2.1.0 to 2.3.0, commit 19ed1871): such a copy, with no hash kept, was written by SpotNav and is still updated.
SHIPPED_BEFORE: Final[Mapping[str, tuple[str, ...]]] = {
    "price_setpoint.yaml": ("4a938bdf89f3b87f8ad709d304cc3f24cb57a1e969db6b759b82f73801d90b41",),
    "solar_surplus_switch.yaml": ("9acc30d24075c922da9d4ec05e56f14f08df58a8de667b3f34d787744d04a091",),
}


def blueprint_path(name: str) -> str:
    """The blueprint's path as an automation refers to it (`use_blueprint.path`)."""
    return f"{INSTALL_FOLDER}/{name}"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class Installed:
    """What one install did: the names `written`, the edited copies `kept`, and the SHA-256 of what SpotNav wrote
    (or adopted), by name, to keep for the next start."""

    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)


def install_blueprints(config_dir: str, stored: Mapping[str, str] | None = None) -> Installed:
    """Copy each shipped blueprint whose installed copy is missing or still SpotNav's own (module docstring), given
    the hashes kept from the last start, `stored`. Blocking I/O; touches nothing but the files."""
    target = Path(config_dir) / "blueprints" / "automation" / INSTALL_FOLDER
    hashes = dict(stored or {})
    done = Installed(hashes=hashes)
    for name in BLUEPRINT_FILES:
        content = (SOURCE_FOLDER / name).read_bytes()
        shipped = _sha256(content)
        path = target / name
        try:
            installed: str | None = _sha256(path.read_bytes())
        except OSError:
            installed = None
        if installed == shipped:
            hashes[name] = shipped
            continue
        if installed is not None:
            ours = hashes.get(name)
            if not (installed == ours or (ours is None and installed in SHIPPED_BEFORE.get(name, ()))):
                done.kept.append(name)
                continue
        target.mkdir(parents=True, exist_ok=True)
        # Written beside it and moved into place, so Home Assistant never reads half a file. The temporary name does
        # not end in .yaml, so the blueprint list never shows it.
        temporary = target / f".{name}.spotnav-new"
        temporary.write_bytes(content)
        os.replace(temporary, path)
        hashes[name] = shipped
        done.written.append(name)
    return done


async def _async_stored_hashes(store: Store[dict[str, Any]]) -> dict[str, str]:
    """The hashes kept from the last start; none on a missing or unreadable file (every copy is then judged as
    from before they were kept)."""
    try:
        data = await store.async_load()
    except Exception as err:  # noqa: BLE001 - a store that cannot be read only loses the kept hashes
        _LOGGER.debug("Could not read SpotNav's blueprint hashes: %s", err)
        return {}
    written = data.get("written") if isinstance(data, dict) else None
    if not isinstance(written, dict):
        return {}
    return {name: value for name, value in written.items() if isinstance(name, str) and isinstance(value, str)}


async def async_install_blueprints(hass: HomeAssistant) -> list[str]:
    """Install the blueprints (module docstring) and let loaded automations pick up a change; the names written."""
    store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
    stored = await _async_stored_hashes(store)
    try:
        done = await hass.async_add_executor_job(install_blueprints, hass.config.config_dir, stored)
    except OSError as err:
        # Nothing a user can act on in the card: the blueprints are an extra, the log says why.
        _LOGGER.warning("Could not install SpotNav's blueprints: %s", err)
        return []
    if done.kept:
        _LOGGER.info(
            "SpotNav blueprints edited in blueprints/automation/%s/ are kept as they are; this version's are not "
            "installed over them: %s",
            INSTALL_FOLDER,
            ", ".join(done.kept),
        )
    if done.hashes != stored:
        try:
            await store.async_save({"written": done.hashes})
        except Exception as err:  # noqa: BLE001 - only the next start's judgement is lost, never the setup
            _LOGGER.warning("Could not save SpotNav's blueprint hashes: %s", err)
    written = done.written
    if not written:
        return []
    _LOGGER.info("Installed SpotNav blueprints: %s", ", ".join(written))
    if "automation" not in hass.config.components:
        # Automations not loaded yet read the files as they load.
        return written
    from homeassistant.components.automation import automations_with_blueprint  # noqa: PLC0415
    from homeassistant.components.automation.helpers import async_get_blueprints  # noqa: PLC0415

    await async_get_blueprints(hass).async_reset_cache()
    if any(automations_with_blueprint(hass, blueprint_path(name)) for name in written):
        await hass.services.async_call("automation", "reload", blocking=True)
    return written


def automations_using(hass: HomeAssistant) -> dict[str, int]:
    """How many automations use each blueprint, by file name (0 for each while automations are not loaded)."""
    if "automation" not in hass.config.components:
        return dict.fromkeys(BLUEPRINT_FILES, 0)
    from homeassistant.components.automation import automations_with_blueprint  # noqa: PLC0415

    return {name: len(automations_with_blueprint(hass, blueprint_path(name))) for name in BLUEPRINT_FILES}
