"""The installation's phones (`phones.py`), kept in `spotnav.notification_phones`, and every write to them.

One store for the installation (spec `plans/v3_notifications_per_phone.md` section 2): the phones and the pairing are
the installation's. Loaded at the integration's setup, where the per-charger and heat choices of the release before
are migrated once (section 3); those stay on disk untouched, so going back keeps them.

Reads are synchronous on the in-memory list (the notifier asks on every event); writes take the lock and save at once.
An app's secret id is kept hashed only, a wake-up reference never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from ..const import DOMAIN
from .phones import (
    APP_SHOWS,
    AppLink,
    LegacyCharger,
    LegacyHeat,
    MAX_PHONES,
    Phone,
    PhoneError,
    ROUTE_APP,
    ROUTE_COMPANION,
    UNTOUCHED_PREFIX,
    apply_charger_write,
    apply_heat_write,
    apply_push_register,
    charger_view,
    clean_name,
    heat_view,
    judge_changes,
    migrate,
    name_key,
    new_phone_id,
    read_phones,
    route_of,
    valid_ref,
)
from .settings import NOTIFY_DOMAIN, NotificationSettings
from .targets import available_targets

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
STORE_KEY: Final = f"{DOMAIN}.notification_phones"
#: The per-charger push registration of the release before (`push.py`), read by the migration only.
LEGACY_PUSH_KEY_PREFIX: Final = f"{DOMAIN}.push"

ERROR_UNKNOWN_PHONE: Final = "spotnav_unknown_phone"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"


class PhoneRefusal(Exception):
    """A change refused with a stable code; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def every_charger(hass: HomeAssistant) -> tuple[str, ...]:
    from ..vehicles.charger_inventory import charger_entries  # noqa: PLC0415 - it imports the API layer

    return tuple(entry.entry_id for entry in charger_entries(hass))


class PhoneStore:
    """The phones, their listing and their writes (see the module)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._phones: tuple[Phone, ...] = ()
        self._migrated = False
        self._lock = asyncio.Lock()
        #: Told when the phones change (the dashboards' setup rows, a card's read).
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ load, save, migrate

    async def async_load(self) -> None:
        try:
            raw = await self._store.async_load()
        except Exception:  # noqa: BLE001 - the integration never fails to start on what the phones kept
            _LOGGER.warning("SpotNav: the phones' notification settings could not be read", exc_info=True)
            raw = None
        if isinstance(raw, dict):
            self._phones = read_phones(raw.get("phones"))
            self._migrated = raw.get("migrated") is True

    async def _async_save(self) -> None:
        await self._store.async_save({"migrated": self._migrated, "phones": [phone.as_dict() for phone in self._phones]})
        for listener in list(self._listeners):
            listener()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    async def async_migrate(self) -> None:
        """Once per installation: the per-charger, push and heat choices of the release before become phones."""
        if self._migrated:
            return
        from ..api.heat import stored_config  # noqa: PLC0415 - the API imports the notifier
        from ..vehicles.charger_inventory import charger_entries, site_entries  # noqa: PLC0415

        auto = _auto_store(self._hass)
        chargers: list[LegacyCharger] = []
        for entry in charger_entries(self._hass):
            notifications = NotificationSettings() if auto is None else auto.settings(entry.entry_id).notifications
            ref, events = await self._legacy_push(entry.entry_id)
            chargers.append(LegacyCharger(entry.entry_id, notifications, ref, events))
        heats = []
        for site in site_entries(self._hass):
            heat = stored_config(site).heat_notifications
            heats.append(LegacyHeat(heat.targets, heat.events))
        names = dict(available_targets(self._hass))
        async with self._lock:
            migrated = migrate(chargers, heats, names, every_charger(self._hass))
            self._phones = (*self._phones, *migrated)[:MAX_PHONES]
            self._migrated = True
            await self._async_save()
        if migrated:
            _LOGGER.info("SpotNav: %d phones' notification settings carried over from the chargers", len(migrated))

    async def _legacy_push(self, charger_id: str) -> tuple[str | None, tuple[str, ...]]:
        try:
            raw = await Store(self._hass, 1, f"{LEGACY_PUSH_KEY_PREFIX}.{charger_id}").async_load()
        except Exception:  # noqa: BLE001 - a registration that cannot be read is no phone
            return None, ()
        if not isinstance(raw, dict) or not valid_ref(raw.get("push_ref")):
            return None, ()
        events = raw.get("events")
        return raw["push_ref"], tuple(event for event in events if isinstance(event, str)) if isinstance(events, list) else ()

    # ------------------------------------------------------------------ reads

    @property
    def phones(self) -> tuple[Phone, ...]:
        return self._phones

    def _has_service(self, service: str) -> bool:
        return self._hass.services.has_service(NOTIFY_DOMAIN, service)

    def _untouched(self, service: str, name: str | None = None) -> Phone:
        names = dict(available_targets(self._hass)) if name is None else {service: name}
        return Phone(
            id=f"{UNTOUCHED_PREFIX}{service}",
            name=clean_name(names.get(service), service),
            notify=service,
        )

    def listing(self) -> tuple[Phone, ...]:
        """Every phone: the stored ones, then each Companion phone nobody touched (off, the defaults)."""
        stored = {phone.notify for phone in self._phones if phone.notify is not None}
        untouched = tuple(
            self._untouched(service, name) for service, name in available_targets(self._hass) if service not in stored
        )
        return (*self._phones, *untouched)

    def phone_names(self) -> dict[str, str]:
        """The Companion phones' names by service, where Home Assistant knows them."""
        return dict(available_targets(self._hass))

    def recipients(
        self, event: str, *, charger: str | None = None, chargers: Iterable[str] = (), heat: bool = False,
        needs: Iterable[str] | None = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """`(Companion services, app wake-up references)` for one notice: the phones that are on and want `event` and
        follow `charger` (a charger's notice) or any of `chargers` or `heat` (the site's notices; both empty: every
        phone), each by its one route (`phones.route_of`); `needs` is what the app must show (the event by default)."""
        involved = tuple(chargers)
        wanted = (event,) if needs is None else tuple(needs)
        services: list[str] = []
        refs: list[str] = []
        for phone in self._phones:
            if not phone.wants(event):
                continue
            if charger is not None and not phone.follows(charger):
                continue
            if charger is None and involved and not heat and not any(phone.follows(c) for c in involved):
                continue
            route = route_of(phone, wanted, self._has_service)
            if route == ROUTE_APP and phone.app is not None and phone.app.push_ref is not None:
                refs.append(phone.app.push_ref)
            elif route == ROUTE_COMPANION and phone.notify is not None:
                services.append(phone.notify)
        return tuple(dict.fromkeys(services)), tuple(dict.fromkeys(refs))

    def companion_of(self, charger: str) -> tuple[str, ...]:
        """Every Companion service of a phone that follows `charger` (whatever it wants): where a question is taken
        off."""
        return tuple(
            dict.fromkeys(
                phone.notify for phone in self._phones if phone.notify is not None and phone.follows(charger)
            )
        )

    def app_phones_of(self, charger: str) -> tuple[Phone, ...]:
        """The phones of `charger` whose app can be woken (on, following it, with a reference)."""
        return tuple(phone for phone in self._phones if phone.on and phone.follows(charger) and phone.instant)

    def has_recipient(self, charger: str) -> bool:
        """Whether any phone that is on follows `charger` by a route that exists (the setup row "choose phones")."""
        return any(
            phone.on and phone.follows(charger) and (phone.instant or (phone.notify is not None and self._has_service(phone.notify)))
            for phone in self._phones
        )

    def charger_view(self, charger: str, url: str | None) -> NotificationSettings:
        return charger_view(self._phones, charger, url)

    def heat_view(self) -> dict[str, Any]:
        return heat_view(self._phones)

    def app_phone(self, key: str) -> Phone | None:
        return next((phone for phone in self._phones if phone.app is not None and phone.app.key == key), None)

    # ------------------------------------------------------------------ older clients' writes

    async def async_charger_write(self, charger: str, written: NotificationSettings) -> None:
        """An older client wrote a charger's `notifications` field (`phones.apply_charger_write`)."""
        async with self._lock:
            before = charger_view(self._phones, charger, written.url)
            if set(before.targets) == set(written.targets) and set(before.events) == set(written.events):
                return
            self._phones = tuple(
                apply_charger_write(
                    list(self._phones), charger, before, written, every_charger(self._hass), self._stored_untouched
                )
            )
            await self._async_save()

    async def async_heat_write(self, raw: Any) -> None:
        """An older card wrote `heat_notifications`; raises `PhoneError` for a body that cannot be one."""
        async with self._lock:
            self._phones = tuple(apply_heat_write(list(self._phones), raw, self._stored_untouched))
            await self._async_save()

    async def async_push_register(self, charger: str, ref: str | None, events: tuple[str, ...]) -> None:
        """An older app's `push_register` on `charger` (`phones.apply_push_register`)."""
        async with self._lock:
            self._phones = tuple(
                apply_push_register(list(self._phones), charger, ref, events, every_charger(self._hass), new_phone_id)
            )
            await self._async_save()

    def _stored_untouched(self, service: str) -> Phone:
        """An untouched Companion phone about to be stored: a real id."""
        return replace(self._untouched(service), id=new_phone_id())

    async def async_forget_charger(self, charger: str) -> None:
        """A charger removed for good: no phone follows it or names it any more."""
        async with self._lock:
            phones = []
            for phone in self._phones:
                if phone.chargers is not None and charger in phone.chargers:
                    phone = replace(phone, chargers=tuple(item for item in phone.chargers if item != charger))
                if phone.app is not None and charger in phone.app.via:
                    via = tuple(item for item in phone.app.via if item != charger)
                    phone = replace(phone, app=replace(phone.app, via=via, push_ref=phone.app.push_ref if via else None))
                phones.append(phone)
            if tuple(phones) != self._phones:
                self._phones = tuple(phones)
                await self._async_save()

    # ------------------------------------------------------------------ the wake-up references

    async def async_drop_ref(self, ref: str) -> None:
        """The relay no longer knows `ref`: the phone falls to Companion from the next notice on."""
        async with self._lock:
            changed = False
            phones = []
            for phone in self._phones:
                if phone.app is not None and phone.app.push_ref == ref:
                    phone = replace(phone, app=replace(phone.app, push_ref=None))
                    changed = True
                phones.append(phone)
            if changed:
                self._phones = tuple(phones)
                await self._async_save()

    async def async_clear_refs(self) -> None:
        """"Disconnect all phones": no app is woken any more (each keeps its entry for when it pairs again)."""
        async with self._lock:
            self._phones = tuple(
                replace(phone, app=replace(phone.app, push_ref=None, via=())) if phone.app is not None else phone
                for phone in self._phones
            )
            await self._async_save()

    # ------------------------------------------------------------------ the app's own phone

    def _match_companion(self, name: str, exclude: str | None = None) -> Phone | None:
        """The one Companion phone (stored without an app, or untouched) whose name is `name`, else `None`."""
        wanted = name_key(name)
        slug = slugify(name)
        found = [
            phone
            for phone in self.listing()
            if phone.notify is not None
            and phone.app is None
            and phone.id != exclude
            and (name_key(phone.name) == wanted or phone.notify == f"mobile_app_{slug}")
        ]
        return found[0] if len(found) == 1 else None

    def _store_phone(self, phone: Phone, *, replacing: str | None = None) -> None:
        kept = [item for item in self._phones if item.id not in (phone.id, replacing)]
        if len(kept) >= MAX_PHONES:
            raise PhoneRefusal(ERROR_INVALID_VALUE)
        index = next((i for i, item in enumerate(self._phones) if item.id in (phone.id, replacing)), None)
        if index is None:
            self._phones = (*kept, phone)
        else:
            out = list(self._phones)
            out[index] = phone
            self._phones = tuple(item for i, item in enumerate(out) if i == index or item.id not in (phone.id, replacing))

    def _attach_app(self, key: str, name: str, ref: str | None) -> Phone:
        """The app's own entry, made now if it has none: one from before this release with the same reference is
        adopted; else a Companion phone of the same name is linked (turned on, its choices kept); else a new one, on,
        the defaults. Not saved."""
        found = self.app_phone(key)
        if found is not None:
            return found
        if ref is not None:
            legacy = next(
                (p for p in self._phones if p.app is not None and p.app.key is None and p.app.push_ref == ref), None
            )
            if legacy is not None:
                phone = replace(legacy, app=replace(legacy.app or AppLink(), key=key, via=()), name=name)
                self._store_phone(phone)
                return phone
        match = self._match_companion(name)
        if match is not None:
            phone = replace(match, id=new_phone_id() if match.id.startswith(UNTOUCHED_PREFIX) else match.id,
                            app=AppLink(key=key), on=True)
            self._store_phone(phone, replacing=match.id)
            return phone
        phone = Phone(id=new_phone_id(), name=name, app=AppLink(key=key), on=True)
        self._store_phone(phone)
        return phone

    async def async_paired(self, key: str, device: str) -> None:
        """A pairing of an app from this release was approved: its entry, linked by name where one phone matches."""
        async with self._lock:
            try:
                self._attach_app(key, clean_name(device, "SpotNav app"), None)
            except PhoneRefusal:
                _LOGGER.warning("SpotNav: no room for another phone's notification settings")
                return
            await self._async_save()

    async def async_app_call(
        self, key: str, *, name: Any, push_ref: Any = ..., shows: Any = ..., changes: Any = None
    ) -> Phone:
        """The app's `phone_notifications`: its own entry (made if none), with the reference, what it shows and its
        changes applied. Raises `PhoneError` (nothing written) for a value that cannot be stored."""
        if push_ref is not ... and push_ref is not None and not valid_ref(push_ref):
            raise PhoneError("push_ref must be short base64url text")
        if shows is not ... and (
            not isinstance(shows, list) or any(item not in APP_SHOWS for item in shows) or len(set(shows)) != len(shows)
        ):
            raise PhoneError("shows must be distinct words the app may show")
        async with self._lock:
            before = self._phones
            try:
                phone = self._attach_app(key, clean_name(name, "SpotNav app"), None if push_ref is ... else push_ref)
                app = phone.app or AppLink(key=key)
                if push_ref is not ...:
                    app = replace(app, push_ref=push_ref)
                if shows is not ...:
                    app = replace(app, shows=tuple(item for item in APP_SHOWS if item in shows))
                phone = replace(phone, app=app)
                if changes is not None:
                    phone = judge_changes(phone, changes, every_charger(self._hass))
                self._store_phone(phone)
            except (PhoneError, PhoneRefusal):
                self._phones = before
                raise
            if self._phones != before:
                await self._async_save()
            return phone

    # ------------------------------------------------------------------ an administrator's writes

    def _find(self, phone_id: Any) -> Phone:
        phone = next((item for item in self.listing() if item.id == phone_id), None)
        if phone is None:
            raise PhoneRefusal(ERROR_UNKNOWN_PHONE)
        return phone

    async def async_update(self, phone_id: Any, changes: Any) -> None:
        """`{on?, events?, chargers?}`, or one of `link: <id>`, `unlink: true`, `forget: true` (spec section 6).
        Raises `PhoneRefusal` (nothing written)."""
        if not isinstance(changes, dict):
            raise PhoneRefusal(ERROR_INVALID_VALUE)
        actions = {key for key in ("link", "unlink", "forget") if key in changes}
        if len(actions) > 1 or (actions and len(changes) > 1):
            raise PhoneRefusal(ERROR_INVALID_VALUE)
        async with self._lock:
            before = self._phones
            phone = self._find(phone_id)
            try:
                if "link" in changes:
                    self._link(phone, self._find(changes["link"]))
                elif "unlink" in changes:
                    if changes["unlink"] is not True or phone.notify is None or phone.app is None:
                        raise PhoneRefusal(ERROR_INVALID_VALUE)
                    self._store_phone(replace(phone, notify=None))
                elif "forget" in changes:
                    if changes["forget"] is not True or phone.id.startswith(UNTOUCHED_PREFIX):
                        raise PhoneRefusal(ERROR_INVALID_VALUE)
                    # Its Companion service, if any, is listed again as untouched (off).
                    self._phones = tuple(item for item in self._phones if item.id != phone.id)
                else:
                    updated = judge_changes(phone, changes, every_charger(self._hass))
                    if updated.id.startswith(UNTOUCHED_PREFIX):
                        updated = replace(updated, id=new_phone_id())
                        self._store_phone(updated, replacing=phone.id)
                    else:
                        self._store_phone(updated)
            except PhoneError:
                self._phones = before
                raise PhoneRefusal(ERROR_INVALID_VALUE) from None
            except PhoneRefusal:
                self._phones = before
                raise
            await self._async_save()

    def _link(self, phone: Phone, other: Phone) -> None:
        """One phone of two entries: the edited one keeps its choices and takes the other's Companion service or app."""
        if phone.id == other.id:
            raise PhoneRefusal(ERROR_INVALID_VALUE)
        if phone.app is not None and phone.notify is None and other.notify is not None and other.app is None:
            merged = replace(phone, notify=other.notify)
        elif phone.notify is not None and phone.app is None and other.app is not None and other.notify is None:
            merged = replace(phone, app=other.app)
        else:
            raise PhoneRefusal(ERROR_INVALID_VALUE)
        if merged.id.startswith(UNTOUCHED_PREFIX):
            merged = replace(merged, id=new_phone_id())
        self._phones = tuple(item for item in self._phones if item.id != other.id)
        self._store_phone(merged, replacing=phone.id)


def _auto_store(hass: HomeAssistant) -> Any:
    from ..runtime import domain_data  # noqa: PLC0415 - the runtime imports the notifier

    return domain_data(hass).auto_store


def phone_store(hass: HomeAssistant) -> PhoneStore | None:
    from ..runtime import domain_data  # noqa: PLC0415

    return domain_data(hass).phones


async def async_setup_phones(hass: HomeAssistant) -> PhoneStore:
    """Create, load and migrate the installation's phones, once (before any entry is set up)."""
    from ..runtime import domain_data  # noqa: PLC0415

    data = domain_data(hass)
    if data.phones is not None:
        return data.phones
    store = PhoneStore(hass)
    await store.async_load()
    try:
        await store.async_migrate()
    except Exception:  # noqa: BLE001 - a migration that fails leaves the phones as they were, tried at the next start
        _LOGGER.warning("SpotNav: the phones' notification settings could not be carried over", exc_info=True)
    data.phones = store
    return store
