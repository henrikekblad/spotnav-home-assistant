"""What a sold kWh earns per slot where the supplier publishes its own export rates.

Today only Great Britain's Octopus Energy export meter (Agile Outgoing, Flux export, Outgoing fixed) has them,
read through the person's own Octopus Energy integration (`providers/octopus.py` `OctopusProvider.export_rates`).
Where they are known they are the sell price of that slot (`battery/plan_shadow.price_slots`, `day_record`): the
export rate itself, in the area's minor unit per kWh, in place of the spot price plus the sold-kWh figure. A slot
the rates do not cover keeps spot plus the figure, as on every other price.

Pure apart from [area_export_rates], which asks the price repository; nothing is fetched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant


@dataclass(frozen=True, slots=True)
class ExportRates:
    """Export rates by slot: UTC start instant → minor units per kWh, each slot [slot] long and aligned to it in
    UTC (GB's half-hours are)."""

    rates: Mapping[datetime, float]
    slot: timedelta = timedelta(minutes=30)
    _key: tuple[Any, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_key", tuple(sorted(self.rates.items())))

    @property
    def key(self) -> tuple[Any, ...]:
        """What changes when a rate does (for a plan that recomputes only when its prices change)."""
        return self._key

    def over(self, start: datetime, end: datetime) -> float | None:
        """The rate over `[start, end)`: the time-weighted mean of the slots it overlaps, `None` unless every part of
        it is covered."""
        if end <= start:
            return None
        step = self.slot.total_seconds()
        start_s = start.timestamp()
        end_s = end.timestamp()
        moment = start_s - (start_s % step)
        total = 0.0
        while moment < end_s:
            value = self.rates.get(datetime.fromtimestamp(moment, tz=timezone.utc))
            if value is None:
                return None
            total += value * (min(end_s, moment + step) - max(start_s, moment))
            moment += step
        return total / (end_s - start_s)


def area_export_rates(hass: HomeAssistant, area_id: str | None) -> ExportRates | None:
    """The export rates the area's price provider holds now, `None` where it has none (every area but a GB region
    with an Octopus export meter). Never raises."""
    from ..runtime import domain_data  # noqa: PLC0415 - the runtime imports the pricing

    if area_id is None:
        return None
    repository = domain_data(hass).price_repository
    if repository is None:
        return None
    try:
        return repository.export_rates(area_id)
    except Exception:  # noqa: BLE001 - without them a sold kWh earns spot plus the figure, as everywhere
        return None
