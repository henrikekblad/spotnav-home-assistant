"""A local stand-in for the SpotNav relay, serving the repository's recorded documents for today and tomorrow.

    python relay_stub.py [port]

The `areas.json` and `index.json` fixtures are served as recorded with the index days rewritten; the day
document is the recorded 96-quarter day with its dates moved, and tomorrow's is a rotated, slightly scaled
copy so the two days differ. `STUB_TOMORROW=0` leaves tomorrow out, as the relay does before it publishes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "relay"
ZONE = ZoneInfo("Europe/Stockholm")
AREA = "SE4"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _days() -> list[date]:
    today = datetime.now(ZONE).date()
    days = [today]
    if os.environ.get("STUB_TOMORROW", "1") != "0":
        days.append(today + timedelta(days=1))
    return days


def _offset(day: date) -> str:
    text = datetime(day.year, day.month, day.day, tzinfo=ZONE).strftime("%z")
    return f"{text[:3]}:{text[3:]}"


def index() -> dict:
    doc = _load("index.json")
    now = datetime.now(ZONE)
    doc["generated"] = now.isoformat(timespec="seconds")
    for area in doc["areas"].values():
        area["days"] = [day.isoformat() for day in _days()]
    return doc


def day_document(day: date) -> dict:
    doc = _load("day_SE4_2026-09-22_96.json")
    tomorrow = day > datetime.now(ZONE).date()
    prices = doc["prices"]
    shift = 10 if tomorrow else 0
    scale = 0.85 if tomorrow else 1.0
    doc["prices"] = [round(prices[(i + shift) % len(prices)] * scale, 5) for i in range(len(prices))]
    doc["date"] = day.isoformat()
    doc["start"] = f"{day.isoformat()}T00:00:00{_offset(day)}"
    published = datetime.now(ZONE).replace(microsecond=0) - timedelta(hours=1)
    doc["fx_date"] = (day - timedelta(days=1)).isoformat()
    doc["published"] = published.isoformat()
    doc["retrieved"] = published.isoformat()
    return doc


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = None
        if path == "/v1/areas.json":
            body = _load("areas.json")
        elif path == "/v1/index.json":
            body = index()
        else:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["v1", AREA]:
                try:
                    day = date.fromisoformat(f"{parts[2]}-{parts[3].removesuffix('.json')}")
                except ValueError:
                    day = None
                if day in _days():
                    body = day_document(day)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8130
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
