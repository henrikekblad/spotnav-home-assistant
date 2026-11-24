"""Why a car's level cannot be read, told where a person can do something about it.

A car's level entity that reads `unavailable` is most often its cloud integration's doing: a login that expired
(the field morning of 24 November: Kia Connect's entities went `unavailable` at 23:01 and stayed so), an
integration that failed to start, or a cloud outage. SpotNav plans on (from the estimate, the last reading or a full
charge: `planning/auto_controller.py`) and the status says so (`soc_unreadable`); this module tells which of those it
is. Only a login or a failed start is something a person can fix (Settings → Devices & services), so only they are
named, at once. Otherwise the line waits `SOC_UNREADABLE_NOTICE_S`, so a cloud poll that drops out for a minute says
nothing. Both end by themselves once the level reads again: nothing is stored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_loaded_integration, IntegrationNotLoaded

from ..planning.status_compose import SocUnreadableFacts

#: How long a level entity reads nothing before the status says so, when no person can fix it (a cloud that drops
#: out for a poll or two says nothing).
SOC_UNREADABLE_NOTICE_S: Final = 900.0

#: The car integration's entry waits for a new login, or failed to start (a retry is the integration's own and ends
#: by itself: not said).
PROBLEM_REAUTH: Final = "reauth"
PROBLEM_SETUP_ERROR: Final = "setup_error"

_FAILED_STATES: Final = (ConfigEntryState.SETUP_ERROR, ConfigEntryState.MIGRATION_ERROR)


def integration_name(hass: HomeAssistant, domain: str) -> str:
    """The integration's own name (its manifest's, from the loader's cache: an entry with a flow or a failed setup has
    it loaded), else its domain. Never the entry's title: a person's title can hold an e-mail address."""
    try:
        name = async_get_loaded_integration(hass, domain).name
    except IntegrationNotLoaded:
        return domain
    return name if isinstance(name, str) and name else domain


def integration_problem(hass: HomeAssistant, entity_id: str) -> tuple[str, str] | None:
    """`(integration name, problem)` while the config entry `entity_id` belongs to needs a person, else `None`."""
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.config_entry_id is None:
        return None
    entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
    if entry is None:
        return None
    title = integration_name(hass, entry.domain)
    if any(True for _flow in entry.async_get_active_flows(hass, {SOURCE_REAUTH})):
        return title, PROBLEM_REAUTH
    if entry.state in _FAILED_STATES:
        return title, PROBLEM_SETUP_ERROR
    return None


def unreadable_facts(hass: HomeAssistant, entity_id: str, now: datetime) -> SocUnreadableFacts | None:
    """What the status says of a level entity that reads nothing: its integration's problem at once, else, once it has
    read nothing for `SOC_UNREADABLE_NOTICE_S`, that it cannot be read; `None` before that, and for an entity with no
    state at all (its integration not loaded yet, as right after a start)."""
    problem = integration_problem(hass, entity_id)
    if problem is not None:
        return SocUnreadableFacts(integration=problem[0], problem=problem[1])
    state = hass.states.get(entity_id)
    if state is None or (now - state.last_changed).total_seconds() < SOC_UNREADABLE_NOTICE_S:
        return None
    return SocUnreadableFacts()
