"""Backtest of the hot-water peaks learner (`heat/dhw_peaks.py`) on a heat pump meter's 5-minute statistics.

Input: the JSON list `recorder/statistics_during_period` answers for one power sensor (`period: "5minute"`, `types:
["mean"]`, `units: {"power": "kW"}`): `[{start: ms, end: ms, mean: kW}, ...]`. For each local day it finds the day's
hot-water runs as the heat shadow does (`meter_split`, levels on the day before and the day), then learns the peaks
on the runs up to that day, as the shadow would have that evening. Prints the runs and the peaks per day, and whether
the peaks stayed put. Nothing is written.

    python -I scripts/dhw_peaks_backtest.py stats.json [--tz Europe/Stockholm] [--warm-days N]

`--warm-days` and `--min-draws` lower the warm start (`WARM_DAYS`, 7) and a peak's least draws (`MIN_DRAWS`, 5) to
see what the clustering finds on a short history.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.spotnav.heat import dhw_peaks  # noqa: E402
from custom_components.spotnav.heat.meter_split import HotWaterRun, hot_water_runs, levels  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stats")
    parser.add_argument("--tz", default="Europe/Stockholm")
    parser.add_argument("--warm-days", type=int, default=None)
    parser.add_argument("--min-draws", type=int, default=None)
    args = parser.parse_args()
    zone = ZoneInfo(args.tz)
    rows = json.loads(Path(args.stats).read_text())
    periods = {
        datetime.fromtimestamp(row["start"] / 1000, timezone.utc): float(row["mean"])
        for row in rows
        if row.get("mean") is not None
    }
    if args.warm_days is not None:
        dhw_peaks.WARM_DAYS = args.warm_days  # type: ignore[misc]
    if args.min_draws is not None:
        dhw_peaks.MIN_DRAWS = args.min_draws  # type: ignore[misc]
    days = sorted({moment.astimezone(zone).date() for moment in periods})
    runs: list[HotWaterRun] = []
    seen: list[str] = []
    previous: tuple[str, ...] | None = None
    changes = 0
    for day in days:
        start = datetime(day.year, day.month, day.day, tzinfo=zone)
        end = start + timedelta(days=1)
        window = {moment: kw for moment, kw in periods.items() if start - timedelta(hours=24) <= moment < end}
        found = levels(window)
        today = [] if found is None else [run for run in hot_water_runs(window, found) if start <= run.start < end]
        runs.extend(today)
        seen.append(day.isoformat())
        recent = [run for run in runs if run.start >= end - timedelta(days=dhw_peaks.PEAK_DAYS)]
        peaks = dhw_peaks.learn_peaks(dhw_peaks.draws_from_runs(recent), zone, days_seen=min(len(seen), dhw_peaks.PEAK_DAYS))
        shown = tuple(f"{p.ready_by:%H:%M}-{p.until:%H:%M}{'*' if p.learned else ''} {p.kwh:.2f}" for p in peaks)
        if previous is not None and shown != previous:
            changes += 1
        previous = shown
        runs_text = ", ".join(
            f"{run.start.astimezone(zone):%H:%M} {round((run.end - run.start).total_seconds() / 60)}m {run.kwh:.2f}"
            + (" (standby)" if dhw_peaks.standby(run, dhw_peaks.standby_kwh(recent)) else "")
            for run in today
        )
        print(f"{day}  runs: {runs_text or '-'}")
        print(f"            peaks (* learned): {'; '.join(shown)}")
    print(
        f"days: {len(days)}, runs: {len(runs)}, standby cutoff: {dhw_peaks.standby_kwh(runs):.2f} kWh, "
        f"draws: {len(dhw_peaks.draws_from_runs(runs))}, peak changes: {changes}"
    )


if __name__ == "__main__":
    main()
