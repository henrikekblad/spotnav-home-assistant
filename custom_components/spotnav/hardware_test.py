"""A person's hardware test of a site's plant ("Kör hårdvarutest" in the card's Support box, `api/beta.py`).

Started by an administrator, never on a timer, at most once a day per site (`ONCE_A_DAY`) and once more after a
SpotNav update or a changed plant firmware (`gate.firmware_key`; `allowance`). It runs the parts the site has, each
through the engine that already writes to it, with every rule of that engine, in at most `MAX_TEST` all told:

* **The home battery** (`battery/control.py`, `async_hardware_test_start`): the verification routine's modes in
  minutes (`verify.Timing.quick`): hold, a small grid charge under the HA-down fuse cap (the learned per-phase house
  peak, or its conservative default; where that leaves the balanced charge no room and the plant takes targets per
  phase, a charge of at most 1 kW on the phase with the most room, its sign and phase mapping judged), a discharge
  for the house, and the hand-back judged. Needs the battery opt-in
  and everything the card's checklist asks first (acknowledgements, controls to enable); runs under shadow only too,
  as a forced test action does.
* **The heat path** (`heat/control.py`, `async_test_start`): engaged, held a minute, released, each write read back.
  Needs heat control running (the opt-in, the check passed, no block, pause or safety reason).
* **Hot water**: a boost only where a self-ending one exists; none does on this branch, so no step is shown (the
  hot-water gate adds its step when it brings one).

**The site's chargers** (`site/charger_test_hold.py`): a test whose battery part would run does not start while a
charger on the site is on (`blocked` `car_charging`), and from its start until its battery part is over, by whatever
path, every charger is held stopped: no start of any kind goes out, a charge a charger begins by itself is stopped, an OCPP charger's
station ceiling is set to 0 A (and set back at the end, or at the next start after a restart in between), and the
battery's HA-down cap may count a held charger that cannot draw without Home Assistant at 0 A. The card asks the person
not to start a charge meanwhile.

The parts run side by side (separate devices; the battery's modes alone take about nine minutes, most of it the
executor's own re-arm after each hand-back). Any step that does not pass (a mode ignored or partial, a write not
taken or not read back) ends the test: that part hands back by its own engine and the other part is ended at once,
handing back what it engaged. The deadline does the same. The result
(`passed` when every part ran, `partial` when a part was skipped, `skipped` when none ran, `failed` when a step
failed; a partial or skipped test leaves the day's slot, `NOT_COUNTED`), the steps, each engine's own report) is kept per site, shown by the card step by step,
carried in the debug bundle as `hardware_test` (the relay flags `result` `failed`) and, while beta is on, sent by
itself as a `beta_test` report; otherwise the card offers "Skicka" (`async_send_test`).

It costs a little grid energy (the charge step: a few minutes at about 2 kW at most), said in the card's "Läs mer".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .battery.gate import mode_verdict
from .const import DOMAIN

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
#: Refusals that mean the part is not there to test (no step shown), rather than not testable now.
ABSENT: Final = frozenset({"no_battery_control", "not_enabled", "no_relay"})
#: Abort reasons that are the plant or its integration not doing as told (read back missing, write refused).
_HEAT_FAILURES: Final = ("aborted:no_read_back", "aborted:write_failed", "aborted:not_read_back")

ERROR_NO_TEST: Final = "spotnav_no_test"
#: A test whose battery part would run waits while a charger on the site is on.
BLOCKED_CAR_CHARGING: Final = "car_charging"
#: The chargers' hold ends by itself this long after the test's deadline, should nothing release it.
HOLD_SLACK: Final = timedelta(minutes=1)
#: How long the unload waits for the OCPP chargers' station ceilings to be set back (s).
RESTORE_AT_UNLOAD_S: Final = 20.0


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
    if text == "skipped":
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
    for step, mode in BATTERY_STEPS:
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
        self._closed = False
        self._unloading = False

    async def async_load(self) -> None:
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

    def _save(self) -> None:
        if not self._closed:
            self._store.async_delay_save(
                lambda: {"last": self.last, "record": self.record, "sent_ref": self.sent_ref}, 1.0
            )

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
        """Why a test cannot start now (`running`, `once_a_day`, `nothing_to_test`, `car_charging`), and when it
        may."""
        if self.running:
            return "running", None
        # Nothing to test comes first: the day's slot is no reason on a site where no test could run at all.
        parts = self._parts(now)
        if not parts or all(why is not None for why in parts.values()):
            return "nothing_to_test", None
        blocked, next_at = allowance(self._counted_last(), now, version, self._firmware_key())
        if blocked is not None:
            return blocked, next_at
        if parts.get("battery", "absent") is None and self._hold().charging_now():
            # The battery's charge step is reckoned with the site's chargers held stopped: none may run at the start.
            return BLOCKED_CAR_CHARGING, None
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
        parts = self._parts(now)
        self._running = {"started_at": now.isoformat(), "parts": parts, "battery": None, "heat": None,
                         "version": version}
        self.record = None
        self.sent_ref = None
        self._save()
        _LOGGER.info("SpotNav hardware test: started (%s)", ", ".join(f"{k}: {v or 'runs'}" for k, v in parts.items()))
        if parts.get("battery", "absent") is None:
            # Held from now until the run's end, by any path (`_async_run`'s `finally`), and never past the deadline.
            self._hold().begin(now + MAX_TEST + HOLD_SLACK)
        try:
            self._task = self.hass.async_create_background_task(self._async_run(now), f"{DOMAIN} hardware test")
        except BaseException:
            self._release_hold()
            raise
        return None

    def _release_hold(self) -> None:
        """End the chargers' hold, and arm again the plans of those whose start it refused, in the background (a run
        being cancelled cannot cut that short). Never raises."""
        try:
            hold = self._hold()
            refused = hold.release()
        except Exception:  # noqa: BLE001 - the hold ends by itself at its deadline either way
            _LOGGER.warning("SpotNav hardware test: the chargers' hold could not be ended", exc_info=True)
            return
        if self._closed or self._unloading:
            # The unload sets the OCPP ceilings back itself, awaited (`async_shutdown`); nothing is armed again.
            return
        self.hass.async_create_background_task(
            self._async_after_release(hold, refused), f"{DOMAIN} hardware test release"
        )

    @staticmethod
    async def _async_after_release(hold: Any, refused: list[str]) -> None:
        """The OCPP chargers' station ceilings set back first, then the refused plans armed again."""
        await hold.async_restore_ceilings()
        if refused:
            await hold.async_rearm_refused(refused)

    async def async_stop(self, why: str) -> None:
        """End a running test at once (everything it engaged handed back)."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        # The run's own `finally` let the chargers go; a run cancelled before it began never got there.
        self._release_hold()
        self._finish_stopped(why)

    async def _async_run(self, started: datetime) -> None:
        deadline = started + MAX_TEST
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
            # chargers are let go, after the battery was handed back.
            self._release_hold()
        self._finish()
        await self._async_send_if_beta()

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
        try:
            while control.hardware_test_progress()["running"]:
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
            # The battery part is over (handed back by its own engine): the chargers go at once, not after the heat.
            self._release_hold()

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

    async def _async_send_if_beta(self) -> None:
        from .beta import beta_on  # noqa: PLC0415 - see `beta.py`'s imports

        if self.record is None or not beta_on(self.hass) or self._closed:
            return
        result = await async_send_test(self.hass, self, finishing=True)
        if result.ref is None:
            _LOGGER.info("SpotNav hardware test: the report was not sent (%s)", result.error)

    async def async_shutdown(self) -> None:
        """The site unloads: a running test ends and hands back first (before the battery's own shutdown)."""
        # The chargers unload too: their plans are not armed again from here.
        self._unloading = True
        await self.async_stop("unload")
        self._closed = True
        # Ended with the run; again here in case the run never got to its end.
        self._release_hold()
        # An OCPP charger's station ceiling set back before the site goes; one not taken stays in the hold's store and
        # is set back at the next start.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(RESTORE_AT_UNLOAD_S):
                await self._hold().async_restore_ceilings()

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
    """Send the site's last finished test as a `beta_test` report (with the beta e-mail address while beta is on).
    `finishing`: the run's own send, at its end (its task is still the running one)."""
    from .beta import beta_of  # noqa: PLC0415 - see `beta.py`'s imports
    from .support_report import KIND_BETA_TEST, ReportResult, async_send_report  # noqa: PLC0415

    if runner.record is None or (runner.running and not finishing):
        return ReportResult(error=ERROR_NO_TEST)
    beta = beta_of(hass)
    email = beta.state.email if beta is not None and beta.state.on else None
    result = await async_send_report(hass, "", email, None, kind=KIND_BETA_TEST)
    if result.ref is not None:
        runner.sent_ref = result.ref
        runner._save()  # noqa: SLF001 - the runner's own store
    return result
