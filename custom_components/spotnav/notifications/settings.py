"""Which phones hear about which events: one charger's notification settings, and their codec.

Pure: no Home Assistant imports. Stored with the charger's Auto settings (`AutoSettings.notifications`)
and carried in the settings record as the additive `notifications` field.

* `targets` are services of Home Assistant's `notify` domain, normally the Companion app's
  `mobile_app_<device>`. A target that no longer exists is kept (a phone being re-registered) and
  skipped when sending.
* `events` are the events that notify (`NOTIFICATION_EVENTS`). With no target nothing is sent, so the
  defaults (`DEFAULT_EVENTS`) are what a first chosen target starts with.
* `url` is where a tap on the notification opens Home Assistant (the dashboard the card is on), a path
  of the Home Assistant frontend; `None` opens Home Assistant's default dashboard.

Per charger, not per pairing: a pairing is not remembered past its handshake (`api/pairing.py`), the
webhook, the card and the events are all one charger's, and a household with two chargers may want
each car's driver to hear about their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

#: A charge SpotNav expected (a plan window is open) stopped or did not start (`unexpected_stop.py`).
EVENT_PLAN_STOPPED: Final = "plan_stopped"
#: The departure cannot be met with the energy that remains.
EVENT_PLAN_AT_RISK: Final = "plan_at_risk"
#: The target or the requested energy was reached, or the plan's last window finished a charge.
EVENT_CHARGE_COMPLETE: Final = "charge_complete"
EVENT_CHARGE_STARTED: Final = "charge_started"
EVENT_PLUGGED_IN: Final = "plugged_in"
EVENT_UNPLUGGED: Final = "unplugged"
EVENT_PLAN_INSTALLED: Final = "plan_installed"

#: Every event a notification can be sent for, in the order a client lists them.
NOTIFICATION_EVENTS: Final = (
    EVENT_PLAN_STOPPED,
    EVENT_PLAN_AT_RISK,
    EVENT_CHARGE_COMPLETE,
    EVENT_CHARGE_STARTED,
    EVENT_PLUGGED_IN,
    EVENT_UNPLUGGED,
    EVENT_PLAN_INSTALLED,
)
#: On until a person turns them off: what needs attention, and the end of a charge.
DEFAULT_EVENTS: Final = (EVENT_PLAN_STOPPED, EVENT_PLAN_AT_RISK, EVENT_CHARGE_COMPLETE)

#: The domain whose services are the targets.
NOTIFY_DOMAIN: Final = "notify"
#: The Companion app's services: the ones a client offers to choose.
MOBILE_APP_PREFIX: Final = "mobile_app_"

#: A service name as Home Assistant spells one (a slug).
_SERVICE = re.compile(r"^[a-z0-9_]{1,100}$")
#: A frontend path: one leading slash, no scheme, no host, no spaces.
_PATH = re.compile(r"^/(?!/)[A-Za-z0-9_\-./?=&%#]{0,200}$")
MAX_TARGETS: Final = 10

#: The wire keys; `available` is read-only (the notify services that exist), accepted and ignored.
NOTIFICATION_KEYS: Final = frozenset({"targets", "events"})
OPTIONAL_NOTIFICATION_KEYS: Final = frozenset({"url", "available"})


class NotificationSettingsError(ValueError):
    """A notification setting that cannot be stored; the message is for the log only."""


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    targets: tuple[str, ...] = ()
    events: tuple[str, ...] = DEFAULT_EVENTS
    url: str | None = None

    @property
    def is_default(self) -> bool:
        return self == NotificationSettings()

    def wants(self, event: str) -> bool:
        """Whether `event` is sent anywhere."""
        return bool(self.targets) and event in self.events

    def validated(self) -> NotificationSettings:
        """The same settings in canonical order, or a refusal."""
        if not isinstance(self.targets, tuple) or len(self.targets) > MAX_TARGETS:
            raise NotificationSettingsError(f"at most {MAX_TARGETS} notify targets")
        for target in self.targets:
            if not isinstance(target, str) or not _SERVICE.match(target):
                raise NotificationSettingsError("a notify target must be a notify service name")
        if len(set(self.targets)) != len(self.targets):
            raise NotificationSettingsError("a notify target must not repeat")
        if not isinstance(self.events, tuple):
            raise NotificationSettingsError("events must be a list")
        for event in self.events:
            if event not in NOTIFICATION_EVENTS:
                raise NotificationSettingsError("an event must be one of the notification events")
        if len(set(self.events)) != len(self.events):
            raise NotificationSettingsError("an event must not repeat")
        if self.url is not None and (not isinstance(self.url, str) or not _PATH.match(self.url)):
            raise NotificationSettingsError("url must be a Home Assistant path starting with one /")
        return NotificationSettings(
            targets=self.targets,
            events=tuple(event for event in NOTIFICATION_EVENTS if event in self.events),
            url=self.url,
        )

    def as_dict(self) -> dict[str, Any]:
        """The stored and the wire shape (without the read-only `available`)."""
        return {"targets": list(self.targets), "events": list(self.events), "url": self.url}

    @classmethod
    def from_dict(cls, raw: Any) -> NotificationSettings:
        """A stored record or a wire body: `targets` and `events` required, `url` optional, `available`
        ignored, nothing else; nothing coerced."""
        if not isinstance(raw, dict):
            raise NotificationSettingsError("notifications must be an object")
        missing = NOTIFICATION_KEYS - set(raw)
        if missing:
            raise NotificationSettingsError(f"notifications is missing {sorted(missing)}")
        unknown = set(raw) - NOTIFICATION_KEYS - OPTIONAL_NOTIFICATION_KEYS
        if unknown:
            raise NotificationSettingsError("notifications has unknown fields")
        targets, events = raw["targets"], raw["events"]
        if not isinstance(targets, list) or not isinstance(events, list):
            raise NotificationSettingsError("targets and events must be lists")
        return cls(targets=tuple(targets), events=tuple(events), url=raw.get("url")).validated()
