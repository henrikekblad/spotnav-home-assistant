"""A local stand-in for the ECB's reference rates, the one thing the screenshot run would otherwise fetch.

    python rates_stub.py [port]

Prices come from the demo Nord Pool integration (`demo_components/nordpool`); an area priced in another currency
than EUR (SE4: SEK) is converted with the ECB's rate, which SpotNav reads from the ECB's daily and 90-day files.
This serves the recorded daily file (`tests/fixtures/ecb/ecb_daily.xml`) for both, dated yesterday, so the run
needs no network. `ha_launch.py` sends the ECB's address here.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ecb" / "ecb_daily.xml"
PATHS = ("/stats/eurofxref/eurofxref-daily.xml", "/stats/eurofxref/eurofxref-hist-90d.xml")


def rates() -> bytes:
    yesterday = datetime.now(ZoneInfo("Europe/Berlin")).date() - timedelta(days=1)
    text = FIXTURE.read_text(encoding="utf-8")
    return re.sub(r"time='\d{4}-\d{2}-\d{2}'", f"time='{yesterday.isoformat()}'", text).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] not in PATHS:
            self.send_response(404)
            self.end_headers()
            return
        data = rates()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8130
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
