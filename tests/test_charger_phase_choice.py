"""The phase choice of a single-phase charger in the site's wiring step: "Automatic (follows the charger)" is
the default and is stored as no phase; L1, L2 and L3 are kept as chosen."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.flows.site_form import charger_wiring_schema, parse_charger_wiring, PHASE_AUTOMATIC


def test_the_wiring_step_offers_automatic_as_the_default_and_stores_it_as_no_phase(hass: HomeAssistant) -> None:
    defaults = charger_wiring_schema(hass, "no_such_charger")({"phases": 1})
    assert defaults["phase"] == PHASE_AUTOMATIC
    assert parse_charger_wiring(None, defaults)["phase"] is None

    # A chosen phase is offered as it is and kept.
    chosen = charger_wiring_schema(hass, "no_such_charger", existing={"phases": 1, "phase": "L2"})({"phases": 1})
    assert chosen["phase"] == "l2"
    assert parse_charger_wiring({"phases": 1, "phase": "l2"}, chosen)["phase"] == "L2"
    assert parse_charger_wiring({"phases": 1, "phase": "L2"}, {"phases": 1, "phase": PHASE_AUTOMATIC})["phase"] is None
