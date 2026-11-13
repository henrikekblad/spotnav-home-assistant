"""Fetching the upstream sources and checking the expectations against them."""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .expectations import KIND_FRAGMENT, KIND_KEY, KIND_SERVICE, KIND_VALUE, Expectation
from .reviewed import REVIEWED
from .upstreams import NOT_COVERED, UPSTREAMS, Source

#: Text files that can name an entity: code, translations, service files, manifests.
_SUFFIXES = frozenset({".py", ".json", ".yaml", ".yml"})
_SKIP_DIRS = frozenset({"tests", "test", ".git", "node_modules", "docs", "__pycache__", ".github"})
_MAX_FILE = 3_000_000
_CORE_URL = "https://github.com/home-assistant/core"


class FetchError(Exception):
    pass


def _git(args: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600, env={"GIT_TERMINAL_PROMPT": "0", "PATH": _path()}
    )
    if result.returncode:
        raise FetchError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "git failed")


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def fetch_repo(repo: str, cache: Path) -> Path:
    """A shallow clone of `owner/name` under `cache` (reused when it is there)."""
    target = cache / repo.replace("/", "__")
    if not (target / ".git").exists():
        _git(["clone", "--depth", "1", "--quiet", f"https://github.com/{repo}", str(target)])
    return target


def fetch_core(domains: list[str], cache: Path) -> Path:
    """home-assistant/core, sparse: only the folders of `domains`."""
    target = cache / "home-assistant__core"
    if not (target / ".git").exists():
        _git(["clone", "--depth", "1", "--filter=blob:none", "--sparse", "--quiet", _CORE_URL, str(target)])
    _git(["sparse-checkout", "set", "--no-cone", *[f"/homeassistant/components/{d}/" for d in domains]], cwd=target)
    return target


def _read_tree(root: Path) -> str:
    parts: list[str] = []
    if not root.exists():
        return ""
    for path in sorted(root.rglob("*")):
        if path.suffix not in _SUFFIXES or not path.is_file():
            continue
        if _SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        # Other languages' translations only repeat the English keys.
        if path.parent.name == "translations" and path.stem != "en":
            continue
        try:
            if path.stat().st_size <= _MAX_FILE:
                parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts).lower()


@dataclass
class Index:
    """The searchable text of one integration's source."""

    text: str
    #: The integration is in the source at all (a core folder that has gone, or a custom one without a manifest).
    present: bool
    services: frozenset[str] = frozenset()

    def has_word(self, word: str) -> bool:
        # A unique id is often built as f"{prefix}_{key}": the key may follow an underscore but must end the word.
        return re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9_])", self.text) is not None

    def has_key(self, key: str) -> bool:
        """The key as written, or as the name an integration slugifies into its unique id ("Session Energy")."""
        if self.has_word(key):
            return True
        spaced = re.escape(key.lower().replace("_", " "))
        return "_" in key and re.search(rf"[\"']{spaced}[\"']", self.text) is not None

    def has_fragment(self, fragment: str) -> bool:
        return fragment.lower() in self.text

    def has_value(self, value: str) -> bool:
        words = re.split(r"[\s_'\-]+", value.lower().strip())
        pattern = r"[\s_'\-]+".join(re.escape(w) for w in words if w)
        return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", self.text) is not None

    def has_service(self, service: str) -> bool:
        return service in self.services or self.has_word(service)


def build_index(domain: str, source: Source, roots: dict[str, Path]) -> Index:
    if source.repo == "core":
        folder = roots["core"] / "homeassistant" / "components" / domain
        text = _read_tree(folder)
        present = (folder / "manifest.json").exists()
        services = _service_names(folder / "services.yaml")
    else:
        base = roots[source.repo] / source.path if source.path else roots[source.repo]
        text = _read_tree(base)
        present = bool(text)
        services = _service_names(*base.rglob("services.yaml"))
    for library in source.libraries:
        text += "\n" + _read_tree(roots[library])
    return Index(text, present, services)


def _service_names(*paths: Path) -> frozenset[str]:
    names: set[str] = set()
    for path in paths:
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if re.match(r"^[a-z][a-z0-9_]*:\s*$", line):
                    names.add(line.split(":")[0])
    return frozenset(names)


@dataclass
class Report:
    missing: list[Expectation] = field(default_factory=list)
    gone: list[tuple[str, str]] = field(default_factory=list)  # (domain, where it should be)
    fetch_errors: list[tuple[str, str]] = field(default_factory=list)
    uncovered: dict[str, str] = field(default_factory=dict)  # domain -> why
    unmapped: list[str] = field(default_factory=list)  # domains with neither a source nor a reason
    checked: dict[str, int] = field(default_factory=dict)  # domain -> expectations read
    reviewed: list[Expectation] = field(default_factory=list)  # misses accepted in `reviewed.py`
    stale: list[tuple[str, str, str]] = field(default_factory=list)  # reviewed entries SpotNav no longer has

    @property
    def drift(self) -> bool:
        return bool(self.missing or self.gone or self.unmapped)


def evaluate(expectations: list[Expectation], indexes: dict[str, Index]) -> Report:
    report = Report()
    for expectation in expectations:
        domain = expectation.domain
        if domain not in UPSTREAMS:
            if domain in NOT_COVERED:
                report.uncovered[domain] = NOT_COVERED[domain]
            elif domain not in report.unmapped:
                report.unmapped.append(domain)
            continue
        index = indexes.get(domain)
        if index is None:
            continue
        report.checked[domain] = report.checked.get(domain, 0) + 1
        if not index.present:
            if (domain, UPSTREAMS[domain].repo) not in report.gone:
                report.gone.append((domain, UPSTREAMS[domain].repo))
            continue
        test = {
            KIND_KEY: index.has_key,
            KIND_FRAGMENT: index.has_fragment,
            KIND_SERVICE: index.has_service,
            KIND_VALUE: index.has_value,
        }.get(expectation.kind)
        found = True if test is None else any(test(name) for name in expectation.names)  # "integration": checked above
        if found:
            continue
        accepted = REVIEWED.get(expectation.ident)
        if accepted is not None and accepted.holds(index):
            report.reviewed.append(expectation)
        else:
            report.missing.append(expectation)
    present = {e.ident for e in expectations}
    report.stale = sorted(ident for ident in REVIEWED if ident not in present)
    return report


def format_report(report: Report) -> str:
    lines: list[str] = []
    by_domain: dict[str, list[Expectation]] = defaultdict(list)
    for item in report.missing:
        by_domain[item.domain].append(item)
    for domain, repo in report.gone:
        lines.append(f"GONE  {domain}: no integration found in {repo}")
    for domain in sorted(by_domain):
        source = UPSTREAMS[domain]
        lines.append(f"\n{domain}  ({source.repo}{', ' + ', '.join(source.libraries) if source.libraries else ''})")
        for item in sorted(by_domain[domain], key=lambda e: (e.kind, e.name)):
            shown = f"one of {', '.join(item.names)}" if len(item.names) > 1 else item.name
            lines.append(f"  missing {item.kind:8} {shown}   <- {item.origin}")
    for domain in report.unmapped:
        lines.append(f"UNMAPPED  {domain}: SpotNav names it but tools/upstream_drift/upstreams.py has no source for it")
    if report.fetch_errors:
        lines.append("\nCould not fetch (not drift):")
        for name, why in report.fetch_errors:
            lines.append(f"  {name}: {why}")
    if report.stale:
        lines.append("\nreviewed.py entries SpotNav no longer relies on (remove them):")
        lines.extend(f"  {d} {k} {n}" for d, k, n in report.stale)
    checked = sum(report.checked.values())
    lines.append(
        f"\n{checked} expectations over {len(report.checked)} integrations; "
        f"{len(report.missing)} missing, {len(report.gone)} gone, {len(report.unmapped)} unmapped, "
        f"{len(report.reviewed)} accepted as reviewed, "
        f"{len(report.uncovered)} named but not covered."
    )
    return "\n".join(lines)
