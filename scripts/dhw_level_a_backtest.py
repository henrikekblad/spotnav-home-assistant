"""Backtest of the hot water's level A (`heat/dhw_level_a.py`) on a heat pump meter's 5-minute statistics and the
hour prices: how many of the heat plan's blocks the rules would have shortened, and which peaks would have gone cold.

Inputs (the JSON lists `recorder/statistics_during_period` answers): the meter's power (`period: "5minute"`,
`types: ["mean"]`, `units: {"power": "kW"}`) and the price's hour means (`period: "hour"`); optionally the relay's
history (`history/history_during_period` with `minimal_response`: `[{s, lu}]`) for the blocks that really happened.

For each whole local day: the hot-water runs as the heat shadow splits them (`meter_split`), the peaks learned on the
runs up to that day (`dhw_peaks`, defaults during the warm start), SpotNav's heat plan for the day as the shadow makes
it at midnight (`shadow_rule.band`, the default comfort settings), and the same plan under level A's rules. Each
plan's lowered runs are the blocks (three hours at most in a row, as the plan keeps them). A run the meter shows
starting inside a block is moved to the block's end, as the block would have held it; a peak is a cold risk when that
held its run into it (`dhw_level_a.cold_risk`). The relay's real closings are checked the same way on the real runs.
Nothing is written.

    python -I scripts/dhw_level_a_backtest.py stats.json prices.json [relay.json] [--tz Europe/Stockholm]
        [--warm-days N] [--min-draws N] [--eur-sek 11.0] [--add 0.9]

`--eur-sek` and `--add` turn a spot price in EUR/kWh into a buy price in SEK/kWh with the fees and tax as one fixed
adder (the plan's dear-hour bar is relative, so the adder matters).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.spotnav.heat import dhw_level_a, dhw_peaks  # noqa: E402
from custom_components.spotnav.heat.config import HeatConfig  # noqa: E402
from custom_components.spotnav.heat.meter_split import HotWaterRun, hot_water_runs, levels  # noqa: E402
from custom_components.spotnav.heat.shadow_rule import ACTION_LOWER, band, day_hours  # noqa: E402

HOUR = timedelta(hours=1)


def blocks_of(plan: list) -> list[tuple[datetime, datetime]]:
    out: list[list[datetime]] = []
    for hour in sorted(plan, key=lambda item: item.start):
        if hour.action != ACTION_LOWER:
            continue
        if out and out[-1][1] == hour.start:
            out[-1][1] = hour.start + HOUR
        else:
            out.append([hour.start, hour.start + HOUR])
    return [(start, end) for start, end in out]


def held(runs: list[HotWaterRun], blocks: list[tuple[datetime, datetime]]) -> list[HotWaterRun]:
    """The runs as the blocks would have held them: one starting inside a block starts at its end."""
    out = []
    for run in runs:
        block = next(((start, end) for start, end in blocks if start <= run.start < end), None)
        if block is None:
            out.append(run)
        else:
            out.append(HotWaterRun(block[1], block[1] + (run.end - run.start), run.kwh))
    return sorted(out, key=lambda run: run.start)


def relay_blocks(raw: list[dict], until: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    since = None
    for item in raw:
        moment = datetime.fromtimestamp(float(item["lu"]), timezone.utc)
        if item["s"] == "on" and since is None:
            since = moment
        elif item["s"] != "on" and since is not None:
            out.append((since, moment))
            since = None
    if since is not None:
        out.append((since, until))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stats")
    parser.add_argument("prices")
    parser.add_argument("relay", nargs="?")
    parser.add_argument("--tz", default="Europe/Stockholm")
    parser.add_argument("--warm-days", type=int, default=None)
    parser.add_argument("--min-draws", type=int, default=None)
    parser.add_argument("--eur-sek", type=float, default=11.0)
    parser.add_argument("--add", type=float, default=0.9)
    args = parser.parse_args()
    zone = ZoneInfo(args.tz)
    if args.warm_days is not None:
        dhw_peaks.WARM_DAYS = args.warm_days  # type: ignore[misc]
    if args.min_draws is not None:
        dhw_peaks.MIN_DRAWS = args.min_draws  # type: ignore[misc]
    periods = {
        datetime.fromtimestamp(row["start"] / 1000, timezone.utc): float(row["mean"])
        for row in json.loads(Path(args.stats).read_text())
        if row.get("mean") is not None
    }
    prices = {
        datetime.fromtimestamp(row["start"] / 1000, timezone.utc): (float(row["mean"]) * args.eur_sek + args.add) * 100.0
        for row in json.loads(Path(args.prices).read_text())
        if row.get("mean") is not None
    }
    first, last = min(periods), max(periods)
    found = levels(periods)
    runs = [] if found is None else hot_water_runs(periods, found)
    real = relay_blocks(json.loads(Path(args.relay).read_text()), last) if args.relay else []
    days: list[date] = []
    day = first.astimezone(zone).date() + timedelta(days=1)
    while datetime.combine(day + timedelta(days=1), datetime.min.time(), zone) <= last:
        days.append(day)
        day += timedelta(days=1)
    config = HeatConfig()
    totals = {"raw_blocks": 0, "shortened": 0, "cut_hours": 0, "raw_cold": 0, "a_cold": 0, "real_cold": 0, "peaks": 0}
    cutoff = dhw_peaks.standby_kwh(runs)
    print(f"meter {first.astimezone(zone):%Y-%m-%d %H:%M} to {last.astimezone(zone):%Y-%m-%d %H:%M}, runs {len(runs)}, standby cutoff {cutoff:.2f} kWh")
    for day in days:
        midnight = datetime.combine(day, datetime.min.time(), zone)
        before = [run for run in runs if run.start < midnight.astimezone(timezone.utc)]
        seen = len({run.start.astimezone(zone).date() for run in before}) or 1
        peaks = dhw_peaks.learn_peaks(dhw_peaks.draws_from_runs(before, (), cutoff), zone, seen)
        hours = day_hours(midnight.astimezone(timezone.utc), zone)
        now = midnight.astimezone(timezone.utc)
        raw, _ = band(config, hours, prices, now, zone)
        guards = dhw_level_a.guards(peaks, [day], zone, before, now, cutoff=cutoff)
        kept = dhw_level_a.kept_hours(guards, dhw_level_a.next_hygiene(before, zone, now))
        cap = dhw_level_a.max_day_hours(False)
        shaped, _ = band(config, hours, prices, now, zone, keep_normal=kept, max_day=cap)
        cuts = dhw_level_a.cuts(raw, shaped, kept, cap, zone)
        raw_blocks, a_blocks = blocks_of(raw), blocks_of(shaped)
        cut_hours = {cut.start for cut in cuts}
        shortened = sum(1 for start, end in raw_blocks if any(start <= hour < end for hour in cut_hours))
        instants = sorted(
            (ready, until) for d in (day - timedelta(days=1), day) for p in peaks for ready, until, _run in [dhw_peaks.peak_instant(p, d, zone)]
        )
        day_runs = [run for run in runs if midnight - timedelta(hours=12) <= run.start < midnight + timedelta(days=1, hours=3)]
        lines = []
        for index, (ready, _until) in enumerate(instants):
            if ready.astimezone(zone).date() != day:
                continue
            previous = instants[index - 1][1] if index > 0 else None
            raw_cold = dhw_level_a.cold_risk(ready, previous, raw_blocks, held(day_runs, raw_blocks))
            a_cold = dhw_level_a.cold_risk(ready, previous, a_blocks, held(day_runs, a_blocks))
            real_cold = dhw_level_a.cold_risk(ready, previous, real, day_runs) if real else False
            totals["raw_cold"] += raw_cold
            totals["a_cold"] += a_cold
            totals["real_cold"] += real_cold
            totals["peaks"] += 1
            lines.append(f"{ready.astimezone(zone):%H:%M}{' COLD(plan)' if raw_cold else ''}{' COLD(A)' if a_cold else ''}{' COLD(relay)' if real_cold else ''}")
        totals["raw_blocks"] += len(raw_blocks)
        totals["shortened"] += shortened
        totals["cut_hours"] += len(cuts)

        def show(blocks: list[tuple[datetime, datetime]]) -> str:
            return ", ".join(f"{s.astimezone(zone):%H}-{e.astimezone(zone):%H}" for s, e in blocks) or "-"

        runs_text = ", ".join(
            f"{run.start.astimezone(zone):%H:%M} {run.kwh:.2f}" for run in day_runs if run.start.astimezone(zone).date() == day
        )
        print(f"{day}  peaks: {', '.join(f'{p.ready_by:%H:%M}{"*" if p.learned else ""}' for p in peaks)}  runs: {runs_text or '-'}")
        print(f"            plan blocks: {show(raw_blocks)}  under level A: {show(a_blocks)}")
        print(f"            cut: {', '.join(f'{c.start.astimezone(zone):%H} {c.kind}' for c in cuts) or '-'}  peaks: {'; '.join(lines)}")
    print(
        f"days {len(days)}, plan blocks {totals['raw_blocks']}, shortened by level A {totals['shortened']} "
        f"({totals['cut_hours']} hours), peaks {totals['peaks']}, cold risks: plan {totals['raw_cold']}, "
        f"level A {totals['a_cold']}, relay's real closings {totals['real_cold']}"
    )


if __name__ == "__main__":
    main()
