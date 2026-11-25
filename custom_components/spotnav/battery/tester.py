"""Becoming a tester for a battery brand whose control is still being tested, and the control code that ends it.

Plan `plans/v3_battery_activation_codes.md` with the owner's decisions of 2026-11-25. Only for a plant whose recipe
writes only after a person's hardware test (`commands.BrandControl.hardware_test_first`, not yet `field_tested`:
every brand but Sigenergy today); Sigenergy needs neither.

1. **Sign-up** (`async_signup`, the card's "Become a tester", administrators only): an e-mail address (required here)
   and an optional note. A `battery_tester` report goes to the relay (the debug bundle, and the `battery` block:
   brand, model, firmware key, integration and its version, recipe revision, phases, fuse); once the relay took it,
   beta mode is on with that address (`beta.py`) and the plant's hardware test is unlocked for the brand
   (`BatteryControl.tester_signup_needed`). A failed send unlocks nothing: the card says so and the person sends
   again.
2. **The test's result goes by itself while beta mode is on** (`async_send_result`, from `hardware_test.py` at a
   test's end, or the card's Send): a `battery_test_result` report with the test's record, the battery control's log over the test, the
   history of the plant's entities over it (the recorder's) and the recipe revision. The card then says the result
   was sent and an answer comes by e-mail. A person is a tester only while beta mode is on
   (`BatteryControl.tester_active`): unticking Beta tester stops every report at once and ends the sign-up
   (`async_end`); a run that starts by itself sends nothing then. A control code already entered keeps working.
3. **The control code** (`async_enter_code`, Support → Hardware test): the developer read the result and sent a code
   for this installation and brand (`codes.py`). It is taken only once the plant's hardware test passed with the
   brand (`spotnav_code_test_first` before): the code approves that result. Entered, it is stored per brand (with
   its text, checked again at each start: `BatteryControl._async_check_codes`) (`BatteryControl.control_codes`) and,
   with the passed hardware test, lifts shadow only; beta's nightly reports run on until `BETA_AFTER_CODE` after it
   (`Beta.async_keep_on_until`). One code per plant and brand: a later firmware, integration or recipe change is
   checked by the automatic routine, never by a new code.

The e-mail address is never kept here, logged or put in the bundle: the beta state holds it, the relay keeps it with
the report. Nothing blocks the event loop: the code's check is a few microseconds with a key held in memory, the
history read runs in the recorder's executor, the report is encoded in the executor.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id
from homeassistant.util import dt as dt_util

from . import codes
from .runtime import plant_entity_id

if TYPE_CHECKING:
    from ..hardware_test import HardwareTest
    from ..support_report import ReportResult
    from .control import BatteryControl

_LOGGER = logging.getLogger(__name__)

#: How long beta's nightly reports run on after a control code was entered (owner decision 2026-11-25).
BETA_AFTER_CODE: Final = timedelta(days=30)
#: The history read around a test: from a little before its start to a little after its end.
HISTORY_BEFORE: Final = timedelta(minutes=2)
HISTORY_AFTER: Final = timedelta(minutes=5)
#: The most entities and rows per entity the result carries.
HISTORY_MAX_ENTITIES: Final = 40
HISTORY_MAX_ROWS: Final = 2000
#: The most log events of the battery control the result carries.
LOG_MAX_EVENTS: Final = 400

ERROR_NOT_GATED: Final = "spotnav_tester_not_needed"
ERROR_CODE_INVALID: Final = "spotnav_code_invalid"
ERROR_CODE_OTHER_INSTALL: Final = "spotnav_code_other_install"
ERROR_CODE_OTHER_BRAND: Final = "spotnav_code_other_brand"
ERROR_CODE_BLOCKED: Final = "spotnav_code_blocked"
ERROR_CODE_NOT_NEEDED: Final = "spotnav_code_not_needed"
ERROR_CODE_TEST_FIRST: Final = "spotnav_code_test_first"


def battery_block(hass: HomeAssistant, control: BatteryControl) -> dict[str, Any]:
    """The report's top-level `battery` block: what the relay's meta and the developer's tool read (no e-mail)."""
    facts = control.facts
    recipe = control.recipe()
    phases = control._grid_phase_kw()  # noqa: SLF001 - the shell's own reading
    fuse = control._main_fuse_a()  # noqa: SLF001
    return {
        "brand": facts.brand,
        "model": facts.label,
        "firmware": facts.firmware,
        "firmware_key": control.firmware_key(),
        "integration": facts.integration,
        "integration_version": control._integration_version(facts.integration),  # noqa: SLF001
        "recipe_rev": None if recipe is None else recipe.recipe_rev,
        "phases": None if phases is None else len(phases),
        "plant_phases": facts.plant_phase_entities is not None,
        "main_fuse_a": fuse,
        "capacity_kwh": facts.capacity_kwh,
        "language": hass.config.language,
        "site": control.site.entry_id,
    }


async def _async_install(hass: HomeAssistant) -> str:
    from ..support_report import install_hash  # noqa: PLC0415 - the bundle imports every part

    return install_hash(await instance_id.async_get(hass))


async def async_signup(
    hass: HomeAssistant, control: BatteryControl, email: Any, note: Any
) -> tuple[str | None, str | None]:
    """The person's sign-up (see the module): `(error, ref)`. The address is required; the note may be empty."""
    from ..beta import beta_of  # noqa: PLC0415 - see `beta.py`'s imports
    from ..support_report import ERROR_INVALID_EMAIL, ERROR_INVALID_MESSAGE, KIND_BATTERY_TESTER  # noqa: PLC0415
    from ..support_report import MAX_MESSAGE_CHARS, async_send_report, clean_email, clean_message  # noqa: PLC0415

    brand = control.gated_brand()
    if brand is None:
        return ERROR_NOT_GATED, None
    if control.recipe_blocked():
        # This release holds the recipe back: no test can run, so no address is sent for one.
        return ERROR_CODE_BLOCKED, None
    valid, address = clean_email(email)
    if not valid or address is None:
        return ERROR_INVALID_EMAIL, None
    message = ""
    if note is not None and not (isinstance(note, str) and not note.strip()):
        cleaned = clean_message(note)
        if cleaned is None or len(cleaned) > MAX_MESSAGE_CHARS:
            return ERROR_INVALID_MESSAGE, None
        message = cleaned
    result = await async_send_report(
        hass, message, address, None, kind=KIND_BATTERY_TESTER, battery=battery_block(hass, control)
    )
    if result.ref is None:
        _LOGGER.info("SpotNav battery tester: the sign-up was not sent (%s)", result.error)
        return result.error, None
    now = dt_util.now()
    control.tester = {"brand": brand, "asked_at": now.isoformat(), "ref": result.ref}
    await control.async_store()
    beta = beta_of(hass)
    if beta is not None:
        await beta.async_set(on=True, email=address)
    _LOGGER.info("SpotNav battery tester: signed up for %s (%s); the hardware test may run", brand, result.ref)
    return None, result.ref


def sends_result(control: BatteryControl) -> bool:
    """Whether a finished test goes as a tester's `battery_test_result`: the person is a tester for the brand now
    (signed up, beta mode on: `tester_active`), until its control code was entered (after it, beta's own reports
    carry the tests)."""
    brand = control.gated_brand()
    return brand is not None and control.tester_active() and brand not in control.control_codes


async def async_end(hass: HomeAssistant) -> None:
    """Beta tester was unticked: every site's sign-up ends (nothing more is sent, the first test waits for a new
    sign-up). A control code already entered stays."""
    from ..runtime import site_controller_for  # noqa: PLC0415 - runtime imports the site lazily too
    from ..const import DOMAIN  # noqa: PLC0415

    for entry in hass.config_entries.async_entries(DOMAIN):
        site = site_controller_for(hass, entry.entry_id)
        control = None if site is None else getattr(site, "battery_control", None)
        if control is None or getattr(control, "tester", None) is None:
            continue
        control.tester = None
        try:
            await control.async_store()
        except Exception:  # noqa: BLE001 - one site's store must not keep the others' sign-ups
            _LOGGER.warning("SpotNav battery tester: a site's sign-up could not be ended", exc_info=True)
        _LOGGER.info("SpotNav battery tester: beta was turned off; the sign-up ended")


def _parsed(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _entities(control: BatteryControl) -> list[str]:
    """The plant's entities a test's history reads: the battery's, its control roles', its phases', the site's
    measurement."""
    from ..debug_bundle import measurement_entity_roles  # noqa: PLC0415 - the bundle imports every part

    found: list[str] = []

    def add(entity_id: Any) -> None:
        if isinstance(entity_id, str) and entity_id and entity_id not in found:
            found.append(entity_id)

    facts = control.facts
    add(facts.battery_entity_id)
    for entity_id in facts.plant_phase_entities or ():
        add(entity_id)
    recipe = control.recipe()
    for role in () if recipe is None else recipe.roles:
        try:
            add(plant_entity_id(control.hass, control.site.config, role.match))
        except Exception:  # noqa: BLE001 - one role not found costs only its own row
            _LOGGER.debug("tester history: a role's entity could not be found", exc_info=True)
    try:
        for entity_id in measurement_entity_roles(control.site.config):
            add(entity_id)
    except Exception:  # noqa: BLE001 - the measurement's entities are a part, not the whole
        _LOGGER.debug("tester history: the measurement's entities could not be read", exc_info=True)
    return found[:HISTORY_MAX_ENTITIES]


def _read_history(hass: HomeAssistant, start: datetime, end: datetime, entity_ids: list[str]) -> dict[str, Any]:
    """The recorder's states of `entity_ids` from `start` to `end` as `[[epoch_s, state], ...]`. Runs in the
    recorder's executor."""
    from homeassistant.components.recorder import history  # noqa: PLC0415 - only with the recorder

    states = history.get_significant_states(
        hass, start, end, entity_ids, None, include_start_time_state=True, significant_changes_only=False,
        minimal_response=True, no_attributes=True, compressed_state_format=True,
    )
    rows: dict[str, Any] = {}
    for entity_id, items in states.items():
        out = []
        # The last rows: the end of a run (its hand-back) is what a cut must keep.
        for item in items[-HISTORY_MAX_ROWS:]:
            if isinstance(item, Mapping):
                out.append([item.get("lu"), item.get("s")])
            else:
                changed = getattr(item, "last_updated", None)
                out.append([None if changed is None else changed.timestamp(), getattr(item, "state", None)])
        rows[entity_id] = out
    return rows


async def async_history(hass: HomeAssistant, control: BatteryControl, start: datetime, end: datetime) -> dict[str, Any]:
    """The plant's history over a test (see `_read_history`), `{}` without a recorder or when it cannot be read."""
    if "recorder" not in hass.config.components:
        return {}
    entity_ids = _entities(control)
    if not entity_ids:
        return {}
    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415

        return await get_instance(hass).async_add_executor_job(
            partial(_read_history, hass, start - HISTORY_BEFORE, end + HISTORY_AFTER, entity_ids)
        )
    except Exception:  # noqa: BLE001 - the result goes without its history rather than not at all
        _LOGGER.info("SpotNav battery tester: the history over the test could not be read", exc_info=True)
        return {}


def log_over(control: BatteryControl, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The battery control's log events from a test's start to its end (and a little after: the hand-back)."""
    rows = []
    until = end + HISTORY_AFTER
    for event in list(control.log):
        at = _parsed(event.get("at"))
        if at is not None and start <= at <= until:
            rows.append(dict(event))
    return rows[-LOG_MAX_EVENTS:]


async def async_send_result(hass: HomeAssistant, runner: HardwareTest) -> ReportResult:
    """The site's last finished test as a tester's `battery_test_result` (see the module); the tester's record says
    it was sent."""
    from ..beta import beta_of  # noqa: PLC0415
    from ..support_report import KIND_BATTERY_TEST_RESULT, async_send_report  # noqa: PLC0415

    control = runner.site.battery_control
    record = runner.record or {}
    start = _parsed(record.get("started_at")) or dt_util.now()
    end = _parsed(record.get("finished_at")) or dt_util.now()
    block = battery_block(hass, control) | {
        "test": {"result": record.get("result"), "started_at": record.get("started_at"),
                 "finished_at": record.get("finished_at"), "stopped": record.get("stopped"),
                 # What the gate recorded: the brand's pass on this plant (the result above is the whole test's).
                 "gate_passed": control.gated_brand() in control.hardware_brands},
    }
    extra = {
        "record": record,
        "log": log_over(control, start, end),
        "history": await async_history(hass, control, start, end),
        "recipe_rev": block["recipe_rev"],
    }
    beta = beta_of(hass)
    # The address goes only while beta mode is on (a send from the card's button while it is off goes without it).
    email = beta.state.email if beta is not None and beta.state.on else None
    result = await async_send_report(
        hass, "", email, None, kind=KIND_BATTERY_TEST_RESULT, battery=block, bundle_extra={"battery_test": extra}
    )
    if result.ref is not None and control.tester is not None:
        control.tester = dict(control.tester) | {"result_ref": result.ref, "result_at": dt_util.now().isoformat()}
        await control.async_store()
    return result


async def async_enter_code(hass: HomeAssistant, control: BatteryControl, text: Any) -> str | None:
    """A control code entered (see the module): `None` once it is stored, else the stable refusal."""
    from ..beta import beta_of  # noqa: PLC0415

    if not isinstance(text, str):
        return ERROR_CODE_INVALID
    try:
        code = codes.verify(text)
    except codes.CodeError:
        return ERROR_CODE_INVALID
    if not code.matches_install(await _async_install(hass)):
        return ERROR_CODE_OTHER_INSTALL
    brand = control.facts.brand
    if code.brand != brand:
        return ERROR_CODE_OTHER_BRAND
    if control.recipe_blocked():
        return ERROR_CODE_BLOCKED
    if control.gated_brand() != brand:
        return ERROR_CODE_NOT_NEEDED
    if brand not in control.hardware_brands:
        # The code approves a passed test's result: none passed on this plant yet.
        return ERROR_CODE_TEST_FIRST
    now = dt_util.now()
    key = control.firmware_key()
    control.control_codes = dict(control.control_codes) | {brand: code.as_stored() | {
        "code": codes.normalise(text), "entered_at": now.isoformat(), "firmware_key": key, "firmware_matches": code.matches_firmware(key),
    }}
    await control.async_store()
    beta = beta_of(hass)
    if beta is not None:
        await beta.async_keep_on_until(now + BETA_AFTER_CODE)
    _LOGGER.info("SpotNav battery tester: the control code for %s was entered", brand)
    return None
