#!/usr/bin/env python3
"""Issue and read the control codes for battery brands that wait for a person's hardware test (developer's machine).

    .venv/bin/python tools/battery_codes.py keygen
    .venv/bin/python tools/battery_codes.py control SN-XXXX [--lang sv|en] [--report FILE]
    .venv/bin/python tools/battery_codes.py show CODE

`keygen` makes the signing key pair once: the private key goes to `~/tokens/spotnav-battery-code-key` (mode 0600,
never overwritten), the public key is printed to paste into `custom_components/spotnav/battery/codes.py`
(`PUBLIC_KEY_HEX`). The private key is never printed, copied or committed.

`control SN-XXXX` reads the report (its `meta.json` and the gzip-compressed report) from the relay over
`ssh ubuntu@netbird` with `sudo -n`, as the support workflow does (or a local copy with `--report`), checks that it
is a tester's report for a brand that takes a code, and prints the person's e-mail address on top, the code, and the
e-mail text for the brand (`tools/battery_code_mails/<brand>.<lang>.md`, `{code}` filled in). Read the test's log
before running it: the code is the developer's approval of that test result.

`show CODE` decodes a code and says whether its signature is the public key's in `codes.py`.

The format and the check are `custom_components/spotnav/battery/codes.py` (loaded by its path: no Home Assistant
needed).
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CODES_PATH = ROOT / "custom_components" / "spotnav" / "battery" / "codes.py"
MAILS = Path(__file__).resolve().parent / "battery_code_mails"
KEY_PATH = Path.home() / "tokens" / "spotnav-battery-code-key"
RELAY_HOST = "ubuntu@netbird"
SUPPORT_DIR = "/data/spotnav/support"
#: The report kinds a control code answers: a finished test (the usual one), or the sign-up itself.
CODE_KINDS = ("battery_test_result", "battery_tester")
#: The brands whose recipe waits for a person's hardware test (`commands.BrandControl.hardware_test_first`).
GATED_BRANDS = ("foxess", "ecoflow", "marstek")
_REF = re.compile(r"^SN-[A-Z0-9]{4}$")


def load_codes() -> Any:
    spec = importlib.util.spec_from_file_location("spotnav_battery_codes", CODES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def keygen(path: Path = KEY_PATH) -> str:
    """Write a new private key to `path` (0600, refused when it exists); answer the public key, hex."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def load_private(path: Path = KEY_PATH) -> Any:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    if path.stat().st_mode & 0o077:
        raise SystemExit(f"{path} is readable by others: chmod 600 it first")
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _ssh(command: str) -> bytes:
    result = subprocess.run(["ssh", RELAY_HOST, command], capture_output=True, check=False, timeout=60)
    if result.returncode != 0:
        raise SystemExit(f"ssh failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def fetch_report(ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(meta, report)` of a stored report on the relay."""
    if _REF.match(ref) is None:
        raise SystemExit(f"not a report reference: {ref!r}")
    found = _ssh(f"sudo -n find {SUPPORT_DIR} -name {ref}.meta.json").decode().split()
    if len(found) != 1:
        raise SystemExit(f"{ref}: {len(found)} reports found")
    meta_path = found[0]
    report_path = meta_path.removesuffix(".meta.json") + ".json.gz"
    meta = json.loads(_ssh(f"sudo -n cat {meta_path}"))
    report = json.loads(gzip.decompress(_ssh(f"sudo -n cat {report_path}")))
    return meta, report


def read_local(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(meta, report)` of a local copy: the report (`.json.gz` or `.json`), its `.meta.json` beside it if there."""
    raw = path.read_bytes()
    report = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
    meta_path = Path(str(path).removesuffix(".gz").removesuffix(".json") + ".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return meta, report


def tester_facts(meta: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """What a code is issued from: kind, install, e-mail, brand, firmware key and language."""
    battery = report.get("battery") if isinstance(report.get("battery"), dict) else {}
    return {
        "kind": meta.get("kind") or report.get("kind"),
        "install": report.get("install") or meta.get("install"),
        "email": meta.get("email") or report.get("email"),
        "brand": meta.get("brand") or battery.get("brand"),
        "firmware_key": meta.get("firmware_key") or battery.get("firmware_key"),
        "language": battery.get("language"),
        "result": (battery.get("test") or {}).get("result") if isinstance(battery.get("test"), dict) else None,
    }


def mail_text(brand: str, language: str, code: str) -> str:
    path = MAILS / f"{brand}.{language}.md"
    if not path.exists():
        path = MAILS / f"{brand}.en.md"
    return path.read_text().replace("{code}", code)


def issue(codes: Any, private: Any, facts: dict[str, Any], today: date) -> str:
    if facts["kind"] not in CODE_KINDS:
        raise SystemExit(f"not a tester's report (kind {facts['kind']!r})")
    if facts["brand"] not in GATED_BRANDS:
        raise SystemExit(f"brand {facts['brand']!r} takes no code")
    if not isinstance(facts["install"], str) or not isinstance(facts["firmware_key"], str):
        raise SystemExit("the report names no install or firmware key")
    body = codes.payload(install=facts["install"], brand=facts["brand"], firmware_key=facts["firmware_key"],
                         issued=today)
    return codes.sign(private, body)


def show(codes: Any, text: str) -> str:
    try:
        body, _signature = codes.read(text)
    except codes.CodeError:
        return "not a code"
    code = codes.parse_body(body)
    try:
        codes.verify(text)
        signed = f"signature OK (key {codes.fingerprint()})"
    except codes.CodeError:
        signed = "SIGNATURE NOT VALID for the public key in codes.py"
    return "\n".join((
        f"kind: {code.kind}", f"brand: {code.brand}", f"install: {code.install}…",
        f"firmware digest: {code.firmware_digest}", f"issued: {code.issued.isoformat()}", signed,
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen", help="make the key pair (once)")
    control = commands.add_parser("control", help="issue a control code for a tester's report")
    control.add_argument("ref", help="the report's reference, SN-XXXX")
    control.add_argument("--lang", choices=("sv", "en"), help="the e-mail's language (default: the report's)")
    control.add_argument("--report", type=Path, help="a local copy of the report instead of the relay")
    shown = commands.add_parser("show", help="decode a code")
    shown.add_argument("code", nargs="+", help="the code (spaces allowed)")
    args = parser.parse_args(argv)
    codes = load_codes()
    if args.command == "keygen":
        if KEY_PATH.exists():
            print(f"{KEY_PATH} exists: not overwritten", file=sys.stderr)
            return 1
        public = keygen()
        print(f"Private key written to {KEY_PATH} (0600). Paste the public key into codes.py:")
        print(f'PUBLIC_KEY_HEX: Final = "{public}"')
        print(f"fingerprint: {codes.fingerprint(public)}")
        return 0
    if args.command == "show":
        print(show(codes, " ".join(args.code)))
        return 0
    meta, report = read_local(args.report) if args.report else fetch_report(args.ref)
    facts = tester_facts(meta, report)
    code = issue(codes, load_private(), facts, date.today())
    language = args.lang or ("sv" if facts["language"] == "sv" else "en")
    print(facts["email"] or "(no e-mail address in the report)")
    print()
    print(f"{args.ref} · {facts['brand']} · {facts['firmware_key']} · test {facts['result'] or 'unknown'}")
    print(code)
    print()
    print(mail_text(facts["brand"], language, code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
