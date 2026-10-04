"""One charger's notifier: what happened, sent to the chosen phones through `notify.<service>`.

It watches what the charger's event entity watches (the controller and the Auto snapshot) with its own
trackers, so a disabled event entity does not silence a phone:

* `ChargerEventTracker` (`execution/charger_events.py`) for `charge_started`, `plugged_in`, `unplugged`,
  `plan_installed` and `plan_at_risk`;
* `UnexpectedStopDetector` (`unexpected_stop.py`) for `plan_stopped`, looked at again when its grace
  period runs out;
* the controller's `completion_record` for `charge_complete`, with the charge's energy and cost from
  its session.

Every event first sets a baseline: loading the integration is not an event. Sending is rate-limited
per charger: the same event is not sent again within `REPEAT_S`, and no more than `HOURLY_LIMIT`
notifications go out in an hour. Each notification carries a `tag` (one per charger and event), so a
phone replaces an older one of the same kind instead of stacking them, and a `url` a tap opens.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Final

from homeassistant.core import callback, CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from ..execution.charge_progress import STATE_VEHICLE_NOT_REQUESTING_CURRENT
from ..execution.charger_entities import charge_control_problem
from ..execution.charger_events import (
    ChargerEventTracker,
    EVENT_CHARGE_STARTED,
    EVENT_PLAN_AT_RISK,
    EVENT_PLAN_INSTALLED,
    EVENT_PLUGGED_IN,
    EVENT_UNPLUGGED,
)
from ..execution.controller import ChargingController
from ..planning.auto_controller import AutoSnapshot
from ..planning.auto_settings import AutoSettingsStore
from ..sessions.store import SessionStore
from .messages import compose, language_of, Money
from .settings import EVENT_CHARGE_COMPLETE, EVENT_PLAN_STOPPED, NOTIFY_DOMAIN
from .unexpected_stop import ExpectationFacts, UnexpectedStopDetector

_LOGGER = logging.getLogger(__name__)

#: The same event for the same charger is not sent again within this long.
REPEAT_S: Final = 15 * 60.0
#: At most this many notifications per charger in an hour, whatever they are.
HOURLY_LIMIT: Final = 12
#: What a tap opens when no dashboard path is set: Home Assistant's default dashboard.
DEFAULT_URL: Final = "/"
#: A completed charge's session is the open one, or one that closed this recently.
_SESSION_RECENT_S: Final = 600.0

#: Tracker events that are notification events of the same name.
_TRACKED: Final = (
    EVENT_CHARGE_STARTED,
    EVENT_PLUGGED_IN,
    EVENT_UNPLUGGED,
    EVENT_PLAN_INSTALLED,
    EVENT_PLAN_AT_RISK,
)


def _local_time(moment: datetime | None) -> str | None:
    return None if moment is None else dt_util.as_local(moment).strftime("%H:%M")


class ChargerNotifier:
    """Sends one charger's chosen events to its chosen notify services."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        name: Callable[[], str],
        controller: ChargingController,
        store: AutoSettingsStore,
        sessions: SessionStore | None = None,
        currency: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._name = name
        self._controller = controller
        self._store = store
        self._sessions = sessions
        self._currency = currency
        self._tracker = ChargerEventTracker()
        self._stops = UnexpectedStopDetector()
        self._snapshot: AutoSnapshot | None = None
        self._completion_at: str | None = None
        self._baselined = False
        self._recheck: CALLBACK_TYPE | None = None
        self._recheck_at: datetime | None = None
        self._last_sent: dict[str, datetime] = {}
        self._sent_times: deque[datetime] = deque()
        self._unsubscribe: list[CALLBACK_TYPE] = []

    @callback
    def async_start(self, preview: Any = None) -> None:
        """Take the baseline and start watching."""
        self._observe()
        self._unsubscribe.append(self._controller.add_listener(self._observe))
        self._unsubscribe.append(self._controller.add_charge_state_listener(self._observe))
        if preview is not None:
            self._unsubscribe.append(preview.add_listener(self._on_snapshot))

    @callback
    def async_shutdown(self) -> None:
        for remove in self._unsubscribe:
            remove()
        self._unsubscribe.clear()
        self._cancel_recheck()

    @callback
    def _on_snapshot(self, snapshot: AutoSnapshot) -> None:
        self._snapshot = snapshot
        self._observe()

    @callback
    def _observe(self, *_: Any) -> None:
        """One observation: decide every event once, then send the chosen ones."""
        from ..event import charger_facts  # the event platform's own facts; imported late (platform module)

        controller = self._controller
        settings = self._store.settings(self._entry_id)
        now = dt_util.utcnow()
        events: list[tuple[str, dict[str, Any]]] = []
        for kind, attributes in self._tracker.observe(
            charger_facts(self._hass, controller, self._snapshot, settings)
        ):
            if kind in _TRACKED:
                events.append((kind, attributes))
        reason = self._stops.observe(self._expectation(now))
        if reason is not None:
            events.append((EVENT_PLAN_STOPPED, {"reason": reason}))
        completion = controller.completion_record
        completion_at = None if completion is None else completion.get("at")
        if self._baselined and completion is not None and completion_at != self._completion_at:
            events.append((EVENT_CHARGE_COMPLETE, dict(completion)))
        self._completion_at = completion_at
        self._baselined = True
        self._arm_recheck(self._stops.due_at)
        for kind, attributes in events:
            self._maybe_send(kind, attributes, now)

    def _expectation(self, now: datetime) -> ExpectationFacts:
        controller = self._controller
        expected = controller.plan_expects_charge
        window = None
        plan = controller.plan
        if expected and plan is not None:
            try:
                start = next((begin for begin, end in plan.windows if begin <= now < end), None)
            except ValueError:
                start = None
            window = None if start is None else f"{plan.start}|{plan.end}|{start.isoformat()}"
        return ExpectationFacts(
            now=now,
            expected=expected,
            window=window,
            charging=bool(controller.charging),
            available=charge_control_problem(self._hass, controller.charge_control) is None,
            start_pending=controller.start_pending,
            vehicle_not_requesting=controller.charge_progress.state == STATE_VEHICLE_NOT_REQUESTING_CURRENT,
            held_by_charger=controller.held_by_charger,
            charger_disabled=controller.charger_disabled,
            vehicle_unknown=controller.adapter.reports_connection and controller.adapter.vehicle_connected() is None,
            at_vehicle_limit=self._at_vehicle_limit(),
        )

    def _at_vehicle_limit(self) -> bool:
        """Whether the plan's target is above the car's own charge limit and the car has reached that
        limit: it takes no current, rightly."""
        controller = self._controller
        plan = controller.plan
        if plan is None or plan.target_soc_percent is None:
            return False
        reading = controller.target_reading()
        if reading is None or reading.soc_percent is None:
            return False
        from ..runtime import charger_data  # the charger's own state-of-charge source

        data = charger_data(self._hass, self._entry_id)
        soc_reader = None if data is None else data.soc_reader
        limit = None if soc_reader is None else soc_reader.vehicle_max_percent(plan.vehicle_id)
        ceiling = 100.0 if limit is None else min(float(limit), 100.0)
        return ceiling < plan.target_soc_percent + 0.5 and reading.soc_percent >= ceiling - 0.5

    def _arm_recheck(self, due: datetime | None) -> None:
        """Look again when trouble's grace period runs out: nothing else may report meanwhile."""
        if due == self._recheck_at:
            return
        self._cancel_recheck()
        if due is None:
            return
        self._recheck_at = due

        @callback
        def _fire(_now: datetime) -> None:
            self._recheck = None
            self._recheck_at = None
            self._observe()

        self._recheck = async_track_point_in_utc_time(self._hass, _fire, due + timedelta(seconds=1))

    def _cancel_recheck(self) -> None:
        if self._recheck is not None:
            self._recheck()
        self._recheck = None
        self._recheck_at = None

    # ------------------------------------------------------------------ sending

    def _maybe_send(self, event: str, attributes: dict[str, Any], now: datetime) -> None:
        notifications = self._store.settings(self._entry_id).notifications
        if not notifications.wants(event):
            return
        last = self._last_sent.get(event)
        if last is not None and (now - last).total_seconds() < REPEAT_S:
            _LOGGER.debug("SpotNav charger %s: %s not notified again so soon", self._entry_id, event)
            return
        while self._sent_times and (now - self._sent_times[0]).total_seconds() >= 3600:
            self._sent_times.popleft()
        if len(self._sent_times) >= HOURLY_LIMIT:
            _LOGGER.debug("SpotNav charger %s: hourly notification limit reached", self._entry_id)
            return
        targets = [
            target
            for target in notifications.targets
            if self._hass.services.has_service(NOTIFY_DOMAIN, target)
        ]
        if not targets:
            return
        self._last_sent[event] = now
        self._sent_times.append(now)
        title, message = compose(
            event, self._name(), self._facts_for(event, attributes), language_of(self._hass.config.language)
        )
        url = notifications.url or DEFAULT_URL
        payload = {
            "title": title,
            "message": message,
            "data": {
                # `url` is what iOS opens on a tap, `clickAction` what Android does.
                "url": url,
                "clickAction": url,
                "tag": f"spotnav_{self._entry_id}_{event}",
                "group": "spotnav",
                "channel": "SpotNav",
            },
        }
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)

    async def _async_send(self, target: str, payload: dict[str, Any]) -> None:
        try:
            await self._hass.services.async_call(NOTIFY_DOMAIN, target, payload, blocking=True)
        except Exception as err:  # noqa: BLE001 - a phone that cannot be reached must not stop the charger
            _LOGGER.warning("SpotNav could not notify %s: %s", target, type(err).__name__)

    def _facts_for(self, event: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """What the message says beside the event: times in the installation's zone, kWh, money."""
        controller = self._controller
        plan = controller.plan
        if event == EVENT_PLAN_STOPPED:
            return {"reason": attributes.get("reason")}
        if event == EVENT_PLAN_AT_RISK:
            return {"time": attributes.get("departure_time")}
        if event == EVENT_CHARGE_STARTED:
            until = None
            if plan is not None and controller.plan_window_active_now:
                now = dt_util.utcnow()
                until = next((end for begin, end in plan.windows if begin <= now < end), None)
            return {"until": _local_time(until)}
        if event == EVENT_PLAN_INSTALLED:
            facts: dict[str, Any] = {"time": None if plan is None else _local_time(plan.start_time)}
            if plan is not None and plan.energy_kwh is not None:
                facts["kwh"] = plan.energy_kwh
            cost = self._plan_cost()
            if cost is not None:
                facts["cost"] = cost
            return facts
        if event == EVENT_CHARGE_COMPLETE:
            facts = {
                "reason": attributes.get("reason"),
                "target_percent": attributes.get("target_soc_percent"),
            }
            session = self._recent_session()
            if session is not None:
                facts["kwh"] = session.energy_kwh
                if session.cost_minor is not None and session.currency:
                    facts["cost"] = Money(int(round(session.cost_minor)), session.currency)
            return facts
        return {}

    def _plan_cost(self) -> Money | None:
        snapshot = self._snapshot
        proposal = None if snapshot is None else snapshot.proposal
        cost = None if proposal is None else proposal.estimated_cost
        currency = self._currency()
        if cost is None or currency is None or snapshot is None or not snapshot.applied:
            return None
        return Money(int(round(cost * 100)), currency)

    def _recent_session(self) -> Any:
        """The session of the charge that just completed: the open one, else one that just closed."""
        store = self._sessions
        if store is None:
            return None
        session = store.open_session(self._entry_id)
        if session is not None:
            return session
        closed = store.closed(self._entry_id)
        if not closed:
            return None
        last = closed[-1]
        if last.end is None or (dt_util.utcnow() - last.end).total_seconds() > _SESSION_RECENT_S:
            return None
        return last
