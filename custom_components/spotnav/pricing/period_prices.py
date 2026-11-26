"""A period's prices as the house card's history and the heat's reviews read them: the area's held day documents
(`PriceRepository.day_snapshot`, in memory, nothing fetched) with the fees as set now.

Read by `api/house_history.py`, `api/heat_history.py`, `api/saving.py` and the heat's day reviews (`heat/runtime.py`,
`heat/dhw_runtime.py`).
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant

from ..battery.plan_shadow import PriceSlot, price_slots
from ..runtime import domain_data
from ..site.history_period import Period
from .export_rates import area_export_rates
from .price_source import site_price_source


def period_price_slots(hass: HomeAssistant, site_entry_id: str, period: Period) -> list[PriceSlot]:
    """The period's price slots with the fees as set now, from the day documents held (nothing is fetched); none
    without a price source, its area or its fees. A day either side is read too (an area on another clock). A sold
    kWh earns the supplier's export rate where one is held for the slot (`export_rates`)."""
    from ..pricing.price_points import price_points  # noqa: PLC0415 - it imports the planner
    from ..planning.planner import PlannerInputError  # noqa: PLC0415

    source = site_price_source(hass, site_entry_id)
    repository = domain_data(hass).price_repository
    if source is None or source.area is None or repository is None:
        return []
    fiscal = source.fiscal
    if fiscal is None:
        return []
    documents = []
    day = period.first - timedelta(days=1)
    while day <= period.end:
        document = repository.day_snapshot(source.area_id, day).document
        if document is not None:
            documents.append(document)
        day += timedelta(days=1)
    try:
        points = price_points(documents, currency=source.area.currency, fiscal=fiscal)
    except PlannerInputError:
        return []
    return price_slots(
        ((point.start, point.end, point.spot) for point in points),
        fiscal,
        export=area_export_rates(hass, source.area_id),
    )
