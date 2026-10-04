"""The one authenticated manual-action command: Start now, a typed Stop (pause), Resume.

The card's three immediate actions must not be three transports, and they must not be re-decided by
each caller. This contract therefore has exactly one command, one entry-scoping rule and *one*
admission boundary: `AutoPlannerController.async_manual_action`, which asks the *two* decisions the
split names -- `auto_execution.decide_immediate` for a command to the charger (`start`, or a `stop`
with no choice) and `auto_execution.decide_automatic` for Auto's own actions (a `stop` carrying a
typed choice, and `resume`) -- inside the execution lock, and executes the action only if the decision
that owns it admits it. These tests pin both halves of that: which boundary is called, how often, and
that nothing happens at all when the action is not the admissible one -- including a stale dashboard
read, a second concurrent request, an external charger, and a choice that stopped being available.

Nothing here reaches a charger: the boundary is the only thing that talks to one, and these tests spy
on the boundary's own effect steps.
"""

from __future__ import annotations

from dataclasses import replace

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.planning.auto_controller import AutoPlannerController
from custom_components.spotnav.execution.auto_execution import (
    EXECUTION_ACTION_UNAVAILABLE,
    AutoControlError,
    AutoExecutor,
)
from custom_components.spotnav.planning.auto_settings import (
    PAUSE_NEXT_PERIOD,
    PAUSE_UNTIL_RESUMED,
    PAUSE_UNTIL_TOMORROW,
    AutoSettingsStore,
)
from custom_components.spotnav.execution.controller import ChargingExecutionError
from custom_components.spotnav.api.manual_action import (
    ACTION_API_VERSION,
    ERROR_ACTION_FAILED,
    ERROR_ACTION_RECONCILE_FAILED,
    ERROR_ACTION_UNAVAILABLE,
    ERROR_INVALID_ACTION,
)
from tests.helpers import make_site_entry
from tests.relay import serve
from tests.world import setup_charger
from tests.harness import Session
from tests.test_auto_pause_intent import _script_stop
from tests.world import go_auto, ws_call
from tests.messages import forbid_charger_writes, stored
from custom_components.spotnav.runtime import preview_for

pytestmark = pytest.mark.usefixtures("offline_relay")

CHARGE_CONTROL = "switch.charger_a"


def action_message(
    entry_id: Any = "entry_a",
    action: Any = "start",
    *,
    choice: Any = None,
    api_version: Any = ACTION_API_VERSION,
    include_choice: bool = False,
) -> dict[str, Any]:
    """One action request, with the choice present only when a caller asks for it."""
    message: dict[str, Any] = {
        "type": "spotnav/manual_action",
        "api_version": api_version,
        "charger_id": entry_id,
        "action": action,
    }
    if include_choice or choice is not None:
        message["choice"] = choice
    return message


class Counted:
    """A method, counted, with the original still called."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
        self.calls = 0
        original = getattr(owner, name)

        async def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)


@pytest.fixture
def effects(monkeypatch: pytest.MonkeyPatch) -> dict[str, Counted]:
    """The four effect steps this contract may reach, counted.

    They are the executor's own lock-held implementations -- the same code the button, the webhook and
    every other manual path uses -- so counting them proves *which* action ran, not merely that
    something did. `stop` and `immediate_stop` are separate steps on purpose: a typed Stop is a pause
    (`_pause_locked`) and a Stop with no choice is a command to the charger
    (`_immediate_stop_locked`), which is the distinction the split exists to make.
    """
    return {
        "start": Counted(monkeypatch, AutoExecutor, "_manual_start_locked"),
        "stop": Counted(monkeypatch, AutoExecutor, "_pause_locked"),
        "immediate_stop": Counted(monkeypatch, AutoExecutor, "_immediate_stop_locked"),
        "resume": Counted(monkeypatch, AutoExecutor, "_resume_locked"),
        "admissions": Counted(monkeypatch, AutoPlannerController, "async_manual_action"),
    }


def total(effects: dict[str, Counted]) -> int:
    return sum(effects[name].calls for name in ("start", "stop", "immediate_stop", "resume"))


def start_charging(hass: HomeAssistant, state: str = "on") -> None:
    """The charger's own reported state: what `charging`, and therefore the decision, read."""
    hass.states.async_set(CHARGE_CONTROL, state)


def preview_of(hass: HomeAssistant, entry_id: str = "entry_a") -> AutoPlannerController:
    preview = preview_for(hass, entry_id)
    assert preview is not None
    return preview


async def test_start_is_admitted_once_through_the_execution_boundary(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """One Start now is one manual start, and it pauses Auto for the plug-in session: the person's own
    pause (`manual`, action `start`), never a pause choice."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    before = stored(hass, entry.entry_id)

    frame = await ws_call(client, action_message(entry.entry_id, "start"))

    assert frame["success"] is True
    assert frame["result"] == {
        "api_version": ACTION_API_VERSION,
        "ok": True,
        "error": None,
        "action": "start",
        "choice": None,
    }, "the exact result envelope, and no prose"
    assert effects["admissions"].calls == 1, "one admission, one boundary"
    assert effects["start"].calls == 1
    assert effects["stop"].calls == 0 and effects["resume"].calls == 0
    after = stored(hass, entry.entry_id)
    assert not before.pause.admitted
    assert (after.pause.choice, after.pause.action, after.pause.expires_at) == ("manual", "start", None)


@pytest.mark.parametrize("choice", [PAUSE_UNTIL_RESUMED, PAUSE_UNTIL_TOMORROW])
async def test_each_pause_choice_is_admitted_once_while_the_charger_is_charging(
    hass: HomeAssistant,
    hass_ws_client,
    offline_relay: None,
    effects: dict[str, Counted],
    choice: str,
) -> None:
    """Stop is a typed pause, and it is the admissible action exactly while the charger charges."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    start_charging(hass)

    frame = await ws_call(client, action_message(entry.entry_id, "stop", choice=choice))

    assert frame["success"] is True
    assert frame["result"]["ok"] is True and frame["result"]["choice"] == choice
    assert effects["stop"].calls == 1
    assert effects["start"].calls == 0 and effects["resume"].calls == 0
    assert stored(hass, entry.entry_id).pause.choice == choice, "the typed intent is stored"


async def test_a_typed_pause_is_admitted_while_the_charger_is_idle(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """An idle Auto charger is exactly what a pause is for: it blocks the next automatic application.

    Physical charging is not a precondition -- a plan for later tonight is the thing being paused -- so
    the typed pause is stored, exactly once, with no invented Start and no strategy change. (`until_resumed`
    needs no plan ahead of it; `next_period` would, which the choice list says by omitting it.)
    """
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    executor = preview_of(hass, entry.entry_id)._executor
    assert executor is not None
    assert PAUSE_UNTIL_RESUMED in executor.pause_choices(), "the choices the boundary would accept"
    assert PAUSE_NEXT_PERIOD not in executor.pause_choices(), "no planned period is still ahead"
    state = hass.states.get(CHARGE_CONTROL)
    assert state is not None and state.state == "off", "the prerequisite: idle"
    before = stored(hass, entry.entry_id)

    frame = await ws_call(client, action_message(entry.entry_id, "stop", choice=PAUSE_UNTIL_RESUMED))

    assert frame["result"]["ok"] is True, frame["result"]
    assert effects["stop"].calls == 1, "the pause, exactly once"
    assert effects["start"].calls == 0, "and never an invented Start"
    assert effects["immediate_stop"].calls == 0 and effects["resume"].calls == 0
    after = stored(hass, entry.entry_id)
    assert after.pause.choice == PAUSE_UNTIL_RESUMED, "the intent is stored"


async def test_a_choice_that_is_not_available_now_is_refused_by_code(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """With no planned period still ahead, `next_period` is not one of the choices Stop offers."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    start_charging(hass)
    executor = preview_of(hass, entry.entry_id)._executor
    assert executor is not None and "next_period" not in executor.pause_choices()
    before = stored(hass, entry.entry_id)

    frame = await ws_call(client, action_message(entry.entry_id, "stop", choice="next_period"))

    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == "invalid_pause", "the precise stable pause code"
    assert total(effects) == 0, "refused before the effect, not after it"
    assert stored(hass, entry.entry_id) == before


async def test_start_is_admitted_while_a_pause_is_persisted_and_the_pause_stands(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """Start now is the *immediate* axis: it is a command to the charger, not a planning decision.

    A persisted pause is Auto's own statement about automatic execution, and it has no opinion about a
    person pressing Start: the command is admitted, the charger is told to charge, and the pause is
    still stored byte for byte so that the next automatic event still finds it. That is precisely what
    the split buys -- a single folded action could not say it (a pause outranks a Start there), which is
    why an app that offered one button could only ever offer Resume while paused.
    """
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    await preview_of(hass, entry.entry_id).async_pause(PAUSE_UNTIL_RESUMED)
    paused = stored(hass, entry.entry_id)
    assert paused.pause.admitted, "the prerequisite: a pause is in force"
    assert not hass.states.get(CHARGE_CONTROL) or hass.states.get(CHARGE_CONTROL).state == "off"
    before_start, before_pause = effects["start"].calls, effects["stop"].calls
    before_resume = effects["resume"].calls

    frame = await ws_call(client, action_message(entry.entry_id, "start"))

    assert frame["result"]["ok"] is True, frame["result"]
    assert effects["start"].calls - before_start == 1, "one immediate command, and only one"
    assert effects["stop"].calls == before_pause, "no pause was taken"
    assert effects["resume"].calls == before_resume, "and nothing was resumed"
    after = stored(hass, entry.entry_id)
    assert after == paused, "the pause is not bypassed, and not cleared either: byte for byte"

    executor = preview_of(hass, entry.entry_id)._executor
    assert executor is not None
    assert executor.automatic_decision().action == "resume", "and the pause is still Auto's state"
    start_charging(hass)  # the charger's own report, which is what the immediate axis reads
    assert executor.immediate_decision().action == "stop", (
        "the immediate axis follows the charger, not the pause"
    )


async def test_a_stop_with_no_choice_is_immediate_and_pauses_auto_for_the_plug_in(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """The other half of the split: a plain Stop stops the charge and pauses Auto for the plug-in session.

    Deliberately *not* resolved into `until_resumed`: a caller that wants a pause for a span names the
    choice it wants. A plain "stop" is the person's own pause (`manual`, action `stop`), which ends when
    the car is unplugged, and changes no planning setting.
    """
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    start_charging(hass)
    before = stored(hass, entry.entry_id)
    assert not before.pause.admitted

    frame = await ws_call(client, action_message(entry.entry_id, "stop"))

    assert frame["result"]["ok"] is True, frame["result"]
    assert frame["result"]["choice"] is None
    assert effects["immediate_stop"].calls == 1, "the immediate stop, not the pause"
    assert effects["stop"].calls == 0, "no pause was taken, because none was asked for"
    assert effects["start"].calls == 0 and effects["resume"].calls == 0
    after = stored(hass, entry.entry_id)
    assert (after.pause.choice, after.pause.action, after.pause.expires_at) == ("manual", "stop", None)
    assert replace(after, revision=before.revision, pause=before.pause) == before, "no planning setting moved"


async def test_resume_is_admitted_once_and_a_repeat_is_refused(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """Resume is the existing transition; once the pause is gone, resuming again is not an action."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    await preview_of(hass, entry.entry_id).async_pause(PAUSE_UNTIL_RESUMED)
    assert stored(hass, entry.entry_id).pause.admitted

    first = await ws_call(client, action_message(entry.entry_id, "resume"))

    assert first["result"]["ok"] is True and first["result"]["action"] == "resume"
    cleared = stored(hass, entry.entry_id)
    assert cleared.pause.choice is None, "resumed"

    second = await ws_call(client, action_message(entry.entry_id, "resume"))

    assert second["result"]["ok"] is False
    assert second["result"]["error"] == ERROR_ACTION_UNAVAILABLE, "no pause, no resume"
    assert effects["resume"].calls == 1, "exactly one effect, and no hidden retry"
    assert stored(hass, entry.entry_id) == cleared, "and the state the first one left stands"


async def test_resume_with_no_pause_at_all_is_refused_with_zero_effects(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """Nothing to resume is not a resume: refused by code, and nothing moves."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    before = stored(hass, entry.entry_id)
    assert not before.pause.admitted

    frame = await ws_call(client, action_message(entry.entry_id, "resume"))

    assert frame["result"]["ok"] is False and frame["result"]["error"] == ERROR_ACTION_UNAVAILABLE
    assert total(effects) == 0
    assert stored(hass, entry.entry_id) == before


async def test_a_stale_dashboard_read_is_not_an_authority_token(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """The action a capture named is refused once the state has moved on: admission re-reads."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    executor = preview_of(hass, entry.entry_id)._executor
    assert executor is not None
    captured = executor.immediate_decision()
    assert captured.action == "start", "the capture said Start: the charger was idle"

    # Between the read and the request, somebody else started the charge.
    start_charging(hass)
    assert executor.immediate_decision().action == "stop", "the fact moved on"

    frame = await ws_call(client, action_message(entry.entry_id, captured.action))

    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == ERROR_ACTION_UNAVAILABLE
    assert effects["start"].calls == 0, "the stale action never ran"
    assert total(effects) == 0


async def test_a_start_awaiting_acknowledgement_blocks_the_next_one_for_card_and_command(
    hass: HomeAssistant,
    hass_ws_client,
    offline_relay: None,
    effects: dict[str, Counted],
) -> None:
    """The card and the command read the same fact: no Start, `action_pending`, until the report.

    The service call completes and changes no state, which is exactly the case serialization alone
    could not cover: the charger has not answered yet, so a second Start would be a duplicate command
    -- and offering one would be asking for it.
    """
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    async_mock_service(hass, "switch", "turn_on")  # a call whose state change has not arrived

    first = await ws_call(client, action_message(entry.entry_id, "start"))
    assert first["result"]["ok"] is True, "the Start itself is admitted"

    read = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )
    control = read["result"]["control"]
    assert control["immediate_action"] == "none", "nothing may be offered yet"
    assert control["immediate_action_reason"] == "action_pending"
    assert control["automatic_action"] == "none" and control["pause_choices"] == []

    second = await ws_call(client, action_message(entry.entry_id, "start"))

    assert second["result"]["ok"] is False
    assert second["result"]["error"] == ERROR_ACTION_UNAVAILABLE
    assert effects["start"].calls == 1, "one command, one effect"

    # The charger's own report is the acknowledgement, and Stop becomes the offered action.
    start_charging(hass)
    await hass.async_block_till_done()

    settled = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )
    assert settled["result"]["control"]["immediate_action"] == "stop"
    assert settled["result"]["control"]["immediate_action_reason"] is None


async def test_the_card_sees_a_recovered_start_as_clean(
    hass: HomeAssistant,
    hass_ws_client,
    offline_relay: None,
    effects: dict[str, Counted],
    pause_appointments: Any,
) -> None:
    """A timeout, an accepted retry and the charger's own report: the stale error is gone.

    As the card sees it: the dashboard's `execution_error` must not keep describing a start that
    has since been answered.
    """
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    async_mock_service(hass, "switch", "turn_on")
    dashboard = {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id}

    await ws_call(client, action_message(entry.entry_id, "start"))
    await pause_appointments.fire(pause_appointments.when)

    stale = await ws_call(client, dashboard)
    assert stale["result"]["control"]["execution_error"] == "action_failed"
    assert stale["result"]["control"]["immediate_action"] == "start", "a retry is what is offered"

    retry = await ws_call(client, action_message(entry.entry_id, "start"))
    assert retry["result"]["ok"] is True
    assert effects["start"].calls == 2, "the retry went out exactly once"

    start_charging(hass)
    await hass.async_block_till_done()

    fresh = await ws_call(client, dashboard)
    assert fresh["result"]["control"]["execution_error"] is None, "the recovered failure is gone"
    assert fresh["result"]["control"]["immediate_action"] == "stop"
    assert fresh["result"]["control"]["immediate_action_reason"] is None


@pytest.fixture
def decisions_under_lock(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Every control-facts capture the boundary took, with whether its lock was held at the time.

    Admission captures the facts once (`control_facts`) and derives *both* axes from that one object
    (`decide_axes`), so the capture is the moment to watch: a decision taken anywhere but under the
    admission lock would be a decision about a moment that has already passed, and would show up here
    as `False`. `asyncio.Lock.locked()` is the question itself, so this records the lock discipline
    rather than trusting the shape of the code.
    """
    recorded: list[bool] = []
    original = AutoExecutor.control_facts

    def record(self: AutoExecutor) -> Any:
        recorded.append(self._lock.locked())
        return original(self)

    monkeypatch.setattr(AutoExecutor, "control_facts", record)
    return recorded


@pytest.mark.parametrize("action", ["start", "stop", "resume"])
async def test_two_concurrent_requests_produce_one_effect_each(
    hass: HomeAssistant,
    offline_relay: None,
    effects: dict[str, Counted],
    decisions_under_lock: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Two requests admitted against one state: the second re-reads it and is refused.

    The first request is held *inside its own effect* (a gate the test closes until both requests
    have arrived) so the second one genuinely arrives while the first is mid-flight. That is the
    interleaving the lock exists for, and it is made deterministic rather than hoped for: every
    decision must be taken with the lock held, and only one effect may land.
    """
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id)
    preview = preview_of(hass, entry.entry_id)
    executor = preview._executor
    assert executor is not None
    gate = asyncio.Event()
    choice: str | None = None
    step = action

    if action == "start":
        inner_start = executor._controller.async_start

        async def gated_start(amps: int | None = None, *, manual: bool = False) -> None:
            await gate.wait()
            await inner_start(amps, manual=manual)
            start_charging(hass)

        executor._controller.async_start = gated_start  # type: ignore[method-assign]
    elif action == "stop":
        start_charging(hass)
        choice = PAUSE_UNTIL_RESUMED
        inner_stop = executor._controller.async_stop

        async def gated_stop(*, clear_schedule: bool = False) -> None:
            await gate.wait()
            await inner_stop(clear_schedule=clear_schedule)

        executor._controller.async_stop = gated_stop  # type: ignore[method-assign]
    else:
        await preview.async_pause(PAUSE_UNTIL_RESUMED)
        choice = None
        inner_write = AutoSettingsStore.async_update

        async def gated_write(*args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            return await inner_write(*args, **kwargs)

        monkeypatch.setattr(AutoSettingsStore, "async_update", gated_write)

    before_effects = effects[step].calls
    pending = asyncio.ensure_future(
        asyncio.gather(
            preview.async_manual_action(action, choice),
            preview.async_manual_action(action, choice),
            return_exceptions=True,
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)  # both requests reach their admission point
    gate.set()
    results = await pending

    refused = [r for r in results if isinstance(r, Exception)]
    assert effects[step].calls - before_effects == 1, "one effect, not two"
    assert len(refused) == 1, results
    assert refused[0].code == EXECUTION_ACTION_UNAVAILABLE, results
    assert decisions_under_lock == [True, True], "every decision was taken under the lock"
    if action == "stop":
        assert stored(hass, entry.entry_id).pause.choice == PAUSE_UNTIL_RESUMED
    elif action == "resume":
        assert stored(hass, entry.entry_id).pause.choice is None
    else:
        state = hass.states.get(CHARGE_CONTROL)
        assert state is not None and state.state == "on", "the one start that landed"


async def test_a_choice_that_becomes_unavailable_between_observation_and_admission(
    session: Session, clock: Any, monkeypatch: pytest.MonkeyPatch, pause_appointments: Any
) -> None:
    """The observation offered `next_period`; by admission time the plan has no period ahead.

    A delivery of "here are your choices" is a description of a moment, so the choice is judged again
    at admission -- against the plan that exists *then* -- and refused by the domain's own pause code
    rather than resolved into something nobody asked for.
    """
    now = clock()
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: now)
    serve(session.transport)
    await session.set_auto()
    assert session.executor.applied is not None, "the prerequisite: a plan is installed"
    session.hass.states.async_set(session.charge_control, "on")

    observed = session.executor.control_axes()
    assert "next_period" in observed.automatic.choices, "the observation offered the next planned period"

    plan = session.controller.plan
    assert plan is not None and plan.windows
    clock.now = max(start for start, _ in plan.windows)
    assert "next_period" not in session.executor.pause_choices(), "the plan's periods are behind us"
    before = session.settings()

    with pytest.raises(AutoControlError) as refusal:
        await session.preview.async_manual_action("stop", PAUSE_NEXT_PERIOD)

    assert refusal.value.code == "invalid_pause"
    assert session.settings() == before, "nothing was written"


async def test_a_start_whose_command_fails_is_a_pre_effect_refusal_with_its_domain_code(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """The charger refused the command: nothing started, so nothing is claimed to have started."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    preview = preview_of(hass, entry.entry_id)
    before = stored(hass, entry.entry_id)
    stops = async_mock_service(hass, "switch", "turn_off")

    async def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise ChargingExecutionError("switch_unavailable", "the switch did not answer")

    preview._executor._controller.async_start = refuse  # type: ignore[union-attr,method-assign]
    frame = await ws_call(client, action_message(entry.entry_id, "start"))

    assert frame["success"] is True
    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == "switch_unavailable", "the domain's own stable code"
    assert stored(hass, entry.entry_id) == before, "no record moved"
    assert stops == [], "and nothing was stopped"


async def test_a_pause_whose_intent_cannot_be_persisted_stops_nothing(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """The write is the effect: without it, the charger is not touched and no pause exists."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    start_charging(hass)
    preview = preview_of(hass, entry.entry_id)
    before = stored(hass, entry.entry_id)
    off_calls = async_mock_service(hass, "switch", "turn_off")

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the disk said no")

    monkeypatched = AutoSettingsStore.async_update
    AutoSettingsStore.async_update = refuse  # type: ignore[method-assign]
    try:
        frame = await ws_call(
            client, action_message(entry.entry_id, "stop", choice="until_resumed")
        )
    finally:
        AutoSettingsStore.async_update = monkeypatched  # type: ignore[method-assign]

    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == ERROR_ACTION_FAILED, "a pre-effect operational failure"
    assert stored(hass, entry.entry_id) == before, "the intent was never stored"
    assert off_calls == [], "and no charger command was issued"


async def test_a_pause_whose_stop_fails_is_accepted_and_offers_its_retry(
    session: Session, clock: Any, monkeypatch: pytest.MonkeyPatch, pause_appointments: Any
) -> None:
    """The intent committed and the stop did not: an accepted admission with the partial failure.

    Deliberately not reported as a refusal -- the pause *is* stored and is what the execution gate
    obeys -- and the choice that failed is offered again, because re-sending it is the boundary's
    documented retry rather than a re-resolution of an instant that has moved on.
    """
    # The controller's own window rules compare against `dt_util.utcnow()`, so pin it to this
    # harness's instant exactly as `tests/test_auto_pause_intent.py` does for its module.
    now = clock()
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: now)
    serve(session.transport)
    await session.set_auto()
    assert session.executor.applied is not None, "the prerequisite: a plan is installed"
    session.hass.states.async_set(session.charge_control, "on")  # and the charger is charging
    _script_stop(monkeypatch, session, failures=5)

    decision = await session.preview.async_manual_action("stop", PAUSE_UNTIL_RESUMED)

    assert decision.action == "stop", "the admission is real, and is not reported as refused"
    assert session.settings().pause.choice == PAUSE_UNTIL_RESUMED, "the intent stands"
    assert session.executor.last_error == "pause_stop_failed", "and the partial failure is named"
    retry = session.executor.control_axes()
    assert retry.automatic.action == "pause", "what is offered next is the retry"
    assert PAUSE_UNTIL_RESUMED in retry.automatic.choices, "with the choice that would perform it"


async def test_a_resume_whose_write_fails_leaves_the_pause_standing(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """Nothing committed, so nothing changed: the refusal says so and the pause is still there."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    preview = preview_of(hass, entry.entry_id)
    await preview.async_pause(PAUSE_UNTIL_RESUMED)
    paused = stored(hass, entry.entry_id)

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the disk said no")

    monkeypatched = AutoSettingsStore.async_update
    AutoSettingsStore.async_update = refuse  # type: ignore[method-assign]
    try:
        frame = await ws_call(client, action_message(entry.entry_id, "resume"))
    finally:
        AutoSettingsStore.async_update = monkeypatched  # type: ignore[method-assign]

    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == ERROR_ACTION_FAILED, "a pre-effect failure"
    assert stored(hass, entry.entry_id) == paused, "the pause was never cleared"


async def test_a_committed_resume_whose_reconcile_fails_is_never_reported_as_uncommitted(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, effects: dict[str, Counted]
) -> None:
    """The clear is durable: the code says committed, and the pause is *not* restored."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)
    preview = preview_of(hass, entry.entry_id)
    await preview.async_pause(PAUSE_UNTIL_RESUMED)

    async def fail_the_reconcile(_settings: Any) -> Any:
        raise RuntimeError("the calculation exploded")

    monkeypatched = AutoPlannerController._calculate
    AutoPlannerController._calculate = fail_the_reconcile  # type: ignore[method-assign]
    try:
        frame = await ws_call(client, action_message(entry.entry_id, "resume"))
    finally:
        AutoPlannerController._calculate = monkeypatched  # type: ignore[method-assign]

    assert frame["success"] is True
    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == ERROR_ACTION_RECONCILE_FAILED, "committed, not refused"
    after = stored(hass, entry.entry_id)
    assert after.pause.choice is None, "the cleared pause is not restored"
    executor = preview._executor
    assert executor is not None and executor.last_error == "reconcile_failed", "recorded, not lost"


async def test_an_unexpected_failure_is_a_stable_code_and_never_the_exception_prose(
    hass: HomeAssistant,
    hass_ws_client,
    offline_relay: None,
    effects: dict[str, Counted],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure from *below* the boundary is still one stable answer, and never an error frame.

    The exception is raised by the boundary itself, which is the case a refusal cannot cover: a
    database, a switch or a calculation can always surprise the transport, and the answer must be
    this contract's own code rather than Home Assistant's generic unhandled-error frame.
    """
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    await go_auto(hass, entry.entry_id)

    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a secret detail nobody should read")

    monkeypatch.setattr(AutoPlannerController, "async_manual_action", explode)
    frame = await ws_call(client, action_message(entry.entry_id, "start"))

    assert frame["success"] is True, "a contract envelope, not an unhandled error frame"
    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == ERROR_ACTION_FAILED
    assert "secret" not in str(frame), "no exception prose reaches a client"
    assert "RuntimeError" not in str(frame)
    assert effects["start"].calls == 0, "and the failing boundary ran no effect"


async def test_every_refused_request_makes_zero_writes_and_zero_boundary_calls(
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    offline_relay: None,
    effects: dict[str, Counted],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad action, misplaced choice, wrong id, site id, unloaded entry, non-admin, wrong version."""
    entry = await setup_charger(hass)
    unloaded = await setup_charger(
        hass, entry_id="entry_c", webhook_id="webhook-c", charge_control="switch.charger_c"
    )
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=[entry.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    assert await hass.config_entries.async_unload(unloaded.entry_id)
    await hass.async_block_till_done()
    charger_calls = forbid_charger_writes(monkeypatch)
    writes = Counted(monkeypatch, AutoSettingsStore, "async_update")
    services = [
        async_mock_service(hass, "switch", "turn_on"),
        async_mock_service(hass, "switch", "turn_off"),
    ]
    before = stored(hass, entry.entry_id)
    client = await hass_ws_client(hass)

    for message, code in (
        (action_message(entry.entry_id, "charge"), ERROR_INVALID_ACTION),
        (action_message(entry.entry_id, None), ERROR_INVALID_ACTION),
        (action_message(entry.entry_id, "start", choice=PAUSE_UNTIL_RESUMED), ERROR_INVALID_ACTION),
        (action_message(entry.entry_id, "resume", choice=PAUSE_UNTIL_TOMORROW), ERROR_INVALID_ACTION),
        (action_message(entry.entry_id, "stop", choice="solar"), "invalid_pause"),
        (action_message("entry_does_not_exist", "start"), "spotnav_unknown_charger"),
        (action_message("some_other_integration_entry", "start"), "spotnav_unknown_charger"),
        (action_message("site_a", "start"), "spotnav_site_not_charger"),
        (action_message("entry_c", "start"), "spotnav_charger_unloaded"),
    ):
        frame = await ws_call(client, message)
        assert frame["success"] is True, message
        assert frame["result"]["ok"] is False, message
        assert frame["result"]["error"] == code, message
        assert frame["result"]["action"] is None and frame["result"]["choice"] is None, message

    # Home Assistant's own admin boundary, not this handler: a non-admin cannot act at all.
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    refused = await ws_call(reader, action_message(entry.entry_id, "start"))
    assert refused["success"] is False and refused["error"]["code"] == "unauthorized"
    assert "result" not in refused

    # A version this contract does not speak is refused the way the other two contracts refuse one.
    wrong_version = await ws_call(client, action_message(entry.entry_id, "start", api_version=2))
    assert wrong_version["success"] is False
    assert wrong_version["error"]["code"] == "spotnav_unsupported_api_version"

    assert total(effects) == 0, "no effect step was reached"
    assert writes.calls == 0, "no settings write"
    assert all(call == [] for call in services), "no Home Assistant service called"
    assert charger_calls == [], "no route to a charger was even attempted"
    assert stored(hass, entry.entry_id) == before, "and the record is untouched"


async def test_the_action_contract_has_its_own_version_and_is_registered_once(
    hass: HomeAssistant, hass_ws_client, offline_relay: None
) -> None:
    """A version of its own, refused by this contract's own code, and registered by the domain."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)
    assert "spotnav/manual_action" in hass.data["websocket_api"]

    missing = await ws_call(
        client, {"type": "spotnav/manual_action", "charger_id": entry.entry_id, "action": "start"}
    )
    assert missing["success"] is False
    assert missing["error"]["code"] == "spotnav_unsupported_api_version"
    # This contract's version is its own constant: the *name* is what is not shared with the settings
    # or dashboard contracts, and each of them refuses the others' values.
    assert ACTION_API_VERSION == 1

    # An unknown key is a bad request, refused before any handler of ours runs.
    extra = await ws_call(
        client,
        {
            "type": "spotnav/manual_action",
            "api_version": ACTION_API_VERSION,
            "charger_id": entry.entry_id,
            "action": "start",
            "amps": 16,
        },
    )
    assert extra["success"] is False and extra["error"]["code"] == "invalid_format"
