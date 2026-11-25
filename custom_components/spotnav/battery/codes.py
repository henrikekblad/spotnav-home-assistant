"""The control code a person enters for a battery brand whose recipe waits for a person's hardware test (pure).

A brand marked `commands.BrandControl.hardware_test_first` and not yet `field_tested` (FoxESS, EcoFlow, Marstek, Solis today)
writes outside a test only once two things hold on the plant: a person's hardware test passed (`BatteryControl`'s
`hardware_brands`) and SpotNav's developer read that test's result and sent a **control code** for this installation
and brand, which the person entered (Support → Hardware test). The code is the developer's approval of the test
result; neither alone lifts shadow only. Plan `plans/v3_battery_activation_codes.md`, owner decisions 2026-11-25:
signing up as a tester unlocks the test itself, the control code is the only code there is.

**Checked offline.** The code is signed with Ed25519; the developer's private key never leaves the developer's own
machine (`tools/battery_codes.py`), the public key is `PUBLIC_KEY` below. Nothing is asked of a server. SpotNav is
open source: anyone may remove the check. The code is a safety belt, so an untried recipe is never switched on by
mistake and every early plant is known and reviewed, not copy protection.

**Binding:** one code per installation and brand. The code names the installation (the first `INSTALL_CHARS` hex
characters of the salted install hash the debug reports carry, `support_report.install_hash`) and the brand; the
plant's firmware key at review is recorded in it (a digest, `firmware_digest`) but does not bind it: a later
firmware, integration or recipe change goes through the automatic verification routine on the plant, as for every
brand, and needs no new code. No expiry by date (the test may have run weeks earlier).

**Format, version 1** (`CODE_VERSION`), in this order:

    version (1 byte) | kind (1 byte, 1 = control) | install (10 bytes) | firmware digest (6 bytes)
    | issued (2 bytes, days since 2020-01-01, big-endian) | brand length (1 byte) | brand (ASCII)
    | Ed25519 signature (64 bytes) of `SIGNING_CONTEXT` + everything before it

written as RFC 4648 base32 without padding, in groups of `GROUP` characters joined by dashes. Reading tolerates
case, spaces, line breaks and dashes anywhere, and reads `0` as `O`, `1` as `I` and `8` as `B` (characters base32
does not use). About 150 characters: the signature alone is 64 bytes; a code is pasted from an e-mail, never typed.

No Home Assistant import: `tools/battery_codes.py` loads this module by its path on the developer's machine.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

#: The developer's public key (raw Ed25519, hex). Its private half is kept only on the developer's machine.
PUBLIC_KEY_HEX: Final = "633ba29c3933ec1f9e0d94f1cea5efe63d6f682014898d47b138da0018c79a2e"

CODE_VERSION: Final = 1
KIND_CONTROL: Final = "control"
_KINDS: Final = {1: KIND_CONTROL}
_KIND_BYTES: Final = {name: number for number, name in _KINDS.items()}
#: The hex characters of the install hash a code names (80 bits: enough to tell installations apart).
INSTALL_CHARS: Final = 20
#: The bytes of the firmware key's SHA-256 a code records.
FIRMWARE_DIGEST_BYTES: Final = 6
#: What is signed before the payload, so a signature made for anything else never reads as a code.
SIGNING_CONTEXT: Final = b"spotnav-battery-code-v1\x00"
EPOCH: Final = date(2020, 1, 1)
MAX_BRAND_CHARS: Final = 32
SIGNATURE_BYTES: Final = 64
GROUP: Final = 5
_HEADER: Final = 1 + 1 + INSTALL_CHARS // 2 + FIRMWARE_DIGEST_BYTES + 2 + 1
_BRAND: Final = re.compile(r"[a-z0-9_]{1,32}")
_HEX: Final = re.compile(r"[0-9a-f]+")
#: Longer than any code: a paste of something else is refused before it is decoded.
MAX_TEXT_CHARS: Final = 400


class CodeError(ValueError):
    """A text that is no valid control code: its `reason` is one of `invalid` (not a code, or tampered with)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Code:
    """One decoded, signature-checked code."""

    kind: str
    install: str
    firmware_digest: str
    issued: date
    brand: str

    def matches_install(self, install_hash: str | None) -> bool:
        return isinstance(install_hash, str) and install_hash.lower()[:INSTALL_CHARS] == self.install

    def matches_firmware(self, firmware_key: str | None) -> bool:
        """Whether the plant's firmware key now is the one reviewed (recorded, never a reason to refuse)."""
        return firmware_key is not None and firmware_digest(firmware_key) == self.firmware_digest

    def as_stored(self) -> dict[str, str]:
        return {
            "kind": self.kind, "install": self.install, "firmware_digest": self.firmware_digest,
            "issued": self.issued.isoformat(), "brand": self.brand,
        }


def firmware_digest(firmware_key: str) -> str:
    """The firmware key (`gate.firmware_key`) as a code records it: the first bytes of its SHA-256, hex."""
    return hashlib.sha256(firmware_key.encode()).digest()[:FIRMWARE_DIGEST_BYTES].hex()


def public_key(raw_hex: str | None = None) -> Ed25519PublicKey:
    """The developer's public key (`PUBLIC_KEY_HEX`, read when called), or the one `raw_hex` names."""
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw_hex or PUBLIC_KEY_HEX))


def fingerprint(raw_hex: str | None = None) -> str:
    """A short name for a public key (the first 16 hex characters of its SHA-256), for support and the tool."""
    return hashlib.sha256(bytes.fromhex(raw_hex or PUBLIC_KEY_HEX)).hexdigest()[:16]


def normalise(text: str) -> str:
    """The code's base32 characters: case, spaces, line breaks and dashes dropped, look-alikes read as letters."""
    cleaned = re.sub(r"[\s\-_.]", "", text).upper()
    return cleaned.translate(str.maketrans({"0": "O", "1": "I", "8": "B"}))


def _grouped(text: str) -> str:
    return "-".join(text[index:index + GROUP] for index in range(0, len(text), GROUP))


def payload(*, install: str, brand: str, firmware_key: str, issued: date, kind: str = KIND_CONTROL) -> bytes:
    """The signed part of a code (the module's format). `install` is the full install hash or its first characters."""
    install = install.lower()[:INSTALL_CHARS]
    if len(install) != INSTALL_CHARS or _HEX.fullmatch(install) is None:
        raise ValueError("install must be the install hash (hex)")
    if _BRAND.fullmatch(brand) is None:
        raise ValueError("brand must be a recipe's brand id")
    days = (issued - EPOCH).days
    if not 0 <= days < 1 << 16:
        raise ValueError("issued date out of range")
    raw_brand = brand.encode("ascii")
    return (
        bytes((CODE_VERSION, _KIND_BYTES[kind])) + bytes.fromhex(install)
        + bytes.fromhex(firmware_digest(firmware_key)) + days.to_bytes(2, "big") + bytes((len(raw_brand),)) + raw_brand
    )


def sign(private: Ed25519PrivateKey, body: bytes) -> str:
    """A code from a payload (`payload`), as the person pastes it. Run on the developer's machine only."""
    signature = private.sign(SIGNING_CONTEXT + body)
    return _grouped(base64.b32encode(body + signature).decode("ascii").rstrip("="))


def read(text: str) -> tuple[bytes, bytes]:
    """`(payload, signature)` of a code's text, unchecked; `CodeError` when it is no code's shape."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise CodeError("invalid")
    letters = normalise(text)
    if not letters or re.fullmatch(r"[A-Z2-7]+", letters) is None:
        raise CodeError("invalid")
    try:
        raw = base64.b32decode(letters + "=" * (-len(letters) % 8))
    except (binascii.Error, ValueError) as err:
        raise CodeError("invalid") from err
    if base64.b32encode(raw).decode("ascii").rstrip("=") != letters:
        # Trailing bits that are not zero: one code has one text (up to case, spaces and look-alikes).
        raise CodeError("invalid")
    if len(raw) < _HEADER + 1 + SIGNATURE_BYTES:
        raise CodeError("invalid")
    body, signature = raw[:-SIGNATURE_BYTES], raw[-SIGNATURE_BYTES:]
    if body[0] != CODE_VERSION or body[1] not in _KINDS or body[_HEADER - 1] != len(body) - _HEADER:
        raise CodeError("invalid")
    return body, signature


def parse_body(body: bytes) -> Code:
    """The fields of a payload (`read`'s first part), unchecked."""
    install_end = 2 + INSTALL_CHARS // 2
    digest_end = install_end + FIRMWARE_DIGEST_BYTES
    days = int.from_bytes(body[digest_end:digest_end + 2], "big")
    try:
        brand = body[_HEADER:].decode("ascii")
    except UnicodeDecodeError as err:
        raise CodeError("invalid") from err
    if _BRAND.fullmatch(brand) is None:
        raise CodeError("invalid")
    return Code(
        kind=_KINDS[body[1]], install=body[2:install_end].hex(), firmware_digest=body[install_end:digest_end].hex(),
        issued=EPOCH + timedelta(days=days), brand=brand,
    )


def verify(text: str, key: Ed25519PublicKey | None = None) -> Code:
    """The code a text holds, its signature checked against the developer's key (or `key`); `CodeError("invalid")`
    for anything else: a typo, a code changed by hand, one signed by another key. Cheap: no disk, no network."""
    body, signature = read(text)
    try:
        (key or public_key()).verify(signature, SIGNING_CONTEXT + body)
    except InvalidSignature as err:
        raise CodeError("invalid") from err
    return parse_body(body)
