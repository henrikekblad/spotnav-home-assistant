"""The `export_day` service's writing: one site's day record (`day_record.py`) as a JSON file in the configuration.

`<config>/spotnav_archive/<site_id>/<YYYY-MM-DD>.json`, one file per site and local day. The record is built on the
event loop from memory; turning it into JSON and writing it run in the executor. The write is atomic: a temporary
file beside the target, flushed and synced, then renamed over it, so a reader never sees half a file and a failed
write leaves the day's earlier file as it was. Exporting a day again overwrites its file. Nothing else is written,
and nothing runs unless the service is called. A day file is kept `ARCHIVE_KEPT_DAYS` before the day written last (the
older ones go with each write), and a site's folder goes with the site (`remove_archive`).
"""

from __future__ import annotations

from datetime import date, timedelta
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Final

#: The folder in Home Assistant's configuration directory that holds the archive.
ARCHIVE_DIR: Final = "spotnav_archive"
#: How many days back from the day written a site's day files are kept (the house history reads a year).
ARCHIVE_KEPT_DAYS: Final = 400

_LOGGER = logging.getLogger(__name__)


def archive_path(config_dir: str | Path, site_id: str, day: str) -> Path:
    """Where a site's day is written (`day` as `YYYY-MM-DD`)."""
    return Path(config_dir) / ARCHIVE_DIR / site_id / f"{day}.json"


def encode(record: dict[str, Any]) -> bytes:
    """The record as UTF-8 JSON: one line per top-level part, the rows of a part on one line each, so a day stays
    small and still reads in an editor."""
    parts = [
        f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in record.items()
    ]
    return ("{\n" + ",\n".join(parts) + "\n}\n").encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    """Write [data] to [path] through a temporary file in the same folder and a rename (see the module); the folders
    are made. Raises `OSError` when it cannot be written; no temporary file is left behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_record(path: Path, record: dict[str, Any]) -> int:
    """Encode and write [record] (executor job); the bytes written."""
    data = encode(record)
    write_atomic(path, data)
    prune_archive(path)
    return len(data)


def prune_archive(written: Path) -> None:
    """Delete the day files in [written]'s folder dated more than `ARCHIVE_KEPT_DAYS` before it (executor job).
    A file that is not named as a day, or cannot be deleted, is left alone."""
    try:
        newest = date.fromisoformat(written.stem)
    except ValueError:
        return
    oldest = newest - timedelta(days=ARCHIVE_KEPT_DAYS)
    for file in written.parent.glob("*.json"):
        try:
            if date.fromisoformat(file.stem) < oldest:
                file.unlink()
        except (ValueError, OSError):
            _LOGGER.debug("SpotNav day archive: %s was left in place", file, exc_info=True)


def remove_archive(config_dir: str | Path, site_id: str) -> None:
    """A site is gone for good: delete its folder of day files (executor job); nothing is raised."""
    shutil.rmtree(Path(config_dir) / ARCHIVE_DIR / site_id, ignore_errors=True)
