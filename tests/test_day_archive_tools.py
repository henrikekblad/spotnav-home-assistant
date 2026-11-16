"""The tools that read an exported day: `tools/spotnav_archive_to_influx.py` (its line protocol and its write to a fake
InfluxDB 3) and `tools/replay_day.py` (the battery plan run again on the day). The day is
`tests/fixtures/day_record/2026-09-22.json`, written by the export: a 10 kWh battery that ran plain self-consumption
beside the plan's Balanced and Max profit modes, made once at midnight on the logged forecasts."""

from __future__ import annotations

from collections.abc import Iterator
import copy
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any

import pytest

TOOLS = Path(__file__).parent.parent / "tools"
FIXTURE = Path(__file__).parent / "fixtures" / "day_record" / "2026-09-22.json"
TOKEN = "s3cret-token-value"


def _tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


influx = _tool("spotnav_archive_to_influx")
replay_day = _tool("replay_day")


def _record() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the line protocol


def test_names_tags_and_strings_are_escaped_by_the_line_protocols_rules() -> None:
    assert influx.escape_measurement("a b,c=d") == "a\\ b\\,c=d"
    assert influx.escape_key("a b,c=d\\e") == "a\\ b\\,c\\=d\\\\e"
    assert influx.field_value('say "hi" \\o/') == '"say \\"hi\\" \\\\o/"'
    assert influx.field_value(3) == "3i" and influx.field_value(1.5) == "1.5" and influx.field_value(True) == "true"
    assert influx.field_value(float("nan")) is None and influx.field_value(None) is None
    point = influx.line("m x", {"site": "my site", "kind": "a=b", "empty": None}, {"v": 1.0, "gone": None}, 1700000000)
    assert point == "m\\ x,kind=a\\=b,site=my\\ site v=1.0 1700000000"
    assert influx.line("m", {"site": "s"}, {"v": None}, 1) is None


def test_the_lead_is_bucketed_from_when_the_forecast_was_made() -> None:
    hour = "2026-09-22T12:00:00+00:00"
    assert influx.lead_bucket(hour, "2026-09-22T11:30:00+00:00") == "0-1"
    assert influx.lead_bucket(hour, "2026-09-22T06:00:00+00:00") == "3-6"
    assert influx.lead_bucket(hour, "2026-09-21T11:00:00+00:00") == "24-36"
    assert influx.lead_bucket(hour, "2026-09-20T11:00:00+00:00") == "36+"


def test_a_day_becomes_forecast_price_and_review_points() -> None:
    lines = influx.record_lines(_record())

    forecasts = [line for line in lines if line.startswith("spotnav_forecast,")]
    prices = [line for line in lines if line.startswith("spotnav_price,")]
    reviews = [line for line in lines if line.startswith("spotnav_review,")]
    assert len(forecasts) == 96 and len(prices) == 96 and len(lines) == len(forecasts) + len(prices) + len(reviews)
    first = next(line for line in forecasts if ",kind=house_kw," in line)
    assert first == (
        "spotnav_forecast,kind=house_kw,lead_h=6-12,site=site_fixture "
        "value=0.99,outcome=0.9,made_at=1789988400i 1790028000"
    )
    assert prices[0].startswith("spotnav_price,site=site_fixture spot=") and prices[0].endswith(" 1790028000")
    assert sum(",mode=" in line for line in reviews) == 2
    assert any(line.startswith("spotnav_review,mode=balanced,site=site_fixture saving=726.75,") for line in reviews)
    assert sum(",kind=" in line for line in reviews) == 4
    # The same day again gives the same points: tags and times are the record's own, so a second write overwrites.
    assert influx.record_lines(_record()) == lines


def test_a_file_that_is_no_day_record_is_refused() -> None:
    with pytest.raises(ValueError, match="not a SpotNav day record"):
        influx.record_lines({"schema": 1})
    record = _record()
    record["schema"] = 2
    with pytest.raises(ValueError, match="schema 2"):
        influx.record_lines(record)


# ------------------------------------------------------------------ the write


class _Influx:
    """A fake InfluxDB 3: answers each write with [status] and keeps what it got."""

    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the server's own name
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append(
                    {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body.decode()}
                )
                self.send_response(owner.status)
                self.end_headers()
                if owner.status >= 400:
                    self.wfile.write(b'{"error":"bad line"}')

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_influx(socket_enabled: None) -> Iterator[_Influx]:
    server = _Influx()
    yield server
    server.close()


def test_the_day_is_written_with_the_token_from_a_file_and_the_token_is_never_printed(
    fake_influx: _Influx, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")

    status = influx.main([str(FIXTURE), "--url", fake_influx.url, "--db", "energy", "--token-file", str(token_file)])
    again = influx.main([str(FIXTURE), "--url", fake_influx.url, "--db", "energy", "--token-file", str(token_file)])

    assert status == again == 0
    first, second = fake_influx.requests
    assert first["path"] == "/api/v3/write_lp?db=energy&precision=second"
    assert first["authorization"] == f"Bearer {TOKEN}"
    assert first["body"] == "\n".join(influx.record_lines(_record())) + "\n"
    assert second["body"] == first["body"]
    printed = capsys.readouterr()
    assert TOKEN not in printed.out + printed.err
    assert "Wrote" in printed.out


def test_the_token_comes_from_the_environment_without_a_file(
    fake_influx: _Influx, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(influx.TOKEN_ENV, TOKEN)

    assert influx.main([str(FIXTURE), "--url", fake_influx.url]) == 0
    assert fake_influx.requests[0]["authorization"] == f"Bearer {TOKEN}"


def test_a_refused_write_and_a_missing_token_are_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], socket_enabled: None
) -> None:
    monkeypatch.delenv(influx.TOKEN_ENV, raising=False)
    assert influx.main([str(FIXTURE), "--url", "http://127.0.0.1:9"]) == 2
    server = _Influx(status=400)
    try:
        monkeypatch.setenv(influx.TOKEN_ENV, TOKEN)
        assert influx.main([str(FIXTURE), "--url", server.url]) == 1
    finally:
        server.close()
    printed = capsys.readouterr()
    assert "refused the write: 400" in printed.err and TOKEN not in printed.out + printed.err


def test_a_dry_run_prints_the_lines_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert influx.main([str(FIXTURE), "--dry-run", "--url", "http://127.0.0.1:9"]) == 0
    assert capsys.readouterr().out.splitlines() == influx.record_lines(_record())


def test_the_influx_tool_uses_the_standard_library_only() -> None:
    source = (TOOLS / "spotnav_archive_to_influx.py").read_text(encoding="utf-8")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    }
    assert imported <= {"argparse", "datetime", "json", "math", "os", "sys", "typing", "urllib"}


# ------------------------------------------------------------------ the replay


def test_one_plan_at_midnight_gives_the_recorded_saving_again() -> None:
    result = replay_day.replay(_record(), once=True)

    assert result["mode"] == "balanced" and result["slots"] == 96 and result["replanned"] == "once"
    assert result["recorded"]["saving"] == 726.75
    # The record's plans were made once at midnight on the same forecasts: today's planner finds the same actions
    # (to within the rounding of the stored prices and kW).
    assert abs(result["difference"]) < 1.0
    assert set(result["replayed"]["mode_savings"]) == {"balanced", "max_profit"}
    # The measured quarter hours are stored rounded: the same runs come out within a few hundredths of a krona.
    assert result["replayed"]["actual_saving"] == pytest.approx(result["recorded"]["actual_saving"], abs=1.0)
    assert result["replayed"]["self_consumption_net"] == pytest.approx(result["recorded"]["self_consumption_net"], abs=1.0)


def test_planned_again_each_hour_and_on_the_measured_day(capsys: pytest.CaptureFixture[str]) -> None:
    hourly = replay_day.replay(_record())
    hindsight = replay_day.replay(_record(), hindsight=True)

    assert hourly["replanned"] == "hourly" and hourly["soc"] == "replayed" and hourly["inputs"] == "forecast"
    assert hindsight["inputs"] == "measured"
    assert hourly["replayed"]["covered"] == 96 and hourly["replayed"]["unplanned"] == 0
    assert replay_day.main([str(FIXTURE), "--once"]) == 0
    out = capsys.readouterr().out
    assert "recorded: saving 726.75" in out and "difference (replayed - recorded)" in out


def test_a_day_without_quarter_hours_or_battery_cannot_be_replayed(tmp_path: Path) -> None:
    record = _record()
    empty = copy.deepcopy(record)
    empty["quarters"]["rows"] = []
    with pytest.raises(replay_day.ReplayError, match="no quarter hours"):
        replay_day.replay(empty)
    no_battery = copy.deepcopy(record)
    no_battery["settings"]["battery"] = None
    with pytest.raises(replay_day.ReplayError, match="no battery"):
        replay_day.replay(no_battery)
    path = tmp_path / "day.json"
    path.write_text(json.dumps(empty), encoding="utf-8")
    assert replay_day.main([str(path)]) == 2
