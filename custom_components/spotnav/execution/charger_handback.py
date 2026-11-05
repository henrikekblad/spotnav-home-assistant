"""Hand a charger back when its SpotNav entry is deleted for good.

Someone who removes SpotNav expects the charger to be as SpotNav found it. SpotNav may have left it stopped (a plan
waiting for a cheap hour, the sun gone, a balancing pause, a target reached) or set to another current (solar, load
balancing, the person's charging current), and with SpotNav gone nothing would ever change that back. Only what
SpotNav itself did is undone, and only with what SpotNav knows; nothing is guessed.

Only `async_remove_entry` calls this, and Home Assistant calls that only when an entry is deleted: an unload (a
restart, a reload, a disabled entry) never comes here, since the entry comes back and takes the charger on as it
left it. What is known comes from the charger's controller record (`spotnav.<entry_id>`), which the unload saved.

What is done, once, through the same paths SpotNav drove the charger with (`chargers/adapter.py`):

* **The current.** Just before SpotNav first wrote a current to the charger, through its current path or a start or
  stop that writes one (ABB's pause by 0 A, Easee's pause, Peblar's hold at the floor), it read what the charger
  carried through that path's own read-back (the current number's state, the connector's entry in OCPP's
  `AssignedCurrent`, Easee's dynamic limit sensor) and recorded it once (`PreSpotnavCurrent`, store key
  `pre_spotnav_current`). That value is written back once, with `WRITE_HANDBACK`, so a charger that keeps the
  setting in flash takes it too, unless the charger already reads it. When it is unknown (the read said nothing,
  SpotNav never wrote, or the charger was set up before this was recorded) nothing is written and the current is
  left as it is. A path SpotNav may not write today is not written either (no opt-in to setting the current, an
  installation-wide number shared by other chargers).
* **The stop.** The charger is started only when its stopped state is SpotNav's own and its control still reads off
  (a switch, a select's stop option, a current number at 0 A) or paused (an Easee SpotNav paused). The controller
  keeps that in its record (store key `spotnav_stopped`, `ownership_state.own_stop_from_record`): set when a stop of
  SpotNav's own decision goes out, cleared by any start, a person's Stop or pause through the card, the app or a
  button, and a charge or an "on" control seen with no stop of SpotNav's on its way. So a person's stop, or one by
  someone or something else, is left as it is. A control that cannot tell (button pairs) is left alone, since
  pressing Start would start a charge rather than allow one.

Each step that fails is logged and the removal goes on; what was done, or why nothing was, is logged at info.
Nothing here touches another charger: a site whose load balancing still governs others keeps doing so. A charger
another SpotNav entry still drives (the same physical charger set up twice) is left to that entry.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import CONF_CHARGE_CONTROL, DOMAIN
from ..vehicles.duplicate_chargers import duplicates_of
from .chargers.adapter import build_adapter, PRE_SPOTNAV_CURRENT_STORE_KEY, PreSpotnavCurrent
from .chargers.base import ASSIGN_ASSIGNED, ASSIGN_UNCHANGED, ASSIGN_UNCONFIRMED, WRITE_HANDBACK
from .ownership_state import own_stop_from_record, person_paused_in_record

_LOGGER = logging.getLogger(__name__)


async def _async_saved_record(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    """The charger's controller record, or `None` when there is none or it cannot be read."""
    # Imported here: the controller imports this package's modules.
    from .controller import STORE_VERSION

    try:
        saved = await Store[dict[str, Any]](hass, STORE_VERSION, f"{DOMAIN}.{entry_id}").async_load()
    except Exception:  # noqa: BLE001 - a store that cannot be read only means nothing is known
        return None
    return saved if isinstance(saved, dict) and saved else None


async def async_hand_back_charger(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Give the charger of a deleted entry back as SpotNav found it, as far as SpotNav knows. See the module docstring.

    Returns what was done (`started`, `current <n> A`), for the log and the tests. Never raises.
    """
    from .controller import ocpp_target_of

    entry_id = entry.entry_id
    config = dict(entry.data)
    if not config.get(CONF_CHARGE_CONTROL):
        return []
    if duplicates_of(hass, entry):
        # Another SpotNav entry drives the same charger and goes on doing so: it is not ours to hand back.
        _LOGGER.info("SpotNav charger %s removed: another SpotNav entry still drives its charger", entry_id)
        return []
    done: list[str] = []
    left: list[str] = []
    try:
        saved = await _async_saved_record(hass, entry_id)
        target, _source = ocpp_target_of(hass, config)
        adapter = build_adapter(hass, config, ocpp_target=lambda: target, energy_entity_id=None)
        adapter.restore_memory(None if saved is None else saved.get("adapter_memory"))
        own_stop = saved is not None and own_stop_from_record(saved) and not person_paused_in_record(saved)
        pre = PreSpotnavCurrent.from_store(
            None if saved is None else saved.get(PRE_SPOTNAV_CURRENT_STORE_KEY), record_exists=saved is not None
        )
    except Exception:  # noqa: BLE001 - the removal goes on whatever happens here
        _LOGGER.warning("SpotNav charger %s removed: its charger could not be handed back", entry_id, exc_info=True)
        return done
    before = pre.amps if pre.recorded else None

    try:
        is_paused = getattr(adapter.path, "is_paused", None)
        stopped = adapter.enabled_state() is False or (callable(is_paused) and is_paused() is True)
    except Exception:  # noqa: BLE001 - a control that cannot be read is not started
        stopped = False
    if stopped and not own_stop:
        left.append("it was stopped by a person or outside SpotNav, so it stays stopped")
    elif stopped:
        try:
            # A start that is itself a current write (ABB's) starts at the current the charger had before SpotNav.
            if await adapter.async_start(before):
                done.append("started")
            else:
                _LOGGER.warning(
                    "SpotNav charger %s removed: SpotNav had stopped the charger and could not turn it back on "
                    "(its control is unavailable)",
                    entry_id,
                )
        except Exception:  # noqa: BLE001 - logged; the current is still handed back
            _LOGGER.warning(
                "SpotNav charger %s removed: SpotNav had stopped the charger and could not turn it back on",
                entry_id,
                exc_info=True,
            )

    if adapter.capabilities.set_current:
        if before is None:
            left.append("the current it had before SpotNav is not known, so its current is left as it is")
        else:
            await _async_restore_current(adapter, entry_id, before, done, left)

    # Nothing is left to read back for: the entry is gone.
    adapter.cancel_pending_reads()
    if done:
        _LOGGER.info(
            "SpotNav charger %s removed: handed the charger back (%s)%s",
            entry_id,
            ", ".join(done),
            f"; {'; '.join(left)}" if left else "",
        )
    else:
        _LOGGER.info(
            "SpotNav charger %s removed: nothing was handed back%s",
            entry_id,
            f" ({'; '.join(left)})" if left else "",
        )
    return done


async def _async_restore_current(
    adapter: Any, entry_id: str, before: int, done: list[str], left: list[str]
) -> None:
    """Write `before` once through the current path, unless the charger already reads it. Never raises."""
    try:
        present = await adapter.current.async_present_a()
    except Exception:  # noqa: BLE001 - an unreadable current is written all the same
        present = None
    if present == before:
        left.append(f"its current already reads {before} A, as before SpotNav")
        return
    try:
        outcome = await adapter.async_set_current(before, reason=WRITE_HANDBACK)
    except Exception:  # noqa: BLE001 - logged; the removal goes on
        _LOGGER.warning(
            "SpotNav charger %s removed: the charger's current could not be set back to %s A",
            entry_id,
            before,
            exc_info=True,
        )
        return
    if outcome in (ASSIGN_ASSIGNED, ASSIGN_UNCONFIRMED):
        done.append(f"current {before} A")
    elif outcome == ASSIGN_UNCHANGED:
        left.append(f"its current already reads {before} A, as before SpotNav")
    else:
        _LOGGER.warning(
            "SpotNav charger %s removed: the charger's current was not set back to %s A (%s)",
            entry_id,
            before,
            outcome,
        )
