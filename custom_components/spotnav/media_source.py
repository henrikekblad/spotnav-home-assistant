"""SpotNav's media source: only the pictures lent to the AI Task during a camera query (`vehicles/camera_media.py`).

Browsing it lists nothing; an id resolves to a local file only while it is lent.
"""

from __future__ import annotations

from homeassistant.components.media_player import MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .vehicles.camera_media import lent_path


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return SpotnavMediaSource(hass)


class SpotnavMediaSource(MediaSource):
    name = "SpotNav"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        path = lent_path(self.hass, item.identifier)
        if path is None:
            raise Unresolvable("Not available")
        return PlayMedia(url="", mime_type="image/jpeg", path=path)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        if item.identifier:
            raise Unresolvable("Not available")
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title="SpotNav",
            can_play=False,
            can_expand=True,
            children=[],
            children_media_class=MediaClass.IMAGE,
        )
