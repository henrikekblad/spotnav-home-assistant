"""Durable, integration-wide record of human decisions about discovered candidates.

Detection is conservative and a person can see that a candidate is wrong (a vehicle-like device that
is not a car). One store serves the whole integration, keyed per decision domain (`"vehicle"` today)
so features cannot collide over an id. It is not per config entry: a decision is about the system (a
device, a sensor) and must outlive that device being renamed, re-added, or not existing yet.

Loaded once from `async_setup`, then read synchronously from memory: `discover_vehicles` runs inside
the dashboard capture, which must not await storage.

    {"dismissed": {"vehicle": ["<stable id>", ...]},
     "confirmed": {"vehicle": {"<stable id>": {"...": "decision payload"}}}}

* `dismissed` is a flag per candidate; `confirmed` carries a payload (for vehicles: which entity
  holds the state of charge) whose shape is the domain's business.
* The two kinds are independent: neither clears the other, since "do not report this" and "read it
  this way if it is ever a candidate" answer different questions. Undismissing later uses the
  confirmation already stored.
* Only this module writes the file, always whole with sorted ids. Keys it does not recognise are
  carried through a save untouched, so it never clobbers a key a newer feature added.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ..runtime import domain_data


# Decision domains: strings, not an enum, because they are part of the persisted format.
DECISION_DOMAIN_VEHICLE = "vehicle"
# Which charge-limit `number` a write goes to when a car has several (AC and DC).
DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT = "vehicle_charge_limit"
# A vehicle's own properties (`capacity_kwh`, `consumption_kwh_per_10km`), in a
# domain of its own for the same reason. Written only by `vehicles/vehicle_properties.py`.
DECISION_DOMAIN_VEHICLE_PROPERTIES = "vehicle_properties"

_DISMISSED_KEY = "dismissed"
_CONFIRMED_KEY = "confirmed"
_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}_discovery_decisions"


class DiscoveryDecisionStore:
    """A human's decisions about discovered candidates, in one file.

    Every mutation is a read-modify-write of one JSON blob. All callers run on the event loop, so
    nothing is re-entrant; the lazy first load (`_ensure_loaded`) keeps a dismissal made before load from
    overwriting a file this instance never read.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        # The whole loaded file, so a save keeps keys this code does not know.
        self._stored: dict[str, Any] = {}
        self._dismissed: dict[str, set[str]] = {}
        self._confirmed: dict[str, dict[str, dict[str, Any]]] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Read the file once and cache its decisions.

        Tolerates anything (missing file, wrong shapes, unusable entries): losing a decision is a smaller
        problem than refusing to load or inventing one from junk.
        """
        raw = await self._store.async_load()
        self._stored = dict(raw) if isinstance(raw, dict) else {}
        self._dismissed = {}
        self._confirmed = {}
        stored_dismissed = self._stored.get(_DISMISSED_KEY)
        if isinstance(stored_dismissed, dict):
            for domain, ids in stored_dismissed.items():
                if not isinstance(domain, str) or not isinstance(ids, list):
                    continue
                valid = {value for value in ids if isinstance(value, str) and value}
                if valid:
                    self._dismissed[domain] = valid
        stored_confirmed = self._stored.get(_CONFIRMED_KEY)
        if isinstance(stored_confirmed, dict):
            for domain, payloads in stored_confirmed.items():
                if not isinstance(domain, str) or not isinstance(payloads, dict):
                    continue
                valid_payloads = {
                    stable_id: dict(payload)
                    for stable_id, payload in payloads.items()
                    if isinstance(stable_id, str)
                    and stable_id
                    and _is_valid_payload(payload)
                }
                if valid_payloads:
                    self._confirmed[domain] = valid_payloads
        self._loaded = True

    def is_dismissed(self, domain: str, stable_id: str) -> bool:
        """Whether this candidate was dismissed, from the in-memory cache.

        Before `async_load` nothing is dismissed. A blank id is never dismissed.
        """
        if not stable_id:
            return False
        dismissed = self._dismissed.get(domain)
        return dismissed is not None and stable_id in dismissed

    async def async_dismiss(self, domain: str, stable_id: str) -> None:
        """Remember that this candidate is dismissed; a no-op (no write) if already so or blank."""
        if not stable_id:
            return
        await self._ensure_loaded()
        dismissed = self._dismissed.get(domain)
        if dismissed is not None and stable_id in dismissed:
            return
        self._dismissed.setdefault(domain, set()).add(stable_id)
        await self._async_save()

    async def async_undismiss(self, domain: str, stable_id: str) -> None:
        """Forget a dismissal, making the candidate eligible again.

        A no-op (no write) when nothing is dismissed. Never touches a confirmation for the same candidate.
        """
        if not stable_id:
            return
        await self._ensure_loaded()
        dismissed = self._dismissed.get(domain)
        if dismissed is None or stable_id not in dismissed:
            return
        dismissed.discard(stable_id)
        if not dismissed:
            self._dismissed.pop(domain, None)
        await self._async_save()

    def confirmed_payload(self, domain: str, stable_id: str) -> dict[str, Any] | None:
        """The stored confirmation for this candidate as a copy, or `None`.

        Synchronous and scoped per domain and id like `is_dismissed`. A copy, so a caller cannot mutate the
        cache; a payload with no usable content is no confirmation.
        """
        if not stable_id:
            return None
        payload = self._confirmed.get(domain, {}).get(stable_id)
        return dict(payload) if payload else None

    async def async_confirm(self, domain: str, stable_id: str, payload: dict[str, Any]) -> None:
        """Record a human's decision about a candidate as a payload.

        Confirming again replaces the payload (the same human changing their mind). A blank id or empty
        payload records nothing; a payload that is not a string-keyed dict is a caller bug and is rejected.
        Does not touch a dismissal of the same candidate and is not undone by one.
        """
        if not stable_id or not payload:
            return
        if not _is_valid_payload(payload):
            raise ValueError("A confirmation payload must be a non-empty dict with string keys")
        await self._ensure_loaded()
        if self._confirmed.get(domain, {}).get(stable_id) == payload:
            return
        self._confirmed.setdefault(domain, {})[stable_id] = dict(payload)
        await self._async_save()

    async def async_unconfirm(self, domain: str, stable_id: str) -> None:
        """Forget a confirmation; a no-op (no write) if none. Never touches a dismissal."""
        if not stable_id:
            return
        await self._ensure_loaded()
        payloads = self._confirmed.get(domain)
        if payloads is None or stable_id not in payloads:
            return
        payloads.pop(stable_id, None)
        if not payloads:
            self._confirmed.pop(domain, None)
        await self._async_save()

    async def _ensure_loaded(self) -> None:
        """Load on first use, so a decision is never saved over an unread file."""
        if not self._loaded:
            await self.async_load()

    async def _async_save(self) -> None:
        data = dict(self._stored)
        data[_DISMISSED_KEY] = {
            domain: sorted(ids) for domain, ids in sorted(self._dismissed.items()) if ids
        }
        data[_CONFIRMED_KEY] = {
            domain: {
                stable_id: dict(payloads[stable_id])
                for stable_id in sorted(payloads)
            }
            for domain, payloads in sorted(self._confirmed.items())
            if payloads
        }
        await self._store.async_save(data)
        self._stored = data


def _is_valid_payload(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and bool(payload)
        and all(isinstance(key, str) and key for key in payload)
    )


async def async_setup_decisions(hass: HomeAssistant) -> DiscoveryDecisionStore:
    """Create, load and publish the integration-wide decision store.

    Idempotent: an already-set-up store is returned untouched, so calling it from `async_setup` and
    from anything else that needs the store never creates a second, stale cache of the same file.
    """
    data = domain_data(hass)
    if data.decision_store is not None:
        return data.decision_store
    store = DiscoveryDecisionStore(hass)
    await store.async_load()
    data.decision_store = store
    return store
