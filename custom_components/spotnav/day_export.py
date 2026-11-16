"""The `export_day` service's writing: one site's day record (`day_record.py`) as a JSON file in the configuration.

`<config>/spotnav_archive/<site_id>/<YYYY-MM-DD>.json`, one file per site and local day. The record is built on the
event loop from memory; turning it into JSON and writing it run in the executor. The write is atomic: a temporary
file beside the target, flushed and synced, then renamed over it, so a reader never sees half a file and a failed
write leaves the day's earlier file as it was. Exporting a day again overwrites its file. Nothing else is written,
and nothing runs unless the service is called.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Final

#: The folder in Home Assistant's configuration directory that holds the archive.
ARCHIVE_DIR: Final = "spotnav_archive"


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
    return len(data)
