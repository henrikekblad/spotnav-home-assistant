"""SpotNav's automation blueprints, kept installed in Home Assistant.

The blueprints ship inside the integration (`blueprints/automation/*.yaml`, so the release zip carries them) and are
copied to `<config>/blueprints/automation/spotnav/` once per Home Assistant start, off the event loop. A file is
written only when its content differs, so a SpotNav update replaces the installed copies and an unchanged start writes
nothing. Each file says in its first lines that SpotNav overwrites it and how to keep a changed copy. Nothing outside
that folder is written, no other file in it is touched, and the automations made from the blueprints are never
edited: when a file changed while automations were loaded, the automation blueprints' cache is cleared and, if an
automation uses a changed blueprint, the automations are reloaded (what Home Assistant does itself when a blueprint is
saved), so they run the new content.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

#: The blueprints, by file name, in the order the card lists them.
BLUEPRINT_FILES: Final = ("price_setpoint.yaml", "solar_surplus_switch.yaml")
#: Where they ship, inside the integration.
SOURCE_FOLDER: Final = Path(__file__).parent / "blueprints" / "automation"
#: The folder under Home Assistant's `blueprints/automation/` that SpotNav owns.
INSTALL_FOLDER: Final = "spotnav"


def blueprint_path(name: str) -> str:
    """The blueprint's path as an automation refers to it (`use_blueprint.path`)."""
    return f"{INSTALL_FOLDER}/{name}"


def install_blueprints(config_dir: str) -> list[str]:
    """Copy each shipped blueprint whose installed copy differs (or is missing); the names written. Blocking I/O."""
    target = Path(config_dir) / "blueprints" / "automation" / INSTALL_FOLDER
    written: list[str] = []
    for name in BLUEPRINT_FILES:
        content = (SOURCE_FOLDER / name).read_bytes()
        path = target / name
        try:
            if path.read_bytes() == content:
                continue
        except OSError:
            pass
        target.mkdir(parents=True, exist_ok=True)
        # Written beside it and moved into place, so Home Assistant never reads half a file. The temporary name does
        # not end in .yaml, so the blueprint list never shows it.
        temporary = target / f".{name}.spotnav-new"
        temporary.write_bytes(content)
        os.replace(temporary, path)
        written.append(name)
    return written


async def async_install_blueprints(hass: HomeAssistant) -> list[str]:
    """Install the blueprints (module docstring) and let loaded automations pick up a change; the names written."""
    try:
        written = await hass.async_add_executor_job(install_blueprints, hass.config.config_dir)
    except OSError as err:
        # Nothing a user can act on in the card: the blueprints are an extra, the log says why.
        _LOGGER.warning("Could not install SpotNav's blueprints: %s", err)
        return []
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
