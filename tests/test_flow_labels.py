"""Every field a flow shows has a label in every language, so no form shows a raw key.

The forms are driven through Home Assistant's own flow manager with `async_show_form` observed; every
schema that is shown must have, for each of its fields, a `data` label under its step in `en` and `sv`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowHandler, FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.spotnav.const import DOMAIN

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry, make_site_entry

TRANSLATIONS = Path(__file__).parents[1] / "custom_components" / "spotnav" / "translations"
LANGUAGES = ("en", "sv")


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], set[str]]:
    """(flow kind, step) -> the field names of every schema shown for it."""
    seen: dict[tuple[str, str], set[str]] = {}
    original = FlowHandler.async_show_form

    def observe(self: Any, *, step_id: Any = None, data_schema: Any = None, **kwargs: Any) -> Any:
        kind = "options" if hasattr(self, "config_entry") or "Options" in type(self).__name__ else "config"
        fields = set()
        if data_schema is not None:
            fields = {str(marker.schema if isinstance(marker, vol.Marker) else marker) for marker in data_schema.schema}
        seen.setdefault((kind, step_id), set()).update(fields)
        return original(self, step_id=step_id, data_schema=data_schema, **kwargs)

    monkeypatch.setattr(FlowHandler, "async_show_form", observe)
    return seen


def _labels(language: str) -> dict[str, Any]:
    return json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))


def _assert_labelled(seen: dict[tuple[str, str], set[str]]) -> None:
    missing = []
    for (kind, step), fields in sorted(seen.items()):
        for language in LANGUAGES:
            data = _labels(language)[kind]["step"].get(step, {}).get("data", {})
            missing += [
                f"{language} {kind}.{step}.{name}"
                for name in sorted(fields)
                if not data.get(name)
            ]
    assert not missing, "fields shown without a label: " + ", ".join(missing)


def test_both_languages_label_the_same_fields() -> None:
    en, sv = (_labels(language) for language in LANGUAGES)
    for kind in ("config", "options"):
        assert {s: sorted(v.get("data", {})) for s, v in en[kind]["step"].items()} == {
            s: sorted(v.get("data", {})) for s, v in sv[kind]["step"].items()
        }


def _meter(hass: HomeAssistant, owner: Any) -> str:
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("ocpp", "meter")}, name="meter"
    )
    entry = er.async_get(hass).async_get_or_create(
        "sensor", "ocpp", "meter_current", device_id=device.id, config_entry=owner,
        suggested_object_id="meter",
    )
    hass.states.async_set(
        entry.entity_id, "unknown",
        {"device_class": "current", "unit_of_measurement": "A", "L1": 5.0, "L2": 6.0, "L3": 7.0},
    )
    return entry.entity_id


async def test_every_options_form_labels_its_fields(
    hass: HomeAssistant, shown: dict[tuple[str, str], set[str]]
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner")
    switch = create_ocpp_charger_device(hass, ocpp_entry=owner, device_unique_id="c1", switch_object_id="c1")
    charger = make_entry(hass, entry_id="c1", charge_control=switch, current_limit=None, webhook_id="h", title="C1")
    meter = _meter(hass, owner)
    site = make_site_entry(hass, entry_id="site", charger_entry_ids=[charger.entry_id])

    # The charger's own controls.
    result = await hass.config_entries.options.async_init(charger.entry_id)
    assert result["type"] is FlowResultType.FORM
    hass.config_entries.options.async_abort(result["flow_id"])

    # The site: basics, then the measurement steps behind "change measurement".
    result = await hass.config_entries.options.async_init(site.entry_id)
    assert result["step_id"] == "site_init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Site",
            "main_fuse_a": 25.0,
            "safety_margin_a": 1.0,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [charger.entry_id],
            "site_enabled": True,
            "change_measurement": True,
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"choice": "manual_attributes"})
    if result["step_id"] == "site_manual_source":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"entity_id": meter, "attribute_L1": "L1", "attribute_L2": "L2", "attribute_L3": "L3"}
        )
    assert result["step_id"] == "site_details"
    hass.config_entries.options.async_abort(result["flow_id"])

    assert {"init", "site_init", "site_current_suggestions", "site_manual_source", "site_details"} <= {step for kind, step in shown if kind == "options"}
    assert {"name", "measurement_mode", "charger_entry_ids", "solar_forecast_entries"} <= shown[("options", "site_init")]
    _assert_labelled(shown)


async def test_every_config_form_labels_its_fields(
    hass: HomeAssistant, shown: dict[tuple[str, str], set[str]]
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner")
    _meter(hass, owner)
    flow = hass.config_entries.flow

    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await flow.async_configure(
        result["flow_id"],
        {"name": "Home", "main_fuse_a": 25, "safety_margin_a": 1, "measurement_mode": "direct_phase_current", "charger_entry_ids": []},
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await flow.async_configure(result["flow_id"], {"choice": "skip"})
    assert result["step_id"] == "site_details"
    flow.async_abort(result["flow_id"])

    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "charger"})
    flow.async_abort(result["flow_id"])

    assert {"user", "site", "site_current_suggestions", "site_details", "charger_type"} <= {step for kind, step in shown if kind == "config"}
    _assert_labelled(shown)


async def test_every_charger_wiring_step_labels_its_fields(
    hass: HomeAssistant, shown: dict[tuple[str, str], set[str]]
) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner")
    switch = create_ocpp_charger_device(hass, ocpp_entry=owner, device_unique_id="c1", switch_object_id="c1")
    charger = make_entry(hass, entry_id="c1", charge_control=switch, current_limit=None, webhook_id="h", title="C1")
    flow = hass.config_entries.flow
    meter = {"direct_L1": "sensor.a", "direct_L2": "sensor.b", "direct_L3": "sensor.c"}

    for choice, manual_step in (("manual", "charger_manual_source"), ("manual_entities", "charger_manual_entities")):
        # The create flow.
        result = await flow.async_init(DOMAIN, context={"source": "user"})
        result = await flow.async_configure(result["flow_id"], {"entry_type": "site"})
        result = await flow.async_configure(
            result["flow_id"],
            {"name": "Home", "main_fuse_a": 25, "safety_margin_a": 1, "measurement_mode": "direct_phase_current", "charger_entry_ids": [charger.entry_id]},
        )
        assert result["step_id"] == "site_current_suggestions"
        result = await flow.async_configure(result["flow_id"], {"choice": "manual"})
        assert result["step_id"] == "site_details"
        result = await flow.async_configure(result["flow_id"], meter)
        assert result["step_id"] == "site_charger_wiring"
        result = await flow.async_configure(result["flow_id"], {"phases": 1, "phase": "L2", "measured_source": choice})
        assert result["step_id"] == manual_step
        flow.async_abort(result["flow_id"])

        # The options flow.
        site = make_site_entry(hass, entry_id=f"site_{choice}", charger_entry_ids=[charger.entry_id])
        result = await hass.config_entries.options.async_init(site.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "name": "Site", "main_fuse_a": 25.0, "safety_margin_a": 1.0,
                "measurement_mode": "direct_phase_current", "charger_entry_ids": [charger.entry_id],
                "site_enabled": True, "change_measurement": True,
            },
        )
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"choice": "manual"})
        assert result["step_id"] == "site_details"
        result = await hass.config_entries.options.async_configure(result["flow_id"], meter)
        assert result["step_id"] == "site_charger_wiring"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"phases": 3, "measured_source": choice})
        assert result["step_id"] == manual_step
        hass.config_entries.options.async_abort(result["flow_id"])
        await hass.config_entries.async_remove(site.entry_id)

    for kind in ("config", "options"):
        assert shown[(kind, "site_charger_wiring")] == {"phases", "phase", "measured_source"}
        assert shown[(kind, "charger_manual_entities")] == {"entity_L1", "entity_L2", "entity_L3"}
        assert (kind, "charger_manual_source") in shown
    _assert_labelled(shown)


async def test_derived_site_details_fields_are_labelled(
    hass: HomeAssistant, shown: dict[tuple[str, str], set[str]]
) -> None:
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await flow.async_configure(
        result["flow_id"],
        {"name": "Home", "main_fuse_a": 25, "safety_margin_a": 1, "measurement_mode": "derived_phase_current", "charger_entry_ids": []},
    )
    assert result["step_id"] == "site_details"
    flow.async_abort(result["flow_id"])

    assert {"derived_L1_power", "derived_L3_voltage", "derived_L2_reactive_power", "grid_power_inverted"} <= shown[("config", "site_details")]
    _assert_labelled(shown)
