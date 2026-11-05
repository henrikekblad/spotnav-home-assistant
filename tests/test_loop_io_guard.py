"""The loop I/O guard itself (`conftest.loop_io_guard`): a file read on the event loop made by SpotNav's code is
recorded; the same read in an executor job, or made by anyone else, is not."""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant

from .conftest import _SPOTNAV_DIR

READER = "def read(path):\n    with open(path, encoding='utf-8') as handle:\n        return handle.read()\n"


def reader(filename: str):
    """`read(path)` as if it were written in `filename`."""
    namespace: dict = {}
    exec(compile(READER, filename, "exec"), namespace)  # noqa: S102 - a function with a chosen file name
    return namespace["read"]


async def test_a_read_on_the_loop_from_spotnavs_code_is_recorded(
    hass: HomeAssistant, loop_io_guard: list[str], tmp_path: Path
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    read = reader(f"{_SPOTNAV_DIR}/fake_module.py")

    assert read(target) == "x"
    assert len(loop_io_guard) == 1 and "fake_module.py:2" in loop_io_guard[0]
    loop_io_guard.clear()

    # In an executor job the same read is allowed.
    assert await hass.async_add_executor_job(read, target) == "x"
    assert loop_io_guard == []


async def test_a_read_on_the_loop_from_anyone_else_is_not_spotnavs(loop_io_guard: list[str], tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")

    assert reader("/somewhere/homeassistant/helpers/fake.py")(target) == "x"
    assert target.read_text(encoding="utf-8") == "x"
    assert loop_io_guard == []
