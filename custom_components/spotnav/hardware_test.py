"""A person's hardware test of a site's plant ("Kör hårdvarutest" in the card's Support box, `api/beta.py`).

Started by an administrator pressing Start, at most once a day per site (`ONCE_A_DAY`) and once more after a
SpotNav update or a changed plant firmware (`gate.firmware_key`; `allowance`). **Nothing else is asked of the person.**
Where the site has a battery part, the test waits for a moment that suits every battery step by itself
(`BatteryControl.hardware_test_moment`, the verification routine's own rule: the house drawing more than the sun
gives, so the battery covers it; its level between `gate.SUITABLE_SOC_PCT`; no car charging; the fuse with room) for
at most `WAIT_MAX`, and the card says when it is likely to run (`state` `waiting`, `next_at`). A run where a battery
step could not be judged (and none failed) runs again by itself at the next such moment, up to `MAX_RETRIES` times.
A brand's **first** pass on the plant (the battery's recipe waits for a person's hardware test,
`BatteryControl.hardware_test_due` `hardware_test_first`: never run on this plant) runs only in waking hours
(`WAKING_HOURS`, local), and when it starts writing each phone that is on for SpotNav's notices gets one notification
"the battery's hardware test is running now (about N min)" with a Stop button (`STOP_ACTION`), taken off when the run
ends; the automatic routine after a later change tells no one.
It runs the parts the site has, each through the engine that already writes to it, with every rule of that engine,
in at most `MAX_TEST` all told (a slow recipe's own `BrandControl.test_total_s`: Tesla's cloud, about an hour):

* **The home battery** (`battery/control.py`, `async_hardware_test_start`): the verification routine's modes in
  minutes (`verify.quick_for`; a slow recipe waits for its re-arm and cloud read-backs at each mode): hold, a small
  grid charge under the HA-down fuse cap (the learned per-phase house peak, or its conservative default; where that
  leaves the balanced charge no room and the plant takes targets per phase, a charge of at most 1 kW on the phase
  with the most room, its sign and phase mapping judged; a plant that charges at its own power, Tesla's, whose power
  does not fit under the cap skips it as `gate.NOT_APPLICABLE_FIXED_CHARGE`, which the pass accepts), a discharge
  for the house, where the plant's dead-man is the integration's and SpotNav can silence it (EcoFlow: Modbus Control
  off, `gate.deadman_testable`) a hold left to that dead-man and timed (`DEADMAN_STEP`, `DEADMAN_EXTRA` more), and
  the hand-back judged; where the dead-man is SpotNav's own renewal and the recipe waits for a hardware test
  (Solis's RC Timeout), the hold is left unrenewed and the inverter's fallback timed the same way. Needs the battery
  opt-in and everything the card's checklist asks first (acknowledgements, controls to enable); runs under shadow
  only too, as a forced test action does. Never while something else runs the battery (an outside system, its own
  optimiser, a force left standing): with nothing else to test the test is refused (`BLOCKED_RUN_ELSEWHERE`) and the
  card asks for that controller to be paused first.
* **The heat path** (`heat/control.py`, `async_test_start`): engaged, held a minute, released, each write read back.
  Needs heat control running (the opt-in, the check passed, no block, pause or safety reason).
* **Hot water**: a boost only where a self-ending one exists; none does on this branch, so no step is shown (the
  hot-water gate adds its step when it brings one).

**The site's chargers** (`site/charger_test_hold.py`): a test whose battery part would run waits while a charger on
the site is on, and from its start until its charge step is over and handed back (`_past_charge`; the later modes
take nothing from the grid), every charger is held stopped; a stop, the deadline, a failure or an error lets them
go only once the battery reads handed back, never past the hold's own end (`_release_when_handed_back`), an unload
at once: no start of any kind goes out, a charge a charger begins by itself is stopped, an OCPP charger's
charge point is taken out of service with its availability switch (and set back to operative at the end, or at the
next start after a restart in between), and the
battery's HA-down cap may count a held charger that cannot draw without Home Assistant at 0 A.

The parts run side by side (separate devices; the battery's modes alone take about nine minutes, most of it the
executor's own re-arm after each hand-back). Any step that does not pass (a mode ignored or partial, a write not
taken or not read back) ends the test: that part hands back by its own engine and the other part is ended at once,
handing back what it engaged. The deadline does the same. The result
(`passed` when every part ran, `partial` when a part was skipped, `skipped` when none ran, `failed` when a step
failed; a partial or skipped test leaves the day's slot, `NOT_COUNTED`), the steps, each engine's own report) is kept per site, shown by the card step by step,
carried in the debug bundle as `hardware_test` (the relay flags `result` `failed`) and, while beta is on, sent by
itself as a `beta_test` report; otherwise the card offers "Skicka" (`async_send_test`). For a battery tester (a brand
whose control is still being tested, the person signed up and no control code yet: `battery/tester.py`) it goes by
itself as a `battery_test_result` instead, beta or not, with the test's log and the plant's history over it.

A brand whose recipe waits for a person's hardware test and was never passed on the plant tests only once the person
signed up as a tester for it (`blocked` `tester_signup`: the card's "Become a tester" in the battery section).

It costs a little grid energy (the charge step: a few minutes at about 2 kW at most), said in the card's "Läs mer".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .battery.gate import mode_verdict
from .const import DOMAIN
from .texts import language_of

if TYPE_CHECKING:
    from .site.site_capacity_controller import SiteCapacityController
    from .support_report import ReportResult

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
#: The longest a test runs, all parts together; the deadline hands everything back.
MAX_TEST: Final = timedelta(minutes=10)
#: The least time between two tests of a site, unless SpotNav or the plant's firmware changed.
ONCE_A_DAY: Final = timedelta(hours=24)
#: How often a running part is read (s).
POLL_S: Final = 2.0
#: The battery's steps (its verification modes) and the heat path's.
BATTERY_STEPS: Final = (("hold", "hold"), ("charge", "charge"), ("discharge", "discharge_house"),
                        ("hand_back", "normal"))
#: The step that times the plant's own dead-man (the verification's mode `expiry`), shown only where the battery part
#: runs it (`BatteryControl.hardware_test_deadman`: EcoFlow, Solis), before the hand-back; and the time it adds.
DEADMAN_STEP: Final = ("deadman", "expiry")
DEADMAN_EXTRA: Final = timedelta(minutes=5)
#: Refusals that mean the part is not there to test (no step shown), rather than not testable now.
ABSENT: Final = frozenset({"no_battery_control", "not_enabled", "no_relay"})
#: Abort reasons that are the plant or its integration not doing as told (read back missing, write refused).
_HEAT_FAILURES: Final = ("aborted:no_read_back", "aborted:write_failed", "aborted:not_read_back")

ERROR_NO_TEST: Final = "spotnav_no_test"
#: A test pressed and waiting for a suitable moment (`blocked` while it waits: one is enough).
BLOCKED_WAITING: Final = "waiting"
#: Nothing can run now and the battery's part is refused because something else runs the battery (an outside system,
#: `outside_control`, or the battery's own optimiser or a force left standing, `self_managed`): the person must pause
#: it first. SpotNav never tests over another controller.
BLOCKED_RUN_ELSEWHERE: Final = "battery_run_elsewhere"
_RUN_ELSEWHERE: Final = ("needs:outside_control", "needs:self_managed")
#: How long a pressed test waits for a moment that suits its battery steps before it gives up (no prompt: the
#: record says `no_suitable_moment`).
WAIT_MAX: Final = timedelta(days=3)
#: How often a waiting test looks at the site (s).
WAIT_POLL_S: Final = 60.0
#: How many times a run with a battery step that could not be judged runs again by itself.
MAX_RETRIES: Final = 3
#: The local hour a daytime wait is likely to end at (the evening's house load over the sun, `gate.EVENING_HOURS`).
LIKELY_HOUR: Final = 17
#: After a start, how long a stored wait keeps waiting for a battery part that is not there yet (an integration that
#: loads after SpotNav's own start-up wait), rather than giving up.
LOAD_GRACE: Final = timedelta(minutes=15)
#: The local hours a brand's first pass on the plant may start in (from, until): someone is likely awake to see it.
WAKING_HOURS: Final = (8, 21)
#: The Stop button of the first pass's notification, with the site's entry id.
STOP_ACTION: Final = "SPOTNAV_HARDWARE_TEST_STOP_"
#: Home Assistant's event for a notification's button (`notifications.notifier.NOTIFICATION_ACTION_EVENT`), and the
#: notify domain: named here, the notifications package imports the site.
NOTIFICATION_ACTION_EVENT: Final = "mobile_app_notification_action"
NOTIFY_DOMAIN: Final = "notify"
#: The chargers' hold ends by itself this long after the test's deadline, should nothing release it.
HOLD_SLACK: Final = timedelta(minutes=1)
#: How long the unload waits for the OCPP chargers to be set back to operative (s): more than one call's own time
#: (`charger_test_hold.CALL_S`).
RESTORE_AT_UNLOAD_S: Final = 35.0


def _past_charge(progress: Mapping[str, Any]) -> bool:
    """Whether a running battery part is past its charge step and handed back from it: a later mode (the charge
    passed, failed or was not applicable) with nothing held. The chargers' hold is for the charge step only."""
    mode = progress.get("mode")
    return mode is not None and mode not in ("hold", "charge") and not progress.get("holding")


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.hardware_test.{entry_id}"


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def allowance(
    last: Mapping[str, Any] | None, now: datetime, version: str | None, firmware_key: str | None
) -> tuple[str | None, datetime | None]:
    """`(blocked, next_at)`: `None` when a test may start; else `once_a_day` and when it may. A SpotNav version or a
    plant firmware key other than the last test's allows one more at once."""
    if not isinstance(last, Mapping):
        return None, None
    at = _time(last.get("at"))
    if at is None or now - at >= ONCE_A_DAY:
        return None, None
    if version is not None and last.get("version") != version:
        return None, None
    if firmware_key is not None and last.get("firmware_key") not in (None, firmware_key):
        return None, None
    return "once_a_day", at + ONCE_A_DAY


def battery_step_status(outcome: Any, running: bool) -> str:
    """One battery mode's step: `running`, `pending`, `passed`, `failed`, `not_judgeable`, `skipped` or `aborted`."""
    if outcome is None:
        return "running" if running else "pending"
    text = str(outcome)
    if text == "skipped" or text.startswith("not_applicable:"):
        # Not applicable: a step the plant cannot run at a safe size (`gate.NOT_APPLICABLE_FIXED_CHARGE`).
        return "skipped"
    if text.startswith("not_judgeable:"):
        return "not_judgeable"
    verdict = mode_verdict(text)
    if verdict == "pass":
        return "passed"
    if verdict == "fail":
        return "failed"
    return "aborted"


def _reason(outcome: Any) -> str | None:
    if not isinstance(outcome, str) or ":" not in outcome:
        return None
    return outcome.split(":", 1)[1]


def battery_steps(report: Mapping[str, Any] | None, current: str | None, running: bool) -> list[dict[str, Any]]:
    modes = (report or {}).get("modes") or {}
    steps = []
    order = BATTERY_STEPS if DEADMAN_STEP[1] not in modes else (*BATTERY_STEPS[:-1], DEADMAN_STEP, BATTERY_STEPS[-1])
    for step, mode in order:
        outcome = (modes.get(mode) or {}).get("outcome")
        steps.append({
            "part": "battery", "step": step,
            "status": battery_step_status(outcome, running and current == mode),
            "reason": _reason(outcome),
        })
    return steps


def heat_steps(test: Mapping[str, Any] | None, running: bool) -> list[dict[str, Any]]:
    test = test or {}
    outcome = test.get("outcome")
    phase = test.get("phase")
    failed = isinstance(outcome, str) and outcome.startswith(_HEAT_FAILURES)
    steps = []
    for step, done_key, phases in (
        ("engage", "engaged_at", ("engage", "engaging")), ("release", "released_at", ("hold", "releasing")),
    ):
        if test.get(done_key):
            status = "passed"
        elif outcome is None:
            status = "running" if running and phase in phases else "pending"
        elif step == "release" and not test.get("engaged_at"):
            status = "skipped"
        elif failed:
            status = "failed"
        elif isinstance(outcome, str) and outcome.startswith("not_judgeable:"):
            status = "not_judgeable"
        else:
            status = "aborted"
        steps.append({"part": "heat", "step": step, "status": status,
                      "reason": None if status in ("passed", "running", "pending") else _reason(outcome)})
    return steps


def _part_skipped(step: Mapping[str, Any]) -> bool:
    """A whole part not run (its one `skipped` row, named after the part), not a step skipped inside a part."""
    return step.get("status") == "skipped" and step.get("step") is not None and step.get("step") == step.get("part")


def result_of(steps: list[dict[str, Any]]) -> str:
    """`failed` when any step failed (the relay flags these); else `skipped` when no part ran, `partial` when a part
    was skipped or a step could not be judged (a check that could not be made is not a pass), `passed` when every
    part ran and every step was judged."""
    if any(step.get("status") == "failed" for step in steps):
        return "failed"
    if not steps or all(_part_skipped(step) for step in steps):
        return "skipped"
    if any(_part_skipped(step) or step.get("status") == "not_judgeable" for step in steps):
        return "partial"
    return "passed"


#: Results that leave the day's slot (a part did not run: it may be run again once ready).
NOT_COUNTED: Final = frozenset({"partial", "skipped"})


class HardwareTest:
    """One site's hardware test (see the module); owned by its `SiteCapacityController`."""

    def __init__(self, hass: HomeAssistant, site: SiteCapacityController) -> None:
        self.hass = hass
        self.site = site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site.entry_id))
        #: The last test's start (`at`, `version`, `firmware_key`): what `allowance` reads.
        self.last: dict[str, Any] | None = None
        #: The last finished test (`record`), and the reference its report got.
        self.record: dict[str, Any] | None = None
        self.sent_ref: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._running: dict[str, Any] | None = None
        #: A test pressed (or a run to retry) waiting for a suitable moment: `since`, `until`, `version`, `retry`,
        #: `why` (the battery's reason to wait now); stored, so a restart goes on waiting.
        self.waiting: dict[str, Any] | None = None
        self._waiter: asyncio.Task[None] | None = None
        #: The phones told that a first pass runs now (taken off at its end), and the action listener.
        self._told: tuple[str, ...] = ()
        #: When this runner loaded its store (`LOAD_GRACE`), and whether a wait began or ended since (written at the
        #: unload).
        self._loaded_at = dt_util.now()
        self._wait_touched = False
        self._unsubscribe: Any = None
        self._closed = False
        self._unloading = False
        #: A release of the chargers' hold that waits for the battery's hand-back (`_release_when_handed_back`):
        #: no other path lets them go meanwhile, but an unload does at once.
        self._release_task: asyncio.Task[None] | None = None

    async def async_load(self) -> None:
        with contextlib.suppress(Exception):
            self._unsubscribe = self.hass.bus.async_listen(NOTIFICATION_ACTION_EVENT, self._on_action)
        try:
            raw = await self._store.async_load()
        except Exception:  # noqa: BLE001 - nothing kept: the next test may start
            _LOGGER.debug("async_load: ignored failure (nothing kept: the next test may start)", exc_info=True)
            raw = None
        raw = raw if isinstance(raw, dict) else {}
        self.last = raw.get("last") if isinstance(raw.get("last"), dict) else None
        self.record = raw.get("record") if isinstance(raw.get("record"), dict) else None
        if self.record is not None and isinstance(self.record.get("steps"), list):
            # A test kept from before the partial/skipped results is judged again by its steps.
            self.record["result"] = result_of([step for step in self.record["steps"] if isinstance(step, dict)])
        self.sent_ref = raw.get("sent_ref") if isinstance(raw.get("sent_ref"), str) else None
        waiting = raw.get("waiting")
        if isinstance(waiting, dict) and _time(waiting.get("until")) is not None:
            self.waiting = waiting
            self._wait()

    def _save(self) -> None:
        self._wait_touched = self._wait_touched or self.waiting is not None
        if not self._closed:
            self._store.async_delay_save(self._data, 1.0)

    def _data(self) -> dict[str, Any]:
        return {"last": self.last, "record": self.record, "sent_ref": self.sent_ref, "waiting": self.waiting}

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    # ---- what can run

    def _parts(self, now: datetime) -> dict[str, str | None]:
        """Each part the site has and why it cannot run now (`None`: it can); a part not there is left out."""
        parts: dict[str, str | None] = {}
        battery = self.site.battery_control.hardware_test_refusal()
        if battery not in ABSENT:
            parts["battery"] = battery
        heat = self.site.heat_control.test_refusal(now)
        if heat not in ABSENT:
            parts["heat"] = heat
        return parts

    async def _async_version(self) -> str | None:
        from .card_asset import async_manifest_version  # noqa: PLC0415 - read when a test starts

        version = await async_manifest_version(self.hass)
        return None if version is None else str(version)

    def _firmware_key(self) -> str | None:
        try:
            return self.site.battery_control.firmware_key()
        except Exception:  # noqa: BLE001 - no battery: no firmware to tell apart
            _LOGGER.debug("firmware_key: ignored failure (no battery: no firmware to tell apart)", exc_info=True)
            return None

    def blocked(self, now: datetime, version: str | None) -> tuple[str | None, datetime | None]:
        """Why a test cannot be pressed now (`running`, `waiting`, `once_a_day`, `nothing_to_test`, `tester_signup`,
        `battery_run_elsewhere` (after `tester_signup`: a recipe not yet unlocked says so first), `not_ready`), and when
        it may."""
        if self.running:
            return "running", None
        if self.waiting is not None:
            return BLOCKED_WAITING, None
        # Nothing to test comes first: the day's slot is no reason on a site where no test could run at all.
        parts = self._parts(now)
        if not parts:
            return "nothing_to_test", None
        blocked, next_at = allowance(self._counted_last(), now, version, self._firmware_key())
        if blocked is not None:
            return blocked, next_at
        if parts.get("battery") == "tester_signup" and all(
            why is not None for name, why in parts.items() if name != "battery"
        ):
            # A recipe never tested here: the sign-up as a tester unlocks its first test.
            return "tester_signup", None
        if all(why is not None for why in parts.values()):
            battery = parts.get("battery")
            if battery is not None and battery.startswith(_RUN_ELSEWHERE):
                # Another controller runs the battery: it must be paused before the test can write to it.
                return BLOCKED_RUN_ELSEWHERE, None
            # A part is there but cannot run now (starting, busy, waiting for an acknowledgement): not "nothing".
            return "not_ready", None
        return None, None

    def _hold(self) -> Any:
        """The site's hold of its chargers (`site/charger_test_hold.py`)."""
        return self.site.charger_test_hold

    def _counted_last(self) -> dict[str, Any] | None:
        """The last test's start, unless that test (finished) had a part not run: it leaves the day's slot."""
        record = self.record
        if (self.last is not None and record is not None and record.get("result") in NOT_COUNTED
                and record.get("started_at") == self.last.get("at")):
            return None
        return self.last

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- the run

    async def async_start(self) -> str | None:
        """Start a test in the background; the refusal code, `None` once it started."""
        if self._closed:
            return "unloaded"
        now = dt_util.now()
        version = await self._async_version()
        blocked, _next = self.blocked(now, version)
        if blocked is not None:
            return blocked
        self.last = {"at": now.isoformat(), "version": version, "firmware_key": self._firmware_key()}
        why = self._wait_reason(now)
        if why is not None:
            # Pressed: the test runs by itself at the first moment that suits its battery steps.
            self._schedule(now, version, retry=0, why=why)
            return None
        return self._begin(now, version)

    def _wait_reason(self, now: datetime) -> str | None:
        """Why a test with a battery part should wait now (`BatteryControl.hardware_test_moment`, a charger on the
        site charging), `None` when it may run: also when it has no battery part to wait for."""
        if self._parts(now).get("battery", "absent") is not None:
            return None
        if self._first_pass() and not WAKING_HOURS[0] <= dt_util.as_local(now).hour < WAKING_HOURS[1]:
            # A recipe never run on this plant: its first writes only when someone is likely awake to see them.
            return "not_waking_hours"
        try:
            why = self.site.battery_control.hardware_test_moment(now)
        except Exception:  # noqa: BLE001 - nothing read: wait, the next look tries again
            _LOGGER.debug("wait_reason: ignored failure (the moment could not be read)", exc_info=True)
            return "stale_data"
        if why is None and self._hold().charging_now():
            # The battery's charge step is reckoned with the site's chargers held stopped: none may run at the start.
            return "car_charging"
        return why

    def _first_pass(self) -> bool:
        """Whether the battery part is the brand's first pass on this plant (`hardware_test_first`)."""
        try:
            return self.site.battery_control.hardware_test_due() == "hardware_test_first"
        except Exception:  # noqa: BLE001 - no battery control: no first pass
            return False

    def _tell_running(self, minutes: int) -> None:
        """A first pass starts writing: one notification to each phone on for SpotNav's notices, with Stop."""
        from .notifications.messages import hardware_test_text  # noqa: PLC0415 - the package imports the site
        from .notifications.phone_store import phone_store  # noqa: PLC0415

        store = phone_store(self.hass)
        targets = () if store is None else store.companion_on()
        if not targets:
            return
        title, message, button = hardware_test_text(language_of(self.hass.config.language), minutes)
        url = (None if store is None else store.url) or "/"
        payload = {"title": title, "message": message, "data": {
            "url": url, "clickAction": url, "tag": self._tag, "group": "spotnav", "channel": "SpotNav",
            "actions": [{"action": f"{STOP_ACTION}{self.site.entry_id}", "title": button}],
        }}
        self._told = targets
        for target in targets:
            self.hass.async_create_background_task(self._async_notify(target, payload), f"{DOMAIN} hardware test notice")
        _LOGGER.info("SpotNav hardware test: the battery's first test told on %d phone(s)", len(targets))

    def _clear_told(self) -> None:
        targets, self._told = self._told, ()
        payload = {"message": "clear_notification", "data": {"tag": self._tag}}
        for target in targets:
            self.hass.async_create_background_task(self._async_notify(target, payload), f"{DOMAIN} hardware test notice")

    @property
    def _tag(self) -> str:
        return f"spotnav_site_{self.site.entry_id}_hardware_test"

    async def _async_notify(self, target: str, payload: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(NOTIFY_DOMAIN, target, payload, blocking=True)
        except Exception as err:  # noqa: BLE001 - a phone that cannot be reached must not stop the test
            _LOGGER.warning("SpotNav could not notify %s: %s", target, type(err).__name__)

    @callback
    def _on_action(self, event: Any) -> None:
        if event.data.get("action") == f"{STOP_ACTION}{self.site.entry_id}" and (self.running or self.waiting):
            _LOGGER.info("SpotNav hardware test: stopped from a phone")
            self.hass.async_create_task(self.async_stop("phone"), f"{DOMAIN} hardware test stop")

    def _schedule(self, now: datetime, version: str | None, *, retry: int, why: str) -> None:
        self.waiting = {"since": now.isoformat(), "until": (now + WAIT_MAX).isoformat(), "version": version,
                        "retry": retry, "why": why}
        self._save()
        _LOGGER.info("SpotNav hardware test: waits for a suitable moment (%s)%s", why,
                     f", run {retry + 1}" if retry else "")
        self._wait()

    def _wait(self) -> None:
        if self._closed or (self._waiter is not None and not self._waiter.done()):
            return
        self._waiter = self.hass.async_create_background_task(self._async_wait(), f"{DOMAIN} hardware test wait")

    async def _async_wait(self) -> None:
        """Look at the site every `WAIT_POLL_S` until the moment suits (the test then runs) or `WAIT_MAX` is over (it
        gives up without asking anything: the record says `no_suitable_moment`)."""
        while self.waiting is not None and not self._closed:
            await asyncio.sleep(WAIT_POLL_S)
            self.look_waiting(dt_util.now())

    def look_waiting(self, now: datetime) -> None:
        """One look of a waiting test (also for the tests)."""
        waiting = self.waiting
        if waiting is None or self._closed or self.running:
            return
        until = _time(waiting.get("until"))
        parts = self._parts(now)
        if "battery" not in parts and until is not None and now < until and now - self._loaded_at < LOAD_GRACE:
            waiting["why"] = "starting"  # the battery's integration may load later than SpotNav itself
            return
        if until is None or now >= until or "battery" not in parts:
            self.waiting, self._wait_touched = None, True
            self._give_up(now, "no_suitable_moment" if "battery" in parts else str(parts.get("battery") or "absent"))
            return
        if parts["battery"] is not None and parts["battery"].startswith(_RUN_ELSEWHERE):
            # Something else took the battery over while the test waited: it gives up rather than start in a gap of
            # the other system's (the card then says to pause it, `batteryRunElsewhere`).
            self.waiting, self._wait_touched = None, True
            self._give_up(now, parts["battery"])
            return
        if parts["battery"] is not None:
            waiting["why"] = parts["battery"]  # starting, verifying, a trial: not yet
            return
        why = self._wait_reason(now)
        if why is not None:
            if why != waiting.get("why"):
                waiting["why"] = why
                self._save()
            return
        self.waiting, self._wait_touched = None, True
        self._save()
        self._begin(now, waiting.get("version"), retry=int(waiting.get("retry") or 0))

    def _give_up(self, now: datetime, why: str) -> None:
        """A wait that found no suitable moment: the record says so, and the day's slot is left (`skipped`)."""
        self.record = {
            # Started when it was pressed: a `skipped` record of that press leaves the day's slot (`_counted_last`).
            "site": self.site.entry_id, "started_at": (self.last or {}).get("at") or now.isoformat(),
            "finished_at": now.isoformat(),
            "spotnav_version": None, "firmware_key": self._firmware_key(), "result": "skipped", "stopped": None,
            "error": None, "steps": [{"part": "battery", "step": "battery", "status": "skipped", "reason": why}],
            "battery": None, "heat": None,
        }
        self._save()
        _LOGGER.info("SpotNav hardware test: no suitable moment came (%s)", why)

    def _begin(self, now: datetime, version: str | None, *, retry: int = 0) -> str | None:
        parts = self._parts(now)
        self._running = {"started_at": now.isoformat(), "parts": parts, "battery": None, "heat": None,
                         "version": version, "retry": retry}
        self.record = None
        self.sent_ref = None
        self._save()
        _LOGGER.info("SpotNav hardware test: started (%s)", ", ".join(f"{k}: {v or 'runs'}" for k, v in parts.items()))
        if parts.get("battery", "absent") is None:
            # Held from now until the run's end, by any path (`_async_run`'s `finally`), and never past the deadline.
            self._hold().begin(now + self._max_test() + HOLD_SLACK)
        try:
            self._task = self.hass.async_create_background_task(self._async_run(now), f"{DOMAIN} hardware test")
        except BaseException:
            self._release_hold()
            raise
        if parts.get("battery", "absent") is None and self._first_pass():
            self._tell_running(int(-(-self._max_test().total_seconds() // 60)))
        return None

    def _release_hold(self, *, waited: bool = False) -> None:
        """End the chargers' hold, and arm again the plans of those whose start it refused, in the background (a run
        being cancelled cannot cut that short). Never raises. Not while a release waits for the battery's hand-back
        (`_release_when_handed_back`; `waited`: that one's own), except at an unload."""
        pending = self._release_task
        if not waited and pending is not None and not pending.done() and not (self._closed or self._unloading):
            return
        try:
            hold = self._hold()
            refused = hold.release()
        except Exception:  # noqa: BLE001 - the hold ends by itself at its deadline either way
            _LOGGER.warning("SpotNav hardware test: the chargers' hold could not be ended", exc_info=True)
            return
        if self._closed or self._unloading:
            # The unload sets the OCPP chargers back itself, awaited (`async_shutdown`); nothing is armed again.
            return
        self.hass.async_create_background_task(
            self._async_after_release(hold, refused), f"{DOMAIN} hardware test release"
        )

    def _battery_holding(self) -> bool:
        """Whether the battery still holds a command of the test's (not yet handed back)."""
        try:
            return bool(self.site.battery_control.hardware_test_progress()["holding"])
        except Exception:  # noqa: BLE001 - no battery control: nothing to wait for
            _LOGGER.debug("battery_holding: ignored failure (no battery control)", exc_info=True)
            return False

    def _release_when_handed_back(self) -> None:
        """A stop, the deadline or a failure while the chargers are still held (in or before the charge step): they
        go only once the battery reads handed back (a Powerwall's hand-back is a cloud write read back within
        minutes, longer than the stop waits), never past the hold's own end (`HOLD_SLACK` after the deadline); at
        that end they go anyway, logged. At once where nothing is held or the battery holds nothing, and at an
        unload."""
        try:
            held = self._hold().until is not None
        except Exception:  # noqa: BLE001 - no hold: nothing to keep
            held = False
        if not held or self._closed or self._unloading or not self._battery_holding():
            self._release_hold()
            return
        if self._release_task is not None and not self._release_task.done():
            return
        self._release_task = self.hass.async_create_background_task(
            self._async_release_after_hand_back(), f"{DOMAIN} hardware test release after the hand-back"
        )

    async def _async_release_after_hand_back(self) -> None:
        hold = self._hold()
        try:
            while not (self._closed or self._unloading):
                if not self._battery_holding():
                    break
                if hold.until is None:
                    _LOGGER.warning(
                        "SpotNav hardware test: the battery was not handed back before the chargers' hold ended; the"
                        " chargers are let go"
                    )
                    break
                await asyncio.sleep(POLL_S)
        finally:
            self._release_hold(waited=True)

    @staticmethod
    async def _async_after_release(hold: Any, refused: list[str]) -> None:
        """The OCPP chargers set back to operative first, then the refused plans armed again."""
        await hold.async_restore_availability()
        if refused:
            await hold.async_rearm_refused(refused)

    async def async_stop(self, why: str) -> None:
        """End a running test at once (everything it engaged handed back), or a waiting one."""
        waiter, self._waiter = self._waiter, None
        if self.waiting is not None and why != "unload":
            # The person's stop ends the wait; an unload keeps it (stored), so a restart goes on waiting.
            self.waiting, self._wait_touched = None, True
            self._save()
        if waiter is not None and not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiter
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        # The run's own `finally` let the chargers go, or waits for the battery's hand-back first; a run cancelled
        # before it began never got there.
        self._release_when_handed_back()
        self._finish_stopped(why)

    def _max_test(self) -> timedelta:
        """The test's deadline from its start: `MAX_TEST`, and `DEADMAN_EXTRA` more where the battery part also times
        the plant's own dead-man."""
        try:
            deadman = bool(self.site.battery_control.hardware_test_deadman())
            step_s = self.site.battery_control.hardware_test_deadman_s() if deadman else None
        except Exception:  # noqa: BLE001 - no battery control: no dead-man step
            _LOGGER.debug("max_test: ignored failure (no battery control: no dead-man step)", exc_info=True)
            deadman, step_s = False, None
        recipe_s = None
        with contextlib.suppress(Exception):
            recipe = self.site.battery_control.recipe()
            recipe_s = None if recipe is None else recipe.test_total_s
        if isinstance(recipe_s, (int, float)) and recipe_s > MAX_TEST.total_seconds():
            # A slow recipe (Tesla's cloud): each mode waits for its re-arm and read-backs (`verify.quick_for`).
            return timedelta(seconds=recipe_s)
        if not deadman:
            return MAX_TEST
        # A long dead-man (Solis's RC Timeout in minutes): the step's own span and two minutes for the hold before it.
        long_extra = timedelta(seconds=step_s + 120.0) if isinstance(step_s, (int, float)) else DEADMAN_EXTRA
        return MAX_TEST + max(DEADMAN_EXTRA, long_extra)

    async def _async_run(self, started: datetime) -> None:
        deadline = started + self._max_test()
        running = self._running
        assert running is not None
        parts: dict[str, str | None] = running["parts"]
        # The parts run side by side (separate devices; the battery's modes alone take most of the ten minutes); a
        # part that fails ends the other at once.
        jobs = []
        if parts.get("battery", "absent") is None:
            jobs.append(self._async_battery(deadline))
        if parts.get("heat", "absent") is None:
            jobs.append(self._async_heat(deadline))
        try:
            try:
                await asyncio.gather(*jobs)
            except asyncio.CancelledError:
                await self._async_hand_back("stopped")
                raise
            except Exception:  # noqa: BLE001 - the test ends; whatever it engaged is handed back
                _LOGGER.warning("SpotNav hardware test: the run failed", exc_info=True)
                running["error"] = "run_failed"
                await self._async_hand_back("error")
        finally:
            # Whatever ended the run (done, a failure, the deadline, the person's stop, an error, a cancel): the
            # chargers are let go once the battery reads handed back (bounded by the hold's own end), and the phones
            # told are cleared.
            self._release_when_handed_back()
            self._clear_told()
        retry = int(running.get("retry") or 0)
        self._finish()
        self._retry_if_unjudged(retry, running.get("version"))
        await self._async_send_after()

    def _retry_if_unjudged(self, retry: int, version: str | None) -> None:
        """A run whose battery steps all passed or could not be judged, some not: it runs again by itself at the
        next suitable moment (up to `MAX_RETRIES` times); nothing is asked of the person."""
        record = self.record
        if record is None or record.get("stopped") is not None or record.get("error") is not None or self._closed:
            return
        battery = [step for step in record.get("steps") or () if step.get("part") == "battery"]
        statuses = {step.get("status") for step in battery}
        if "not_judgeable" not in statuses or not statuses <= {"passed", "not_judgeable", "skipped"}:
            return
        if retry >= MAX_RETRIES:
            return
        self._schedule(dt_util.now(), version, retry=retry + 1, why="not_judgeable")

    def _failed(self) -> bool:
        """Whether a part failed so far (the other then ends at once)."""
        running = self._running
        return running is not None and any(step["status"] == "failed" for step in self._steps(running, live=True))

    async def _async_battery(self, deadline: datetime) -> None:
        control = self.site.battery_control
        why = await control.async_hardware_test_start()
        assert self._running is not None
        if why is not None:
            self._running["parts"]["battery"] = why
            # No charge step to hold the chargers for.
            self._release_hold()
            return
        self._running["battery_live"] = True
        released = False
        try:
            while (progress := control.hardware_test_progress())["running"]:
                if released:
                    pass
                elif _past_charge(progress):
                    # The charge step is over and handed back: the chargers go now, not after the last mode.
                    self._release_hold()
                    released = True
                else:
                    # A charge a charger began by itself under the hold is stopped (bounded per charger).
                    self._hold().observe()
                if dt_util.now() >= deadline:
                    self._running["battery_stopped"] = "deadline"
                    await control.async_hardware_test_stop("hardware_test_deadline")
                    break
                if self._failed():
                    self._running["battery_stopped"] = "after_failure"
                    await control.async_hardware_test_stop("hardware_test_failure")
                    break
                await asyncio.sleep(POLL_S)
        finally:
            self._running["battery_live"] = False
            self._running["battery"] = control.hardware_test_progress()["report"]
        # The battery part ended (handed back by its own engine, or stopped): the chargers go once it reads handed
        # back, not after the heat. On a cancel or an error the run's own `finally` does the same.
        self._release_when_handed_back()

    async def _async_heat(self, deadline: datetime) -> None:
        heat = self.site.heat_control
        why = heat.async_test_start(dt_util.now())
        assert self._running is not None
        if why is not None:
            self._running["parts"]["heat"] = why
            return
        self._running["heat_live"] = True
        try:
            while (heat.test_view() or {}).get("phase") not in (None, "done"):
                stop = "deadline" if dt_util.now() >= deadline else "after_failure" if self._failed() else None
                if stop is not None:
                    self._running["heat_stopped"] = stop
                    heat.async_test_stop(stop)
                    # The look that gives the path back runs at once; wait for it, bounded.
                    for _ in range(20):
                        await asyncio.sleep(0.5)
                        if (heat.test_view() or {}).get("phase") == "done":
                            break
                    break
                await asyncio.sleep(POLL_S)
        finally:
            self._running["heat_live"] = False
            self._running["heat"] = heat.test_view()

    async def _async_hand_back(self, why: str) -> None:
        with contextlib.suppress(Exception):
            await self.site.battery_control.async_hardware_test_stop(f"hardware_test_{why}")
        with contextlib.suppress(Exception):
            self.site.heat_control.async_test_stop(why)

    def _steps(self, running: Mapping[str, Any], live: bool) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        parts = running.get("parts") or {}
        if "battery" in parts:
            if parts["battery"] is not None:
                steps.append({"part": "battery", "step": "battery", "status": "skipped", "reason": parts["battery"]})
            else:
                report, current, active = running.get("battery"), None, False
                if live and running.get("battery_live"):
                    progress = self.site.battery_control.hardware_test_progress()
                    report, current, active = progress["report"], progress["mode"], progress["running"]
                rows = battery_steps(report, current, active)
                if running.get("battery_stopped"):
                    for row in rows:
                        if row["status"] in ("pending", "running"):
                            row.update(status="aborted", reason=running["battery_stopped"])
                steps.extend(rows)
        if "heat" in parts:
            if parts["heat"] is not None:
                steps.append({"part": "heat", "step": "heat", "status": "skipped", "reason": parts["heat"]})
            else:
                test = running.get("heat")
                heat_live = live and bool(running.get("heat_live"))
                if heat_live:
                    test = self.site.heat_control.test_view()
                steps.extend(heat_steps(test, heat_live))
        return steps

    def _finish(self, stopped: str | None = None) -> None:
        running = self._running
        if running is None:
            return
        self._running = None
        steps = self._steps(running, live=False)
        if stopped is not None:
            for row in steps:
                if row["status"] in ("pending", "running"):
                    row.update(status="aborted", reason=stopped)
        self.record = {
            "site": self.site.entry_id,
            "started_at": running["started_at"],
            "finished_at": dt_util.now().isoformat(),
            "spotnav_version": running.get("version"),
            "firmware_key": (self.last or {}).get("firmware_key"),
            "result": result_of(steps),
            "stopped": stopped,
            "error": running.get("error"),
            "steps": steps,
            "battery": running.get("battery"),
            "heat": running.get("heat"),
        }
        self._save()
        _LOGGER.info("SpotNav hardware test: %s", self.record["result"])

    def _finish_stopped(self, why: str) -> None:
        if self._running is not None:
            self._finish(stopped=why)

    def _tester_sends(self) -> bool:
        """Whether a finished test with a battery part goes as a tester's result (`battery/tester.sends_result`)."""
        from .battery.tester import sends_result  # noqa: PLC0415 - the battery package imports this one's site

        record = self.record or {}
        if not any(step.get("part") == "battery" for step in record.get("steps") or () if isinstance(step, dict)):
            return False
        try:
            return sends_result(self.site.battery_control)
        except Exception:  # noqa: BLE001 - no battery control: no tester
            return False

    async def _async_send_after(self) -> None:
        """A test ended: a tester's result goes by itself, beta or not; otherwise the test goes while beta is on."""
        from .beta import beta_on  # noqa: PLC0415 - see `beta.py`'s imports

        if self.record is None or self._closed or not (self._tester_sends() or beta_on(self.hass)):
            return
        result = await async_send_test(self.hass, self, finishing=True)
        if result.ref is None:
            _LOGGER.info("SpotNav hardware test: the report was not sent (%s)", result.error)

    async def async_shutdown(self) -> None:
        """The site unloads: a running test ends and hands back first (before the battery's own shutdown)."""
        # The chargers unload too: their plans are not armed again from here.
        self._unloading = True
        if self._unsubscribe is not None:
            with contextlib.suppress(Exception):
                self._unsubscribe()
            self._unsubscribe = None
        await self.async_stop("unload")
        self._clear_told()
        if self._wait_touched:
            # Written now: a wait kept for the next start, or one just ended (a delayed save may not come before it).
            with contextlib.suppress(Exception):
                await self._store.async_save(self._data())
        self._closed = True
        # Ended with the run; again here in case the run never got to its end.
        self._release_hold()
        # An OCPP charger set back to operative before the site goes; one not confirmed stays in the hold's store and
        # is set back at the next start.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(RESTORE_AT_UNLOAD_S):
                await self._hold().async_restore_availability()

    # ---- views

    def view(self) -> dict[str, Any]:
        """The card's test block (`spotnav/beta`'s `test`)."""
        now = dt_util.now()
        version = None if self.last is None else self.last.get("version")
        running = self._running if self.running else None
        if running is not None:
            return {"state": "running", "blocked": "running", "next_at": None, "started_at": running["started_at"],
                    "finished_at": None, "result": None, "steps": self._steps(running, live=True),
                    "sent_ref": None}
        blocked, next_at = self.blocked(now, _current_version(self.hass, version))
        record = self.record
        if self.waiting is not None:
            # Pressed and waiting for a suitable moment: when it is likely to run (`likely_at`), nothing asked.
            likely = likely_at(now, waking_only=self._first_pass())
            return {
                "state": "waiting", "blocked": BLOCKED_WAITING, "next_at": None if likely is None else likely.isoformat(),
                "started_at": self.waiting.get("since"), "finished_at": None,
                "result": None if record is None else record.get("result"),
                "steps": [] if record is None else list(record.get("steps") or []), "sent_ref": None,
            }
        return {
            "state": "idle" if record is None else "done",
            "blocked": blocked,
            "next_at": None if next_at is None else next_at.isoformat(),
            "started_at": None if record is None else record.get("started_at"),
            "finished_at": None if record is None else record.get("finished_at"),
            "result": None if record is None else record.get("result"),
            "steps": [] if record is None else list(record.get("steps") or []),
            "sent_ref": self.sent_ref,
        }


def likely_at(now: datetime, *, waking_only: bool = False) -> datetime | None:
    """When a waiting test is likely to run, for the card: in the daytime the evening (`LIKELY_HOUR`, when the house
    draws more than the sun gives), `None` (soon) in the evening and at night; a first pass (`waking_only`) after
    `WAKING_HOURS` the next day's evening."""
    local = dt_util.as_local(now)
    evening = local.replace(hour=LIKELY_HOUR, minute=0, second=0, microsecond=0)
    if waking_only and local.hour >= WAKING_HOURS[1]:
        return evening + timedelta(days=1)
    if (6 if not waking_only else 0) <= local.hour < LIKELY_HOUR:
        return evening
    return None


def _current_version(hass: HomeAssistant, fallback: str | None) -> str | None:
    """SpotNav's version as loaded, without awaiting (the integration is loaded once a site runs)."""
    from homeassistant.loader import async_get_loaded_integration  # noqa: PLC0415

    try:
        version = async_get_loaded_integration(hass, DOMAIN).version
    except Exception:  # noqa: BLE001 - not loaded yet: the last test's version stands in
        _LOGGER.debug("current_version: ignored failure (not loaded yet: the last test's version stands in)", exc_info=True)
        return fallback
    return None if version is None else str(version)


def test_runner_for(hass: HomeAssistant, entry_id: str) -> HardwareTest | None:
    from .runtime import site_controller_for  # noqa: PLC0415 - runtime imports the site lazily too

    site = site_controller_for(hass, entry_id)
    return None if site is None else getattr(site, "hardware_test", None)


def latest_record(hass: HomeAssistant) -> dict[str, Any] | None:
    """The installation's last finished test, any site (the debug bundle's `hardware_test`)."""
    from .runtime import site_data  # noqa: PLC0415

    records = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = site_data(hass, entry.entry_id)
        runner = None if data is None else getattr(data.controller, "hardware_test", None)
        if runner is not None and runner.record is not None:
            records.append(runner.record)
    return max(records, key=lambda record: str(record.get("finished_at") or ""), default=None)


async def async_send_test(hass: HomeAssistant, runner: HardwareTest, *, finishing: bool = False) -> ReportResult:
    """Send the site's last finished test as a `beta_test` report (with the beta e-mail address while beta is on), or
    a battery tester's as a `battery_test_result` (`battery/tester.py`). `finishing`: the run's own send, at its end
    (its task is still the running one)."""
    from .beta import beta_of  # noqa: PLC0415 - see `beta.py`'s imports
    from .support_report import KIND_BETA_TEST, ReportResult, async_send_report  # noqa: PLC0415

    if runner.record is None or (runner.running and not finishing):
        return ReportResult(error=ERROR_NO_TEST)
    if runner._tester_sends():  # noqa: SLF001 - the runner's own rule
        from .battery.tester import async_send_result  # noqa: PLC0415

        result = await async_send_result(hass, runner)
        if result.ref is not None:
            runner.sent_ref = result.ref
            runner._save()  # noqa: SLF001 - the runner's own store
        return result
    beta = beta_of(hass)
    email = beta.state.email if beta is not None and beta.state.on else None
    result = await async_send_report(hass, "", email, None, kind=KIND_BETA_TEST)
    if result.ref is not None:
        runner.sent_ref = result.ref
        runner._save()  # noqa: SLF001 - the runner's own store
    return result
