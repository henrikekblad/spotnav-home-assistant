"""Hand a charger back when its SpotNav entry is deleted for good.

Someone who removes SpotNav expects the charger to behave as it did before SpotNav: allowed to charge,
at its full current. SpotNav may have left it stopped (a plan waiting for a cheap hour, a pause) or held
at a lower current (solar, load balancing), and with SpotNav gone nothing would ever change that back.

Only `async_remove_entry` calls this, and Home Assistant calls that only when an entry is deleted: an
unload (a restart, a reload, a disabled entry) never comes here, since the entry comes back and takes the
charger on as it left it.

What is done, once, through the same paths SpotNav drove the charger with (`chargers/adapter.py`):

* the start/stop control is turned back on when it reads off (a switch, a select's start option, a
  current number paused at 0 A), or an Easee paused by SpotNav is resumed; a control that cannot tell
  (button pairs) is left alone, since pressing Start would start a charge rather than allow one;
* when SpotNav set the charger's current (the person's opt-in), it is set to the top of its range
  (`charger_current_range`: the current number's own maximum, the OCPP limits, else 32 A, which the
  charger's own maximum still caps). One write, so a charger that keeps the setting in flash takes it
  too; the rest of its write policy still applies (an installation-wide number shared by other chargers
  is not written). What the charger carried before SpotNav first wrote it is not stored anywhere, so the
  top of the range stands in for it.

Each step that fails is logged and the removal goes on. Nothing here touches another charger: a site
whose load balancing still governs others keeps doing so. A charger another SpotNav entry still drives
(the same physical charger set up twice) is left to that entry.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import CONF_CHARGE_CONTROL, DOMAIN
from ..vehicles.duplicate_chargers import duplicates_of
from ..vehicles.ocpp_identity import discover_controls
from .chargers.adapter import build_adapter
from .chargers.base import ASSIGN_ASSIGNED, ASSIGN_UNCHANGED, ASSIGN_UNCONFIRMED, WRITE_HANDBACK

_LOGGER = logging.getLogger(__name__)


async def _async_saved_memory(hass: HomeAssistant, entry_id: str) -> Any:
    """What the charger's start/stop path remembered (an Easee pause of SpotNav's), from the controller's own
    store; `None` when there is nothing readable."""
    # Imported here: the controller imports this package's modules.
    from .controller import STORE_VERSION

    try:
        saved = await Store[dict[str, Any]](hass, STORE_VERSION, f"{DOMAIN}.{entry_id}").async_load()
    except Exception:  # noqa: BLE001 - a store that cannot be read only means nothing is known
        return None
    return saved.get("adapter_memory") if isinstance(saved, dict) else None


async def async_hand_back_charger(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Leave the charger of a deleted entry allowed to charge at its full current. See the module docstring.

    Returns what was done (`started`, `current <n> A`), for the log and the tests. Never raises.
    """
    from .controller import charger_current_range, ocpp_target_of

    entry_id = entry.entry_id
    config = dict(entry.data)
    if not config.get(CONF_CHARGE_CONTROL):
        return []
    if duplicates_of(hass, entry):
        # Another SpotNav entry drives the same charger and goes on doing so: it is not ours to hand back.
        _LOGGER.info("SpotNav charger %s removed: another SpotNav entry still drives its charger", entry_id)
        return []
    done: list[str] = []
    try:
        target, _source = ocpp_target_of(hass, config)
        adapter = build_adapter(hass, config, ocpp_target=lambda: target, energy_entity_id=None)
        adapter.restore_memory(await _async_saved_memory(hass, entry_id))
    except Exception:  # noqa: BLE001 - the removal goes on whatever happens here
        _LOGGER.warning("SpotNav charger %s removed: its charger could not be handed back", entry_id, exc_info=True)
        return done

    is_paused = getattr(adapter.path, "is_paused", None)
    stopped = adapter.enabled_state() is False or (callable(is_paused) and is_paused() is True)
    if stopped:
        try:
            if await adapter.async_start():
                done.append("started")
            else:
                _LOGGER.warning(
                    "SpotNav charger %s removed: the charger is stopped and could not be turned back on "
                    "(its control is unavailable)",
                    entry_id,
                )
        except Exception:  # noqa: BLE001 - logged; the current is still handed back
            _LOGGER.warning(
                "SpotNav charger %s removed: the charger is stopped and could not be turned back on",
                entry_id,
                exc_info=True,
            )

    if adapter.capabilities.set_current:
        controls = None if target is None else discover_controls(hass, target)
        current_limit = getattr(adapter.current, "entity_id", None)
        maximum = int(charger_current_range(hass, current_limit, controls)["max_a"])
        try:
            outcome = await adapter.async_set_current(maximum, reason=WRITE_HANDBACK)
        except Exception:  # noqa: BLE001 - logged; the removal goes on
            outcome = None
            _LOGGER.warning(
                "SpotNav charger %s removed: the charger's current could not be set back to %s A",
                entry_id,
                maximum,
                exc_info=True,
            )
        if outcome in (ASSIGN_ASSIGNED, ASSIGN_UNCONFIRMED):
            done.append(f"current {maximum} A")
        elif outcome is not None and outcome != ASSIGN_UNCHANGED:
            _LOGGER.warning(
                "SpotNav charger %s removed: the charger's current was not set back to %s A (%s)",
                entry_id,
                maximum,
                outcome,
            )

    # Nothing is left to read back for: the entry is gone.
    adapter.cancel_pending_reads()
    if done:
        _LOGGER.info("SpotNav charger %s removed: handed the charger back (%s)", entry_id, ", ".join(done))
    else:
        _LOGGER.info("SpotNav charger %s removed: the charger needed nothing handed back", entry_id)
    return done
