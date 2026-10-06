#!/usr/bin/env python3
"""Replay the charge-ownership events of a SpotNav debug bundle through today's pure core.

    .venv/bin/python tools/replay_bundle.py bundle.json [--json] [--charger NAME_OR_ID]

For each charger the report has the shadow's counts, its tally per event kind kept across restarts (`coverage`, a
bundle of version 6 or later), the disagreements and drift it recorded, whether today's core decides the recorded
events differently from the recording, and whether it differs from the code that ran. Only the pure core is imported
(no Home Assistant needed, running or installed).
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
import types
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _load_core() -> Any:
    """`core.replay`, without running the integration's `__init__` (which imports Home Assistant)."""
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        ("custom_components.spotnav", ROOT / "custom_components" / "spotnav"),
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(path)]
            sys.modules[name] = package
    return importlib.import_module("custom_components.spotnav.core.replay")


def chargers_of(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in bundle.get("chargers") or () if isinstance(item, dict)]


def shadow_of(charger: dict[str, Any]) -> dict[str, Any] | None:
    controller = (charger.get("diagnostics") or {}).get("controller")
    shadow = controller.get("ownership_shadow") if isinstance(controller, dict) else None
    return shadow if isinstance(shadow, dict) else None


def _mismatch(item: Any) -> dict[str, Any]:
    return {"index": item.index, "event": item.kind, "against": item.against, "recorded": item.recorded,
            "replayed": item.replayed}


def analyse(bundle: dict[str, Any], selector: str | None = None) -> list[dict[str, Any]]:
    """One result per charger (matching `selector` by title or entry id, case-insensitively, when given)."""
    replay = _load_core().replay
    results: list[dict[str, Any]] = []
    for charger in chargers_of(bundle):
        entry_id, title = str(charger.get("entry_id")), charger.get("title")
        if selector is not None and selector.lower() not in (entry_id.lower(), str(title).lower()):
            continue
        result: dict[str, Any] = {"entry_id": entry_id, "title": title}
        shadow = shadow_of(charger)
        if shadow is None:
            result["shadow"] = False
            results.append(result)
            continue
        report = replay(shadow.get("events") or ())
        result.update(
            shadow=True,
            counts=shadow.get("counts") or {},
            coverage=shadow.get("coverage") if isinstance(shadow.get("coverage"), dict) else None,
            session=shadow.get("session"),
            disagreements=shadow.get("disagreements") or [],
            drift=shadow.get("drift") or [],
            events=report.events,
            differs_from_recording=[_mismatch(item) for item in report.differs_from_recording],
            differs_from_today=[_mismatch(item) for item in report.differs_from_today],
        )
        results.append(result)
    return results


def _fields(fields: dict[str, Any]) -> str:
    parts = []
    for name, value in fields.items():
        if isinstance(value, dict) and ("core" in value or "today" in value):
            parts.append(f"{name}: core {value.get('core')}, today {value.get('today')}")
        else:
            parts.append(f"{name}: {value}")
    return "; ".join(parts)


def _coverage_lines(coverage: dict[str, Any] | None) -> list[str]:
    """The tally per event kind: how many kinds were seen, which never were, and every kind with a disagreement,
    drift or error."""
    if coverage is None:
        return ["  coverage: not in this bundle (before version 6)"]
    kinds = coverage.get("kinds") if isinstance(coverage.get("kinds"), dict) else {}
    seen = [kind for kind, counts in kinds.items() if isinstance(counts, dict) and counts.get("events")]
    never = [kind for kind in kinds if kind not in seen]
    lines = [f"  coverage since {coverage.get('since')} ({coverage.get('version')}): {len(seen)} of {len(kinds)} "
             "event kinds seen"]
    if never:
        lines.append("    never seen: " + ", ".join(never))
    for kind, counts in kinds.items():
        if isinstance(counts, dict) and any(counts.get(name) for name in ("disagreements", "drift", "errors")):
            lines.append(f"    {kind}: {counts.get('events')} events, {counts.get('compared')} compared, "
                         f"{counts.get('disagreements')} disagreements, {counts.get('drift')} drift, "
                         f"{counts.get('errors')} errors")
    unattributed = coverage.get("unattributed")
    if isinstance(unattributed, dict) and any(unattributed.values()):
        lines.append("    no event kind: " + ", ".join(f"{k} {v}" for k, v in unattributed.items() if v))
    return lines


def _context(item: dict[str, Any]) -> str:
    events = item.get("events") or []
    if events and isinstance(events[-1], dict):
        return str((events[-1].get("event") or {}).get("kind"))
    return str(item.get("after"))


def render(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No charger in the bundle matches."
    lines: list[str] = []
    for result in results:
        lines.append(f"{result['title'] or result['entry_id']} ({result['entry_id']})")
        if not result["shadow"]:
            lines.append("  No ownership shadow in this charger's diagnostics (a bundle from before version 5, or "
                         "the charger had no controller). Nothing to replay.")
            continue
        counts = result["counts"]
        lines.append("  counts: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "none"))
        lines.extend(_coverage_lines(result["coverage"]))
        lines.append(f"  recorded events replayed: {result['events']}")
        lines.append(f"  disagreements recorded: {len(result['disagreements'])}")
        for item in result["disagreements"]:
            line = f"    {item.get('at')} {_context(item)} at {item.get('where')}: {_fields(item.get('fields') or {})}"
            commands = item.get("commands")
            if commands:
                line += f"; commands core {commands.get('core')}, today {commands.get('today')}"
            lines.append(line)
        lines.append(f"  drift recorded: {len(result['drift'])}")
        for item in result["drift"]:
            lines.append(f"    {item.get('at')} after {item.get('after')}: {_fields(item.get('fields') or {})}")
        recording = result["differs_from_recording"]
        if recording:
            lines.append(f"  today's core decides differently from the recording: yes, {len(recording)} event(s)")
            for item in recording:
                lines.append(f"    #{item['index']} {item['event']}: recorded {item['recorded']}, "
                             f"now {item['replayed']}")
        else:
            lines.append("  today's core decides differently from the recording: no")
        today = result["differs_from_today"]
        if today:
            lines.append(f"  the core disagrees with the code that ran, on replay: yes, {len(today)} event(s)")
            for item in today:
                lines.append(f"    #{item['index']} {item['event']}: today {item['recorded']}, "
                             f"core {item['replayed']}")
        else:
            lines.append("  the core disagrees with the code that ran, on replay: no")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a SpotNav debug bundle's ownership events.")
    parser.add_argument("bundle", help="the debug bundle JSON file (bundle_version 5 or later)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--charger", help="only the charger with this name or entry id")
    args = parser.parse_args(argv)
    try:
        with open(args.bundle, encoding="utf-8") as handle:
            bundle = json.load(handle)
    except (OSError, ValueError) as err:
        print(f"Cannot read {args.bundle}: {err}", file=sys.stderr)
        return 2
    if not isinstance(bundle, dict) or "chargers" not in bundle:
        print("This is not a SpotNav debug bundle (no chargers).", file=sys.stderr)
        return 2
    results = analyse(bundle, args.charger)
    if args.json:
        print(json.dumps({"bundle_version": bundle.get("bundle_version"), "chargers": results}, indent=2))
    else:
        print(render(results))
    if args.charger and not results:
        return 2
    differs = any(r.get("differs_from_recording") or r.get("differs_from_today") for r in results)
    return 1 if differs else 0


if __name__ == "__main__":
    raise SystemExit(main())
