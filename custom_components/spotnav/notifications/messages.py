"""What a notification says: short, in Home Assistant's language (the card's five), with the charger's name.

Pure: no Home Assistant imports. `compose(event, facts, language)` answers `(title, message)`; the
facts are typed (kWh as a number, money as minor units and a currency code, times as `HH:MM` already
in the installation's zone) and worded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

LANGUAGES: Final = ("en", "sv", "da", "nb", "fi")
#: A person's test (`spotnav.send_test_notification`), not an event a charger has.
EVENT_TEST: Final = "test"


def language_of(configured: str | None) -> str:
    """One of `LANGUAGES` for Home Assistant's configured language (`sv-SE`, `nb`, `no`, `nn`, ...)."""
    code = (configured or "en").lower().replace("_", "-").split("-")[0]
    if code in ("no", "nn"):
        return "nb"
    return code if code in LANGUAGES else "en"


_TEXT: Final[dict[str, dict[str, str]]] = {
    "en": {
        "title": "SpotNav · {name}",
        "stopped.not_started": "Charging did not start as planned.",
        "stopped.stopped": "Charging stopped during a planned window.",
        "stopped.charger_unavailable": "The charger is unavailable during a planned window.",
        "stopped.vehicle_not_requesting": "The car is not taking current during a planned window.",
        "stopped.held_by_charger": "The charger's own schedule is holding back the planned charge.",
        "stopped.charger_ignores_stop": "The charger keeps charging although it was stopped. Stop it at the charger or unplug the car.",
        "stopped.charger_disabled": "The charger is disabled, so the planned charge cannot start.",
        "at_risk": "The charge will not be ready by the departure.",
        "at_risk.time": "The charge will not be ready by the departure at {time}.",
        "complete.target": "Charging complete: the target {percent} was reached.",
        "complete.energy": "Charging complete: the requested energy was delivered.",
        "complete.plan_done": "The planned charge has finished.",
        "complete.vehicle_full": "Charging complete: the car is full.",
        "started": "Charging started.",
        "test": "Test notification from SpotNav: notifications reach this phone.",
        "started.until": "Charging started, until {time}.",
        "plugged_in": "The car is plugged in.",
        "unplugged": "The car is unplugged.",
        "plan_installed": "New plan: charging from {time}.",
        "identify.ask": "Which car is plugged in?",
        "identify.open": "Open SpotNav",
        "identify.chosen": "{vehicle} chosen.",
        "identify.recognised": "{vehicle} chosen automatically.",
        "identify.kept": "{vehicle} kept.",
        "charged": "{kwh} charged",
        "planned": "{kwh} planned",
        "cost": "about {cost}",
    },
    "sv": {
        "title": "SpotNav · {name}",
        "stopped.not_started": "Laddningen startade inte enligt planen.",
        "stopped.stopped": "Laddningen stannade under ett planerat fönster.",
        "stopped.charger_unavailable": "Laddaren är inte tillgänglig under ett planerat fönster.",
        "stopped.vehicle_not_requesting": "Bilen tar inte emot ström under ett planerat fönster.",
        "stopped.held_by_charger": "Laddarens eget schema håller tillbaka den planerade laddningen.",
        "stopped.charger_ignores_stop": "Laddaren fortsätter ladda fast den stoppades. Stoppa den vid laddaren eller koppla ur bilen.",
        "stopped.charger_disabled": "Laddaren är avstängd, så den planerade laddningen kan inte starta.",
        "at_risk": "Laddningen hinner inte bli klar till avresan.",
        "at_risk.time": "Laddningen hinner inte bli klar till avresan {time}.",
        "complete.target": "Laddningen är klar: målet {percent} är nått.",
        "complete.energy": "Laddningen är klar: den begärda energin är levererad.",
        "complete.plan_done": "Den planerade laddningen är slutförd.",
        "complete.vehicle_full": "Laddningen är klar: bilen är full.",
        "started": "Laddningen har startat.",
        "test": "Testnotis från SpotNav: notiserna når den här telefonen.",
        "started.until": "Laddningen har startat, till {time}.",
        "plugged_in": "Bilen är inkopplad.",
        "unplugged": "Bilen är urkopplad.",
        "plan_installed": "Ny plan: laddar från {time}.",
        "identify.ask": "Vilken bil är inkopplad?",
        "identify.open": "Öppna SpotNav",
        "identify.chosen": "{vehicle} vald.",
        "identify.recognised": "{vehicle} vald automatiskt.",
        "identify.kept": "{vehicle} behålls.",
        "charged": "{kwh} laddat",
        "planned": "{kwh} planerat",
        "cost": "cirka {cost}",
    },
    "da": {
        "title": "SpotNav · {name}",
        "stopped.not_started": "Opladningen startede ikke som planlagt.",
        "stopped.stopped": "Opladningen stoppede i et planlagt vindue.",
        "stopped.charger_unavailable": "Laderen er ikke tilgængelig i et planlagt vindue.",
        "stopped.vehicle_not_requesting": "Bilen tager ikke strøm i et planlagt vindue.",
        "stopped.held_by_charger": "Laderens egen tidsplan holder den planlagte opladning tilbage.",
        "stopped.charger_ignores_stop": "Laderen bliver ved med at lade, selvom den blev stoppet. Stop den ved laderen, eller tag stikket ud af bilen.",
        "stopped.charger_disabled": "Laderen er slået fra, så den planlagte opladning kan ikke starte.",
        "at_risk": "Opladningen bliver ikke færdig til afgangen.",
        "at_risk.time": "Opladningen bliver ikke færdig til afgangen kl. {time}.",
        "complete.target": "Opladningen er færdig: målet {percent} er nået.",
        "complete.energy": "Opladningen er færdig: den ønskede energi er leveret.",
        "complete.plan_done": "Den planlagte opladning er afsluttet.",
        "complete.vehicle_full": "Opladningen er færdig: bilen er fuld.",
        "started": "Opladningen er startet.",
        "test": "Testnotifikation fra SpotNav: notifikationerne når frem til denne telefon.",
        "started.until": "Opladningen er startet, til {time}.",
        "plugged_in": "Bilen er tilsluttet.",
        "unplugged": "Bilen er frakoblet.",
        "plan_installed": "Ny plan: lader fra {time}.",
        "identify.ask": "Hvilken bil er tilsluttet?",
        "identify.open": "Åbn SpotNav",
        "identify.chosen": "{vehicle} valgt.",
        "identify.recognised": "{vehicle} valgt automatisk.",
        "identify.kept": "{vehicle} beholdes.",
        "charged": "{kwh} ladet",
        "planned": "{kwh} planlagt",
        "cost": "cirka {cost}",
    },
    "nb": {
        "title": "SpotNav · {name}",
        "stopped.not_started": "Ladingen startet ikke som planlagt.",
        "stopped.stopped": "Ladingen stoppet i et planlagt vindu.",
        "stopped.charger_unavailable": "Laderen er ikke tilgjengelig i et planlagt vindu.",
        "stopped.vehicle_not_requesting": "Bilen tar ikke imot strøm i et planlagt vindu.",
        "stopped.held_by_charger": "Laderens egen tidsplan holder den planlagte ladingen tilbake.",
        "stopped.charger_ignores_stop": "Laderen fortsetter å lade selv om den ble stoppet. Stopp den ved laderen eller koble fra bilen.",
        "stopped.charger_disabled": "Laderen er slått av, så den planlagte ladingen kan ikke starte.",
        "at_risk": "Ladingen blir ikke ferdig til avreise.",
        "at_risk.time": "Ladingen blir ikke ferdig til avreise kl. {time}.",
        "complete.target": "Ladingen er ferdig: målet {percent} er nådd.",
        "complete.energy": "Ladingen er ferdig: den ønskede energien er levert.",
        "complete.plan_done": "Den planlagte ladingen er fullført.",
        "complete.vehicle_full": "Ladingen er ferdig: bilen er full.",
        "started": "Ladingen har startet.",
        "test": "Testvarsel fra SpotNav: varslene når frem til denne telefonen.",
        "started.until": "Ladingen har startet, til {time}.",
        "plugged_in": "Bilen er tilkoblet.",
        "unplugged": "Bilen er frakoblet.",
        "plan_installed": "Ny plan: lader fra {time}.",
        "identify.ask": "Hvilken bil er tilkoblet?",
        "identify.open": "Åpne SpotNav",
        "identify.chosen": "{vehicle} valgt.",
        "identify.recognised": "{vehicle} valgt automatisk.",
        "identify.kept": "{vehicle} beholdes.",
        "charged": "{kwh} ladet",
        "planned": "{kwh} planlagt",
        "cost": "cirka {cost}",
    },
    "fi": {
        "title": "SpotNav · {name}",
        "stopped.not_started": "Lataus ei alkanut suunnitellusti.",
        "stopped.stopped": "Lataus pysähtyi suunnitellun jakson aikana.",
        "stopped.charger_unavailable": "Laturi ei ole käytettävissä suunnitellun jakson aikana.",
        "stopped.vehicle_not_requesting": "Auto ei ota virtaa suunnitellun jakson aikana.",
        "stopped.held_by_charger": "Laturin oma aikataulu pidättää suunniteltua latausta.",
        "stopped.charger_ignores_stop": "Laturi jatkaa lataamista, vaikka se pysäytettiin. Pysäytä se laturista tai irrota auto.",
        "stopped.charger_disabled": "Laturi on pois käytöstä, joten suunniteltu lataus ei voi alkaa.",
        "at_risk": "Lataus ei valmistu lähtöön mennessä.",
        "at_risk.time": "Lataus ei valmistu lähtöön klo {time} mennessä.",
        "complete.target": "Lataus on valmis: tavoite {percent} saavutettiin.",
        "complete.energy": "Lataus on valmis: pyydetty energia on toimitettu.",
        "complete.plan_done": "Suunniteltu lataus on päättynyt.",
        "complete.vehicle_full": "Lataus on valmis: auto on täynnä.",
        "started": "Lataus alkoi.",
        "test": "SpotNavin testi-ilmoitus: ilmoitukset tulevat tähän puhelimeen.",
        "started.until": "Lataus alkoi, klo {time} asti.",
        "plugged_in": "Auto on kytketty.",
        "unplugged": "Auto on irrotettu.",
        "plan_installed": "Uusi suunnitelma: lataus alkaa klo {time}.",
        "identify.ask": "Mikä auto on kytketty?",
        "identify.open": "Avaa SpotNav",
        "identify.chosen": "{vehicle} valittu.",
        "identify.recognised": "{vehicle} valittu automaattisesti.",
        "identify.kept": "{vehicle} säilytetään.",
        "charged": "{kwh} ladattu",
        "planned": "{kwh} suunniteltu",
        "cost": "noin {cost}",
    },
}

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
    language: str, text: dict[str, str], key: str, kwh: float | None, cost: Money | None, *, estimate: bool
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
    text = _TEXT[language if language in _TEXT else "en"]
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
    text = _TEXT[language if language in _TEXT else "en"]
    return text[f"identify.{key}"].format(vehicle=vehicle or "")
