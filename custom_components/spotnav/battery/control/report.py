"""The gate as the shell reads it: the site's options, the plant's firmware key, the report control runs on (its own or
one carried from the last key that passed), the state and the actions allowed, and a change of the settings."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from homeassistant.loader import async_get_loaded_integration
from homeassistant.util import dt as dt_util

from ...beta import learning_gate_open
from .. import gate
from ..commands import BrandControl, control_for, blocked, field_tested as recipe_field_tested
from ..verify import MAX_REPORTS, supported_modes
from .trial import ControlNotReachable

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class Report:
    """The options, the key, the report and the state. A mixin of `BatteryControl`, on its state."""

    # ---- the gate

    def options(self) -> gate.Options:
        """The site's options, from its entry as stored now (a settings write needs no reload). The acceptance of
        the HA-down risk counts only for the setup it was given for (`gate.ha_down_setup_state`). A recipe that
        writes only after a person's hardware test (`BrandControl.hardware_test_first`) runs as shadow only until one
        passed on this firmware (`hardware_test_due`), whatever is stored and on a beta site too; the test itself and
        every hand-back still run (`_writes_allowed_now`)."""
        options = self._stored_options()
        if not options.shadow_only and (self.hardware_test_due() is not None or self.recipe_blocked()):
            options = replace(options, shadow_only=True)
        return options

    def _stored_options(self) -> gate.Options:
        """The options as the site stored them (what the card's switches show)."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        data = entry.data if entry is not None else self.site.config
        options = gate.Options.from_config(data, shadow_default=self.shadow_default())
        if options.accept_ha_down and self._ha_down_setup_state(data) != "same":
            options = replace(options, accept_ha_down=False)
        return options

    def recipe(self) -> BrandControl | None:
        """The plant's recipe (`commands.control_for` its brand), `None` for a brand SpotNav has none for."""
        return control_for(self.facts.control_brand)

    async def async_store(self) -> None:
        """Store the state now (a tester's sign-up, a control code: `battery/tester.py`)."""
        await self._async_save()

    def _ha_down_setup_state(self, data: Mapping[str, Any]) -> str:
        return gate.ha_down_setup_state(data.get(gate.CONF_ACCEPT_HA_DOWN_SETUP), self._main_fuse_a(), self.facts.brand)

    def _ha_down_setup_look(self) -> None:
        """At each look: an acceptance of the HA-down risk given before the brand was read is completed with it; one
        whose setup changed (another main fuse, another brand) is cleared, and logged."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None or entry.data.get(gate.CONF_ACCEPT_HA_DOWN) is not True:
            return
        stored = entry.data.get(gate.CONF_ACCEPT_HA_DOWN_SETUP)
        state = self._ha_down_setup_state(entry.data)
        if state == "unknown" and self.facts.brand is not None and isinstance(stored, Mapping):
            data = {**entry.data, gate.CONF_ACCEPT_HA_DOWN_SETUP: gate.ha_down_setup(
                self._main_fuse_a(), self.facts.brand)}
            self.hass.config_entries.async_update_entry(entry, data=data)
        elif state == "changed":
            data = {key: value for key, value in entry.data.items() if key != gate.CONF_ACCEPT_HA_DOWN_SETUP}
            data[gate.CONF_ACCEPT_HA_DOWN] = False
            self.hass.config_entries.async_update_entry(entry, data=data)
            _LOGGER.warning(
                "SpotNav battery control: the acceptance of the Home Assistant down risk was cleared: the main fuse "
                "or the battery changed since it was given"
            )
            self._log([{"at": dt_util.now().isoformat(), "kind": "ha_down_cleared", "reason": "setup_changed"}])

    def shadow_default(self) -> bool:
        """Shadow only's default here (`gate.shadow_default`): the plant's recipe as last read (none read yet: on),
        and whether the site is a beta tester."""
        return gate.shadow_default(
            field_tested=recipe_field_tested(self.facts.brand),
            beta=learning_gate_open(self.hass, self.site.entry_id),
        )

    def _migrate_mode(self) -> None:
        """Once, at load: a site that allowed selling (`battery_control_allow_sell`, an early 3.0 build's option) and
        never chose a mode gets Max profit stored, and the legacy option is removed from the entry."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None or gate.CONF_ALLOW_SELL not in entry.data:
            return
        data = {key: value for key, value in entry.data.items() if key != gate.CONF_ALLOW_SELL}
        mode = gate.legacy_mode(entry.data)
        if mode is not None:
            data[gate.CONF_MODE] = mode
        self.hass.config_entries.async_update_entry(entry, data=data)

    def _integration_version(self, domain: str | None) -> str | None:
        if not domain:
            return None
        try:
            version = async_get_loaded_integration(self.hass, domain).version
        except Exception:  # noqa: BLE001 - not loaded (yet): the version is unknown
            _LOGGER.debug("integration_version: ignored failure (not loaded (yet): the version is unknown)", exc_info=True)
            return None
        return None if version is None else str(version)

    def firmware_key(self) -> str:
        """The plant's firmware and integration version as a report key; the last one read while the firmware reads
        nothing (a reading that blinks must not hand the plant back)."""
        facts = self.facts
        version = self._integration_version(facts.integration)
        if facts.firmware and (
            version is not None or self._key is None or not self._key.startswith(f"{facts.firmware} |")
        ):
            # An integration not loaded (yet) is no change of version: the key read before is kept.
            self._key = gate.firmware_key(facts.firmware, facts.integration, version)
        return self._with_revision(self._key or gate.firmware_key(None, facts.integration, version))

    def _with_revision(self, key: str) -> str:
        """A report key with the recipe's revision: a recipe changed since its first revision is checked again, as
        a firmware update is (`BrandControl.recipe_rev`, ` r<rev>` from 2)."""
        control = control_for(self.facts.control_brand)
        return key if control is None or control.recipe_rev <= 1 else f"{key} r{control.recipe_rev}"

    def firmware_known(self) -> bool:
        """Whether the plant's firmware was ever read (the last reading is kept while it blinks): without it a later
        firmware change could not be seen, so no check runs and no report counts."""
        return gate.firmware_known(self.firmware_key())

    def report(self) -> dict[str, Any] | None:
        if not self.firmware_known():
            return None
        key = self.firmware_key()
        report = self.reports.get(key)
        if not isinstance(report, dict):
            return None
        control = control_for(self.facts.control_brand)
        current = report if control is None else gate.without_removed_hold(report, control.variant_names())
        if current is not report:
            # A hold passed in a variant the recipe no longer has: pending again, so the routine checks it.
            report = self.reports[key] = dict(current)
        return report

    def control_report(self, key: str | None = None) -> Mapping[str, Any] | None:
        """The report control runs on (`gate.control_report`): the routine's own for the key now (`report`), with
        each mode still open there answered by the latest answer it has on the brand's earlier keys on this plant
        while the routine has not finished the key, so an update never stops control by itself. The reports are this site's; one passed
        on a key this release holds back (`commands.blocked`) is no source, nor one passed before the plant last
        failed a check that left no failed report (`carry_after`). The routine itself, the status's `report` and a measured
        expiry read `report`. `key`: read the stored report of that key as it stands instead (`_phases_mapped`)."""
        if key is None:
            if not self.firmware_known():
                return None
            key, own = self.firmware_key(), self.report()
        else:
            stored = self.reports.get(key)
            own = stored if isinstance(stored, dict) else None
        control = control_for(self.facts.control_brand)
        if control is None:
            return own
        # A key held back by this release, or one a person's hardware test failed on since its pass (also kept by a
        # version that stored no `carry_after`): its report is no pass, and hides no older one.
        void = {name for name in self.reports if blocked(control, name)} | set(self.hardware_failed)
        since = gate.parse_time(self.carry_after)
        if self.carry_after is not None and since is None:
            return own  # a stored time that cannot be read: nothing is carried
        return gate.control_report(
            own, self.reports, key=key, brand=control.brand, variants=control.variant_names(), since=since, void=void
        )

    def carried_from(self) -> dict[str, str] | None:
        """The steps control runs on an earlier key's answer while the routine checks the key now, each with the key
        it came from (by mode); `None` when control runs on the key's own report (or not at all)."""
        source = (self.control_report() or {}).get("carried_from")
        return dict(source) if isinstance(source, Mapping) and source else None

    def _phases_mapped(self) -> bool:
        """Whether the plant's phases A, B and C are shown to be the meter's L1, L2 and L3: only the verification's
        mode `per_phase`, passed on the firmware last read (or carried over an update, `control_report`), shows it
        (`gate.per_phase`). Reads the key as it stands (`firmware_key` would set it from the facts of this look)."""
        key = self._key
        if key is None or not gate.firmware_known(key):
            return False
        return gate.per_phase(self.control_report(self._with_revision(key))) == "verified"

    def state(self, options: gate.Options | None = None) -> str:
        """`off`, `shadow_only`, `verifying`, `waiting` (for the routine), `on` or `failed`; `on` also while
        control runs on a report carried over an update (`control_report`)."""
        options = options or self.options()
        if not options.enabled:
            return "off"
        if options.shadow_only:
            return "shadow_only"
        if self.verification is not None and not self.verification.done:
            return "verifying"
        status = gate.report_status(self.control_report())
        return {"passed": "on", "failed": "failed"}.get(status, "waiting")

    def allowed(self, options: gate.Options | None = None) -> frozenset[str]:
        options = options or self.options()
        if self.state(options) != "on":
            return frozenset()
        return self._supported(gate.allowed_actions(
            self.control_report(), options, export_needs_charge=self._export_needs_charge()
        ))

    def _supported(self, actions: frozenset[str]) -> frozenset[str]:
        """The actions the plant's recipe has (a limits-only recipe has no grid charge or sale): a report never allows
        what the executor would refuse."""
        control = control_for(self.facts.control_brand)
        return frozenset() if control is None else frozenset(action for action in actions if control.supports(action))

    def _supported_modes(self) -> frozenset[str] | None:
        """The battery modes the plant's recipe has an action for (`verify.supported_modes`), `None` without one."""
        control = control_for(self.facts.control_brand)
        return None if control is None else supported_modes(control)

    def _export_needs_charge(self) -> bool:
        """Whether this brand's discharge to the grid needs the charge step passed (`gate.allowed_actions`)."""
        control = control_for(self.facts.control_brand)
        return control is not None and control.export_signed_by_charge()

    def _require(self, ok: bool, what: str) -> None:
        if not ok:
            raise ControlNotReachable(f"{what} is not allowed by the site's battery control settings now")

    def _keep_report(self, key: str, report: dict[str, Any]) -> None:
        self.reports = gate.keep_reports(self.reports, key, report, most=MAX_REPORTS)

    # ---- the settings

    async def async_update_options(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Write some of the site's options (`gate.WRITABLE`: booleans, the battery mode and the wear) to its entry,
        without a reload (a wear of `None` removes the key: the currency's default); a changed mode or wear re-plans
        at once (the comparison's \"in force\" row moves with it); act
        on them at once: turned off or back to shadow only, the plant is handed back before this returns; turned on,
        the next look starts the routine. Answers the status."""
        wear_default = self.wear_default()
        unknown = [
            key for key, value in changes.items() if not gate.valid_change(key, value, wear_default=wear_default)
        ]
        if unknown:
            raise ValueError(f"not a battery control option: {', '.join(sorted(unknown))}")
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None:
            raise ValueError("the site is not loaded")
        # Under the lock: a look in progress finishes with the options it read before this write changes them.
        async with self._lock:
            before = self.options()
            stamped = dict(changes)
            if changes.get(gate.CONF_ACCEPT_HA_DOWN) is True:
                # Given for this setup: another main fuse or another brand clears it (`_ha_down_setup_look`).
                stamped[gate.CONF_ACCEPT_HA_DOWN_SETUP] = gate.ha_down_setup(self._main_fuse_a(), self.facts.brand)
            data = {**entry.data, **stamped}
            if gate.CONF_WEAR in stamped and stamped[gate.CONF_WEAR] is None:
                data.pop(gate.CONF_WEAR)
            self.hass.config_entries.async_update_entry(entry, data=data)
            after = self.options()
            _LOGGER.info("SpotNav battery control: options %s", after.as_dict())
            if before.mode != after.mode or before.wear_minor_per_kwh != after.wear_minor_per_kwh:
                self.site.battery_shadow.plan.mode_changed(dt_util.now())
            # The entry is written: nothing below may raise into the caller (the plant is handed back at the next
            # look if this hand-back fails).
            if self._started and not self._closed:
                trial = self.trial
                try:
                    if (before.writes and not after.writes) or (
                        trial is not None and not self._trial_allowed(trial, after)
                    ):
                        # A test action ends with the options that no longer allow it, forced or not.
                        # The export limit has its own switch: while that runs it, it is not handed back here.
                        await self._async_stop(
                            "disabled" if not after.enabled else "shadow_only", export=self.export.state(after) != "on"
                        )
                except Exception:  # noqa: BLE001
                    self._look_failed("options_stop")
                try:
                    if before.export_writes and not after.export_writes:
                        # "No export at negative prices" turned off: the limit is handed back before the answer.
                        await self.export.async_switched_off()
                except Exception:  # noqa: BLE001
                    self._look_failed("export_off")
                try:
                    await self._async_look(dt_util.now())
                except Exception:  # noqa: BLE001
                    self._look_failed("look")
        try:
            return self.status()
        except Exception:  # noqa: BLE001
            self._look_failed("status")
            return {"options": after.as_dict(), "state": self.state(after)}
