"""The upstream drift canary (`tools/upstream_drift_canary.py`): its readers and its checks, offline.

The network part (cloning) is not tested; what is: that every integration SpotNav's definitions name has a source or a
stated reason, that the reviewed list holds nothing SpotNav has dropped, and how the checks judge a source's text.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import custom_components.spotnav  # noqa: F401  (imported before the canary, which then uses it as it is)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

from upstream_drift import check, expectations  # noqa: E402
from upstream_drift.reviewed import REVIEWED  # noqa: E402
from upstream_drift.upstreams import NOT_COVERED, UPSTREAMS  # noqa: E402


def _all() -> list[expectations.Expectation]:
    return expectations.collect(REPO, frozenset(UPSTREAMS) | frozenset(NOT_COVERED))


def test_every_named_integration_has_a_source_or_a_reason() -> None:
    domains = {e.domain for e in _all()}
    unknown = sorted(d for d in domains if d not in UPSTREAMS and d not in NOT_COVERED)
    assert unknown == [], f"add to tools/upstream_drift/upstreams.py (UPSTREAMS or NOT_COVERED): {unknown}"
    assert not set(UPSTREAMS) & set(NOT_COVERED)


def test_the_definitions_are_read() -> None:
    found = {e.ident for e in _all()}
    assert ("easee", "service", "action_command") in found
    assert ("ocpp", "service", "configure") in found
    assert ("huawei_solar", "service", "forcible_charge") in found
    assert ("kia_uvo", "service", "force_update") in found
    assert ("sigen", "key", "plant_ess_soc") in found
    assert ("tesla_fleet", "key", "wake") in found
    assert any(d == "wallbox" and k == "key" and "pause_resume" in n for d, k, n in found)


def test_reviewed_entries_are_still_relied_on() -> None:
    present = {e.ident for e in _all()}
    assert sorted(set(REVIEWED) - present) == []


def test_literal_runs() -> None:
    assert expectations.literal_runs(re.compile(r"(?:^|_)plant_ess_soc$")) == ["plant_ess_soc"]
    runs = expectations.literal_runs(re.compile(r"-em:\d+-(?P<a>[abc])_current$"))
    assert "_current" in runs and "-em:" in runs


def test_keys_and_fragments_are_told_apart() -> None:
    plain = expectations.match_expectations(["sigen"], re.compile(r"(?:^|_)plant_ess_soc$"), "x")
    assert {(e.kind, e.name) for e in plain} == {("key", "plant_ess_soc")}
    grouped = expectations.match_expectations(["dsmr"], re.compile(r"(?:^|_)current_l(?P<n>[123])$"), "x")
    assert {(e.kind, e.name) for e in grouped} == {("fragment", "current_l")}


def _index(text: str, services: frozenset[str] = frozenset()) -> check.Index:
    return check.Index(text.lower(), True, services)


def test_a_key_must_end_where_it_does_but_may_follow_a_prefix() -> None:
    index = _index('self._attr_unique_id = f"{uid}_storage_command_mode"  key="battery_power_charge_total"')
    assert index.has_key("storage_command_mode")
    assert not index.has_key("battery_power_charge")
    assert not index.has_key("command")


def test_a_key_may_be_the_name_a_unique_id_is_made_of() -> None:
    assert _index('HypervoltSensor(coordinator, "Session Energy", "x")').has_key("session_energy")
    assert not _index("the session energy is shown").has_key("session_energy")


def test_values_ignore_spelling() -> None:
    index = _index('"ready_to_charge": "Ready to charge"')
    assert index.has_value("ready to charge") and index.has_value("ready_to_charge")
    assert not index.has_value("charge now")


def test_services_come_from_services_yaml_or_the_code() -> None:
    assert _index("", frozenset({"configure"})).has_service("configure")
    assert _index('hass.services.async_register(DOMAIN, "set_charger_dynamic_limit", x)').has_service(
        "set_charger_dynamic_limit"
    )
    assert not _index("nothing").has_service("configure")


def test_an_alternative_is_enough_and_a_gone_integration_is_reported() -> None:
    wanted = expectations.Expectation("zaptec", "key", "charger_operation_mode", "x", ("charger_operation_mode", "charger_mode"))
    assert check.evaluate([wanted], {"zaptec": _index("key='charger_mode'")}).drift is False
    assert check.evaluate([wanted], {"zaptec": _index("nothing")}).missing == [wanted]
    gone = check.Index("", False)
    assert check.evaluate([wanted], {"zaptec": gone}).gone == [("zaptec", UPSTREAMS["zaptec"].repo)]


def test_an_unmapped_integration_is_drift_not_silence() -> None:
    other = expectations.Expectation("brand_new", "key", "some_key", "x")
    assert check.evaluate([other], {}).unmapped == ["brand_new"]
