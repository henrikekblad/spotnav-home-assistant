// Home Assistant's own words for an entity the backend names in a status line: the frontend formats names and
// states (translated by the entity's integration), never this card. Everything is optional on `hass`, so an older
// Home Assistant, or an entity the frontend does not know, gives `null` and the card says the backend's raw words.

import type { EntityWords } from "./format";
import type { HassStateLike, HomeAssistantLike } from "./types";

export function entityWordsOf(hass: HomeAssistantLike | null): EntityWords | undefined {
  if (hass === null || hass.states === undefined) {
    return undefined;
  }
  const stateOf = (entityId: string): HassStateLike | null => hass.states?.[entityId] ?? null;
  return {
    name(entityId) {
      const stateObj = stateOf(entityId);
      if (stateObj === null) {
        return null;
      }
      try {
        const formatted = hass.formatEntityName?.(stateObj, { type: "entity" });
        if (typeof formatted === "string" && formatted.trim() !== "") {
          return formatted.trim();
        }
      } catch {
        // An older formatter: the friendly name below.
      }
      return withoutDevice(hass, entityId, stateObj);
    },
    state(entityId, value) {
      const stateObj = stateOf(entityId);
      if (stateObj === null || hass.formatEntityState === undefined) {
        return null;
      }
      try {
        const formatted = value === undefined ? hass.formatEntityState(stateObj) : hass.formatEntityState(stateObj, value);
        return typeof formatted === "string" && formatted.trim() !== "" ? formatted.trim() : null;
      } catch {
        return null;
      }
    },
  };
}

/** The friendly name without the device's name in front, as Home Assistant shows an entity on its device page. */
function withoutDevice(hass: HomeAssistantLike, entityId: string, stateObj: HassStateLike): string | null {
  const friendly = stateObj.attributes["friendly_name"];
  if (typeof friendly !== "string" || friendly.trim() === "") {
    return null;
  }
  const deviceId = hass.entities?.[entityId]?.device_id ?? null;
  const device = deviceId === null ? undefined : hass.devices?.[deviceId];
  const deviceName = (device?.name_by_user ?? device?.name ?? "").trim();
  if (deviceName !== "" && friendly.startsWith(`${deviceName} `)) {
    return friendly.slice(deviceName.length + 1).trim() || friendly;
  }
  return friendly.trim();
}
