"""What the gate keeps of a plant beyond its reports: the brands whose hardware test passed here, the keys a later test
failed on, the tester's sign-up and the control codes, a plant whose control ran before its brand was gated, and the
notes."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from homeassistant.helpers import instance_id
from homeassistant.util import dt as dt_util

from ...beta import beta_on
from .. import codes, gate
from ..commands import (
    GATED_AFTER_RUNNING,
    BrandControl,
    control_for,
    blocked,
)
from ..verify import (
    MAX_REPORTS,
)
from .const import (
    NEED_CONTROL_CODE,
    NEED_HARDWARE_TEST_AGAIN,
    NEED_HARDWARE_TEST_FIRST,
)


_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class GateRecord:
    """The gate's record of the plant. A mixin of `BatteryControl`, on its state."""

    def _load_gate(self, raw: Any) -> None:
        data = raw if isinstance(raw, dict) else {}
        key = data.get("key")
        self._key = key if isinstance(key, str) else None
        self._enabled_seen = data.get("enabled_seen") is True
        self.took_from_ai = data.get("took_from_ai") is True
        notes = data.get("notes")
        self.notes = [dict(note) for note in notes if isinstance(note, dict)] if isinstance(notes, list) else []
        brands, failed = data.get("hardware_brands"), data.get("hardware_failed")
        self.hardware_brands = [brand for brand in brands if isinstance(brand, str)] if isinstance(brands, list) else []
        self.hardware_failed = (
            [key for key in failed if isinstance(key, str)][-MAX_REPORTS:] if isinstance(failed, list) else []
        )
        carry_after = data.get("carry_after")
        self.carry_after = carry_after if isinstance(carry_after, str) else None
        since = data.get("key_since")
        self.key_since = {
            key: at for key, at in since.items() if isinstance(key, str) and isinstance(at, str)
        } if isinstance(since, dict) else {}
        checked = data.get("grandfather_checked")
        self.grandfather_checked = (
            [brand for brand in checked if isinstance(brand, str)] if isinstance(checked, list) else []
        )
        tester, codes = data.get("tester"), data.get("control_codes")
        self.tester = dict(tester) if isinstance(tester, dict) and isinstance(tester.get("brand"), str) else None
        self.control_codes = {
            brand: dict(code) for brand, code in codes.items() if isinstance(brand, str) and isinstance(code, dict)
        } if isinstance(codes, dict) else {}

    async def _async_check_codes(self) -> None:
        """Each stored control code checked again (`battery/codes.py`): its text against the public key in this
        release, its installation and its brand. One that no longer verifies (a key changed in a release, a store
        edited by hand, one stored without its text) is dropped and logged: control then waits for a code again."""
        if not self.control_codes:
            return
        from ...support_report import install_hash  # noqa: PLC0415 - the bundle imports every part

        install = install_hash(await instance_id.async_get(self.hass))
        kept: dict[str, dict[str, Any]] = {}
        for brand, stored in self.control_codes.items():
            if stored.get("grandfathered") is True and brand in GATED_AFTER_RUNNING:
                # Control ran on the plant before its brand was gated (`_grandfather_running`): no code text to check.
                kept[brand] = stored
                continue
            text = stored.get("code")
            try:
                code = codes.verify(text) if isinstance(text, str) else None
            except codes.CodeError:
                code = None
            if code is None or code.brand != brand or not code.matches_install(install):
                _LOGGER.warning(
                    "SpotNav battery control: the stored control code for %s no longer verifies and was dropped; "
                    "control waits for a code again", brand,
                )
                continue
            kept[brand] = stored
        self.control_codes = kept

    def _grandfather_running(self) -> None:
        """A plant of a brand gated after its control could already run (`commands.GATED_AFTER_RUNNING`) whose control
        ran: the routine's check passed here for the brand on any firmware key and recipe revision (`reports`; a key
        with an unread firmware is no evidence). It counts as the brand's first pass here (`hardware_brands`) and as
        having its control code (a `grandfathered` record with the date): control never stops by an update (owner
        rule), and no tester sign-up or code is asked of it. Whether control is switched on now and the key now do not
        matter: an integration updated in the same restart, a firmware the routine has not re-passed yet or control
        switched off at the update keep it, and the routine checks the key now as it always did. Never for a plant a
        person's hardware test passed on (`hardware_brands`: the code approves that pass) or that a tester signed up
        for. Decided once per plant and brand (`grandfather_checked`), granted or not: a plant with no passed report
        when its brand is first seen is never looked at again, and a pass voided later by a block stays void."""
        control = self._gated_control()
        if control is None or control.brand not in GATED_AFTER_RUNNING or control.brand in self.grandfather_checked:
            return
        # Read before the brand is marked as decided: a store that cannot be read decides nothing.
        passed = [
            key for key, report in self.reports.items()
            if isinstance(report, dict) and report.get("brand") == control.brand and gate.firmware_known(key)
            and gate.report_status(report) == "passed"
        ]
        self.grandfather_checked = [*self.grandfather_checked, control.brand]
        if (
            control.brand in self.control_codes or control.brand in self.hardware_brands
            or (self.tester is not None and self.tester.get("brand") == control.brand)
        ):
            # A record already, a person's hardware test passed here (its pass is the routine's report too) or a
            # tester's sign-up: the developer's code approves that one.
            return
        if not passed:
            return
        key = passed[-1]
        now = dt_util.now()
        self.hardware_brands.append(control.brand)
        self.control_codes = dict(self.control_codes) | {control.brand: {
            "grandfathered": True, "brand": control.brand, "entered_at": now.isoformat(), "firmware_key": key,
        }}
        self._log([{"at": now.isoformat(), "kind": "control_grandfathered", "reason": key}])
        _LOGGER.info(
            "SpotNav battery control: %s control already ran here (passed on %s); it goes on without a hardware test "
            "or a control code", control.brand, key,
        )

    def grandfathered(self) -> bool:
        """Whether the plant's gated brand holds a `grandfathered` record (`_grandfather_running`)."""
        brand = self.gated_brand()
        return brand is not None and (self.control_codes.get(brand) or {}).get("grandfathered") is True

    def _void_blocked_passes(self) -> None:
        """At each look: a recipe this release holds back on the plant (`commands.BLOCKED`) voids the plant's stored
        pass with the brand (`hardware_brands`), so after the block a hardware test must pass again (and its result
        is reviewed with the code already given). Logged once."""
        control = self._gated_control()
        if control is None or control.brand not in self.hardware_brands or not self.recipe_blocked():
            return
        self.hardware_brands = [brand for brand in self.hardware_brands if brand != control.brand]
        self._log([{"at": dt_util.now().isoformat(), "kind": "hardware_pass_voided", "reason": self.firmware_key()}])
        _LOGGER.warning("SpotNav battery control: this release blocks the %s recipe here; its stored pass is void",
                        control.brand)

    def hardware_test_due(self) -> str | None:
        """What a recipe that writes only after a person's hardware test (`BrandControl.hardware_test_first`, not yet
        `field_tested`) waits for now, `None` when nothing: `hardware_test_first` before its first pass on this plant;
        `control_code` after that pass until the developer's control code for this installation and brand was
        entered (`battery/codes.py`: the developer's approval of the test's result, so control needs both; one
        code per plant and brand); after both, `hardware_test_again` once the routine's check on the key now failed
        (or gave up), or a later hardware test failed on it. A change of firmware, integration version or recipe
        revision after the first pass is checked by the routine by itself, and control runs on meanwhile on the
        brand's last passed report here (`control_report`): no new code is needed."""
        control = self._gated_control()
        if control is None:
            return None
        if control.brand not in self.hardware_brands:
            return NEED_HARDWARE_TEST_FIRST
        if control.brand not in self.control_codes:
            return NEED_CONTROL_CODE
        key = self.firmware_key()
        if key in self.hardware_failed or gate.report_status(self.reports.get(key)) == "failed":
            return NEED_HARDWARE_TEST_AGAIN
        return None

    def _gated_control(self) -> BrandControl | None:
        """The plant's recipe when it writes only after a person's hardware test and the developer's control code
        (`BrandControl.hardware_test_first`, not yet `field_tested`), `None` for every other plant."""
        control = control_for(self.facts.control_brand)
        if control is None or not control.hardware_test_first or control.field_tested:
            return None
        return control

    def gated_brand(self) -> str | None:
        """The brand that takes a tester sign-up and a control code on this plant (`_gated_control`), `None` else."""
        control = self._gated_control()
        return None if control is None else control.brand

    def tester_active(self) -> bool:
        """Whether the person is a tester for the plant's brand now: signed up for it, and beta mode on (a tester is
        one while the reports may go: unticking Beta tester ends it, `battery/tester.async_end`)."""
        brand = self.gated_brand()
        return (
            brand is not None and self.tester is not None and self.tester.get("brand") == brand
            and beta_on(self.hass)
        )

    def tester_signup_needed(self) -> bool:
        """Whether the plant's recipe was never passed here and the person is no tester for its brand now
        (`tester_active`): the hardware test then waits for the sign-up (owner decision 2026-11-25: the sign-up
        unlocks it)."""
        brand = self.gated_brand()
        return brand is not None and brand not in self.hardware_brands and not self.tester_active()

    def tester_view(self) -> dict[str, Any] | None:
        """The battery section's tester block (`spotnav/battery_control`'s `tester`), `None` for a plant whose recipe
        takes no code: `stage` `none` (not signed up, or beta mode off: `tester_active`), `asked` (signed up, no test result sent), `tested` (a test's
        result was sent), `control_code` (the code was entered); the brand and the times (never the e-mail). `None` too
        for a plant whose control ran before its brand was gated (`grandfathered`): it has nothing to sign up for."""
        brand = self.gated_brand()
        if brand is None or self.grandfathered():
            return None
        tester = self.tester if self.tester_active() else None
        code = self.control_codes.get(brand)
        if code is not None:
            stage = "control_code"
        elif tester is not None and tester.get("result_at") is not None:
            stage = "tested"
        elif tester is not None:
            stage = "asked"
        else:
            stage = "none"
        return {
            "stage": stage,
            "brand": brand,
            "sent_at": None if tester is None else tester.get("asked_at"),
            "result_sent_at": None if tester is None else tester.get("result_at"),
            "test_passed": brand in self.hardware_brands,
            "code_entered_at": None if code is None else code.get("entered_at"),
        }

    def recipe_blocked(self) -> bool:
        """Whether this SpotNav release holds the recipe back on the plant's firmware key or the recipe's revision
        (`commands.blocked`): shadow only, whatever was passed."""
        control = control_for(self.facts.control_brand)
        return control is not None and blocked(control, self.firmware_key())

    def _add_note(self, code: str, now: datetime) -> None:
        if not any(note.get("code") == code for note in self.notes):
            self.notes.append({"code": code, "at": now.isoformat()})

    def _keep_hardware_verdict(self, done: bool, now: datetime) -> None:
        """What a person's finished hardware test means for a recipe that writes only after one
        (`gate.hardware_test_passed`): a pass is the plant's first pass with the brand (`hardware_brands`) and stands
        as the routine's check on the key now, so control runs at once; a test that failed after the first pass holds
        control in shadow only on that key (`hardware_failed`) until one passes again. A stopped test without a
        failure changes nothing."""
        key = self.firmware_key()
        control = control_for(self.facts.control_brand)
        if control is None or not control.hardware_test_first or not gate.firmware_known(key):
            return
        report = self.hardware_report or {}
        if done and gate.hardware_test_passed(
            report, self._supported_modes(), deadman_required=control.deadman_must_pass,
        ):
            if control.brand not in self.hardware_brands:
                self.hardware_brands.append(control.brand)
            self.hardware_failed = [known for known in self.hardware_failed if known != key]
            self._keep_report(key, gate.merge_report(None, report, key=key, attempts=1, now=now))
        elif control.brand in self.hardware_brands and (
            gate.failed_mode(report) is not None or (done and not self.grandfathered())
        ):
            # A grandfathered plant's control already ran: only a mode that failed stops it, never a test that could
            # not judge (the sun, no room under the fuse).
            self.hardware_failed = [*(known for known in self.hardware_failed if known != key), key][-MAX_REPORTS:]
            # Nor does a report passed before this failure run control on a later key (`control_report`).
            self.carry_after = now.isoformat()

    def hardware_test_missing(self, report: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
        """What a hardware test's report leaves unshown of what this plant's recipe waits for before control may
        start (`gate.hardware_test_missing`, as `_keep_hardware_verdict` judges it): `[{mode, outcome}]`, empty when
        the test showed it all. `None` for a plant whose control does not wait for a person's test: a recipe without
        `hardware_test_first` (or `field_tested`), and one whose control ran before its brand was gated
        (`grandfathered`); their test's result is its steps' alone."""
        control = self._gated_control()
        if control is None or self.grandfathered():
            return None
        missing = gate.hardware_test_missing(
            report, self._supported_modes(), deadman_required=control.deadman_must_pass,
        )
        return [{"mode": mode, "outcome": outcome} for mode, outcome in missing]
