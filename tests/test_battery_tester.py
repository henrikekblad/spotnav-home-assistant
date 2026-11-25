"""Becoming a tester for a battery brand whose control is still being tested, and its control code
(`battery/tester.py`, the gate in `battery/control.py`, `spotnav/battery_control`'s actions, beta's longer period).

The codes are signed with a throwaway key; the developer's public key is swapped for its half in each test."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id
from homeassistant.util import dt as dt_util

from custom_components.spotnav import hardware_test, support_report
from custom_components.spotnav.api.battery_control import async_battery_control_answer
from custom_components.spotnav.battery import codes, tester
from custom_components.spotnav.battery import control as control_module
from custom_components.spotnav.beta import BETA_DAYS, BetaState, kept_on_until, switched
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.support_report import ReportResult

from .test_battery_control import _LIMITS_ONLY_PASS, _hardware_test_ends, _waiting_site
from .world import admin, non_admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
T0 = dt_util.parse_datetime("2026-11-25T12:00:00+01:00")


@pytest.fixture(autouse=True)
def _throwaway_key(monkeypatch: pytest.MonkeyPatch) -> None:
    public = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr(codes, "PUBLIC_KEY_HEX", public)


class _Relay:
    """The relay as `async_send_report` meets it: each report kept, a reference or a refusal answered."""

    def __init__(self, error: str | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error = error

    async def __call__(self, hass: HomeAssistant, message: str, email: str | None, client: Any, **kwargs: Any) -> ReportResult:
        self.sent.append({"message": message, "email": email, **kwargs})
        if self.error is not None:
            return ReportResult(error=self.error)
        return ReportResult(ref=f"SN-T{len(self.sent):03d}")


def _relay(monkeypatch: pytest.MonkeyPatch, error: str | None = None) -> _Relay:
    relay = _Relay(error)
    monkeypatch.setattr(support_report, "async_send_report", relay)
    return relay


async def _code(hass: HomeAssistant, brand: str = "sigenergy", install: str | None = None) -> str:
    install = install or support_report.install_hash(await instance_id.async_get(hass))
    return codes.sign(KEY, codes.payload(install=install, brand=brand, firmware_key="V1 | sigen 1", issued=date(2026, 11, 25)))


# ---- beta's longer period, pure


def test_a_control_code_keeps_a_running_period_on_for_thirty_days() -> None:
    state = switched(BetaState(), True, T0)
    kept = kept_on_until(state, T0 + timedelta(days=30), T0 + timedelta(days=3))
    assert kept.on and kept.ends_at() == T0 + timedelta(days=30)
    # Never shorter than the period itself.
    assert kept_on_until(state, T0 + timedelta(days=1), T0).ends_at() == T0 + timedelta(days=BETA_DAYS)


def test_a_period_that_ended_by_itself_starts_again_and_one_the_person_ended_stays_off() -> None:
    done = BetaState(on=False, started_at=T0, ended_at=T0 + timedelta(days=BETA_DAYS), completed=True)
    later = T0 + timedelta(days=20)
    again = kept_on_until(done, later + timedelta(days=30), later)
    assert again.on and again.started_at == later and again.ends_at() == later + timedelta(days=30)
    unticked = switched(switched(BetaState(), True, T0), False, T0 + timedelta(days=1))
    assert kept_on_until(unticked, later, later) == unticked
    # Ticking again starts a plain period.
    assert switched(again, False, later).extended_until is not None
    assert switched(switched(again, False, later), True, later).extended_until is None


# ---- the sign-up


async def test_signing_up_sends_the_testers_report_turns_beta_on_and_unlocks_the_test(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    relay = _relay(monkeypatch)
    assert control.hardware_test_refusal() == "tester_signup"
    # The address is required; nothing goes without one.
    assert await tester.async_signup(hass, control, None, None) == ("spotnav_report_invalid_email", None)
    assert await tester.async_signup(hass, control, "not an address", "x") == ("spotnav_report_invalid_email", None)
    assert relay.sent == []
    error, ref = await tester.async_signup(hass, control, " tester@example.com ", "Fox H3, 3 phases")
    assert error is None and ref == "SN-T001"
    sent = relay.sent[0]
    assert sent["kind"] == "battery_tester" and sent["email"] == "tester@example.com"
    assert sent["message"] == "Fox H3, 3 phases"
    assert sent["battery"]["brand"] == "sigenergy" and sent["battery"]["firmware_key"] == control.firmware_key()
    assert {"model", "integration", "integration_version", "recipe_rev", "phases", "main_fuse_a"} <= set(sent["battery"])
    # Beta on with the address; the stage and the unlocked test; the address is not kept with the battery.
    beta = domain_data(hass).beta
    assert beta.state.on and beta.state.email == "tester@example.com"
    assert control.tester_view()["stage"] == "asked" and control.hardware_test_refusal() is None
    assert "tester@example.com" not in str(control._data())
    await control.async_stop()


async def test_a_sign_up_the_relay_did_not_take_unlocks_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _relay(monkeypatch, error="spotnav_report_network")
    assert await tester.async_signup(hass, control, "tester@example.com", "") == ("spotnav_report_network", None)
    assert control.tester is None and control.hardware_test_refusal() == "tester_signup"
    assert not domain_data(hass).beta.state.on
    await control.async_stop()


async def test_a_plant_whose_recipe_takes_no_code_has_no_tester_block_and_no_sign_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, _controller, _plant, control, recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    from dataclasses import replace  # noqa: PLC0415

    recipe["control"] = replace(recipe["control"], hardware_test_first=False)
    relay = _relay(monkeypatch)
    assert control.tester_view() is None and control.status()["tester"] is None
    assert await tester.async_signup(hass, control, "tester@example.com", "") == ("spotnav_tester_not_needed", None)
    assert await tester.async_enter_code(hass, control, await _code(hass)) == "spotnav_code_not_needed"
    assert relay.sent == [] and control.hardware_test_refusal() is None
    await control.async_stop()


# ---- the code


async def test_the_control_code_with_a_passed_test_lifts_shadow_only_and_keeps_beta_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _relay(monkeypatch)
    await tester.async_signup(hass, control, "tester@example.com", "")
    control.reports.clear()
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    assert control.state() == "shadow_only" and control.hardware_test_due() == "control_code"
    # Refused: not a code, another installation's, another brand's.
    assert await tester.async_enter_code(hass, control, "hello") == "spotnav_code_invalid"
    assert await tester.async_enter_code(hass, control, await _code(hass, install="cd" * 32)) == "spotnav_code_other_install"
    assert await tester.async_enter_code(hass, control, await _code(hass, brand="foxess")) == "spotnav_code_other_brand"
    other_key = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    forged = codes.sign(other_key, codes.payload(
        install=support_report.install_hash(await instance_id.async_get(hass)), brand="sigenergy",
        firmware_key="x", issued=date(2026, 11, 25)))
    assert await tester.async_enter_code(hass, control, forged) == "spotnav_code_invalid"
    assert control.state() == "shadow_only"
    # This installation's code for the brand, pasted with spaces in lower case: control runs.
    text = (await _code(hass)).lower().replace("-", " ")
    assert await tester.async_enter_code(hass, control, text) is None
    assert control.state() == "on" and control.tester_view()["stage"] == "control_code"
    stored = control._data()["gate"]["control_codes"]["sigenergy"]
    assert stored["brand"] == "sigenergy" and stored["firmware_key"] == control.firmware_key()
    beta = domain_data(hass).beta
    assert beta.state.on and beta.state.ends_at() >= dt_util.now() + timedelta(days=29)
    await control.async_stop()


async def test_a_code_before_the_test_passed_is_refused(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _relay(monkeypatch)
    await tester.async_signup(hass, control, "tester@example.com", "")
    # The code approves a passed test's result: none passed here yet, so nothing is stored.
    assert await tester.async_enter_code(hass, control, await _code(hass)) == "spotnav_code_test_first"
    assert control.control_codes == {}
    control.reports.clear()
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    assert control.state() == "shadow_only" and control.hardware_test_due() == "control_code"
    await control.async_stop()


async def test_a_stored_code_is_checked_again_at_the_start_and_one_that_no_longer_verifies_is_dropped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _relay(monkeypatch)
    await tester.async_signup(hass, control, "tester@example.com", "")
    control.reports.clear()
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    assert await tester.async_enter_code(hass, control, await _code(hass)) is None
    stored = control._data()["gate"]["control_codes"]["sigenergy"]
    assert codes.verify(stored["code"]).brand == "sigenergy"
    # Loaded again: the code still verifies and is kept.
    control.control_codes = {"sigenergy": dict(stored)}
    await control._async_check_codes()
    assert "sigenergy" in control.control_codes
    # One without its text, or signed by a key this release no longer has: dropped, control waits for a code.
    control.control_codes = {"sigenergy": {key: value for key, value in stored.items() if key != "code"}}
    await control._async_check_codes()
    assert control.control_codes == {} and control.hardware_test_due() == "control_code"
    control.control_codes = {"sigenergy": dict(stored)}
    other = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    monkeypatch.setattr(codes, "PUBLIC_KEY_HEX", other.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
    await control._async_check_codes()
    assert control.control_codes == {} and control.state() == "shadow_only"
    await control.async_stop()


async def test_a_blocked_recipe_refuses_the_code_and_the_sign_up_and_voids_the_stored_pass(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage)
    control.reports.clear()
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    assert control.state() == "on"
    relay = _relay(monkeypatch)
    monkeypatch.setattr(control_module, "blocked", lambda control, key: True)
    assert await tester.async_enter_code(hass, control, await _code(hass)) == "spotnav_code_blocked"
    assert await tester.async_signup(hass, control, "tester@example.com", "") == ("spotnav_code_blocked", None)
    assert relay.sent == []
    # The next look voids the plant's pass: after the block a test must pass again.
    await control.async_look(dt_util.now())
    assert control.hardware_brands == [] and any(row["kind"] == "hardware_pass_voided" for row in control.log)
    monkeypatch.setattr(control_module, "blocked", lambda control, key: False)
    assert control.hardware_test_due() == "hardware_test_first"
    await control.async_stop()


# ---- the test's result


async def test_a_testers_finished_test_goes_as_its_result_while_beta_is_on_until_the_code(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    relay = _relay(monkeypatch)
    await tester.async_signup(hass, control, "tester@example.com", "")
    now = dt_util.now()
    control.log.append({"at": now.isoformat(), "kind": "hardware_test_started", "reason": "x"})
    control.log.append({"at": (now - timedelta(hours=2)).isoformat(), "kind": "earlier", "reason": None})
    record = {"started_at": now.isoformat(), "finished_at": (now + timedelta(minutes=9)).isoformat(),
              "result": "passed", "stopped": None, "steps": [{"part": "battery", "step": "hold", "status": "passed"}]}
    runner = SimpleNamespace(site=controller, record=record, running=False, sent_ref=None, _save=lambda: None,
                             _tester_sends=lambda: hardware_test.HardwareTest._tester_sends(runner))
    controller.battery_control = control
    assert runner._tester_sends()
    result = await hardware_test.async_send_test(hass, runner)  # type: ignore[arg-type]
    assert result.ref == "SN-T002" and runner.sent_ref == "SN-T002"
    sent = relay.sent[-1]
    assert sent["kind"] == "battery_test_result" and sent["battery"]["test"]["result"] == "passed"
    extra = sent["bundle_extra"]["battery_test"]
    assert [event["kind"] for event in extra["log"]] == ["hardware_test_started"]
    assert extra["record"] == record and "history" in extra and extra["recipe_rev"] == 1
    assert sent["email"] == "tester@example.com" and sent["battery"]["test"]["gate_passed"] is False
    assert control.tester_view()["stage"] == "tested" and control.tester_view()["result_sent_at"] is not None
    # Once the code was entered, beta's own reports carry the tests.
    control.control_codes = {"sigenergy": {"brand": "sigenergy", "entered_at": now.isoformat()}}
    assert not runner._tester_sends()
    await control.async_stop()


async def test_unticking_beta_stops_every_report_at_once_and_ends_the_sign_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site, controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    relay = _relay(monkeypatch)
    await tester.async_signup(hass, control, "tester@example.com", "")
    controller.battery_control = control
    now = dt_util.now()
    record = {"started_at": now.isoformat(), "finished_at": now.isoformat(), "result": "passed", "stopped": None,
              "steps": [{"part": "battery", "step": "hold", "status": "passed"}]}
    runner = hardware_test.HardwareTest(hass, controller)
    runner.record = record
    await domain_data(hass).beta.async_set(on=False)
    assert control.tester is None and control.tester_view()["stage"] == "none"
    assert control.hardware_test_refusal() == "tester_signup"
    # A run's own send at its end (a retry or a waited run included): nothing goes.
    sent = len(relay.sent)
    await runner._async_send_after()
    assert len(relay.sent) == sent
    # Only a person's deliberate Send goes, as a plain test report and without the address.
    result = await hardware_test.async_send_test(hass, runner)
    assert result.ref is not None and relay.sent[-1]["kind"] == "beta_test" and relay.sent[-1]["email"] is None
    # A period that ran out by itself stops the sending too.
    await tester.async_signup(hass, control, "tester@example.com", "")
    domain_data(hass).beta._state = BetaState(on=False, completed=True)
    assert not tester.sends_result(control) and control.tester_view()["stage"] == "none"
    await control.async_stop()


# ---- the command


async def test_the_actions_are_an_administrators_and_never_over_the_webhook(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], hass_ws_client: Any,
    hass_read_only_access_token: str,
) -> None:
    site, controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    # The site's own battery control is the one the command reads.
    controller.battery_control = control
    _relay(monkeypatch)
    answer = await async_battery_control_answer(
        hass, site, None, admin=True, over_webhook=True, tester_signup={"email": "tester@example.com"}
    )
    assert answer["error"] == "spotnav_not_permitted_over_webhook"
    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    frame = await ws_call(reader, {"type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
                                   "tester_signup": {"email": "tester@example.com"}})
    assert frame["result"]["error"] == "spotnav_not_admin"
    client = await admin(hass, hass_ws_client)
    frame = await ws_call(client, {"type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
                                   "tester_signup": {"email": "tester@example.com", "extra": 1}})
    assert frame["success"] is False and frame["error"]["code"] == "invalid_format"
    answer = await async_battery_control_answer(
        hass, site, None, admin=True, tester_signup={"email": "tester@example.com"}, enter_code={"code": "x"}
    )
    assert answer["error"] == "spotnav_invalid_value"
    frame = await ws_call(client, {"type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
                                   "tester_signup": {"email": "tester@example.com", "note": "hi"}})
    assert frame["result"]["ok"] is True and frame["result"]["status"]["tester"]["stage"] == "asked"
    frame = await ws_call(client, {"type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
                                   "enter_code": {"code": "nonsense"}})
    assert frame["result"]["error"] == "spotnav_code_invalid" and frame["result"]["status"]["tester"]["stage"] == "asked"
    control.reports.clear()
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    frame = await ws_call(client, {"type": "spotnav/battery_control", "api_version": 1, "site_id": site.entry_id,
                                   "enter_code": {"code": await _code(hass)}})
    assert frame["result"]["ok"] is True and frame["result"]["status"]["tester"]["stage"] == "control_code"
    await control.async_stop()
