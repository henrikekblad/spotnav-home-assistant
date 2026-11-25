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


# ---- a brand gated after its control could already run (`commands.GATED_AFTER_RUNNING`)


def _gated_after_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test plant's brand (Sigenergy's recipe, gated by `_waiting_site`) counted as one gated after running."""
    monkeypatch.setattr(control_module, "GATED_AFTER_RUNNING", frozenset({"sigenergy"}))


def _not_yet_looked_at(control: control_module.BatteryControl) -> None:
    """The plant as the first start after the update finds it: `_waiting_site` passes the routine only after its
    start, which already decided the brand (`grandfather_checked`); a real plant has its reports in the store."""
    control.grandfather_checked = []


def _moved_to_an_older_key(control: control_module.BatteryControl) -> str:
    """The passed report under the key now moved to an older key (the integration updated in the same restart): the
    key now has no report yet. Returns the older key."""
    key = control.firmware_key()
    older = key.replace(" | ", " | old-", 1)
    control.reports = {older: {**control.reports.pop(key), "key": older}}
    return older


async def test_a_plant_whose_control_already_ran_keeps_it_with_no_sign_up_or_code_asked(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _gated_after_running(monkeypatch)
    site, controller, plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _not_yet_looked_at(control)
    # Switched on, with the routine's check passed on the firmware key now (`_started`): the first look counts it as
    # passed and as having its code, so control goes on as before the update.
    now = dt_util.now()
    await control.async_look(now)
    assert control.hardware_test_due() is None and control.state() == "on"
    record = control.control_codes["sigenergy"]
    assert record["grandfathered"] is True and record["firmware_key"] == control.firmware_key()
    assert dt_util.parse_datetime(record["entered_at"]) is not None
    assert control.hardware_brands == ["sigenergy"]
    assert any(row["kind"] == "control_grandfathered" for row in control.log)
    # No tester row, no sign-up, no code, no tester's result report.
    assert control.tester_view() is None and not control.tester_signup_needed()
    assert not {control_module.NEED_TESTER_SIGNUP, control_module.NEED_CONTROL_CODE,
                control_module.NEED_HARDWARE_TEST_FIRST} & set(control._needs())
    assert control.hardware_test_refusal() is None and not tester.sends_result(control)
    # Kept over a restart: the record has no code text and is not dropped by the code check.
    await control.async_look(now + timedelta(seconds=10))
    await control.async_stop()
    again = control_module.BatteryControl(hass, controller, plan_source=control._plan_source)
    await again.async_start()
    assert again.control_codes["sigenergy"]["grandfathered"] is True and again.grandfather_checked == ["sigenergy"]
    assert again.hardware_test_due() is None and again.tester_view() is None
    await again.async_stop()


async def test_a_plant_updated_in_the_same_restart_keeps_control_and_the_routine_checks_the_new_key(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _gated_after_running(monkeypatch)
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _not_yet_looked_at(control)
    # The integration was updated with SpotNav: the pass is on the key before, the key now has no report yet.
    older = _moved_to_an_older_key(control)
    assert control.reports.get(control.firmware_key()) is None
    await control.async_look(dt_util.now())
    assert control.control_codes["sigenergy"]["grandfathered"] is True
    assert control.control_codes["sigenergy"]["firmware_key"] == older
    # Nothing forces shadow only: the routine runs on the key now, as it did before the brand was gated.
    assert control.hardware_test_due() is None
    assert control.options().shadow_only is False and control.state() != "shadow_only"
    await control.async_stop()


async def test_a_plant_whose_control_was_off_at_the_update_keeps_it_when_switched_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _gated_after_running(monkeypatch)
    site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _not_yet_looked_at(control)
    hass.config_entries.async_update_entry(site, data={**site.data, "battery_control_enabled": False})
    await control.async_look(dt_util.now())
    # The passed report proves it ran: grandfathered while off.
    assert control.control_codes["sigenergy"]["grandfathered"] is True
    # Switched on later: no sign-up, no test, no code.
    hass.config_entries.async_update_entry(site, data={**site.data, "battery_control_enabled": True})
    await control.async_look(dt_util.now() + timedelta(seconds=10))
    assert control.hardware_test_due() is None and control.tester_view() is None
    assert control.options().shadow_only is False
    await control.async_stop()


async def test_a_grandfathered_plants_own_hardware_test_stops_control_only_on_a_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _gated_after_running(monkeypatch)
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _not_yet_looked_at(control)
    await control.async_look(dt_util.now())
    assert control.grandfathered()
    # A test that could not judge (the sun): control goes on.
    _hardware_test_ends(control, hold="not_judgeable", normal="not_judgeable")
    assert control.hardware_failed == [] and control.hardware_test_due() is None
    # One that failed: shadow only on the key until one passes again.
    _hardware_test_ends(control, hold="ignored", normal="restored")
    assert control.hardware_test_due() == control_module.NEED_HARDWARE_TEST_AGAIN
    await control.async_stop()


async def test_a_plant_that_never_passed_still_signs_up_tests_and_enters_a_code(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _gated_after_running(monkeypatch)
    site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _relay(monkeypatch)
    _not_yet_looked_at(control)
    # No passed report at all: nothing is granted, the sign-up comes first.
    control.reports.clear()
    await control.async_look(dt_util.now())
    assert control.control_codes == {} and control.hardware_brands == []
    assert control.hardware_test_refusal() == control_module.NEED_TESTER_SIGNUP
    assert control.tester_view()["stage"] == "none" and control.state() == "shadow_only"
    await tester.async_signup(hass, control, "tester@example.com", "")
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    # The test's own pass is a report on the key now: still the developer's code, never a grandfathered record.
    await control.async_look(dt_util.now() + timedelta(seconds=10))
    assert control.hardware_test_due() == control_module.NEED_CONTROL_CODE and "sigenergy" not in control.control_codes
    assert await tester.async_enter_code(hass, control, await _code(hass)) is None
    assert control.hardware_test_due() is None and control.tester_view()["stage"] == "control_code"
    assert "grandfathered" not in control.control_codes["sigenergy"]
    await control.async_stop()


async def test_another_brand_or_a_second_look_is_never_grandfathered(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    # A brand gated from the start (FoxESS, Solis and the others): a passed report is no reason.
    await control.async_look(dt_util.now())
    assert control.control_codes == {} and control.hardware_test_due() == control_module.NEED_HARDWARE_TEST_FIRST
    # A record of one stored by hand for such a brand is dropped at the code check.
    control.control_codes = {"sigenergy": {"grandfathered": True, "entered_at": "2026-11-25T12:00:00+01:00"}}
    await control._async_check_codes()
    assert control.control_codes == {}
    # One gated after running whose brand was already looked at (here at the start, before the routine passed):
    # a pass found later is never counted.
    _gated_after_running(monkeypatch)
    control.grandfather_checked = ["sigenergy"]
    await control.async_look(dt_util.now() + timedelta(seconds=10))
    assert control.control_codes == {} and control.hardware_brands == []
    await control.async_stop()


async def test_a_testers_pass_voided_by_a_block_never_becomes_grandfathered(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _gated_after_running(monkeypatch)
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _relay(monkeypatch)
    _not_yet_looked_at(control)
    control.reports.clear()
    await control.async_look(dt_util.now())
    await tester.async_signup(hass, control, "tester@example.com", "")
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    assert control.hardware_brands == ["sigenergy"] and "sigenergy" not in control.control_codes
    # A block voids the pass; its report is still a passed one, but the brand was decided: no record.
    control.hardware_brands = []
    await control.async_look(dt_util.now() + timedelta(seconds=10))
    assert "sigenergy" not in control.control_codes
    assert control.hardware_test_due() == control_module.NEED_HARDWARE_TEST_FIRST
    await control.async_stop()


async def test_a_grandfathered_pass_voided_by_a_block_is_not_given_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _gated_after_running(monkeypatch)
    _site, _controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    _not_yet_looked_at(control)
    await control.async_look(dt_util.now())
    assert control.hardware_brands == ["sigenergy"]
    # A block voids the pass (`_void_blocked_passes`); after it, the record stays and the pass is not given back.
    control.hardware_brands = []
    await control.async_look(dt_util.now() + timedelta(seconds=10))
    assert control.hardware_brands == [] and control.hardware_test_due() == control_module.NEED_HARDWARE_TEST_FIRST
    await control.async_stop()


def test_the_brands_gated_after_running_are_gated_and_not_field_tested() -> None:
    from custom_components.spotnav.battery import commands  # noqa: PLC0415

    assert commands.GATED_AFTER_RUNNING <= {
        control.brand for control in commands.CONTROLS if commands.hardware_test_first(control.brand)
    }
    assert "sigenergy" not in commands.GATED_AFTER_RUNNING and not commands.hardware_test_first("sigenergy")


async def test_a_damaged_report_in_the_store_never_stops_the_start_or_decides_the_grandfathering(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """Storage is untrusted: a report that is no mapping, or one whose mode rows are not, is read as no report. The
    start goes on to its hand-back, and the plant's real pass still counts."""
    _gated_after_running(monkeypatch)
    _site, controller, _plant, control, _recipe = await _waiting_site(hass, monkeypatch, hass_storage, approved=False)
    key = control.firmware_key()
    await control.async_look(dt_util.now())
    await control.async_stop()
    stored = next(
        item["data"] for item in hass_storage.values()
        if isinstance(item.get("data"), dict) and isinstance(item["data"].get("reports"), dict) and "gate" in item["data"]
    )
    good = stored["reports"][key]
    stored["reports"] = {
        "V0 | broken 1": {"brand": "sigenergy", "modes": {"hold": "x", "normal": 7}},
        "V0 | broken 2": ["not", "a", "report"],
        "V0 | broken 3": {"brand": "sigenergy", "modes": ["hold"]},
        key: good,
    }
    stored["gate"]["grandfather_checked"] = []
    stored["gate"]["control_codes"] = {}
    stored["gate"]["hardware_brands"] = []
    again = control_module.BatteryControl(hass, controller, plan_source=control._plan_source)
    await again.async_start()
    assert again._started
    assert again.control_codes["sigenergy"]["grandfathered"] is True
    assert again.hardware_test_due() is None
    # Every look reads them too.
    await again.async_look(dt_util.now() + timedelta(seconds=10))
    assert again.status()["state"] is not None
    await again.async_stop()


def test_a_report_that_is_no_mapping_reads_as_none_and_a_damaged_mode_row_as_not_run() -> None:
    from custom_components.spotnav.battery import gate

    damaged = {"modes": {"hold": "x", "normal": 7, "charge": ["y"]}}
    assert gate.report_status(damaged) == "pending"
    assert gate.failed_mode(damaged) is None
    assert gate.hardware_test_passed(damaged) is False
    assert gate.report_status(["not", "a", "report"]) == "none"  # type: ignore[arg-type]
    assert gate.failed_mode("x") is None  # type: ignore[arg-type]
