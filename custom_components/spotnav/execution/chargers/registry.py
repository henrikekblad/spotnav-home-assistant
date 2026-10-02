"""Where each charger module registers how its path is built.

A start/stop factory is looked up by the stored path `kind`, a current factory by the current-control
kind. Both receive one `ChargerContext` and return the path, or `None` when the stored configuration
is incomplete for that kind (the caller then falls back to the generic path). Adding a vendor is a new
module that calls `register_start_stop` / `register_current`; neither `adapter.py` nor the controller
changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from ..charger_profiles import PlatformProfile, PATH_SWITCH, WritePolicy
from ...vehicles.ocpp_identity import OcppConnectorTarget
from .base import CurrentPath, StartStopPath, WriteRateLimiter


@dataclass(frozen=True, slots=True)
class ChargerContext:
    """Everything a path factory may need, collected once by `build_adapter`."""

    hass: HomeAssistant
    #: The charger's stored configuration entry data, and the stored control path inside it.
    config: dict[str, Any]
    raw_path: dict[str, Any]
    profile: PlatformProfile | None
    policy: WritePolicy
    limiter: WriteRateLimiter
    now: Callable[[], datetime]
    charge_control: str
    current_limit: str | None
    status_entity_id: str | None
    #: The adapter's own readings, available once it is built (a path calls them lazily).
    status: Callable[[], str | None]
    read_back: Callable[[], int | None]
    enabled_for_writes: Callable[[], bool | None]
    ocpp_target: Callable[[], OcppConnectorTarget | None]
    #: The start/stop path already built; set for the current factories.
    path: StartStopPath | None = None


StartStopFactory = Callable[[ChargerContext], StartStopPath | None]
CurrentFactory = Callable[[ChargerContext], CurrentPath | None]
InstallationCheck = Callable[[HomeAssistant, str], bool]
#: Which other system, if any, writes an installation-wide current limit of this charger's platform.
ExternalBalancerCheck = Callable[[HomeAssistant], str | None]

_START_STOP: dict[str, StartStopFactory] = {}
_CURRENT: dict[str, CurrentFactory] = {}
_installation_check: InstallationCheck | None = None
_external_balancer_check: ExternalBalancerCheck | None = None


def register_start_stop(kind: str, factory: StartStopFactory) -> None:
    _START_STOP[kind] = factory


def register_current(kind: str, factory: CurrentFactory) -> None:
    _CURRENT[kind] = factory


def register_installation_check(check: InstallationCheck) -> None:
    """How to tell that the installation a limit number belongs to has exactly one charger."""
    global _installation_check
    _installation_check = check


def installation_check() -> InstallationCheck | None:
    return _installation_check


def register_external_balancer(check: ExternalBalancerCheck) -> None:
    """How to tell that another system writes the installation-wide limit (Perific for Zaptec)."""
    global _external_balancer_check
    _external_balancer_check = check


def external_balancer(hass: HomeAssistant) -> str | None:
    """The domain of the system that balances the installation-wide limit, or `None`."""
    return _external_balancer_check(hass) if _external_balancer_check is not None else None


def build_start_stop(context: ChargerContext) -> StartStopPath:
    """The path for the stored kind, or the plain switch when the kind is unknown or incomplete."""
    factory = _START_STOP.get(context.raw_path.get("kind"))
    path = factory(context) if factory is not None else None
    if path is None:
        path = _START_STOP[PATH_SWITCH](context)
    assert path is not None
    return path


def build_current(kind: str, context: ChargerContext) -> CurrentPath | None:
    """The current path for the current-control kind, or `None` (no current can be set)."""
    factory = _CURRENT.get(kind)
    return factory(context) if factory is not None else None
