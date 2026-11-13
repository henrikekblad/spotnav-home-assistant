#!/usr/bin/env python3
"""Upstream drift canary: do the entity keys, states and services SpotNav relies on still exist upstream?

Reads what SpotNav expects from its own definitions (`tools/upstream_drift/expectations.py`), shallow-clones each
integration it depends on (`upstreams.py`; no token needed, only public repositories) and looks every key, state and
service up in the source. Exit 0: all found. Exit 1: something is missing, gone or unmapped (the report says what
and where SpotNav relies on it). Exit 2: a source could not be fetched and nothing else is wrong.

    python tools/upstream_drift_canary.py [--cache DIR] [--only DOMAIN ...] [--list-coverage]

Needs Home Assistant installed (`pip install -r requirements_test.txt`): SpotNav's modules import its types.
SpotNav is not changed by this: a miss is a prompt to read the integration's change log and decide.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from upstream_drift import check, expectations  # noqa: E402
from upstream_drift.upstreams import NOT_COVERED, UPSTREAMS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, help="where clones are kept (default: a temporary folder)")
    parser.add_argument("--only", nargs="*", metavar="DOMAIN", help="check only these integrations")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--list-coverage", action="store_true", help="print what is read and exit; no network")
    args = parser.parse_args(argv)

    items = expectations.collect(args.repo, frozenset(UPSTREAMS) | frozenset(NOT_COVERED))
    if args.only:
        items = [i for i in items if i.domain in args.only]
    domains = sorted({i.domain for i in items if i.domain in UPSTREAMS})
    if args.list_coverage:
        for domain in domains:
            print(f"{domain:28} {sum(i.domain == domain for i in items):4} expectations  {UPSTREAMS[domain].repo}")
        return 0

    cache_dir = args.cache or Path(tempfile.mkdtemp(prefix="spotnav-canary-"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    roots: dict[str, Path] = {}
    errors: list[tuple[str, str]] = []
    failed: set[str] = set()

    core_domains = [d for d in domains if UPSTREAMS[d].repo == "core"]
    if core_domains:
        try:
            roots["core"] = check.fetch_core(core_domains, cache_dir)
        except check.FetchError as error:
            errors.append(("home-assistant/core", str(error)))
            failed.update(core_domains)
    repos = {r for d in domains for r in (UPSTREAMS[d].repo, *UPSTREAMS[d].libraries) if r != "core"}
    for repo in sorted(repos):
        try:
            roots[repo] = check.fetch_repo(repo, cache_dir)
        except check.FetchError as error:
            errors.append((repo, str(error)))

    indexes: dict[str, check.Index] = {}
    for domain in domains:
        source = UPSTREAMS[domain]
        needed = [r for r in (source.repo, *source.libraries) if r != "core"] + (["core"] if source.repo == "core" else [])
        if domain in failed or any(r not in roots for r in needed):
            continue
        indexes[domain] = check.build_index(domain, source, roots)

    report = check.evaluate(items, indexes)
    report.fetch_errors = errors
    print(check.format_report(report))
    if report.drift:
        return 1
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
