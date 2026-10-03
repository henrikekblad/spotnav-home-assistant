"""Start Home Assistant with the relay's address redirected to the local stub.

The integration fetches prices from a fixed public address. This launcher, used only by the screenshot
tool, rewrites requests to that address so they reach `RELAY_STUB_URL`; the integration itself is not
touched. Usage: `python ha_launch.py -c <config dir>`.
"""

from __future__ import annotations

import os
import sys

import aiohttp

RELAY = "https://spotnav.sensnology.se"
STUB = os.environ.get("RELAY_STUB_URL", "http://127.0.0.1:8130")

_request = aiohttp.ClientSession._request


async def _redirected(self, method, str_or_url, *args, **kwargs):
    url = str(str_or_url)
    if url.startswith(RELAY):
        str_or_url = STUB + url[len(RELAY):]
    return await _request(self, method, str_or_url, *args, **kwargs)


aiohttp.ClientSession._request = _redirected

from homeassistant.__main__ import main  # noqa: E402

if os.environ.get("DOCS_SEED_SESSIONS"):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import seed_sessions  # noqa: E402

    seed_sessions.install()

if __name__ == "__main__":
    sys.exit(main())
