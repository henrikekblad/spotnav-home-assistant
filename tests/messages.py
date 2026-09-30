"""WebSocket request builders and audit helpers for the settings, entity-config and site-settings contracts."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.planning.auto_settings import AutoSettings
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.api.entity_config import ENTITY_CONFIG_API_VERSION
from custom_components.spotnav.api.site_settings import SITE_SETTINGS_API_VERSION
from custom_components.spotnav.runtime import domain_data


BANNED_TEXT = (
    "webhook-a",
    "webhook-b",
    "webhook-c",
    "switch.charger_a",
    "switch.charger_b",
    "switch.charger_c",
    "number.charger_a_limit",
    "charging_current",
    "spotnav_auto_settings",
    "store_key",
    "access_token",
    "Bearer",
    "Traceback",
    "exploded",
)


BANNED_KEYS = frozenset(
    {
        "intervals",
        "interval_count",
        "effective_price",
        "raw_price",
        "fallback_price",
        "resolutions_minutes",
        "provenance",
    }
)


def audit_privacy(value: Any, path: str = "result") -> None:
    """Walk one answer and refuse any charger material, credential or price document in it."""
    if isinstance(value, dict):
        assert BANNED_KEYS.isdisjoint(value), f"{path} carries a price document key"
        for key, item in value.items():
            audit_privacy(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            audit_privacy(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        for banned in BANNED_TEXT:
            assert banned not in value, f"{path} leaks {banned!r}"


def break_persistence(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deterministic persistence seam: the save that must succeed raises instead."""
    store = domain_data(hass).auto_store
    assert store is not None

    async def failing_save(_data: Any) -> None:
        raise RuntimeError("the disk said no")

    monkeypatch.setattr(store._store, "async_save", failing_save)


def forbid_charger_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Close every route from the settings path to a charger, and report what tried to use one."""
    seen: list[str] = []

    def forbidden(name: str):
        async def record(*_args, **_kwargs):
            seen.append(f"ChargingController.{name}")
            raise AssertionError(f"a refused settings request reached ChargingController.{name}")

        return record

    for name in (
        "async_install",
        "async_start",
        "async_stop",
        "async_cancel",
        "async_follow_schedule",
        "async_enforce_target",
    ):
        if hasattr(ChargingController, name):
            monkeypatch.setattr(ChargingController, name, forbidden(name))
    return seen


def read_settings_message(entry_id: str = "entry_a", api_version: Any = 1) -> dict[str, Any]:
    return {"id": 1, "type": "spotnav/get_settings", "api_version": api_version, "charger_id": entry_id}


def stored(hass: HomeAssistant, entry_id: str = "entry_a") -> AutoSettings:
    store = domain_data(hass).auto_store
    assert store is not None
    return store.settings(entry_id)


def update_settings_message(entry_id: str, revision: Any, body: Any, api_version: Any = 1) -> dict[str, Any]:
    return {
        "id": 2,
        "type": "spotnav/update_settings",
        "api_version": api_version,
        "charger_id": entry_id,
        "expected_revision": revision,
        "settings": body,
    }


def field(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in result["config"]["fields"] if item["field"] == name)


def get_message(charger_id: Any, api_version: Any = ENTITY_CONFIG_API_VERSION) -> dict[str, Any]:
    return {"type": "spotnav/get_entity_config", "api_version": api_version, "charger_id": charger_id}


def register(hass: HomeAssistant, domain: str, object_id: str, name: str | None = None, **attrs: Any) -> str:
    """A real registry entity with a live state."""
    entry = er.async_get(hass).async_get_or_create(
        domain, "test", f"uid_{object_id}", suggested_object_id=object_id
    )
    hass.states.async_set(
        entry.entity_id, "0", {"friendly_name": name or object_id, **attrs}
    )
    return entry.entity_id


def update_entity_config_message(
    charger_id: Any, *, scope: Any, expected: Any = None, changes: Any = None
) -> dict[str, Any]:
    return {
        "type": "spotnav/update_entity_config",
        "api_version": ENTITY_CONFIG_API_VERSION,
        "charger_id": charger_id,
        "scope": scope,
        "expected": {} if expected is None else expected,
        "changes": {} if changes is None else changes,
    }


def update_site_settings_message(
    *,
    charger_id: Any = "entry_a",
    expected: Any = None,
    changes: Any = None,
    api_version: Any = SITE_SETTINGS_API_VERSION,
    omit_expected: bool = False,
    omit_changes: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "spotnav/update_site_settings", "api_version": api_version}
    if charger_id is not None:
        body["charger_id"] = charger_id
    if not omit_expected:
        body["expected"] = {} if expected is None else expected
    if not omit_changes:
        body["changes"] = {} if changes is None else changes
    return body
