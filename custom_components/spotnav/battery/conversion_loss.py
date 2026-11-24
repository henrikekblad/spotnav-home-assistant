"""A hybrid inverter's own loss on the battery's way in and out, learnt or assumed, and the battery on the AC side (pure).

The meter at the grid connection sees the battery on the AC side: a kWh charged costs that kWh and the inverter's loss
on the way in, and a kWh discharged gives the house that kWh less the loss on the way out. Many hybrids read the
battery's power at the battery, the DC side (`adapters`: `dc_power`). Counted as read, `grid - car - battery` puts that
loss in the house: the day with no battery, the perfect plan and every way simulated beside the real one then pay for
losses only the real battery's own running caused, which flatters the battery's own way (on the owner's plant in
November 2026 by 7-13 kr a day; `plans/research_integrations/sigen_ai_above_perfect_2026-11-24.md`). So a DC reading is
turned into an AC one (`ac_kw`) before the review, the perfect plan and the house figures count it:

    AC = DC x (1 + charge loss)      for the part of a charge the grid gave (at most what was imported),
    AC = DC x (1 - discharge loss)   for the rest of a charge, and while it discharges.

The rest of a charge is the sun's over the DC bus of a hybrid: it never crossed the inverter, but without the battery it
would have gone out through it and lost about the way-out share; so the AC side counts it at that, and the midday house
is not lowered by a loss the sun's charge never had. The plan prices every stored kWh with the AC round trip, so it
values storing the sun a few percent below what that costs (the sun's real penalty is about the DC round trip): with
sell prices far under buy prices this rarely changes a decision.

The SoC, the battery's size and its round trip learnt from its own counters stay on the DC side (they are the cells').
The plan's round trip is the AC one (`ac_round_trip`): the DC round trip x (1 - discharge loss) / (1 + charge loss).

* **Learnt** where the plant reads its own house load and solar power (`adapters`: `plant_load_power`,
  `plant_pv_power`; Sigenergy). Per quarter hour the balance `grid + sun - battery(DC) - load` is what the plant's own
  load leaves out: a standing use of the inverter plus the loss in proportion to the battery's charge and discharge. A
  least-squares fit of that balance on the quarter hour's mean charge and discharge power (and a constant) gives the
  two losses. Only dark quarter hours count (the sun below `DARK_KW`), so the solar side's own conversion is not taken
  for the battery's; only fully measured ones (`FULL_COVER_S`). The sums fade with a half-life of `HALF_LIFE_QUARTERS`,
  and the answer is used once `MIN_ACTIVE` quarter hours charged and as many discharged at `ACTIVE_KW` or more, and
  only while both losses lie in `LOSS_RANGE`; else the assumed ones stand (a fit outside it is logged, never shown:
  a cell heater on a cold night's charge, or the plant's meter reading a little off SpotNav's, is fitted as loss too).
  The half-life counts dark quarter hours, not days, so in summer the fit moves more slowly.
* **Assumed** otherwise: `DEFAULT_CHARGE_LOSS` and `DEFAULT_DISCHARGE_LOSS`, a typical hybrid's 96-97 % each way
  (the owner's Sigenergy plant measured 3.5 % and 6.5 %).
* **Read as they are** where the battery's power is not marked DC (an AC-coupled battery, or a brand whose side is not
  known): no loss.

Whatever the constant is (the inverter's standing use) stays in the house: a house with a hybrid and no battery would
have most of it too.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final

#: The assumed losses (shares of the DC power) where none is learnt.
DEFAULT_CHARGE_LOSS: Final = 0.03
DEFAULT_DISCHARGE_LOSS: Final = 0.05
#: A believable loss (share), either way: a hybrid's inverter loses 2-7 % each way.
LOSS_RANGE: Final = (0.0, 0.08)
#: A quarter hour with the sun below this (kW) is dark.
DARK_KW: Final = 0.1
#: A quarter hour charging or discharging at this mean power (kW) or more counts towards `MIN_ACTIVE`.
ACTIVE_KW: Final = 0.5
MIN_ACTIVE: Final = 48.0
#: The sums fade by half over this many dark quarter hours (60 nights' worth).
HALF_LIFE_QUARTERS: Final = 60 * 96
QUARTER: Final = timedelta(minutes=15)
FULL_COVER_S: Final = 0.9 * QUARTER.total_seconds()
#: A gap between readings longer than this (s) counts nothing.
MAX_GAP_S: Final = 60.0

_LOGGER = logging.getLogger(__name__)

SOURCE_LEARNED: Final = "learned"
SOURCE_ASSUMED: Final = "assumed"
SOURCE_AC: Final = "ac"

_TERMS: Final = 3  # constant, charge kW, discharge kW


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def ac_kw(dc_kw: float, loss: tuple[float, float], grid_kw: float | None = None) -> float:
    """The battery's power on the AC side (+ charging) from its power at the battery, with `(charge, discharge)` loss
    (see the module): a charge up to what the grid gave ([grid_kw], + import; all of it where not known) at the way-in
    loss, the rest of it (the sun's, over the DC bus) and a discharge at the way-out loss."""
    charge, discharge = loss
    if dc_kw <= 0:
        return dc_kw * (1.0 - discharge)
    from_grid = dc_kw if grid_kw is None else min(dc_kw, max(0.0, grid_kw))
    return from_grid * (1.0 + charge) + (dc_kw - from_grid) * (1.0 - discharge)


def ac_round_trip(dc_round_trip: float, loss: tuple[float, float]) -> float:
    """The round trip on the AC side from one counted at the battery."""
    charge, discharge = loss
    return dc_round_trip * (1.0 - discharge) / (1.0 + charge)


def _quarter_of(moment: datetime) -> datetime:
    moment = moment.astimezone(timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)


@dataclass(slots=True)
class _Running:
    """The quarter hour being measured: seconds, and kWh charged, discharged, of balance and of sun."""

    start: datetime
    seconds: float = 0.0
    charge: float = 0.0
    discharge: float = 0.0
    balance: float = 0.0
    sun: float = 0.0


@dataclass(slots=True)
class ConversionLoss:
    """What was learnt of the inverter's loss on the battery's way; persisted (`as_dict`/`from_dict`)."""

    #: The fit's sums (`X'X` row by row and `X'y`, over constant, charge kW, discharge kW), faded.
    xtx: list[float] = field(default_factory=lambda: [0.0] * (_TERMS * _TERMS))
    xty: list[float] = field(default_factory=lambda: [0.0] * _TERMS)
    #: The (faded) quarter hours that charged and that discharged at `ACTIVE_KW` or more.
    charged_n: float = 0.0
    discharged_n: float = 0.0
    #: The losses learnt, `None` until the fit holds.
    charge_loss: float | None = None
    discharge_loss: float | None = None
    running: _Running | None = None
    #: The last reading: `(when, battery DC kW, grid kW, sun kW, load kW)`.
    last: tuple[datetime, float, float, float, float] | None = None

    # ---- the answers

    def loss(self, power_dc: bool) -> tuple[float, float]:
        """The `(charge, discharge)` loss to count the battery's power with: none for a battery read on the AC side."""
        if not power_dc:
            return (0.0, 0.0)
        if self.charge_loss is None or self.discharge_loss is None:
            return (DEFAULT_CHARGE_LOSS, DEFAULT_DISCHARGE_LOSS)
        return (self.charge_loss, self.discharge_loss)

    def source(self, power_dc: bool) -> str:
        if not power_dc:
            return SOURCE_AC
        return SOURCE_ASSUMED if self.charge_loss is None else SOURCE_LEARNED

    # ---- learning

    def observe(
        self,
        now: datetime,
        battery_kw: float | None,
        grid_kw: float | None,
        pv_kw: float | None,
        load_kw: float | None,
    ) -> bool:
        """One reading of the battery's power at the battery, the grid (+ import), the plant's sun and its house load
        (kW). The last reading counts until now; a quarter hour ended is fitted. `True` when the losses changed."""
        changed = False
        last = self.last
        if last is not None:
            at, battery, grid, sun, load = last
            gap = (now - at).total_seconds()
            running = self.running
            if running is not None and 0 < gap <= MAX_GAP_S and _quarter_of(at) == running.start:
                hours = gap / 3600.0
                running.seconds += gap
                running.charge += max(0.0, battery) * hours
                running.discharge += max(0.0, -battery) * hours
                running.balance += (grid + sun - battery - load) * hours
                running.sun += max(0.0, sun) * hours
        quarter = _quarter_of(now)
        if self.running is None or self.running.start != quarter:
            if self.running is not None:
                changed = self._take(self.running)
            self.running = _Running(quarter)
        readings = (battery_kw, grid_kw, pv_kw, load_kw)
        self.last = (now, *map(float, readings)) if all(_finite(value) for value in readings) else None  # type: ignore[arg-type]
        return changed

    def _take(self, running: _Running) -> bool:
        if running.seconds < FULL_COVER_S:
            return False
        hours = running.seconds / 3600.0
        if running.sun / hours >= DARK_KW:
            return False
        charge, discharge, balance = running.charge / hours, running.discharge / hours, running.balance / hours
        fade = 0.5 ** (1.0 / HALF_LIFE_QUARTERS)
        row = (1.0, charge, discharge)
        for i in range(_TERMS):
            self.xty[i] = self.xty[i] * fade + row[i] * balance
            for j in range(_TERMS):
                self.xtx[i * _TERMS + j] = self.xtx[i * _TERMS + j] * fade + row[i] * row[j]
        self.charged_n = self.charged_n * fade + (1.0 if charge >= ACTIVE_KW else 0.0)
        self.discharged_n = self.discharged_n * fade + (1.0 if discharge >= ACTIVE_KW else 0.0)
        return self._fit()

    def _fit(self) -> bool:
        if self.charged_n < MIN_ACTIVE or self.discharged_n < MIN_ACTIVE:
            return False
        solved = _solve([self.xtx[i * _TERMS:(i + 1) * _TERMS] for i in range(_TERMS)], list(self.xty))
        if solved is None:
            return False
        _constant, charge, discharge = solved
        low, high = LOSS_RANGE
        if not (low <= charge <= high and low <= discharge <= high):
            _LOGGER.debug("SpotNav battery: a conversion loss of %.3f in, %.3f out is not believed", charge, discharge)
            return False
        charge, discharge = round(charge, 4), round(discharge, 4)
        if (charge, discharge) == (self.charge_loss, self.discharge_loss):
            return False
        self.charge_loss, self.discharge_loss = charge, discharge
        return True

    # ---- persistence and diagnostics

    def as_dict(self) -> dict[str, Any]:
        return {
            "xtx": [round(value, 6) for value in self.xtx],
            "xty": [round(value, 6) for value in self.xty],
            "charged_n": round(self.charged_n, 4),
            "discharged_n": round(self.discharged_n, 4),
            "charge_loss": self.charge_loss,
            "discharge_loss": self.discharge_loss,
        }

    def view(self, power_dc: bool) -> dict[str, Any]:
        """For the diagnostics: the losses counted, where they come from and what the fit rests on."""
        charge, discharge = self.loss(power_dc)
        return {
            "power_dc": power_dc,
            "source": self.source(power_dc),
            "charge_loss": charge,
            "discharge_loss": discharge,
            "charged_quarters": round(self.charged_n, 1),
            "discharged_quarters": round(self.discharged_n, 1),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ConversionLoss:
        if not isinstance(raw, dict):
            return cls()
        xtx, xty = raw.get("xtx"), raw.get("xty")
        if not (
            isinstance(xtx, list)
            and len(xtx) == _TERMS * _TERMS
            and isinstance(xty, list)
            and len(xty) == _TERMS
            and all(_finite(value) for value in (*xtx, *xty))
        ):
            return cls()
        made = cls(xtx=[float(v) for v in xtx], xty=[float(v) for v in xty])
        for key in ("charged_n", "discharged_n"):
            value = raw.get(key)
            if _finite(value) and value >= 0:
                setattr(made, key, float(value))
        charge, discharge = raw.get("charge_loss"), raw.get("discharge_loss")
        low, high = LOSS_RANGE
        if _finite(charge) and _finite(discharge) and low <= charge <= high and low <= discharge <= high:
            made.charge_loss, made.discharge_loss = float(charge), float(discharge)
        return made


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """`matrix x = vector` by Gaussian elimination with partial pivoting; `None` when it is singular."""
    size = len(vector)
    rows = [[*matrix[i], vector[i]] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot][column]) < 1e-9:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for r in range(size):
            if r != column:
                factor = rows[r][column] / rows[column][column]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[column])]
    return [rows[i][size] / rows[i][i] for i in range(size)]
