"""Home Assistant's Repairs surface, kept in step with `vehicles/resolution.py`.

One fixable issue per decision `resolve_required` reports, none for a decision that has been
made, created and deleted by `async_sync_resolution_repairs` (called by every mutating path).
An issue is a view of a decision: its id is `{kind}_{device_id}`, its text names the device and
kind but never a candidate entity id. The fix flow offers the same candidates, validates with the
same `valid_*_entity_id` and records through the same store calls as the services.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback, Event, EventStateChangedData, HassJob, HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
    selector,
)
from homeassistant.helpers.event import async_call_later

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .vehicles.duplicate_chargers import charger_entries_except, duplicate_pairs
from .vehicles.choices import (
    DISMISS_VEHICLE_CHOICE,
    entity_option,
    flow_language,
    RESOLVE_VEHICLE_TEXT,
)
from .vehicles.discovery_decisions import (
    async_setup_decisions,
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
)
from .vehicles.resolution import ResolutionDecision, resolve_required
from .vehicles.vehicle_discovery import (
    discover_ambiguous_charge_limits,
    discover_ambiguous_vehicles,
    valid_charge_limit_entity_id,
    valid_soc_entity_id,
)


#: The only entity domains discovery reads (`sensor` for a vehicle's shape, `number` for limits);
#: state changes elsewhere cannot change `resolve_required`.
_DISCOVERY_READ_DOMAINS = frozenset({"sensor", "number", "select"})

#: Debounce for a burst of discovery-input changes. Not zero: each sync reads the whole device and
#: entity registry, and one read should cover an integration populating many entities at once.
_RESYNC_DEBOUNCE_S = 2.0

#: Each kind of outstanding decision and its translation key under `issues` in `translations/*.json`.
_ISSUE_TRANSLATION_KEYS = {
    "soc": "vehicle_soc_needs_decision",
    "charge_limit": "vehicle_charge_limit_needs_decision",
}

#: Issues about how entries are set up (translation keys under `issues`, issue-id prefixes).
DUPLICATE_CHARGER_ISSUE = "duplicate_charger"
SITE_WITHOUT_CHARGERS_ISSUE = "site_without_chargers"

#: Abort reason when the decision is already gone; not an error, the human's goal is met.
_NO_LONGER_NEEDS_DECISION = "no_longer_needs_decision"


def issue_id_for(kind: str, device_id: str) -> str:
    """The stable issue id for one decision: its kind and its device, so a rename never orphans it."""
    return f"{kind}_{device_id}"


def decide_issue_id(issue_id: str) -> tuple[str, str] | None:
    """`(kind, device_id)` from an issue id, or `None`. The strict inverse of `issue_id_for`."""
    for kind in _ISSUE_TRANSLATION_KEYS:
        prefix = f"{kind}_"
        if issue_id.startswith(prefix) and (device_id := issue_id[len(prefix) :]):
            return kind, device_id
    return None


def _issue_fields(decision: ResolutionDecision) -> dict[str, Any]:
    """The issue fields one decision's issue carries.

    The device goes in the placeholders only, never a candidate entity id. `data` repeats
    `(kind, device_id)` but the id stays authoritative. `is_fixable` adds the *Fix* button;
    `WARNING` because nothing is broken, something is waiting for a person.
    """
    return {
        "translation_key": _ISSUE_TRANSLATION_KEYS[decision.kind],
        "translation_placeholders": {"device": decision.name},
        "is_fixable": True,
        "severity": ir.IssueSeverity.WARNING,
        "data": {"kind": decision.kind, "device_id": decision.device_id},
    }


def _setup_issue_fields(
    hass: HomeAssistant, exclude_entry_ids: Iterable[str] = ()
) -> dict[str, dict[str, Any]]:
    """The issues about how entries are set up, by issue id: two charger entries that are one physical
    charger (named on both, nothing removed for the person) and a site with no charger yet (how to add
    one). Not fixable: the person decides which entry stays.
    """
    excluded = set(exclude_entry_ids)
    issues: dict[str, dict[str, Any]] = {}
    for first, second, _ in duplicate_pairs(hass, excluded):
        issues[f"{DUPLICATE_CHARGER_ISSUE}_{first.entry_id}_{second.entry_id}"] = {
            "translation_key": DUPLICATE_CHARGER_ISSUE,
            "translation_placeholders": {"charger": first.title, "other": second.title},
            "is_fixable": False,
            "severity": ir.IssueSeverity.WARNING,
        }
    chargers = charger_entries_except(hass, excluded)
    if not chargers:
        for site in hass.config_entries.async_entries(DOMAIN):
            if site.entry_id not in excluded and site.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
                issues[f"{SITE_WITHOUT_CHARGERS_ISSUE}_{site.entry_id}"] = {
                    "translation_key": SITE_WITHOUT_CHARGERS_ISSUE,
                    "translation_placeholders": {"site": site.title},
                    "is_fixable": False,
                    "severity": ir.IssueSeverity.WARNING,
                }
    return issues


async def async_sync_resolution_repairs(
    hass: HomeAssistant, exclude_entry_ids: Iterable[str] = ()
) -> None:
    """Create and delete repair issues so they mirror `resolve_required` and the setup issues exactly.

    Called at setup and after every decision mutation or entry change; idempotent (an unchanged issue
    is left alone by `async_get_or_create`), so over-calling costs one read of live state. Deletion is
    scoped to this integration's own issues. `exclude_entry_ids` are entries being removed.
    """
    wanted = {
        issue_id_for(decision.kind, decision.device_id): _issue_fields(decision)
        for decision in resolve_required(hass)
    }
    wanted.update(_setup_issue_fields(hass, exclude_entry_ids))
    registry = ir.async_get(hass)
    for issue_id in [key[1] for key in registry.issues if key[0] == DOMAIN]:
        if issue_id not in wanted:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    for issue_id, fields in wanted.items():
        ir.async_create_issue(hass, DOMAIN, issue_id, **fields)


async def async_arm_resolution_sync(hass: HomeAssistant) -> Callable[[], None]:
    """Re-sync the Repairs issues whenever something discovery reads changes.

    The sync mirrors live state, so it goes stale when state changes without a decision (e.g. a car
    integration populating its charge-limit entities after startup). This subscribes to:

    * `sensor`, `number` and `select` state changes (`_DISCOVERY_READ_DOMAINS`);
    * the entity registry and the device registry (the coarse trigger; the debounce makes it cheap).

    Each schedules one debounced sync. The returned callable removes all listeners and cancels a
    pending sync. Not idempotent: arm once per process.
    """
    pending_cancel: Callable[[], None] | None = None

    @callback
    def run_scheduled_sync(_now: Any) -> None:
        """Run the one sync a burst of changes earned."""
        nonlocal pending_cancel
        pending_cancel = None
        hass.async_create_task(async_sync_resolution_repairs(hass))

    @callback
    def schedule_sync(_event: Any) -> None:
        """Collect one more relevant change and sync two seconds after the last.

        The timer is a `HassJob` cancelled when Home Assistant stops; cancelling the previous one first
        makes the delay run from the last change.
        """
        nonlocal pending_cancel
        if pending_cancel is not None:
            pending_cancel()
        pending_cancel = async_call_later(
            hass,
            _RESYNC_DEBOUNCE_S,
            HassJob(run_scheduled_sync, cancel_on_shutdown=True),
        )

    @callback
    def state_changed(event: Event[EventStateChangedData]) -> None:
        """React to a state change only when discovery could have read it."""
        entity_id = event.data.get("entity_id")
        if isinstance(entity_id, str) and entity_id.split(".", 1)[0] in _DISCOVERY_READ_DOMAINS:
            schedule_sync(event)

    unlisteners = [
        hass.bus.async_listen(EVENT_STATE_CHANGED, state_changed),
        # The registry events live on their own registry modules, not `homeassistant.const`.
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, schedule_sync),
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, schedule_sync),
    ]

    @callback
    def cancel() -> None:
        """Stop listening, and drop a sync that has not run yet."""
        nonlocal pending_cancel
        for unlisten in unlisteners:
            unlisten()
        if pending_cancel is not None:
            pending_cancel()
            pending_cancel = None

    return cancel


class ResolutionRepairFlow(RepairsFlow):
    """The fix for one outstanding decision: pick, and record it.

    Not a second decision path: candidates are re-read live, the pick is checked with the services'
    `valid_*_entity_id` and recorded through the same store calls. A device that stopped being
    ambiguous while the issue was open aborts instead of offering a stale dropdown.
    """

    def __init__(self, kind: str, device_id: str, name: str) -> None:
        """Remember which decision this flow is about, and what to call it."""
        self._kind = kind
        self._device_id = device_id
        self._name = name

    def _live_candidates(self) -> list[str]:
        """What this decision still has to choose between, read fresh, or `[]` if it is gone."""
        if self._kind == "soc":
            candidates = [
                candidate
                for candidate in discover_ambiguous_vehicles(self.hass)
                if candidate.id == self._device_id
            ]
        else:
            candidates = [
                candidate
                for candidate in discover_ambiguous_charge_limits(self.hass)
                if candidate.id == self._device_id
            ]
        return list(candidates[0].candidate_entity_ids) if candidates else []

    def _selector(self, candidate_entity_ids: list[str]) -> selector.SelectSelector:
        """The dropdown for these candidates.

        The state-of-charge kind also gets the "this is not a vehicle" option (the config flow's
        sentinel); a charge limit does not, because the device is a vehicle and only its limit is undecided.
        """
        options = [entity_option(self.hass, entity_id) for entity_id in candidate_entity_ids]
        if self._kind == "soc":
            options.append(
                selector.SelectOptionDict(
                    value=DISMISS_VEHICLE_CHOICE,
                    label=RESOLVE_VEHICLE_TEXT[flow_language(self.hass)]["dismiss"],
                )
            )
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Offer the live candidates, or abort when nothing is left to decide."""
        candidate_entity_ids = self._live_candidates()
        if not candidate_entity_ids:
            return self.async_abort(reason=_NO_LONGER_NEEDS_DECISION)
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required("choice"): self._selector(candidate_entity_ids)}),
            description_placeholders={"device": self._name},
        )

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Record the pick through the same store the services write to."""
        if user_input is None:
            return await self.async_step_init()
        choice = str(user_input["choice"])
        store = await async_setup_decisions(self.hass)
        if self._kind == "soc":
            if choice == DISMISS_VEHICLE_CHOICE:
                await store.async_dismiss(DECISION_DOMAIN_VEHICLE, self._device_id)
            elif valid_soc_entity_id(self.hass, self._device_id, choice):
                await store.async_confirm(
                    DECISION_DOMAIN_VEHICLE, self._device_id, {"soc_entity_id": choice}
                )
            else:
                # Only reachable via a race with something else resolving the device first.
                return self.async_abort(reason=_NO_LONGER_NEEDS_DECISION)
        elif valid_charge_limit_entity_id(self.hass, self._device_id, choice):
            await store.async_confirm(
                DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
                self._device_id,
                {"charge_limit_entity_id": choice},
            )
        else:
            return self.async_abort(reason=_NO_LONGER_NEEDS_DECISION)
        # Sync now so the list the human is looking at updates the moment they act.
        await async_sync_resolution_repairs(self.hass)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Build the flow for one issue (Home Assistant's repairs-platform contract).

    The issue id, parsed with `decide_issue_id`, says which decision this is; the name is read
    live so a rename shows at once. An id this module did not create is refused.
    """
    decided = decide_issue_id(issue_id)
    if decided is None:
        raise ValueError(f"Not a resolution issue id: {issue_id}")
    kind, device_id = decided
    return ResolutionRepairFlow(kind, device_id, _decision_name(hass, kind, device_id))


def _decision_name(hass: HomeAssistant, kind: str, device_id: str) -> str:
    """What to call this decision's device, or the device id when it is gone (read live)."""
    for decision in resolve_required(hass):
        if decision.kind == kind and decision.device_id == device_id:
            return decision.name
    return device_id


async def async_record_vehicle_soc(
    hass: HomeAssistant, store: Any, device_id: str, entity_id: str
) -> None:
    """Record `entity_id` as `device_id`'s state of charge: the one decision write.

    The `confirm_vehicle_soc` service and the card's `spotnav/choose_vehicle_soc` command both go
    through this. The caller has already validated the pair.
    """
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": entity_id})
    await async_sync_resolution_repairs(hass)


async def async_clear_vehicle_soc(hass: HomeAssistant, store: Any, device_id: str) -> None:
    """Forget the state-of-charge decision, back to automatic detection (the `unconfirm_vehicle` undo)."""
    await store.async_unconfirm(DECISION_DOMAIN_VEHICLE, device_id)
    await async_sync_resolution_repairs(hass)
