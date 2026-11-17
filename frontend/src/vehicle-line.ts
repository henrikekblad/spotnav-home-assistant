// The planned vehicle as the car button above the action bar shows it, with its charge, how old its reading is
// (shown only in the dialog the button opens), and the list the switcher dialog shows. Pure facts from the
// dashboard; the view only draws them.
//
// Nothing here invents a figure: no vehicle chosen or no reading means no line at all.

import { formatNumber, percentAmount } from "./format";
import { translate, type Language } from "./i18n";
import { SETTINGS_DRIVER_TARGET_SOC, type ConnectionState, type SettingsRecord } from "./types";
import { GUEST_VEHICLE_ID, type Soc, type Vehicle } from "./validate";

export interface VehicleLineFacts {
  vehicleId: string;
  /** The vehicle's own name, or `null` when it has none: the line then starts with the charge. */
  name: string | null;
  /** `92 %`, or `62 % of 80 % target` while the target drives the plan. */
  charge: string;
  /** `~` while the figure is an estimate between readings. */
  estimatePrefix: string;
  /** `~62 % · read 3 h ago`: the charge and the age of its reading, for the car dialog; `null` without an age. */
  reading: string | null;
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
  const name = soc.vehicle_name !== null && soc.vehicle_name.trim() !== "" ? soc.vehicle_name : null;
  const target =
    settings?.driver === SETTINGS_DRIVER_TARGET_SOC
      ? (settings.target.target_percent ?? soc.target_percent)
      : null;
  const now = percentAmount(language, soc.value);
  const charge =
    target === null
      ? now
      : translate(language, "vehicleLine.ofTarget", { level: now, target: percentAmount(language, target) });
  const estimatePrefix = soc.estimated ? "~" : "";
  const reading =
    soc.age_s === null
      ? null
      : translate(language, "vehicleLine.reading", {
          charge: `${estimatePrefix}${charge}`,
          age: ageSentence(language, soc.age_s),
        });
  return {
    vehicleId: soc.vehicle_id,
    name,
    charge,
    estimatePrefix,
    reading,
  };
}

/** A guest car's line: "Guest car · 10 kWh", the amount its plan charges (it has no level of its own). */
export function guestLineFor(language: Language, settings: SettingsRecord | null): VehicleLineFacts {
  const name = translate(language, "identify.guest");
  const kwh = settings?.requested_kwh ?? null;
  const charge = kwh === null || !Number.isFinite(kwh) ? "" : `${formatNumber(language, kwh, 1)} kWh`;
  return {
    vehicleId: GUEST_VEHICLE_ID,
    name,
    charge,
    estimatePrefix: "",
    reading: null,
  };
}

/** The words for a connection state, or `null` for `unknown` or none: then the header says nothing. */
export function connectionLabel(language: Language, connection: ConnectionState | null): string | null {
  if (connection === null || connection.shown === "unknown") {
    return null;
  }
  return translate(language, `connection.${connection.shown}`);
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
