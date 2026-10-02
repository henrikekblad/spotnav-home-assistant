"""Hold a charge that starts by itself outside a planned window: the once-per-plug-in decision.

Pure: no Home Assistant imports. While a plan has a window still ahead and the time is outside every
window, a charge SpotNav did not start is stopped once, as soon as it is seen. The controller owns the
facts (its plan, its guard, the charger's states) and the stop; this class owns only what it
remembers between two observations, so the rule can be read, and tested, on its own.

A charge is *seen* when the charge control turns on (the controller's `charging`, or the control
itself for a charger that enables it at plug-in) after having been off. A charge is *SpotNav's* from
the moment the controller starts one, whatever started it (a window, a manual Start, a webhook, solar
or hybrid execution), until the control is seen off again or SpotNav stops it.

A session is one plug-in. After the hold, a start SpotNav did not make is respected as a person's
decision (`overridden`). The session ends when the vehicle is reported disconnected, or when the next
window starts. A charger that cannot report a vehicle (a plain switch) has only the second end.
"""

from __future__ import annotations

from typing import Final

#: Nothing to do.
NOTHING: Final = "nothing"
#: Stop the charge once: it started by itself outside the plan.
HOLD: Final = "hold"
#: The charge started again after the hold without SpotNav: a person overrode the plan.
OVERRIDE: Final = "override"


class WindowHold:
    """What the rule remembers: whether the charge is SpotNav's, whether this session was held, and
    whether a person overrode the hold.
    """

    def __init__(self) -> None:
        self.owned = False
        self.held = False
        self.overridden = False
        self._on: bool | None = None

    def baseline(self, control_on: bool) -> None:
        """Take the control's state as it is now, so what already runs is never "seen"."""
        self._on = control_on

    def spotnav_started(self) -> None:
        """SpotNav started (or took over) the charge."""
        self.owned = True

    def spotnav_stopped(self) -> None:
        """SpotNav stopped the charge."""
        self.owned = False

    def held_now(self) -> None:
        """A stop outside the window took a charge that was running: the rest of the session is
        respected.
        """
        self.held = True
        self.overridden = False

    def end_session(self) -> None:
        """The next window starts: the plug-in session is over."""
        self.held = False
        self.overridden = False

    def observe(self, *, control_on: bool, connected: bool | None, gap: bool) -> str:
        """One observation of the charger. `gap`: a window is still ahead, the time is outside every
        one, and nothing else (a pause, solar) owns the charger.
        """
        was_on = self._on
        self._on = control_on
        if connected is False:
            self.end_session()
        if not control_on:
            self.owned = False
            self.overridden = False
            return NOTHING
        if was_on is not False or not gap or self.owned:
            return NOTHING
        if self.held:
            self.overridden = True
            return OVERRIDE
        self.held = True
        return HOLD
