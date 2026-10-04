"""Control buttons for SpotNav charging control."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import AutoSurface, SpotNavAutoEntity, SpotNavChargingEntity
from .execution.auto_execution import (
    AutoControlError,
    AutoExecutor,
    EXECUTION_PAUSE_STOP_FAILED,
    EXECUTION_RECONCILE_FAILED,
    EXECUTION_VEHICLE_NOT_CONNECTED,
)
from .execution.controller import ChargingController
from .runtime import ChargerConfigEntry, executor_for


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller = entry.runtime_data.controller
    entities: list[Any] = [
        ControlButton(entry, controller, key, action)
        for key, action in manual_actions(executor_for(hass, entry.entry_id), controller)
    ]
    auto = AutoSurface.resolve(hass, entry.entry_id)
    entities.extend(
        [
            AutoRecalculateButton(entry, controller, auto),
            AutoPauseButton(entry, controller, auto),
            AutoResumeButton(entry, controller, auto),
            AutoClearDepartureDateButton(entry, controller, auto),
        ]
    )
    async_add_entities(entities)


def manual_actions(
    executor: AutoExecutor | None, controller: ChargingController
) -> list[tuple[str, Callable[[], Awaitable[None]]]]:
    """The four manual actions, routed through the charger's authority boundary.

    A button is a person deciding, so it takes the boundary like any manual action (no interleaving
    with Auto; `cancel` must not reinstate the plan it cancelled). The direct-controller fallback
    covers a missing boundary.
    """
    if executor is None:
        return [
            ("start", controller.async_start),
            ("stop", controller.async_stop),
            ("follow", controller.async_follow_schedule),
            ("cancel", controller.async_cancel),
        ]
    return [
        ("start", executor.async_manual_start),
        ("stop", executor.async_manual_stop),
        ("follow", executor.async_manual_follow),
        ("cancel", executor.async_manual_cancel),
    ]


class ControlButton(SpotNavChargingEntity, ButtonEntity):
    """Run one charger command."""

    def __init__(
        self,
        entry: ConfigEntry,
        controller: ChargingController,
        key: str,
        action: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._key = key
        self._action = action

    @property
    def available(self) -> bool:
        """The follow action requires an existing charging plan."""
        return self._key != "follow" or self.controller.plan is not None

    async def async_press(self) -> None:
        try:
            await self._action()
        except AutoControlError as err:
            # The boundary's stable code, as a sentence the person can read (`ChargingExecutionError` is one
            # already).
            if err.code in _TRANSLATED_CONTROL_CODES:
                raise HomeAssistantError(
                    err.code, translation_domain=DOMAIN, translation_key=err.code
                ) from err
            raise HomeAssistantError(f"The charger command failed ({err.code})") from err


#: The boundary's codes a control button can meet that have a translated sentence.
_TRANSLATED_CONTROL_CODES = frozenset({EXECUTION_VEHICLE_NOT_CONNECTED, EXECUTION_RECONCILE_FAILED})


class AutoActionButton(SpotNavAutoEntity, ButtonEntity):
    """Base for the three Auto actions: recalculate, pause and resume.

    Availability is a courtesy, never a boundary: the backend methods still validate every
    call, because a service call can reach a button that is unavailable.
    """

    def preview(self):
        """The preview this button acts on, or a concise refusal when there is none."""
        preview = self._auto.preview
        if preview is None:
            raise HomeAssistantError(
                f"Auto is not available for this charger ({self.unavailable_reason})"
            )
        return preview

    def require_auto_mode(self) -> None:
        """Refuse until this charger has an HA-owned settings record."""
        if self.settings is None:
            raise ServiceValidationError(
                "Automatic planning settings are unavailable for this charger",
                translation_domain=DOMAIN,
                translation_key="not_auto_mode",
            )


class AutoRecalculateButton(AutoActionButton):
    """Recalculate the Auto proposal now, from the prices already on hand."""

    _attr_translation_key = "recalculate_auto"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="recalculate_auto")

    @property
    def available(self) -> bool:
        """Only with a settings record: there is no proposal to recalculate without one."""
        settings = self.settings
        return super().available and settings is not None

    async def async_press(self) -> None:
        self.require_auto_mode()
        await self.preview().async_recalculate()


class AutoClearDepartureDateButton(AutoActionButton):
    """Return to a daily departure: forget the departure date."""

    _attr_translation_key = "clear_departure_date"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="clear_departure_date")

    @property
    def available(self) -> bool:
        settings = self.settings
        return super().available and settings is not None and settings.departure_date is not None

    async def async_press(self) -> None:
        self.require_auto_mode()
        await self.async_write_settings(lambda settings: replace(settings, departure_date=None))


class AutoPauseButton(AutoActionButton):
    """Stop and clear Auto's own plan, and apply nothing until resumed."""

    _attr_translation_key = "pause_auto"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="pause_auto")

    @property
    def available(self) -> bool:
        """Available while running, and after a pause whose stop failed so the retry stays reachable.

        A bounded pause that has run out counts as ended, per the boundary's own view.
        """
        settings = self.settings
        if settings is None:
            return False
        executor = self._auto.executor
        retryable = executor is not None and executor.last_error == EXECUTION_PAUSE_STOP_FAILED
        return super().available and (
            not self._pause_in_force(settings) or retryable
        )

    def _pause_in_force(self, settings: Any) -> bool:
        """Whether a pause still suspends this charger, from the one clock that owns the answer."""
        executor = self._auto.executor
        return settings.execution_paused if executor is None else executor.paused

    async def async_press(self) -> None:
        """Pause until a person resumes.

        The button takes the one choice that needs no question; bounded pauses are offered by
        interfaces that can ask, through the same boundary.
        """
        self.require_auto_mode()
        await self.preview().async_pause()


class AutoResumeButton(AutoActionButton):
    """Resume automatic execution and let the latest proposal be applied once."""

    _attr_translation_key = "resume_auto"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="resume_auto")

    @property
    def available(self) -> bool:
        """Only while paused: resuming what was never paused is not an action."""
        settings = self.settings
        if settings is None:
            return False
        return super().available and self._pause_in_force(settings)

    def _pause_in_force(self, settings: Any) -> bool:
        """Whether a pause still suspends this charger, from the one clock that owns the answer."""
        executor = self._auto.executor
        return settings.execution_paused if executor is None else executor.paused

    async def async_press(self) -> None:
        self.require_auto_mode()
        await self.preview().async_resume()
