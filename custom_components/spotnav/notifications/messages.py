"""What a notification says: short, in Home Assistant's language, with the charger's name.

Pure: no Home Assistant imports; the words are in `i18n/<lang>.json` under `notifications`.
`compose(event, facts, language)` answers `(title, message)`; the facts are typed (kWh as a number,
money as minor units and a currency code, times as `HH:MM` already in the installation's zone) and
worded here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ..texts import table

#: A person's test (`spotnav.send_test_notification`), not an event a charger has.
EVENT_TEST: Final = "test"

#: The namespace in `i18n/<lang>.json` holding these texts.
NAMESPACE: Final = "notifications"


#: How a currency is written after its amount; an unknown code is written as itself.
_CURRENCY: Final = {"SEK": "kr", "NOK": "kr", "DKK": "kr", "EUR": "€", "GBP": "£", "PLN": "zł", "CHF": "CHF"}


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str


def _number(language: str, value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}"
    return text if language == "en" else text.replace(".", ",")


def kwh_text(language: str, kwh: float) -> str:
    return f"{_number(language, kwh, 1)} kWh"


def money_text(language: str, money: Money, decimals: int = 2) -> str:
    amount = _number(language, money.amount_minor / 100, decimals)
    unit = _CURRENCY.get(money.currency, money.currency)
    if unit in ("€", "£") and language == "en":
        return f"{unit}{amount}"
    if unit in ("€", "£") and language == "nl":
        return f"{unit} {amount}"
    return f"{amount} {unit}"


def percent_text(language: str, percent: float) -> str:
    decimals = 0 if float(percent).is_integer() else 1
    spaced = "" if language == "en" else " "
    return f"{_number(language, percent, decimals)}{spaced}%"


def _figures(
    language: str, text: Mapping[str, str], key: str, kwh: float | None, cost: Money | None, *, estimate: bool
) -> str:
    """` 12,4 kWh laddat, 18,30 kr.`: the energy and the money where they are known, else nothing."""
    parts: list[str] = []
    if kwh is not None and kwh > 0:
        parts.append(text[key].format(kwh=kwh_text(language, kwh)))
    if cost is not None:
        money = money_text(language, cost)
        parts.append(text["cost"].format(cost=money) if estimate else money)
    if not parts:
        return ""
    joined = ", ".join(parts)
    return f" {joined[:1].upper()}{joined[1:]}."


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compose(event: str, name: str, facts: dict[str, Any], language: str) -> tuple[str, str]:
    """The title and the message of one notification.

    `facts` by event: `plan_stopped` {reason} (`charger_ignores_stop` too: the charger keeps charging under a
    person's Stop; `car_refuses` with `level_percent` and `target_percent`: the car takes no current below its target); `plan_at_risk` {time?}; `charge_complete` {reason,
    target_percent?, level_percent?, basis?, kwh?, cost?}; `charge_started` {until?}; `plan_installed` {time, kwh?,
    cost?, guest?} (`guest`: the plan is a guest car's).

    A target's completion says what the stop was taken on (`_target_message`): the target and the car's own level
    for a reading at or above it, the level as an estimate for one from delivered energy, and the level below the
    target for a car that ended the charge just short of it.
    """
    text = table(language, NAMESPACE)
    title = text["title"].format(name=name)
    kwh = facts.get("kwh")
    cost = facts.get("cost")
    if event == "plan_stopped":
        message = text.get(f"stopped.{facts.get('reason')}", text["stopped.not_started"])
        level = facts.get("level_percent")
        target = facts.get("target_percent")
        if facts.get("reason") == "car_refuses" and _is_number(level) and _is_number(target):
            message = text["stopped.car_refuses_level"].format(
                level=percent_text(language, round(level)), target=percent_text(language, round(target))
            )
    elif event == "plan_at_risk":
        time = facts.get("time")
        message = text["at_risk.time"].format(time=time) if time else text["at_risk"]
    elif event == "charge_complete":
        reason = facts.get("reason")
        percent = facts.get("target_percent")
        if reason == "target" and percent is not None:
            message = _target_message(language, text, percent, facts.get("level_percent"), facts.get("basis"))
        elif reason == "energy":
            message = text["complete.energy"]
        elif reason == "vehicle_full":
            message = text["complete.vehicle_full"]
        else:
            message = text["complete.plan_done"]
        message += _figures(language, text, "charged", kwh, cost, estimate=False)
    elif event == "charge_started":
        until = facts.get("until")
        message = text["started.until"].format(time=until) if until else text["started"]
    elif event == "plan_installed":
        key = "plan_installed.guest" if facts.get("guest") else "plan_installed"
        message = text[key].format(time=facts.get("time")) + _figures(
            language, text, "planned", kwh, cost, estimate=True
        )
    elif event == "vehicle_identify":
        message = text["identify.ask"]
    else:
        message = text[event]
    return title, message


def _target_message(
    language: str, text: Mapping[str, str], target: float, level: Any, basis: Any
) -> str:
    """A target's completion: never "the target is reached" on an estimate, nor for a level read below it.

    * no level known: `complete.target`, the target alone;
    * an estimate (`basis` `estimate`): `complete.target_estimate`, the level rounded to a whole percent and called
      an estimate, beside the target;
    * a reading at or above the target: `complete.target_reading`, the target and the car's level;
    * a reading below it (a car that ended the charge just short of the target): `complete.stopped_below`.
    """
    target_text = percent_text(language, target)
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return text["complete.target"].format(percent=target_text)
    if basis == "estimate":
        return text["complete.target_estimate"].format(
            level=percent_text(language, round(level)), percent=target_text
        )
    key = "complete.target_reading" if level >= target else "complete.stopped_below"
    return text[key].format(level=percent_text(language, level), percent=target_text)


def identify_text(language: str, key: str, vehicle: str | None = None) -> str:
    """One of the identification question's own texts (`ask`, `open`, `chosen`, `recognised`, `kept`, `guest`)."""
    text = table(language, NAMESPACE)
    return text[f"identify.{key}"].format(vehicle=vehicle or "")


def does_not_fit_text(
    name: str, language: str, *, time: str | None, short_kwh: float | None, ceiling_kw: float, set_kw: float | None = None
) -> tuple[str, str]:
    """The title and message of the "does not fit" notice (`ChargerNotifier.does_not_fit`), naming the ceiling: "The
    car will not be ready by 07:00 under the 6.0 kW import target – 4 kWh short."; where the month's peak lifts the
    hour (`ceiling_kw` above the target as set, `set_kw`), it names the month's peak and the import target both; "by
    the departure" without its time. Whole kWh, one decimal kW."""
    text = table(language, NAMESPACE)
    peak = set_kw is not None and ceiling_kw > set_kw + 1e-9
    key = "import_target.at_risk.peak" if peak else "import_target.at_risk"
    key = f"{key}.time" if time else key
    message = text[key].format(
        time=time or "",
        target=_number(language, ceiling_kw, 1),
        set=_number(language, ceiling_kw if set_kw is None else set_kw, 1),
        kwh=_number(language, 0.0 if short_kwh is None else float(short_kwh), 0),
    )
    return text["title"].format(name=name), message


def does_not_fit_choice_text(
    language: str,
    code: str,
    *,
    kwh: float | None = None,
    peak_kw: float | None = None,
    cost: Money | None = None,
    time: str | None = None,
) -> str:
    """A "does not fit" choice's button, as the card words it: "Take what fits (≈ 12 kWh)", "Later departure
    (14:00)", "Allow the peak (≈ 12 kW, ≈ 105 kr)" (whole kWh, kW and money; a figure not known is left out)."""
    text = table(language, NAMESPACE)
    if code == "later":
        return text["does_not_fit.later"].format(time=time or "")
    if code == "takes_what_fits":
        if kwh is None:
            return text["does_not_fit.take"]
        return text["does_not_fit.take.kwh"].format(kwh=_number(language, kwh, 0))
    if peak_kw is None:
        return text["does_not_fit.peak"]
    kw = _number(language, peak_kw, 0)
    if cost is None:
        return text["does_not_fit.peak.kw"].format(kw=kw)
    return text["does_not_fit.peak.cost"].format(kw=kw, cost=money_text(language, cost, 0))


def not_ready_title(language: str) -> str:
    """The combined notice's title (`site_notice.py`): "SpotNav · Blir inte klart i tid"."""
    return table(language, NAMESPACE)["not_ready.title"]


def not_ready_car_line(language: str, name: str, *, time: str, short_kwh: float, ceiling_kw: float) -> str:
    """A car's line in the combined notice: "Garage 07:00: 4 kWh saknas under effektmålet 6,0 kW"."""
    return table(language, NAMESPACE)["not_ready.line.car"].format(
        name=name, time=time, kwh=_number(language, short_kwh, 0), target=_number(language, ceiling_kw, 1)
    )


def not_ready_heat_line(language: str, kind: str, *, time: str, reach_c: float) -> str:
    """A bath's or a session's line in the combined notice: "Spa 19:00: når 35 °C" (`kind` `spa` or `sauna`)."""
    return table(language, NAMESPACE)[f"not_ready.line.{kind}"].format(time=time, reach=_number(language, reach_c, 0))


def not_ready_heat_choice(language: str, kind: str, code: str, *, time: str | None = None, reach_c: float | None = None) -> str:
    """A bath's or a session's button: "Senare badtid (20:00)", "Senare bastupass (20:00)", "Ta det som ryms (≈ 35 °C)"."""
    text = table(language, NAMESPACE)
    if code == "later":
        return text[f"not_ready.later.{kind}"].format(time=time or "")
    if reach_c is None:
        return text["does_not_fit.take"]
    return text["not_ready.take.c"].format(reach=_number(language, reach_c, 0))


def not_ready_button(language: str, load: str, choice: str) -> str:
    """A button of one load among several: "Spa: Ta det som ryms"."""
    return table(language, NAMESPACE)["not_ready.button"].format(load=load, choice=choice)


def not_ready_load(language: str, kind: str) -> str:
    """The spa's or the sauna's word in a button ("Spa", "Bastu")."""
    return table(language, NAMESPACE)[f"not_ready.load.{kind}"]


def site_peak_choice(language: str) -> str:
    """The combined notice's "Allow the peak" for every item under the ceiling."""
    return table(language, NAMESPACE)["does_not_fit.peak"]


def pool_season_text(language: str) -> tuple[str, str, str]:
    """The pool's season notice: its title, message and button ("Stäng säsongen")."""
    text = table(language, NAMESPACE)
    return text["pool_season.title"], text["pool_season.message"], text["pool_season.close"]
