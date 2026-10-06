"""The pictures lent to the AI Task while one camera query runs, by an unguessable media-source id.

`ai_task.generate_data` takes its attachments as media-source ids that resolve to a local file. SpotNav's
media source (`media_source.py`) resolves only the ids lent here, for as long as the query runs: the crop of the
snapshot (a temporary file in SpotNav's private storage, deleted afterwards) and the cars' reference pictures
(their stored files). It lists nothing when browsed, and an id that is not lent, or no longer, resolves to
nothing.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from ..const import DOMAIN

_LENT: Final[HassKey[dict[str, Path]]] = HassKey(f"{DOMAIN}_camera_media")


def lend(hass: HomeAssistant, path: Path) -> tuple[str, str]:
    """Lend `path` to the AI Task: (its media-source id, the token to take it back with)."""
    token = secrets.token_urlsafe(24)
    hass.data.setdefault(_LENT, {})[token] = path
    return f"media-source://{DOMAIN}/{token}", token


def take_back(hass: HomeAssistant, token: str) -> None:
    hass.data.setdefault(_LENT, {}).pop(token, None)


def lent_path(hass: HomeAssistant, token: str) -> Path | None:
    """The file a lent id stands for, or `None`."""
    return hass.data.get(_LENT, {}).get(token)
