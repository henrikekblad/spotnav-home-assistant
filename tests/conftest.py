"""Fixtures for SpotNav charging control tests."""

from __future__ import annotations

import asyncio
import sys
import sysconfig
from pathlib import Path
from typing import Any, Callable

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import event as event_helper
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav import time_zones
from custom_components.spotnav.execution import auto_execution as auto_execution_module, controller as controller_module
from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan

from .relay import Clock, FakeScheduler, serve, StubProvider, StubRouter, StubTransport
from .harness import FlakyStore, Harness, RecordedAppointments, Session


pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make custom_components/spotnav discoverable during tests."""
    yield


@pytest.fixture(autouse=True)
def resolved_time_zones():
    """Every test starts with the same zones resolved, whatever ran before it on this worker: the ones
    the relay fixtures name, as a fetched or restored catalogue leaves them in a running installation.
    Resolved here, off the loop. `test_time_zones.py` starts from an empty cache instead."""
    time_zones.clear()
    for name in RESOLVED_ZONES:
        time_zones.zone(name)
    yield


#: Every zone the relay fixtures and the tests' own documents name.
RESOLVED_ZONES = (
    "Europe/Berlin",
    "Europe/Brussels",
    "Europe/Copenhagen",
    "Europe/Helsinki",
    "Europe/Lisbon",
    "Europe/London",
    "Europe/Madrid",
    "Europe/Oslo",
    "Europe/Paris",
    "Europe/Stockholm",
)


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
def no_blueprint_install(request, monkeypatch):
    """Setting the integration up writes no blueprints into the shared test configuration folder;
    `tests/test_blueprint_install.py` opts back in with the `blueprint_install` marker."""
    if request.node.get_closest_marker("blueprint_install") is not None:
        return

    async def _nothing(*_args: Any, **_kwargs: Any) -> list[str]:
        return []

    import custom_components.spotnav as integration

    monkeypatch.setattr(integration, "async_install_blueprints", _nothing)


@pytest.fixture
def no_price_entry_from_settings(monkeypatch):
    """A settings write that sets the area keeps the record the charger stores, as it did before price entries:
    no electricity price entry is made from it (`pricing/price_setup.py:async_price_entry_from_settings`). For the
    tests whose envelopes are the contract's fixtures, written before that."""

    async def _none(*_args: Any, **_kwargs: Any) -> None:
        return None

    from custom_components.spotnav.api import settings as settings_api

    monkeypatch.setattr(settings_api, "async_price_entry_from_settings", _none)


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


# --- Guard: SpotNav's own code does no file I/O and no sleep on the event loop ---
#
# Home Assistant's blocking-call detection (`block_async_io`) is not on in tests, and even on it skips `open` and the
# `Path` reads and writes when `unittest` is loaded. This guard wraps the cheap-to-patch blocking calls: `open`, the
# `Path` reads, writes, stats and listings, `os.listdir/scandir/walk/stat`, `os.path.exists/isfile/isdir/getsize`,
# `shutil`'s copies and `rmtree`, and `time.sleep`. A call is a violation when it is made on the thread running the
# event loop and the first frame above it outside Python's own library is SpotNav's (`custom_components/spotnav`):
# Home Assistant's own I/O and the tests' fixture reads are not SpotNav's to fix, and an executor job runs on a
# worker thread, so it is allowed. Imports are left to Home Assistant's own detector (an importlib frame ends the
# walk), and so are the source-line reads of asyncio's debug mode (a `linecache` or `traceback` frame ends it). It records instead of raising (a raise can be swallowed by the code under test) and fails the test at
# teardown. A test about a deliberate read on the loop opts out with the `allow_loop_io` marker.

_SPOTNAV_DIR = str(Path(__file__).resolve().parents[1] / "custom_components" / "spotnav")
#: Python's own library (in a virtual environment `platstdlib` holds the site-packages: not that one), and its frozen
#: modules. Without a virtual environment (as on CI) the site-packages lie inside it: those frames are not stdlib.
_STDLIB_DIRS = (sysconfig.get_paths()["stdlib"] + "/", "<frozen ")
#: Asyncio's debug mode (on in tests) reads source lines for the traceback of every task and handle it makes.
_NOISE = ("/linecache.py", "/traceback.py")


def _spotnav_caller() -> str | None:
    """The SpotNav file and line that made this blocking call, when SpotNav made it."""
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if "importlib" in name or name.endswith(_NOISE):
            return None
        if (name.startswith(_STDLIB_DIRS) and "/site-packages/" not in name) or name == __file__:
            frame = frame.f_back
            continue
        return f"{name}:{frame.f_lineno}" if name.startswith(_SPOTNAV_DIR) else None
    return None


def _on_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture(autouse=True)
def loop_io_guard(request, monkeypatch):
    import builtins
    import io
    import os
    import shutil
    import time

    if request.node.get_closest_marker("allow_loop_io") is not None:
        yield
        return
    violations: list[str] = []

    def guarded(label, real):
        def wrapper(*args, **kwargs):
            if _on_loop_thread():
                caller = _spotnav_caller()
                if caller is not None:
                    violations.append(f"{label} on the event loop from {caller}")
            return real(*args, **kwargs)

        wrapper.__wrapped__ = real
        return wrapper

    monkeypatch.setattr(builtins, "open", guarded("open", builtins.open))
    monkeypatch.setattr(io, "open", guarded("open", io.open))
    for name in (
        "open", "read_text", "read_bytes", "write_text", "write_bytes", "stat", "exists", "is_file", "is_dir",
        "iterdir", "glob", "rglob", "mkdir", "unlink", "rmdir", "rename", "replace", "touch",
    ):
        monkeypatch.setattr(Path, name, guarded(f"Path.{name}", getattr(Path, name)))
    for name in ("listdir", "scandir", "walk", "stat", "remove", "unlink", "replace", "rename", "makedirs"):
        monkeypatch.setattr(os, name, guarded(f"os.{name}", getattr(os, name)))
    for name in ("exists", "isfile", "isdir", "getsize"):
        monkeypatch.setattr(os.path, name, guarded(f"os.path.{name}", getattr(os.path, name)))
    for name in ("copy", "copy2", "copyfile", "copytree", "rmtree", "move"):
        monkeypatch.setattr(shutil, name, guarded(f"shutil.{name}", getattr(shutil, name)))
    monkeypatch.setattr(time, "sleep", guarded("time.sleep", time.sleep))
    yield violations
    assert not violations, "blocking calls on the event loop:\n" + "\n".join(dict.fromkeys(violations))


# --- The charge-ownership core: every decision it could not take fails the test ---


@pytest.fixture
def chargers_not_switch_only(monkeypatch):
    """For tests about the sun's state machine and its hand-overs, written before chargers that can only be switched
    on and off were reckoned at their whole current: their plain-switch chargers start and stop on the minimum-current
    thresholds as a charger whose current is set does, not on a switched current (`tests/test_switch_only_solar.py`
    covers those)."""
    monkeypatch.setattr(ChargingController, "is_switch_only", property(lambda self: False))


@pytest.fixture(autouse=True)
def core_never_fails(request):
    """Every test fails on a decision the charge-ownership core could not take (`execution/session_runner.failed`:
    the core raised, or its facts or its stored session could not be read) and on an error of its runner. A test
    about such a failure itself opts out with the `core_failure_expected` marker.

    `SPOTNAV_CORE_DUMP_EVENTS` set to part of a test's id writes the events every runner of that test recorded to
    `SPOTNAV_CORE_DUMP` (default: here), as a debug bundle carries them for `core/replay.py`."""
    from custom_components.spotnav.execution import session_runner

    seen: list[tuple[str, dict[str, Any]]] = []

    def listener(kind: str, record: dict[str, Any]) -> None:
        seen.append((kind, record))

    session_runner.LISTENERS.append(listener)
    try:
        yield seen
    finally:
        session_runner.LISTENERS.remove(listener)
        import os

        dump = os.environ.get("SPOTNAV_CORE_DUMP_EVENTS")
        if dump and dump in request.node.nodeid:
            import json

            records = [list(runner.events) for runner in session_runner.RUNNERS]
            name = request.node.nodeid.replace("/", "_").replace("::", "__")
            with open(os.path.join(os.environ.get("SPOTNAV_CORE_DUMP", "."), f"events_{name}.json"), "w") as handle:
                json.dump(records, handle, indent=1, default=str)
    if request.node.get_closest_marker("core_failure_expected") is not None:
        return
    failed = [{"kind": kind, **record} for kind, record in seen if kind in ("failure", "error")]
    if failed:
        import json

        pytest.fail(
            "the charge-ownership core could not decide:\n"
            + "\n".join(json.dumps(record, default=str)[:2000] for record in failed[:3]),
            pytrace=False,
        )


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
    """Give the whole installation's price repository the stub price source (`tests/relay.py`), days served.

    Every area with no real provider reads its day files from the stub wire. Opt in with
    `pytestmark = pytest.mark.usefixtures("offline_relay")` for a module whose tests set up real config entries; a
    test that needs a different answer overrides the routes.
    """
    from custom_components.spotnav.pricing import price_repository as repository_module

    stub = StubProvider(transport)
    monkeypatch.setattr(
        repository_module, "ProviderRouter", lambda *args, **kwargs: StubRouter(stub, *args, **kwargs)
    )
    serve(transport)
    return transport


@pytest.fixture
def no_bundled_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """An installation whose bundled area list could not be read: no area is listed."""
    from custom_components.spotnav.pricing import price_repository as repository_module

    async def absent(_hass):
        return None

    monkeypatch.setattr(repository_module, "async_load_bundled", absent)


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


@pytest.fixture(params=["unload", "ha_stop"])
def both_restarts(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Runs a test whose world restarts (`pause_world.World.restart`) once after an unload and once as Home
    Assistant's own stop, which unloads no entry (`World.ha_restart`)."""
    from .pause_world import World

    if request.param == "ha_stop":
        monkeypatch.setattr(World, "restart", World.ha_restart)
    return request.param
