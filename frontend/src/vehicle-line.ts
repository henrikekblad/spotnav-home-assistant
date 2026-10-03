// The vehicle line under the charger name: the one vehicle the charger plans for, with its charge,
// and the list the switcher dialog shows. Pure facts from the dashboard; the view only draws them.
//
// Nothing here invents a figure: no vehicle chosen or no reading means no line at all.

import { percentAmount } from "./format";
import { translate, type Language } from "./i18n";
import { SETTINGS_DRIVER_TARGET_SOC, type ConnectionState, type SettingsRecord } from "./types";
import type { Soc, Vehicle } from "./validate";

/** A reading older than this (seconds), that is not an estimate, says how old it is on the line. */
export const STALE_READING_S = 3600;

export interface VehicleLineFacts {
  vehicleId: string;
  name: string;
  /** `92 %`, or `62 % → 80 %` while the target drives the plan. */
  charge: string;
  /** `~` while the figure is an estimate between readings. */
  estimatePrefix: string;
  /** `3 h ago`, only for a stale, non-estimated reading. */
  age: string | null;
  /** The tooltip of an estimate: `Estimated between readings, read 12 min ago`. */
  estimateTitle: string | null;
  ariaLabel: string;
}

export interface VehicleChoice {
  id: string;
  name: string;
  /** The vehicle's own charge, or `null` when it has no reading. */
  charge: string | null;
  selected: boolean;
}

/** How long ago, in the card's own words ("just now", "5 min ago", "3 h ago", "2 d ago"). */
export function ageSentence(language: Language, seconds: number): string {
  if (seconds < 90) {
    return translate(language, "settings.soc.age.now");
  }
  if (seconds < 3600) {
    return translate(language, "settings.soc.age.min", { count: String(Math.floor(seconds / 60)) });
  }
  if (seconds < 86400) {
    return translate(language, "settings.soc.age.hour", { count: String(Math.floor(seconds / 3600)) });
  }
  return translate(language, "settings.soc.age.day", { count: String(Math.floor(seconds / 86400)) });
}

function vehicleName(language: Language, name: string | null): string {
  return name !== null && name.trim() !== "" ? name : translate(language, "settings.vehicle.unnamed");
}

export function vehicleLineFor(
  language: Language,
  soc: Soc | null,
  settings: SettingsRecord | null,
): VehicleLineFacts | null {
  if (soc === null || soc.vehicle_id === null || soc.value === null) {
    return null;
  }
  const name = vehicleName(language, soc.vehicle_name);
  const target =
    settings?.driver === SETTINGS_DRIVER_TARGET_SOC
      ? (settings.target.target_percent ?? soc.target_percent)
      : null;
  const now = percentAmount(language, soc.value);
  const charge = target === null ? now : `${now} → ${percentAmount(language, target)}`;
  const estimatePrefix = soc.estimated ? "~" : "";
  const age = !soc.estimated && soc.age_s !== null && soc.age_s > STALE_READING_S ? ageSentence(language, soc.age_s) : null;
  const estimateTitle =
    soc.estimated && soc.age_s !== null
      ? translate(language, "vehicleLine.estimateTitle", { age: ageSentence(language, soc.age_s) })
      : null;
  const spoken = [`${estimatePrefix}${charge}`, estimateTitle ?? age].filter((part) => part !== null).join(", ");
  return {
    vehicleId: soc.vehicle_id,
    name,
    charge,
    estimatePrefix,
    age,
    estimateTitle,
    ariaLabel: translate(language, "vehicleLine.aria", { name, summary: spoken }),
  };
}

/** The words for a connection state, or `null` for `unknown` or none: then the header says nothing. */
export function connectionLabel(language: Language, connection: ConnectionState | null): string | null {
  if (connection === null || connection.state === "unknown") {
    return null;
  }
  return translate(language, `connection.${connection.state}`);
}

export function vehicleChoicesFor(
  language: Language,
  vehicles: readonly Vehicle[],
  plannedId: string | null,
): VehicleChoice[] {
  return vehicles.map((vehicle) => ({
    id: vehicle.id,
    name: vehicleName(language, vehicle.name),
    charge: vehicle.soc_percent === null ? null : percentAmount(language, vehicle.soc_percent),
    selected: vehicle.id === plannedId,
  }));
}
