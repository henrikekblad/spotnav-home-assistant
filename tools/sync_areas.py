"""Write `custom_components/spotnav/pricing/areas.json` from the relay's own area table.

The file is generated, never edited by hand. Run from the repository root:

    python tools/sync_areas.py                      # a spotnav-relay checkout next to this repository
    python tools/sync_areas.py --relay PATH         # another checkout

It runs `go run ./cmd/spotnav-areas` in the relay checkout (Go must be installed), checks the result with the
integration's own parser, and writes it with areas sorted by id. When nothing but the `generated` stamp would
change, the file is left as it is, so a sync without changes makes no diff.

It refuses to write when an area the current file lists is gone, or has lost a route (its Nord Pool code, its
ENTSO-E route, its Octopus region or its PVPC tariff) or its CET calendar, unless `--allow-removal` is given: a
lost route is a provider that stops serving people who chose that area, which must be a decision, not a side
effect.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.spotnav.pricing.area_catalogue import BUNDLED_PATH, parse_bundled  # noqa: E402

DEFAULT_RELAY = ROOT.parent / "spotnav-relay"
ROUTE_KEYS = ("nordpool", "entsoe", "octopus", "pvpc", "cet_market_tz")


def generate(relay: Path) -> dict[str, Any]:
    """The relay command's output, as a document."""
    result = subprocess.run(
        ["go", "run", "./cmd/spotnav-areas"],
        cwd=relay,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def lost_routes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Every area or route the old file had and the new one lacks, as readable lines."""
    new_areas = {area["id"]: area for area in new["areas"]}
    problems: list[str] = []
    for area in old["areas"]:
        area_id = area["id"]
        if area_id not in new_areas:
            problems.append(f"{area_id}: the area is gone")
            continue
        old_routes = area.get("routes") or {}
        new_routes = new_areas[area_id].get("routes") or {}
        problems.extend(
            f"{area_id}: lost its {key} route" for key in ROUTE_KEYS if old_routes.get(key) and not new_routes.get(key)
        )
    return problems


def render(document: dict[str, Any]) -> str:
    document = {**document, "areas": sorted(document["areas"], key=lambda area: area["id"])}
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--relay", type=Path, default=DEFAULT_RELAY, help="a spotnav-relay checkout")
    parser.add_argument("--allow-removal", action="store_true", help="write even when an area or route is lost")
    args = parser.parse_args(argv)

    new = generate(args.relay)
    # The integration's own reader must accept it, routes included.
    parse_bundled(new)

    old = json.loads(BUNDLED_PATH.read_text(encoding="utf-8")) if BUNDLED_PATH.exists() else None
    if old is not None:
        problems = lost_routes(old, new)
        if problems and not args.allow_removal:
            print("Refusing to write; the new catalogue loses:", *problems, sep="\n  ", file=sys.stderr)
            return 1
        if render({**new, "generated": old["generated"]}) == render(old):
            print(f"{BUNDLED_PATH.relative_to(ROOT)} is up to date")
            return 0
    BUNDLED_PATH.write_text(render(new), encoding="utf-8")
    print(f"Wrote {BUNDLED_PATH.relative_to(ROOT)}: {len(new['areas'])} areas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
