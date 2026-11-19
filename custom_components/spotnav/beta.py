"""The beta tester mode: an installation that tests SpotNav ahead of the rest (plan `plans/v3_beta_tester.md`).

**The state** (one per installation, kept in `spotnav.beta`, changed only by an administrator through
`spotnav/beta`, `api/beta.py`): `on`, when it started (`started_at`), an optional e-mail address (used only to
tell the person about a problem seen in their reports or a fix for it) and, once a period has run its full
`BETA_DAYS`, `completed`. Off by default.

**The period**: `BETA_DAYS` from the tick. When it ends the box unticks itself (`on` false, `completed` true) and
the card says one line that nothing is sent any more: no alert, no notification. Unticking ends the period at once
(nothing is sent after it); ticking again starts a new one.

**The daily report** while on: the same redacted debug bundle a person's "Send debug info" sends
(`support_report.async_send_report`, `kind` `beta_daily`, an empty message, the e-mail address when given), once a
night at a minute of its own for each installation (`night_time`: from the salted install hash, between 01:00 and
05:00 local time), so the installations do not all send at once. A report that fails (the network, the relay's
refusal or rate limit) is skipped, never queued or retried: the next night sends the next one. Nothing blocks the
event loop (the bundle is encoded in the executor, the POST is Home Assistant's shared client session), and the
timer and a report in flight are cancelled at Home Assistant's stop (`async_stop`).

**What beta opens** (`learning_gate_open`): learning gates only, never safety. A gate that waits for SpotNav to
have learned something (the hot-water shadow gate's days and peaks, learned profiles) asks
`learning_gate_open(hass, site)` and opens when it answers true; the home battery's untested recipes leave shadow
only by default on a beta site (`battery/gate.py`, `commands.BrandControl.field_tested`). Never skipped, beta or
not: fuse caps (the learned per-phase house peak and its conservative default included), write caps, the brand's
verification routine, acknowledgements, opt-in entity enabling, heat safety reasons, a person's own boost. Control a
period started stays on after it ended by itself (the owner's decision of 2026-11-19): the gate stays open once a
period was `completed`. A period ended early by unticking closes it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

# `support_report` (and through it the debug bundle, which reads every part of the integration) is imported where it
# is used: the gate accessor below is imported by those parts (`battery/control.py`, the hot-water gate).

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
STORE_KEY: Final = f"{DOMAIN}.beta"
#: How long a period lasts (days); the reports stop when it ends.
BETA_DAYS: Final = 14
#: The night the daily report is sent in: minutes after local midnight, from the start, over this span.
NIGHT_START_MIN: Final = 60
NIGHT_SPAN_MIN: Final = 240


@dataclass(frozen=True, slots=True)
class BetaState:
    """The installation's beta tester state (see the module)."""

    on: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
    email: str | None = None
    completed: bool = False
    last_daily_at: datetime | None = None
    last_daily_ref: str | None = None

    def ends_at(self) -> datetime | None:
        """When the running period ends by itself, `None` when none runs."""
        if not self.on or self.started_at is None:
            return None
        return self.started_at + timedelta(days=BETA_DAYS)

    def as_stored(self) -> dict[str, Any]:
        return {
            "on": self.on,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "email": self.email,
            "completed": self.completed,
            "last_daily_at": _iso(self.last_daily_at),
            "last_daily_ref": self.last_daily_ref,
        }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def parse_state(raw: Any) -> BetaState:
    """The stored state; anything unreadable is off (an `on` without a start is off too)."""
    if not isinstance(raw, Mapping):
        return BetaState()
    from .support_report import clean_email  # noqa: PLC0415 - see the imports

    started = _time(raw.get("started_at"))
    _valid, email = clean_email(raw.get("email"))
    return BetaState(
        on=raw.get("on") is True and started is not None,
        started_at=started,
        ended_at=_time(raw.get("ended_at")),
        email=email,
        completed=raw.get("completed") is True,
        last_daily_at=_time(raw.get("last_daily_at")),
        last_daily_ref=raw.get("last_daily_ref") if isinstance(raw.get("last_daily_ref"), str) else None,
    )


def expire(state: BetaState, now: datetime) -> BetaState:
    """The state with a period past its end ended by itself (unticked, `completed`); unchanged otherwise."""
    end = state.ends_at()
    if end is None or now < end:
        return state
    return replace(state, on=False, ended_at=end, completed=True)


def switched(state: BetaState, on: bool, now: datetime) -> BetaState:
    """Ticked (a new period from now) or unticked (ended now; an early end does not complete it)."""
    if on == state.on:
        return state
    if on:
        return replace(state, on=True, started_at=now, ended_at=None, completed=False)
    return replace(state, on=False, ended_at=now)


def night_time(install: str) -> tuple[int, int, int]:
    """The local hour, minute and second of an installation's nightly report: one of its own, from its hash."""
    try:
        seed = int(install[:12], 16)
    except ValueError:
        seed = 0
    minute = NIGHT_START_MIN + seed % NIGHT_SPAN_MIN
    return minute // 60, minute % 60, (seed // NIGHT_SPAN_MIN) % 60


def view(state: BetaState) -> dict[str, Any]:
    """The card's beta block (`spotnav/beta`): the e-mail address is the administrator's own, shown back to them."""
    end = state.ends_at()
    return {
        "on": state.on,
        "started_at": _iso(state.started_at),
        "ends_at": _iso(end),
        # The period ran its full length and unticked itself: the card's one line that nothing is sent any more.
        "ended": not state.on and state.completed,
        "email": state.email,
    }


def bundle_block(state: BetaState) -> dict[str, Any]:
    """What the debug bundle says about the mode: never the address itself."""
    return {
        "on": state.on,
        "started_at": _iso(state.started_at),
        "ends_at": _iso(state.ends_at()),
        "ended_at": _iso(state.ended_at),
        "completed": state.completed,
        "email_given": state.email is not None,
        "last_daily_at": _iso(state.last_daily_at),
        "last_daily_ref": state.last_daily_ref,
    }


Sender = Callable[..., Any]
#: `Beta.async_set`'s "the address is not changed".
UNSET: Final = object()


class Beta:
    """The installation's beta tester mode (see the module). One, in `domain_data(hass).beta`."""

    def __init__(self, hass: HomeAssistant, *, sender: Sender | None = None) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._sender = sender
        self._state = BetaState()
        self._timer_cancel: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self.install: str | None = None

    @property
    def state(self) -> BetaState:
        """The state now: a period past its end is ended here, and the change stored."""
        expired = expire(self._state, dt_util.now())
        if expired != self._state:
            _LOGGER.info("SpotNav beta: the beta period ended; no more reports are sent")
            self._state = expired
            self._save()
        return self._state

    async def async_load(self) -> None:
        """Read the stored state and arm the nightly timer. A store that cannot be read is off."""
        try:
            raw = await self._store.async_load()
        except Exception:  # noqa: BLE001 - an unreadable store is no beta tester
            _LOGGER.warning("SpotNav beta: the stored state could not be read; beta is off", exc_info=True)
            raw = None
        from .support_report import install_hash  # noqa: PLC0415 - see the imports

        self._state = parse_state(raw)
        self.install = install_hash(await instance_id.async_get(self.hass))
        _ = self.state  # a period that ended while Home Assistant was down ends now
        hour, minute, second = night_time(self.install)
        self._timer_cancel = async_track_time_change(
            self.hass, self._on_night, hour=hour, minute=minute, second=second
        )

    def _save(self) -> None:
        if not self._stopped:
            self._store.async_delay_save(self._state.as_stored, 1.0)

    async def async_set(self, *, on: bool | None = None, email: Any = UNSET) -> BetaState:
        """An administrator's change: the box (`on`) and the e-mail address (`None` clears it). Stored at once."""
        state = self.state
        now = dt_util.now()
        if on is not None:
            state = switched(state, on, now)
            if on != self._state.on:
                _LOGGER.info("SpotNav beta: %s", "a beta period started" if on else "beta was turned off")
        if email is not UNSET:
            state = replace(state, email=email if isinstance(email, str) and email else None)
        self._state = state
        if on is False and self._task is not None and not self._task.done():
            # Unticked: a report not yet on its way is not sent.
            self._task.cancel()
        await self._store.async_save(state.as_stored())
        return state

    @callback
    def _on_night(self, now: datetime) -> None:
        if self._stopped or (self._task is not None and not self._task.done()):
            return
        self._task = self.hass.async_create_background_task(self.async_daily(now), "spotnav beta daily report")

    async def async_daily(self, now: datetime) -> str | None:
        """The night's report, when beta is on and none was sent today; its reference, `None` when none went or it
        failed (skipped, not queued)."""
        state = self.state
        if not state.on:
            return None
        last = state.last_daily_at
        if last is not None and dt_util.as_local(last).date() == dt_util.as_local(now).date():
            return None
        # Counted as tried before it is sent: a failure is skipped, never tried again the same night.
        self._state = replace(state, last_daily_at=now)
        self._save()
        from .support_report import KIND_BETA_DAILY, async_send_report  # noqa: PLC0415 - see the imports

        sender = self._sender or async_send_report
        try:
            result = await sender(self.hass, "", state.email, None, kind=KIND_BETA_DAILY)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one night's report never disturbs anything else
            _LOGGER.info("SpotNav beta: the daily report could not be built or sent", exc_info=True)
            return None
        ref = getattr(result, "ref", None)
        if ref is None:
            _LOGGER.info("SpotNav beta: the daily report was not taken (%s); the next night sends the next one",
                         getattr(result, "error", None))
            return None
        if self._state.on:
            self._state = replace(self._state, last_daily_ref=ref)
            self._save()
        return ref

    async def async_stop(self) -> None:
        """Home Assistant stops: the timer ends and a report in flight is cancelled; the state is written."""
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if not self._stopped:
            await self._store.async_save(self._state.as_stored())
        self._stopped = True


def beta_of(hass: HomeAssistant) -> Beta | None:
    """The installation's beta mode, `None` before the domain set it up."""
    from .runtime import domain_data  # noqa: PLC0415 - runtime imports the platforms' modules lazily too

    return domain_data(hass).beta


def beta_on(hass: HomeAssistant) -> bool:
    """Whether a beta period runs now (the daily reports are sent, a hardware test is sent by itself)."""
    beta = beta_of(hass)
    return beta is not None and beta.state.on


def learning_gate_open(hass: HomeAssistant, site: Any = None) -> bool:
    """Whether a gate that waits for SpotNav to have learned something may open early on `site` (an entry id, a
    config entry or `None`; the mode is the installation's, so every site answers alike today): while a beta
    period runs, and after one ran its full length (control it started stays on). Never a safety rule: a caller
    asks this only for a learning gate (the module's list)."""
    beta = beta_of(hass)
    if beta is None:
        return False
    state = beta.state
    return state.on or state.completed
