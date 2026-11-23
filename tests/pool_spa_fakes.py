"""Fake pool, spa and sauna devices and a stub site for the pool and spa control's tests (`pool/control.py`,
`spa/control.py`): every service SpotNav may call is a fake that records the call and shows the new value unless told
to ignore it. Nothing reaches a device."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.spotnav.heat.config import read_config
from custom_components.spotnav.pool.control import PoolSpaControl
from custom_components.spotnav.spa.runtime import Reading

TZ = "Europe/Stockholm"
NOW = datetime.fromisoformat("2026-07-01T10:00:00+00:00")
S = timedelta(seconds=1)
MIN = timedelta(minutes=1)
H = timedelta(hours=1)

_SERVICES = {
    "switch": ("turn_on", "turn_off"),
    "input_boolean": ("turn_on", "turn_off"),
    "climate": ("set_hvac_mode", "set_temperature", "set_preset_mode"),
    "water_heater": ("set_temperature", "set_operation_mode"),
    "number": ("set_value",),
}


class Devices:
    """Every fake entity, by id: its state and attributes; each call recorded as `(domain.service, entity, value)`."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str, Any]] = []
        self.ignore: set[str] = set()
        self.refuse: set[str] = set()
        self.entities: dict[str, tuple[str, dict[str, Any]]] = {}
        for domain, services in _SERVICES.items():
            for service in services:
                hass.services.async_register(domain, service, self._handler(domain, service))

    def add(self, entity_id: str, state: str, **attributes: Any) -> None:
        self.entities[entity_id] = (state, attributes)
        self.hass.states.async_set(entity_id, state, attributes)

    def set(self, entity_id: str, state: str | None = None, **attributes: Any) -> None:
        old_state, old = self.entities[entity_id]
        self.add(entity_id, old_state if state is None else state, **{**old, **attributes})

    def state(self, entity_id: str) -> str:
        return self.entities[entity_id][0]

    def attr(self, entity_id: str, key: str) -> Any:
        return self.entities[entity_id][1].get(key)

    def _handler(self, domain: str, service: str):  # noqa: ANN202
        async def handle(call: ServiceCall) -> None:
            entity_id = call.data["entity_id"]
            if entity_id in self.refuse:
                raise RuntimeError("refused")
            value = {
                "turn_on": "on",
                "turn_off": "off",
                "set_hvac_mode": call.data.get("hvac_mode"),
                "set_temperature": call.data.get("temperature"),
                "set_preset_mode": call.data.get("preset_mode"),
                "set_operation_mode": call.data.get("operation_mode"),
                "set_value": call.data.get("value"),
            }[service]
            self.calls.append((f"{domain}.{service}", entity_id, value))
            if entity_id in self.ignore:
                return
            if service in ("turn_on", "turn_off", "set_hvac_mode", "set_operation_mode", "set_value"):
                self.set(entity_id, str(value))
            elif service == "set_temperature":
                self.set(entity_id, temperature=value)
            elif service == "set_preset_mode":
                # Balboa: its hvac mode follows the preset (Ready is heat, Rest is off).
                self.set(entity_id, {"ready": "heat", "rest": "off"}.get(str(value), "auto"), preset_mode=value)

        return handle

    def writes(self, entity_id: str | None = None) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if entity_id is None or call[1] == entity_id]


class Order:
    """The site's shed order as the control reads it: a live step per load until a time."""

    def __init__(self) -> None:
        self.steps: dict[str, datetime] = {}

    def until(self, load: str, now: datetime) -> datetime | None:
        until = self.steps.get(load)
        return until if until is not None and until > now else None


class Deadline:
    """The spa's or the sauna's shadow as the control reads it."""

    def __init__(self) -> None:
        self.plan: Any = None
        self.reading = Reading(None, None)
        self.action: str | None = None

    def set(self, action: str | None, *, current: float | None = None, session: datetime | None = None, locked: bool | None = None, max_on: timedelta | None = None) -> None:
        self.action = action
        self.plan = None if action is None else SimpleNamespace(action=action, next_session=None if session is None else (session, "weekly"))
        self.reading = Reading(current, None, locked, max_on)

    def would(self, _config: Any) -> dict[str, Any] | None:
        return None if self.action is None else {"action": self.action, "until": None, "reason": "bath"}


class Shadow:
    """The pool and spa shadow as the control reads it: the settings, the pool's plan now and the deadlines'."""

    def __init__(self, heat: dict[str, Any]) -> None:
        self.raw = heat
        self.would: dict[str, Any] | None = None
        self.spa = Deadline()
        self.sauna = Deadline()
        self.tz = TZ

    def config(self):  # noqa: ANN201
        return read_config(self.raw)

    def pool_action(self, action: str | None) -> None:
        self.would = None if action is None else {"action": action, "until": None, "reason": "cheap"}


def make_control(hass: HomeAssistant, heat: dict[str, Any], entry_id: str = "site_a") -> tuple[PoolSpaControl, Shadow, Order]:
    shadow = Shadow(heat)
    order = Order()
    site = SimpleNamespace(entry_id=entry_id, pool_spa=shadow, shed_order=order)
    control = PoolSpaControl(hass, site)
    control._started = True
    return control, shadow, order


async def looks(control: PoolSpaControl, start: datetime, end: datetime, step: timedelta = 30 * S) -> datetime:
    """Look every `step` from `start` up to `end`; the last look's time."""
    moment = start
    while moment <= end:
        await control.async_look(moment)
        moment += step
    return moment - step
