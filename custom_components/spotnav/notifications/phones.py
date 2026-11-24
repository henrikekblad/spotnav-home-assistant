"""The installation's phones and what each hears (spec `plans/v3_notifications_per_phone.md`): the model, its codec, the
one-time migration from the per-charger and heat settings, the translations for older clients, and the routing. Pure:
no Home Assistant imports.

A phone is one entry whether it reaches Home Assistant through the Companion app (`notify`, a `notify.mobile_app_*`
service), the paired SpotNav app (`app`) or both. Each phone has its own `on`, its own `events` (one set over the
installation) and the chargers it follows (`chargers`: `None` for every charger, also one added later).

Every notice goes to a phone by exactly one route (`route_of`): the SpotNav app when it has a wake-up address and can
show the notice itself (`AppLink.shows`), else the Companion service, else nowhere. So a phone with both never hears
the same notice twice.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from .settings import (
    _SERVICE,
    DEFAULT_EVENTS,
    EVENT_PLAN_AT_RISK,
    EVENT_VEHICLE_IDENTIFY,
    MOBILE_APP_PREFIX,
    NOTIFICATION_EVENTS,
    NotificationSettings,
)

#: The pool's season hint (the site's; `site_notice.py`).
EVENT_POOL_SEASON: Final = "pool_season"
#: Every event a phone can hear, in the order a client lists them: the charger's eight, then the pool's.
PHONE_EVENTS: Final = (*NOTIFICATION_EVENTS, EVENT_POOL_SEASON)
#: The heat card's events before this release (`heat_notifications`).
HEAT_EVENTS: Final = (EVENT_PLAN_AT_RISK, EVENT_POOL_SEASON)
#: A new phone's events (never a copy of another phone's).
DEFAULT_PHONE_EVENTS: Final = (*DEFAULT_EVENTS, EVENT_POOL_SEASON)

#: What the app shows for a spa's or a sauna's "not ready in time" (a `shows` word, not an event).
SHOWS_HEAT_AT_RISK: Final = "heat_at_risk"
#: What an app may say it shows itself.
APP_SHOWS: Final = (*PHONE_EVENTS, SHOWS_HEAT_AT_RISK)
#: What an app from before this release shows: the charger's events.
LEGACY_SHOWS: Final = NOTIFICATION_EVENTS

ROUTE_APP: Final = "app"
ROUTE_COMPANION: Final = "companion"
ROUTE_NONE: Final = "none"

#: An untouched Companion phone's id in a listing: this prefix and its service.
UNTOUCHED_PREFIX: Final = "notify:"
MAX_PHONES: Final = 20
MAX_NAME: Final = 60
#: The name of an app phone nothing else names (an app from before this release gives no name).
APP_NAME: Final = "SpotNav app"

_ID = re.compile(r"^ph_[0-9a-f]{12}$")
_KEY = re.compile(r"^[0-9a-f]{64}$")
#: An app's secret id as it sends it: base64url text, long enough to be unguessable.
_APP_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_REF = re.compile(r"^[A-Za-z0-9_-]{16,512}={0,2}$")


class PhoneError(ValueError):
    """A phone change that cannot be stored; the message is for the log only."""


def new_phone_id() -> str:
    return f"ph_{secrets.token_hex(6)}"


def app_key(app_id: Any) -> str:
    """The stored form of an app's secret id (its SHA-256), or a refusal for one that is not an app id."""
    if not isinstance(app_id, str) or not _APP_ID.match(app_id):
        raise PhoneError("app_id must be base64url text of 32 to 128 characters")
    return hashlib.sha256(app_id.encode()).hexdigest()


def valid_ref(ref: Any) -> bool:
    return isinstance(ref, str) and bool(_REF.match(ref))


def name_key(name: str) -> str:
    """A name as two names are compared: case and spaces folded, punctuation and underscores as spaces."""
    return " ".join(re.sub(r"[\W_]+", " ", name.casefold()).split())


def clean_name(raw: Any, fallback: str) -> str:
    if not isinstance(raw, str):
        return fallback
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in raw[: MAX_NAME * 4]).split())[:MAX_NAME]
    return text or fallback


@dataclass(frozen=True, slots=True)
class AppLink:
    """The paired SpotNav app of a phone."""

    #: SHA-256 of the app's secret `app_id`; `None` for an app from before this release (known by its `push_ref`).
    key: str | None = None
    #: The relay's wake-up reference; `None` while the app has instant notifications off.
    push_ref: str | None = None
    #: What the app shows itself (`APP_SHOWS`).
    shows: tuple[str, ...] = LEGACY_SHOWS
    #: An app from before this release: the chargers whose webhook registered its `push_ref`.
    via: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "push_ref": self.push_ref, "shows": list(self.shows), "via": list(self.via)}


@dataclass(frozen=True, slots=True)
class Phone:
    id: str
    name: str
    notify: str | None = None
    app: AppLink | None = None
    on: bool = False
    events: tuple[str, ...] = DEFAULT_PHONE_EVENTS
    #: `None`: every charger; a tuple: only those (may be empty: a phone that follows only the heat loads).
    chargers: tuple[str, ...] | None = None
    #: An administrator split this phone's app from its Companion phone ("Split"): never linked again by itself.
    apart: bool = False

    def follows(self, charger_id: str) -> bool:
        return self.chargers is None or charger_id in self.chargers

    def wants(self, event: str) -> bool:
        return self.on and event in self.events

    @property
    def instant(self) -> bool:
        return self.app is not None and self.app.push_ref is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "notify": self.notify,
            "app": None if self.app is None else self.app.as_dict(),
            "on": self.on,
            "events": list(self.events),
            "chargers": None if self.chargers is None else list(self.chargers),
            "apart": self.apart,
        }


def ordered_events(events: Iterable[str]) -> tuple[str, ...]:
    chosen = set(events)
    return tuple(event for event in PHONE_EVENTS if event in chosen)


def _strings(raw: Any) -> tuple[str, ...] | None:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return None
    return tuple(dict.fromkeys(raw))


def _read_app(raw: Any) -> AppLink | None:
    if not isinstance(raw, Mapping):
        return None
    key = raw.get("key")
    ref = raw.get("push_ref")
    shows = _strings(raw.get("shows"))
    via = _strings(raw.get("via"))
    return AppLink(
        key=key if isinstance(key, str) and _KEY.match(key) else None,
        push_ref=ref if valid_ref(ref) else None,
        shows=LEGACY_SHOWS if shows is None else tuple(item for item in APP_SHOWS if item in shows),
        via=via or (),
    )


def read_phone(raw: Any) -> Phone | None:
    """A stored phone, or `None` for one that cannot be read (left out, never a failed setup). An event this release
    does not know is read past."""
    if not isinstance(raw, Mapping):
        return None
    phone_id = raw.get("id")
    if not isinstance(phone_id, str) or not _ID.match(phone_id):
        return None
    notify = raw.get("notify")
    notify = notify if isinstance(notify, str) and _SERVICE.match(notify) else None
    app = _read_app(raw.get("app"))
    if notify is None and app is None:
        return None
    events = _strings(raw.get("events"))
    chargers = raw.get("chargers")
    return Phone(
        id=phone_id,
        name=clean_name(raw.get("name"), APP_NAME),
        notify=notify,
        app=app,
        on=raw.get("on") is True,
        events=DEFAULT_PHONE_EVENTS if events is None else ordered_events(event for event in events if event in PHONE_EVENTS),
        chargers=None if chargers is None else (_strings(chargers) or ()),
        apart=raw.get("apart") is True,
    )


def read_phones(raw: Any) -> tuple[Phone, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[Phone] = []
    seen: set[str] = set()
    for item in raw:
        phone = read_phone(item)
        if phone is not None and phone.id not in seen:
            seen.add(phone.id)
            found.append(phone)
    return tuple(found[:MAX_PHONES])


# ------------------------------------------------------------------------------------------- the app's Companion


def _ends_with_word(longer: str, shorter: str) -> bool:
    return bool(shorter) and longer.endswith(f" {shorter}")


def match_companion(
    name: str | None, model: str | None, devices: Iterable[tuple[str, Iterable[str], str | None]]
) -> str | None:
    """The service of the one Companion phone the app's phone is, else `None`. `devices` are `(service, names, model)`
    (`targets.companion_devices`). Tried in order, the first rule that matches any phone decides, and only when it
    matches exactly one: a name the same (case, spaces and punctuation folded) or the service named after it; one name
    ending with the other as a word ("Henrik N6", "N6"); the same model, from the app's `model` or its name (an older
    app sends the model as its name). Two phones of one model link nothing."""
    devices = [(service, [name_key(item) for item in names], name_key(dev_model or "")) for service, names, dev_model in devices]
    sent = name_key(name or "")
    sent_model = name_key(model or "")
    slug_service = f"{MOBILE_APP_PREFIX}{'_'.join(sent.split())}" if sent else None

    def exact(service: str, names: list[str], _model: str) -> bool:
        return bool(sent) and (sent in names or service == slug_service)

    def suffix(_service: str, names: list[str], _model: str) -> bool:
        return bool(sent) and any(_ends_with_word(sent, item) or _ends_with_word(item, sent) for item in names)

    def same_model(_service: str, _names: list[str], dev_model: str) -> bool:
        return bool(dev_model) and dev_model in (sent_model, sent)

    for rule in (exact, suffix, same_model):
        found = [service for service, names, dev_model in devices if rule(service, names, dev_model)]
        if found:
            return found[0] if len(set(found)) == 1 else None
    return None


def merge_choices(keep: Phone, other: Phone) -> Phone:
    """`keep` (its id, name, routes) with the choices of both entries of one phone: on if either is on, the union of
    the events and of the chargers (`None`, every charger, if either follows every one) of the entries that are on;
    an entry that is off adds nothing, and with both off `keep` stays as it is."""
    ons = [phone for phone in (keep, other) if phone.on]
    if not ons:
        return keep
    events = ordered_events(event for phone in ons for event in phone.events)
    if any(phone.chargers is None for phone in ons):
        chargers: tuple[str, ...] | None = None
    else:
        chargers = tuple(dict.fromkeys(charger for phone in ons for charger in phone.chargers or ()))
    return replace(keep, on=True, events=events, chargers=chargers)


# ---------------------------------------------------------------------------------------------------------- routing


def route_of(phone: Phone, needs: Iterable[str], has_service: Any) -> str:
    """The one route a notice that needs `needs` (what the app must show) takes to `phone`: the app when it has a
    wake-up address and shows all of it, else Companion when the service exists now, else none."""
    app = phone.app
    if app is not None and app.push_ref is not None and all(item in app.shows for item in needs):
        return ROUTE_APP
    if phone.notify is not None and has_service(phone.notify):
        return ROUTE_COMPANION
    return ROUTE_NONE


def normal_chargers(chargers: tuple[str, ...] | None, every: Iterable[str]) -> tuple[str, ...] | None:
    """`None` for a list that covers every charger that exists (it follows them all, also one added later)."""
    if chargers is None:
        return None
    every = tuple(every)
    if every and set(every) <= set(chargers):
        return None
    return chargers


def _with_charger(phone: Phone, charger_id: str) -> Phone:
    if phone.chargers is None or charger_id in phone.chargers:
        return phone
    return replace(phone, chargers=(*phone.chargers, charger_id))


def _without_charger(phone: Phone, charger_id: str, every: Iterable[str]) -> Phone:
    chargers = tuple(every) if phone.chargers is None else phone.chargers
    return replace(phone, chargers=tuple(charger for charger in chargers if charger != charger_id))


# ------------------------------------------------------------------------------------------------------- migration


@dataclass(frozen=True, slots=True)
class LegacyCharger:
    """What one charger chose before this release."""

    charger_id: str
    notifications: NotificationSettings
    push_ref: str | None = None
    push_events: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyHeat:
    """What one site's heat card chose before this release."""

    targets: tuple[str, ...]
    events: tuple[str, ...]


def migrate(
    chargers: Iterable[LegacyCharger],
    heats: Iterable[LegacyHeat],
    names: Mapping[str, str],
    every: Iterable[str],
    make_id: Any = new_phone_id,
) -> tuple[Phone, ...]:
    """The phones the per-charger and heat settings make (spec section 3): each phone follows the chargers that
    chose it with the union of their events; a phone only the heat card chose follows no charger; one app phone per
    wake-up reference. Nobody loses a notification; one phone's events may widen on a charger."""
    every = tuple(every)
    by_service: dict[str, Phone] = {}
    by_ref: dict[str, Phone] = {}

    def companion(service: str) -> Phone:
        found = by_service.get(service)
        if found is None:
            found = Phone(
                id=make_id(),
                name=clean_name(names.get(service), service.removeprefix(MOBILE_APP_PREFIX).replace("_", " ")),
                notify=service,
                on=True,
                events=(),
                chargers=(),
            )
        return found

    for legacy in chargers:
        for service in legacy.notifications.targets:
            phone = companion(service)
            by_service[service] = _with_charger(
                replace(phone, events=ordered_events((*phone.events, *legacy.notifications.events))), legacy.charger_id
            )
        ref = legacy.push_ref
        if ref is not None and valid_ref(ref):
            phone = by_ref.get(ref) or Phone(
                id=make_id(), name=APP_NAME, app=AppLink(push_ref=ref), on=True, events=(), chargers=()
            )
            app = phone.app or AppLink(push_ref=ref)
            phone = replace(
                phone,
                app=replace(app, via=tuple(dict.fromkeys((*app.via, legacy.charger_id)))),
                events=ordered_events((*phone.events, *legacy.push_events)),
            )
            by_ref[ref] = _with_charger(phone, legacy.charger_id)
    for heat in heats:
        for service in heat.targets:
            phone = companion(service)
            by_service[service] = replace(
                phone, events=ordered_events((*phone.events, *(event for event in heat.events if event in HEAT_EVENTS)))
            )
    found = [*by_service.values(), *by_ref.values()]
    return tuple(replace(phone, chargers=normal_chargers(phone.chargers, every)) for phone in found[:MAX_PHONES])


# --------------------------------------------------------------------------------------- older clients' fields


def charger_view(phones: Iterable[Phone], charger_id: str, url: str | None) -> NotificationSettings:
    """A charger's `notifications` field as an older client reads it: the Companion services of the phones that are
    on and follow it, the union of their charger events (the defaults when none) and the charger's own `url`."""
    chosen = [phone for phone in phones if phone.on and phone.notify is not None and phone.follows(charger_id)]
    if not chosen:
        return NotificationSettings(targets=(), events=DEFAULT_EVENTS, url=url)
    events = {event for phone in chosen for event in phone.events if event in NOTIFICATION_EVENTS}
    return NotificationSettings(
        targets=tuple(dict.fromkeys(phone.notify for phone in chosen if phone.notify is not None)),
        events=tuple(event for event in NOTIFICATION_EVENTS if event in events),
        url=url,
    )


def heat_view(phones: Iterable[Phone]) -> dict[str, Any]:
    """The heat settings' `heat_notifications` as an older card reads it."""
    chosen = [
        phone
        for phone in phones
        if phone.on and phone.notify is not None and any(event in phone.events for event in HEAT_EVENTS)
    ]
    events = {event for phone in chosen for event in phone.events if event in HEAT_EVENTS}
    return {
        "targets": [phone.notify for phone in chosen],
        "events": [event for event in HEAT_EVENTS if event in events] if chosen else list(HEAT_EVENTS),
    }


def _charger_part(phone: Phone, events: Iterable[str]) -> tuple[str, ...]:
    """`events` as the phone's charger events, its pool event kept."""
    kept = [event for event in phone.events if event not in NOTIFICATION_EVENTS]
    return ordered_events((*events, *kept))


def apply_charger_write(
    phones: list[Phone],
    charger_id: str,
    before: NotificationSettings,
    written: NotificationSettings,
    every: Iterable[str],
    make: Any,
) -> list[Phone]:
    """An older client's write of a charger's `notifications` field, applied as the change it makes against what it
    read (`before`, `charger_view`): a target added is on and follows the charger, a target removed stops following it
    (off when it then follows nothing and wants no pool event), changed events become each remaining target's charger
    events. `make(service)` gives the untouched phone of a service not stored yet."""
    every = tuple(every)
    added = [service for service in written.targets if service not in before.targets]
    removed = [service for service in before.targets if service not in written.targets]
    events_changed = set(written.events) != set(before.events)
    out = list(phones)

    def index_of(service: str) -> int | None:
        return next((i for i, phone in enumerate(out) if phone.notify == service), None)

    for service in added:
        i = index_of(service)
        if i is None:
            if len(out) >= MAX_PHONES:
                continue
            # Chosen for this charger by a client that knows only the charger's events: those, and this charger.
            out.append(replace(make(service), on=True, events=ordered_events(written.events), chargers=(charger_id,)))
            continue
        phone = out[i]
        chargers = phone.chargers if phone.on else ()
        phone = replace(phone, on=True, chargers=chargers)
        out[i] = _with_charger(replace(phone, events=_charger_part(phone, written.events)), charger_id)
    for service in removed:
        i = index_of(service)
        if i is None:
            continue
        phone = _without_charger(out[i], charger_id, every)
        if not phone.chargers and EVENT_POOL_SEASON not in phone.events:
            phone = replace(phone, on=False, chargers=None)
        out[i] = phone
    if events_changed:
        for service in written.targets:
            if service in added:
                continue
            i = index_of(service)
            if i is not None:
                out[i] = replace(out[i], events=_charger_part(out[i], written.events))
    return [replace(phone, chargers=normal_chargers(phone.chargers, every)) for phone in out]


def apply_heat_write(phones: list[Phone], raw: Any, make: Any) -> list[Phone]:
    """An older card's write of `heat_notifications` (`{targets?, events?}`, `null` for the defaults): a target added
    is on with the heat events (following no charger when it is new), a target removed loses the heat events,
    changed events become every target's heat events. Raises `PhoneError` for a body that cannot be one."""
    before = heat_view(phones)
    if raw is None:
        raw = {"targets": [], "events": list(HEAT_EVENTS)}
    if not isinstance(raw, Mapping) or set(raw) - {"targets", "events"}:
        raise PhoneError("heat_notifications must be {targets?, events?}")
    targets = raw.get("targets", before["targets"])
    events = raw.get("events", before["events"])
    if (
        not isinstance(targets, list)
        or any(not isinstance(item, str) or not _SERVICE.match(item) for item in targets)
        or len(set(targets)) != len(targets)
        or not isinstance(events, list)
        or any(item not in HEAT_EVENTS for item in events)
        or len(set(events)) != len(events)
    ):
        raise PhoneError("heat_notifications is not a list of notify services and heat events")
    out = list(phones)

    def heat_part(phone: Phone, chosen: Iterable[str]) -> tuple[str, ...]:
        # `plan_at_risk` is also a car's: a phone that follows a charger keeps it.
        kept = [event for event in phone.events if event not in HEAT_EVENTS]
        if phone.chargers != () and EVENT_PLAN_AT_RISK in phone.events:
            kept.append(EVENT_PLAN_AT_RISK)
        return ordered_events((*kept, *chosen))

    for service in targets:
        i = next((n for n, phone in enumerate(out) if phone.notify == service), None)
        if i is None:
            if len(out) >= MAX_PHONES:
                continue
            out.append(replace(make(service), on=True, events=ordered_events(events), chargers=()))
            continue
        phone = out[i]
        if not phone.on:
            phone = replace(phone, on=True, chargers=())
        out[i] = replace(phone, events=heat_part(phone, events))
    for service in before["targets"]:
        if service in targets:
            continue
        i = next((n for n, phone in enumerate(out) if phone.notify == service), None)
        if i is not None:
            phone = replace(out[i], events=heat_part(out[i], ()))
            if phone.chargers == ():
                phone = replace(phone, on=False, chargers=None)
            out[i] = phone
    return out


def apply_push_register(
    phones: list[Phone], charger_id: str, ref: str | None, events: tuple[str, ...], every: Iterable[str], make_id: Any
) -> list[Phone]:
    """An older app's `push_register` on one charger (spec section 7): the app phone with that reference takes the
    events and follows the charger (made if none); `null` takes the charger out of the older apps' `via` and clears
    the reference of one left with none."""
    every = tuple(every)
    out = list(phones)
    if ref is None:
        for i, phone in enumerate(out):
            app = phone.app
            if app is None or app.key is not None or charger_id not in app.via:
                continue
            via = tuple(item for item in app.via if item != charger_id)
            out[i] = replace(phone, app=replace(app, via=via, push_ref=app.push_ref if via else None))
        return out
    chosen = set(events)
    i = next((n for n, phone in enumerate(out) if phone.app is not None and phone.app.push_ref == ref), None)
    if i is None:
        if len(out) >= MAX_PHONES:
            return out
        out.append(Phone(id=make_id(), name=APP_NAME, app=AppLink(push_ref=ref), on=True, chargers=(charger_id,)))
        i = len(out) - 1
    elif EVENT_VEHICLE_IDENTIFY in out[i].events:
        # An older app leaves the question out for a charger that does not identify cars: never dropped by that.
        chosen.add(EVENT_VEHICLE_IDENTIFY)
    phone = out[i]
    app = phone.app or AppLink(push_ref=ref)
    via = (*app.via, charger_id) if app.key is None and charger_id not in app.via else app.via
    phone = _with_charger(replace(phone, app=replace(app, via=via), events=_charger_part(phone, chosen)), charger_id)
    out[i] = replace(phone, chargers=normal_chargers(phone.chargers, every))
    return out


# ----------------------------------------------------------------------------------------------- person's changes


def judge_changes(phone: Phone, changes: Any, every: Iterable[str]) -> Phone:
    """`phone` with `{on?, events?, chargers?}` applied, or a refusal. `chargers` names chargers that exist (`null`
    for every one)."""
    if not isinstance(changes, Mapping) or set(changes) - {"on", "events", "chargers"}:
        raise PhoneError("changes must be {on?, events?, chargers?}")
    every = tuple(every)
    if "on" in changes:
        if not isinstance(changes["on"], bool):
            raise PhoneError("on must be true or false")
        phone = replace(phone, on=changes["on"])
    if "events" in changes:
        events = changes["events"]
        if not isinstance(events, list) or any(event not in PHONE_EVENTS for event in events) or len(set(events)) != len(events):
            raise PhoneError("events must be distinct phone events")
        phone = replace(phone, events=ordered_events(events))
    if "chargers" in changes:
        chargers = changes["chargers"]
        if chargers is not None and (
            not isinstance(chargers, list)
            or any(charger not in every for charger in chargers)
            or len(set(chargers)) != len(chargers)
        ):
            raise PhoneError("chargers must be null or distinct chargers that exist")
        phone = replace(phone, chargers=normal_chargers(None if chargers is None else tuple(chargers), every))
    return phone
