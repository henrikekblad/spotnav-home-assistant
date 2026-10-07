"""SpotNav's own words, one JSON file per language in `i18n/<lang>.json`.

Home Assistant's `translations/<lang>.json` carries what Home Assistant itself shows (flow titles,
field labels, entity names, issues, exceptions) and has a fixed schema. Everything SpotNav words in
code (notifications, dropdown labels, flow summaries) lives here instead, as `namespace -> key ->
text`. English is complete; any other language falls back to English key by key, so a language
file may be partial while it is being translated.

The files are read once, in an executor (`async_load`), by the integration's setup and by the
config flow's first step. A read before that (a test calling a helper directly) loads them
synchronously as a last resort. Pure otherwise: no Home Assistant imports.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

_LOGGER = logging.getLogger(__name__)

FOLDER: Final = Path(__file__).parent / "i18n"
FALLBACK: Final = "en"

#: language -> namespace -> key -> text, English already merged under every language.
_catalog: dict[str, dict[str, Mapping[str, str]]] | None = None


def read_files(folder: Path = FOLDER) -> dict[str, dict[str, dict[str, str]]]:
    """Every `<lang>.json` in `folder`, as read (no fallback merged)."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.json"))
    }


def _merge(files: dict[str, dict[str, dict[str, str]]]) -> dict[str, dict[str, Mapping[str, str]]]:
    english = files[FALLBACK]
    return {
        language: {
            namespace: MappingProxyType({**texts, **own.get(namespace, {})})
            for namespace, texts in english.items()
        }
        for language, own in files.items()
    }


def load() -> None:
    """Read the files (blocking: run it in an executor)."""
    global _catalog
    if _catalog is None:
        _catalog = _merge(read_files())


async def async_load(hass: Any) -> None:
    """Read the files once, off the event loop."""
    if _catalog is None:
        await hass.async_add_executor_job(load)


def is_loaded() -> bool:
    return _catalog is not None


def _loaded() -> dict[str, dict[str, Mapping[str, str]]]:
    if _catalog is None:
        _LOGGER.debug("SpotNav's texts read before setup loaded them; reading them now")
        load()
    assert _catalog is not None
    return _catalog


def languages() -> tuple[str, ...]:
    """The languages there is a file for, English first."""
    return (FALLBACK, *sorted(language for language in _loaded() if language != FALLBACK))


def language_of(configured: str | None) -> str:
    """One of `languages()` for Home Assistant's configured language (`sv-SE`, `nb`, `no`, `nn`, ...)."""
    code = (configured or FALLBACK).lower().replace("_", "-").split("-")[0]
    if code in ("no", "nn"):
        code = "nb"
    return code if code in _loaded() else FALLBACK


def table(language: str, namespace: str) -> Mapping[str, str]:
    """One namespace's texts in `language` (English for an unknown language or a missing key)."""
    catalog = _loaded()
    return catalog.get(language, catalog[FALLBACK])[namespace]


def hass_table(hass: Any, namespace: str) -> Mapping[str, str]:
    """`table` in Home Assistant's configured language."""
    return table(language_of(getattr(hass.config, "language", None)), namespace)
