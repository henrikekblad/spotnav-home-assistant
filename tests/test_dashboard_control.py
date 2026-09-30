"""The dashboard's `control` and `strategy` blocks: pure functions of one capture.

A compact card must render the action the *backend* named, and no client may infer control safety
from prices, entity names or a plan it read for itself. So `control` answers two axes, each with its
own action and its own reason:

* **immediate** -- what the *charger* may be told to do now (`start` or `stop`), blind to Auto's
  planning record and to the pause;
* **automatic** -- what may happen to Home Assistant's own execution (`pause`, with the choices it
  would accept, or `resume`), which is why an *idle* Auto charger with a plan for later tonight is
  offered both "Start now" and "Pause".

Both come from the capture's own facts through `execution.decide_axes` -- no live accessor is called
while serializing -- and most tests here build a capture by hand, so they need no socket at all. The
states that matter are pinned as contract fixtures the card decodes (`tests/fixtures/dashboard/`),
written by the real capture and serializer (`SPOTNAV_WRITE_FIXTURES=1` rewrites them).
"""

from __future__ import annotations

import itertools
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution.auto_execution import (
    CONTROL_ACTION_PENDING,
    EXECUTION_PAUSE_CLEAR_FAILED,
    EXECUTION_PAUSE_STOP_FAILED,
    AutoExecutor,
    decide_axes,
)
from custom_components.spotnav.planning.auto_settings import (
    PAUSE_NEXT_PERIOD,
    PAUSE_UNTIL_RESUMED,
    PAUSE_UNTIL_TOMORROW,
    AutoSettings,
    PauseIntent,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.api.dashboard import (
    ACTION_NONE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    ACTION_STOP,
    CONTROL_NO_SETTINGS,
    CONTROL_PAUSE_UNSETTLED,
    STRATEGY_HYBRID,
    STRATEGY_HYBRID_REASON,
    STRATEGY_SOLAR,
    STRATEGY_SOLAR_REASON,
    serialize_control,
    serialize_strategy,
)
from tests.test_dashboard_api import NOW, Counted
from tests.world import go_auto, setup_charger, ws_call
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import executor_for, preview_for

pytestmark = pytest.mark.usefixtures("offline_relay")

#: The charge-control entity the fixtures' chargers are wired to.
CHARGE_CONTROL = "switch.charger_a"

UNSET = object()

#: An instant inside the days the dashboard fixtures serve, so a plan is calculable at it.
MOMENT = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
_REQUEST_IDS = itertools.count(9000)

#: The control block's exact key set, frozen so a rename or a dropped field is a failure rather than
#: a surprise in a client.
CONTROL_KEYS = frozenset(
    {
        "immediate_action",
        "immediate_action_reason",
        "automatic_action",
        "automatic_action_reason",
        "pause_choices",
        "pause",
        "pause_blocks_execution",
        "execution_error",
        "can_act",
    }
)

FIXTURE_DIR: Final = Path(__file__).resolve().parent / "fixtures" / "dashboard"


def capture_with(
    *,
    settings: Any = UNSET,
    execution_state: str = "not_applied",
    execution_error: str | None = None,
    paused: bool | None = False,
    charging: bool = False,
    schedule_active: bool = False,
    pause_choices: tuple[str, ...] = (),
) -> dashboard_api.CapturedDashboard:
    """One hand-built capture, so the pure serializers can be pinned without a socket.

    Only the fields the strategy and control blocks read are ever interesting here; everything else is
    a stable placeholder. The defaults describe a charger that holds nothing: no pause, no plan and no
    boundary facts beyond that.
    """
    if settings is UNSET:
        settings = replace(AutoSettings(), area_id="SE4")
    return dashboard_api.CapturedDashboard(
        generated_at=MOMENT,
        charger=dashboard_api.CapturedCharger("entry_a", "A", True, ()),
        settings=settings,
        snapshot=None,
        catalogue=None,
        area_entry=None,
        area=None,
        days=(),
        plan=None,
        live=dashboard_api.CapturedLive(charging, schedule_active, None, None, None),
        execution=dashboard_api.CapturedExecution(
            execution_state, execution_error, None, None, None, paused
        ),
        pause_choices=pause_choices,
        intervals=None,
        site=None,
    )


def paused_settings(choice: str = PAUSE_UNTIL_RESUMED) -> AutoSettings:
    """An Auto record whose pause is admitted, so the automatic axis has a real intent to read."""
    return replace(AutoSettings(), pause=PauseIntent(choice=choice, admitted_at=None))


def control_for(**kwargs: Any) -> dict[str, Any]:
    return serialize_control(capture_with(**kwargs), can_act=True)


# ------------------------------------------- the two axes, for every state that exists


def test_the_control_block_is_exactly_these_keys() -> None:
    control = control_for()
    assert set(control) == CONTROL_KEYS
    assert "primary_action" not in control, "one fold answers one question; two axes answer both"


def test_an_idle_auto_charger_offers_start_now_and_pause_at_once() -> None:
    """Idle Auto is `start` *and* `pause`.

    Physical charging is deliberately not a precondition for the automatic axis: a pause is a stored
    intent that blocks the next application, so an idle charger with a plan for later tonight is
    exactly what somebody pauses.
    """
    capture = capture_with(charging=False, pause_choices=(PAUSE_NEXT_PERIOD, PAUSE_UNTIL_RESUMED))
    control = serialize_control(capture, can_act=True)

    assert control["immediate_action"] == ACTION_START and control["immediate_action_reason"] is None
    assert control["automatic_action"] == ACTION_PAUSE and control["automatic_action_reason"] is None
    assert control["pause_choices"] == [PAUSE_NEXT_PERIOD, PAUSE_UNTIL_RESUMED], "the capture's own list"


def test_every_matrix_row_is_two_independent_answers() -> None:
    """The product matrix, state by state, as the two axes.

    Each row is a charger state the design names, and the assertion is the pair of answers -- never a
    folded one. `none` states carry their own stable reason on the axis that has no action, so a client
    renders a sentence per axis rather than one compromise sentence for both.
    """
    idle = capture_with(charging=False, pause_choices=(PAUSE_UNTIL_RESUMED,))
    charging = capture_with(charging=True, pause_choices=(PAUSE_UNTIL_RESUMED,))
    paused_idle = capture_with(
        charging=False, paused=True, settings=paused_settings(), pause_choices=(PAUSE_UNTIL_RESUMED,)
    )
    paused_charging = capture_with(
        charging=True, paused=True, settings=paused_settings(), pause_choices=(PAUSE_UNTIL_RESUMED,)
    )
    rows = {
        "idle, not paused": (idle, (ACTION_START, ACTION_PAUSE)),
        "charging, not paused": (charging, (ACTION_STOP, ACTION_PAUSE)),
        "idle, paused": (paused_idle, (ACTION_START, ACTION_RESUME)),
        "charging, paused": (paused_charging, (ACTION_STOP, ACTION_RESUME)),
    }
    for why, (capture, (immediate, automatic)) in rows.items():
        control = serialize_control(capture, can_act=True)
        assert (control["immediate_action"], control["automatic_action"]) == (immediate, automatic), why
        assert control["immediate_action_reason"] is None, why
        assert control["automatic_action_reason"] is None, why
        # Choices exist beside the automatic pause and nowhere else: a resume has nothing to choose.
        expected = list(capture.pause_choices) if automatic == ACTION_PAUSE else []
        assert control["pause_choices"] == expected, why

    # No settings record: neither axis can name an action, and the pause observation is honestly null.
    control = serialize_control(capture_with(settings=None), can_act=True)
    assert (control["immediate_action"], control["automatic_action"]) == (ACTION_NONE, ACTION_NONE)
    assert control["immediate_action_reason"] == CONTROL_NO_SETTINGS
    assert control["automatic_action_reason"] == CONTROL_NO_SETTINGS
    assert control["pause"] is None and control["pause_choices"] == []

    # A Start awaiting the charger's acknowledgement: nothing on either axis, which is the one state
    # where an idle-looking charger is offered no pause either.
    base = capture_with(charging=False, pause_choices=(PAUSE_UNTIL_RESUMED,))
    pending = replace(base, execution=replace(base.execution, start_pending=True))
    control = serialize_control(pending, can_act=True)
    assert (control["immediate_action"], control["automatic_action"]) == (ACTION_NONE, ACTION_NONE)
    assert control["immediate_action_reason"] == CONTROL_ACTION_PENDING
    assert control["automatic_action_reason"] == CONTROL_ACTION_PENDING
    assert control["pause_choices"] == []


def test_a_stored_intent_the_boundary_has_not_settled_offers_no_automatic_action() -> None:
    unsettled = control_for(
        settings=replace(
            AutoSettings(),
            pause=PauseIntent(choice=PAUSE_UNTIL_TOMORROW, admitted_at=MOMENT, expires_at=MOMENT),
        ),
        paused=False,
    )
    assert unsettled["automatic_action"] == ACTION_NONE
    assert unsettled["automatic_action_reason"] == CONTROL_PAUSE_UNSETTLED
    assert unsettled["pause_blocks_execution"] is True, "and it still blocks execution"


def test_the_two_failed_pause_states_keep_their_own_truthful_answers() -> None:
    """The two ways a pause can be half-done, and why only one of them offers the retry.

    * a pause whose *physical stop* failed: the intent is stored and the plan may still be charging,
      and re-sending the same choice is the boundary's documented retry -- so the automatic axis
      offers the pause again, with the choices it would accept;
    * a pause whose *clearing write* failed: elapsed yet still persisted, so nothing may be applied
      and there is nothing to resume -- `none`, with the code that says why.
    """
    stop_failed = capture_with(
        charging=True,
        paused=True,
        execution_error=EXECUTION_PAUSE_STOP_FAILED,
        settings=paused_settings(choice=PAUSE_NEXT_PERIOD),
        pause_choices=(PAUSE_UNTIL_RESUMED, PAUSE_NEXT_PERIOD),
    )
    control = serialize_control(stop_failed, can_act=True)
    assert control["immediate_action"] == ACTION_STOP, "the charger reads itself, pause or no pause"
    assert control["automatic_action"] == ACTION_PAUSE and control["automatic_action_reason"] is None
    assert control["pause_choices"] == [PAUSE_UNTIL_RESUMED, PAUSE_NEXT_PERIOD]

    intent = PauseIntent(
        choice=PAUSE_NEXT_PERIOD, admitted_at=MOMENT - timedelta(hours=2), expires_at=MOMENT
    )
    clear_failed = capture_with(
        charging=False,
        paused=False,
        execution_error=EXECUTION_PAUSE_CLEAR_FAILED,
        settings=replace(AutoSettings(), pause=intent),
        pause_choices=(PAUSE_UNTIL_RESUMED,),
    )
    control = serialize_control(clear_failed, can_act=True)
    assert control["immediate_action"] == ACTION_START
    assert control["automatic_action"] == ACTION_NONE
    assert control["automatic_action_reason"] == EXECUTION_PAUSE_CLEAR_FAILED
    assert control["pause_choices"] == []
    assert control["pause"] == {
        "choice": PAUSE_NEXT_PERIOD,
        "admitted_at": intent.admitted_at.isoformat(),
        "expires_at": intent.expires_at.isoformat(),
    }, "the *persisted* record, not the clock's view of it"
    assert control["pause_blocks_execution"] is True, "the gate and the record are one fact"
    assert control["execution_error"] == EXECUTION_PAUSE_CLEAR_FAILED


def test_the_axis_pair_is_one_decision_over_one_capture() -> None:
    """`serialize_control` is the paired pure rule: `decide_axes` over the capture's own facts.

    Stated as an equality rather than a shape, so the serializer cannot quietly grow an opinion of its
    own -- and so the two halves it publishes are provably the two halves of *one* fact object.
    """
    capture = capture_with(charging=True, pause_choices=(PAUSE_UNTIL_RESUMED,))
    axes = decide_axes(dashboard_api.control_facts(capture))
    control = serialize_control(capture, can_act=False)

    assert control["immediate_action"] == axes.immediate.action
    assert control["immediate_action_reason"] == axes.immediate.reason
    assert control["automatic_action"] == axes.automatic.action
    assert control["automatic_action_reason"] == axes.automatic.reason
    assert control["pause_choices"] == list(axes.automatic.choices)
    assert control["can_act"] is False


def test_the_strategy_block_states_cheapest_and_describes_the_unavailable_ones() -> None:
    """One real strategy, two honest capability rows, and no hole where an unavailable value sits."""
    strategy = serialize_strategy(capture_with())
    assert strategy["selected"] == "cheapest"
    assert strategy["available"] == [
        {"strategy": "cheapest", "available": True, "reason": None},
        {"strategy": STRATEGY_SOLAR, "available": False, "reason": STRATEGY_SOLAR_REASON},
        {"strategy": STRATEGY_HYBRID, "available": False, "reason": STRATEGY_HYBRID_REASON},
    ]

    empty = serialize_strategy(capture_with(settings=None))
    assert empty["selected"] is None
    assert all(row is not None for row in empty["available"]), "no null rows to render around"
    assert [row["strategy"] for row in empty["available"]] == [STRATEGY_SOLAR, STRATEGY_HYBRID]


# ---------------------------------------------- one capture, never two live reads


async def test_a_response_reads_memory_and_calls_nothing(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture is a read-only one: no network, no service, no install, no settings write."""
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id)
    store = domain_data(hass).auto_store
    assert store is not None
    revision = store.settings(entry.entry_id).revision
    client = await hass_ws_client(hass)
    repository = dashboard_api.domain_data(hass).price_repository
    assert repository is not None
    calls = len(repository._client().calls)  # noqa: SLF001 - the stub wire's own record
    installs = Counted(monkeypatch, ChargingController, "async_install")
    stops = Counted(monkeypatch, ChargingController, "async_stop")

    frame = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )

    assert frame["success"] is True
    assert frame["result"]["control"]["can_act"] is True, "an administrative connection"
    assert len(repository._client().calls) == calls, "no request to the relay"
    assert installs.calls == 0 and stops.calls == 0, "no schedule touched"
    assert store.settings(entry.entry_id).revision == revision, "no settings write"


async def test_a_non_admin_sees_the_same_facts_with_can_act_false(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, offline_relay: None
) -> None:
    """Authority is a fact about the caller, published beside the facts, never guessed by a client."""
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id)
    reader = await hass_ws_client(hass, hass_read_only_access_token)

    frame = await ws_call(
        reader,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )

    assert frame["success"] is True
    assert frame["result"]["control"]["can_act"] is False
    assert frame["result"]["control"]["immediate_action"] == ACTION_START, "the same control facts"


async def test_the_control_never_reads_a_live_boundary(
    hass: HomeAssistant, hass_ws_client, offline_relay: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two axes come from the capture, so poisoning every live accessor changes nothing.

    `capture_dashboard` takes one snapshot, and if any *serializer* still reached for the running
    boundary's own decision, these methods would be called and this test would fail with the assertion
    inside the poison. (The capture's own reads -- `execution_state`, `pause_choices` and friends --
    are not poisoned: reading them once *is* the capture, and it is the second read by a serializer
    that the rule forbids.)
    """
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id)
    hass.states.async_set(CHARGE_CONTROL, "on")
    await hass.async_block_till_done()

    def poison(name: str) -> Any:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"a live read of {name} happened while serializing")

        return boom

    for name in ("control_facts", "control_axes", "immediate_decision", "automatic_decision"):
        monkeypatch.setattr(AutoExecutor, name, poison(name))

    client = await hass_ws_client(hass)
    frame = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )
    assert frame["success"] is True, frame
    control = frame["result"]["control"]
    # The charger is charging and Auto owns it with no pause: both axes, from the capture.
    assert control["immediate_action"] == ACTION_STOP
    assert control["automatic_action"] == ACTION_PAUSE


# ------------------------------------------------- the cross-language contract fixtures


def _pause(**changes: Any) -> PauseIntent:
    """A pause written straight into the store, with fixed instants.

    Deliberately not admitted through the boundary: `until_tomorrow` resolves against the *real*
    clock (the next local midnight), which would make a fixture different every day. A fixture is a
    contract sample, so its instants are constants and only the *shape* of the record varies.
    """
    base = PauseIntent(
        choice=PAUSE_UNTIL_TOMORROW,
        admitted_at=datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc),
    )
    return replace(base, **changes)


async def _payload(hass: HomeAssistant, entry: Any, *, can_act: bool = True) -> dict[str, Any]:
    """One charger's answer through the real capture and serializer.

    Deliberately *not* normalized: the clock is frozen and the relay's two documents are fixed, so
    every leaf of this payload is deterministic, and a fixture that shows the real bytes is stronger
    evidence than one whose volatile leaves were scrubbed before it was compared.
    """
    return dashboard_api.serialize_dashboard(
        dashboard_api.capture_dashboard(hass, entry), can_act=can_act
    )


async def _state_start_idle(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """Idle with a plan installed: Start now, and Pause."""
    await go_auto(hass, entry.entry_id)
    return await _payload(hass, entry)


async def _state_stop_charging(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """A charging charger: Stop, and Pause with the choices this charger can honour."""
    await go_auto(hass, entry.entry_id)
    hass.states.async_set(CHARGE_CONTROL, "on")
    return await _payload(hass, entry)


async def _state_resume_active(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """An indefinite pause: Resume, with its typed record."""
    await go_auto(hass, entry.entry_id)
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        entry.entry_id,
        mutate=lambda current: replace(
            current, pause=_pause(choice=PAUSE_UNTIL_RESUMED, expires_at=None)
        ),
    )
    return await _payload(hass, entry)


async def _fresh_charger(hass: HomeAssistant, entry: Any) -> Any:
    """The same charger, back to a fresh installation between fixtures.

    A fixture is a state, and a state must not depend on the order the fixtures are built in: the
    store goes back to its defaults and the boundary's own in-memory facts (its last error, an
    outstanding Start, a pending acknowledgement failure) are cleared with it, because they are not
    part of the record and no fresh installation has them.
    """
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(entry.entry_id, mutate=lambda _current: AutoSettings())
    executor = executor_for(hass, entry.entry_id)
    assert executor is not None
    executor._last_error = None  # noqa: SLF001 - the boundary's own facts, not the record
    executor._start_pending = False  # noqa: SLF001 - as above
    executor._start_ack_timed_out = False  # noqa: SLF001 - as above
    hass.states.async_set(CHARGE_CONTROL, "off")
    return entry


async def _state_pause_clear_failed(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """An expired pause whose clearing write failed: the record stands, and nothing may run."""
    await go_auto(hass, entry.entry_id)
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        entry.entry_id,
        mutate=lambda current: replace(
            current,
            pause=_pause(
                choice=PAUSE_UNTIL_TOMORROW,
                admitted_at=datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc),
                expires_at=datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc),
            ),
        ),
    )
    persistence = store._store  # noqa: SLF001 - the store's own persistence seam
    original = persistence.async_save

    async def refuse(_data: Any) -> Any:
        raise RuntimeError("the disk said no")

    persistence.async_save = refuse
    try:
        executor = executor_for(hass, entry.entry_id)
        assert executor is not None
        assert await executor.async_settle_pause() is False, "the clear must not commit"
    finally:
        persistence.async_save = original
    assert executor.last_error == "pause_clear_failed"
    return await _payload(hass, entry)


async def _state_action_pending(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """A Start accepted, with the charger not having reported charging yet."""
    await go_auto(hass, entry.entry_id)
    async_mock_service(hass, "switch", "turn_on")
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    await preview.async_manual_action("start")
    return await _payload(hass, entry)


async def _state_no_settings(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """No settings record at all: `no_settings`, and a `pause` that is honestly null.

    Built from a capture with `settings=None`, which is exactly when the control serializer emits a
    null pause -- the serializer itself still writes every byte of the payload.
    """
    capture = replace(dashboard_api.capture_dashboard(hass, entry), settings=None)
    return dashboard_api.serialize_dashboard(capture, can_act=True)


#: The control-state fixtures, and the builder for each. The names are the card's contract too.
CONTROL_FIXTURES: Final = {
    "start_idle.json": _state_start_idle,
    "stop_charging.json": _state_stop_charging,
    "resume_active.json": _state_resume_active,
    "pause_clear_failed.json": _state_pause_clear_failed,
    "action_pending.json": _state_action_pending,
    "no_settings.json": _state_no_settings,
}


@freeze_time(NOW)
async def test_the_committed_control_fixtures_are_the_serializers_own_output(
    hass: HomeAssistant, offline_relay: None
) -> None:
    """Every control-state fixture equals what the real serializer produces, or this test fails.

    The fixtures are consumed by the *card* suite (and the app's), which decode these very files, so
    this is the one place that keeps the languages agreeing: they are generated here by the Python
    serializer and read there. There is no second, hand-maintained copy.
    """
    entry = await setup_charger(hass, current_limit="number.charger_a_limit")
    hass.states.async_set("number.charger_a_limit", "16", {"min": 6, "max": 16})
    assert entry.entry_id == "entry_a"

    produced: dict[str, Any] = {}
    for name, build in CONTROL_FIXTURES.items():
        # One fresh charger per fixture: a state is built from a clean installation, not from
        # whatever the previous one left behind.
        entry = await _fresh_charger(hass, entry)
        produced[name] = await build(hass, entry)
        assert set(produced[name]["control"]) == CONTROL_KEYS, name

    for name, payload in produced.items():
        path = FIXTURE_DIR / name
        if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            continue
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored == payload, f"{name} no longer matches the serializer output"

    # The two states the design names, read straight off the produced payloads.
    assert produced["start_idle.json"]["control"]["immediate_action"] == ACTION_START
    assert produced["start_idle.json"]["control"]["automatic_action"] == ACTION_PAUSE
    assert produced["stop_charging.json"]["control"]["immediate_action"] == ACTION_STOP
    assert produced["resume_active.json"]["control"]["automatic_action"] == ACTION_RESUME
    assert produced["pause_clear_failed.json"]["control"]["automatic_action"] == ACTION_NONE
    assert produced["action_pending.json"]["control"]["immediate_action_reason"] == CONTROL_ACTION_PENDING
    assert produced["no_settings.json"]["control"]["immediate_action_reason"] == CONTROL_NO_SETTINGS
