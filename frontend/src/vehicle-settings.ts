// One car's section on the Settings page, in the app's compact style: "Bil · EV6" with its icon, and one value
// row per property. A changeable value is tappable and opens its own editor (the view builds those); the page
// only reports the tap, and the card owns the requests and every refusal.

import { formatFixed, formatNumber } from "./format";
import { sourceText } from "./identification";
import { translate, type Language } from "./i18n";
import type { Vehicle } from "./validate";
import { sectionHeading, settingRow } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** The editors a car's rows open; a row without one is read-only. */
export interface VehicleEdits {
  sensor?: () => void;
  target?: () => void;
  capacity?: () => void;
  consumption?: () => void;
  onboard?: () => void;
  plug?: () => void;
  location?: () => void;
}

export interface VehicleSummaryInput {
  row: Vehicle;
  /** `false` for a vehicle only its sensors are known for: no capacity or consumption rows. */
  properties: boolean;
  planned: boolean;
  /** The vehicle's charge as the header line spells it (`~36 %`, `36 %`, or `No reading`). */
  charge: string;
  edits: VehicleEdits;
}

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

export function vehicleSummary(doc: Document, language: Language, input: VehicleSummaryInput): HTMLElement {
  const { row, edits } = input;
  const card = element(doc, "section", C.settingsSection);
  card.dataset["section"] = "vehicle";
  card.dataset["vehicle"] = row.id;
  card.dataset["planned"] = String(input.planned);
  card.append(
    sectionHeading(doc, "car", translate(language, "settings.heading.car"), row.name ?? translate(language, "settings.vehicle.unnamed")),
  );
  if (input.planned) {
    const mark = element(doc, "p", C.settingsNote, translate(language, "settings.vehicle.plannedHere"));
    mark.dataset["vehicleMark"] = "planned";
    card.append(mark);
  }
  const valueRow = (key: string, label: string, value: string, tap?: () => void, help?: string): void => {
    card.append(
      ...settingRow(doc, {
        key,
        label,
        value,
        ...(help === undefined ? {} : { help }),
        ...(tap === undefined
          ? {}
          : { onTap: tap, changeableText: translate(language, "settings.row.changeable", { label, value }) }),
      }),
    );
  };
  const notSet = translate(language, "entity.notSet");
  valueRow("charge_level", translate(language, "settings.vehicle.charge"), input.charge, edits.sensor);
  if (input.properties) {
    if (row.target_percent !== undefined) {
      valueRow(
        "target",
        translate(language, "settings.soc.target"),
        row.target_percent === null ? notSet : `${formatNumber(language, row.target_percent, 0)} %`,
        edits.target,
        translate(language, "settings.vehicle.targetHelp"),
      );
    }
    const reported = row.capacity_source === "reported" && row.capacity_kwh !== null;
    valueRow(
      "capacity",
      translate(language, "settings.capacity.label"),
      row.capacity_kwh === null ? notSet : `${formatFixed(language, row.capacity_kwh, 1)} kWh`,
      reported ? undefined : edits.capacity,
      reported ? translate(language, "settings.vehicle.capacityReported") : undefined,
    );
    valueRow(
      "consumption",
      translate(language, "settings.consumption.label"),
      row.consumption_kwh_per_10km === null
        ? notSet
        : `${formatFixed(language, row.consumption_kwh_per_10km, 1)} ${translate(language, "settings.consumption.unit")}`,
      edits.consumption,
    );
    valueRow(
      "onboard",
      translate(language, "settings.vehicle.onboardLegend"),
      translate(language, row.onboard_phases === 1 ? "settings.vehicle.onboardOne" : "settings.vehicle.onboardThree"),
      edits.onboard,
    );
  }
  if (row.identification !== undefined) {
    // What tells which car is plugged in: read only for that, never to start or stop a charge.
    card.append(element(doc, "hr", C.settingsDivider));
    valueRow("plug", translate(language, "identify.source.plug"), sourceText(language, row.identification.plug), edits.plug);
    valueRow(
      "location",
      translate(language, "identify.source.location"),
      sourceText(language, row.identification.location),
      edits.location,
      translate(language, "settings.vehicle.sourcesHelp"),
    );
  }
  return card;
}
