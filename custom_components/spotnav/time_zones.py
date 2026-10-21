"""Time zones by name, resolved off the event loop and read synchronously from a cache.

Building a `ZoneInfo` the first time reads its rules from disk, which must not happen on Home
Assistant's event loop. Every zone name SpotNav meets comes from a relay document (an area's `tz` and
`market_tz`, a publication's `tz`, a day file's or profile's zone), from Home Assistant's own
configuration, or from a constant here. Each is resolved with `dt_util.async_get_time_zone` before it
can be used: the repository resolves every zone a document names when the document is fetched or
restored, before it is parsed ([async_resolve_document], which adds the fixed ones), setup resolves the
fixed ones and Home Assistant's own ([async_resolve_defaults]), and the refresh manager resolves a zone a
subscriber names itself. The parsers, the planner and the rest then look a name up synchronously
with [zone] or [require_zone], which never touch the disk on the loop.

A name not yet in the cache:

* off the event loop (an executor job, a plain unit test) is resolved on the spot, which is safe there;
* on the event loop is never resolved there. Its resolution is scheduled and the name reads as not
  usable yet: [zone] answers `None`, which every caller already treats as "no usable zone" (a document
  naming it is refused this time and read again on the next refresh, a plan has no zone and waits),
  and [require_zone] raises [ZoneNotResolved] rather than build a naive datetime that would silently
  read as the host's local time. Neither should happen in practice, since a name reaches a caller only
  through a document that was resolved before it was parsed; a warning is logged once per name if it does.

Home Assistant's own zone and UTC are answered without a lookup.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import tzinfo
from typing import Any, Final

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

#: Zones every installation may meet before any document names them: the relay's default publication
#: clock (Brussels) and UTC.
DEFAULT_ZONES: Final = ("Europe/Brussels", "UTC")

#: The keys whose string values name a zone in a relay document (area lists, day files, profiles, and the
#: stored cache that holds them).
ZONE_KEYS: Final = frozenset({"tz", "market_tz"})

#: Bounds on what an untrusted document can add: a zone name is short, and the cache does not grow past
#: this many names (a real catalogue names a few dozen).
MAX_NAME_LENGTH: Final = 64
MAX_CACHED: Final = 512

#: Resolved names; `None` is a name known not to be a zone.
_CACHE: dict[str, tzinfo | None] = {}
#: Names whose resolution was scheduled from the loop, so it is scheduled once.
_PENDING: set[str] = set()
_TASKS: set[asyncio.Task[None]] = set()
#: Names already warned about.
_WARNED: set[str] = set()


class ZoneNotResolved(LookupError):
    """A zone needed on the event loop that was not resolved beforehand."""


def _immediate(name: str) -> tzinfo | None | bool:
    """The answer that needs no lookup, or `False` when there is none."""
    if name in _CACHE:
        return _CACHE[name]
    if name in ("UTC", "Etc/UTC"):
        return dt_util.UTC
    default = dt_util.get_default_time_zone()
    if getattr(default, "key", None) == name:
        return default
    return False


def _lookup_blocking(name: str) -> tzinfo | None:
    """`ZoneInfo(name)`, reading the disk: only ever off the event loop."""
    try:
        return dt_util.get_time_zone(name)
    except (ValueError, OSError):
        # A name that is not a key at all (an absolute path, a stray character) is not a zone either.
        return None


def _remember(name: str, value: tzinfo | None) -> None:
    if name in _CACHE or len(_CACHE) < MAX_CACHED:
        _CACHE[name] = value


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def async_resolve(names: Iterable[Any]) -> None:
    """Resolve every zone name in [names] off the loop and cache the answer; non-names are skipped."""
    for name in names:
        if not isinstance(name, str) or not name or len(name) > MAX_NAME_LENGTH:
            continue
        if _immediate(name) is not False:
            continue
        try:
            value: tzinfo | None = await dt_util.async_get_time_zone(name)
        except (ValueError, OSError):
            value = None
        _remember(name, value)


def zone_names(document: Any) -> set[str]:
    """Every string under a `tz` or `market_tz` key anywhere in a JSON document."""
    found: set[str] = set()
    stack = [document]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in ZONE_KEYS and isinstance(value, str):
                    found.add(value)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(value for value in item if isinstance(value, (dict, list)))
    return found


async def async_resolve_document(document: Any) -> None:
    """Resolve every zone a relay document (or the stored cache of them) names, before it is parsed, and
    the fixed ones beside them: an area that states no publication is read on the default clock.
    """
    await async_resolve((*DEFAULT_ZONES, *sorted(zone_names(document))))


async def async_resolve_defaults(home_zone: str | None = None) -> None:
    """The fixed zones, and Home Assistant's own when given."""
    await async_resolve((*DEFAULT_ZONES, *((home_zone,) if home_zone else ())))


def zone(name: str) -> tzinfo | None:
    """The zone called [name], or `None` when it is not one (or, on the loop, not resolved yet)."""
    if not isinstance(name, str) or not name:
        return None
    known = _immediate(name)
    if known is not False:
        return known
    if not _on_loop():
        value = _lookup_blocking(name)
        _remember(name, value)
        return value
    _schedule(name)
    return None


def require_zone(name: str) -> tzinfo:
    """The zone called [name]; raises [ZoneNotResolved] rather than answer `None`."""
    value = zone(name)
    if value is None:
        raise ZoneNotResolved(f"{name!r} is not a resolved time zone")
    return value


def _schedule(name: str) -> None:
    if name not in _WARNED:
        _WARNED.add(name)
        _LOGGER.warning("Time zone %r was needed before it was resolved; resolving it now", name)
    if name in _PENDING or len(name) > MAX_NAME_LENGTH:
        return
    _PENDING.add(name)
    task = asyncio.get_running_loop().create_task(async_resolve((name,)))
    _TASKS.add(task)

    def _done(finished: asyncio.Task[None]) -> None:
        _TASKS.discard(finished)
        _PENDING.discard(name)

    task.add_done_callback(_done)


def resolved(name: str) -> bool:
    """Whether [name] is answered without a lookup (for tests and diagnostics)."""
    return _immediate(name) is not False


def clear() -> None:
    """Forget every resolved name (tests only)."""
    _CACHE.clear()
    _PENDING.clear()
    _WARNED.clear()
