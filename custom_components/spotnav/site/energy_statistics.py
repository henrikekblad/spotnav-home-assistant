"""The Energy dashboard's statistics as SpotNav reads them: which statistics hold the solar production and the grid
export, and their energy per period from the recorder. Found without a setting.

Read by the solar forecast correction (`site/solar_correction_shadow.py`: the outcome, the inverter's ceiling, an
export limit) and the house battery plan (`battery/plan_runtime.py`: the production that its own house rows, measured
net of the sun, leave out). The two pure helpers are kept apart from the two that read Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import math
from typing import Any

from homeassistant.core import HomeAssistant


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


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
            if not _number(change):
                continue
            if isinstance(start, datetime):
                moment = start.astimezone(timezone.utc)
            elif _number(start):
                moment = datetime.fromtimestamp(float(start), timezone.utc)
            else:
                continue
            values[moment] = max(0.0, float(change))
        per_id.append(values)
    common = set(per_id[0]).intersection(*per_id[1:])
    return {start: sum(values[start] for values in per_id) for start in sorted(common)}


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
