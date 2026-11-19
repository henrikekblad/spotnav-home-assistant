"""The beta tester mode and the hardware test for the card's Support box: `spotnav/beta` (administrators only).

`{api_version: 1, site_id? | charger_id?, action?, changes?}`; the site (named, or the charger's) is needed only for
the hardware test. `action`:

* `get` (the default): the answer below.
* `set` with `changes: {on?: bool, email?: str | null}`: the "Betatestare" box and the e-mail address (blank or
  `null` clears it; refused with `spotnav_report_invalid_email` when it is no address). Ticking starts a 14-day
  period; unticking ends it at once (`beta.py`).
* `start_test`: start the site's hardware test (`hardware_test.py`); refused with its reason in `test.blocked`
  and `error` `spotnav_test_blocked`.
* `stop_test`: end a running test at once (everything handed back).
* `send_test`: send the site's last finished test as a `beta_test` report (the card's "Skicka" when beta is off);
  `ref` or the report's stable code as `spotnav/send_debug_info` answers them, with `retry_after_s`.

The answer, every action: `{api_version, ok, error, beta, test, ref, retry_after_s}`: `beta` is
`{on, started_at, ends_at, ended, email}` (`beta.view`), `test` the site's test (`hardware_test.view`) or `null`
without a site. Not over the charger webhook: the webhook's holder is no administrator.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..beta import beta_of, view
from ..hardware_test import async_send_test, test_runner_for
from ..support_report import ERROR_INVALID_EMAIL, clean_email
from .common import ERROR_NO_SITE, ERROR_NOT_ADMIN, is_admin, lookup_charger, lookup_site, send_unsupported_version, site_of_charger

BETA_API_VERSION: Final = 1
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"
ERROR_TEST_BLOCKED: Final = "spotnav_test_blocked"
ERROR_NOT_READY: Final = "spotnav_not_ready"
ACTIONS: Final = ("get", "set", "start_test", "stop_test", "send_test")


def _site_of(hass: HomeAssistant, msg: dict[str, Any]) -> ConfigEntry | None:
    if "site_id" in msg:
        entry, _code = lookup_site(hass, msg.get("site_id"), require_loaded=False)
        return entry
    if "charger_id" in msg:
        charger, _code = lookup_charger(hass, msg.get("charger_id"), require_loaded=False)
        return None if charger is None else site_of_charger(hass, charger.entry_id)
    return None


async def async_beta_answer(
    hass: HomeAssistant, site: ConfigEntry | None, action: Any, changes: Any, *, admin: bool
) -> dict[str, Any]:
    """The command's answer (see the module)."""
    answer: dict[str, Any] = {
        "api_version": BETA_API_VERSION, "ok": False, "error": None, "beta": None, "test": None, "ref": None,
        "retry_after_s": None,
    }
    if not admin:
        answer["error"] = ERROR_NOT_ADMIN
        return answer
    beta = beta_of(hass)
    if beta is None:
        answer["error"] = ERROR_NOT_READY
        return answer
    action = "get" if action is None else action
    if action not in ACTIONS:
        answer["error"] = ERROR_INVALID_VALUE
        return answer
    runner = None if site is None else test_runner_for(hass, site.entry_id)
    if action == "set":
        error = await _async_set(beta, changes)
        if error is not None:
            answer["error"] = error
    elif action in ("start_test", "stop_test", "send_test"):
        if runner is None:
            answer["error"] = ERROR_NO_SITE
        elif action == "start_test":
            if await runner.async_start() is not None:
                answer["error"] = ERROR_TEST_BLOCKED
        elif action == "stop_test":
            await runner.async_stop("stopped")
        else:
            result = await async_send_test(hass, runner)
            answer["ref"], answer["retry_after_s"] = result.ref, result.retry_after_s
            answer["error"] = result.error
    answer["ok"] = answer["error"] is None
    answer["beta"] = view(beta.state)
    answer["test"] = None if runner is None else runner.view()
    return answer


async def _async_set(beta: Any, changes: Any) -> str | None:
    if not isinstance(changes, dict) or not changes or any(key not in ("on", "email") for key in changes):
        return ERROR_INVALID_VALUE
    on = changes.get("on")
    if "on" in changes and not isinstance(on, bool):
        return ERROR_INVALID_VALUE
    fields: dict[str, Any] = {}
    if "email" in changes:
        valid, email = clean_email(changes["email"])
        if not valid:
            return ERROR_INVALID_EMAIL
        fields["email"] = email
    await beta.async_set(on=on, **fields)
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/beta",
        vol.Optional("api_version"): object,
        vol.Optional("site_id"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("action"): object,
        vol.Optional("changes"): object,
    }
)
@websocket_api.async_response
async def websocket_beta(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The beta state and the site's hardware test (module docstring)."""
    version = msg.get("api_version")
    if type(version) is not int or version != BETA_API_VERSION:
        send_unsupported_version(connection, msg, BETA_API_VERSION)
        return
    answer = await async_beta_answer(
        hass, _site_of(hass, msg), msg.get("action"), msg.get("changes"), admin=is_admin(connection)
    )
    connection.send_result(msg["id"], answer)


@callback
def async_setup_beta_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_beta)
