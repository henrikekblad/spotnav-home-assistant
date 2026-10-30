"""What the charger's charge control last said, readably: the observation the hold of a charge that starts by itself
outside a planned window is decided from.

Pure: no Home Assistant imports. The hold itself (stop a charge SpotNav did not start, seen while a plan has a window
still ahead and the time is outside every window, once per plug-in; a start after it is a person's override) is the
charge-ownership core's (`core/ownership.py`, `charger_reported_on`), and so is its memory (the session's `held` and
`overridden`). A charge is *seen* when the control turns on after having been off: the core reads that from the state
before the report (`was_on`), which this class keeps between two reports.
"""

from __future__ import annotations


class WindowHold:
    """The control's last readable state: `None` until one is read, so what already runs at the start is never
    "seen" beginning."""

    def __init__(self) -> None:
        self._on: bool | None = None

    @property
    def was_on(self) -> bool | None:
        """The control's state at the last readable report (`None`: none read yet)."""
        return self._on

    def observe(self, control_on: bool | None) -> None:
        """One report of the control. `None` (unreadable: unavailable or unknown) is no observation and keeps what
        was read before."""
        if control_on is not None:
            self._on = control_on
