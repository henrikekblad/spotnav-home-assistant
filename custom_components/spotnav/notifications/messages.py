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


def money_text(language: str, money: Money) -> str:
    amount = _number(language, money.amount_minor / 100, 2)
    unit = _CURRENCY.get(money.currency, money.currency)
    if unit in ("€", "£") and language == "en":
        return f"{unit}{amount}"
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


def compose(event: str, name: str, facts: dict[str, Any], language: str) -> tuple[str, str]:
    """The title and the message of one notification.

    `facts` by event: `plan_stopped` {reason} (`charger_ignores_stop` too: the charger keeps charging under a
    person's Stop); `plan_at_risk` {time?}; `charge_complete` {reason,
    target_percent?, kwh?, cost?}; `charge_started` {until?}; `plan_installed` {time, kwh?, cost?}.
    """
    text = table(language, NAMESPACE)
    title = text["title"].format(name=name)
    kwh = facts.get("kwh")
    cost = facts.get("cost")
    if event == "plan_stopped":
        message = text.get(f"stopped.{facts.get('reason')}", text["stopped.not_started"])
    elif event == "plan_at_risk":
        time = facts.get("time")
        message = text["at_risk.time"].format(time=time) if time else text["at_risk"]
    elif event == "charge_complete":
        reason = facts.get("reason")
        percent = facts.get("target_percent")
        if reason == "target" and percent is not None:
            message = text["complete.target"].format(percent=percent_text(language, percent))
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
        message = text["plan_installed"].format(time=facts.get("time")) + _figures(
            language, text, "planned", kwh, cost, estimate=True
        )
    elif event == "vehicle_identify":
        message = text["identify.ask"]
    else:
        message = text[event]
    return title, message


def identify_text(language: str, key: str, vehicle: str | None = None) -> str:
    """One of the identification question's own texts (`ask`, `open`, `chosen`, `recognised`, `kept`)."""
    text = table(language, NAMESPACE)
    return text[f"identify.{key}"].format(vehicle=vehicle or "")
