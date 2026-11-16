#!/usr/bin/env python3
"""Write one SpotNav day record (the `spotnav.export_day` action's file) to InfluxDB 3.

    python3 spotnav_archive_to_influx.py /config/spotnav_archive/<site_id>/<YYYY-MM-DD>.json \\
        --url http://localhost:8181 --db spotnav --token-file /config/.influx_token [--dry-run]

The token is read from Home Assistant's `secrets.yaml` (`--token-secret NAME`), from `--token-file`, or else the environment (`INFLUXDB3_AUTH_TOKEN`); it is sent as a Bearer header
and never printed. `--dry-run` prints the line protocol instead of writing it (no token needed). Only the standard
library is used, so it runs with the Python in Home Assistant's container (for example from a `shell_command`).

Measurements (time in seconds; the same day written again overwrites, as every point keeps its tags and time):

* `spotnav_forecast`, tags `site`, `kind`, `lead_h` (how long before the hour the forecast was made, in buckets
  `0-1`, `1-3`, `3-6`, `6-12`, `12-24`, `24-36`, `36+`); fields `value`, `outcome` (once known) and `made_at` (epoch
  seconds); time: the hour the forecast is for. The log keeps only the first forecast per hour and kind.
* `spotnav_price`, tag `site`; fields `spot`, `buy` and `sell` (minor unit per kWh: öre, cent; buy and sell with the
  fees as set at the export); time: the interval's start.
* `spotnav_review`, per day (time: the local day's start). Tags `site`, `mode`: the site's mode with `saving`,
  `actual_saving`, `battery_saving`, `sun_gave`; every other mode reviewed with its `saving` (minor units, against
  self-consumption). Tags `site`, `kind`: each forecast kind's `mae`, `under_share`, `big_under_share` and `hours`.

Exit status: 0 written, 1 InfluxDB refused or could not be reached, 2 the file or the token could not be read.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
import sys
from typing import Any
from urllib import error, parse, request

SCHEMA = 1
TOKEN_ENV = "INFLUXDB3_AUTH_TOKEN"
#: Upper bounds (hours) of the lead buckets; the last is open.
LEAD_BUCKETS = ((1, "0-1"), (3, "1-3"), (6, "3-6"), (12, "6-12"), (24, "12-24"), (36, "24-36"))
#: Points per request.
BATCH = 5000
TIMEOUT_S = 30


# ------------------------------------------------------------------------------------------------ line protocol


def escape_measurement(name: str) -> str:
    """A measurement name: commas and spaces escaped."""
    return name.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def escape_key(text: str) -> str:
    """A tag key, tag value or field key: commas, equals signs and spaces escaped."""
    return text.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def field_value(value: Any) -> str | None:
    """A field value: a float as is, an int with `i`, a bool as `true`/`false`, a string quoted with `"` and `\\`
    escaped; `None` (left out) for nothing or a number that is not finite."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else None
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def line(measurement: str, tags: dict[str, Any], fields: dict[str, Any], seconds: int) -> str | None:
    """One point, tags sorted; `None` when no field has a value. A tag without a value is left out."""
    shown = [(key, rendered) for key, value in fields.items() if (rendered := field_value(value)) is not None]
    if not shown:
        return None
    tag_text = "".join(
        f",{escape_key(key)}={escape_key(str(value))}" for key, value in sorted(tags.items()) if value not in (None, "")
    )
    field_text = ",".join(f"{escape_key(key)}={rendered}" for key, rendered in shown)
    return f"{escape_measurement(measurement)}{tag_text} {field_text} {seconds}"


def epoch(instant: str) -> int:
    """An ISO 8601 instant with its offset, as whole seconds since the epoch."""
    moment = datetime.fromisoformat(instant)
    if moment.tzinfo is None:
        raise ValueError(f"an instant without its offset: {instant}")
    return int(moment.timestamp())


def lead_bucket(hour: str, made_at: str) -> str:
    """How long before the hour the forecast was made, as a bucket (see the module)."""
    hours = (epoch(hour) - epoch(made_at)) / 3600.0
    for bound, name in LEAD_BUCKETS:
        if hours <= bound:
            return name
    return "36+"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


# ------------------------------------------------------------------------------------------------ the record


def forecast_lines(record: dict[str, Any], site: str) -> list[str]:
    forecasts = record.get("forecasts") or {}
    columns = forecasts.get("columns") or ["hour", "value", "made_at", "outcome"]
    out: list[str] = []
    for kind, rows in (forecasts.get("kinds") or {}).items():
        for row in rows:
            item = dict(zip(columns, row))
            point = line(
                "spotnav_forecast",
                {"site": site, "kind": kind, "lead_h": lead_bucket(item["hour"], item["made_at"])},
                {"value": _number(item.get("value")), "outcome": _number(item.get("outcome")),
                 "made_at": epoch(item["made_at"])},
                epoch(item["hour"]),
            )
            if point is not None:
                out.append(point)
    return out


def price_lines(record: dict[str, Any], site: str) -> list[str]:
    prices = record.get("prices") or {}
    columns = prices.get("columns") or ["start", "end", "spot", "buy", "sell"]
    out: list[str] = []
    for row in prices.get("rows") or []:
        item = dict(zip(columns, row))
        point = line(
            "spotnav_price",
            {"site": site},
            {name: _number(item.get(name)) for name in ("spot", "buy", "sell")},
            epoch(item["start"]),
        )
        if point is not None:
            out.append(point)
    return out


def review_lines(record: dict[str, Any], site: str) -> list[str]:
    seconds = epoch(record["start"])
    out: list[str] = []
    review = record.get("battery_review")
    if isinstance(review, dict):
        baselines = review.get("baselines") or {}
        mode = review.get("mode") or "plan"
        point = line(
            "spotnav_review",
            {"site": site, "mode": mode},
            {
                "saving": _number(review.get("saving")),
                "actual_saving": _number(review.get("actual_saving")),
                "battery_saving": _number(review.get("battery_saving")),
                "sun_gave": _number(baselines.get("sun_gave")) if baselines.get("sun_quarters") else None,
            },
            seconds,
        )
        if point is not None:
            out.append(point)
        for other, saving in sorted((review.get("mode_savings") or {}).items()):
            if other == mode:
                continue
            point = line("spotnav_review", {"site": site, "mode": other}, {"saving": _number(saving)}, seconds)
            if point is not None:
                out.append(point)
    for kind, day in sorted(((record.get("forecasts") or {}).get("reviews") or {}).items()):
        whole = day.get("all") if isinstance(day, dict) else None
        if not isinstance(whole, dict):
            continue
        point = line(
            "spotnav_review",
            {"site": site, "kind": kind},
            {
                "mae": _number(whole.get("mae")),
                "under_share": _number(whole.get("under_share")),
                "big_under_share": _number(whole.get("big_under_share")),
                "hours": whole.get("hours") if isinstance(whole.get("hours"), int) else None,
            },
            seconds,
        )
        if point is not None:
            out.append(point)
    return out


def record_lines(record: dict[str, Any]) -> list[str]:
    """Every point of a day record (see the module)."""
    if not isinstance(record, dict) or record.get("kind") != "spotnav_day":
        raise ValueError("not a SpotNav day record")
    if record.get("schema") != SCHEMA:
        raise ValueError(f"schema {record.get('schema')} is not {SCHEMA}")
    site = str(record.get("site_id"))
    return [*forecast_lines(record, site), *price_lines(record, site), *review_lines(record, site)]


# ------------------------------------------------------------------------------------------------ the write


def secret_value(secrets_path: str, name: str) -> str:
    """One top-level `name: value` from Home Assistant's `secrets.yaml` (plain or quoted scalar), without a YAML library."""
    with open(secrets_path, encoding="utf-8") as handle:
        for raw in handle:
            key, sep, value = raw.partition(":")
            if sep and key == name:
                value = value.split(" #", 1)[0].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                return value
    return ""


def read_token(path: str | None, secret: str | None = None, secrets_path: str = "/config/secrets.yaml") -> str:
    if secret:
        token = secret_value(secrets_path, secret).strip()
    elif path:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    else:
        token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ValueError(f"no token: give --token-file, --token-secret or set {TOKEN_ENV}")
    return token


def write(url: str, db: str, token: str, lines: list[str]) -> None:
    """POST the lines to `/api/v3/write_lp` in batches; raises `error.URLError` (an `HTTPError` when refused)."""
    target = f"{url.rstrip('/')}/api/v3/write_lp?{parse.urlencode({'db': db, 'precision': 'second'})}"
    for first in range(0, len(lines), BATCH):
        body = ("\n".join(lines[first : first + BATCH]) + "\n").encode("utf-8")
        call = request.Request(
            target,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain; charset=utf-8"},
        )
        with request.urlopen(call, timeout=TIMEOUT_S) as answer:  # noqa: S310 - the URL is the user's own
            answer.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a SpotNav day record to InfluxDB 3.")
    parser.add_argument("record", help="the day record JSON file")
    parser.add_argument("--url", default="http://localhost:8181", help="InfluxDB 3 (default %(default)s)")
    parser.add_argument("--db", default="spotnav", help="the database (default %(default)s)")
    parser.add_argument("--token-file", help=f"a file holding the token (else ${TOKEN_ENV})")
    parser.add_argument("--token-secret", help="the name of the token in Home Assistant's secrets.yaml")
    parser.add_argument("--secrets", default="/config/secrets.yaml", help="secrets.yaml (default %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="print the line protocol, write nothing")
    args = parser.parse_args(argv)
    try:
        with open(args.record, encoding="utf-8") as handle:
            lines = record_lines(json.load(handle))
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f"Cannot read {args.record}: {err}", file=sys.stderr)
        return 2
    if args.dry_run:
        print("\n".join(lines))
        return 0
    try:
        token = read_token(args.token_file, args.token_secret, args.secrets)
    except (OSError, ValueError) as err:
        print(f"Cannot read the token: {err}", file=sys.stderr)
        return 2
    try:
        write(args.url, args.db, token, lines)
    except error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        print(f"InfluxDB refused the write: {err.code} {detail}", file=sys.stderr)
        return 1
    except (error.URLError, OSError) as err:
        print(f"InfluxDB could not be reached: {getattr(err, 'reason', err)}", file=sys.stderr)
        return 1
    print(f"Wrote {len(lines)} points from {args.record}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
