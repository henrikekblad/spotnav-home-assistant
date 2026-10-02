"""The one sentence that says how to add a charger to a site that has none, in the words Home Assistant's
own pages use. Shown by the site's setup step, the site's entity, its Repairs issue and the card."""

from __future__ import annotations

from typing import Final

ADD_CHARGER_HINT: Final[dict[str, str]] = {
    "en": "Add a charger: Settings \u2192 Devices & services \u2192 SpotNav \u2192 Add entry \u2192 Charger; "
    "it will offer to join this site.",
    "sv": "L\u00e4gg till en laddare: Inst\u00e4llningar \u2192 Enheter och tj\u00e4nster \u2192 SpotNav \u2192 L\u00e4gg till post "
    "\u2192 Laddare; den erbjuder sig att ansluta till den h\u00e4r anl\u00e4ggningen.",
}
