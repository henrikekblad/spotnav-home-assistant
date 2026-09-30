"""The connector's entities, registered with the entity-id and unique-id shapes the real integration emits."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import DOMAIN


class ConnectorEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, domain: str, cpid: str, label: str, key: str, title: str, *, connector: bool = True) -> None:
        device = f"{cpid}-1" if connector else cpid
        self._attr_name = title
        self._attr_unique_id = f"{domain}.{DOMAIN}.{cpid}.conn1.{key}" if connector else f"{domain}.{DOMAIN}.{cpid}.{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device)})
        self._object_id = f"{cpid}_connector_1_{key}" if connector else f"{cpid}_{key}"
        self.entity_id = f"{domain}.{self._object_id}"
