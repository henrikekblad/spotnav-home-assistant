"""The one sentence that says how to add a charger to a site that has none, in the words Home Assistant's
own pages use. Shown by the site's setup step, the site's entity, its Repairs issue and the card (whose
`state.addCharger` says the same). The words are in `i18n/<lang>.json` under `add_charger`."""

from __future__ import annotations

from .texts import table


def add_charger_hint(language: str) -> str:
    return table(language, "add_charger")["hint"]
