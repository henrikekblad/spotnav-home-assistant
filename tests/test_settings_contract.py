"""The public settings contract: one value, full replacement, strict decoding.

Pure tests over `api/settings.py`: no `hass`, no store, no controller. They pin the wire shape a
phone and a dashboard both read, every place absence differs from zero, and the promise that the wire
shape is written by the contract itself rather than by the storage record's own helpers.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import time

import pytest
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.auto_settings import (
    DRIVER_TARGET_SOC,
    PAUSE_UNTIL_RESUMED,
    STRATEGY_HYBRID,
    AreaAutoSettings,
    AutoSettings,
    AutoSettingsError,
    FiscalOverride,
    PauseIntent,
    TargetSocIntent,
)
from custom_components.spotnav.api.settings import (
    SETTINGS_API_VERSION,
    SETTINGS_ENVELOPE_KEYS,
    SETTINGS_KEYS,
    SETTINGS_RESPONSE_KEYS,
    decode_settings,
    encode_pause,
    encode_settings,
    encoded_override,
    encoded_target,
    expected_revision_from,
    settings_envelope,
    settings_failure,
)


def full_settings() -> AutoSettings:
    """Every field of the public value set to something other than its default."""
    return AutoSettings(
        revision=7,
        area_id="SE4",
        overrides=(
            AreaAutoSettings(
                area_id="SE4",
                vat=FiscalOverride(enabled=True, value=25.0),
                tax=FiscalOverride(enabled=True, value=None),
                transfer=FiscalOverride(enabled=False, value=None),
            ),
            AreaAutoSettings(area_id="FI", vat=FiscalOverride(enabled=True, value=0.0)),
        ),
        phases=3,
        amps=16,
        requested_kwh=22.5,
        max_periods=4,
        departure_enabled=True,
        departure=time(6, 45),
        pause=PauseIntent(choice=PAUSE_UNTIL_RESUMED, admitted_at=dt_util.utcnow()),
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="vehicle-1", target_percent=80.0),
        strategy=STRATEGY_HYBRID,
    )


def body_of(settings: AutoSettings) -> dict:
    """The public value without its revision: exactly what a replacement body carries."""
    encoded = encode_settings(settings)
    return {key: value for key, value in encoded.items() if key != "revision"}


def test_every_field_survives_a_round_trip():
    settings = full_settings()
    decoded = decode_settings(body_of(settings))

    assert decoded.area_id == settings.area_id
    # Overrides are canonicalised by the domain (a stable order by area id), so the round trip is
    # compared as a set of records rather than by the order the body happened to list them in.
    assert {item.area_id: item for item in decoded.overrides} == {
        item.area_id: item for item in settings.overrides
    }
    assert decoded.phases == settings.phases
    assert decoded.amps == settings.amps
    assert decoded.requested_kwh == settings.requested_kwh
    assert decoded.max_periods == settings.max_periods
    assert decoded.departure_enabled is True
    assert decoded.departure == settings.departure
    assert decoded.strategy == STRATEGY_HYBRID
    assert decoded.driver == settings.driver
    assert decoded.target == settings.target
    # A body never carries the record's revision, and decoding never invents one.
    assert decoded.revision == 0
    assert "revision" not in SETTINGS_KEYS


def test_the_public_keys_are_exactly_the_documented_ones():
    encoded = encode_settings(full_settings())
    assert set(encoded) == SETTINGS_RESPONSE_KEYS
    assert encoded["departure_time"] == "06:45"
    assert encoded["strategy"] == "hybrid"
    assert encoded["overrides"][0]["vat"] == {"enabled": True, "value": 25.0}
    assert encoded["target"] == {"vehicle_id": "vehicle-1", "target_percent": 80.0}
    # The retired keys never come back: no estimate switch, no pause Boolean, no authority mode,
    # and no vehicle properties (those live on the vehicle).
    for retired in (
        "allow_estimated_prices",
        "execution_paused",
        "mode",
        "consumption_kwh_per_10km",
    ):
        assert retired not in encoded
    assert "remembered_capacity_kwh" not in encoded["target"]


def test_absence_is_not_zero_at_every_nesting_level():
    settings = replace(
        AutoSettings(),
        area_id=None,
        phases=None,
        amps=None,
        overrides=(AreaAutoSettings(area_id="SE4", vat=FiscalOverride(enabled=True, value=0.0)),),
        # `target_percent = 0` is a legal choice ("charge to nothing"), which is exactly the case where
        # zero must not be confused with "no target".
        target=TargetSocIntent(vehicle_id=None, target_percent=0.0),
    )
    decoded = decode_settings(body_of(settings))

    assert decoded.area_id is None
    assert decoded.phases is None
    assert decoded.amps is None
    assert decoded.overrides[0].vat == FiscalOverride(enabled=True, value=0.0)
    assert decoded.target.target_percent == 0.0  # zero is a value, not an absence
    assert decoded.overrides[0].tax == FiscalOverride(enabled=False, value=None)
    assert decoded.target.vehicle_id is None
    # And the domain still refuses what it always refused: a zero energy request is not a plan.
    body = body_of(replace(settings, requested_kwh=0.0))
    with pytest.raises(AutoSettingsError):
        decode_settings(body)


def test_fiscal_has_its_four_distinguishable_states():
    settings = replace(
        AutoSettings(),
        overrides=(
            AreaAutoSettings(
                area_id="SE4",
                vat=FiscalOverride(enabled=False, value=None),  # off
                tax=FiscalOverride(enabled=True, value=None),  # the catalogue's suggestion
                transfer=FiscalOverride(enabled=True, value=0.0),  # an explicit zero
            ),
        ),
    )
    decoded = decode_settings(body_of(settings))
    override = decoded.overrides[0]
    assert (override.vat.enabled, override.vat.value) == (False, None)
    assert (override.tax.enabled, override.tax.value) == (True, None)
    assert (override.transfer.enabled, override.transfer.value) == (True, 0.0)
    # Off with a zero is not the same value as off, and the codec keeps the difference.
    assert decode_settings(body_of(settings)) == decoded


@pytest.mark.parametrize(
    "mutation, code",
    [
        (lambda body: body.pop("max_periods"), "missing_field"),
        (lambda body: body.update(revision=3), "unknown_field"),
        (lambda body: body.update(nonsense=True), "unknown_field"),
        (lambda body: body["target"].pop("vehicle_id"), "missing_field"),
        (lambda body: body["target"].update(extra=1), "unknown_field"),
        (lambda body: body["overrides"][0]["vat"].pop("value"), "missing_field"),
        (lambda body: body["overrides"][0].update(area="SE4"), "unknown_field"),
    ],
)
def test_exact_keys_are_enforced_at_every_nesting_level(mutation, code):
    body = body_of(replace(full_settings(), overrides=(AreaAutoSettings(area_id="SE4"),)))
    mutation(body)
    with pytest.raises(AutoSettingsError) as refusal:
        decode_settings(body)
    assert refusal.value.code == code


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("departure_enabled", "false", "invalid_departure"),
        ("requested_kwh", True, "invalid_energy"),
        ("requested_kwh", float("nan"), "invalid_energy"),
        ("requested_kwh", float("inf"), "invalid_energy"),
        ("max_periods", 1.5, "invalid_periods"),
        ("phases", "3", "invalid_phases"),
        ("amps", 16.5, "invalid_amps"),
        ("strategy", "fastest", "invalid_strategy"),
        ("strategy", None, "invalid_strategy"),
        ("driver", "battery", "invalid_driver"),
        ("departure_time", "6:45", "invalid_departure"),
        ("departure_time", "24:00", "invalid_departure"),
        ("area_id", 4, "invalid_area"),
    ],
)
def test_types_and_domains_are_enforced_without_coercion(field, value, code):
    body = body_of(full_settings())
    body[field] = value
    with pytest.raises(AutoSettingsError) as refusal:
        decode_settings(body)
    assert refusal.value.code == code


def test_a_nested_fiscal_flag_must_be_a_real_boolean():
    body = body_of(full_settings())
    body["overrides"][0]["vat"]["enabled"] = "false"  # truthy string, refused by name
    with pytest.raises(AutoSettingsError) as refusal:
        decode_settings(body)
    assert refusal.value.code == "invalid_fiscal"


@pytest.mark.parametrize(
    "retired, value",
    [
        ("allow_estimated_prices", False),
        ("execution_paused", False),
        ("mode", "auto_price"),
        ("consumption_kwh_per_10km", 2.0),
    ],
)
def test_a_retired_key_is_an_unknown_field(retired, value):
    """Nothing keeps a dead key alive: a body that still carries one is refused by name."""
    body = body_of(full_settings())
    body[retired] = value
    with pytest.raises(AutoSettingsError) as refusal:
        decode_settings(body)
    assert refusal.value.code == "unknown_field"
    body = body_of(full_settings())
    body["target"]["remembered_capacity_kwh"] = 60.0
    with pytest.raises(AutoSettingsError) as refusal:
        decode_settings(body)
    assert refusal.value.code == "unknown_field"


def test_the_target_domain_rules_still_apply():
    body = body_of(full_settings())
    body["target"]["target_percent"] = 101
    with pytest.raises(AutoSettingsError):
        decode_settings(body)


def test_public_encoding_is_independent_of_the_storage_helpers(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("the wire contract must not call a storage serializer")

    monkeypatch.setattr(FiscalOverride, "as_dict", boom)
    monkeypatch.setattr(AreaAutoSettings, "as_dict", boom)
    monkeypatch.setattr(TargetSocIntent, "as_dict", boom)
    monkeypatch.setattr(AutoSettings, "as_dict", boom)

    encoded = encode_settings(full_settings())
    assert encoded["overrides"][0]["vat"] == {"enabled": True, "value": 25.0}
    assert encoded["target"]["vehicle_id"] == "vehicle-1"
    assert encoded_override(full_settings().overrides[0])["area_id"] == "SE4"
    assert encoded_target(full_settings().target)["target_percent"] == 80.0


@pytest.mark.parametrize("value", [None, True, False, -1, 1.5, "8", float("nan")])
def test_the_expected_revision_must_be_a_whole_number(value):
    with pytest.raises(AutoSettingsError) as refusal:
        expected_revision_from(value)
    assert refusal.value.code == "invalid_number"
    assert expected_revision_from(0) == 0
    assert expected_revision_from(8) == 8


def test_the_envelopes_are_one_shape_for_both_outcomes():
    settings = full_settings()
    success = settings_envelope(settings)
    assert SETTINGS_API_VERSION == 1
    assert success == {
        "api_version": SETTINGS_API_VERSION,
        "ok": True,
        "error": None,
        "settings": encode_settings(settings),
        "pause": encode_pause(settings.pause),
    }
    assert set(success) == SETTINGS_ENVELOPE_KEYS
    conflict = settings_failure("revision_conflict", replace(settings, revision=9))
    assert conflict["api_version"] == SETTINGS_API_VERSION
    assert conflict["ok"] is False
    assert conflict["error"] == "revision_conflict"
    assert conflict["settings"] == encode_settings(replace(settings, revision=9))
    assert conflict["pause"] == encode_pause(settings.pause)
    # An entry that cannot be resolved has nothing readable to send, and says so instead of faking one.
    unknown = settings_failure("spotnav_unknown_charger", None)
    assert unknown["settings"] is None and unknown["pause"] is None


def test_a_body_that_is_not_an_object_is_refused_by_code():
    for raw in (None, 3, "settings", [1, 2]):
        with pytest.raises(AutoSettingsError) as refusal:
            decode_settings(raw)
        assert refusal.value.code in {"unknown_field", "missing_field"}
    assert math.isfinite(full_settings().requested_kwh)


def test_a_replacement_carries_the_stored_pause_through_untouched() -> None:
    """A settings body has no pause at all: the mutator keeps the stored intent exactly, whatever the
    record it replaces held -- an unrelated edit can never clear, admit or re-time a pause."""
    from custom_components.spotnav.api.settings import replacement_mutator

    stored_pause = PauseIntent(choice=PAUSE_UNTIL_RESUMED, admitted_at=dt_util.utcnow())
    current = replace(full_settings(), pause=stored_pause)
    incoming = decode_settings(body_of(replace(full_settings(), amps=10, pause=PauseIntent())))

    result = replacement_mutator(incoming)(current)

    assert result.amps == 10
    assert result.pause == stored_pause
