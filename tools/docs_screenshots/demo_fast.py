"""A quick battery check and quick past prices for the screenshot tool's throwaway Home Assistant.

`ha_launch.py` calls `install()` when `DOCS_FAST_CHECK` is set. In memory only, in this process: the integration's
files are untouched and a real installation never loads this. Each patch is applied the moment the integration's
module it changes has been imported.

* The demo battery obeys every command at once, so the check of its control (hold, charge, discharge, per phase,
  hand-back) has nothing to wait for: its settle and reading gaps and the re-arm pause after a hand-back are cut from
  minutes to seconds (the check takes about two minutes instead of twenty).
* The past days' exchange rates (the ECB's 90-day file, from the local stub) are read two seconds after start, not a
  minute.
* The price backfill (the past days the month's cost and earnings need) starts with the first price cycle instead of a
  minute after Home Assistant has started and, from the demo Nord Pool, asks one day after the other without the
  pause a real provider needs (it otherwise waits for the next hourly cycle).
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
from datetime import timedelta

PACKAGE = "custom_components.spotnav."


def _verify(module) -> None:
    quick = module.Timing(
        settle_s=3.0, readings=2, reading_gap_s=3.0, command_timeout_s=120.0, phase_settle_s=0.0,
        zero_settle_s=module.ZERO_SETTLE_S,
    )
    original = module.Verification.__init__

    def init(self, *args, **kwargs):
        if kwargs.get("timing") is None:
            kwargs["timing"] = quick
        original(self, *args, **kwargs)

    module.Verification.__init__ = init
    module.RATE_UP_DWELL_S = 5.0
    module.VARIANT_WAIT_S = 5.0


def _executor(module) -> None:
    # The pause that lets a hand-back take hold before the next write; the simulated plant needs none.
    module.REARM_S = 5.0
    module.RATE_UP_DWELL_S = 5.0


def _hold(module) -> None:
    module.REARM_S = 5.0


def _price_history(module) -> None:
    module.HISTORY_GAP_S = 0.1

    def arm_after_start(self) -> None:
        self._ready_at = self._now()

    module.HistoryBackfill.arm_after_start = arm_after_start


def _fx_rates(module) -> None:
    # The ECB's 90-day file (the past days' exchange rates, from the local stub) is read a moment after start.
    module.STARTUP_DELAY = timedelta(seconds=2)


PATCHES = {
    PACKAGE + "pricing.fx_rates": _fx_rates,
    PACKAGE + "battery.verify": _verify,
    PACKAGE + "battery.executor": _executor,
    PACKAGE + "battery.hold": _hold,
    PACKAGE + "pricing.price_history": _price_history,
}


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        patch = PATCHES.get(fullname)
        if patch is None:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                loader = spec.loader
                execute = loader.exec_module

                def exec_module(module, _execute=execute, _patch=patch):
                    _execute(module)
                    _patch(module)

                loader.exec_module = exec_module
                return spec
        return None


def install() -> None:
    sys.meta_path.insert(0, _Finder())
