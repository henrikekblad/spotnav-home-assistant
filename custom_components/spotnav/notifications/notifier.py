"""One charger's notifier: what happened, sent to the chosen phones through `notify.<service>`.

It watches what the charger's event entity watches (the controller and the Auto snapshot) with its own
trackers, so a disabled event entity does not silence a phone:

* `ChargerEventTracker` (`execution/charger_events.py`) for `charge_started`, `plugged_in`, `unplugged`,
  `plan_installed` and `plan_at_risk`;
* `UnexpectedStopDetector` (`unexpected_stop.py`) for `plan_stopped`, looked at again when its grace
  period runs out, and the controller's `ignores_person_stop` for `plan_stopped` as
  `charger_ignores_stop` (SpotNav gave up stopping a charger that keeps charging under a person's Stop);
* the controller's `completion_record` for `charge_complete`, with the charge's energy and cost from
  its session.

A new plan (`plan_installed`) is told only when it is news to a person, on the phones and in the app's wake-up
alike (the event entity still fires for every plan):

* it settles first: plans installed within `SETTLE_S` of one another are one burst, and only the plan standing
  when the burst is over is judged (`SETTLE_MAX_S` bounds a burst that never ends);
* not while the car is known to be away: a charger that says no car is connected keeps planning, but nothing
  is told. An unknown reading keeps the last known one; a charger that cannot say at all tells as before. After
  a plug-in the first plan installed is told; if none comes, the standing plan is told after `PLUG_IN_WAIT_S`
  or when its first window opens, whichever is sooner;
* not when it is the plan last told about (`plan_fingerprint`, kept across restarts), nor what is left of it
  (`plan_is_remainder`: a window ended, or the plan was calculated again partway through);
* not when it follows a person's own settings write (`QUIET_AFTER_WRITE_S`): they see it.

Each plan told is counted (`plan_notice`, kept across restarts with the told plan), whatever phones are chosen
or limits allow: the dashboard carries the count, and the paired app's own check tells a new plan when it
rises instead of deciding by itself.

No outcome of a charge that cannot happen is told while the car is known to be away (the same reading as
above: an unknown one keeps the last known, a charger that cannot say tells as before), on the phones and in the
app's wake-up alike: no `plan_at_risk`, no `plan_stopped` for a window that did not start and no `charge_complete`.
A `plan_at_risk` held back is told after the plug-in, when the plan made for the car is judged, if the departure
still cannot be met then.

Every event first sets a baseline: loading the integration is not an event. Sending is rate-limited
per charger: the same event is not sent again within `REPEAT_S`, and no more than `HOURLY_LIMIT`
notifications go out in an hour. Each notification carries a `tag` (one per charger and event), so a
phone replaces an older one of the same kind instead of stacking them, and a `url` a tap opens.

The question of which car is plugged in (`vehicle_identify`) is asked by the charger's vehicle identification
(`vehicles/identification.py`) through `ask_vehicle`: one button per car with the action ids it gives, the tag it
gives, counted against `HOURLY_LIMIT` but not `REPEAT_S` (there is at most one per plug-in). Once answered it is
replaced on every phone that got it (`retire_vehicle_question`) or cleared (`clear_vehicle_question`).

The paired app's instant notifications (`push.py`) hear about the same events, with their own choice
of events and their own limits, whether or not any phone is chosen here.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Final

from homeassistant.core import callback, CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.storage import Store
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
from ..const import DOMAIN
from ..texts import language_of
from .messages import compose, EVENT_TEST, identify_text, Money
from .push import ChargerPush
from .settings import EVENT_CHARGE_COMPLETE, EVENT_PLAN_STOPPED, EVENT_VEHICLE_IDENTIFY, NOTIFY_DOMAIN
from .unexpected_stop import ExpectationFacts, REASON_NOT_STARTED, UnexpectedStopDetector

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION: Final = 1
_STORE_KEY_PREFIX: Final = f"{DOMAIN}.notified_plan"

#: The same event for the same charger is not sent again within this long.
REPEAT_S: Final = 15 * 60.0
#: At most this many notifications per charger in an hour, whatever they are.
HOURLY_LIMIT: Final = 12
#: What a tap opens when no dashboard path is set: Home Assistant's default dashboard.
DEFAULT_URL: Final = "/"
#: `plan_stopped`'s reason when SpotNav gave up stopping a charger under a person's Stop.
REASON_CHARGER_IGNORES_STOP: Final = "charger_ignores_stop"
#: A new plan calculated this soon after a settings write for the charger is not announced.
QUIET_AFTER_WRITE_S: Final = 60.0
#: Plans installed this close to one another are one burst: only the last is judged. Long enough for a
#: person's few quick writes (seven seconds apart in the field) and for Auto's replan after a plug-in, whose
#: connector status settles for 5 s; short enough that the plan is still told while the person looks at it.
SETTLE_S: Final = 30.0
#: A burst that keeps going is judged this long after its first plan all the same.
SETTLE_MAX_S: Final = 120.0
#: After a plug-in with no new plan, the standing plan is told this long after it (or as its first window
#: opens, if sooner). A car's own cloud often reports its level only on its next poll, about half an hour
#: after a plug-in in the field, and the plan made from that reading is the one worth telling.
PLUG_IN_WAIT_S: Final = 45 * 60.0
#: Two period edges this close are the same edge.
_EDGE_S: Final = 60.0
#: A remainder may carry this much more energy than the told plan (rounding).
_ENERGY_SLACK_KWH: Final = 0.05
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


def plan_fingerprint(plan: Any) -> str | None:
    """What a person sees of a plan: its periods, the energy planned (to 0.1 kWh) and the target vehicle.

    Not the plan's Auto identity, which also changes with the settings revision and price identity
    without the plan itself changing.
    """
    if plan is None:
        return None
    periods = plan.periods or [{"start": plan.start, "end": plan.end}]
    energy = None if plan.energy_kwh is None else round(plan.energy_kwh, 1)
    return json.dumps(
        [[[item["start"], item["end"]] for item in periods], energy, plan.vehicle_id], separators=(",", ":")
    )


def _intervals(periods: list[Any]) -> list[tuple[datetime, datetime]] | None:
    """Sorted, merged `(start, end)` instants of `[start, end]` ISO pairs, or `None` when one is unreadable."""
    parsed: list[tuple[datetime, datetime]] = []
    for start, end in periods:
        begin, finish = dt_util.parse_datetime(start), dt_util.parse_datetime(end)
        if begin is None or finish is None:
            return None
        if finish > begin:
            parsed.append((begin, finish))
    merged: list[tuple[datetime, datetime]] = []
    for begin, finish in sorted(parsed):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def plan_is_remainder(plan: Any, told: str | None, now: datetime) -> bool:
    """Whether `plan` is only what is left of the plan last told about (its `plan_fingerprint`): the same car,
    the told periods from now on (or from the new plan's first start, if that is earlier), and no more energy.

    A window that ended, or a plan calculated again partway through, gives one; any period added, moved or
    left out of what was still ahead, more energy or another car is a real change.
    """
    fingerprint = plan_fingerprint(plan)
    if fingerprint is None or told is None:
        return False
    try:
        told_periods, told_energy, told_vehicle = json.loads(told)
    except (ValueError, TypeError):
        return False
    new_periods, new_energy, new_vehicle = json.loads(fingerprint)
    if new_vehicle != told_vehicle or (new_energy is None) != (told_energy is None):
        return False
    if new_energy is not None and new_energy > told_energy + _ENERGY_SLACK_KWH:
        return False
    new, before = _intervals(new_periods), _intervals(told_periods)
    if not new or before is None:
        return False
    cut = min(new[0][0], now)
    left = [(max(begin, cut), finish) for begin, finish in before if finish > cut]
    return len(left) == len(new) and all(
        abs((a[0] - b[0]).total_seconds()) <= _EDGE_S and abs((a[1] - b[1]).total_seconds()) <= _EDGE_S
        for a, b in zip(left, new, strict=True)
    )


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
        push: ChargerPush | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._name = name
        self._controller = controller
        self._store = store
        self._sessions = sessions
        self._currency = currency
        self._push = push
        self._tracker = ChargerEventTracker()
        self._stops = UnexpectedStopDetector()
        self._snapshot: AutoSnapshot | None = None
        self._completion_at: str | None = None
        self._ignores_stop = False
        self._baselined = False
        self._recheck: CALLBACK_TYPE | None = None
        self._recheck_at: datetime | None = None
        self._last_sent: dict[str, datetime] = {}
        self._sent_times: deque[datetime] = deque()
        self._unsubscribe: list[CALLBACK_TYPE] = []
        # The fingerprint of the plan last told about, kept across restarts (see `plan_fingerprint`).
        self._plan_store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}")
        self._notified_plan: str | None = None
        # The plans told so far and when the last was (`plan_notice`), kept with the told plan.
        self._notice_seq = 0
        self._notice_at: datetime | None = None
        # The car at the charger: the last known reading, `None` before one (or for a charger that cannot say).
        self._connected: bool | None = None
        # A plan burst being settled: when it began, when it is judged, and whether its last plan followed a
        # person's own write. `_owed_until`: after a plug-in, when the standing plan is told if none follows.
        self._burst_since: datetime | None = None
        self._burst_due: datetime | None = None
        self._burst_quiet = False
        self._owed_until: datetime | None = None
        self._plan_timer: CALLBACK_TYPE | None = None
        self._plan_timer_at: datetime | None = None
        # `plan_at_risk` held back while the car was away (its attributes), told after the plug-in if the departure
        # still cannot be met then; and whether it cannot be met now.
        self._at_risk_owed: dict[str, Any] | None = None
        self._at_risk_now = False

    async def async_load(self) -> None:
        """Read the last told plan's fingerprint, before `async_start`."""
        raw = await self._plan_store.async_load()
        fingerprint = raw.get("fingerprint") if isinstance(raw, dict) else None
        self._notified_plan = fingerprint if isinstance(fingerprint, str) else None
        seq = raw.get("notice_seq") if isinstance(raw, dict) else None
        self._notice_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else 0
        at = raw.get("notice_at") if isinstance(raw, dict) else None
        self._notice_at = dt_util.parse_datetime(at) if isinstance(at, str) and self._notice_seq else None

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: forget the plan it last told about."""
        await Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}").async_remove()

    @property
    def plan_notice(self) -> dict[str, Any]:
        """How many plans were told (`seq`, 0 before the first) and when the last was (`at`, ISO, or `None`)."""
        at = self._notice_at
        return {"seq": self._notice_seq, "at": None if at is None else at.isoformat()}

    def _remember_plan(self, fingerprint: str | None) -> None:
        if fingerprint is None or fingerprint == self._notified_plan:
            return
        self._notified_plan = fingerprint
        self._save_plan()

    def _save_plan(self) -> None:
        record: dict[str, Any] = {"fingerprint": self._notified_plan}
        if self._notice_seq:
            record["notice_seq"] = self._notice_seq
            record["notice_at"] = None if self._notice_at is None else self._notice_at.isoformat()
        self._hass.async_create_task(self._plan_store.async_save(record), eager_start=True)

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
        self._arm_plan_timer(None)

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
        self._observe_connection(now)
        # The car is known to be away: no outcome of a charge that cannot happen is told.
        away = self._connected is False
        events: list[tuple[str, dict[str, Any]]] = []
        facts = charger_facts(self._hass, controller, self._snapshot, settings)
        for kind, attributes in self._tracker.observe(facts):
            if kind == EVENT_PLAN_AT_RISK:
                # Told now, or after the plug-in if it still holds then; either way this one is settled.
                self._at_risk_owed = attributes if away else None
                if away:
                    _LOGGER.debug("SpotNav charger %s: plan_at_risk held back while no car is plugged in", self._entry_id)
                    continue
            if kind in _TRACKED:
                events.append((kind, attributes))
        self._at_risk_now = facts.at_risk
        if not facts.at_risk:
            self._at_risk_owed = None
        reason = self._stops.observe(self._expectation(now))
        if reason is not None and not (away and reason == REASON_NOT_STARTED):
            events.append((EVENT_PLAN_STOPPED, {"reason": reason}))
        ignores_stop = bool(controller.ignores_person_stop)
        if self._baselined and ignores_stop and not self._ignores_stop:
            events.append((EVENT_PLAN_STOPPED, {"reason": REASON_CHARGER_IGNORES_STOP}))
        self._ignores_stop = ignores_stop
        completion = controller.completion_record
        completion_at = None if completion is None else completion.get("at")
        if self._baselined and completion is not None and completion_at != self._completion_at:
            if away:
                # Nothing was charged for the plan with no car there: a window's end is no completion.
                _LOGGER.debug("SpotNav charger %s: charge_complete not told while no car is plugged in", self._entry_id)
            else:
                events.append((EVENT_CHARGE_COMPLETE, dict(completion)))
        self._completion_at = completion_at
        self._baselined = True
        self._arm_recheck(self._stops.due_at)
        for kind, attributes in events:
            self._maybe_send(kind, attributes, now)

    def _observe_connection(self, now: datetime) -> None:
        """Follow the car at the charger. An unknown reading keeps the last known one; a plug-in owes the
        plan that then stands, an unplug drops a plan not yet told."""
        adapter = self._controller.adapter
        if not adapter.reports_connection:
            self._connected = None
            return
        reading = adapter.vehicle_connected()
        if reading is None or reading == self._connected:
            return
        before, self._connected = self._connected, reading
        if not self._baselined or before is None:
            return
        if reading:
            plan = self._controller.plan
            until = now + timedelta(seconds=PLUG_IN_WAIT_S)
            if plan is not None:
                until = max(min(until, plan.start_time), now + timedelta(seconds=SETTLE_S))
            self._owed_until = until
        else:
            self._burst_since = self._burst_due = self._owed_until = None
            self._burst_quiet = False
        self._arm_plan_timer(self._plan_due())

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
        """Whether the car takes no current rightly: the plan charges to the car's own limit (the car ends
        it), or its target is above that limit and the car has reached it."""
        controller = self._controller
        plan = controller.plan
        if plan is not None and controller.charges_to_vehicle_limit():
            # A charge to the car's own limit ends when the car stops taking current: that is full.
            return True
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

    # ------------------------------------------------------------------ a new plan, settled

    def _plan_installed(self, now: datetime) -> None:
        """A plan was installed: start or extend its burst. Nothing is told while the car is away."""
        if self._connected is False:
            return
        written = self._store.last_settings_write(self._entry_id)
        self._burst_quiet = written is not None and 0 <= (now - written).total_seconds() < QUIET_AFTER_WRITE_S
        if self._burst_since is None:
            self._burst_since = now
        self._burst_due = min(
            now + timedelta(seconds=SETTLE_S), self._burst_since + timedelta(seconds=SETTLE_MAX_S)
        )
        # The plan after a plug-in is here: it, not the one that stood, is what is told.
        self._owed_until = None
        self._arm_plan_timer(self._plan_due())

    def _plan_due(self) -> datetime | None:
        return self._burst_due if self._burst_due is not None else self._owed_until

    def _arm_plan_timer(self, due: datetime | None) -> None:
        if due == self._plan_timer_at:
            return
        if self._plan_timer is not None:
            self._plan_timer()
        self._plan_timer = None
        self._plan_timer_at = due
        if due is None:
            return

        @callback
        def _fire(_now: datetime) -> None:
            self._plan_timer = None
            self._plan_timer_at = None
            self._judge_plan(dt_util.utcnow())

        self._plan_timer = async_track_point_in_utc_time(self._hass, _fire, due)

    def _judge_plan(self, now: datetime) -> None:
        """The burst is over (or a plug-in's wait ran out): tell the plan that stands if it is news."""
        due = self._plan_due()
        if due is None or now < due:
            self._arm_plan_timer(due)
            return
        quiet = self._burst_due is not None and self._burst_quiet
        self._burst_since = self._burst_due = self._owed_until = None
        self._burst_quiet = False
        self._tell_owed_at_risk(now)
        plan = self._controller.plan
        fingerprint = plan_fingerprint(plan)
        if fingerprint is None or self._connected is False:
            return
        if quiet:
            # A person just changed this charger's settings and sees the plan that followed: not told, but
            # known, so the same plan after a restart is not told either.
            self._remember_plan(fingerprint)
            return
        if fingerprint == self._notified_plan:
            # The same plan as the one last told about (Home Assistant restarted, a burst came back to it, or
            # it was calculated again to the same result): nothing new for a person.
            return
        if plan_is_remainder(plan, self._notified_plan, now):
            # What is left of the told plan (a window ended): told already. The told plan stays the measure.
            _LOGGER.debug("SpotNav charger %s: the rest of the told plan is not told again", self._entry_id)
            return
        # News: counted and known whether or not a phone or the app hears of it now (the app reads the count).
        self._notice_seq += 1
        self._notice_at = now
        self._notified_plan = fingerprint
        self._save_plan()
        self._send(EVENT_PLAN_INSTALLED, {}, now, fingerprint)

    def _tell_owed_at_risk(self, now: datetime) -> None:
        """The plan made for the car after its plug-in is in: a departure that still cannot be met is told now."""
        if self._at_risk_owed is None or self._connected is False:
            return
        owed, self._at_risk_owed = self._at_risk_owed, None
        if self._at_risk_now:
            self._send(EVENT_PLAN_AT_RISK, owed, now, None)

    # ------------------------------------------------------------------ sending

    def _maybe_send(self, event: str, attributes: dict[str, Any], now: datetime) -> None:
        if event == EVENT_PLAN_INSTALLED:
            self._plan_installed(now)
            return
        self._send(event, attributes, now, None)

    def _send(self, event: str, attributes: dict[str, Any], now: datetime, fingerprint: str | None) -> None:
        if self._push is not None:
            # The paired app's wake-up, with its own events and limits, whatever phones are chosen here.
            if self._push.async_event(event, now):
                self._remember_plan(fingerprint)
        notifications = self._store.settings(self._entry_id).notifications
        if not notifications.wants(event):
            return
        # Giving up on a charger under a person's Stop is its own kind of trouble: a missed window told a
        # moment before does not silence it.
        repeat_key = (
            REASON_CHARGER_IGNORES_STOP
            if event == EVENT_PLAN_STOPPED and attributes.get("reason") == REASON_CHARGER_IGNORES_STOP
            else event
        )
        last = self._last_sent.get(repeat_key)
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
        self._last_sent[repeat_key] = now
        self._sent_times.append(now)
        self._remember_plan(fingerprint)
        title, message = compose(
            event, self._name(), self._facts_for(event, attributes), language_of(self._hass.config.language)
        )
        payload = self._payload(event, title, message, notifications.url)
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)

    def _payload(self, event: str, title: str, message: str, url: str | None) -> dict[str, Any]:
        url = url or DEFAULT_URL
        return {
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

    # ------------------------------------------------------------------ which car is plugged in?

    def _within_hourly_limit(self, now: datetime) -> bool:
        while self._sent_times and (now - self._sent_times[0]).total_seconds() >= 3600:
            self._sent_times.popleft()
        return len(self._sent_times) < HOURLY_LIMIT

    def _identify_data(self, tag: str) -> dict[str, Any]:
        url = self._store.settings(self._entry_id).notifications.url or DEFAULT_URL
        return {"url": url, "clickAction": url, "tag": tag, "group": "spotnav", "channel": "SpotNav"}

    def ask_vehicle(self, tag: str, choices: list[tuple[str, str]], *, open_button: bool) -> tuple[str, ...]:
        """Ask the chosen phones which car is plugged in: one button per `(action id, car name)`, and an
        "Open SpotNav" button when not every car fits. Answers the phones it went to.

        The text names no plate, place or person: it passes Google's and Apple's push services.
        """
        now = dt_util.utcnow()
        if self._push is not None:
            # Every new question wakes the paired app, which asks on its own (`ChargerPush.async_question`).
            self._push.async_question(now)
        notifications = self._store.settings(self._entry_id).notifications
        if not notifications.wants(EVENT_VEHICLE_IDENTIFY):
            return ()
        if not self._within_hourly_limit(now):
            _LOGGER.debug("SpotNav charger %s: hourly notification limit reached", self._entry_id)
            return ()
        targets = tuple(
            target for target in notifications.targets if self._hass.services.has_service(NOTIFY_DOMAIN, target)
        )
        if not targets:
            return ()
        self._sent_times.append(now)
        language = language_of(self._hass.config.language)
        data = self._identify_data(tag)
        actions: list[dict[str, Any]] = [{"action": action, "title": title} for action, title in choices]
        if open_button:
            actions.append({"action": "URI", "title": identify_text(language, "open"), "uri": data["url"]})
        title, message = compose(EVENT_VEHICLE_IDENTIFY, self._name(), {}, language)
        payload = {"title": title, "message": message, "data": {**data, "actions": actions}}
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)
        return targets

    def vehicle_question_settled(self) -> None:
        """The open question ended, however it ended: wake the paired app so it takes its own question down."""
        if self._push is not None:
            self._push.async_question_settled(dt_util.utcnow())

    def retire_vehicle_question(self, tag: str, targets: tuple[str, ...], wording: str, vehicle: str) -> None:
        """Replace the question on every phone it went to: `chosen`, `recognised` or `kept`, without buttons and
        silently: the same tag replaces it, `alert_once` keeps Android quiet and the passive interruption level
        (iOS 15 and later) delivers it without a sound or a banner."""
        if not targets:
            return
        language = language_of(self._hass.config.language)
        title, _ = compose(EVENT_VEHICLE_IDENTIFY, self._name(), {}, language)
        payload = {
            "title": title,
            "message": identify_text(language, wording, vehicle),
            "data": {**self._identify_data(tag), "alert_once": True, "push": {"interruption-level": "passive"}},
        }
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)

    def clear_vehicle_question(self, tag: str, targets: tuple[str, ...]) -> None:
        """Take the question off every phone it went to (the car was unplugged, or nobody answered in time)."""
        payload = {"message": "clear_notification", "data": {"tag": tag}}
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)

    async def async_send_test(self) -> list[str]:
        """A test message to the chosen phones that exist now (`spotnav.send_test_notification`), outside
        the limits: the services it went to."""
        notifications = self._store.settings(self._entry_id).notifications
        targets = [
            target
            for target in notifications.targets
            if self._hass.services.has_service(NOTIFY_DOMAIN, target)
        ]
        if not targets:
            return []
        title, message = compose(EVENT_TEST, self._name(), {}, language_of(self._hass.config.language))
        payload = self._payload(EVENT_TEST, title, message, notifications.url)
        sent = []
        for target in targets:
            if await self._async_send(target, payload):
                sent.append(target)
        return sent

    async def _async_send(self, target: str, payload: dict[str, Any]) -> bool:
        try:
            await self._hass.services.async_call(NOTIFY_DOMAIN, target, payload, blocking=True)
        except Exception as err:  # noqa: BLE001 - a phone that cannot be reached must not stop the charger
            _LOGGER.warning("SpotNav could not notify %s: %s", target, type(err).__name__)
            return False
        return True

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
