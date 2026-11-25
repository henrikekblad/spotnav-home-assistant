"""`get_strings` (API v1): the card's words in one language, for the paired app, so the app and the card say the same.

The words are the card's own built language files, `www/i18n/<lang>.<hash>.json` (`card_asset.py`; written by
`frontend/scripts/build.mjs`, English included for the app), never a second copy. Over the WebSocket
(`spotnav/get_strings`) and on both webhooks (the charger's and the home's, `action: "get_strings"`); any signed-in
user or paired app may read (the words are no secret).

Request: `{api_version?: 1, language, known_hash?}`. `language` is a Home Assistant or Android language tag
(`sv`, `sv-SE`, `nb_NO`); it resolves as the card's `resolveLanguage` does (`no` and `nn` are `nb`, anything the
card has no words for is English). `known_hash` is the hash the caller already holds for that language: when it is
still the current one the answer says `unchanged: true` and leaves the words out.

    {api_version: 1, language: "sv", hash: "4635a84f", unchanged: false, strings: {"<key>": "<text>", ...}}

`hash` is the file's own content hash (the file name's), so it changes with any wording; `strings` is `null` when
`unchanged`. Texts carry the card's placeholders (`{name}`) and plural keys as the card uses them.

Refusals: `spotnav_unsupported_api_version`, `spotnav_invalid_value` (`language` or `known_hash` not a string;
webhook 400) and `spotnav_strings_unavailable` (no language file in the package, a broken install; webhook 404).

The files are read off the event loop, once per file name (a name is its content, so it never goes stale).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..card_asset import LANGUAGE_DIR_PATH
from .common import ERROR_UNSUPPORTED_VERSION, send_unsupported_version

STRINGS_API_VERSION: Final = 1
#: The webhook action for the same.
WEBHOOK_ACTION: Final = "get_strings"

ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"
ERROR_STRINGS_UNAVAILABLE: Final = "spotnav_strings_unavailable"

#: The card's languages (`frontend/src/i18n/index.ts:LANGUAGES`); English is the fallback.
LANGUAGES: Final = ("en", "sv", "nb", "da", "fi", "de", "nl", "fr", "es")
FALLBACK_LANGUAGE: Final = "en"

_FILE_NAME: Final = re.compile(r"(?P<language>[a-z]{2})\.(?P<hash>[0-9a-f]{8})\.json")

#: Parsed files by path. A file's name is its content's hash, so an entry never goes stale.
_PARSED: dict[Path, dict[str, str]] = {}


class StringsRefusal(Exception):
    """A request the answer cannot honour; `code` is its stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_language(language: str) -> str:
    """The card's language for a tag, as the card's `resolveLanguage`: base subtag, `no`/`nn` as `nb`, else English."""
    base = re.split(r"[-_]", language.strip().lower(), maxsplit=1)[0].strip()
    if base in ("no", "nn"):
        base = "nb"
    return base if base in LANGUAGES else FALLBACK_LANGUAGE


def _language_file(directory: Path, language: str) -> Path | None:
    """The one built file of a language in `directory`, `None` when there is none. Blocking."""
    try:
        names = [
            path
            for path in directory.iterdir()
            if (match := _FILE_NAME.fullmatch(path.name)) is not None and match["language"] == language
        ]
    except OSError:
        return None
    # The build keeps one per language; were there more, the newest wins.
    return max(names, key=lambda path: path.stat().st_mtime, default=None)


def _read_words(path: Path) -> dict[str, str] | None:
    """A language file's `{key: text}`, `None` when it cannot be read or is not that shape. Blocking."""
    try:
        words = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(words, dict) or not all(
        isinstance(key, str) and isinstance(text, str) for key, text in words.items()
    ):
        return None
    return words


def read_language(directory: Path, language: str) -> tuple[str, str, dict[str, str]] | None:
    """`(language served, hash, words)` of a language's built file, English when it has none (or it cannot be read);
    `None` without either. Blocking: run it in an executor job."""
    for candidate in dict.fromkeys((language, FALLBACK_LANGUAGE)):
        path = _language_file(directory, candidate)
        if path is None:
            continue
        words = _PARSED.get(path)
        if words is None:
            words = _read_words(path)
            if words is None:
                continue
            _PARSED[path] = words
        match = _FILE_NAME.fullmatch(path.name)
        assert match is not None
        return candidate, match["hash"], words
    return None


def parse_request(request: dict[str, Any]) -> tuple[str, str | None]:
    """`(the card's language, known_hash)` of a request, or a `StringsRefusal`."""
    language = request.get("language")
    known_hash = request.get("known_hash")
    if not isinstance(language, str) or not language.strip() or len(language) > 35:
        raise StringsRefusal(ERROR_INVALID_VALUE, "language must be a language tag")
    if known_hash is not None and not isinstance(known_hash, str):
        raise StringsRefusal(ERROR_INVALID_VALUE, "known_hash must be a string")
    return resolve_language(language), known_hash


async def async_strings_answer(hass: HomeAssistant, request: dict[str, Any]) -> dict[str, Any]:
    """The answer for one request (WebSocket and webhook alike), or a `StringsRefusal`."""
    language, known_hash = parse_request(request)
    found = await hass.async_add_executor_job(read_language, LANGUAGE_DIR_PATH, language)
    if found is None:
        raise StringsRefusal(ERROR_STRINGS_UNAVAILABLE, "The card's language files are missing")
    served, words_hash, words = found
    unchanged = known_hash == words_hash
    return {
        "api_version": STRINGS_API_VERSION,
        "language": served,
        "hash": words_hash,
        "unchanged": unchanged,
        "strings": None if unchanged else words,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_strings",
        vol.Optional("api_version"): object,
        vol.Optional("language"): object,
        vol.Optional("known_hash"): object,
    }
)
@websocket_api.async_response
async def websocket_get_strings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The card's words in one language."""
    version = msg.get("api_version")
    if type(version) is not int or version != STRINGS_API_VERSION:
        send_unsupported_version(connection, msg, STRINGS_API_VERSION)
        return
    try:
        answer = await async_strings_answer(hass, msg)
    except StringsRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


_WEBHOOK_STATUS: Final = {ERROR_STRINGS_UNAVAILABLE: 404}


async def async_webhook_strings(hass: HomeAssistant, _entry: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
    """`get_strings` on either webhook (the entry is not read: the words are the installation's): HTTP 200 with the
    answer and the routing `action`, else the refusal's code (400, or 404 for missing files)."""
    version = payload.get("api_version", STRINGS_API_VERSION)
    if type(version) is not int or version != STRINGS_API_VERSION:
        return web.json_response({"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": WEBHOOK_ACTION}, status=400)
    try:
        answer = await async_strings_answer(hass, payload)
    except StringsRefusal as refusal:
        return web.json_response(
            {"ok": False, "error": refusal.code, "action": WEBHOOK_ACTION},
            status=_WEBHOOK_STATUS.get(refusal.code, 400),
        )
    return web.json_response({"ok": True, "action": WEBHOOK_ACTION, **answer})


@callback
def async_setup_strings_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_strings)
