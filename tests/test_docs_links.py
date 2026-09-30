"""The documentation's relative links resolve, and the screenshots still to come are listed.

A missing screenshot does not fail the suite: the owner adds the files later, and the pages keep
their image lines meanwhile. Run with `-s` to see the list.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _targets(page: Path) -> list[tuple[str, str]]:
    kinds = []
    for match in LINK.finditer(page.read_text(encoding="utf-8")):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        kinds.append(("image" if match.group(0).startswith("!") else "link", target))
    return kinds


def _resolve(page: Path, target: str) -> Path:
    return (page.parent / target.split("#", 1)[0]).resolve()


def _inside(page: Path, target: str) -> bool:
    """A link that leaves the repository (`../../../issues/...`) is a GitHub address, not a file."""
    return ROOT in _resolve(page, target).parents


def test_relative_links_resolve() -> None:
    broken = [
        f"{page.relative_to(ROOT)}: {target}"
        for page in PAGES
        for kind, target in _targets(page)
        if kind == "link" and _inside(page, target) and not _resolve(page, target).exists()
    ]
    assert not broken, broken


def test_list_screenshots_that_do_not_exist_yet() -> None:
    missing: dict[str, list[str]] = {}
    for page in PAGES:
        for kind, target in _targets(page):
            if kind == "image" and not _resolve(page, target).exists():
                missing.setdefault(Path(target).name, []).append(str(page.relative_to(ROOT)))
    for name in sorted(missing):
        print(f"missing screenshot docs/images/{name}  (used in {', '.join(sorted(set(missing[name])))})")
    print(f"{len(missing)} referenced screenshots do not exist yet")
