"""The grid tariff beyond the fixed transfer fee, as the card and the app show and change it.

The tariff is the electricity price entry's (`planning/auto_settings.py:GridTariff`): a grid fee that is a
percentage of the spot price (`transfer_percent`), a grid fee inside a high-load window that replaces the transfer
fee there (`transfer_high`), and that window. Every charger pricing with the entry follows it.

The dashboard's optional `grid_tariff` block (`serialize_grid_tariff`), present exactly where `price_source` is
(the charger prices with an electricity price entry):

    {entry_id, write_revision, transfer_percent, transfer_high, high_load_window, demand, writable}

* `transfer_percent`/`transfer_high`: `{policy, explicit_value, effective_value, unit}`. `policy` is `off`,
  `manual` or `included` (the area's published price already holds the grid fee: locked like `transfer`, nothing
  added, whatever is stored). `explicit_value` is what is stored, `effective_value` what is added (`null` while
  off or included). `unit` is `%` for the percentage and the area's minor unit per kWh for the high-load fee.
* `high_load_window`: `{days, from_month, to_month, from_hour, to_hour}` or `null`. `days` is `all`, `weekdays`
  (Monday to Friday) or `mon_sat`; months 1-12, both included, wrapping over the new year (11 to 3 is November to
  March, 1 to 12 all year); whole hours of the area's own clock, `from_hour` 0-23 and `to_hour` 1-24, wrapping over
  midnight when `from_hour` is after `to_hour` (equal is refused). A night's hours belong to the day they start on
  (`planning/planner.py:HighLoadWindow`).
* `demand`: the demand charge and the import target, measured on the charger's site (`serialize_demand`):

      {components, available, reason, price_unit, per_component, hour, import_target_kw, effective_target_kw}

  `components` is the price entry's demand-charge components as a write states them
  (`[{window, top_n, price_per_kw: {winter, summer}}]`, at most three, empty for none; `window` `null` counts all
  hours, else it is a window of the high-load window's shape). `available` is whether the site can measure them:
  `reason` is `no_site` (the charger is in no site) or `needs_total_grid_power` (a direct site without the meter's
  total grid power), `null` while available. `price_unit` is the area's major unit per kW (`kr/kW`), `null` while
  the area is not listed. `per_component` is aligned with `components` while the site measures (else empty): each
  `{bar_kw, free_kw, price_per_kw, days_counted, peaks: [{date, start, kw}]}` for the month so far on the area's
  clock (`planning/demand_charge.py`; `price_per_kw` is this month's season's). `hour` is the clock hour running
  now, `{start, kw_so_far, kwh_so_far}`, or `null` before it has a reading: `kw_so_far` the mean import over the
  time measured so far, `kwh_so_far` the energy. `import_target_kw` is the site's import target (`null` for none),
  set in the site's settings (`import_target_kw` in `spotnav/update_entity_config`). `effective_target_kw` is the
  level the running hour is held under (`site/import_target.effective_target`): the target lifted to the month's
  free level where a demand-charge part counts the hour and the month has already reached more (the month's peak),
  else the target itself; `null` without a target or where the site cannot measure.
* `write_revision`: the price entry's write counter, which a write names as `expected_write_revision`.
* `writable`: whether this caller may change it (an administrator; the paired app).

One write, a full replacement of the three parts (and of the demand-charge components when the body names them),
compare-and-set against the entry's write counter:

* WebSocket `spotnav/update_grid_tariff {charger_id, expected_write_revision, grid_tariff}`, administrators only,
  answering `{ok: true, grid_tariff: <block>}` or a stable error code;
* the webhook action `update_grid_tariff {expected_write_revision, grid_tariff}` for its own charger, answering
  `{ok, error, field_errors, grid_tariff}` with the block re-read now.

`grid_tariff` is `{transfer_percent: {enabled, value}, transfer_high: {enabled, value}, high_load_window}` and,
optionally, `demand_components` (the list as `demand.components` states it; absent keeps the stored components, so
a writer that does not know them leaves them alone). A fee that is on needs its figure (no area suggests one); the
high-load fee needs a window. A component needs `top_n` 1, 3 or 5, a window of whole hours (or `null`) and prices
that are `null` or not negative. A tariff equal to the stored one (all off equals none stored) writes nothing and
leaves the write counter. Refusals: `invalid_fiscal` (the body, with the field that is wrong), `revision_conflict`
(the entry was written since), `spotnav_no_price_entry` (the charger prices with its own settings; the block is
absent then), and the charger lookup's codes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, Mapping

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..planning.auto_settings import (
    AutoSettingsError,
    DEMAND_COMPONENTS,
    demand_components_from,
    DemandComponent,
    FiscalOverride,
    GridTariff,
    high_load_window_dict,
    high_load_window_from,
)
from ..planning.first_run import site_for_charger
from ..pricing.price_source import async_write_price_entry, entry_source_for, PriceSource
from ..runtime import site_controller_for
from ..site.demand_tracker import DemandSnapshot
from ..site.solar_capability import REASON_NEEDS_TOTAL_GRID_POWER, solar_capability
from ..const import CONF_GRID_POWER_SOURCE, CONF_MEASUREMENT_MODE
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger
from .entity_fields import ERR_INVALID_VALUE, stored_import_target_kw

#: The charger prices with its own settings: there is no electricity price entry to hold a grid tariff.
ERROR_NO_PRICE_ENTRY: Final = "spotnav_no_price_entry"

#: The body's keys, all required.
GRID_TARIFF_KEYS: Final = frozenset({"transfer_percent", "transfer_high", "high_load_window"})
FEE_KEYS: Final = frozenset({"enabled", "value"})

POLICY_OFF: Final = "off"
POLICY_MANUAL: Final = "manual"
POLICY_INCLUDED: Final = "included"

#: Why a site cannot measure the demand charge or the import target (`demand.reason`).
DEMAND_NO_SITE: Final = "no_site"
DEMAND_NEEDS_TOTAL_GRID_POWER: Final = REASON_NEEDS_TOTAL_GRID_POWER
DEMAND_REASONS: Final = (DEMAND_NO_SITE, DEMAND_NEEDS_TOTAL_GRID_POWER)


@dataclass(frozen=True, slots=True)
class CapturedDemand:
    """The charger's site's measurement of its demand charge and import target, read once."""

    available: bool
    reason: str | None
    #: What the site's hours bill per component and the running hour; `None` while nothing is measured.
    snapshot: DemandSnapshot | None
    import_target_kw: float | None
    #: The level the running hour is held under (the target lifted to the month's free level), `None` without one.
    effective_target_kw: float | None = None


@dataclass(frozen=True, slots=True)
class CapturedGridTariff:
    """One charger's price entry's grid tariff, read once."""

    entry_id: str
    write_revision: int
    tariff: GridTariff
    #: The area's published price already includes the grid fee.
    included: bool
    #: The area's minor unit, `None` while the area is not listed.
    minor_unit: str | None
    #: The area's major unit (a demand charge's price per kW), `None` while the area is not listed.
    major_unit: str | None = None
    demand: CapturedDemand = CapturedDemand(available=False, reason=DEMAND_NO_SITE, snapshot=None, import_target_kw=None)


def capture_grid_tariff(hass: HomeAssistant, charger_id: str) -> CapturedGridTariff | None:
    """The charger's price entry's grid tariff, or `None` while it prices with its own settings."""
    source = entry_source_for(hass, charger_id)
    if source is None or source.entry_id is None:
        return None
    captured = _captured(source)
    return replace(captured, demand=capture_demand(hass, charger_id, captured.tariff.demand))


def capture_demand(hass: HomeAssistant, charger_id: str, components: tuple[DemandComponent, ...]) -> CapturedDemand:
    """What the charger's site measures for `components` and its import target (see the module)."""
    site = site_for_charger(hass, charger_id)
    if site is None:
        return CapturedDemand(available=False, reason=DEMAND_NO_SITE, snapshot=None, import_target_kw=None)
    capability = solar_capability(site.data.get(CONF_MEASUREMENT_MODE), site.data.get(CONF_GRID_POWER_SOURCE))
    target = stored_import_target_kw(site.data)
    if not capability.capable:
        return CapturedDemand(
            available=False, reason=DEMAND_NEEDS_TOTAL_GRID_POWER, snapshot=None, import_target_kw=target
        )
    controller = site_controller_for(hass, site.entry_id)
    snapshot = None if controller is None else controller.demand.snapshot(components, import_target_kw=target)
    effective = None
    if controller is not None and target is not None:
        try:
            effective = controller.effective_import_target_kw(target)
        except Exception:  # noqa: BLE001 - a figure shown only: the target itself stands in for it
            effective = target
    return CapturedDemand(
        available=True, reason=None, snapshot=snapshot, import_target_kw=target, effective_target_kw=effective
    )


def demand_needs_grid_power(captured: CapturedGridTariff | None) -> bool:
    """Whether a demand-charge component or the site's import target is set where the site cannot measure it: the
    "Get started" row pointing at the site's total grid power (`setup_checklist.py`)."""
    if captured is None or captured.demand.reason != DEMAND_NEEDS_TOTAL_GRID_POWER:
        return False
    return bool(captured.tariff.demand) or captured.demand.import_target_kw is not None


def _captured(source: PriceSource) -> CapturedGridTariff:
    assert source.entry_id is not None
    area = source.area
    return CapturedGridTariff(
        entry_id=source.entry_id,
        write_revision=source.write_revision,
        tariff=source.overrides.grid_tariff or GridTariff(),
        included=area is not None and area.includes("grid_fee"),
        minor_unit=None if area is None or not area.minor_unit else area.minor_unit,
        major_unit=None if area is None or not area.major_unit else area.major_unit,
    )


def _fee(fee: FiscalOverride, unit: str | None, included: bool) -> dict[str, Any]:
    if included:
        policy = POLICY_INCLUDED
    else:
        policy = POLICY_MANUAL if fee.enabled else POLICY_OFF
    return {
        "policy": policy,
        "explicit_value": fee.value,
        "effective_value": fee.value if policy == POLICY_MANUAL else None,
        "unit": unit,
    }


def _kw(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def serialize_demand(captured: CapturedGridTariff) -> dict[str, Any]:
    """The block's `demand` (see the module)."""
    tariff = captured.tariff
    demand = captured.demand
    snapshot = demand.snapshot
    per_component: list[dict[str, Any]] = []
    hour: dict[str, Any] | None = None
    if snapshot is not None:
        for month in snapshot.components:
            per_component.append(
                {
                    "bar_kw": _kw(month.bar_kw),
                    "free_kw": _kw(month.free_kw),
                    "price_per_kw": month.price_per_kw,
                    "days_counted": month.days_counted,
                    "peaks": [
                        {"date": peak.day.isoformat(), "start": peak.start.isoformat(), "kw": _kw(peak.kw)}
                        for peak in month.peaks
                    ],
                }
            )
        running = snapshot.running
        mean = None if running is None else running.mean_kw()
        if running is not None and mean is not None:
            hour = {
                "start": running.start.isoformat(),
                "kw_so_far": _kw(mean),
                "kwh_so_far": _kw(running.import_wh / 1000),
            }
    return {
        "components": [component.as_dict() for component in tariff.demand],
        "available": demand.available,
        "reason": demand.reason,
        "price_unit": None if captured.major_unit is None else f"{captured.major_unit}/kW",
        "per_component": per_component,
        "hour": hour,
        "import_target_kw": demand.import_target_kw,
        "effective_target_kw": _kw(demand.effective_target_kw),
    }


def serialize_grid_tariff(grid_tariff: CapturedGridTariff | None, *, can_act: bool) -> dict[str, Any] | None:
    """The dashboard's `grid_tariff` block (see the module), or `None` for none."""
    if grid_tariff is None:
        return None
    captured = grid_tariff
    tariff = captured.tariff
    window = tariff.high_load_window
    return {
        "entry_id": captured.entry_id,
        "write_revision": captured.write_revision,
        "transfer_percent": _fee(tariff.transfer_percent, "%", captured.included),
        "transfer_high": _fee(
            tariff.transfer_high,
            None if captured.minor_unit is None else f"{captured.minor_unit}/kWh",
            captured.included,
        ),
        "high_load_window": None if window is None else high_load_window_dict(window),
        "demand": serialize_demand(captured),
        "writable": bool(can_act),
    }


class GridTariffRefusal(Exception):
    """A stable-code refusal of a write; nothing was written."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: The request field that is not of its type, else `None`.
        self.field = field


def _invalid(message: str, field: str = "grid_tariff") -> GridTariffRefusal:
    return GridTariffRefusal("invalid_fiscal", message, field)


def _decode_fee(raw: Any, name: str) -> FiscalOverride:
    if not isinstance(raw, Mapping) or set(raw) != FEE_KEYS:
        raise _invalid(f"{name} must be exactly {{enabled, value}}", name)
    enabled, value = raw["enabled"], raw["value"]
    if not isinstance(enabled, bool):
        raise _invalid(f"{name}.enabled must be a boolean", name)
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise _invalid(f"{name}.value must be a number or null", name)
    try:
        return FiscalOverride(enabled=enabled, value=None if value is None else float(value)).validated(name)
    except AutoSettingsError as err:
        raise _invalid(str(err), name) from err


def decode_grid_tariff(raw: Any) -> tuple[GridTariff, bool]:
    """A write's `grid_tariff`: exactly its three parts and optionally `demand_components`, validated, or
    `invalid_fiscal`. Also whether the body named the components (absent keeps the stored ones)."""
    keys = set(raw) if isinstance(raw, Mapping) else set()
    if not isinstance(raw, Mapping) or keys - {DEMAND_COMPONENTS} != GRID_TARIFF_KEYS:
        raise _invalid(f"grid_tariff must be exactly {sorted(GRID_TARIFF_KEYS)} (and optionally {DEMAND_COMPONENTS})")
    window = None
    if raw["high_load_window"] is not None:
        try:
            window = high_load_window_from(raw["high_load_window"])
        except AutoSettingsError as err:
            raise _invalid(str(err), "high_load_window") from err
    demand: tuple[DemandComponent, ...] = ()
    named = DEMAND_COMPONENTS in keys
    if named:
        try:
            demand = demand_components_from(raw[DEMAND_COMPONENTS])
        except AutoSettingsError as err:
            raise _invalid(str(err), DEMAND_COMPONENTS) from err
    tariff = GridTariff(
        transfer_percent=_decode_fee(raw["transfer_percent"], "transfer_percent"),
        transfer_high=_decode_fee(raw["transfer_high"], "transfer_high"),
        high_load_window=window,
        demand=demand,
    )
    try:
        return tariff.validated(), named
    except AutoSettingsError as err:
        raise _invalid(str(err)) from err


def _expected(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise GridTariffRefusal(
            "invalid_number", "expected_write_revision must be a non-negative whole number", "expected_write_revision"
        )
    return raw


def update_grid_tariff(hass: HomeAssistant, charger_id: str, expected_write_revision: Any, body: Any) -> None:
    """Store the grid tariff on the charger's electricity price entry, in place, compare-and-set (see the module).

    Every charger pricing with the entry replans (its revision moves). A tariff equal to the stored one (all off
    equals none stored) passes the compare-and-set and writes nothing: the write counter stays.
    """
    expected = _expected(expected_write_revision)
    tariff, demand_named = decode_grid_tariff(body)
    source = entry_source_for(hass, charger_id)
    if source is None or source.entry_id is None:
        raise GridTariffRefusal(ERROR_NO_PRICE_ENTRY, "This charger has no electricity price entry")
    entry = hass.config_entries.async_get_entry(source.entry_id)
    if entry is None:
        raise GridTariffRefusal(ERROR_NO_PRICE_ENTRY, "The electricity price entry is gone")
    if expected != source.write_revision:
        raise GridTariffRefusal(
            "revision_conflict", f"the electricity price is at write {source.write_revision}, not {expected}"
        )
    stored = source.overrides.grid_tariff or GridTariff()
    if not demand_named:
        # A writer that does not know the demand charge leaves it as stored.
        tariff = replace(tariff, demand=stored.demand)
    if tariff == stored:
        # The tariff as stored (nothing stored reads as all off): nothing to write, and no replan.
        return
    try:
        async_write_price_entry(
            hass, entry, replace(source.overrides, grid_tariff=tariff), expected_write_revision=expected
        )
    except AutoSettingsError as err:
        raise GridTariffRefusal(err.code, str(err)) from err


async def async_webhook_update_grid_tariff(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`update_grid_tariff {expected_write_revision, grid_tariff}` over the webhook, for its own charger. Returns
    `(http_status, body)`: 200, 400 for a bad body or no price entry, 409 for a conflict."""
    refusal: GridTariffRefusal | None = None
    try:
        update_grid_tariff(hass, entry.entry_id, payload.get("expected_write_revision"), payload.get("grid_tariff"))
    except GridTariffRefusal as refused:
        refusal = refused
    body = {
        "ok": refusal is None,
        "error": None if refusal is None else refusal.code,
        "field_errors": []
        if refusal is None or refusal.field is None
        else [{"field": refusal.field, "code": ERR_INVALID_VALUE}],
        "grid_tariff": serialize_grid_tariff(capture_grid_tariff(hass, entry.entry_id), can_act=True),
    }
    if refusal is None:
        return 200, body
    return (409 if refusal.code == "revision_conflict" else 400), body


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_grid_tariff",
        vol.Required("charger_id"): str,
        vol.Required("expected_write_revision"): object,
        vol.Required("grid_tariff"): object,
    }
)
@websocket_api.async_response
async def websocket_update_grid_tariff(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The grid tariff of the charger's electricity price entry, replaced (see the module)."""
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only administrators can change the electricity price")
        return
    charger, failure = lookup_charger(hass, msg["charger_id"], require_loaded=False)
    if failure is not None or charger is None:
        connection.send_error(msg["id"], failure or ERROR_NOT_ADMIN, "Not a charger")
        return
    try:
        update_grid_tariff(hass, charger.entry_id, msg["expected_write_revision"], msg["grid_tariff"])
    except GridTariffRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(
        msg["id"],
        {"ok": True, "grid_tariff": serialize_grid_tariff(capture_grid_tariff(hass, charger.entry_id), can_act=True)},
    )


@callback
def async_setup_grid_tariff_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_update_grid_tariff)
