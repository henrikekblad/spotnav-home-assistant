"""The Auto settings document: defaults, validation, atomicity and round-trips.

Pure tests over the store and its models — one `hass` fixture for the `Store` itself, and
no controller, no prices and no charger anywhere.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import (
    DEFAULT_DEPARTURE,
    DEFAULT_MAX_PERIODS,
    DEFAULT_REQUESTED_KWH,
    DRIVER_TARGET_SOC,
    AreaAutoSettings,
    AutoSettings,
    AutoSettingsError,
    AutoSettingsStore,
    FiscalOverride,
    StoredProposal,
    TargetSocIntent,
    async_setup_auto_settings,
)
from .harness import FlakyStore
from custom_components.spotnav.runtime import domain_data


@pytest.fixture
def store(hass: HomeAssistant) -> AutoSettingsStore:
    return AutoSettingsStore(hass)


def test_a_missing_record_has_the_visible_defaults(store: AutoSettingsStore) -> None:
    settings = store.settings("entry-a")
    assert settings.area_id is None
    assert settings.phases is None and settings.amps is None
    assert settings.requested_kwh == DEFAULT_REQUESTED_KWH
    assert settings.max_periods == DEFAULT_MAX_PERIODS
    assert settings.departure == DEFAULT_DEPARTURE and settings.departure_enabled is True
    assert settings.driver == "manual_kwh" and settings.strategy == "cheapest"
    assert settings.revision == 0
    # Nothing guessed about what could steer a charge.
    assert settings.missing_for_auto() == ("area", "phases", "amps")


async def test_round_trip_and_revision_bump(hass: HomeAssistant) -> None:
    store = await async_setup_auto_settings(hass)
    assert domain_data(hass).auto_store is store
    assert await async_setup_auto_settings(hass) is store  # idempotent

    updated = await store.async_update(
        "entry-a",
        mutate=lambda settings: replace(
            settings, area_id="SE4", phases=1, amps=10
        ),
    )
    assert updated.revision == 1 and updated.area_id == "SE4"
    assert store.settings("entry-a") == updated

    # A second edit bumps again, and unknown entries stay untouched.
    again = await store.async_update("entry-a", mutate=lambda s: replace(s, amps=16))
    assert again.revision == 2 and again.amps == 16
    assert store.settings("entry-b").revision == 0

    # The whole document survives a fresh store reading the same storage.
    reopened = AutoSettingsStore(hass)
    await reopened.async_load()
    assert reopened.settings("entry-a") == again


async def test_compare_and_set_refuses_a_stale_edit(hass: HomeAssistant) -> None:
    store = AutoSettingsStore(hass)
    await store.async_load()
    first = await store.async_update("entry-a", mutate=lambda s: replace(s, amps=10))

    with pytest.raises(AutoSettingsError) as conflict:
        await store.async_update(
            "entry-a", mutate=lambda s: replace(s, amps=16), expected_revision=first.revision - 1
        )
    assert conflict.value.code == "revision_conflict"
    # The refused edit changed nothing.
    assert store.settings("entry-a").amps == 10

    accepted = await store.async_update(
        "entry-a", mutate=lambda s: replace(s, amps=16), expected_revision=first.revision
    )
    assert accepted.amps == 16 and accepted.revision == first.revision + 1


async def test_two_chargers_editing_at_once_lose_nothing(hass: HomeAssistant) -> None:
    store = AutoSettingsStore(hass)
    await store.async_load()
    await asyncio.gather(
        store.async_update("entry-a", mutate=lambda s: replace(s, area_id="SE4", phases=1, amps=6)),
        store.async_update("entry-b", mutate=lambda s: replace(s, area_id="FI", phases=3, amps=16)),
    )
    assert store.settings("entry-a").area_id == "SE4"
    assert store.settings("entry-b").area_id == "FI"
    assert store.settings("entry-a").revision == 1 and store.settings("entry-b").revision == 1


def test_overrides_are_per_area_and_never_copied_across(store: AutoSettingsStore) -> None:
    settings = AutoSettings()
    sweden = AreaAutoSettings(
        area_id="SE4",
        vat=FiscalOverride(enabled=True, value=None),
        tax=FiscalOverride(enabled=True, value=36.0),
    )
    finland = AreaAutoSettings(area_id="FI", tax=FiscalOverride(enabled=True, value=2.325))
    settings = settings.with_override(sweden).with_override(finland)

    assert settings.override_for("SE4").tax.value == 36.0
    assert settings.override_for("FI").tax.value == 2.325
    # Switching to an area with no override gives an all-off record, not the other's values.
    empty = settings.override_for("NO1")
    assert empty.tax == FiscalOverride() and empty.vat == FiscalOverride()
    # Replacing one area leaves the other alone.
    changed = settings.with_override(AreaAutoSettings(area_id="SE4", tax=FiscalOverride(True, 50.0)))
    assert changed.override_for("SE4").tax.value == 50.0
    assert changed.override_for("FI").tax.value == 2.325


def test_fiscal_states_stay_distinct_in_storage(store: AutoSettingsStore) -> None:
    """Absent suggestion, present zero, and an explicit override are three things."""
    settings = AutoSettings().with_override(
        AreaAutoSettings(
            area_id="SE4",
            # Off entirely, on with no override (use the suggestion), and an explicit zero.
            vat=FiscalOverride(enabled=True, value=None),
            tax=FiscalOverride(enabled=True, value=0.0),
            transfer=FiscalOverride(enabled=False, value=None),
        )
    )
    stored = AutoSettings.from_stored(settings.validated().as_dict())
    assert stored.override_for("SE4").vat == FiscalOverride(True, None)
    assert stored.override_for("SE4").tax == FiscalOverride(True, 0.0)
    assert stored.override_for("SE4").transfer == FiscalOverride(False, None)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"driver": "weekly"}, "invalid_driver"),
        ({"area_id": ""}, "invalid_area"),
        ({"phases": 2}, "invalid_phases"),
        ({"amps": 0}, "invalid_amps"),
        ({"amps": -16}, "invalid_amps"),
        ({"amps": True}, "invalid_amps"),
        ({"amps": 200}, "invalid_amps"),
        ({"requested_kwh": 0}, "invalid_energy"),
        ({"requested_kwh": float("nan")}, "invalid_energy"),
        ({"max_periods": 0}, "invalid_periods"),
        ({"max_periods": 9}, "invalid_periods"),
        ({"departure": time(8, 0, tzinfo=timezone.utc)}, "invalid_departure"),
        ({"departure_enabled": 1}, "invalid_departure"),
        ({"target": None}, "invalid_target"),
    ],
)
def test_every_field_is_validated_without_clamping(changes: dict, code: str) -> None:
    settings = AutoSettings(**changes)
    with pytest.raises(AutoSettingsError) as caught:
        settings.validated()
    assert caught.value.code == code
    # And nothing was silently repaired into a stored value.
    assert not hasattr(settings, "validated_quietly")


def test_a_target_needs_a_percentage() -> None:
    # An empty target record is valid; a missing one is refused by name.
    assert AutoSettings(driver=DRIVER_TARGET_SOC, target=TargetSocIntent()).validated().target.vehicle_id is None
    with pytest.raises(AutoSettingsError) as absent:
        AutoSettings(driver=DRIVER_TARGET_SOC, target=None).validated()
    assert absent.value.code == "invalid_target"
    with pytest.raises(AutoSettingsError) as percent:
        AutoSettings(
            driver=DRIVER_TARGET_SOC, target=TargetSocIntent(target_percent=101)
        ).validated()
    assert percent.value.code == "invalid_target"

    # Live state of charge is not a stored setting at all: there is no field for it.
    assert "soc_percent" not in AutoSettings().as_dict()
    assert "soc_percent" not in AutoSettings().as_dict()["target"]


# --------------------------------------------------------- strictness of what is stored


@pytest.mark.parametrize(
    "component",
    [
        {"enabled": "false", "value": None},
        {"enabled": 1, "value": None},
        {"enabled": 0, "value": None},
        {"enabled": "true", "value": 12.0},
        {"value": None},
        {"enabled": True},
        {"enabled": True, "value": None, "extra": 1},
        None,
        "off",
    ],
    ids=[
        "string-false",
        "int-one",
        "int-zero",
        "string-true",
        "missing-enabled",
        "missing-value",
        "unknown-key",
        "null",
        "text",
    ],
)
def test_a_stored_fiscal_component_must_be_a_real_boolean_and_a_full_shape(component: Any) -> None:
    """`bool("false")` is `True`: a stored flag that is not a boolean cannot mean "on"."""
    with pytest.raises(AutoSettingsError) as refused:
        FiscalOverride.from_stored(component, "vat")
    assert refused.value.code in ("invalid_fiscal", "missing_field", "unknown_field")


def test_a_stored_fiscal_component_reads_back_exactly() -> None:
    """The three states survive storage: off, on with a figure, on with the suggestion."""
    for component in (FiscalOverride(False, None), FiscalOverride(True, 0.0), FiscalOverride(True, None)):
        stored = FiscalOverride.from_stored(component.as_dict(), "vat")
        assert stored == component


#: One complete, all-off fiscal component: the shape every schema-1 record carries.
OFF = {"enabled": False, "value": None}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"area_id": "SE4"}, "missing_field"),
        ({"area_id": "SE4", "vat": OFF, "tax": OFF, "transfer": OFF, "extra": 1}, "unknown_field"),
        ({"area_id": "SE4", "vat": {}, "tax": OFF, "transfer": OFF}, "missing_field"),
        ({"area_id": "", "vat": OFF, "tax": OFF, "transfer": OFF}, "invalid_area"),
        ({"area_id": 4, "vat": OFF, "tax": OFF, "transfer": OFF}, "invalid_area"),
    ],
)
def test_a_stored_area_override_is_read_by_shape(raw: Any, code: str) -> None:
    """An area record is exactly its four fields, and its id is a non-empty name."""
    with pytest.raises(AutoSettingsError) as refused:
        AreaAutoSettings.from_stored(raw)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({}, "missing_field"),
        ({"vehicle_id": "v", "target_percent": 80, "z": 1}, "unknown_field"),
        ({"vehicle_id": "v", "target_percent": 80, "remembered_capacity_kwh": 60.0}, "unknown_field"),
        ({"vehicle_id": "v"}, "missing_field"),
        ({"vehicle_id": 7, "target_percent": 80}, "invalid_target"),
        ({"vehicle_id": None, "target_percent": "80"}, "invalid_target"),
        ({"vehicle_id": None, "target_percent": 101}, "invalid_target"),
    ],
)
def test_a_stored_target_is_read_by_shape(raw: Any, code: str) -> None:
    """Every field is present, nullable where the intent can be empty, and never coerced."""
    with pytest.raises(AutoSettingsError) as refused:
        TargetSocIntent.from_stored(raw)
    assert refused.value.code == code


def stored_settings(**changes: Any) -> dict[str, Any]:
    """A complete schema-1 settings record, with these changes applied."""
    document = AutoSettings().validated().as_dict()
    document.update(changes)
    return document


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"area_id": 4}, "invalid_area"),
        ({"phases": "1"}, "invalid_phases"),
        ({"amps": "10"}, "invalid_amps"),
        ({"amps": True}, "invalid_amps"),
        ({"requested_kwh": "20"}, "invalid_energy"),
        ({"max_periods": True}, "invalid_periods"),
        ({"revision": True}, "invalid_number"),
        ({"revision": -1}, "invalid_number"),
        ({"departure_enabled": "yes"}, "invalid_departure"),
        ({"departure": None}, "invalid_departure"),
        ({"departure": "not a time"}, "invalid_departure"),
        ({"departure": "08:00:00+02:00"}, "invalid_departure"),
        ({"overrides": {"SE4": {}}}, "invalid_area"),
        ({"overrides": [{"area_id": "SE4"}]}, "missing_field"),
        ({"target": None}, "invalid_target"),
    ],
)
def test_a_stored_settings_record_is_never_coerced(changes: dict, code: str) -> None:
    """Wrong types are refused: a plausible-looking value is not the value that was stored."""
    raw = stored_settings()
    for key, value in changes.items():
        raw[key] = value
    with pytest.raises(AutoSettingsError) as refused:
        AutoSettings.from_stored(raw)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("drop", "add", "code"),
    [
        ("amps", None, "missing_field"),
        (None, {"amperage": 10}, "unknown_field"),
        ("target", None, "missing_field"),
    ],
)
def test_a_stored_settings_record_needs_every_field_and_no_others(
    drop: str | None, add: dict | None, code: str
) -> None:
    """The shape is what `as_dict` writes: a misspelled or absent key is not this release."""
    raw = stored_settings()
    if drop is not None:
        del raw[drop]
    if add is not None:
        raw.update(add)
    with pytest.raises(AutoSettingsError) as refused:
        AutoSettings.from_stored(raw)
    assert refused.value.code == code


def test_a_complete_stored_record_round_trips_exactly() -> None:
    """Everything a person chose survives the round trip, overrides included."""
    settings = AutoSettings(
        revision=3,
        area_id="SE4",
        overrides=(
            AreaAutoSettings(area_id="SE4", tax=FiscalOverride(True, 36.0), vat=FiscalOverride(True, None)),
        ),
        phases=3,
        amps=16,
        requested_kwh=32.0,
        max_periods=2,
        departure_enabled=True,
        departure=time(6, 30),
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-1", target_percent=80.0),
        strategy="solar",
    )

    stored = AutoSettings.from_stored(settings.validated().as_dict())

    assert stored == settings
    assert stored.departure == time(6, 30)
    assert stored.override_for("SE4").tax == FiscalOverride(True, 36.0)
    assert stored.strategy == "solar"


@pytest.mark.parametrize("retired", ["consumption_kwh_per_10km", "allow_estimated_fallback"])
def test_a_stored_record_with_a_retired_field_is_refused(retired: str) -> None:
    """The record is exactly what `as_dict` writes: a field this release dropped is not this release."""
    stored = AutoSettings(area_id="SE4", phases=3, amps=16).validated().as_dict()
    stored[retired] = 2.0
    with pytest.raises(AutoSettingsError) as refused:
        AutoSettings.from_stored(stored)
    assert refused.value.code == "unknown_field"


# ----------------------------------------------------- a stored proposal, read strictly


def a_proposal(**changes: Any) -> StoredProposal:
    """One valid stored summary, with these changes applied to its dictionary."""
    stamp = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    base = StoredProposal(
        calculated_at=stamp,
        state="proposal_ready",
        reason="ready",
        settings_revision=1,
        area_id="SE4",
        slots_needed=4,
        priced_slots=4,
        unpriced_slots=0,
        delivered_kwh=4.6,
        estimated_cost=1.234,
        distance_mil=2.3,
        period_starts=(stamp.isoformat(),),
        period_ends=((stamp + timedelta(hours=1)).isoformat(),),
    )
    document = base.as_dict()
    document.update(changes)
    return document


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"slots_needed": True}, "invalid_proposal"),
        ({"slots_needed": -1}, "invalid_proposal"),
        ({"priced_slots": "4"}, "invalid_proposal"),
        ({"unpriced_slots": 2.5}, "invalid_proposal"),
        ({"priced_slots": 5}, "invalid_proposal"),
        ({"unpriced_slots": 2, "unpriced": False}, "invalid_proposal"),
        ({"unpriced": "yes"}, "invalid_proposal"),
        ({"state": "external"}, "invalid_proposal"),
        ({"reason": "no_prices_yet"}, "invalid_proposal"),
        ({"reason": None}, "invalid_proposal"),
        ({"calculated_at": 1758500000}, "invalid_proposal"),
        ({"calculated_at": "not a time"}, "invalid_proposal"),
        ({"calculated_at": "2026-09-22T06:00:00"}, "invalid_proposal"),
        ({"settings_revision": True}, "invalid_proposal"),
        ({"area_id": ""}, "invalid_proposal"),
        ({"area_id": 4}, "invalid_proposal"),
        ({"price_identity": 7}, "invalid_proposal"),
        ({"delivered_kwh": "4.6"}, "invalid_proposal"),
        ({"delivered_kwh": -1.0}, "invalid_proposal"),
        ({"delivered_kwh": float("inf")}, "invalid_proposal"),
        ({"distance_mil": float("nan")}, "invalid_proposal"),
        ({"period_starts": ["2026-09-22T06:00:00"]}, "invalid_proposal"),
        ({"period_starts": ["nonsense"], "period_ends": ["nonsense"]}, "invalid_proposal"),
        ({"period_ends": ["2026-09-22T05:00:00+00:00"]}, "invalid_proposal"),
        ({"period_ends": []}, "invalid_proposal"),
        ({"period_starts": [], "period_ends": []}, "invalid_proposal"),
        ({"period_starts": ["2026-09-22T06:00:00+00:00"] * 5, "period_ends": ["2026-09-22T07:00:00+00:00"] * 5}, "invalid_proposal"),
        (
            {
                "period_starts": ["2026-09-22T08:00:00+00:00", "2026-09-22T06:00:00+00:00"],
                "period_ends": ["2026-09-22T09:00:00+00:00", "2026-09-22T07:00:00+00:00"],
            },
            "invalid_proposal",
        ),
        ({"unknown": 1}, "unknown_field"),
    ],
)
def test_a_stored_proposal_is_read_strictly(changes: dict, code: str) -> None:
    """Every field of the one record a person may read back is checked, and none coerced."""
    with pytest.raises(AutoSettingsError) as refused:
        StoredProposal.from_stored(a_proposal(**changes))
    assert refused.value.code == code


def test_a_stored_proposal_needs_every_field_of_the_written_shape() -> None:
    """A missing key is not "the default": the writer always writes all of them."""
    whole = a_proposal()
    assert set(whole) == {
        "calculated_at",
        "state",
        "reason",
        "settings_revision",
        "area_id",
        "slots_needed",
        "priced_slots",
        "unpriced_slots",
        "delivered_kwh",
        "estimated_cost",
        "distance_mil",
        "period_starts",
        "period_ends",
        "price_identity",
        "unpriced",
    }
    for key in whole:
        partial = a_proposal()
        del partial[key]
        with pytest.raises(AutoSettingsError) as refused:
            StoredProposal.from_stored(partial)
        assert refused.value.code == "missing_field", key


def test_a_stored_proposal_round_trips_with_both_period_bounds() -> None:
    """Both ends of every period survive, and a partly estimated one says so."""
    stamp = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    original = StoredProposal(
        calculated_at=stamp,
        state="proposal_unpriced",
        reason="unpriced",
        settings_revision=2,
        area_id="SE4",
        slots_needed=8,
        priced_slots=6,
        unpriced_slots=2,
        delivered_kwh=9.2,
        # Negative prices are prices: a stored cost may be below zero.
        estimated_cost=-0.5,
        distance_mil=4.6,
        period_starts=(stamp.isoformat(), (stamp + timedelta(hours=2)).isoformat()),
        period_ends=((stamp + timedelta(hours=1)).isoformat(), (stamp + timedelta(hours=3)).isoformat()),
        price_identity="2026-09-22/2026-09-23|rev|stamp",
        unpriced=True,
    )

    stored = StoredProposal.from_stored(original.as_dict())

    assert stored == original
    assert len(stored.period_starts) == len(stored.period_ends) == 2
    assert stored.unpriced is True and stored.estimated_cost == -0.5


# ----------------------------------------------------------- loading tolerance, warnings


def stored_document(**chargers: Any) -> dict[str, Any]:
    return {"schema": 1, "chargers": chargers}


def record(settings: Any, proposal: Any = None) -> dict[str, Any]:
    return {"settings": settings, "proposal": proposal}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "a string",
        {"schema": 99, "chargers": {}},
        {"chargers": {}},
        stored_document() | {"chargers": []},
    ],
    ids=["empty", "not-a-document", "future-schema", "no-schema", "not-a-mapping"],
)
async def test_a_document_this_release_does_not_understand_is_ignored(
    hass: HomeAssistant, flaky: FlakyStore, payload: Any
) -> None:
    """Bad content is one warning and the defaults: refusing to start would be worse."""
    flaky.payload = payload
    store = AutoSettingsStore(hass, store=flaky)

    await store.async_load()

    assert store.loaded is True
    assert store.entry_ids() == ()
    assert store.settings("entry-a") == AutoSettings()
    assert store.proposal("entry-a") is None


async def test_one_bad_charger_record_does_not_stop_the_others(
    hass: HomeAssistant, flaky: FlakyStore
) -> None:
    """Each record is read on its own: one malformed charger keeps only its own defaults."""
    good = AutoSettings(amps=10, area_id="SE4", phases=1).validated().as_dict()
    flaky.payload = stored_document(
        **{
            "entry-good": record(good),
            "entry-bad-shape": record({"amperage": 10}),
            "entry-bad-value": record(good | {"amps": "10"}),
            "entry-bad-proposal": record(good, {"state": "external"}),
            "entry-not-a-record": "nonsense",
            "": record(good),
        }
    )
    store = AutoSettingsStore(hass, store=flaky)

    await store.async_load()

    assert store.entry_ids() == ("entry-bad-proposal", "entry-good")
    assert store.settings("entry-good").amps == 10
    assert store.settings("entry-bad-shape") == AutoSettings()
    assert store.settings("entry-bad-value") == AutoSettings()
    assert store.settings("entry-good").revision == 0
    # The proposal was the malformed half, so the *settings* still loaded and the summary
    # was dropped rather than guessed at.
    assert store.proposal("entry-bad-proposal") is None


async def test_the_warnings_name_a_code_and_never_a_value(
    hass: HomeAssistant, flaky: FlakyStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A log line about stored data must be useful without quoting what was stored."""
    # A complete record with two extra keys: the shape is what is refused, so this is the
    # unknown-field warning, and neither the names nor the value may be quoted back.
    bogus = AutoSettings().validated().as_dict() | {"amperage": 10, "secret": "s3cr3t"}
    flaky.payload = stored_document(**{"entry-a": record(bogus)})
    store = AutoSettingsStore(hass, store=flaky)

    with caplog.at_level(logging.WARNING):
        await store.async_load()

    text = caplog.text
    assert "unknown_field" in text
    assert "s3cr3t" not in text
    assert "amperage" not in text, "a warning must not quote the shape it refused"


async def test_loading_once_is_enough(hass: HomeAssistant, flaky: FlakyStore) -> None:
    """The file is read once, however often the store is asked to load."""
    store = AutoSettingsStore(hass, store=flaky)

    await store.async_load()
    await store.async_load()

    assert flaky.loads == 1


# ------------------------------------------------------------ transactions and identity


async def test_a_failed_save_leaves_memory_at_the_last_value_that_was_written(
    hass: HomeAssistant, flaky: FlakyStore
) -> None:
    """A write that raises must not be remembered: memory agrees with the file, not with hope."""
    store = AutoSettingsStore(hass, store=flaky)
    await store.async_load()
    saved = await store.async_update("entry-a", mutate=lambda s: replace(s, amps=10, area_id="SE4"))
    document = flaky.payload

    flaky.fail_next = True
    with pytest.raises(RuntimeError):
        await store.async_update("entry-a", mutate=lambda s: replace(s, amps=16))

    current = store.settings("entry-a")
    assert current.amps == 10 and current.revision == saved.revision
    assert flaky.payload is document

    # And the next edit is built on what was actually written, not on the refused one.
    again = await store.async_update("entry-a", mutate=lambda s: replace(s, amps=16))
    assert again.amps == 16 and again.revision == saved.revision + 1
    assert again.area_id == "SE4"
    assert flaky.payload["chargers"]["entry-a"]["settings"]["amps"] == 16


async def test_a_failed_proposal_write_keeps_the_settings_it_belongs_to(
    hass: HomeAssistant, flaky: FlakyStore
) -> None:
    """A proposal summary that could not be saved changes nothing at all."""
    store = AutoSettingsStore(hass, store=flaky)
    await store.async_load()
    await store.async_update("entry-a", mutate=lambda s: replace(s, amps=10, area_id="SE4"))
    stamp = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    summary = StoredProposal(
        calculated_at=stamp,
        state="proposal_ready",
        reason="ready",
        settings_revision=1,
        area_id="SE4",
        slots_needed=4,
        priced_slots=4,
        unpriced_slots=0,
        delivered_kwh=4.6,
        estimated_cost=1.0,
        distance_mil=2.3,
        period_starts=(stamp.isoformat(),),
        period_ends=((stamp + timedelta(hours=1)).isoformat(),),
    )

    flaky.fail_next = True
    with pytest.raises(RuntimeError):
        await store.async_update("entry-a", proposal=summary)

    assert store.proposal("entry-a") is None
    assert store.settings("entry-a").amps == 10

    await store.async_update("entry-a", proposal=summary)
    assert store.proposal("entry-a") == summary


async def test_two_chargers_are_never_each_others_casualty(
    hass: HomeAssistant, flaky: FlakyStore
) -> None:
    """Two entries edited at once, with every write failing: neither is left half-changed."""
    store = AutoSettingsStore(hass, store=flaky)
    await store.async_load()

    flaky.fail_all = True
    results = await asyncio.gather(
        store.async_update("entry-a", mutate=lambda s: replace(s, amps=10)),
        store.async_update("entry-b", mutate=lambda s: replace(s, amps=32)),
        return_exceptions=True,
    )

    assert all(isinstance(result, RuntimeError) for result in results)
    assert store.settings("entry-a") == AutoSettings()
    assert store.settings("entry-b") == AutoSettings()
    assert store.entry_ids() == (), "nothing was written, so nothing is on record"

    # With the writes working, both land, and neither loses the other's field.
    flaky.fail_all = False
    await asyncio.gather(
        store.async_update("entry-a", mutate=lambda s: replace(s, amps=10, area_id="SE4")),
        store.async_update("entry-b", mutate=lambda s: replace(s, amps=32, area_id="FI")),
    )
    assert store.settings("entry-a").area_id == "SE4"
    assert store.settings("entry-b").area_id == "FI"
    assert set(flaky.payload["chargers"]) == {"entry-a", "entry-b"}


async def test_a_proposal_write_is_not_a_settings_edit(hass: HomeAssistant) -> None:
    """A calculation must not look like somebody changing a setting."""
    store = AutoSettingsStore(hass)
    await store.async_load()
    before = await store.async_update("entry-a", mutate=lambda s: replace(s, amps=10))
    stamp = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    summary = StoredProposal(
        calculated_at=stamp,
        state="proposal_ready",
        reason="ready",
        settings_revision=before.revision,
        area_id="SE4",
        slots_needed=4,
        priced_slots=4,
        unpriced_slots=0,
        delivered_kwh=4.6,
        estimated_cost=1.0,
        distance_mil=2.3,
        period_starts=(stamp.isoformat(),),
        period_ends=((stamp + timedelta(hours=1)).isoformat(),),
    )

    after = await store.async_update("entry-a", proposal=summary)

    assert after == before and after.revision == before.revision
    assert store.proposal("entry-a") is not None


async def test_clearing_a_proposal_is_one_atomic_write(hass: HomeAssistant) -> None:
    store = AutoSettingsStore(hass)
    await store.async_load()
    await store.async_update(
        "entry-a", mutate=lambda s: replace(s, area_id="SE4", phases=1, amps=10)
    )
    stamp = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    stored = StoredProposal(
        calculated_at=stamp,
        state="proposal_ready",
        reason="ready",
        settings_revision=1,
        area_id="SE4",
        slots_needed=4,
        priced_slots=4,
        unpriced_slots=0,
        delivered_kwh=4.6,
        estimated_cost=1.0,
        distance_mil=2.3,
        period_starts=(stamp.isoformat(),),
        period_ends=((stamp + timedelta(hours=1)).isoformat(),),
    )
    await store.async_update("entry-a", proposal=stored)
    assert store.proposal("entry-a") is not None

    updated = await store.async_update(
        "entry-a", mutate=lambda s: s, clear_proposal=True
    )

    assert updated.area_id == "SE4"
    assert store.proposal("entry-a") is None
    assert store.settings("entry-a").revision == updated.revision


async def test_removing_one_charger_keeps_anothers_everything(hass: HomeAssistant) -> None:
    """Deletion is per entry, it reports what it did, and a failed write changes nothing."""
    store = AutoSettingsStore(hass)
    await store.async_load()
    await store.async_update("entry-a", mutate=lambda s: replace(s, amps=10, area_id="SE4"))
    await store.async_update("entry-b", mutate=lambda s: replace(s, amps=32, area_id="FI"))

    assert await store.async_remove("entry-unknown") is False
    assert store.entry_ids() == ("entry-a", "entry-b")

    assert await store.async_remove("entry-a") is True
    assert store.entry_ids() == ("entry-b",)
    assert store.settings("entry-a") == AutoSettings()
    assert store.settings("entry-b").amps == 32
    assert await store.async_remove("entry-a") is False


async def test_a_failed_removal_keeps_the_record_it_could_not_delete(
    hass: HomeAssistant, flaky: FlakyStore
) -> None:
    """A delete whose write failed is not a delete: memory still holds the charger."""
    store = AutoSettingsStore(hass, store=flaky)
    await store.async_load()
    await store.async_update("entry-a", mutate=lambda s: replace(s, amps=10))

    flaky.fail_next = True
    with pytest.raises(RuntimeError):
        await store.async_remove("entry-a")

    assert store.entry_ids() == ("entry-a",)
    assert store.settings("entry-a").amps == 10
    assert "entry-a" in flaky.payload["chargers"]
