"""Reading an entity's *key* (what the integration calls it), for tie-breaks between look-alikes.

Detection stays structural (domain, device class, unit, range); these helpers only rank entities
that already passed the structural test, by the words in their `translation_key`, `unique_id`,
original name and entity id. No brand is ever matched: the word lists in `vehicle_discovery` are
generic vocabulary shared by many integrations (`soh`, `target`, `v2l`, `capacity`, ...).

camelCase and dotted descriptors (`stateOfCharge.displayed`) are split into words, so one
vocabulary covers snake_case keys, BMW-style descriptors and display names alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from homeassistant.helpers import entity_registry as er

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _NON_ALNUM.split(_CAMEL_BOUNDARY.sub(" ", text).lower()) if t]


@dataclass(frozen=True, slots=True)
class EntityKey:
    """The words of one entity's key: whole-word phrase tests and squashed substring tests."""

    tokens: tuple[str, ...]

    @property
    def squashed(self) -> str:
        return "".join(self.tokens)

    def has(self, phrase: str) -> bool:
        """Whether the words of `phrase` occur contiguously, as whole words (for short words)."""
        wanted = phrase.split()
        size = len(wanted)
        return any(
            list(self.tokens[i : i + size]) == wanted
            for i in range(len(self.tokens) - size + 1)
        )

    def contains(self, fragment: str) -> bool:
        """Whether `fragment` (lowercase, no separators) occurs anywhere, across word borders."""
        return fragment in self.squashed

    def has_any(self, phrases: Iterable[str]) -> bool:
        return any(self.has(phrase) for phrase in phrases)

    def contains_any(self, fragments: Iterable[str]) -> bool:
        return any(self.contains(fragment) for fragment in fragments)


def entity_key(entry: er.RegistryEntry) -> EntityKey:
    """The key words of a registry entry.

    `translation_key`, `unique_id` and the original name, which describe the entity itself. The
    entity id is only a fallback when none of those exist: it starts with the device's name, and a
    car called "Max" or "Target" must not colour every one of its entities.
    """
    parts: list[Any] = [entry.translation_key, entry.unique_id, entry.original_name]
    if not any(isinstance(part, str) and part for part in parts):
        parts = [entry.entity_id.split(".", 1)[-1]]
    words: list[str] = []
    for part in parts:
        if isinstance(part, str) and part:
            words.extend(_tokens(part))
    return EntityKey(tuple(words))
