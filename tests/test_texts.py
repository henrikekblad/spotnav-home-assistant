"""SpotNav's own words: read from `i18n/<lang>.json` once, off the event loop, English per missing key."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav import texts
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.vehicles.discovery import (
    REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH,
    REASON_ATTRIBUTES_HISTORICAL_MATCH,
    REASON_ATTRIBUTES_PROFILE_MATCH,
    REASON_ATTRIBUTES_UNIT_MATCH_ONLY,
    REASON_POSSIBLE_INVERTER_OUTPUT,
    REASON_SEPARATE_ENTITIES_DEVICE_CLASS_AND_UNIT_MATCH,
    REASON_SEPARATE_ENTITIES_DEVICE_CLASS_MATCH_ONLY,
    REASON_SEPARATE_ENTITIES_NAME_MATCH_ONLY,
    REASON_SEPARATE_ENTITIES_PROFILE_MATCH,
    REASON_SEPARATE_ENTITIES_UNIT_MATCH_ONLY,
)


@pytest.fixture
def unloaded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The texts not yet read; the list records the thread every read ran on."""
    monkeypatch.setattr(texts, "_catalog", None)
    threads: list[str] = []
    original = texts.load

    def recording() -> None:
        threads.append(threading.current_thread().name)
        original()

    monkeypatch.setattr(texts, "load", recording)
    return threads


def test_a_missing_key_falls_back_to_english_key_by_key(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text(json.dumps({"ns": {"a": "A", "b": "B"}, "other": {"c": "C"}}))
    (tmp_path / "xx.json").write_text(json.dumps({"ns": {"a": "X"}}))

    merged = texts._merge(texts.read_files(tmp_path))

    assert dict(merged["xx"]["ns"]) == {"a": "X", "b": "B"}
    assert dict(merged["xx"]["other"]) == {"c": "C"}
    assert dict(merged["en"]["ns"]) == {"a": "A", "b": "B"}


def test_an_unknown_language_reads_english() -> None:
    assert texts.table("it", "site_form")["default_name"] == "Site"
    assert texts.table("de", "site_form")["default_name"] == "Standort"
    assert texts.table("sv", "site_form")["default_name"] == "Anläggning"


def test_home_assistants_language_picks_a_file() -> None:
    codes = ("sv", "sv-SE", "nb", "no", "nn", "da", "fi", "de", "nl-BE", "fr_FR", "es", "it", "en_GB", None)
    assert [texts.language_of(code) for code in codes] == [
        "sv", "sv", "nb", "nb", "nb", "da", "fi", "de", "nl", "fr", "es", "en", "en", "en",
    ]


def test_every_discovery_reason_code_has_words() -> None:
    reasons = {
        REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH,
        REASON_ATTRIBUTES_HISTORICAL_MATCH,
        REASON_ATTRIBUTES_PROFILE_MATCH,
        REASON_ATTRIBUTES_UNIT_MATCH_ONLY,
        REASON_POSSIBLE_INVERTER_OUTPUT,
        REASON_SEPARATE_ENTITIES_DEVICE_CLASS_AND_UNIT_MATCH,
        REASON_SEPARATE_ENTITIES_DEVICE_CLASS_MATCH_ONLY,
        REASON_SEPARATE_ENTITIES_NAME_MATCH_ONLY,
        REASON_SEPARATE_ENTITIES_PROFILE_MATCH,
        REASON_SEPARATE_ENTITIES_UNIT_MATCH_ONLY,
    }
    assert set(texts.table("en", "candidate_reason")) == reasons


async def test_setup_reads_the_files_once_off_the_event_loop(hass: HomeAssistant, unloaded: list[str]) -> None:
    await texts.async_load(hass)
    await texts.async_load(hass)

    assert texts.is_loaded()
    assert len(unloaded) == 1
    assert unloaded[0] != threading.current_thread().name


async def test_the_first_config_flow_step_reads_them_before_any_label(
    hass: HomeAssistant, unloaded: list[str]
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    assert result["step_id"] == "user"
    assert len(unloaded) == 1
    assert unloaded[0] != threading.current_thread().name
