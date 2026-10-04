"""Fixtures for SpotNav charging control tests."""

from __future__ import annotations

from functools import partial
from typing import Any, Callable

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import event as event_helper
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution import auto_execution as auto_execution_module, controller as controller_module
from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan

from .relay import BASE_URL, Clock, FakeScheduler, serve, StubTransport
from .harness import FlakyStore, Harness, RecordedAppointments, Session


pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make custom_components/spotnav discoverable during tests."""
    yield


@pytest.fixture(autouse=True)
def no_first_run_defaults(request, monkeypatch):
    """Keep a charger's settings exactly as each test set them up.

    Setting a charger up seeds first-run defaults (`planning/first_run.py`), which would move every
    test's revision and calculation. `tests/test_first_run.py` opts back in with its marker.
    """
    if request.node.get_closest_marker("first_run_defaults") is not None:
        return

    async def _no_defaults(*_args: Any, **_kwargs: Any) -> bool:
        return False

    import custom_components.spotnav as integration

    monkeypatch.setattr(integration, "async_seed_first_run", _no_defaults)


@pytest.fixture(autouse=True)
def no_history_import(request, monkeypatch):
    """A charger's setup does not schedule the background import of earlier charges (a timer that
    outlives the test); `tests/test_history_import.py` opts back in with the `history_import` marker."""
    if request.node.get_closest_marker("history_import") is not None:
        return
    import custom_components.spotnav as integration

    monkeypatch.setattr(integration, "_async_schedule_history_import", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def no_installation_questions(request, monkeypatch):
    """Let a new charger's flow create the entry at once, as the flow tests that are about something
    else expect. The step that asks the charger's phases and the voltage between phases
    (`charger_installation`) is exercised by the tests marked `installation_questions`.
    """
    if request.node.get_closest_marker("installation_questions") is not None:
        return
    from custom_components.spotnav.flows.flow import SpotNavChargingConfigFlow

    monkeypatch.setattr(
        SpotNavChargingConfigFlow, "_installation_questions", lambda self, data: (False, False)
    )


@pytest.fixture(autouse=True)
def frontend_is_set_up(request):
    """`frontend` is a hard dependency of the integration, and the real one cannot be set up here.

    It needs the `hass_frontend` bundle and drags in a dozen more components. What the integration
    uses of it is `add_extra_js_url`, which needs only the URL manager the frontend's own setup would
    have created -- so a test that has a `hass` finds the frontend already set up, with that manager.
    """
    if "hass" not in request.fixturenames:
        return
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager

    hass = request.getfixturevalue("hass")
    hass.config.components.add("frontend")
    hass.data.setdefault(DATA_EXTRA_MODULE_URL, UrlManager(lambda *_: None, []))


# --- Guard: every function handed to a Home Assistant timer/listener helper runs on the loop ---
#
# Home Assistant runs a plain (unmarked, non-coroutine) function in a worker thread. From there
# `async_write_ha_state`, `async_create_task` and every controller recompute are off-loop errors --
# two of which reached a live system. The guard wraps each `async_track_*` / `async_call_later`
# helper *as the integration's modules see it* and classifies what they are handed with
# `HassJob`, which is Home Assistant's own classification: the same function that will decide
# between loop and executor in production. It records instead of raising (a raise inside a timer
# setup can be swallowed by the code under test) and asserts at teardown of every test.
#
# Runtime, not an AST scan, on purpose: a static scan cannot resolve `self._x`, partials, lambdas
# built in factories or names bound in other modules, and would either miss those or need
# suppressions. The runtime guard sees the actual object; its cost is that only exercised paths
# are checked, so `tests/test_loop_callback_guard.py` adds a static check that every call site
# uses a helper this fixture wraps, and the suite is broad enough to reach every arm site.

_GUARDED_HELPERS = (
    "async_track_point_in_time",
    "async_track_point_in_utc_time",
    "async_track_time_interval",
    "async_track_state_change_event",
    "async_track_state_report_event",
    "async_track_time_change",
    "async_track_utc_time_change",
    "async_track_template",
    "async_track_state_change",
    "async_call_later",
    "async_call_at",
)


@pytest.fixture(autouse=True)
def loop_callback_guard(monkeypatch):
    import importlib
    import inspect
    import pkgutil

    from homeassistant.core import HassJob, HassJobType
    from homeassistant.helpers import event as ha_event

    import custom_components.spotnav as package

    violations: list[str] = []

    def make_wrapper(name, real):
        signature = inspect.signature(real)

        def wrapper(*args, **kwargs):
            try:
                action = signature.bind(*args, **kwargs).arguments.get("action")
            except TypeError:
                action = None
            if action is not None:
                job = action if isinstance(action, HassJob) else HassJob(action)
                if job.job_type == HassJobType.Executor:
                    violations.append(
                        f"{name} was given {job.target!r}, which is neither a coroutine function "
                        "nor marked @callback: Home Assistant would run it in a worker thread"
                    )
            return real(*args, **kwargs)

        wrapper.__wrapped__ = real
        wrapper._loop_callback_guard = True
        return wrapper

    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        module = importlib.import_module(info.name)
        for name in _GUARDED_HELPERS:
            real = getattr(module, name, None)
            if real is not None and real is getattr(ha_event, name, None):
                monkeypatch.setattr(module, name, make_wrapper(name, real))
    yield
    assert not violations, "\n".join(violations)


# --- The charge-ownership core's shadow: every test runs with it, and it must agree with today's code ---


def pytest_configure(config):
    """`SPOTNAV_CORE_OWNERSHIP=1` runs the whole suite with the charge-ownership core driving ownership
    (`const.CONF_CORE_OWNERSHIP` on for every charger that does not say), step 2 of the refactor."""
    import os

    global COMMANDS
    if os.environ.get("SPOTNAV_SHADOW_REPORT"):
        COMMANDS = {}
    if os.environ.get("SPOTNAV_CORE_OWNERSHIP") == "1":
        from custom_components.spotnav.execution import ownership_shadow

        ownership_shadow.CORE_OWNERSHIP_DEFAULT = True


#: Over the whole run: events the shadow decided, comparisons, disagreements (explained or not), drifts, errors.
SHADOW_TOTALS: dict[str, int] = {}
#: Every drift seen (a change of today's owner or intent that no event explained), and every owner the core wrote back
#: over today's fields when it drives, for the run's summary.
DRIFTS: list[tuple[str, dict[str, Any]]] = []
#: With `SPOTNAV_SHADOW_REPORT` set: every start and stop each test sent a charger, to compare the two modes.
COMMANDS: dict[str, list[str]] | None = None


@pytest.fixture(autouse=True)
def ownership_shadow_agrees(request):
    """Every test runs the charge-ownership core in shadow mode beside today's code
    (`execution/ownership_shadow.py`) and fails on a disagreement nobody explained, on a drift nobody explained,
    or on a shadow error.

    An explained disagreement is a real difference of today's code the core does not copy, named in
    `tests/shadow_known.py` with its event sequence; an explained drift is a test that sets today's state directly,
    named there with what it sets. A test about the shadow itself opts out with the `shadow_disagreement_expected`
    marker.
    """
    from custom_components.spotnav.execution import ownership_shadow

    from .shadow_known import explain, explain_drift

    seen: list[tuple[str, dict[str, Any]]] = []

    def listener(kind: str, record: dict[str, Any]) -> None:
        seen.append((kind, record))
        if kind in ("drift", "write_back", "verdict_differs"):
            DRIFTS.append((request.node.nodeid, {"kind": kind, **record}))

    ownership_shadow.LISTENERS.append(listener)
    commands = COMMANDS.setdefault(request.node.nodeid, []) if COMMANDS is not None else None
    if commands is not None:
        from custom_components.spotnav.execution.chargers import adapter as adapter_module

        real_start, real_stop = adapter_module.ChargerAdapter.async_start, adapter_module.ChargerAdapter.async_stop

        async def start(self, *args, **kwargs):
            commands.append("start")
            return await real_start(self, *args, **kwargs)

        async def stop(self, *args, **kwargs):
            commands.append("stop")
            return await real_stop(self, *args, **kwargs)

        adapter_module.ChargerAdapter.async_start, adapter_module.ChargerAdapter.async_stop = start, stop
    try:
        yield seen
    finally:
        import os

        dump = os.environ.get("SPOTNAV_SHADOW_DUMP_EVENTS")
        if dump and dump in request.node.nodeid:
            import json

            records = [list(shadow.events) for shadow in ownership_shadow.SHADOWS]
            name = request.node.nodeid.replace("/", "_").replace("::", "__")
            with open(os.path.join(os.environ.get("SPOTNAV_SHADOW_DUMP", "."), f"events_{name}.json"), "w") as handle:
                json.dump(records, handle, indent=1, default=str)
        if commands is not None:
            adapter_module.ChargerAdapter.async_start, adapter_module.ChargerAdapter.async_stop = real_start, real_stop
    ownership_shadow.LISTENERS.remove(listener)
    unexplained: list[dict[str, Any]] = []
    for kind, record in seen:
        key = kind
        if kind == "disagreement":
            reason = explain(record)
            key = "disagreement_explained" if reason else "disagreement"
            if reason:
                SHADOW_TOTALS[f"explained:{reason}"] = SHADOW_TOTALS.get(f"explained:{reason}", 0) + 1
            else:
                unexplained.append(record)
        elif kind == "drift":
            reason = explain_drift(request.node.originalname, record)
            if reason:
                SHADOW_TOTALS[f"drift_explained:{reason}"] = SHADOW_TOTALS.get(f"drift_explained:{reason}", 0) + 1
            else:
                unexplained.append({"drift": record})
        elif kind == "error":
            unexplained.append(record)
        SHADOW_TOTALS[key] = SHADOW_TOTALS.get(key, 0) + 1
    if request.node.get_closest_marker("shadow_disagreement_expected") is not None:
        return
    if unexplained:
        import json
        import os

        dump = os.environ.get("SPOTNAV_SHADOW_DUMP")
        if dump:
            name = request.node.nodeid.replace("/", "_").replace("::", "__")
            with open(os.path.join(dump, f"{name}.json"), "w", encoding="utf-8") as handle:
                json.dump(unexplained, handle, indent=1, default=str)
        pytest.fail(
            "the ownership core's shadow disagrees with today's code:\n"
            + "\n".join(json.dumps(record, default=str)[:4000] for record in unexplained[:3]),
            pytrace=False,
        )


def pytest_sessionfinish(session, exitstatus):
    """With `SPOTNAV_SHADOW_REPORT` set, write the run's shadow totals there (JSON)."""
    import json
    import os

    from custom_components.spotnav.execution import ownership_shadow

    path = os.environ.get("SPOTNAV_SHADOW_REPORT")
    if not path:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        path = f"{path}.{worker}"
    totals = {**SHADOW_TOTALS, **{f"shadow_{key}": value for key, value in ownership_shadow.TOTALS.items()}}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"totals": totals, "drifts": DRIFTS, "commands": COMMANDS}, handle, indent=1, sort_keys=True, default=str)


# --- The relay seam: one fake wire, one clock, one opt-in offline installation ---


@pytest.fixture
def transport() -> StubTransport:
    """The recording relay wire; a test serves the routes it needs."""
    return StubTransport()


@pytest.fixture
def clock() -> Clock:
    """An injected clock: tests move time, nothing waits for it."""
    return Clock()


@pytest.fixture
def offline_relay(monkeypatch: pytest.MonkeyPatch, transport: StubTransport) -> StubTransport:
    """Point the whole installation's price repository at the stub wire, catalogue and days served.

    Opt in with `pytestmark = pytest.mark.usefixtures("offline_relay")` for a module whose tests
    set up real config entries; a test that needs a different answer overrides the routes.
    """
    from custom_components.spotnav import async_setup_price_repository as real_setup
    from custom_components.spotnav.pricing import price_repository as repository_module
    import custom_components.spotnav as integration

    monkeypatch.setattr(repository_module, "async_get_clientsession", lambda _hass: transport)
    monkeypatch.setattr(
        integration, "async_setup_price_repository", partial(real_setup, base_url=BASE_URL)
    )
    serve(transport)
    return transport


@pytest.fixture
def install_spy(monkeypatch: pytest.MonkeyPatch) -> list[ChargingPlan]:
    """Every real installation, through the controller's own public method.

    A plan that reached a charger must have come through here: this is the audit point for
    "every Auto charger command goes through `ChargingController`".
    """
    plans: list[ChargingPlan] = []
    original = ChargingController.async_install

    async def spy(self: ChargingController, plan: ChargingPlan) -> None:
        plans.append(plan)
        return await original(self, plan)

    monkeypatch.setattr(ChargingController, "async_install", spy)
    return plans


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> FakeScheduler:
    """The charging controller's window timers, recorded instead of waited for."""
    scheduler = FakeScheduler()
    monkeypatch.setattr(controller_module, "async_track_point_in_utc_time", scheduler)
    return scheduler


@pytest.fixture
async def session(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, timers: FakeScheduler
) -> Session:
    instance = Session(hass, transport, clock, timers)
    await instance.start()
    return instance


@pytest.fixture
async def harness(hass: HomeAssistant, transport: StubTransport, clock: Clock) -> Harness:
    instance = Harness(hass, transport, clock)
    await instance.start()
    return instance


@pytest.fixture
def no_execution(monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant) -> dict[str, Any]:
    """Close every route from this layer to a charger, and report what tried to use one.

    The controller methods are patched on the *class*: the preview holds no
    `ChargingController`, so the only honest statement is that no instance, however
    obtained, was asked to schedule, install, start, stop, cancel or follow anything. The
    services are mocked at the registry for the same reason — a call that then failed to
    reach a service would still have been made — and HA's point-in-time timer helper is
    recorded, since a preview that armed a timer would be executing something.
    """
    called: list[str] = []

    def forbidden(name: str) -> Callable[..., None]:
        def record(*_args: Any, **_kwargs: Any) -> None:
            called.append(f"ChargingController.{name}")
            raise AssertionError(f"Auto called ChargingController.{name}")

        return record

    for name in (
        "async_install",
        "async_start",
        "async_stop",
        "async_cancel",
        "async_follow_schedule",
        "async_enforce_target",
    ):
        if hasattr(ChargingController, name):
            monkeypatch.setattr(ChargingController, name, forbidden(name))

    services = {
        (domain, service): async_mock_service(hass, domain, service)
        for domain, service in (
            ("switch", "turn_on"),
            ("switch", "turn_off"),
            ("number", "set_value"),
            ("input_number", "set_value"),
            ("homeassistant", "turn_on"),
            ("homeassistant", "turn_off"),
        )
    }
    timers: list[tuple[Any, ...]] = []

    def record_timer(*args: Any, **_kwargs: Any) -> Callable[[], None]:
        timers.append(args)

        def cancel() -> None:
            return None

        return cancel

    monkeypatch.setattr(event_helper, "async_track_point_in_utc_time", record_timer)
    return {"called": called, "services": services, "timers": timers}


@pytest.fixture
def pause_appointments(monkeypatch: pytest.MonkeyPatch) -> RecordedAppointments:
    recorder = RecordedAppointments()
    monkeypatch.setattr(auto_execution_module, "async_track_point_in_time", recorder)
    return recorder


@pytest.fixture
def flaky(hass: HomeAssistant) -> FlakyStore:
    return FlakyStore()
