"""The control codes (`battery/codes.py`) and the developer's tool that issues them (`tools/battery_codes.py`).

Every code here is signed with a throwaway key made by the test; the developer's private key is never read."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custom_components.spotnav.battery import codes
from custom_components.spotnav.battery.codes import CodeError

ROOT = Path(__file__).resolve().parent.parent
INSTALL = "ab" * 32
KEY = "V1.2.3 | foxess_modbus 1.15.0"
#: A fixed throwaway key, so the golden code below stays the same run after run.
SEED = bytes(range(32))


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(SEED)


def _public(private: Ed25519PrivateKey):  # noqa: ANN202 - the library's type
    return private.public_key()


def _code(brand: str = "foxess", install: str = INSTALL, firmware: str = KEY, issued: date = date(2026, 11, 25)) -> str:
    return codes.sign(_key(), codes.payload(install=install, brand=brand, firmware_key=firmware, issued=issued))


GOLDEN = (
    "AEA2X-K5LVO-V2XK5-LVOV4-ICGM7-7HWUC-OYAZT-G66DF-ONZWH-Y25GG-C7USJ-URV5N-ONOHE-KZBQU-DZHO4-"
    "VWFDC-6ZTYX-SOTE7-M7APV-S4T5K-ZYNKV-7KHRL-OULQX-GFQOS-V2DFT-IAMK2-6X5JO-EYGVH-CFPFB-M"
)


def test_a_code_decodes_to_what_was_signed() -> None:
    code = codes.verify(_code(), _public(_key()))
    assert code.kind == "control" and code.brand == "foxess" and code.issued == date(2026, 11, 25)
    assert code.matches_install(INSTALL) and not code.matches_install("cd" * 32)
    assert code.matches_firmware(KEY) and not code.matches_firmware("V1.2.4 | foxess_modbus 1.15.0")
    assert code.as_stored()["install"] == INSTALL[: codes.INSTALL_CHARS]


def test_the_golden_code_is_stable() -> None:
    # The format is a contract with every code already sent: a change here breaks them.
    text = _code()
    assert text == GOLDEN
    assert codes.verify(GOLDEN, _public(_key())).brand == "foxess"


def test_reading_tolerates_case_spaces_breaks_and_look_alikes() -> None:
    text = _code()
    pasted = "  " + text.lower().replace("-", " ").replace("o", "0", 1) + "\n"
    assert codes.verify(pasted, _public(_key())).brand == "foxess"
    assert codes.verify(text.replace("-", "\n"), _public(_key())).brand == "foxess"


@pytest.mark.parametrize("text", ["", "hello", "SN-ABCD", "A" * 500, "AAAA-BBBB", "!!!!"])
def test_text_that_is_no_code_is_invalid(text: str) -> None:
    with pytest.raises(CodeError) as err:
        codes.verify(text, _public(_key()))
    assert err.value.reason == "invalid"


def test_a_tampered_code_is_invalid() -> None:
    letters = codes.normalise(_code())
    # Change one character of the payload (the brand's) and of the signature: neither verifies.
    for index in (30, len(letters) - 3):
        swapped = "B" if letters[index] != "B" else "C"
        tampered = letters[:index] + swapped + letters[index + 1:]
        with pytest.raises(CodeError):
            codes.verify(tampered, _public(_key()))


def test_a_code_signed_by_another_key_is_invalid() -> None:
    other = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
    text = codes.sign(other, codes.payload(install=INSTALL, brand="foxess", firmware_key=KEY, issued=date(2026, 11, 25)))
    with pytest.raises(CodeError):
        codes.verify(text, _public(_key()))
    # Nor against the developer's real key, which the tests never sign with.
    with pytest.raises(CodeError):
        codes.verify(_code())


def test_a_signature_over_the_bare_payload_is_no_code() -> None:
    # The signing context: a signature made for the bytes alone (anything else the key might sign) is refused.
    body = codes.payload(install=INSTALL, brand="foxess", firmware_key=KEY, issued=date(2026, 11, 25))
    import base64  # noqa: PLC0415

    text = base64.b32encode(body + _key().sign(body)).decode().rstrip("=")
    with pytest.raises(CodeError):
        codes.verify(text, _public(_key()))


def test_payload_refuses_what_a_code_cannot_carry() -> None:
    with pytest.raises(ValueError):
        codes.payload(install="xyz", brand="foxess", firmware_key=KEY, issued=date(2026, 1, 1))
    with pytest.raises(ValueError):
        codes.payload(install=INSTALL, brand="Fox ESS", firmware_key=KEY, issued=date(2026, 1, 1))
    with pytest.raises(ValueError):
        codes.payload(install=INSTALL, brand="foxess", firmware_key=KEY, issued=date(2019, 1, 1))


def test_the_public_key_is_a_real_key() -> None:
    assert codes.public_key() is not None
    assert codes.PUBLIC_KEY_HEX != "0" * 64 and len(codes.fingerprint()) == 16


# ---- the tool


def _tool():  # noqa: ANN202 - a module
    spec = importlib.util.spec_from_file_location("battery_codes_tool", ROOT / "tools" / "battery_codes.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_keygen_writes_a_private_key_only_its_owner_reads_and_never_overwrites(tmp_path: Path) -> None:
    tool = _tool()
    path = tmp_path / "tokens" / "key"
    public = tool.keygen(path)
    assert path.stat().st_mode & 0o777 == 0o600
    private = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex() == public
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        tool.keygen(path)
    assert path.read_bytes() == before


def _report(tmp_path: Path, kind: str = "battery_test_result", brand: str = "foxess") -> Path:
    report = {
        "version": 1, "install": INSTALL, "kind": kind, "email": "tester@example.com", "message": "",
        "battery": {"brand": brand, "firmware_key": KEY, "language": "sv", "test": {"result": "passed"}},
        "bundle": {},
    }
    path = tmp_path / "SN-TEST.json.gz"
    path.write_bytes(gzip.compress(json.dumps(report).encode()))
    (tmp_path / "SN-TEST.meta.json").write_text(json.dumps(
        {"kind": kind, "email": "tester@example.com", "brand": brand, "firmware_key": KEY}
    ))
    return path


def test_control_issues_a_code_for_this_install_and_brand(tmp_path: Path) -> None:
    tool = _tool()
    meta, report = tool.read_local(_report(tmp_path))
    facts = tool.tester_facts(meta, report)
    assert facts["email"] == "tester@example.com" and facts["language"] == "sv"
    text = tool.issue(codes, _key(), facts, date(2026, 11, 25))
    code = codes.verify(text, _public(_key()))
    assert code.brand == "foxess" and code.matches_install(INSTALL) and code.matches_firmware(KEY)


def test_control_refuses_a_report_that_is_not_a_testers_or_a_brand_without_codes(tmp_path: Path) -> None:
    tool = _tool()
    meta, report = tool.read_local(_report(tmp_path, kind="support"))
    with pytest.raises(SystemExit):
        tool.issue(codes, _key(), tool.tester_facts(meta, report), date(2026, 11, 25))
    meta, report = tool.read_local(_report(tmp_path, brand="sigenergy"))
    with pytest.raises(SystemExit):
        tool.issue(codes, _key(), tool.tester_facts(meta, report), date(2026, 11, 25))


def test_the_tools_brands_are_the_gated_recipes() -> None:
    from custom_components.spotnav.battery import commands  # noqa: PLC0415

    gated = {control.brand for control in commands.CONTROLS if commands.hardware_test_first(control.brand)}
    assert set(_tool().GATED_BRANDS) == gated


def test_show_says_whether_the_signature_is_the_developers() -> None:
    tool = _tool()
    shown = tool.show(codes, _code())
    assert "brand: foxess" in shown and "NOT VALID" in shown
    assert tool.show(codes, "nonsense") == "not a code"
