"""The fuse's overload episodes, counted for the year's review (`year_review/`, its "Säkringen" page): how often
SpotNav lowered a charge for an import overload instead of letting the main fuse trip.

The site hands each recompute's finding here (`observe`): whether a decision that must lower for an overload stood
(`site_capacity_controller._must_lower`: an import overload, its pause, or a lowering on the magnitude) while the site's
active control was on (with it off nothing was lowered). An episode is one or more such recomputes with no gap of
`EPISODE_GAP` or more between them, counted once, when it begins: a minute of overload read on every meter update is
one episode, not a hundred. Pure and side-effect free on control: it only remembers when the last one was seen and
tells the listener (the year store's `add_fuse`) of a new one; a listener that fails is logged and never raises into
the recompute.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Final

_LOGGER = logging.getLogger(__name__)

#: Overload recomputes closer together than this are one episode.
EPISODE_GAP: Final = timedelta(minutes=10)


class OverloadEpisodes:
    """The site's overload episodes (see the module)."""

    def __init__(self) -> None:
        self._last: datetime | None = None
        self._listener: Callable[[datetime], None] | None = None

    def set_listener(self, listener: Callable[[datetime], None] | None) -> None:
        """Who is told when an episode begins (the instant it was first seen); `None` tells nobody."""
        self._listener = listener

    def observe(self, now: datetime, lowering: bool) -> bool:
        """One recompute's finding; whether it began an episode."""
        if not lowering:
            return False
        began = self._last is None or now - self._last >= EPISODE_GAP
        self._last = now
        if began and self._listener is not None:
            try:
                self._listener(now)
            except Exception:  # noqa: BLE001 - a count must never disturb the regulator
                _LOGGER.debug("SpotNav: an overload episode could not be counted", exc_info=True)
        return began
