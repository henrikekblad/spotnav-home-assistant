"""`get_strings` (`api/strings.py`): the card's built words in one language, for the paired app.

The answers over the webhook are pinned under `tests/fixtures/webhook/get_strings_*.json` from a small language
directory of their own (`SPOTNAV_WRITE_FIXTURES=1` rewrites them), so a wording change in the card never moves a
fixture; the app's tests read the same files. The package's own files are checked against the card's source.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import strings
from custom_components.spotnav.const import CONF_WEBHOOK_ID

from .test_webhook_parity import _pinned, post
from .world import admin, non_admin, setup_site_with_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

SOURCE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n"

#: The words of the pinned answers: a plain text, one with a placeholder, and a plural pair.
WORDS: dict[str, dict[str, str]] = {
    "en": {
        "house.title": "House",
        "heat.measured": "Indoors {value}",
        "learning.daysLeft.one": "{days} day left",
        "learning.daysLeft.other": "{days} days left",
    },
    "sv": {
        "house.title": "Huset",
        "heat.measured": "Inne {value}",
        "learning.daysLeft.one": "{days} dygn kvar",
        "learning.daysLeft.other": "{days} dygn kvar",
    },
}


def _write(directory: Path, words: dict[str, dict[str, str]]) -> dict[str, str]:
    """Language files as the build writes them; their hashes by language."""
    directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for language, table in words.items():
        content = f"{json.dumps(table, separators=(',', ':'), ensure_ascii=False)}\n".encode()
        digest = hashlib.sha256(content).hexdigest()[:8]
        (directory / f"{language}.{digest}.json").write_bytes(content)
        hashes[language] = digest
    return hashes


@pytest.fixture
def small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(strings, "LANGUAGE_DIR_PATH", tmp_path / "i18n")
    return _write(tmp_path / "i18n", WORDS)


def test_a_language_tag_resolves_as_the_card_resolves_it() -> None:
    assert strings.resolve_language("sv") == "sv"
    assert strings.resolve_language("sv-SE") == "sv"
    assert strings.resolve_language("nb_NO") == "nb"
    assert strings.resolve_language("no") == "nb"
    assert strings.resolve_language("nn-NO") == "nb"
    assert strings.resolve_language(" DE ") == "de"
    assert strings.resolve_language("pl") == "en"
    assert strings.resolve_language("") == "en"


def test_every_language_of_the_package_is_the_cards_own_words() -> None:
    for language in strings.LANGUAGES:
        found = strings.read_language(strings.LANGUAGE_DIR_PATH, language)
        assert found is not None, language
        served, words_hash, words = found
        assert served == language
        assert words == json.loads((SOURCE / f"{language}.json").read_text(encoding="utf-8")), language
        assert (strings.LANGUAGE_DIR_PATH / f"{language}.{words_hash}.json").is_file()


def test_a_language_without_a_file_is_english_and_no_file_at_all_is_none(tmp_path: Path) -> None:
    _write(tmp_path, {"en": WORDS["en"]})
    served, _hash, words = strings.read_language(tmp_path, "sv") or ("", "", {})
    assert (served, words) == ("en", WORDS["en"])
    assert strings.read_language(tmp_path / "absent", "sv") is None
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "en.00000000.json").write_text("[1, 2]", encoding="utf-8")
    assert strings.read_language(tmp_path / "broken", "en") is None


async def test_the_files_are_read_off_the_event_loop(
    hass: HomeAssistant, small: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    loop_thread = threading.get_ident()
    real = strings.read_language

    def spy(*args: Any) -> Any:
        seen.append(threading.get_ident() != loop_thread)
        return real(*args)

    monkeypatch.setattr(strings, "read_language", spy)
    answer = await strings.async_strings_answer(hass, {"language": "sv"})
    assert seen == [True]
    assert answer["hash"] == small["sv"]


async def test_the_webhooks_answer_the_words_and_unchanged_for_the_hash_held(
    hass: HomeAssistant, hass_client_no_auth, small: dict[str, str]
) -> None:
    charger, site = await setup_site_with_charger(hass, charger_entry_id="entry_strings")
    client = await hass_client_no_auth()
    status, body = await post(client, charger.data[CONF_WEBHOOK_ID], {"action": "get_strings", "language": "sv-SE"})
    assert status == 200
    assert body == {
        "ok": True,
        "action": "get_strings",
        "api_version": 1,
        "language": "sv",
        "hash": small["sv"],
        "unchanged": False,
        "strings": WORDS["sv"],
    }
    _pinned("get_strings_success.json", body)
    # The home's webhook answers the same, with or without a charger named.
    for extra in ({}, {"charger_id": charger.entry_id}):
        again = await post(client, site.data[CONF_WEBHOOK_ID], {"action": "get_strings", "language": "sv-SE", **extra})
        assert again == (200, body), extra

    status, body = await post(
        client, site.data[CONF_WEBHOOK_ID], {"action": "get_strings", "language": "sv", "known_hash": small["sv"]}
    )
    assert status == 200 and body["unchanged"] is True and body["strings"] is None
    _pinned("get_strings_unchanged.json", body)

    status, body = await post(client, site.data[CONF_WEBHOOK_ID], {"action": "get_strings", "language": "pl"})
    assert status == 200 and body["language"] == "en" and body["strings"] == WORDS["en"]
    _pinned("get_strings_english.json", body)


async def test_the_webhooks_refuse_a_bad_request(
    hass: HomeAssistant, hass_client_no_auth, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _charger, site = await setup_site_with_charger(hass, charger_entry_id="entry_strings_bad")
    client = await hass_client_no_auth()
    hook = site.data[CONF_WEBHOOK_ID]
    for request, code in (
        ({"language": "sv", "api_version": 2}, "spotnav_unsupported_api_version"),
        ({}, "spotnav_invalid_value"),
        ({"language": 7}, "spotnav_invalid_value"),
        ({"language": "sv", "known_hash": 1}, "spotnav_invalid_value"),
    ):
        status, body = await post(client, hook, {"action": "get_strings", **request})
        assert (status, body) == (400, {"ok": False, "error": code, "action": "get_strings"}), request
    monkeypatch.setattr(strings, "LANGUAGE_DIR_PATH", tmp_path / "absent")
    status, body = await post(client, hook, {"action": "get_strings", "language": "sv"})
    assert (status, body) == (404, {"ok": False, "error": "spotnav_strings_unavailable", "action": "get_strings"})


async def test_the_socket_answers_anyone_signed_in(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, small: dict[str, str]
) -> None:
    await setup_site_with_charger(hass, charger_entry_id="entry_strings_ws")
    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    frame = await ws_call(reader, {"type": "spotnav/get_strings", "api_version": 1, "language": "sv"})
    assert frame["success"] is True
    assert frame["result"] == {
        "api_version": 1,
        "language": "sv",
        "hash": small["sv"],
        "unchanged": False,
        "strings": WORDS["sv"],
    }
    socket = await admin(hass, hass_ws_client)
    frame = await ws_call(socket, {"type": "spotnav/get_strings", "language": "sv"})
    assert frame["success"] is False and frame["error"]["code"] == "spotnav_unsupported_api_version"
    frame = await ws_call(socket, {"type": "spotnav/get_strings", "api_version": 1})
    assert frame["success"] is False and frame["error"]["code"] == "spotnav_invalid_value"
