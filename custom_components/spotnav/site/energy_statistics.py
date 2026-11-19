"""The Energy dashboard's statistics as SpotNav reads them: which statistics hold the solar production and the grid
export, and their energy per period from the recorder. Found without a setting.

Read by the house card's battery SoC history (`soc_at_hour_ends`: the hours its plan's log has no level for), its
energy flow (`site/energy_flow.py`: every meter of the dashboard, `energy_flow_ids`), the
solar forecast correction (`site/solar_correction_shadow.py`: the outcome, the inverter's ceiling, an
export limit) and the house battery plan (`battery/plan_runtime.py`: the production that its own house rows, measured
net of the sun, leave out). The two pure helpers are kept apart from the two that read Home Assistant.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

from ..core.parse import is_number

_LOGGER = logging.getLogger(__name__)


def energy_statistic_ids(prefs: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The Energy dashboard's solar production and grid export statistic ids, from its preferences."""
    solar: list[str] = []
    export: list[str] = []
    sources = prefs.get("energy_sources") if isinstance(prefs, Mapping) else None
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, Mapping):
            continue
        if source.get("type") == "solar" and isinstance(source.get("stat_energy_from"), str):
            solar.append(source["stat_energy_from"])
        elif source.get("type") == "grid":
            if isinstance(source.get("stat_energy_to"), str):
                export.append(source["stat_energy_to"])
            # The format before Home Assistant 2025.12: a list of export meters.
            for flow in source.get("flow_to") or []:
                if isinstance(flow, Mapping) and isinstance(flow.get("stat_energy_to"), str):
                    export.append(flow["stat_energy_to"])
    return tuple(dict.fromkeys(solar)), tuple(dict.fromkeys(export))


@dataclass(frozen=True, slots=True)
class FlowIds:
    """Every Energy dashboard meter the house card's energy flow reads: the grid's import and export, the sun's
    production, the home battery's charge (`stat_energy_to`) and discharge (`stat_energy_from`)."""

    grid_in: tuple[str, ...] = ()
    grid_out: tuple[str, ...] = ()
    solar: tuple[str, ...] = ()
    battery_in: tuple[str, ...] = ()
    battery_out: tuple[str, ...] = ()

    def all(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.grid_in, *self.grid_out, *self.solar, *self.battery_in, *self.battery_out)))


def energy_flow_ids(prefs: Any) -> FlowIds:
    """The Energy dashboard's meters for the energy flow, from its preferences (both grid formats, as
    `energy_statistic_ids`). A battery counts only with both its meters."""
    solar, grid_out = energy_statistic_ids(prefs)
    grid_in: list[str] = []
    battery_in: list[str] = []
    battery_out: list[str] = []
    sources = prefs.get("energy_sources") if isinstance(prefs, Mapping) else None
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, Mapping):
            continue
        if source.get("type") == "grid":
            if isinstance(source.get("stat_energy_from"), str):
                grid_in.append(source["stat_energy_from"])
            for flow in source.get("flow_from") or []:
                if isinstance(flow, Mapping) and isinstance(flow.get("stat_energy_from"), str):
                    grid_in.append(flow["stat_energy_from"])
        elif (
            source.get("type") == "battery"
            and isinstance(source.get("stat_energy_from"), str)
            and isinstance(source.get("stat_energy_to"), str)
        ):
            battery_out.append(source["stat_energy_from"])
            battery_in.append(source["stat_energy_to"])
    return FlowIds(
        grid_in=tuple(dict.fromkeys(grid_in)),
        grid_out=grid_out,
        solar=solar,
        battery_in=tuple(dict.fromkeys(battery_in)),
        battery_out=tuple(dict.fromkeys(battery_out)),
    )


def summed(rows_by_id: Mapping[str, Sequence[Mapping[str, Any]]], ids: Sequence[str]) -> dict[datetime, float]:
    """The statistics' `change` per period start (UTC), summed over `ids`; a period is kept only when every id has
    it (a missing source would undercount)."""
    if not ids:
        return {}
    per_id: list[dict[datetime, float]] = []
    for statistic_id in ids:
        values: dict[datetime, float] = {}
        for row in rows_by_id.get(statistic_id) or []:
            start, change = row.get("start"), row.get("change")
            if not is_number(change):
                continue
            if isinstance(start, datetime):
                moment = start.astimezone(timezone.utc)
            elif is_number(start):
                moment = datetime.fromtimestamp(float(start), timezone.utc)
            else:
                continue
            values[moment] = max(0.0, float(change))
        per_id.append(values)
    common = set(per_id[0]).intersection(*per_id[1:])
    return {start: sum(values[start] for values in per_id) for start in sorted(common)}


def _row_start(row: Mapping[str, Any]) -> datetime | None:
    start = row.get("start")
    if isinstance(start, datetime):
        return start.astimezone(timezone.utc)
    if is_number(start):
        return datetime.fromtimestamp(float(start), timezone.utc)
    return None


def soc_at_hour_ends(
    starts: Sequence[datetime], short_rows: Sequence[Mapping[str, Any]], hour_rows: Sequence[Mapping[str, Any]]
) -> dict[datetime, float]:
    """A battery's SoC (%) at each hour's end, by the hour's start (UTC), from the recorder's statistics of its SoC
    sensor: the mean of the last 5-minute period before the hour's end (the level as the hour ends, as the battery
    plan's log records it), else the hour's mean (5-minute statistics are kept 10 days by default, hourly ones for
    good). Hours with neither, and figures outside 0-100, are left out."""
    short = {_row_start(row): row.get("mean") for row in short_rows}
    hourly = {_row_start(row): row.get("mean") for row in hour_rows}
    out: dict[datetime, float] = {}
    for start in starts:
        start = start.astimezone(timezone.utc)  # noqa: PLW2901
        for value in (short.get(start + timedelta(minutes=55)), hourly.get(start)):
            if is_number(value) and 0 <= value <= 100:
                out[start] = float(value)
                break
    return out


async def async_soc_statistics(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The SoC sensor's 5-minute and hourly means in `[start, end)`, in the recorder's executor."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    def read() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        short = statistics_during_period(hass, start, end, {entity_id}, "5minute", None, {"mean"})
        hourly = statistics_during_period(hass, start, end, {entity_id}, "hour", None, {"mean"})
        return list(short.get(entity_id) or []), list(hourly.get(entity_id) or [])

    return await get_instance(hass).async_add_executor_job(read)


async def async_mean_max_statistics(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> tuple[list[tuple[datetime, float | None, float | None]], int]:
    """A measured sensor's `(start, mean, max)` per 5 minutes in `[start, end)`, else per hour where the 5-minute
    statistics are gone (kept 10 days by default), with the period's minutes; in the recorder's executor."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    def rows(period: str) -> list[tuple[datetime, float | None, float | None]]:
        found = statistics_during_period(hass, start, end, {entity_id}, period, None, {"mean", "max"})
        out = []
        for row in found.get(entity_id) or []:
            moment = _row_start(row)
            if moment is not None:
                out.append((moment, row.get("mean"), row.get("max")))
        return out

    def read() -> tuple[list[tuple[datetime, float | None, float | None]], int]:
        short = rows("5minute")
        return (short, 5) if short else (rows("hour"), 60)

    return await get_instance(hass).async_add_executor_job(read)


async def async_energy_prefs(hass: HomeAssistant) -> Any:
    """The Energy dashboard's preferences; `None` while the Energy integration is not loaded."""
    if "energy" not in hass.config.components:
        return None
    from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415 - only when loaded

    manager = await async_get_manager(hass)
    return manager.data


async def async_energy_ids(hass: HomeAssistant) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The Energy dashboard's solar and export statistic ids; none while the Energy integration is not loaded."""
    if "energy" not in hass.config.components:
        return (), ()
    from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415 - only when loaded

    manager = await async_get_manager(hass)
    return energy_statistic_ids(manager.data)


async def async_statistics(
    hass: HomeAssistant, ids: Sequence[str], start: datetime, end: datetime, period: str
) -> dict[str, list[dict[str, Any]]]:
    """The statistics' `change` (kWh) per `period` in `[start, end)`, in the recorder's executor."""
    if not ids:
        return {}
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, set(ids), period, {"energy": "kWh"}, {"change"}
    )


def short_term_days(hass: HomeAssistant) -> int | None:
    """How many days the recorder keeps its short-term (5-minute) statistics: its `purge_keep_days` (they are purged
    with the states); `None` without a recorder."""
    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded

        days = get_instance(hass).keep_days
    except Exception:  # noqa: BLE001 - no recorder: no short-term statistics
        _LOGGER.debug("short_term_days: ignored failure (no recorder: no short-term statistics)")
        return None
    return days if isinstance(days, int) and days > 0 else None
