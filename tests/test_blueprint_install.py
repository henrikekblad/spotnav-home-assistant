"""SpotNav installs its blueprints in `<config>/blueprints/automation/spotnav/`, only when they differ, never anywhere
else, keeps a copy the user edited, reloads automations only when a blueprint they use was written, and counts the
automations that use each."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.automation import EVENT_AUTOMATION_RELOADED
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

import custom_components.spotnav as integration
import custom_components.spotnav.blueprint_install as blueprint_install
from custom_components.spotnav.blueprint_install import (
    async_install_blueprints,
    BLUEPRINT_FILES,
    install_blueprints,
    SHIPPED_BEFORE,
    SOURCE_FOLDER,
    STORE_KEY,
)
from custom_components.spotnav.const import DOMAIN

from .world import admin, non_admin, ws_call

pytestmark = pytest.mark.blueprint_install


def _files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _installed(root: Path, name: str) -> Path:
    return root / "blueprints" / "automation" / "spotnav" / name


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _shipped(name: str) -> bytes:
    return (SOURCE_FOLDER / name).read_bytes()


def _keep(hass_storage: dict[str, Any], hashes: dict[str, str]) -> None:
    """The hashes SpotNav kept at its last start."""
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": {"written": hashes}}


def test_the_shipped_folder_holds_the_listed_blueprints() -> None:
    assert sorted(path.name for path in SOURCE_FOLDER.iterdir()) == sorted(BLUEPRINT_FILES)


def test_installs_both_then_writes_nothing_while_equal(tmp_path: Path) -> None:
    # Missing: written, and the hash of what was written kept.
    done = install_blueprints(str(tmp_path))
    assert done.written == list(BLUEPRINT_FILES)
    assert done.hashes == {name: _sha(_shipped(name)) for name in BLUEPRINT_FILES}
    for name in BLUEPRINT_FILES:
        assert _installed(tmp_path, name).read_bytes() == _shipped(name)
    before = {name: _installed(tmp_path, name).stat().st_mtime_ns for name in BLUEPRINT_FILES}
    again = install_blueprints(str(tmp_path), done.hashes)
    assert (again.written, again.kept, again.hashes) == ([], [], done.hashes)
    assert {name: _installed(tmp_path, name).stat().st_mtime_ns for name in BLUEPRINT_FILES} == before


def test_a_copy_equal_to_the_shipped_one_is_adopted_without_a_write(tmp_path: Path) -> None:
    install_blueprints(str(tmp_path))
    before = _files(tmp_path)
    done = install_blueprints(str(tmp_path), {})
    assert (done.written, done.kept) == ([], [])
    assert done.hashes == {name: _sha(_shipped(name)) for name in BLUEPRINT_FILES}
    assert _files(tmp_path) == before


def test_a_copy_still_as_spotnav_wrote_it_is_updated_and_everything_else_left(tmp_path: Path) -> None:
    # The user's own files: another blueprint folder, a file of their own in SpotNav's folder, an automation file.
    (tmp_path / "automations.yaml").write_text("- id: mine\n")
    (tmp_path / "blueprints" / "automation" / "homeassistant").mkdir(parents=True)
    (tmp_path / "blueprints" / "automation" / "homeassistant" / "motion_light.yaml").write_text("blueprint: {}\n")
    install_blueprints(str(tmp_path))
    (tmp_path / "blueprints" / "automation" / "spotnav" / "my_copy.yaml").write_text("blueprint: {}\n")
    # What an older SpotNav wrote, and kept the hash of.
    older = b"blueprint: {name: older}\n"
    _installed(tmp_path, "price_setpoint.yaml").write_bytes(older)
    stored = {"price_setpoint.yaml": _sha(older), "solar_surplus_switch.yaml": _sha(_shipped("solar_surplus_switch.yaml"))}
    before = _files(tmp_path)

    done = install_blueprints(str(tmp_path), stored)

    assert (done.written, done.kept) == (["price_setpoint.yaml"], [])
    assert done.hashes["price_setpoint.yaml"] == _sha(_shipped("price_setpoint.yaml"))
    after = _files(tmp_path)
    assert set(after) == set(before)
    changed = {name for name in after if after[name] != before[name]}
    assert changed == {"blueprints/automation/spotnav/price_setpoint.yaml"}
    assert after["blueprints/automation/spotnav/price_setpoint.yaml"] == _shipped("price_setpoint.yaml")


@pytest.mark.parametrize("stored", [{}, {"price_setpoint.yaml": "0" * 64}], ids=["nothing_kept", "another_hash_kept"])
def test_an_edited_copy_is_kept(tmp_path: Path, stored: dict[str, str]) -> None:
    install_blueprints(str(tmp_path))
    edited = b"blueprint: {name: edited}\n"
    _installed(tmp_path, "price_setpoint.yaml").write_bytes(edited)
    done = install_blueprints(str(tmp_path), stored)
    assert (done.written, done.kept) == ([], ["price_setpoint.yaml"])
    assert _installed(tmp_path, "price_setpoint.yaml").read_bytes() == edited
    # The kept hash is not changed by a copy SpotNav did not write.
    assert done.hashes.get("price_setpoint.yaml") == stored.get("price_setpoint.yaml")


def test_a_copy_an_earlier_version_installed_is_updated_before_any_hash_was_kept(tmp_path: Path, monkeypatch) -> None:
    install_blueprints(str(tmp_path))
    earlier = b"blueprint: {name: as shipped before}\n"
    _installed(tmp_path, "solar_surplus_switch.yaml").write_bytes(earlier)
    monkeypatch.setattr(blueprint_install, "SHIPPED_BEFORE", {"solar_surplus_switch.yaml": (_sha(earlier),)})
    done = install_blueprints(str(tmp_path), {})
    assert (done.written, done.kept) == (["solar_surplus_switch.yaml"], [])
    assert _installed(tmp_path, "solar_surplus_switch.yaml").read_bytes() == _shipped("solar_surplus_switch.yaml")


def test_the_versions_shipped_before_are_hashes_of_other_content() -> None:
    assert set(SHIPPED_BEFORE) == set(BLUEPRINT_FILES)
    for name, hashes in SHIPPED_BEFORE.items():
        assert hashes and all(len(value) == 64 and int(value, 16) >= 0 for value in hashes)
        assert _sha(_shipped(name)) not in hashes


def test_a_fresh_config_gets_only_spotnavs_folder(tmp_path: Path) -> None:
    install_blueprints(str(tmp_path))
    assert set(_files(tmp_path)) == {f"blueprints/automation/spotnav/{name}" for name in BLUEPRINT_FILES}


async def _automation_using(hass: HomeAssistant, tmp_path: Path, name: str) -> None:
    """One automation made from the installed blueprint `name`, with sample entities."""
    inputs = {
        "price_setpoint.yaml": {
            "thermostats": ["climate.hall"],
            "cheap_now": "binary_sensor.electricity_price_se4_cheap_now",
            "expensive_now": "binary_sensor.electricity_price_se4_expensive_now",
            "price_now": "sensor.electricity_price_se4_price_now",
        },
        "solar_surplus_switch.yaml": {"surplus_sensor": "sensor.home_solar_surplus", "target": "switch.pool"},
    }[name]
    config = {
        "automation": [
            {"id": "from_blueprint", "alias": "from blueprint", "use_blueprint": {"path": f"spotnav/{name}", "input": inputs}}
        ]
    }
    # What a reload reads again.
    (tmp_path / "configuration.yaml").write_text(json.dumps(config))
    assert await async_setup_component(hass, "automation", config)
    await hass.async_block_till_done()
    assert hass.states.get("automation.from_blueprint").state == "on"


@pytest.fixture
async def stopped_after(hass: HomeAssistant):
    yield
    if hass.services.has_service("automation", "turn_off"):
        await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
        await hass.async_block_till_done()


async def test_reloads_automations_only_when_a_blueprint_they_use_changed(
    hass: HomeAssistant, tmp_path: Path, hass_storage: dict[str, Any], stopped_after
) -> None:
    hass.config.config_dir = str(tmp_path)
    assert await async_install_blueprints(hass) == list(BLUEPRINT_FILES)
    await _automation_using(hass, tmp_path, "price_setpoint.yaml")
    reloads: list[object] = []
    hass.bus.async_listen(EVENT_AUTOMATION_RELOADED, reloads.append)

    # Nothing differs: nothing written, nothing reloaded.
    assert await async_install_blueprints(hass) == []
    await hass.async_block_till_done()
    assert reloads == []

    # SpotNav's own older copy (its hash kept) of a blueprint no automation uses: written, not reloaded.
    old = b"# old\n"
    shipped = {name: _sha(_shipped(name)) for name in BLUEPRINT_FILES}
    _installed(tmp_path, "solar_surplus_switch.yaml").write_bytes(old)
    _keep(hass_storage, {**shipped, "solar_surplus_switch.yaml": _sha(old)})
    assert await async_install_blueprints(hass) == ["solar_surplus_switch.yaml"]
    await hass.async_block_till_done()
    assert reloads == []
    assert hass_storage[STORE_KEY]["data"] == {"written": shipped}

    # An edited copy of the one the automation uses: kept, nothing written, nothing reloaded.
    _installed(tmp_path, "price_setpoint.yaml").write_bytes(old)
    assert await async_install_blueprints(hass) == []
    await hass.async_block_till_done()
    assert reloads == []
    assert _installed(tmp_path, "price_setpoint.yaml").read_bytes() == old

    # The same copy, SpotNav's own older one: written and reloaded once, and the automation still runs.
    _keep(hass_storage, {**shipped, "price_setpoint.yaml": _sha(old)})
    assert await async_install_blueprints(hass) == ["price_setpoint.yaml"]
    await hass.async_block_till_done()
    assert len(reloads) == 1
    assert hass.states.get("automation.from_blueprint").state == "on"


async def test_an_edited_copy_is_said_in_the_log(hass: HomeAssistant, tmp_path: Path, caplog) -> None:
    hass.config.config_dir = str(tmp_path)
    assert await async_install_blueprints(hass) == list(BLUEPRINT_FILES)
    _installed(tmp_path, "price_setpoint.yaml").write_text("blueprint: {name: edited}\n")
    caplog.clear()
    assert await async_install_blueprints(hass) == []
    kept = [record for record in caplog.records if "are kept as they are" in record.getMessage()]
    assert [(record.levelname, "price_setpoint.yaml" in record.getMessage()) for record in kept] == [("INFO", True)]


async def test_a_store_that_cannot_be_saved_does_not_stop_the_install(
    hass: HomeAssistant, tmp_path: Path, monkeypatch, caplog
) -> None:
    async def _fails(self, data) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(blueprint_install.Store, "async_save", _fails)
    hass.config.config_dir = str(tmp_path)
    assert await async_install_blueprints(hass) == list(BLUEPRINT_FILES)
    assert "Could not save SpotNav's blueprint hashes" in caplog.text


async def test_an_unreadable_store_judges_the_copies_as_from_before(
    hass: HomeAssistant, tmp_path: Path, hass_storage: dict[str, Any]
) -> None:
    hass.config.config_dir = str(tmp_path)
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": ["garbage"]}
    assert await async_install_blueprints(hass) == list(BLUEPRINT_FILES)
    assert hass_storage[STORE_KEY]["data"] == {"written": {name: _sha(_shipped(name)) for name in BLUEPRINT_FILES}}


async def test_installs_without_automations_loaded(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    assert "automation" not in hass.config.components
    assert await async_install_blueprints(hass) == list(BLUEPRINT_FILES)


async def test_a_folder_that_cannot_be_written_is_only_logged(hass: HomeAssistant, tmp_path: Path, caplog) -> None:
    (tmp_path / "blueprints").write_text("a file where the folder should be")
    hass.config.config_dir = str(tmp_path)
    assert await async_install_blueprints(hass) == []
    assert "Could not install SpotNav's blueprints" in caplog.text


async def test_the_integration_installs_them_once_as_it_sets_up(hass: HomeAssistant, monkeypatch) -> None:
    calls: list[HomeAssistant] = []

    async def _count(target: HomeAssistant) -> list[str]:
        calls.append(target)
        return []

    monkeypatch.setattr(integration, "async_install_blueprints", _count)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert calls == [hass]


async def test_the_command_counts_the_automations_using_each(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, tmp_path: Path, monkeypatch, stopped_after
) -> None:
    monkeypatch.setattr(integration, "async_install_blueprints", lambda _hass: _none())
    assert await async_setup_component(hass, DOMAIN, {})
    hass.config.config_dir = str(tmp_path)
    request = {"type": "spotnav/get_blueprints", "api_version": 1}

    # Before automations are loaded every count is 0.
    reply = await ws_call(await admin(hass, hass_ws_client), dict(request))
    assert reply["result"] == {
        "api_version": 1,
        "ok": True,
        "error": None,
        "blueprints": [
            {"file": "price_setpoint.yaml", "path": "spotnav/price_setpoint.yaml", "automations": 0},
            {"file": "solar_surplus_switch.yaml", "path": "spotnav/solar_surplus_switch.yaml", "automations": 0},
        ],
    }

    install_blueprints(str(tmp_path))
    await _automation_using(hass, tmp_path, "price_setpoint.yaml")
    reply = await ws_call(await admin(hass, hass_ws_client), dict(request))
    assert [row["automations"] for row in reply["result"]["blueprints"]] == [1, 0]

    denied = await ws_call(await non_admin(hass, hass_ws_client, hass_read_only_access_token), dict(request))
    assert denied["result"] == {"api_version": 1, "ok": False, "error": "spotnav_not_admin", "blueprints": []}
    wrong = await ws_call(await admin(hass, hass_ws_client), {"type": "spotnav/get_blueprints", "api_version": 2})
    assert wrong["success"] is False


async def _none() -> list[str]:
    return []
