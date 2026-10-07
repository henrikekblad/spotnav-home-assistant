"""Every language file says everything English says, nothing more, with the same placeholders.

Two sets of files: Home Assistant's `translations/<lang>.json` (flows, entities, issues, exceptions)
and SpotNav's own `i18n/<lang>.json` (notifications, dropdown labels, flow summaries). A new language
is a new file in each folder; this test then holds it to English.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1] / "custom_components" / "spotnav"
FOLDERS = ("translations", "i18n")
LANGUAGES = ("en", "sv", "da", "nb", "fi", "de", "nl", "fr", "es")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _flat(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flat(value, path))
        else:
            assert isinstance(value, str), path
            flat[path] = value
    return flat


def _read(folder: str, language: str) -> dict[str, str]:
    return _flat(json.loads((ROOT / folder / f"{language}.json").read_text(encoding="utf-8")))


@pytest.mark.parametrize("folder", FOLDERS)
def test_every_language_has_a_file(folder: str) -> None:
    assert {path.stem for path in (ROOT / folder).glob("*.json")} >= set(LANGUAGES)


def _pairs() -> list[tuple[str, str]]:
    return [
        (folder, path.stem)
        for folder in FOLDERS
        for path in sorted((ROOT / folder).glob("*.json"))
        if path.stem != "en"
    ]


@pytest.mark.parametrize(("folder", "language"), _pairs())
def test_a_language_has_exactly_the_english_keys(folder: str, language: str) -> None:
    english, own = _read(folder, "en"), _read(folder, language)
    assert sorted(set(english) - set(own)) == [], "missing"
    assert sorted(set(own) - set(english)) == [], "extra"
    assert [key for key, text in own.items() if not text.strip()] == [], "empty"


@pytest.mark.parametrize(("folder", "language"), _pairs())
def test_a_language_uses_the_english_placeholders(folder: str, language: str) -> None:
    english, own = _read(folder, "en"), _read(folder, language)
    differing = [
        key
        for key, text in own.items()
        if key in english and set(PLACEHOLDER.findall(text)) != set(PLACEHOLDER.findall(english[key]))
    ]
    assert differing == []
