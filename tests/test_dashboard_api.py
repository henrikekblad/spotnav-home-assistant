"""The read-only WebSocket dashboard contract (API v1).

Almost every test here calls the *pure* serializers directly, because the contract is a value:
that is what lets the shape be pinned exactly, the geometry be asserted to the minute, and the
privacy audit be recursive without a socket in the way. Four tests do go through Home
Assistant's own authenticated WebSocket, because "registered, versioned and authenticated" is
only true if the real handshake says so.

**The clock is part of every fixture here.** A test that reads live state -- a plan, a proposal,
a price state -- freezes `NOW` (or `LATE`, when it needs the price gap on purpose) and
asserts the prerequisite it depends on before it tests its subject, so it cannot pass on a run in
which the served fixture days happened to be yesterday, or in which the evening left no plan to
calculate. The handful of tests that speak to Home Assistant's own socket cannot freeze the clock
(see the note on `NOW`), so they assert only what the transport proves, and the pure tests build
their own documents and instants.
"""

from __future__ import annotations

import itertools
import json
import math
import os
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.planning import auto_controller, auto_settings
from custom_components.spotnav.execution import auto_execution
from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.auto_settings import AreaAutoSettings
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.api.settings import SETTINGS_RESPONSE_KEYS
from custom_components.spotnav.api.dashboard import (
    DASHBOARD_API_VERSION,
    MAX_RESPONSE_BYTES,
    serialize_dashboard,
)
from custom_components.spotnav.api.common import (
    ERROR_CHARGER_REQUIRED,
    ERROR_CHARGER_UNLOADED,
    ERROR_SITE_NOT_CHARGER,
    ERROR_UNKNOWN_CHARGER,
    ERROR_UNSUPPORTED_VERSION,
)
from custom_components.spotnav.planning.planner import (
    FiscalChoice,
    PlanRequest,
    calculate_plan,
    chart_intervals,
)

from .helpers import make_site_entry
from .relay import DE_LU, SE4, flat_day, serve, serve_index
from .relay import StubTransport
from .relay import TODAY, TOMORROW, YESTERDAY, fixture
from .world import go_auto, setup_charger, ws_call
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import preview_for
from custom_components.spotnav.util import finite_number
from custom_components.spotnav.util import aware_iso

pytestmark = pytest.mark.usefixtures("offline_relay")

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "dashboard"

#: The instant this module's live-state tests run at -- the same instant both golden fixtures
#: were captured at. It is inside the two served fixture days (2026-09-22/23), and early enough
#: that the published prices cover the whole planning horizon, so the plan really is installed
#: and the proposal really is fully published. `freeze_time` reads a naive string as UTC, so this
#: is 08:00 in Stockholm.
NOW = "2026-09-22 06:00:00"

#: Late evening in the same day, for the tests that need the price gap on purpose: nothing
#: published is left ahead of `now`, so the price state is the waiting/stale family rather than a
#: full day.
LATE = "2026-09-22 20:30:00"

#: Why the socket tests below carry no freeze: Home Assistant mints the access token the
#: `hass_ws_client` fixture uses while setting the instance up, at the real clock, so a frozen
#: clock makes it "not yet valid" (`auth_invalid`), and a frozen clock cannot fire the WebSocket
#: heartbeat at teardown. Those tests therefore assert only what the transport proves -- the
#: handshake, the codes, the exact payload shape -- and leave proposal and installed semantics
#: to the frozen tests that own them.

#: The fixture catalogue's other market with a literal zero suggestion for the grid fee.
NO1 = "NO1"

#: The dashboard's sections, exactly and in order: this list *is* the contract's top level.
DASHBOARD_SECTIONS = [
    "api_version",
    "generated_at",
    "charger",
    "settings",
    "fiscal",
    "planning",
    "market",
    "prices",
    "plan",
    "live",
    "strategy",
    "strategy_options",
    "strategy_state",
    "control",
    "charge_progress",
    "site",
    "current_range",
    "soc",
    "vehicles",
    "target_vehicle_id",
    "charging_phases",
    "detected_phases",
    "phase_detection",
    "chargers",
    "status",
    "summary",
    "sessions_summary",
    "connection",
    "starting_up",
    "charger_priority",
    "identification",
]

#: One chart row, exactly.
INTERVAL_KEYS = {
    "start",
    "end",
    "day",
    "duration_minutes",
    "raw_price",
    "effective_price",
    "proposal_planned",
    "installed_planned",
}

#: Home Assistant refuses a reused or non-integer request id, so every call gets a fresh one.
_REQUEST_IDS = itertools.count(1)


def response_for(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """One dashboard response, through the contract's own capture and serializer."""
    return serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)


#: Leaf keys a response may never carry. A capability *name* is public and documented
#: (`current_limit` among them) and lives under `$.charger.capabilities.*`; a private key is not,
#: wherever it sits.
PRIVATE_KEYS = frozenset(
    {
        "webhook",
        "webhook_id",
        "webhook_url",
        "owner",
        "owner_id",
        "pairing_uri",
        "token",
        "access_token",
        "entity_id",
        "charge_control",
        "current_limit",
        "exceptions",
        "traceback",
    }
)

#: Fragments a *string value* may never contain: an entity id, a webhook id or url, a bare url.
PRIVATE_FRAGMENTS = ("switch.", "sensor.", "number.", "webhook-", "http://", "https://", "/api/webhook")


def assert_no_private_identifiers(payload: dict[str, Any]) -> None:
    """The recursive, path-based audit: leaf keys first, then string values.

    The key is derived from the *path* rather than from a name in the payload, so a private key is
    caught wherever it is nested, and the documented capability-name exception is the one hole in
    it -- precisely because a capability name is public. Every string is checked in either case.
    """
    for path, value in walk(payload):
        key = path.rsplit(".", 1)[-1]
        if not path.startswith("$.charger.capabilities."):
            assert key not in PRIVATE_KEYS, path
        if isinstance(value, str):
            assert not any(fragment in value for fragment in PRIVATE_FRAGMENTS), path


def walk(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    """Every leaf of a response, with its path, for the audits below."""
    leaves: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            leaves.extend(walk(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            leaves.extend(walk(item, f"{path}[{index}]"))
    else:
        leaves.append((path, value))
    return leaves

# ---------------------------------------------------------- registration and requests


def document(day: str, *, resolution: int, prices: list[float], area: str = SE4, tz: str = "Europe/Stockholm"):
    """One hand-built day document, for geometry assertions with no socket involved."""
    from datetime import date as _date

    from homeassistant.util import dt as dt_util

    from custom_components.spotnav.pricing.relay_contract import PriceDocument, PriceInterval

    zone = dt_util.get_time_zone(tz)
    parsed = _date.fromisoformat(day)
    start = datetime(parsed.year, parsed.month, parsed.day, tzinfo=zone)
    # Instants, not wall-clock arithmetic: a spring day really is 23 hours of intervals and an
    # autumn day 25, which only holds if each row is built by instant and *then* read locally.
    first = start.astimezone(dt_util.UTC)
    intervals = tuple(
        PriceInterval.from_instants(
            first + timedelta(minutes=resolution * index),
            first + timedelta(minutes=resolution * (index + 1)),
            zone=zone,
            eur_per_kwh=price,
        )
        for index, price in enumerate(prices)
    )
    return PriceDocument(
        version=1,
        area_id=area,
        day=parsed,
        tz=tz,
        start=start,
        start_instant=start.astimezone(dt_util.UTC),
        resolution_minutes=resolution,
        unit="EUR/kWh",
        prices=tuple(prices),
        intervals=intervals,
        fx={},
        fx_date=None,
        fx_src=None,
        src="test",
        published=start,
        retrieved=start,
    )


def test_interval_geometry_is_the_published_geometry() -> None:
    """15- and 60-minute days, and both 23- and 25-hour days, exactly as published.

    The rows are the relay's own intervals on the area's clock: a spring day really is 23
    hours of rows and an autumn day 25, and a 60-minute day is 24 hourly rows rather than 96
    quarter-hours somebody re-gridded. Nothing here rounds a boundary.
    """
    quarter = document("2026-03-29", resolution=15, prices=[0.1] * 92)  # 23-hour day
    hourly = document("2026-09-22", resolution=60, prices=[0.2] * 24)
    autumn = document("2026-10-25", resolution=15, prices=[0.3] * 100)  # 25-hour day
    fiscal = FiscalChoice(vat_enabled=True, vat_percent=25.0)

    rows = chart_intervals((quarter,), currency="EUR", fiscal=fiscal)
    assert len(rows) == 92
    assert all((row.utc_end - row.utc_start) == timedelta(minutes=15) for row in rows)
    assert rows[0].start.utcoffset() == timedelta(hours=1)  # before the change
    assert rows[-1].start.utcoffset() == timedelta(hours=2)  # after it
    assert rows[-1].end.date() == rows[-1].start.date() + timedelta(days=1)

    hourly_rows = chart_intervals((hourly,), currency="EUR", fiscal=fiscal)
    assert len(hourly_rows) == 24
    assert all((row.utc_end - row.utc_start) == timedelta(hours=1) for row in hourly_rows)
    assert [row.day for row in hourly_rows] == [hourly.day] * 24

    autumn_rows = chart_intervals((autumn,), currency="EUR", fiscal=fiscal)
    assert len(autumn_rows) == 100
    assert autumn_rows[-1].utc_end - autumn_rows[0].utc_start == timedelta(hours=25)

    # Both prices are in the area's minor unit, and the effective one carries the fiscal rule.
    assert rows[0].raw_minor_per_kwh == pytest.approx(10.0)
    assert rows[0].effective_minor_per_kwh == pytest.approx(12.5)
    assert hourly_rows[0].raw_minor_per_kwh == pytest.approx(20.0)


def test_the_held_documents_are_the_capture_instants_own_local_days() -> None:
    """What is held is what the *capture instant* calls today and tomorrow, in the area's zone.

    The repository keys a document by its own local date, so a capture after local midnight asks for
    the new date first, and a fresh read therefore relabels the chart with no settings edit, no slider
    movement and no action. The dates a response carries are a fact about the capture instant -- which
    is why the card must not read a date's *role* out of the order the rows arrived in.
    """

    # The one document the repository holds: fetched on the 22nd as "tomorrow", held under its own date.
    the_23rd = object()

    class Snap:
        def __init__(self, day: date, document: object | None) -> None:
            self.day = day
            self.document = document

    class Repo:
        def __init__(self) -> None:
            self.asked: list[date] = []
            self.held: dict[date, object] = {date(2026, 9, 23): the_23rd}

        def day_snapshot(self, area_id: str, day: date) -> Snap:
            assert area_id == "SE4"
            self.asked.append(day)
            return Snap(day, self.held.get(day))

    repository = Repo()
    documents, days = dashboard_api._held_documents(
        repository, "SE4", "Europe/Stockholm", datetime(2026, 9, 22, 22, 11, tzinfo=timezone.utc)
    )

    # 22:11Z is 00:11 local on the 23rd: the capture asks for the 23rd and the 24th, in that order.
    assert repository.asked == [date(2026, 9, 23), date(2026, 9, 24)]
    assert [snapshot.day for snapshot in days] == [date(2026, 9, 23), date(2026, 9, 24)]
    assert len(documents) == 1, "the 23rd is held; the 24th has published nothing yet"

    # Eleven minutes before midnight the same record is asked for the old date and the new one, and the
    # very same held document is then the *second* date's row source. One document, two roles, decided
    # by the capture instant -- which is why a client may not read a date's role out of the row order.
    before = Repo()
    before_documents, held_before = dashboard_api._held_documents(
        before, "SE4", "Europe/Stockholm", datetime(2026, 9, 22, 21, 50, tzinfo=timezone.utc)
    )
    assert before.asked == [date(2026, 9, 22), date(2026, 9, 23)]
    assert [snapshot.day for snapshot in held_before] == [date(2026, 9, 22), date(2026, 9, 23)]
    assert before_documents == documents, "the same held document, in the other role"



def test_planned_flags_follow_the_captured_plan_at_boundaries_and_across_midnight() -> None:
    """`planned` is read off the plan's periods, half-open, by instant."""
    first = document("2026-09-22", resolution=60, prices=[0.2] * 24)
    second = document("2026-09-23", resolution=60, prices=[0.2] * 24)
    rows = chart_intervals((first, second), currency="EUR", fiscal=FiscalChoice())
    assert len(rows) == 48

    capture = dashboard_api.CapturedDashboard(
        generated_at=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
        charger=dashboard_api.CapturedCharger("entry_a", "A", True, ()),
        settings=None,
        snapshot=None,
        catalogue=None,
        area_entry=None,
        area=None,
        days=(),
        # One period that ends exactly on a row boundary, and one that runs across local
        # midnight into the next day's first row.
        plan=_Plan(periods=((rows[10].utc_start, rows[11].utc_start), (rows[22].utc_start, rows[25].utc_start))),
        live=dashboard_api.CapturedLive(False, True, None, None, None),
        execution=dashboard_api.CapturedExecution("scheduled", None, None, None, None, False),
        pause_choices=(),
        intervals=rows,
        site=None,
    )

    rows = dashboard_api.serialize_intervals(capture)
    planned = [row["installed_planned"] for row in rows]
    assert all(row["proposal_planned"] is False for row in rows), "no proposal here"

    assert planned[10] is True, "the period starts here"
    assert planned[11] is False, "a period that ends on a boundary does not plan the next row"
    assert planned[22] is True and planned[24] is True, "across local midnight, by instant"
    assert planned[25] is False
    assert planned.count(True) == 4, "two hours and three hours, and nothing else"


class _Plan:
    """The smallest thing the serializer reads off a plan, for these two pure tests."""

    amps = 10
    phases = 1
    power_kw = 2.3
    origin = "auto_price"
    auto_identity = None
    energy_kwh = None

    def __init__(self, periods=()) -> None:
        self.periods = periods
        self.windows = periods

# ------------------------------------------------------- the real authenticated socket


# The server side of a refused socket keeps aiohttp's heartbeat timer until it is collected; it is
# Home Assistant's websocket server, not this integration, and core's own tests allow it the same way.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_an_unauthenticated_connection_never_reaches_the_commands(
    hass: HomeAssistant, aiohttp_client, socket_enabled: None
) -> None:
    """No token, no command: the gate is Home Assistant's own authentication.

    A raw connection to the very endpoint the commands are served from: the server asks for
    auth, an invalid token is refused as `auth_invalid`, and no message of this integration is
    ever delivered. That is exactly why these commands need no access control of their own --
    and why the source audit beside this test checks that they do not try to invent one.
    """
    from homeassistant.components.websocket_api.const import URL as WS_URL
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "websocket_api", {})
    client = await aiohttp_client(hass.http.app)
    socket = await client.ws_connect(WS_URL)
    try:
        assert (await socket.receive_json())["type"] == "auth_required"
        await socket.send_json({"type": "auth", "access_token": "definitely-not-a-token"})
        assert (await socket.receive_json())["type"] == "auth_invalid"
    finally:
        await socket.close()
        # Closing the client lets the server finish its side of the socket; until then aiohttp
        # still holds the server-side heartbeat timer.
        await client.close()
        await hass.async_block_till_done()


async def test_the_commands_are_websocket_only_and_registered_once(
    hass: HomeAssistant, hass_ws_client
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    await hass_ws_client(hass)
    """The contract registers exactly its two WebSocket commands, and no HTTP door."""
    commands = {name for name in hass.data["websocket_api"] if name.startswith("spotnav/")}
    assert {"spotnav/get_dashboard", "spotnav/list_chargers"} <= commands
    # Registering more than once (two setup paths, or a test) changes nothing.
    dashboard_api.async_setup_dashboard_api(hass)
    dashboard_api.async_setup_dashboard_api(hass)


async def test_list_chargers_over_the_real_socket_is_ordered_and_never_lists_a_site(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """Two chargers, a site, and the one documented order."""
    await setup_charger(hass, entry_id="entry_a", title="Zebra", charge_control="switch.charger_a")
    await setup_charger(
        hass,
        entry_id="entry_b",
        title="alpha",
        webhook_id="webhook-b",
        charge_control="switch.charger_b",
    )
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=["entry_a"])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    reply = await ws_call(client, {"type": "spotnav/list_chargers", "api_version": 1})

    assert reply["success"] is True
    payload = reply["result"]
    assert payload["api_version"] == DASHBOARD_API_VERSION
    assert [item["charger_name"] for item in payload["chargers"]] == ["alpha", "Zebra"]
    assert [item["charger_id"] for item in payload["chargers"]] == ["entry_b", "entry_a"]
    assert [item["available"] for item in payload["chargers"]] == [True, True]
    for item in payload["chargers"]:
        assert set(item) == {"charger_id", "charger_name", "available", "capabilities"}
        assert list(item["capabilities"]) == list(dashboard_api.CAPABILITY_KEYS)
    # The site is a site: it is not in the list, and its id is refused as a charger.
    assert "site_a" not in [item["charger_id"] for item in payload["chargers"]]
    reply = await ws_call(
        client, {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": "site_a"}
    )
    assert reply["success"] is False and reply["error"]["code"] == ERROR_SITE_NOT_CHARGER


async def test_versions_and_ids_are_refused_by_stable_code(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """One code per refusal, and no answer that leaks what an unrelated id is."""
    entry = await setup_charger(hass)
    client = await hass_ws_client(hass)

    for version in (None, 2, 3, 7, 0, True, "1"):
        payload = {"type": "spotnav/get_dashboard", "charger_id": entry.entry_id}
        if version is not None:
            payload["api_version"] = version
        reply = await ws_call(client, payload)
        assert reply["success"] is False, version
        assert reply["error"]["code"] == ERROR_UNSUPPORTED_VERSION, version

    # The one version answers, and says so.
    reply = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )
    assert reply["success"] is True
    assert reply["result"]["api_version"] == 1
    for section in ("control", "charge_progress", "strategy_state", "site", "fiscal", "chargers"):
        assert section in reply["result"], section
    assert "load_balancing" not in reply["result"], "the root load_balancing block is retired"

    reply = await ws_call(client, {"type": "spotnav/get_dashboard", "api_version": 1})
    assert reply["error"]["code"] == ERROR_CHARGER_REQUIRED

    for charger_id in ("entry_does_not_exist", "some_other_integration_entry"):
        reply = await ws_call(
            client,
            {
                "type": "spotnav/get_dashboard",
                "api_version": 1,
                "charger_id": charger_id,
            },
        )
        assert reply["error"]["code"] == ERROR_UNKNOWN_CHARGER, charger_id
        assert "integration" not in reply["error"]["message"], "no leak about other domains"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    reply = await ws_call(
        client,
        {
            "type": "spotnav/get_dashboard",
            "api_version": 1,
            "charger_id": entry.entry_id,
        },
    )
    assert reply["error"]["code"] == ERROR_CHARGER_UNLOADED

# ------------------------------------------------------------- the response contract


def normalized(response: dict[str, Any]) -> dict[str, Any]:
    """A response with its bounded identities replaced, for golden comparison and key checks.

    The clock is frozen in every test that writes a golden, so instants are the same on every run and
    stay real; only the identities (content hashes) are replaced with the placeholder every other
    contract fixture uses.
    """
    identities = {"identity", "applied_identity", "pending_identity", "price_identity"}

    def scrub(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {name: scrub(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if key in identities and isinstance(value, str):
            return "<id>"
        return value

    return scrub(response)


@freeze_time(NOW)
async def test_one_response_is_one_observation_while_settings_are_written(
    hass: HomeAssistant,
) -> None:
    """A write that lands after the capture cannot change what the response says."""
    entry = await setup_charger(hass)
    serve(transport_of(hass), flat=True)
    await go_auto(hass)
    captured = dashboard_api.capture_dashboard(hass, entry)

    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    await preview.async_apply_settings(mutate=lambda settings: replace(settings, amps=16))
    await hass.async_block_till_done()

    response = serialize_dashboard(captured, can_act=True)

    assert response["settings"]["amps"] == 10, "the captured observation, not a later one"
    settings_now = response["settings"]
    assert settings_now["revision"] == response["planning"]["settings_revision"], (
        "one revision per response"
    )
    assert settings_now["amps"] != 16


def dt_utcnow() -> datetime:
    """Home Assistant's own clock, which is the *frozen* clock inside a frozen test.

    Used to build instants relative to the test's own instant instead of wall-clock literals.
    """
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow()


def transport_of(hass: HomeAssistant) -> StubTransport:
    """The stub wire this installation is pointed at, for a test that needs to serve more."""
    repository = domain_data(hass).price_repository
    assert repository is not None
    return repository._client()


@freeze_time(NOW)
async def test_the_commands_read_and_command_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fetch, no service call, no charger command, no settings write, no schedule."""
    entry = await setup_charger(hass)
    await go_auto(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    revision = store.settings(entry.entry_id).revision
    transport = transport_of(hass)
    calls = len(transport.calls)
    services = [
        async_mock_service(hass, "switch", "turn_on"),
        async_mock_service(hass, "switch", "turn_off"),
        async_mock_service(hass, "number", "set_value"),
        async_mock_service(hass, "button", "press"),
        async_mock_service(hass, "ocpp", "configure"),
    ]
    installs = Counted(monkeypatch, ChargingController, "async_install")
    stops = Counted(monkeypatch, ChargingController, "async_stop")

    response = response_for(hass, entry)

    assert response["charger"]["charger_id"] == entry.entry_id
    assert len(transport.calls) == calls, "no request to the relay"
    assert all(calls_made == [] for calls_made in services), "no Home Assistant service called"
    assert installs.calls == 0 and stops.calls == 0, "no schedule touched"
    assert store.settings(entry.entry_id).revision == revision, "no settings write"
    assert entry.state.name == "LOADED"


class Counted:
    """A method, counted, with the original still called."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
        self.calls = 0
        original = getattr(owner, name)

        async def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)


@freeze_time(NOW)
async def test_ordinary_entity_attributes_stay_compact(hass: HomeAssistant) -> None:
    """Only the dashboard contract carries chart points; entities stay small."""
    from homeassistant.helpers import entity_registry as er

    entry = await setup_charger(hass)
    await go_auto(hass)
    registry = er.async_get(hass)
    checked = 0
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(entity.entity_id)
        if state is None:
            continue
        checked += 1
        for key, value in state.attributes.items():
            assert key not in ("intervals", "prices", "price_series"), entity.entity_id
            if isinstance(value, list):
                assert len(value) <= 8, (entity.entity_id, key)
    assert checked >= 20


@freeze_time(NOW)
async def test_the_proposal_horizon_response_stays_inside_the_documented_bound(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The proposal-horizon response is measured, not assumed: both full held days, 192 rows.

    The held rows are the base and the horizon is only overlaid (see `serialize_intervals`), so a
    proposal spanning one 24-hour window inside two fully published days changes *which facts* each
    row carries, never how many rows there are: the response is the union of both held days.

    This is the common case; the worst case (a 100-slot autumn day plus a complete day, no
    proposal) is `test_the_largest_response_is_two_held_days_with_no_proposal`. The row count is
    asserted exactly, so a chart truncated to the proposal's horizon cannot pass.
    """
    entry = await setup_charger(hass)
    await go_auto(hass)
    response = response_for(hass, entry)
    size = len(json.dumps(response, default=str).encode("utf-8"))
    rows = len(response["prices"]["intervals"])

    assert response["plan"]["proposal"] is not None, "the prerequisite: a usable proposal"
    assert rows == 192, "both held days, in full -- the proposal overlays them, it does not replace them"
    assert size < MAX_RESPONSE_BYTES, (size, MAX_RESPONSE_BYTES)
    print(f"--- proposal-horizon response: {rows} rows, {size} bytes")


@freeze_time(NOW)
async def test_serialization_cannot_emit_nan_infinity_or_a_naive_timestamp(
    hass: HomeAssistant,
) -> None:
    """Every number is finite, and every timestamp string carries an offset."""
    assert finite_number(float("nan")) is None
    assert finite_number(float("inf")) is None
    assert finite_number(-float("inf")) is None
    assert aware_iso(datetime(2026, 9, 22, 12, 0)) is None
    assert aware_iso(datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)) is not None

    entry = await setup_charger(hass)
    await go_auto(hass)
    response = response_for(hass, entry)
    for path, value in walk(response):
        if isinstance(value, float):
            assert math.isfinite(value), path
        if not isinstance(value, str) or "T" not in value:
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue  # not a timestamp, but a bounded identity that embeds one
        assert parsed.tzinfo is not None, path


@freeze_time(NOW)
async def test_fiscal_states_stay_distinct_in_the_response(hass: HomeAssistant) -> None:
    """off, suggested, manual, a literal zero and a missing suggestion are five readings."""
    entry = await setup_charger(hass)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    await preview.async_apply_settings(
        mutate=lambda settings: replace(settings, area_id=SE4, amps=10, phases=1)
    )
    await preview.async_apply_settings(
        mutate=lambda settings: settings.with_override(
            replace(
                settings.override_for(SE4),
                vat=replace(settings.override_for(SE4).vat, enabled=True, value=8.5),
                tax=replace(settings.override_for(SE4).tax, enabled=True, value=None),
                transfer=replace(settings.override_for(SE4).transfer, enabled=False, value=4.0),
            )
        )
    )
    await hass.async_block_till_done()

    fiscal = response_for(hass, entry)["fiscal"]

    assert (fiscal["vat"]["policy"], fiscal["vat"]["value_source"]) == ("manual", "manual")
    assert fiscal["vat"]["effective_value"] == 8.5 and fiscal["vat"]["unit"] == "%"
    assert (fiscal["tax"]["policy"], fiscal["tax"]["value_source"]) == ("suggested", "suggested")
    assert fiscal["tax"]["effective_value"] == 36.0, "the area's own suggestion"
    assert fiscal["tax"]["explicit_value"] is None
    assert (fiscal["transfer"]["policy"], fiscal["transfer"]["value_source"]) == ("off", "off")
    assert fiscal["transfer"]["explicit_value"] == 4.0, "retained"
    assert fiscal["transfer"]["effective_value"] is None, "and not applied"
    assert fiscal["transfer"]["unit"].endswith("/kWh")

    # A literal zero suggestion, and a market that publishes none at all.
    for area, expected in ((NO1, ("suggested", 0.0)), (DE_LU, ("suggestion_unavailable", None))):
        await preview.async_apply_settings(
            mutate=lambda settings, area=area: replace(settings, area_id=area)
        )
        await preview.async_apply_settings(
            mutate=lambda settings, area=area: settings.with_override(
                replace(
                    settings.override_for(area),
                    transfer=replace(
                        settings.override_for(area).transfer, enabled=True, value=None
                    ),
                )
            )
        )
        await hass.async_block_till_done()
        transfer = response_for(hass, entry)["fiscal"]["transfer"]
        assert (transfer["value_source"], transfer["effective_value"]) == expected, area
        assert transfer["explicit_value"] is None

@freeze_time(NOW)
async def test_no_secret_or_private_identifier_reaches_a_response(
    hass: HomeAssistant,
) -> None:
    """A recursive audit of a live response *and* of the golden fixture.

    Both, and deliberately: the live one is what a browser receives today, and the fixture is
    what a card author copies from, so an id that leaked into either would leak into every
    consumer.
    """
    entry = await setup_charger(hass, current_limit="number.charger_a_limit")
    await go_auto(hass)
    live = response_for(hass, entry)
    fixture = json.loads((GOLDEN / "proposal_ready.json").read_text(encoding="utf-8"))
    for response in (live, fixture):
        assert_no_private_identifiers(response)


@freeze_time(NOW)
async def test_a_capture_taken_before_teardown_serializes_and_touches_nothing(
    hass: HomeAssistant,
) -> None:
    """Teardown cannot make an already-captured observation act: it is a value.

    The prerequisite is stated, not assumed: at this instant the Auto plan really is installed,
    so `live.schedule_active` is `True` *before* the entry goes away, and the serialized value is
    compared with itself after the unload. Without both halves this test would pass on a run in
    which nothing was ever installed, which is exactly what it did in the evening.
    """
    entry = await setup_charger(hass)
    await go_auto(hass)
    captured = dashboard_api.capture_dashboard(hass, entry)
    before = serialize_dashboard(captured, can_act=True)

    assert before["plan"]["installed"] is not None, "a plan really is installed here"
    assert before["live"]["schedule_active"] is True, "and the charger is running it"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    response = serialize_dashboard(captured, can_act=True)
    assert response["charger"]["charger_id"] == entry.entry_id
    assert response == before, "the captured observation, unchanged"
    assert response["live"]["schedule_active"] is True
    # And the command itself now refuses, rather than answering from a gone charger.
    failure = dashboard_api.resolve_charger_request(hass, {"charger_id": entry.entry_id})
    assert isinstance(failure, dashboard_api.DashboardFailure)
    assert failure.code == ERROR_CHARGER_UNLOADED


@freeze_time(NOW)
async def test_the_golden_fixture_is_the_shape_this_contract_promises(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The checked-in v1 fixture: exact sections, exact keys, normalized volatile leaves."""
    entry = await setup_charger(hass, current_limit="number.charger_a_limit")
    hass.states.async_set("number.charger_a_limit", "16", {"min": 6, "max": 16})
    serve(transport, rising=True)
    await go_auto(hass)
    response = normalized(response_for(hass, entry))
    # Set `SPOTNAV_WRITE_FIXTURES=1` to (re)write these two fixtures; the default run only compares. A geometry change is a reviewed, written change -- never a silent
    # drift -- and the diff it produces is the shape the card promises. Both fixtures are written by one
    # such run: the second describes a state this same test has to reach first.
    write = os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1"
    ready_path = GOLDEN / "proposal_ready.json"
    pending_path = GOLDEN / "pending_beside_installed.json"
    if write:
        ready_path.write_text(json.dumps(response, indent=2) + "\n", encoding="utf-8")
    else:
        fixture = json.loads(ready_path.read_text(encoding="utf-8"))
        assert set(response) == set(fixture), "the top-level sections are the contract"
        assert list(response) == list(fixture), "and in the documented order"
        assert response == fixture, "a shape change is a reviewed, written change"

    # The second fixture: an installed plan with a newer proposal waiting beside it; each must
    # carry its own generation's facts.
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    await preview.async_apply_settings(
        mutate=lambda settings: replace(settings, requested_kwh=settings.requested_kwh + 12)
    )
    await hass.async_block_till_done()
    raw = response_for(hass, entry)
    pending = normalized(raw)
    if write:
        pending_path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
        return
    pending_fixture = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending == pending_fixture, "the second fixture marks this shape too"
    assert raw["plan"]["proposal"]["identity"] != raw["plan"]["installed"]["identity"]
    assert raw["plan"]["relation"]["pending_identity"] == raw["plan"]["proposal"]["identity"]
    assert raw["plan"]["relation"]["applied_identity"] == raw["plan"]["installed"]["identity"]
    assert raw["plan"]["relation"]["applied"] is False
    assert response["charger"]["capabilities"]["current_limit"] is True
    assert response["charger"]["capabilities"]["target_soc"] is False
    assert response["strategy_options"] == ["cheapest"]
    # Both held days, in full, at quarter-hour resolution -- 96 + 96 -- with the 24-hour proposal
    # horizon overlaid on top of them rather than replacing them (see `serialize_intervals`).
    assert len(response["prices"]["intervals"]) == 192, "both held days, exactly"
    assert response["planning"]["state"] == "proposal_ready"
    assert response["planning"]["reason"] == "ready"
    assert response["plan"]["proposal"] is not None
    assert response["plan"]["installed"] is not None

@freeze_time(NOW)
async def test_two_chargers_are_isolated_in_their_own_responses(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """One charger with a plan and one with no settings: two dashboards, two honest answers."""
    entry_a = await setup_charger(hass, entry_id="entry_a", charge_control="switch.charger_a")
    entry_b = await setup_charger(
        hass, entry_id="entry_b", charge_control="switch.charger_b", webhook_id="webhook-b"
    )
    await go_auto(hass, "entry_a")

    first = response_for(hass, entry_a)
    second = response_for(hass, entry_b)

    assert first["settings"]["strategy"] == "cheapest"
    assert second["settings"]["strategy"] == "cheapest"
    assert first["plan"]["proposal"]["periods"] and first["plan"]["installed"]["periods"]
    assert second["plan"]["proposal"] is None and second["plan"]["installed"] is None
    assert first["planning"]["state"] == "proposal_ready", "the same clock, the same answer"
    assert second["planning"]["state"] == "incomplete_settings"
    assert first["charger"]["charger_id"] != second["charger"]["charger_id"]
    # The unconfigured charger is not subscribed to a price area, and says so rather than guessing.
    assert second["prices"]["state"] is None
    assert second["prices"]["intervals"] == []


def test_serializers_cannot_consult_live_state() -> None:
    """The coherence guard, pinned structurally: serializers take a capture, and no `hass`.

    This is the guard behind the capture test above. It is structural rather than removable:
    a serializer that wanted to re-read live state would have to be handed `hass` (or a
    controller) and there is no such parameter anywhere in this contract, which is why "one
    response is one observation" holds even while a write is in flight. A future change that
    threaded a live source into a serializer fails here first.
    """
    import inspect

    # The capture may (and must) take `hass`; the serializers may not. `can_act` is allowed because
    # it is a fact about the *caller* rather than live state: it is read once from the connection
    # before serialization and cannot change what the capture observed.
    serializers = [name for name in dir(dashboard_api) if name.startswith("serialize_")]
    assert serializers
    for name in serializers:
        function = getattr(dashboard_api, name)
        parameters = inspect.signature(function).parameters
        assert set(parameters) <= {"capture", "charger", "chargers", "settings", "area_entry", "site", "component", "suggestion", "unit", "intervals", "can_act", "active_control_writable", "soc", "vehicle", "summary", "phases", "connection", "state", "priority"}, (name, list(parameters))
        assert "hass" not in parameters, name

# ------------------------------------------------- the states the contract must distinguish


@freeze_time(NOW)
async def test_a_proposal_that_was_never_installed_stands_beside_no_schedule(hass: HomeAssistant) -> None:
    """Paused execution: the proposal is real, nothing is installed, and nothing is borrowed.

    Both halves are stated: the proposal is asserted to exist (with the installed schedule it
    replaces) *before* the pause, and the pause is shown to remove the schedule without touching
    the proposal -- same identity, same periods. A run in which no proposal was ever calculated
    now fails at the prerequisite instead of passing vacuously.
    """
    entry = await setup_charger(hass)
    await go_auto(hass)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None

    before = response_for(hass, entry)["plan"]
    assert before["proposal"] is not None, "a proposal really exists here"
    assert before["proposal"]["periods"], "with periods"
    assert before["installed"] is not None, "and an installed schedule to remove"

    await preview.async_pause()
    await hass.async_block_till_done()

    plan = response_for(hass, entry)["plan"]

    assert plan["proposal"] is not None, "the pause installed nothing and removed no proposal"
    assert plan["proposal"]["periods"] == before["proposal"]["periods"], "the same schedule"
    # The identity is deliberately *not* compared: it is a bound value over the inputs, and the
    # pause itself is a settings write (execution off), so its revision-derived half moves while
    # the schedule it describes does not. The periods above are the claim; the identity is the
    # contract saying which inputs it was calculated from.
    assert plan["installed"] is None, "nothing is installed"
    assert plan["relation"]["applied"] is False
    assert plan["relation"]["applied_identity"] is None
    assert plan["delivered_kwh"] is None and plan["remaining_kwh"] is None


@freeze_time(NOW)
async def test_an_older_installed_plan_and_a_newer_pending_proposal_are_never_mixed(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Two generations, two objects: the installed plan and the waiting proposal."""
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    installed_before = response_for(hass, entry)["plan"]["installed"]
    assert installed_before is not None

    # A material change while the window is charging: it waits for the boundary.
    await preview.async_apply_settings(
        mutate=lambda settings: replace(settings, requested_kwh=settings.requested_kwh + 12)
    )
    await hass.async_block_till_done()

    plan = response_for(hass, entry)["plan"]

    assert plan["installed"] is not None and plan["proposal"] is not None
    assert plan["installed"]["periods"] == installed_before["periods"], "unchanged"
    assert plan["installed"]["active_period_index"] == 0
    assert plan["installed"]["identity"] == installed_before["identity"]
    assert plan["proposal"]["identity"] != plan["installed"]["identity"], "a newer proposal"
    assert plan["relation"]["pending_identity"] == plan["proposal"]["identity"]
    assert plan["relation"]["applied_identity"] == plan["installed"]["identity"]
    assert plan["relation"]["applied"] is False, "the proposal in this snapshot is not applied"
    assert plan["proposal"]["planned_kwh"] > 0

    # The wait is a normal status line naming the end of the open window, not an item to review.
    status = response_for(hass, entry)["status"]
    pending = [line for line in status["lines"] if line["code"] == "proposal_pending"]
    assert len(pending) == 1
    assert pending[0]["params"]["waits_for"] == "window_end"
    window_end = datetime.fromisoformat(pending[0]["params"]["installs_at"])
    active = plan["installed"]["periods"][plan["installed"]["active_period_index"]]
    assert window_end == datetime.fromisoformat(active["end"])
    assert status["tone"] == "normal"


@freeze_time(NOW)
async def test_a_re_described_plan_the_charger_already_runs_is_reported_as_applied(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A harmless edit describes the same charge a second time, and the contract says so.

    The two identities stay exact and different -- the installed one names the plan on the charger,
    the proposal is the freshly calculated document -- and `relation.applied` deliberately does *not*
    follow the identity. It is the field the card reads to draw one block instead of two: an edit to a
    figure the plan's own hours and current do not use would otherwise be drawn as a waiting change
    beside the very schedule it describes.
    """
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    installed_before = response_for(hass, entry)["plan"]["installed"]
    assert installed_before is not None
    assert response_for(hass, entry)["plan"]["relation"]["applied"] is True

    # A fiscal override for an area this charger does not use is not part of the plan, and it is not
    # in the material key either. The revision moves, so the proposal gets a new identity, and
    # nothing installs.
    await preview.async_apply_settings(
        mutate=lambda settings: replace(settings, overrides=(AreaAutoSettings(area_id="FI"),))
    )
    await hass.async_block_till_done()

    plan = response_for(hass, entry)["plan"]

    assert plan["installed"]["identity"] == installed_before["identity"], "still the same charge"
    assert [row["start"] for row in plan["installed"]["periods"]] == [
        row["start"] for row in installed_before["periods"]
    ], "the same hours, untouched"
    assert plan["proposal"] is not None, "a recalculated document"
    assert plan["proposal"]["identity"] != plan["installed"]["identity"], "and a newer identity"
    assert plan["relation"]["applied"] is True, "materially the same plan is what the charger runs"
    assert plan["relation"]["applied_identity"] == plan["installed"]["identity"]
    assert plan["relation"]["pending_identity"] is None, "nothing is waiting"


@freeze_time(NOW)
async def test_a_fresh_unconfigured_charger_serializes_the_contract_shape(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A charger nobody configured, as the default setup actually serializes it.

    Two facts decide this response and neither is a guess: the Auto controller has already run, so
    `planning` holds a snapshot (`state` `"incomplete_settings"`) and `market` holds the catalogue
    -- both keys are present objects -- while nothing has proposed or installed anything, so
    `plan.proposal` and `plan.installed` are explicit `None`, and `plan.relation.applied` is the
    nullable boolean `False`, not `None`. The key sets are asserted exactly so the fixtures cannot
    quietly drift from the wire.
    """
    entry = await setup_charger(hass)

    response = response_for(hass, entry)

    assert response["api_version"] == 1
    assert list(response) == DASHBOARD_SECTIONS, "the sections are the contract, in the documented order"
    assert response["site"] is None and response["strategy_state"] is None and response["soc"] is None
    assert response["vehicles"] == [] and response["target_vehicle_id"] is None
    assert response["chargers"] == [{"id": entry.entry_id, "name": entry.title}]
    assert response["strategy_options"] == ["cheapest"]
    assert set(response["phase_detection"]) == {"source", "confidence"}

    # `prices` and `plan` are never null: neither serializer has a `None` path (each has a
    # single `return`, and it is the object), so absence is stated by their nullable leaves.
    assert isinstance(response["prices"], dict)
    assert isinstance(response["plan"], dict)

    # `planning` and `market` are *present* here (a snapshot and a held catalogue exist) and their
    # own nullable leaves are what read `null`. `plan.relation.applied` is the nullable boolean
    # `false`, which the TypeScript decoder must accept.
    planning = response["planning"]
    assert set(planning) == {
        "state",
        "reason",
        "settings_revision",
        "generation",
        "calculated_at",
        "historical",
        "missing",
        "price_state",
        "price_identity",
        "today",
        "tomorrow",
        "execution_state",
        "execution_reason",
        "applied",
        "applied_identity",
        "pending_identity",
        "pending_attempt",
        "execution_paused",
        "price_wait",
        "publication_at",
        "must_buy_now_kwh",
    }
    assert planning["state"] == "incomplete_settings"
    assert planning["applied"] is False
    assert planning["price_state"] is None and planning["price_identity"] is None

    market = response["market"]
    assert set(market) == {
        "catalogue_state",
        "catalogue_fetched_at",
        "catalogue_attempt_error",
        "area_id",
        "area_name",
        "countries",
        "timezone",
        "currency",
        "major_unit",
        "minor_unit",
        "suggested_vat_percent",
        "suggested_tax",
        "suggested_grid_fee",
        "market_timezone",
        "included",
        "source",
    }
    assert market["area_id"] is None and market["timezone"] is None
    assert market["market_timezone"] is None and market["included"] is None and market["source"] is None
    assert market["currency"] is None and market["countries"] is None

    settings = response["settings"]
    assert settings is not None
    assert set(settings) == SETTINGS_RESPONSE_KEYS, "the canonical settings record, and its revision"
    assert settings["strategy"] == "cheapest" and settings["driver"] == "manual_kwh"
    assert settings["revision"] == 0
    assert settings["area_id"] is None and settings["amps"] is None
    # The phases a charge uses (the effective count, three until the charger or a car says otherwise).
    assert settings["phases"] == 3
    assert settings["overrides"] == [] and settings["target"] == {"vehicle_id": None, "target_percent": None}
    assert settings["departure_time"] == "08:00"
    assert response["fiscal"] is None, "no market, no fiscal block"

    prices = response["prices"]
    assert set(prices) == {
        "area_id",
        "state",
        "reason",
        "today",
        "tomorrow",
        "today_state",
        "tomorrow_state",
        "today_source",
        "tomorrow_source",
        "today_fetched_at",
        "tomorrow_fetched_at",
        "today_attempt_at",
        "tomorrow_attempt_at",
        "today_attempt_error",
        "tomorrow_attempt_error",
        "index_state",
        "index_revision",
        "waiting_for_tomorrow",
        "resolution_minutes",
        "resolutions_minutes",
        "priced_slots",
        "unpriced_slots",
        "unpriced",
        "interval_count",
        "intervals",
    }
    assert prices["intervals"] == []
    assert prices["interval_count"] == 0
    assert prices["resolutions_minutes"] == []
    assert prices["resolution_minutes"] is None
    assert prices["priced_slots"] == 0 and prices["unpriced_slots"] == 0
    assert prices["unpriced"] is False, "nothing unpriced, and the flag is a real boolean"
    assert prices["priced_slots"] == 0 and prices["unpriced_slots"] == 0
    assert prices["today"] is None and prices["tomorrow"] is None
    assert prices["area_id"] is None
    assert prices["state"] is None and prices["reason"] is None

    plan = response["plan"]
    assert set(plan) == {"proposal", "installed", "relation", "delivered_kwh", "remaining_kwh"}
    assert plan["proposal"] is None, "nothing Auto proposed anything yet"
    assert plan["installed"] is None, "no schedule is installed"
    relation = plan["relation"]
    assert set(relation) == {"applied", "applied_identity", "pending_identity"}
    assert relation["applied"] is False, "a nullable boolean, not an absent answer"
    assert relation["applied_identity"] is None and relation["pending_identity"] is None
    assert plan["delivered_kwh"] is None and plan["remaining_kwh"] is None

    live = response["live"]
    assert set(live) == {
        "charging",
        "schedule_active",
        "requested_current_a",
        "setpoint_current_a",
        "measured_current_a",
    }
    assert live["charging"] is False and live["schedule_active"] is False
    assert live["requested_current_a"] is None and live["setpoint_current_a"] is None
    assert live["measured_current_a"] is None

    charger = response["charger"]
    assert set(charger) == {"charger_id", "charger_name", "available", "capabilities"}
    assert list(charger["capabilities"]) == [
        "auto_price",
        "current_limit",
        "set_current",
        "regulated_current",
        "target_soc",
        "load_balancing",
        "refresh_vehicle",
        "set_charge_limit",
        "target_stop",
    ]


@freeze_time(NOW)
async def test_an_active_installed_interval_is_indexed_on_installed_only(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The active index describes what the charger is doing, not what was proposed."""
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass)

    plan = response_for(hass, entry)["plan"]

    assert plan["installed"]["active_period_index"] == 0
    assert "active_period_index" not in plan["proposal"]


async def test_get_dashboard_answers_over_the_real_authenticated_socket(
    hass: HomeAssistant, transport: StubTransport, hass_ws_client
) -> None:
    """The command works through Home Assistant's own WebSocket, not only as a serializer.

    Deliberately limited to what a transport test can prove: the authenticated handshake, the
    success envelope, and the exact sections and key sets of the answer. It asserts no proposal
    and no installed schedule, because at the real clock those are time-of-day facts -- in the
    evening the same setup legitimately has neither. Those semantics belong to the frozen tests
    that own them (`test_the_golden_fixture_is_the_shape_this_contract_promises` among them), and
    this test cannot be frozen: see the note on `NOW` above.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_auto(hass)
    client = await hass_ws_client(hass)

    reply = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )

    assert reply["success"] is True
    payload = reply["result"]
    assert list(payload) == DASHBOARD_SECTIONS
    assert payload["api_version"] == DASHBOARD_API_VERSION
    assert isinstance(payload["generated_at"], str)
    assert set(payload["plan"]) == {
        "proposal",
        "installed",
        "relation",
        "delivered_kwh",
        "remaining_kwh",
    }
    assert set(payload["planning"]) == set(
        json.loads((GOLDEN / "proposal_ready.json").read_text(encoding="utf-8"))["planning"]
    ), "the documented section, whatever state it reports"
    assert isinstance(payload["prices"]["intervals"], list), "the rows are a list, whatever is held"
    assert set(payload["charger"]) == {"charger_id", "charger_name", "available", "capabilities"}
    assert payload["charger"]["charger_id"] == entry.entry_id


def hourly_body(area: str, day: Any, price: float = 0.2) -> str:
    """The fixture day document republished at the relay's other resolution (60 minutes)."""
    document = json.loads(flat_day(area, day, flat=True))
    document["prices"] = [price] * 24
    document["res"] = 60
    return json.dumps(document)


@freeze_time(LATE)
async def test_a_charge_without_prices_is_a_notice_and_draws_no_borrowed_price(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Tomorrow never came and the deadline is near: the guarantee charges now, and says so plainly.

    Nothing on the chart is borrowed, so every row is a
    published one; the plan's own section says its slots have no published price.
    """
    entry = await setup_charger(hass)
    transport.serve(transport.day_path(SE4, TOMORROW), 503, "")
    serve(transport, days=(TODAY,), listed=(TODAY, TOMORROW))
    await go_auto(hass, departure_enabled=True, departure=time(8, 0))

    response = response_for(hass, entry)
    plan = response["plan"]
    rows = response["prices"]["intervals"]

    assert response["planning"]["state"] == "proposal_unpriced"
    assert response["planning"]["reason"] == "charging_without_prices"
    assert plan["proposal"]["unpriced"] is True
    assert plan["proposal"]["unpriced_slots"] > 0 and plan["proposal"]["priced_slots"] == 0
    assert rows and all(set(row) == INTERVAL_KEYS for row in rows), "no borrowed price, ever"
    assert all(row["raw_price"] is not None for row in rows)


@freeze_time(LATE)
async def test_waiting_for_tomorrow_is_the_state_a_listed_today_without_tomorrow_gives(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A valid index that lists today and not tomorrow, driven through the real manager.

    This is the actual "waiting for tomorrow" case: the relay is answering, it simply has not
    published tomorrow yet, so tomorrow is *not requested at all* -- and the states say exactly
    that, rather than reporting a failure that never happened.
    """
    entry = await setup_charger(hass)
    serve(transport, days=(TODAY,), listed=(TODAY,))
    await go_auto(hass)

    response = response_for(hass, entry)

    assert response["prices"]["state"] == "waiting_for_tomorrow"
    assert response["prices"]["reason"] == "waiting_for_tomorrow"
    assert response["prices"]["waiting_for_tomorrow"] is True
    assert response["prices"]["today_state"] == "ready"
    assert response["prices"]["tomorrow_state"] == "unavailable"
    assert response["prices"]["tomorrow_attempt_error"] is None, "it was never attempted"
    assert transport.call_count(transport.day_path(SE4, TOMORROW)) == 0, "not requested"
    assert response["planning"]["state"] == "waiting_for_publication"
    assert response["planning"]["reason"] == "publication_pending"
    assert response["plan"]["proposal"] is None
    assert response["prices"]["intervals"]
    assert all(row["day"] == TODAY.isoformat() for row in response["prices"]["intervals"])
    assert all(set(row) == INTERVAL_KEYS for row in response["prices"]["intervals"])


@freeze_time(LATE)
async def test_a_listed_but_unreachable_tomorrow_is_degraded_not_waiting(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Listed and failing is a different fact from listed and not published yet."""
    entry = await setup_charger(hass)
    transport.serve(transport.day_path(SE4, TOMORROW), 503, "")
    serve(transport, days=(TODAY,), listed=(TODAY, TOMORROW))
    await go_auto(hass)

    response = response_for(hass, entry)

    assert response["prices"]["waiting_for_tomorrow"] is False
    assert response["prices"]["tomorrow_state"] == "unavailable"
    assert response["prices"]["tomorrow_attempt_error"] == "http_status"
    assert transport.call_count(transport.day_path(SE4, TOMORROW)) >= 1, "it was attempted"
    assert response["plan"]["proposal"] is None
    assert response["planning"]["reason"] == "publication_pending"
    assert all(row["day"] == TODAY.isoformat() for row in response["prices"]["intervals"])


async def test_a_real_failed_refresh_reports_stale_and_keeps_the_retained_rows(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A day that fails to refresh is exactly `stale`: same document, newer attempt, named error.

    Every value here is asserted exactly -- the day's own state, the stable attempt code, the
    unchanged acquisition timestamp, the retained prices, the combined price state and reason,
    and what that does to the planning state. Nothing about this test would still pass if the
    refresh had not happened, or if the retained document had been replaced or dropped.
    """
    # The clock is frozen *and* then ticked: the claim is a stale day whose acquisition time is
    # older than its failed attempt, and a clock that never moves would make the two equal.
    with freeze_time(NOW) as frozen:
        entry = await setup_charger(hass)
        await go_auto(hass)
        repository = domain_data(hass).price_repository
        manager = domain_data(hass).price_refresh
        preview = preview_for(hass, entry.entry_id)
        assert repository is not None and manager is not None and preview is not None

        before = repository.day_snapshot(SE4, TODAY)
        assert before.state == "ready"
        retained = before.document
        assert retained is not None
        fetched_at = before.fetched_at
        assert fetched_at is not None
        rows_before = response_for(hass, entry)["prices"]["intervals"]
        assert rows_before

        # The relay starts answering with a named HTTP failure for today's document, which is still
        # listed: the refresh must fail, and must keep what it already had.
        # Five minutes later, so "a newer attempt" is a fact of the clock, not of the wall.
        frozen.tick(delta=timedelta(minutes=5))
        transport.serve(transport.day_path(SE4, TODAY), 503, "")
        await repository.async_get_day(SE4, TODAY, refresh=True)
        await manager.async_refresh_area(SE4)
        await preview.async_recalculate()
        await hass.async_block_till_done()

        after = repository.day_snapshot(SE4, TODAY)

        assert after.state == "stale"
        assert after.attempt_error == "http_status"
        assert after.attempt_at is not None and after.attempt_at > fetched_at
        assert after.fetched_at == fetched_at, "the retained document's own acquisition time"
        assert after.document is retained, "the very same retained document"
        assert after.document.prices == retained.prices, "and the very same prices"
        assert repository.day_snapshot(SE4, TOMORROW).state == "ready", "only today failed"

        response = response_for(hass, entry)

        assert response["prices"]["state"] == "stale"
        assert response["prices"]["reason"] == "last_good_retained"
        assert response["prices"]["today_state"] == "stale"
        assert response["prices"]["today_attempt_error"] == "http_status"
        assert response["prices"]["today_fetched_at"] == fetched_at.isoformat()
        assert response["planning"]["state"] == "price_data_stale"
        assert response["planning"]["reason"] == "price_data_stale"
        assert response["prices"]["intervals"], "the retained rows are still drawn"
        assert len(response["prices"]["intervals"]) == len(rows_before), "the same horizon"
        assert [row["raw_price"] for row in response["prices"]["intervals"]] == [
            row["raw_price"] for row in rows_before
        ], "and the same prices"


@freeze_time(NOW)
async def test_mixed_relay_resolutions_are_reported_as_a_set(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Two held days in different resolutions: a null single value, and both grids in the rows.

    The chart must show the whole held day, past hours included. Held rows are the base and the
    proposal horizon is overlaid on them (see `serialize_intervals`), so elapsed hours before the
    planner's first candidate stay at today's 60-minute resolution, and every hour from the first
    candidate through the end of tomorrow is split onto the planner's 15-minute grid. Both
    resolutions are present in one response, as `resolutions_minutes` says.
    """
    entry = await setup_charger(hass)
    transport.serve_area(SE4)
    transport.serve(transport.day_path(SE4, TODAY), 200, hourly_body(SE4, TODAY))
    transport.serve(
        transport.day_path(SE4, TOMORROW), 200, flat_day(SE4, TOMORROW, flat=True)
    )  # 15-minute tomorrow against an hourly today
    serve_index(transport, {SE4: [TODAY, TOMORROW]})
    await go_auto(hass)

    response = response_for(hass, entry)
    prices = response["prices"]

    assert response["plan"]["proposal"] is not None, "the prerequisite: a usable proposal"
    assert response["plan"]["proposal"]["periods"]
    assert response["planning"]["state"] == "proposal_ready"

    assert prices["resolutions_minutes"] == [15, 60]
    assert prices["resolution_minutes"] is None, "no single resolution describes both days"
    durations = {row["duration_minutes"] for row in prices["intervals"]}
    assert durations == {15, 60}, "today's elapsed hours stay 60 minutes; the horizon is 15"
    # The whole held span, in both days, with no gap and no double-covered minute: exactly two
    # full days of elapsed time across however many rows that took at either resolution.
    total_minutes = sum(row["duration_minutes"] for row in prices["intervals"])
    assert total_minutes == 48 * 60
    assert {row["day"] for row in prices["intervals"]} == {
        TODAY.isoformat(),
        TOMORROW.isoformat(),
    }


@freeze_time("2026-09-22 06:20:00")
async def test_an_hour_the_proposal_enters_mid_way_is_drawn_as_quarters_without_overlap(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A proposal's first candidate at :15 or later lies inside a published hour whose start it
    does not share. That hour must be neither drawn whole beside the proposal's quarters (a
    double-covered span) nor dropped (a gap): the rows tile both days exactly, in order.
    """
    entry = await setup_charger(hass)
    transport.serve_area(SE4)
    transport.serve(transport.day_path(SE4, TODAY), 200, hourly_body(SE4, TODAY))
    transport.serve(transport.day_path(SE4, TOMORROW), 200, flat_day(SE4, TOMORROW, flat=True))
    serve_index(transport, {SE4: [TODAY, TOMORROW]})
    await go_auto(hass)

    response = response_for(hass, entry)
    rows = response["prices"]["intervals"]
    assert response["plan"]["proposal"] is not None, "the prerequisite: a usable proposal"
    starts = [datetime.fromisoformat(row["start"]) for row in rows]
    ends = [datetime.fromisoformat(row["end"]) for row in rows]
    for index in range(1, len(rows)):
        assert starts[index] == ends[index - 1], f"gap or overlap at row {index}: {rows[index]['start']}"
    assert sum(row["duration_minutes"] for row in rows) == 48 * 60


@freeze_time(NOW)
async def test_without_a_proposal_the_held_days_are_drawn_at_their_own_resolution(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The other honest answer for the same two days: no proposal, so nothing is re-gridded.

    The request cannot fit the horizon, so the calculation refuses instead of estimating its way
    out, and what is serialized is exactly what the repository holds: an hourly day and a
    quarter-hour day, each at its published resolution.
    """
    entry = await setup_charger(hass)
    transport.serve_area(SE4)
    transport.serve(transport.day_path(SE4, TODAY), 200, hourly_body(SE4, TODAY))
    transport.serve(
        transport.day_path(SE4, TOMORROW), 200, flat_day(SE4, TOMORROW, flat=True)
    )
    serve_index(transport, {SE4: [TODAY, TOMORROW]})
    await go_auto(hass, requested_kwh=1000.0)

    response = response_for(hass, entry)
    prices = response["prices"]

    assert response["plan"]["proposal"] is None, "a refusal is not a proposal"
    assert response["planning"]["reason"] == "insufficient_price_horizon"
    assert prices["resolutions_minutes"] == [15, 60]
    assert prices["resolution_minutes"] is None
    assert {row["duration_minutes"] for row in prices["intervals"]} == {15, 60}
    assert {row["day"] for row in prices["intervals"]} == {
        TODAY.isoformat(),
        TOMORROW.isoformat(),
    }
    assert all(set(row) == INTERVAL_KEYS for row in prices["intervals"])


@freeze_time(NOW)
async def test_a_charger_with_no_settings_record_is_reported_honestly(
    hass: HomeAssistant,
) -> None:
    """The store's own default record, reported as stored -- nothing invented, nothing hidden.

    Worth stating plainly: the settings store has *no* "absent record" state. A charger that
    nobody has ever configured has a record of defaults (revision 0, `cheapest`, a current of
    `null`), and this contract reports exactly that record rather than a second set of defaults
    of its own.
    """
    entry = await setup_charger(hass)

    response = response_for(hass, entry)
    settings = response["settings"]

    assert settings["revision"] == 0
    assert settings["strategy"] == "cheapest"
    assert settings["area_id"] is None and settings["amps"] is None
    assert settings["phases"] == 3
    assert response["fiscal"] is None, "no market, no fiscal figures"
    assert response["plan"]["proposal"] is None and response["plan"]["installed"] is None
    assert response["prices"]["intervals"] == [], "nothing held, nothing drawn"
    assert response["planning"]["state"] == "incomplete_settings"

# ------------------------------------------------------------ DST and the real largest case


def request_for(
    documents: tuple[Any, ...], *, now: datetime, **changes: Any
) -> Any:
    """A plan request for hand-built days, in Stockholm's own zone."""
    fields: dict[str, Any] = {
        "area_id": SE4,
        "timezone": "Europe/Stockholm",
        "currency": "EUR",
        "major_unit": "€",
        "minor_unit": "cent",
        "documents": documents,
        "now": now,
        "phases": 1,
        "amps": 10,
        "requested_kwh": 5.0,
        "consumption_kwh_per_10km": 2.0,
    }
    fields.update(changes)
    return PlanRequest(**fields)


def transition_documents(day: str, slots: int) -> tuple[Any, Any]:
    """The 23- or 25-hour day, plus the complete day after it."""
    start = date.fromisoformat(day)
    after = start + timedelta(days=1)
    return (
        document(day, resolution=15, prices=[0.2] * slots),
        document(after.isoformat(), resolution=15, prices=[0.2] * 96),
    )


def assert_horizon_is_exact(horizon: Any) -> None:
    """Every row is fifteen elapsed minutes, ordered strictly by instant, and serializes so."""
    for row in horizon:
        assert row.utc_end - row.utc_start == timedelta(minutes=15)
        assert row.end.astimezone(timezone.utc) - row.start.astimezone(timezone.utc) == timedelta(
            minutes=15
        )
        start = datetime.fromisoformat(aware_iso(row.start))
        end = datetime.fromisoformat(aware_iso(row.end))
        assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(
            minutes=15
        )
        assert start.tzinfo is not None and end.tzinfo is not None
        # And the displayed bound is the zone's own reading of that instant: a local wall time
        # that the zone skipped (or a folded one written with the wrong offset) does not survive
        # the round trip, which is exactly how a wall-clock projection gives itself away.
        assert row.start == row.start.astimezone(timezone.utc).astimezone(row.start.tzinfo)
        assert row.end == row.end.astimezone(timezone.utc).astimezone(row.end.tzinfo)
    instants = [row.utc_start for row in horizon]
    assert instants == sorted(instants)
    assert len(set(instants)) == len(instants), "no instant twice"


def test_a_spring_forward_horizon_keeps_fifteen_elapsed_minutes() -> None:
    """Europe/Stockholm, 29 March 2026: 02:00 local does not exist, and no row invents it."""
    now = datetime(2026, 3, 29, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    result = calculate_plan(
        request_for(transition_documents("2026-03-29", 92), now=now)
    )

    assert result.reason is None
    assert_horizon_is_exact(result.horizon)

    # The horizon starts at the rounded-up `now` (00:45), so the first two quarter-hours of the
    # day are behind it -- the other 90 published quarter-hours of the 23-hour day are all here.
    spring = [row for row in result.horizon if row.day == date(2026, 3, 29)]
    assert len(spring) == 92 - 2, "the whole 23-hour day from the start of the window"
    assert not [row for row in spring if row.start.hour == 2], "the skipped hour"
    bridge = [row for row in spring if row.start.hour == 1][-1:] + [
        row for row in spring if row.start.hour == 3
    ][:1]
    assert [row.start.isoformat() for row in bridge] == [
        "2026-03-29T01:45:00+01:00",
        "2026-03-29T03:00:00+02:00",
    ], "wall clock jumps, instants do not"
    assert bridge[1].utc_start - bridge[0].utc_start == timedelta(minutes=15)
    assert sum(1 for row in result.horizon if row.in_proposal) == result.slots_needed


def test_an_autumn_repeated_hour_keeps_both_local_hours_distinct() -> None:
    """Europe/Stockholm, 25 October 2026: 02:xx happens twice, and neither is lost."""
    now = datetime(2026, 10, 25, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    result = calculate_plan(
        request_for(transition_documents("2026-10-25", 100), now=now)
    )

    assert result.reason is None
    assert_horizon_is_exact(result.horizon)

    autumn = [row for row in result.horizon if row.day == date(2026, 10, 25)]
    assert len(autumn) == 96, "the whole 24-hour window, all of it inside the 25-hour day"
    second = [row for row in autumn if row.start.hour == 2]
    assert len(second) == 8, "four quarter-hours, twice"
    assert {row.start.isoformat() for row in second} == {
        "2026-10-25T02:00:00+02:00",
        "2026-10-25T02:15:00+02:00",
        "2026-10-25T02:30:00+02:00",
        "2026-10-25T02:45:00+02:00",
        "2026-10-25T02:00:00+01:00",
        "2026-10-25T02:15:00+01:00",
        "2026-10-25T02:30:00+01:00",
        "2026-10-25T02:45:00+01:00",
    }, "both folds, with their own offsets, none folded away"
    assert [row.start.fold for row in second] == [0, 0, 0, 0, 1, 1, 1, 1]
    assert sum(1 for row in result.horizon if row.in_proposal) == result.slots_needed


def test_overlap_flags_on_a_repeated_hour_are_decided_by_instant() -> None:
    """The half-open span a chart shades is compared by instant, not by wall clock.

    On the autumn night two rows share the wall time 02:00 and differ by an hour. A span that
    starts at the *second* of them must exclude the first, and both boundary rows fall outside a
    half-open span: the row ending exactly where it starts, and the row starting exactly where it
    ends. A wall-clock comparison cannot tell the two 02:00 rows apart at all.
    """
    now = datetime(2026, 10, 25, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    result = calculate_plan(request_for(transition_documents("2026-10-25", 100), now=now))
    rows = [row for row in result.horizon if row.day == date(2026, 10, 25)]

    second = [row for row in rows if row.start.hour == 2 and row.start.fold == 1]
    assert len(second) == 4
    span_start = second[0].utc_start
    span = (span_start, span_start + timedelta(hours=1))

    flagged = [
        row for row in rows if dashboard_api._overlaps(row.utc_start, row.utc_end, (span,))
    ]
    assert flagged == second, "the second 02:xx hour, and not the first"

    before = [row for row in rows if row.utc_end == span_start]
    after = [row for row in rows if row.utc_start == span[1]]
    assert len(before) == 1 and len(after) == 1
    assert before[0].start.isoformat() == "2026-10-25T02:45:00+02:00"
    assert after[0].start.isoformat() == "2026-10-25T03:00:00+01:00"
    assert dashboard_api._overlaps(before[0].utc_start, before[0].utc_end, (span,)) is False
    assert dashboard_api._overlaps(after[0].utc_start, after[0].utc_end, (span,)) is False


@freeze_time("2025-10-26 06:00:00")
async def test_the_largest_response_is_two_held_days_with_no_proposal(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The real worst case: a 100-slot autumn day plus a complete 96-slot day, 196 rows.

    No proposal horizon replaces them -- the calculation refuses (a request that cannot fit the
    horizon), which is the documented fallback: what is serialized is exactly the published
    intervals the repository holds.
    """
    autumn = date(2025, 10, 26)
    neighbour = date(2025, 10, 27)
    entry = await setup_charger(hass)
    transport.serve_area(SE4)
    transport.serve(transport.day_path(SE4, autumn), 200, fixture("day_SE4_2025-10-26_100.json"))
    transport.serve(
        transport.day_path(SE4, neighbour), 200, flat_day(SE4, neighbour, flat=True)
    )
    serve_index(transport, {SE4: [autumn, neighbour]})
    await go_auto(hass, requested_kwh=1000.0)

    response = response_for(hass, entry)
    rows = response["prices"]["intervals"]
    compact = len(json.dumps(response, separators=(",", ":")).encode("utf-8"))

    assert response["plan"]["proposal"] is None, "the refusal is not a proposal"
    assert response["planning"]["reason"] == "insufficient_price_horizon"
    assert len(rows) == 196, (len(rows), sorted({row["day"] for row in rows}))
    assert {row["day"] for row in rows} == {autumn.isoformat(), neighbour.isoformat()}
    assert all(row["duration_minutes"] == 15 for row in rows)
    instants = [datetime.fromisoformat(row["start"]).astimezone(timezone.utc) for row in rows]
    assert instants == sorted(instants), "ordered by instant, not by wall clock"
    assert len(set(instants)) == len(instants)
    assert response["prices"]["resolutions_minutes"] == [15]
    assert compact < MAX_RESPONSE_BYTES, (compact, MAX_RESPONSE_BYTES)
    print(f"--- 196-row response: {compact} bytes")

# The market graph, whatever the plan is: the market observation is a display need, so a charger
# with an area and nothing to plan still charts its market. Settings are written through
# `async_apply_settings`; no test in this section calls `go_auto`.


async def go_area_only(hass: HomeAssistant, entry: Any, **changes: Any) -> None:
    """Write an area and nothing else through the reviewed path, mutating the whole document.

    `replace` on the *stored* record, never a hand-built one.
    """
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    changes.setdefault("area_id", SE4)
    await preview.async_apply_settings(mutate=lambda settings: replace(settings, **changes))
    await hass.async_block_till_done()


@freeze_time(NOW)
async def test_a_charger_with_only_an_area_charts_its_market_with_no_bands_at_all(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """An area and no plan: a populated chart, and not one shaded row.

    Two independent absences, and both are asserted: the charger proposed nothing (it cannot
    plan yet, so nothing ever ran), and nothing is installed either. The rows are still there, from the
    area the observation holds -- which is the whole point of the display need being its own owner.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)

    response = response_for(hass, entry)
    prices = response["prices"]
    plan = response["plan"]

    assert prices["area_id"] == SE4, "the market the settings name"
    assert prices["intervals"], "a charger with only an area still has a chart of its market"
    assert prices["intervals"][0]["start"] is not None
    assert all(row["installed_planned"] is False for row in prices["intervals"])
    assert all(row["proposal_planned"] is False for row in prices["intervals"])
    assert plan["installed"] is None and plan["proposal"] is None
    assert plan["relation"]["applied"] is False
    assert response["site"] is None
    # The market facts are only resolvable through the catalogue, so this is also the proof that the
    # observation resolved SE4 for a charger that never chose Auto.
    assert response["market"]["area_id"] == SE4


@freeze_time(NOW)
async def test_a_settings_change_that_keeps_the_area_neither_refetches_nor_resubscribes(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """Same area, new energy: the chart is retained, not rebuilt and not emptied.

    A settings write that does not move the market may not disturb the subscription -- the observation
    is already watching exactly that area -- so the rows the response holds are the same rows, and the
    manager's record for the area is the same record.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)
    manager = domain_data(hass).price_refresh
    assert manager is not None
    before = response_for(hass, entry)["prices"]
    revision = manager.area_snapshot(SE4)
    assert revision is not None

    fetches = transport.call_count(transport.day_path(SE4, TODAY))
    await go_area_only(hass, entry, requested_kwh=30.0)

    after = response_for(hass, entry)["prices"]
    assert after["intervals"] == before["intervals"], "the same market, the same rows"
    assert after["area_id"] == SE4
    assert manager.area_snapshot(SE4) is not None
    assert transport.call_count(transport.day_path(SE4, TODAY)) == fetches, "and no refetch"


@freeze_time(NOW)
async def test_a_failed_same_area_refresh_keeps_se4s_rows_and_reports_stale(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The area's own refresh cannot get current prices: the last good day is retained, and said so.

    The failure is made deterministic through the one thing that *can* fail for a held area: the
    relay's own authority. Day documents are immutable, so a held day is never re-fetched (the rule
    `test_price_refresh` pins), which means current prices can only go missing by the index ceasing
    to list the day -- the case the product documents as `stale` / `last_good_retained`. The
    manager's real refresh path is driven for it, and the assertion is about what the dashboard then
    reports: exactly the rows that were held, still SE4's, and a region honest about their age. Then
    the relay is restored and a later successful refresh clears it.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)
    manager = domain_data(hass).price_refresh
    assert manager is not None

    before = response_for(hass, entry)["prices"]
    # The prerequisite, stated rather than assumed: two complete, freshly published days, so
    # "the rows are retained" is a claim about real documents and not about an empty chart.
    assert before["area_id"] == SE4
    assert before["state"] == "ready" and before["reason"] == "ready"
    assert before["today_state"] == "ready" and before["tomorrow_state"] == "ready"
    assert before["intervals"]

    # The relay's index stops listing the days it served: SE4's next refresh cannot obtain current
    # prices, and the held documents are precisely what must survive that.
    serve_index(transport, {SE4: [YESTERDAY]})
    await manager.async_refresh_area(SE4)
    await hass.async_block_till_done()

    after = response_for(hass, entry)["prices"]
    assert after["state"] == "stale", "the documented state for a retained last good"
    assert after["reason"] == "last_good_retained"
    assert after["today_state"] == "stale" and after["tomorrow_state"] == "stale"
    # Not one row moves, and nothing is borrowed: the same rows in the same order, with the same
    # instants, the same prices, the same day and the same area.
    assert after["intervals"] == before["intervals"]
    assert after["area_id"] == SE4
    assert after["today"] == before["today"] and after["tomorrow"] == before["tomorrow"]
    assert after["today_fetched_at"] == before["today_fetched_at"], "the same held document"
    assert after["tomorrow_fetched_at"] == before["tomorrow_fetched_at"]
    assert [row["start"] for row in after["intervals"]] == [
        row["start"] for row in before["intervals"]
    ]
    assert all(
        row["day"] in {before["today"], before["tomorrow"]} for row in after["intervals"]
    ), "no row is relabelled to another day"

    # The relay is restored: the next successful refresh clears the stale region, on the same two
    # documents (nothing is re-requested, because nothing about them changed).
    serve(transport, flat=True)
    await manager.async_refresh_area(SE4)
    await hass.async_block_till_done()

    recovered = response_for(hass, entry)["prices"]
    assert recovered["state"] == "ready" and recovered["reason"] == "ready"
    assert recovered["today_state"] == "ready" and recovered["tomorrow_state"] == "ready"
    assert recovered["intervals"] == before["intervals"], "same published documents"
    assert recovered["today_fetched_at"] == before["today_fetched_at"]


@freeze_time(NOW)
async def test_moving_market_never_borrows_the_previous_areas_rows(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A move to another market: the old area's rows are refused, not relabelled.

    `DE-LU` is in the served catalogue and has no days served, which is the ordinary "moved to a
    market the relay has nothing for yet" case. The settings write succeeds, the response names the
    new area -- and not one SE4 row appears underneath it.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)
    assert response_for(hass, entry)["prices"]["intervals"], "the old market has rows"

    await go_area_only(hass, entry, area_id=DE_LU)

    response = response_for(hass, entry)
    assert response["settings"]["area_id"] == DE_LU, "the write itself succeeded"
    assert response["prices"]["area_id"] == DE_LU, "and the graph moved with it"
    assert response["prices"]["intervals"] == [], "SE4's rows are not DE-LU's chart"
    assert response["api_version"] == 1, "and the charger is still served"


@freeze_time(NOW)
async def test_capturing_the_dashboard_never_touches_the_network(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """A response is assembled from what is held: three captures, not one request."""
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)
    paths = (
        "/v1/areas.json",
        "/v1/index.json",
        transport.day_path(SE4, TODAY),
        transport.day_path(SE4, TOMORROW),
    )
    before = {path: transport.call_count(path) for path in paths}

    for _ in range(3):
        response_for(hass, entry)

    assert {path: transport.call_count(path) for path in paths} == before, "a read is a read"


async def test_the_authenticated_socket_serves_an_area_only_chargers_market(
    hass: HomeAssistant, transport: StubTransport, hass_ws_client
) -> None:
    """The real socket, for a charger that has an area and nothing else.

    No freeze: Home Assistant mints the token this client uses at the real clock (see the note on
    `NOW`), so this asserts what the transport proves -- the handshake, the shape, and that the
    market the settings name is the one the response carries -- and leaves row geometry to the
    frozen tests above.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)
    client = await hass_ws_client(hass)

    reply = await ws_call(
        client,
        {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
    )

    assert reply["success"] is True
    payload = reply["result"]
    assert payload["api_version"] == DASHBOARD_API_VERSION
    assert payload["prices"]["area_id"] == SE4
    assert isinstance(payload["prices"]["intervals"], list)
    assert payload["plan"]["proposal"] is None and payload["plan"]["installed"] is None
    assert payload["market"]["area_id"] == SE4, "the market it chose"
    # The same recursive, path-based audit the direct response passes above -- over the payload the
    # socket actually sent, which is the only place "a browser receives this" is literally true.
    assert_no_private_identifiers(payload)
    assert set(payload) == set(response_for(hass, entry)), "same contract as the direct path"


@freeze_time(NOW)
async def test_a_dashboard_read_plans_installs_saves_and_calls_nothing(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading is reading: no calculation, no install, no settings write, no service call.

    The recorders are on the *only* paths that could do any of it -- the planner the controller
    imports, the executor's apply, the store's update -- plus Home Assistant's own service registry,
    so "nothing happened" is a fact about the unmodified machinery rather than about a stub.
    """
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)
    preview = preview_for(hass, entry.entry_id)
    assert preview is not None
    before = preview.snapshot()

    calls: list[str] = []

    def record(what: str) -> Any:
        def wrapper(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(what)
            return None

        return wrapper

    monkeypatch.setattr(auto_controller, "calculate_plan", record("calculate"))
    monkeypatch.setattr(auto_execution.AutoExecutor, "async_apply_pending", record("apply"))
    monkeypatch.setattr(auto_settings.AutoSettingsStore, "async_update", record("save"))
    services = [
        async_mock_service(hass, "switch", "turn_on"),
        async_mock_service(hass, "switch", "turn_off"),
        async_mock_service(hass, "number", "set_value"),
        async_mock_service(hass, "button", "press"),
        async_mock_service(hass, "ocpp", "configure"),
    ]

    for _ in range(3):
        assert response_for(hass, entry)["prices"]["area_id"] == SE4

    assert calls == [], calls
    assert all(service_calls == [] for service_calls in services)
    assert preview.snapshot() == before, "not even the snapshot moved"


@freeze_time(NOW)
async def test_a_real_entry_charts_on_reload_and_releases_everything_on_removal(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    """The whole entry lifecycle, on the real config entry: unload, reload, remove."""
    entry = await setup_charger(hass)
    serve(transport, flat=True)
    await go_area_only(hass, entry)
    manager = domain_data(hass).price_refresh
    assert manager is not None
    assert response_for(hass, entry)["prices"]["intervals"]
    assert manager.area_snapshot(SE4) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert manager.area_snapshot(SE4) is None, "the unloaded observation released the market"
    assert manager._catalogue_listeners == [], "and its catalogue listener with it"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert response_for(hass, entry)["prices"]["intervals"], "a reload restores the chart"
    assert manager.area_snapshot(SE4) is not None
    # One area selector's listener and one market observation's: exactly as many as before the reload.
    assert len(manager._catalogue_listeners) == 2, "one per consumer, not two per consumer"

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert manager.area_snapshot(SE4) is None
    assert manager._catalogue_listeners == []
