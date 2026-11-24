"""The site's combined "not ready in time" notice (spec `plans/v3_pool_spa_spec.md` section 9, `SiteNotice`), and the
pool's season notice. One per site, owned by its `SiteCapacityController`.

**What it tells.** The site's block of what will not be ready in time (`site/not_ready.py`): the cars' departures under
the ceiling, the spa's baths and the sauna's sessions. Only the items that ask (no choice made, a choice to offer, the
deadline ahead) and, for a car, only where its notifier may tell (a car there, no person's Stop:
`ChargerNotifier.fit_may_tell`).

* **One car alone** is told as before, by its charger's notifier (`ChargerNotifier.does_not_fit`: the same words,
  buttons, tag and rules), so nothing changes where no other deadline is involved.
* **Anything more** (two cars, a car and a bath, a bath alone) is one notice per phone, under the site's tag
  (`spotnav_site_<site>_not_ready`): title "Blir inte klart i tid", one line per item ("Garage 07:00: 4 kWh saknas
  under effektmålet 6,0 kW", "Spa 19:00: når 35 °C") and the `MAX_BUTTONS` choices that free the most (the block's
  `ranked`, Android shows at most three); a tap opens the card's choice sheet (the dashboard's url with
  `#spotnav-not-ready`). The car's own notices are taken off meanwhile and its `plan_at_risk` is held back behind this
  one (`ChargerNotifier.hold_for_site`), as behind its own. Once on the phones it stays the site's until nothing asks
  (a car left alone in it is not told again on its own).
* **Recipients**: the phones that are on, want `plan_at_risk` and follow an involved charger, or any such phone where a
  bath or a session is involved (`phone_store.py`); each phone once, by one route: its SpotNav app when the app shows
  every kind in the notice (`plan_at_risk` for a car, `heat_at_risk` for a bath or a session), woken once per address,
  else its Companion service. An app from before this release shows no bath, so such a phone gets that on Companion.

**Buttons.** A car's keep its own action ids, handled by its notifier (`SPOTNAV_TAKE_WHAT_FITS_`,
`SPOTNAV_LATER_DEPARTURE_`, `SPOTNAV_IMPORT_TARGET_RELEASE_` with the charger's id). A bath's and a session's:
`SPOTNAV_SPA_TAKE_WHAT_FITS_<site>_<at>`, `SPOTNAV_LATER_BATH_<site>_<at>_<to>`, `SPOTNAV_SPA_PEAK_<site>_<at>` (the
sauna's `SPOTNAV_SAUNA_TAKE_WHAT_FITS_`, `SPOTNAV_LATER_SESSION_`, `SPOTNAV_SAUNA_PEAK_`), `at` and `to` in epoch
seconds: a press for a session that is no longer the one told, or a later time that is no longer the plan's, is
refused. "Allow the peak" frees every item under the ceiling: where one item alone is under it, that item's own id;
else `SPOTNAV_NOT_READY_PEAK_<site>_<told>` (`told` the epoch second the notice went out), which sets the ceiling
aside for each item told, refused once another notice has replaced it.

**When.** Once per deadline: a deadline joining the ones told tells the notice again under the same tag, listing all
that stand; nothing else does (a shortfall that grows, a restart). Not between `QUIET_FROM` and `QUIET_UNTIL` (22–07,
local): one that arises in the night is told at 07:00 if it still stands and a deadline is after 07:00; else never.
Taken off the phones once nothing asks (it fits, a choice was made here, in the card or in the app). At most
`HOURLY_LIMIT` an hour.

**The pool's season** (`pool_season`): once a season (the pool's shadow keeps the year told), in the daytime, to the
phones that want it (an app that shows `pool_season` is woken instead): "The water has been around 10 °C for three days – time to close the pool season?"
with "Close the season" (`SPOTNAV_POOL_SEASON_<site>`), which closes it as the card's switch does.

Kept across a restart (`spotnav.site_notice.<site>`): the deadlines told, the phones it is on and when it was told.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..planning.flexible_load import CHOICE_ACCEPT_PEAK, CHOICE_LATER, CHOICE_TAKES_WHAT_FITS
from ..texts import language_of
from .phone_store import phone_store
from .phones import EVENT_POOL_SEASON, SHOWS_HEAT_AT_RISK
from .push import async_wake_refs
from .messages import (
    does_not_fit_choice_text,
    not_ready_button,
    not_ready_car_line,
    not_ready_heat_choice,
    not_ready_heat_line,
    not_ready_load,
    not_ready_title,
    pool_season_text,
    site_peak_choice,
)
from .notifier import (
    DEFAULT_URL,
    LATER_DEPARTURE_ACTION,
    NOTIFICATION_ACTION_EVENT,
    TAKE_WHAT_FITS_ACTION,
    TARGET_RELEASE_ACTION,
    is_quiet,
    quiet_end,
)
from .settings import EVENT_PLAN_AT_RISK, NOTIFY_DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
#: Android shows at most this many buttons on a notification.
MAX_BUTTONS: Final = 3
HOURLY_LIMIT: Final = 12
#: What a tap opens on the card: its choice sheet.
SHEET_HASH: Final = "#spotnav-not-ready"

SPA_TAKE_ACTION: Final = "SPOTNAV_SPA_TAKE_WHAT_FITS_"
SAUNA_TAKE_ACTION: Final = "SPOTNAV_SAUNA_TAKE_WHAT_FITS_"
LATER_BATH_ACTION: Final = "SPOTNAV_LATER_BATH_"
LATER_SESSION_ACTION: Final = "SPOTNAV_LATER_SESSION_"
SPA_PEAK_ACTION: Final = "SPOTNAV_SPA_PEAK_"
SAUNA_PEAK_ACTION: Final = "SPOTNAV_SAUNA_PEAK_"
SITE_PEAK_ACTION: Final = "SPOTNAV_NOT_READY_PEAK_"
POOL_SEASON_ACTION: Final = "SPOTNAV_POOL_SEASON_"

#: Per heat load: its actions' prefixes by choice.
HEAT_ACTIONS: Final = {
    "spa": {CHOICE_TAKES_WHAT_FITS: SPA_TAKE_ACTION, CHOICE_LATER: LATER_BATH_ACTION, CHOICE_ACCEPT_PEAK: SPA_PEAK_ACTION},
    "sauna": {
        CHOICE_TAKES_WHAT_FITS: SAUNA_TAKE_ACTION, CHOICE_LATER: LATER_SESSION_ACTION, CHOICE_ACCEPT_PEAK: SAUNA_PEAK_ACTION
    },
}
#: The ceilings an accepted peak lifts.
_PEAK_CAUSES: Final = frozenset({"import_target", "power_level"})
#: Deadlines told are kept this long after they passed.
_KEEP: Final = timedelta(days=1)


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.site_notice.{entry_id}"


def _instant(raw: Any) -> datetime | None:
    parsed = dt_util.parse_datetime(raw) if isinstance(raw, str) else None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _epoch(moment: datetime | None) -> int:
    return 0 if moment is None else int(moment.timestamp())


def _clock(moment: datetime | None) -> str:
    return "" if moment is None else dt_util.as_local(moment).strftime("%H:%M")


def _key(item: Mapping[str, Any]) -> str:
    return f"{item['load_id']}@{item['deadline']}"


class SiteNotice:
    """The site's combined notice (see the module)."""

    def __init__(
        self,
        hass: HomeAssistant,
        site_entry_id: str,
        *,
        site: Any,
        notifier_of: Callable[[str], Any],
        recheck: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._entry_id = site_entry_id
        self._site = site
        self._notifier_of = notifier_of
        self._recheck = recheck
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(site_entry_id))
        #: The deadlines told (`load@deadline` -> deadline), the phones it is on and when it went out.
        self._told: dict[str, str] = {}
        self._phones: tuple[str, ...] = ()
        self._told_at: int = 0
        #: The items told last (for the site's "Allow the peak").
        self._items: list[dict[str, Any]] = []
        self._quiet = False
        self._timer: CALLBACK_TYPE | None = None
        self._timer_at: datetime | None = None
        self._sent: deque[datetime] = deque()
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._closed = False

    @property
    def tag(self) -> str:
        return f"spotnav_site_{self._entry_id}_not_ready"

    @property
    def on_phones(self) -> tuple[str, ...]:
        return self._phones

    async def async_load(self) -> None:
        try:
            raw = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a site never fails to start on what a notice kept
            _LOGGER.debug("SpotNav site %s: the combined notice's record could not be read", self._entry_id, exc_info=True)
            raw = None
        if isinstance(raw, dict):
            told = raw.get("told")
            if isinstance(told, dict):
                self._told = {str(k): str(v) for k, v in told.items() if isinstance(v, str)}
            phones = raw.get("phones")
            if isinstance(phones, list):
                self._phones = tuple(phone for phone in phones if isinstance(phone, str))
            stamp = raw.get("told_at")
            self._told_at = stamp if isinstance(stamp, int) and not isinstance(stamp, bool) else 0
            items = raw.get("items")
            if isinstance(items, list):
                self._items = [item for item in items if isinstance(item, dict)]

    @callback
    def async_start(self) -> None:
        self._unsubscribe = self._hass.bus.async_listen(NOTIFICATION_ACTION_EVENT, self._on_action)

    @callback
    def async_shutdown(self) -> None:
        self._closed = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._arm(None)

    def _save(self, now: datetime) -> None:
        cutoff = now - _KEEP
        self._told = {key: at for key, at in self._told.items() if (_instant(at) or now) > cutoff}
        self._store.async_delay_save(
            lambda: {"told": dict(self._told), "phones": list(self._phones), "told_at": self._told_at, "items": self._items},
            1.0,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {"told": sorted(self._told), "phones": len(self._phones), "quiet": self._quiet}

    # ------------------------------------------------------------------ deciding

    def _asking(self, block: Mapping[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
        out = []
        for item in [] if block is None else block.get("items") or ():
            if item.get("chosen") is not None or not item.get("choices"):
                continue
            moment = _instant(item.get("deadline"))
            if moment is None or moment <= now:
                continue
            if item["kind"] == "car":
                notifier = self._notifier_of(item["load_id"])
                if notifier is None or not notifier.fit_may_tell():
                    continue
            out.append(dict(item))
        return out

    def update(
        self, block: Mapping[str, Any] | None, car_blocks: Mapping[str, dict[str, Any] | None]
    ) -> dict[str, str]:
        """The site's block now (`site/not_ready.py`) and each member charger's "does not fit" block where its plan
        asks (else `None`): told, held or taken off (see the module). Answers what each charger's phones were told,
        for the site's change log: `told` or `cleared`."""
        now = dt_util.utcnow()
        asking = self._asking(block, now)
        ranked = [] if block is None else list(block.get("ranked") or ())
        single = not self._phones and len(asking) == 1 and asking[0]["kind"] == "car"
        outcomes: dict[str, str] = {}
        if not asking or single:
            # The site's notice stands no more: each car is its own notifier's again.
            if self._phones:
                self._clear(self._phones)
                for item in self._items:
                    if item.get("kind") == "car":
                        outcomes[item["load_id"]] = "cleared"
                self._phones = ()
                self._items = []
                self._save(now)
            self._quiet = False
            self._arm(None)
            for charger_entry_id, car_block in car_blocks.items():
                notifier = self._notifier_of(charger_entry_id)
                if notifier is None:
                    continue
                notifier.hold_for_site(False)
                outcome = notifier.does_not_fit(car_block, recheck=self._recheck)
                if outcome is not None:
                    outcomes[charger_entry_id] = outcome
            return outcomes
        involved = {item["load_id"] for item in asking if item["kind"] == "car"}
        for charger_entry_id, car_block in car_blocks.items():
            if charger_entry_id in involved:
                continue
            # Not in the site's notice: its own notifier is told as alone (its notice taken off once nothing asks, its
            # held plan_at_risk let go; a car away or under a Stop is its notifier's to judge).
            notifier = self._notifier_of(charger_entry_id)
            if notifier is None:
                continue
            notifier.hold_for_site(False)
            outcome = notifier.does_not_fit(car_block, recheck=self._recheck)
            if outcome is not None:
                outcomes[charger_entry_id] = outcome
        for charger_entry_id in involved:
            notifier = self._notifier_of(charger_entry_id)
            notifier.hold_for_site(True)
            # Its own notice is taken off: the site's says it.
            outcome = notifier.does_not_fit(None, recheck=self._recheck)
            if outcome is not None:
                outcomes[charger_entry_id] = outcome
        new = [item for item in asking if _key(item) not in self._told]
        if not is_quiet(now):
            self._quiet = False
            self._arm(None)
            if new and self._tell(asking, ranked, car_blocks, now):
                for item in asking:
                    if item["kind"] == "car":
                        self._notifier_of(item["load_id"]).mark_fit_told(item["deadline"])
                        outcomes[item["load_id"]] = "told"
        elif new:
            # Not in the night: at 07:00 if it still stands then and a deadline is later; else never.
            due = quiet_end(now)
            self._quiet = True
            self._arm(due if any((_instant(item["deadline"]) or now) > due for item in asking) else None)
        holds = bool(self._phones) or self._quiet
        for charger_entry_id in car_blocks:
            notifier = self._notifier_of(charger_entry_id)
            if notifier is not None:
                notifier.hold_for_site(holds and charger_entry_id in involved)
        return outcomes

    def _arm(self, due: datetime | None) -> None:
        if self._closed:
            due = None
        if due == self._timer_at:
            return
        if self._timer is not None:
            self._timer()
        self._timer = None
        self._timer_at = due
        if due is None:
            return

        @callback
        def _fire(_now: datetime) -> None:
            self._timer = None
            self._timer_at = None
            self._recheck()

        self._timer = async_track_point_in_utc_time(self._hass, _fire, due)

    # ------------------------------------------------------------------ telling

    def _within_limit(self, now: datetime) -> bool:
        while self._sent and (now - self._sent[0]).total_seconds() >= 3600:
            self._sent.popleft()
        return len(self._sent) < HOURLY_LIMIT

    def _tell(
        self,
        asking: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        car_blocks: Mapping[str, dict[str, Any] | None],
        now: datetime,
    ) -> bool:
        """Tell the notice to the phones and wake the paired app; whether either heard it."""
        cars = [item["load_id"] for item in asking if item["kind"] == "car"]
        heat = any(item["kind"] != "car" for item in asking)
        # Each phone by one route: its app where the app shows every kind in the notice, else Companion.
        needs = ([EVENT_PLAN_AT_RISK] if cars else []) + ([SHOWS_HEAT_AT_RISK] if heat else [])
        store = phone_store(self._hass)
        phones, refs = (
            ((), ()) if store is None else store.recipients(EVENT_PLAN_AT_RISK, chargers=cars, heat=heat, needs=needs)
        )
        cars_told = [notifier for notifier in (self._notifier_of(car) for car in cars) if notifier.fit_targets(now)]
        if phones and not self._within_limit(now):
            _LOGGER.debug("SpotNav site %s: hourly limit of the combined notice reached", self._entry_id)
            phones = ()
        pushed = bool(refs)
        if refs:
            self._hass.async_create_task(async_wake_refs(self._hass, refs), eager_start=True)
        if not phones and not pushed:
            return False
        # Told for these deadlines from here on, whatever else happens to them.
        for item in asking:
            self._told[_key(item)] = item["deadline"]
        dropped = tuple(phone for phone in self._phones if phone not in phones)
        if dropped:
            self._clear(dropped)
        self._phones = phones
        self._told_at = int(now.timestamp())
        self._items = [
            {"load_id": item["load_id"], "kind": item["kind"], "deadline": item["deadline"], "cause": item["cause"]}
            for item in asking
        ]
        self._save(now)
        if not phones:
            return True
        self._sent.append(now)
        for notifier in cars_told:
            notifier.fit_sent(now)
        payload = self._payload(asking, ranked, car_blocks, now)
        for target in phones:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)
        _LOGGER.info("SpotNav site %s: told that %d deadline(s) will not be ready in time", self._entry_id, len(asking))
        return True

    def _payload(
        self,
        asking: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        car_blocks: Mapping[str, dict[str, Any] | None],
        now: datetime,
    ) -> dict[str, Any]:
        language = language_of(self._hass.config.language)
        lines: list[str] = []
        names: dict[str, str] = {}
        url: str | None = None
        for item in asking:
            moment = _instant(item["deadline"])
            if item["kind"] == "car":
                notifier = self._notifier_of(item["load_id"])
                names[item["load_id"]] = notifier.name
                url = url or notifier.notifications_url
                car_block = car_blocks.get(item["load_id"]) or {}
                lines.append(
                    not_ready_car_line(
                        language,
                        notifier.name,
                        time=_clock(moment),
                        short_kwh=float(item["short_kwh"] or 0.0),
                        ceiling_kw=float(car_block.get("ceiling_kw") or 0.0),
                    )
                )
            else:
                names[item["load_id"]] = not_ready_load(language, item["kind"])
                lines.append(not_ready_heat_line(language, item["kind"], time=_clock(moment), reach_c=float(item["reach_c"] or 0.0)))
        store = phone_store(self._hass)
        url = url or (None if store is None else store.url) or DEFAULT_URL
        if "#" not in url:
            url = f"{url}{SHEET_HASH}"
        actions = self._buttons(language, asking, ranked, names)
        return {
            "title": not_ready_title(language),
            "message": "\n".join(lines),
            "data": {
                "url": url,
                "clickAction": url,
                "tag": self.tag,
                "group": "spotnav",
                "channel": "SpotNav",
                "actions": actions,
            },
        }

    def _buttons(
        self, language: str, asking: list[dict[str, Any]], ranked: list[dict[str, Any]], names: Mapping[str, str]
    ) -> list[dict[str, str]]:
        items = {item["load_id"]: item for item in asking}
        several = len(asking) > 1
        under = [item for item in asking if item["cause"] in _PEAK_CAUSES and any(c["code"] == CHOICE_ACCEPT_PEAK for c in item["choices"])]
        out: list[dict[str, str]] = []
        for entry in ranked:
            if len(out) >= MAX_BUTTONS:
                break
            code = entry.get("code")
            if code == CHOICE_ACCEPT_PEAK and entry.get("load_id") is None:
                if not under:
                    continue
                if len(under) == 1:
                    button = self._item_button(language, under[0], CHOICE_ACCEPT_PEAK)
                    if button is not None:
                        out.append(button)
                    continue
                out.append({"action": f"{SITE_PEAK_ACTION}{self._entry_id}_{self._told_at}", "title": site_peak_choice(language)})
                continue
            item = items.get(entry.get("load_id"))
            if item is None:
                continue
            button = self._item_button(language, item, code)
            if button is None:
                continue
            if several:
                button["title"] = not_ready_button(language, names[item["load_id"]], button["title"])
            out.append(button)
        return out

    def _item_button(self, language: str, item: Mapping[str, Any], code: str | None) -> dict[str, str] | None:
        choice = next((c for c in item["choices"] if c["code"] == code), None)
        if choice is None:
            return None
        stamp = _epoch(_instant(item["deadline"]))
        later = _instant(choice.get("later"))
        if item["kind"] == "car":
            charger = item["load_id"]
            if code == CHOICE_LATER:
                if later is None:
                    return None
                return {
                    "action": f"{LATER_DEPARTURE_ACTION}{charger}_{stamp}_{_epoch(later)}",
                    "title": does_not_fit_choice_text(language, code, time=_clock(later)),
                }
            if code == CHOICE_TAKES_WHAT_FITS:
                return {
                    "action": f"{TAKE_WHAT_FITS_ACTION}{charger}_{stamp}",
                    "title": does_not_fit_choice_text(language, code, kwh=choice.get("kwh")),
                }
            return {
                "action": f"{TARGET_RELEASE_ACTION}{charger}_{stamp}",
                "title": does_not_fit_choice_text(language, code, peak_kw=choice.get("peak_kw")),
            }
        prefix = HEAT_ACTIONS[item["kind"]][code]
        if code == CHOICE_LATER:
            if later is None:
                return None
            return {
                "action": f"{prefix}{self._entry_id}_{stamp}_{_epoch(later)}",
                "title": not_ready_heat_choice(language, item["kind"], code, time=_clock(later)),
            }
        if code == CHOICE_TAKES_WHAT_FITS:
            return {
                "action": f"{prefix}{self._entry_id}_{stamp}",
                "title": not_ready_heat_choice(language, item["kind"], code, reach_c=item.get("reach_c")),
            }
        return {"action": f"{prefix}{self._entry_id}_{stamp}", "title": site_peak_choice(language)}

    def _clear(self, targets: tuple[str, ...], tag: str | None = None) -> None:
        payload = {"message": "clear_notification", "data": {"tag": tag or self.tag}}
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)

    async def _async_send(self, target: str, payload: dict[str, Any]) -> bool:
        try:
            await self._hass.services.async_call(NOTIFY_DOMAIN, target, payload, blocking=True)
        except Exception as err:  # noqa: BLE001 - a phone that cannot be reached must not stop the site
            _LOGGER.warning("SpotNav could not notify %s: %s", target, type(err).__name__)
            return False
        return True

    # ------------------------------------------------------------------ the pool's season

    def pool_season(self, now: datetime) -> bool:
        """Tell the pool's season hint once (the caller keeps the season told): in the daytime, to the
        phones that want it. Whether it went to any."""
        if is_quiet(now):
            return False
        store = phone_store(self._hass)
        targets, refs = ((), ()) if store is None else store.recipients(EVENT_POOL_SEASON)
        if refs:
            self._hass.async_create_task(async_wake_refs(self._hass, refs), eager_start=True)
        if not targets or not self._within_limit(now):
            return bool(refs)
        self._sent.append(now)
        title, message, button = pool_season_text(language_of(self._hass.config.language))
        url = (None if store is None else store.url) or DEFAULT_URL
        payload = {
            "title": title,
            "message": message,
            "data": {
                "url": url,
                "clickAction": url,
                "tag": self._season_tag,
                "group": "spotnav",
                "channel": "SpotNav",
                "actions": [{"action": f"{POOL_SEASON_ACTION}{self._entry_id}", "title": button}],
            },
        }
        for target in targets:
            self._hass.async_create_task(self._async_send(target, payload), eager_start=True)
        _LOGGER.info("SpotNav site %s: told that the pool season could be closed", self._entry_id)
        return True

    @property
    def _season_tag(self) -> str:
        return f"spotnav_site_{self._entry_id}_pool_season"

    # ------------------------------------------------------------------ the buttons

    @callback
    def _on_action(self, event: Any) -> None:
        action = event.data.get("action")
        if not isinstance(action, str):
            return
        site = self._entry_id
        if action == f"{POOL_SEASON_ACTION}{site}":
            self._close_season()
            return
        prefix = f"{SITE_PEAK_ACTION}{site}_"
        if action.startswith(prefix):
            self._site_peak(action.removeprefix(prefix))
            return
        for kind, prefixes in HEAT_ACTIONS.items():
            for code, start in prefixes.items():
                head = f"{start}{site}_"
                if not action.startswith(head):
                    continue
                stamps = action.removeprefix(head).split("_")
                if len(stamps) != (2 if code == CHOICE_LATER else 1) or not all(stamp.isdigit() for stamp in stamps):
                    return
                self.heat_choice(kind, code, int(stamps[0]), int(stamps[1]) if code == CHOICE_LATER else None)
                return

    def heat_choice(self, kind: str, code: str, at: int, to: int | None = None) -> bool:
        """A bath's or a session's choice from a phone, for the session told (`at`, epoch seconds); whether taken."""
        shadow = getattr(self._site, "pool_spa", None)
        part = None if shadow is None else getattr(shadow, kind, None)
        plan = None if part is None else part.plan
        not_ready = None if plan is None else plan.not_ready
        if not_ready is None or _epoch(not_ready.at) != at:
            _LOGGER.debug("SpotNav site %s: a %s choice for a session no longer told; not taken", self._entry_id, kind)
            return False
        if code == CHOICE_TAKES_WHAT_FITS:
            part.take_what_fits(not_ready.at)
        elif code == CHOICE_LATER:
            if not_ready.later is None or _epoch(not_ready.later) != to:
                _LOGGER.debug("SpotNav site %s: the later %s told is not the one held now; not taken", self._entry_id, kind)
                return False
            part.move_later()
        else:
            if not_ready.cause not in _PEAK_CAUSES:
                return False
            part.allow_peak(not_ready.at)
        _LOGGER.info("SpotNav site %s: %s %s from a phone", self._entry_id, kind, code)
        shadow.request_look()
        self._recheck()
        return True

    def _site_peak(self, stamp: str) -> None:
        """The site's "Allow the peak" for every item told under the ceiling, while the notice told is the one standing."""
        if not stamp.isdigit() or int(stamp) != self._told_at or not self._phones:
            _LOGGER.debug("SpotNav site %s: an \"Allow the peak\" for a notice no longer standing; not taken", self._entry_id)
            return
        for item in list(self._items):
            if item.get("cause") not in _PEAK_CAUSES:
                continue
            at = _epoch(_instant(item.get("deadline")))
            if item.get("kind") == "car":
                self._site.async_release_import_target(item["load_id"], for_departure=at, source="phone")
            elif item.get("kind") in HEAT_ACTIONS:
                self.heat_choice(item["kind"], CHOICE_ACCEPT_PEAK, at)
        self._recheck()

    def _close_season(self) -> None:
        from ..api.pool_spa import pool_answer  # noqa: PLC0415 - the API imports the site

        entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return
        answer = pool_answer(self._hass, entry, {"action": "season", "open": False})
        if answer.get("ok"):
            _LOGGER.info("SpotNav site %s: the pool season closed from a phone", self._entry_id)
            store = phone_store(self._hass)
            self._clear(() if store is None else store.recipients(EVENT_POOL_SEASON)[0], self._season_tag)
