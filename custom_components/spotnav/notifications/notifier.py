"""One charger's notifier: what happened, sent to the phones that follow it through `notify.<service>`.

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
* not when it follows a person's own settings write (`QUIET_AFTER_WRITE_S`): they see it, and it counts as
  this plug-in's told plan below;
* not when a plan was already told for this plug-in, unless it rests on new prices: it reaches a later day (the
  local date its last window ends) than the told plan did, e.g. tomorrow's prices came at midday and the plan now
  reaches into tomorrow. Load balancing moving the windows in the night, or the rest laid out again after a
  window, is not news. Such a plan is not counted, but it becomes the told plan's measure (fingerprint and
  remainder), so its own remainders and a restart do not tell it either. The plug-in is the car connected since
  the last known unplug (or since start-up with the car connected), kept across restarts with the told plan; an
  unplug ends it, so the next plug-in's first plan is told. For a charger that cannot say whether a car is
  connected it lasts while a plan stands: once no plan stands (dropped, ended, or cleared by a change of
  strategy) the next plan is told.

Each plan told is counted (`plan_notice`, kept across restarts with the told plan), whatever phones are chosen
or limits allow: the dashboard carries the count, and the paired app's own check tells a new plan when it
rises instead of deciding by itself.

No outcome of a charge that cannot happen is told while the car is known to be away (the same reading as
above: an unknown one keeps the last known, a charger that cannot say tells as before), on the phones and in the
app's wake-up alike: no `plan_at_risk`, no `plan_stopped` for a window that did not start and no `charge_complete`.
A `plan_at_risk` held back is told after the plug-in, when the plan made for the car is judged, if the departure
still cannot be met then.

A restart of Home Assistant keeps, with the told plan, what a person would otherwise get twice, lose or get
wrong: a new plan still settling or owed after a plug-in (judged again shortly after the start: the plan restored
at the start is no event), a `plan_at_risk` already told for the coming departure (the first Auto snapshot after
a start is no news), and the open window's `plan_stopped` judgement (whether it charged, so the reason is right,
and whether its trouble was told). A `plan_at_risk` held back while the car is away is not kept: Auto's first
snapshot after the start holds it back again. The rate limits below are not kept: a restart lets at most one more
notification of a real event through, replacing the earlier one on the phone (the same tag).

Every event first sets a baseline: loading the integration is not an event. Sending is rate-limited
per charger: the same event is not sent again within `REPEAT_S`, and no more than `HOURLY_LIMIT`
notifications go out in an hour. Each notification carries a `tag` (one per charger and event), so a
phone replaces an older one of the same kind instead of stacking them, and a `url` a tap opens.

The question of which car is plugged in (`vehicle_identify`) is asked by the charger's vehicle identification
(`vehicles/identification.py`) through `ask_vehicle`: one button per car with the action ids it gives, the tag it
gives, counted against `HOURLY_LIMIT` but not `REPEAT_S` (there is at most one per plug-in). Once answered it is
replaced on every phone that got it (`retire_vehicle_question`) or cleared (`clear_vehicle_question`).

A planned charge that does not fit under the ceiling by its departure, where the plan asks (`does_not_fit`, the
block of `site/does_not_fit.py` from the site on each recompute) is told as `plan_at_risk` is, under its choice and in
its place: the same tag, so it replaces one told before, and no `plan_at_risk` while it is on the phones or waits for
the morning. It says which ceiling and what is short ("The car will not be ready by 07:00 under the 6.0 kW import
target – 4 kWh short.", or "under the month's peak of 7.5 kW (import target 6.0 kW)") and carries the person's choices for this departure as buttons: "Take what fits (≈ 12 kWh)"
(`SiteCapacityController.async_take_what_fits`), "Later departure (14:00)" where a later whole hour fits under the
same ceiling within the prices held (`SiteCapacityController.async_later_departure`: this departure alone moved to
the time told; without one there is no button, the departure's editor is the way, which a tap on the notice opens
with the dashboard) and "Allow the peak (≈ 12 kW, ≈ 105 kr)" (the one-period release,
`SiteCapacityController.async_release_import_target`). A press is the person's choice; one for a departure that is no
longer the plan's is refused.

* **Once per departure**, never again for it (also not when it grows, comes back or Home Assistant restarts: the
  departure told is kept with the told plan).
* **Not at night** (`QUIET_FROM` to `QUIET_UNTIL`, 22:00–07:00 local): a person decides while awake. One that arises
  in the night is told at 07:00 if it still stands and its departure is after 07:00; else never, and the person's
  lasting `if_not_ready` decides as it would unanswered.
* **Taken off the phones** once it no longer stands: the charge fits again, a choice was made (here, in the card or
  in the app), another departure is planned, or the target is gone.

The block is any flexible load's (plan step 3c's evening notice reuses it): `chosen` `None` and choices to offer.
Where more than this car will not be ready in time (another car, a spa's bath, a sauna's session), the site's combined
notice (`site_notice.py`) tells it instead: this charger's own notice is taken off, its departure counts as told
(`mark_fit_told`) and its `plan_at_risk` is held back behind the site's as behind its own (`hold_for_site`).

No notice about the plan is told while a person's Stop holds (the core's person intent, `held_off_by_person`): no
`plan_installed` (nor counted), no `plan_at_risk` (nor the import target's in its place) and no `plan_stopped`, but
for `charger_ignores_stop`, which is about the person's Stop itself. A departure at risk that arose under the Stop is
told once when the person resumes the plan (Resume) if it still cannot be met; a person's Start or an unplug drops
it. The plan still shows in the
card, under the paused line. Kept as they are: `charge_started`, `plugged_in`, `unplugged`, `charge_complete` and the
question of which car is plugged in. Once the Stop ends (Start, Resume, or the unplug) the plan's notices come back.

Who hears it is the phones' (`phone_store.py`): the phones that are on, want the event and follow this charger, each by
one route. A phone whose SpotNav app can be woken and shows the event is woken (`push.py`, its own limits); the rest
get it from their Companion service here. Never both.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, time, timedelta
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
from ..planning.flexible_load import CHOICE_ACCEPT_PEAK, CHOICE_LATER, CHOICE_TAKES_WHAT_FITS
from ..sessions.store import SessionStore
from ..vehicles.guest import is_guest
from ..const import DOMAIN
from ..texts import language_of
from .messages import compose, does_not_fit_choice_text, does_not_fit_text, EVENT_TEST, identify_text, Money
from .phone_store import phone_store
from .phones import ROUTE_COMPANION, route_of
from .push import ChargerPush
from .settings import EVENT_CHARGE_COMPLETE, EVENT_PLAN_STOPPED, EVENT_VEHICLE_IDENTIFY, NOTIFY_DOMAIN
from .unexpected_stop import ExpectationFacts, REASON_NOT_STARTED, UnexpectedStopDetector

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION: Final = 1
#: 2: the record also keeps a plan's pending judgement, the `plan_at_risk` told and the open window's `plan_stopped`
#: judgement; 3: the "does not fit" notice's departure told and its phones. Each optional, so an older record reads as
#: it is.
_STORE_MINOR_VERSION: Final = 3
_STORE_KEY_PREFIX: Final = f"{DOMAIN}.notified_plan"

#: The same event for the same charger is not sent again within this long.
REPEAT_S: Final = 15 * 60.0
#: The action id prefix of the "does not fit" notice's "Allow the peak" button (the import target set aside for this
#: charge until its departure).
TARGET_RELEASE_ACTION: Final = "SPOTNAV_IMPORT_TARGET_RELEASE_"
#: The action id prefix of the "does not fit" notice's "Take what fits" button (for this departure only).
TAKE_WHAT_FITS_ACTION: Final = "SPOTNAV_TAKE_WHAT_FITS_"
#: The action id prefix of the "does not fit" notice's "Later departure (14:00)" button (this departure moved to the
#: time told, for this departure only): `<prefix><charger>_<departure epoch>_<later epoch>`.
LATER_DEPARTURE_ACTION: Final = "SPOTNAV_LATER_DEPARTURE_"
#: No "does not fit" notice is sent from this local time ...
QUIET_FROM: Final = time(22, 0)
#: ... to this one: one that arises in the night is told at this time, if it still stands and its departure is later.
QUIET_UNTIL: Final = time(7, 0)
#: The event Home Assistant's companion app fires when a notification's button is pressed.
NOTIFICATION_ACTION_EVENT: Final = "mobile_app_notification_action"
#: At most this many notifications per charger in an hour, whatever they are (a limit of its own, apart from the
#: wake-ups' `push.HOURLY_LIMIT`, though both are 12).
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
#: A completed charge's session is the open one, or one that closed this recently (`event.py` uses the same rule).
SESSION_RECENT_S: Final = 600.0

#: The notices about the plan: none is told while a person's Stop holds (`plan_stopped`'s `charger_ignores_stop`
#: excepted, which is about the Stop itself).
_PLAN_NOTICES: Final = (EVENT_PLAN_INSTALLED, EVENT_PLAN_AT_RISK, EVENT_PLAN_STOPPED)

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


def is_quiet(moment: datetime) -> bool:
    """Whether `moment` is in the night's quiet hours (`QUIET_FROM` to `QUIET_UNTIL`, local)."""
    local = dt_util.as_local(moment).time()
    return local >= QUIET_FROM or local < QUIET_UNTIL


def quiet_end(moment: datetime) -> datetime:
    """The first `QUIET_UNTIL` (local wall clock, also across a DST change) after `moment`."""
    local = dt_util.as_local(moment)
    end = local.replace(hour=QUIET_UNTIL.hour, minute=QUIET_UNTIL.minute, second=0, microsecond=0)
    if end <= local:
        end = (local + timedelta(days=1)).replace(
            hour=QUIET_UNTIL.hour, minute=QUIET_UNTIL.minute, second=0, microsecond=0
        )
    return dt_util.as_utc(end)


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


def _last_day(plan: Any) -> str | None:
    """The local date the plan's last window ends (ISO), or `None` when it cannot be read."""
    try:
        end = max(finish for _begin, finish in plan.windows)
    except (ValueError, TypeError):
        return None
    return dt_util.as_local(end).date().isoformat()


def _departure_after(departure: Any, now: datetime) -> datetime | None:
    """The first departure at local `HH:MM` after `now`, or `None` when it cannot be read."""
    try:
        hour, minute = (int(part) for part in str(departure).split(":"))
        local = dt_util.as_local(now)
        moment = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (TypeError, ValueError):
        return None
    if moment <= local:
        moment += timedelta(days=1)
    return dt_util.as_utc(moment)


def _epoch(moment: datetime | None) -> int:
    """A departure as the button's action id carries it: whole seconds since the epoch (0 when unknown)."""
    return 0 if moment is None else int(moment.timestamp())


def _instant(raw: Any) -> datetime | None:
    parsed = dt_util.parse_datetime(raw) if isinstance(raw, str) else None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


class _NotifierStore(Store[dict[str, Any]]):
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        # Minor versions only add optional keys: an older record reads as it is.
        return old_data


def _later_day(day: str | None, told: str | None) -> bool:
    """Whether a plan reaching `day` reaches past the told plan's `told` day: it rests on new prices."""
    return day is not None and (told is None or day > told)


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
        self._plan_store: Store[dict[str, Any]] = _NotifierStore(
            hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}", minor_version=_STORE_MINOR_VERSION
        )
        self._notified_plan: str | None = None
        # The plans told so far and when the last was (`plan_notice`), kept with the told plan.
        self._notice_seq = 0
        self._notice_at: datetime | None = None
        # Whether a plan was told for the current plug-in, and the last day the told plan reaches (ISO local date):
        # a later plan in the same plug-in is told only when it reaches a later day. Kept with the told plan.
        self._plug_in_told = False
        self._plan_day: str | None = None
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
        # A `plan_at_risk` that arose under a person's Stop (its attributes): told once when the person ends the Stop
        # (Start, Resume) if the departure still cannot be met then; an unplug drops it. And whether a Stop held at
        # the last observation.
        self._at_risk_under_stop: dict[str, Any] | None = None
        self._held_before = False
        # The `plan_at_risk` told for the coming departure (its departure time and that departure's instant): one
        # seen again after a restart, before that departure, is not news.
        self._at_risk_told: dict[str, Any] | None = None
        # The open window's `plan_stopped` judgement as last stored (`UnexpectedStopDetector.window_state`).
        self._stop_state: dict[str, Any] | None = None
        # The "does not fit" notice (`does_not_fit`): the departure last told (never told again; kept with the told
        # plan), the phones it is on now (kept too, to take it off after a restart), the block that asks now, whether
        # it waits for the morning, the timer that tells it then and who to ask for the block again at that time.
        self._fit_told: str | None = None
        self._fit_phones: tuple[str, ...] = ()
        self._fit_asking: dict[str, Any] | None = None
        self._fit_quiet = False
        self._fit_timer: CALLBACK_TYPE | None = None
        self._fit_timer_at: datetime | None = None
        self._fit_recheck: Callable[[], None] | None = None
        #: Shut down (`async_shutdown`): no timer is armed any more.
        self._closed = False
        # A `plan_at_risk` held back behind the "does not fit" notice.
        self._at_risk_behind_target: dict[str, Any] | None = None
        # The site's combined "not ready in time" notice (`site_notice.py`) holds this car's departure on the phones,
        # or waits for the morning with it: `plan_at_risk` is held back behind it as behind this charger's own.
        self._site_holds = False

    async def async_load(self) -> None:
        """Read the last told plan's fingerprint, before `async_start`."""
        raw = await self._plan_store.async_load()
        fingerprint = raw.get("fingerprint") if isinstance(raw, dict) else None
        self._notified_plan = fingerprint if isinstance(fingerprint, str) else None
        seq = raw.get("notice_seq") if isinstance(raw, dict) else None
        self._notice_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else 0
        at = raw.get("notice_at") if isinstance(raw, dict) else None
        self._notice_at = dt_util.parse_datetime(at) if isinstance(at, str) and self._notice_seq else None
        told = raw.get("plug_in_told") if isinstance(raw, dict) else None
        self._plug_in_told = told is True
        day = raw.get("plan_day") if isinstance(raw, dict) else None
        self._plan_day = day if isinstance(day, str) else None
        if isinstance(raw, dict):
            self._load_pending(raw)

    def _load_pending(self, raw: dict[str, Any]) -> None:
        """What was under way at a restart. A plan judgement still due is judged again no sooner than `SETTLE_S`
        after the start, so the plans Auto calculates at the start settle first."""
        now = dt_util.utcnow()
        floor = now + timedelta(seconds=SETTLE_S)
        pending = raw.get("pending")
        if isinstance(pending, dict):
            burst_due = _instant(pending.get("burst_due"))
            owed_until = _instant(pending.get("owed_until"))
            if burst_due is not None:
                self._burst_since = now
                self._burst_due = max(burst_due, floor)
                self._burst_quiet = pending.get("burst_quiet") is True
            if owed_until is not None:
                self._owed_until = max(owed_until, floor)
        told = raw.get("at_risk_told")
        if isinstance(told, dict) and _instant(told.get("until")) is not None:
            self._at_risk_told = dict(told)
        self._stops.restore(raw.get("stop"))
        self._stop_state = self._stops.window_state
        fit = raw.get("does_not_fit")
        if isinstance(fit, dict) and isinstance(fit.get("departure"), str):
            self._fit_told = fit["departure"]
            phones = fit.get("phones")
            if isinstance(phones, list):
                self._fit_phones = tuple(phone for phone in phones if isinstance(phone, str))

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: forget the plan it last told about."""
        await Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}").async_remove()

    @property
    def outside_pauses(self) -> dict[str, Any]:
        """The pauses of the plan's charge SpotNav did not command (`outside_pause.py`), for the diagnostics."""
        return self._stops.pauses.as_dict(dt_util.utcnow())

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
        if self._plug_in_told:
            record["plug_in_told"] = True
        if self._plan_day is not None:
            record["plan_day"] = self._plan_day
        pending: dict[str, Any] = {}
        if self._burst_due is not None:
            pending["burst_due"] = self._burst_due.isoformat()
            pending["burst_quiet"] = self._burst_quiet
        if self._owed_until is not None:
            pending["owed_until"] = self._owed_until.isoformat()
        if pending:
            record["pending"] = pending
        if self._at_risk_told is not None:
            record["at_risk_told"] = self._at_risk_told
        if self._stop_state is not None:
            record["stop"] = self._stop_state
        if self._fit_told is not None:
            record["does_not_fit"] = {"departure": self._fit_told, "phones": list(self._fit_phones)}
        self._hass.async_create_task(self._plan_store.async_save(record), eager_start=True)

    @callback
    def async_start(self, preview: Any = None) -> None:
        """Take the baseline and start watching."""
        self._observe()
        self._unsubscribe.append(self._controller.add_listener(self._observe))
        self._unsubscribe.append(self._controller.add_charge_state_listener(self._observe))
        self._unsubscribe.append(self._hass.bus.async_listen(NOTIFICATION_ACTION_EVENT, self._on_action))
        if preview is not None:
            self._unsubscribe.append(preview.add_listener(self._on_snapshot))
        # A plan judgement under way at a restart.
        self._arm_plan_timer(self._plan_due())

    @callback
    def async_shutdown(self) -> None:
        for remove in self._unsubscribe:
            remove()
        self._unsubscribe.clear()
        self._cancel_recheck()
        self._arm_plan_timer(None)
        self._closed = True
        self._fit_recheck = None
        self._arm_fit_timer(None)

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
        at_risk_told = self._at_risk_told
        held = self._person_stop_holds()
        for kind, attributes in self._tracker.observe(facts):
            if kind == EVENT_PLAN_AT_RISK:
                if held:
                    # A person's Stop holds: nothing is told about the plan; told once the person ends it, if it holds.
                    _LOGGER.debug("SpotNav charger %s: plan_at_risk not told under a person's Stop", self._entry_id)
                    self._at_risk_under_stop = attributes
                    continue
                if self._fit_holds_back():
                    # The "does not fit" notice is told in its place (`does_not_fit`); kept, and told once that
                    # notice is taken down while the plan still cannot be met.
                    _LOGGER.debug("SpotNav charger %s: plan_at_risk told as the import target's", self._entry_id)
                    self._at_risk_behind_target = attributes
                    continue
                if self._at_risk_told_for(attributes, now):
                    # Told before a restart for this departure: the first snapshot after the start is no news.
                    _LOGGER.debug("SpotNav charger %s: plan_at_risk told already for this departure", self._entry_id)
                    continue
                # Told now, or after the plug-in if it still holds then; either way this one is settled.
                self._at_risk_owed = attributes if away else None
                if away:
                    _LOGGER.debug("SpotNav charger %s: plan_at_risk held back while no car is plugged in", self._entry_id)
                    continue
                self._mark_at_risk_told(attributes, now)
            if kind in _TRACKED:
                events.append((kind, attributes))
        self._at_risk_now = facts.at_risk
        if not facts.at_risk and self._snapshot is not None:
            # Read from a snapshot (before the first one after a start, nothing is known either way).
            self._at_risk_owed = None
            self._at_risk_told = None
            self._at_risk_under_stop = None
        if self._held_before and not held and self._at_risk_under_stop is not None:
            # The person ended their Stop (an unplug dropped what arose under it): a departure still at risk is told,
            # but only when Auto has the charger back with its plan (Resume). A person's Start drops the plan and
            # charges by hand: nothing about the plan is news then.
            owed, self._at_risk_under_stop = self._at_risk_under_stop, None
            auto_back = controller.plan is not None and not controller.session_runner.session.person_started
            if (
                auto_back
                and facts.at_risk
                and not away
                and not self._fit_holds_back()
                and not self._at_risk_told_for(owed, now)
            ):
                self._mark_at_risk_told(owed, now)
                events.append((EVENT_PLAN_AT_RISK, owed))
        self._held_before = held
        reason = self._stops.observe(self._expectation(now))
        for pause in self._stops.take_pause_ends():
            # Not told: an outside pause ends by itself and the plan is kept; the log and the diagnostics say it.
            _LOGGER.log(
                logging.INFO if pause.outcome in ("resumed", "plan_moved_on") else logging.DEBUG,
                "SpotNav charger %s: the charge was paused by something outside SpotNav for %.1f min (%s, %s);"
                " the plan is kept",
                self._entry_id, pause.seconds / 60.0, pause.how, pause.outcome,
            )
        stop_state = self._stops.window_state
        if stop_state != self._stop_state or self._at_risk_told != at_risk_told:
            self._stop_state = stop_state
            self._save_plan()
        if reason is not None and not (away and reason == REASON_NOT_STARTED) and not held:
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
            if self._controller.plan is None:
                # A charger that cannot say: no plan standing ends what is taken as the plug-in.
                self._end_plug_in()
            return
        reading = adapter.vehicle_connected()
        if reading is False:
            # Unplugged (also when found so at start-up): the next plug-in's first plan is told, and what arose under
            # a person's Stop is over with it.
            self._end_plug_in()
            self._at_risk_under_stop = None
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
        self._save_plan()
        if reading and self._fit_asking is not None:
            # A car plugged in whose departure does not fit: told now (by day), not first at the site's next pass.
            if self._fit_recheck is not None:
                self._fit_recheck()
            else:
                self.does_not_fit(self._fit_asking)

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
        """A plan was installed: start or extend its burst. Nothing is told while the car is away, nor while a
        person's Stop holds."""
        if self._connected is False or self._person_stop_holds():
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
        self._save_plan()

    def _end_plug_in(self) -> None:
        if self._plug_in_told:
            self._plug_in_told = False
            self._save_plan()

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
        self._save_plan()
        if self._person_stop_holds():
            # Nothing about the plan is told (nor counted) under a person's Stop; an owed `plan_at_risk` is dropped.
            self._at_risk_owed = None
            return
        self._tell_owed_at_risk(now)
        plan = self._controller.plan
        fingerprint = plan_fingerprint(plan)
        if fingerprint is None or self._connected is False:
            return
        day = _last_day(plan)
        if quiet:
            # A person just changed this charger's settings and sees the plan that followed: not told, but
            # known, so the same plan after a restart is not told either. The person has seen this plug-in's
            # plan, so later replans in it on the same prices are not told.
            self._plug_in_told = True
            self._plan_day = day
            self._notified_plan = fingerprint
            self._save_plan()
            return
        if fingerprint == self._notified_plan:
            # The same plan as the one last told about (Home Assistant restarted, a burst came back to it, or
            # it was calculated again to the same result): nothing new for a person.
            return
        if plan_is_remainder(plan, self._notified_plan, now):
            # What is left of the told plan (a window ended): told already. The told plan stays the measure.
            _LOGGER.debug("SpotNav charger %s: the rest of the told plan is not told again", self._entry_id)
            return
        if self._plug_in_told and not _later_day(day, self._plan_day):
            # Calculated again in the same plug-in on the same prices (load balancing moved the windows): the
            # card shows it, nobody is woken. It is the measure from now, but not counted.
            _LOGGER.debug("SpotNav charger %s: a replan in the same plug-in is not told", self._entry_id)
            self._remember_plan(fingerprint)
            return
        # News: counted and known whether or not a phone or the app hears of it now (the app reads the count).
        self._notice_seq += 1
        self._notice_at = now
        self._notified_plan = fingerprint
        self._plug_in_told = True
        self._plan_day = day
        self._save_plan()
        self._send(EVENT_PLAN_INSTALLED, {}, now, fingerprint)

    def _tell_owed_at_risk(self, now: datetime) -> None:
        """The plan made for the car after its plug-in is in: a departure that still cannot be met is told now."""
        if self._at_risk_owed is None or self._connected is False:
            return
        owed, self._at_risk_owed = self._at_risk_owed, None
        if self._at_risk_now:
            self._mark_at_risk_told(owed, now)
            self._save_plan()
            self._send(EVENT_PLAN_AT_RISK, owed, now, None)

    def _mark_at_risk_told(self, attributes: dict[str, Any], now: datetime) -> None:
        until = _departure_after(attributes.get("departure_time"), now)
        self._at_risk_told = (
            None if until is None else {"departure_time": attributes.get("departure_time"), "until": until.isoformat()}
        )

    def _at_risk_told_for(self, attributes: dict[str, Any], now: datetime) -> bool:
        """Whether this `plan_at_risk` was told already: the same departure time, and that departure still ahead."""
        told = self._at_risk_told
        if told is None or told.get("departure_time") != attributes.get("departure_time"):
            return False
        until = _instant(told.get("until"))
        return until is not None and now < until

    # ------------------------------------------------------------------ sending

    def _maybe_send(self, event: str, attributes: dict[str, Any], now: datetime) -> None:
        if event == EVENT_PLAN_INSTALLED:
            self._plan_installed(now)
            return
        self._send(event, attributes, now, None)

    def _person_stop_holds(self) -> bool:
        """Whether a person's Stop holds: no notice about the plan is told then."""
        return bool(self._controller.held_off_by_person)

    def _send(self, event: str, attributes: dict[str, Any], now: datetime, fingerprint: str | None) -> None:
        if (
            event in _PLAN_NOTICES
            and attributes.get("reason") != REASON_CHARGER_IGNORES_STOP
            and self._person_stop_holds()
        ):
            _LOGGER.debug("SpotNav charger %s: %s not told under a person's Stop", self._entry_id, event)
            return
        if self._push is not None:
            # The paired app's wake-up, with its own events and limits, whatever phones are chosen here.
            if self._push.async_event(event, now):
                self._remember_plan(fingerprint)
        targets = self._companion(event)
        if not targets:
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
        self._last_sent[repeat_key] = now
        self._sent_times.append(now)
        self._remember_plan(fingerprint)
        title, message = compose(
            event, self._name(), self._facts_for(event, attributes), language_of(self._hass.config.language)
        )
        payload = self._payload(event, title, message, self._url())
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)

    def _companion(self, event: str) -> tuple[str, ...]:
        """The Companion services of the phones that hear `event` from this charger by that route (`phone_store.py`):
        a phone whose SpotNav app can show it is woken instead, never both."""
        phones = phone_store(self._hass)
        return () if phones is None else phones.recipients(event, charger=self._entry_id)[0]

    def _has_service(self, service: str) -> bool:
        return self._hass.services.has_service(NOTIFY_DOMAIN, service)

    def _url(self) -> str | None:
        """What a tap opens: the dashboard the charger's settings were saved from."""
        return self._store.settings(self._entry_id).notifications.url

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

    # ------------------------------------------------------------------ a charge that does not fit

    def _fit_holds_back(self) -> bool:
        """Whether `plan_at_risk` is held back: the "does not fit" notice (this charger's, or the site's combined one
        with this car in it) is on the phones, or waits for the morning."""
        return bool(self._fit_phones) or self._fit_quiet or self._site_holds

    # -- what the site's combined notice (`site_notice.py`) asks of the charger

    def fit_may_tell(self) -> bool:
        """Whether a "does not fit" for this car may be told now: a car is (or may be) there and no person's Stop
        holds."""
        return self._connected is not False and not self._person_stop_holds()

    def hold_for_site(self, holds: bool) -> None:
        """The site's combined notice holds this car's departure (on the phones, or waiting for the morning), or no
        longer does: a `plan_at_risk` held back behind it is told then, if the plan still cannot be met for reasons of
        its own and this charger's own notice does not hold it."""
        if holds == self._site_holds:
            return
        self._site_holds = holds
        if holds or self._fit_phones or self._fit_quiet:
            return
        behind, self._at_risk_behind_target = self._at_risk_behind_target, None
        if behind is not None and self._at_risk_now:
            now = dt_util.utcnow()
            self._mark_at_risk_told(behind, now)
            self._maybe_send(EVENT_PLAN_AT_RISK, behind, now)

    def mark_fit_told(self, departure: str) -> None:
        """The site's combined notice told this departure: never told again on its own (kept with the told plan)."""
        if self._fit_told != departure:
            self._fit_told = departure
            self._save_plan()

    def fit_targets(self, now: datetime) -> tuple[str, ...]:
        """The phones that hear this charger's `plan_at_risk`, as the "does not fit" notice reaches them (chosen, the
        service there, within the hourly limit)."""
        if not self._within_hourly_limit(now):
            return ()
        return self._companion(EVENT_PLAN_AT_RISK)

    def fit_sent(self, now: datetime) -> None:
        """The site's combined notice went to this charger's phones: counted as its own would be."""
        self._sent_times.append(now)
        self._last_sent[EVENT_PLAN_AT_RISK] = now

    @property
    def push(self) -> ChargerPush | None:
        """The paired app's wake-up for this charger."""
        return self._push

    @property
    def notifications_url(self) -> str:
        """What a tap opens: the dashboard chosen for this charger, else the default one."""
        return self._url() or DEFAULT_URL

    @property
    def name(self) -> str:
        return self._name()

    def does_not_fit(self, block: dict[str, Any] | None, recheck: Callable[[], None] | None = None) -> str | None:
        """The charge's "does not fit" block (`site/does_not_fit.py`) where the plan asks the person, else `None` (it
        fits, a choice was made, or the lasting `if_not_ready` answers). `recheck` gives the block again (at 07:00 for
        one that arose in the night). Answers what the phones were told for the site's change log: `told`, `cleared`,
        or `None` (see the module)."""
        now = dt_util.utcnow()
        asking = block if block is not None and block.get("chosen") is None and block.get("choices") else None
        departure = None if asking is None else asking.get("departure")
        self._fit_asking = asking
        self._fit_recheck = recheck
        outcome: str | None = None
        if self._fit_phones and (asking is None or departure != self._fit_told):
            # It no longer stands (or stands for another departure): taken off the phones.
            self.clear_vehicle_question(self._at_risk_tag(), self._fit_phones)
            self._fit_phones = ()
            self._save_plan()
            outcome = "cleared"
        moment = _instant(departure)
        if asking is None or moment is None or moment <= now:
            self._arm_fit_timer(None)
            was_quiet, self._fit_quiet = self._fit_quiet, False
            behind = None if self._site_holds else self._at_risk_behind_target
            if not self._site_holds:
                # Kept while the site's combined notice holds this car: told once that one lets go.
                self._at_risk_behind_target = None
            if (outcome is not None or was_quiet) and behind is not None and self._at_risk_now:
                # The plan still cannot be met for reasons of its own: told now, as it was not before.
                self._mark_at_risk_told(behind, now)
                self._maybe_send(EVENT_PLAN_AT_RISK, behind, now)
            return outcome
        if departure == self._fit_told:
            return outcome  # once per departure
        if self._connected is False or self._person_stop_holds():
            # Nothing is charged with no car there; nothing about the plan is told under a person's Stop.
            self._fit_quiet = False
            self._arm_fit_timer(None)
            return outcome
        if is_quiet(now):
            # Not in the night: at 07:00 if it still stands then and its departure is later; else never.
            due = quiet_end(now)
            self._fit_quiet = True
            self._arm_fit_timer(due if moment > due else None)
            return outcome
        self._fit_quiet = False
        self._arm_fit_timer(None)
        return "told" if self._tell_fit(asking, moment, now) else outcome

    def _tell_fit(self, block: dict[str, Any], departure: datetime, now: datetime) -> bool:
        """Tell the "does not fit" notice to the paired app and the chosen phones; whether either heard it."""
        pushed = self._push is not None and self._push.async_event(EVENT_PLAN_AT_RISK, now)
        targets: tuple[str, ...] = self._companion(EVENT_PLAN_AT_RISK) if self._within_hourly_limit(now) else ()
        if not targets and not pushed:
            return False
        # Told for this departure from here on, whatever else happens to it.
        self._fit_told = block["departure"]
        self._fit_phones = targets
        self._save_plan()
        if not targets:
            return True
        self._sent_times.append(now)
        self._last_sent[EVENT_PLAN_AT_RISK] = now
        language = language_of(self._hass.config.language)
        currency = self._currency()
        title, message = does_not_fit_text(
            self._name(),
            language,
            time=_local_time(departure),
            short_kwh=block.get("short_kwh"),
            ceiling_kw=float(block["ceiling_kw"]),
            set_kw=block.get("set_kw"),
        )
        # The departure it was told for rides with the press: one for a departure no longer the plan's is refused.
        stamp = _epoch(departure)
        prefixes = {CHOICE_TAKES_WHAT_FITS: TAKE_WHAT_FITS_ACTION, CHOICE_ACCEPT_PEAK: TARGET_RELEASE_ACTION}
        actions = []
        for choice in block.get("choices") or []:
            code = choice.get("code")
            if code == CHOICE_LATER:
                # Only with the time it moves to: without one the departure's editor is the way, which a tap opens.
                later = _instant(choice.get("departure"))
                if later is None:
                    continue
                actions.append(
                    {
                        "action": f"{LATER_DEPARTURE_ACTION}{self._entry_id}_{stamp}_{_epoch(later)}",
                        "title": does_not_fit_choice_text(language, code, time=_local_time(later)),
                    }
                )
                continue
            if code not in prefixes:
                continue
            actions.append(
                {
                    "action": f"{prefixes[code]}{self._entry_id}_{stamp}",
                    "title": does_not_fit_choice_text(
                        language,
                        code,
                        kwh=choice.get("kwh"),
                        peak_kw=choice.get("peak_kw"),
                        cost=None
                        if choice.get("cost") is None or currency is None
                        else Money(int(round(choice["cost"])), currency),
                    ),
                }
            )
        url = self._url() or DEFAULT_URL
        payload = {
            "title": title,
            "message": message,
            "data": {
                "url": url,
                "clickAction": url,
                "tag": self._at_risk_tag(),
                "group": "spotnav",
                "channel": "SpotNav",
                "actions": actions,
            },
        }
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)
        return True

    def _arm_fit_timer(self, due: datetime | None) -> None:
        if self._closed:
            # Shut down: a late `does_not_fit` (the site's last recompute) arms nothing; one still armed is cancelled.
            due = None
        if due == self._fit_timer_at:
            return
        if self._fit_timer is not None:
            self._fit_timer()
        self._fit_timer = None
        self._fit_timer_at = due
        if due is None:
            return

        @callback
        def _fire(_now: datetime) -> None:
            self._fit_timer = None
            self._fit_timer_at = None
            if self._fit_recheck is not None:
                # The block as it stands now (the site tells it, and logs what was told).
                self._fit_recheck()
            else:
                self.does_not_fit(self._fit_asking)

        self._fit_timer = async_track_point_in_utc_time(self._hass, _fire, due)

    def _at_risk_tag(self) -> str:
        return f"spotnav_{self._entry_id}_{EVENT_PLAN_AT_RISK}"

    @callback
    def _on_action(self, event: Any) -> None:
        """A phone's button on the "does not fit" notice: "Allow the peak" sets the import target aside for this
        charge until its departure, "Take what fits" holds that departure under it, "Later departure (14:00)" moves
        it to the time told; each for the departure told."""
        action = event.data.get("action")
        if not isinstance(action, str):
            return
        prefixes = {
            f"{TARGET_RELEASE_ACTION}{self._entry_id}_": CHOICE_ACCEPT_PEAK,
            f"{TAKE_WHAT_FITS_ACTION}{self._entry_id}_": CHOICE_TAKES_WHAT_FITS,
            f"{LATER_DEPARTURE_ACTION}{self._entry_id}_": CHOICE_LATER,
        }
        prefix = next((prefix for prefix in prefixes if action.startswith(prefix)), None)
        if prefix is None:
            return
        choice = prefixes[prefix]
        stamps = action.removeprefix(prefix).split("_")
        if len(stamps) != (2 if choice == CHOICE_LATER else 1) or not all(stamp.isdigit() for stamp in stamps):
            return
        from ..execution.solar_execution import site_controller_for_charger  # noqa: PLC0415 - it imports the site

        site = site_controller_for_charger(self._hass, self._entry_id)
        if site is None:
            return
        if choice == CHOICE_ACCEPT_PEAK:
            site.async_release_import_target(self._entry_id, for_departure=int(stamps[0]), source="phone")
        elif choice == CHOICE_LATER:
            site.async_later_departure(self._entry_id, for_departure=int(stamps[0]), to=int(stamps[1]), source="phone")
        else:
            site.async_take_what_fits(self._entry_id, for_departure=int(stamps[0]), source="phone")

    # ------------------------------------------------------------------ which car is plugged in?

    def _within_hourly_limit(self, now: datetime) -> bool:
        while self._sent_times and (now - self._sent_times[0]).total_seconds() >= 3600:
            self._sent_times.popleft()
        return len(self._sent_times) < HOURLY_LIMIT

    def _identify_data(self, tag: str) -> dict[str, Any]:
        url = self._url() or DEFAULT_URL
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
        targets = self._companion(EVENT_VEHICLE_IDENTIFY)
        if not targets:
            return ()
        if not self._within_hourly_limit(now):
            _LOGGER.debug("SpotNav charger %s: hourly notification limit reached", self._entry_id)
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
        """A test message to this charger's phones that are on and reached through the Companion app now
        (`spotnav.send_test_notification`), outside the limits: the services it went to. A phone whose SpotNav app can
        be woken gets the app's test instead (`ChargerPush.async_send_test`)."""
        phones = phone_store(self._hass)
        targets = (
            []
            if phones is None
            else [
                phone.notify
                for phone in phones.phones
                if phone.on
                and phone.follows(self._entry_id)
                and route_of(phone, (), self._has_service) == ROUTE_COMPANION
                and phone.notify is not None
            ]
        )
        if not targets:
            return []
        title, message = compose(EVENT_TEST, self._name(), {}, language_of(self._hass.config.language))
        payload = self._payload(EVENT_TEST, title, message, self._url())
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
            if is_guest(self._store.settings(self._entry_id)):
                # A guest car's plan says so (`vehicles/guest.py`).
                facts["guest"] = True
            cost = self._plan_cost()
            if cost is not None:
                facts["cost"] = cost
            return facts
        if event == EVENT_CHARGE_COMPLETE:
            facts = {
                "reason": attributes.get("reason"),
                "target_percent": attributes.get("target_soc_percent"),
                # A target's stop: the level it was taken at and whether that was read or estimated.
                "level_percent": attributes.get("soc_percent"),
                "basis": attributes.get("basis"),
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
        if last.end is None or (dt_util.utcnow() - last.end).total_seconds() > SESSION_RECENT_S:
            return None
        return last
