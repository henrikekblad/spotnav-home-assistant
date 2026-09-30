// One vehicle's summary card on the Settings page: name, charge-level sensor, battery capacity and
// consumption, and the button that opens its dialog (`entity-editor.ts`, `vehicleEditorBody`). The
// page only reports the press; the card owns the requests and every refusal.

import { formatFixed } from "./format";
import { translate, type Language } from "./i18n";
import type { VehicleSoc } from "./entity-config";
import type { Vehicle } from "./validate";
import { VISUAL_CLASSES as C } from "./visual-styles";

export interface VehicleSummaryInput {
  row: Vehicle;
  /** `false` for a vehicle only its sensors are known for: no capacity or consumption rows. */
  properties: boolean;
  planned: boolean;
  sensor: VehicleSoc | null | undefined;
  isAdmin: boolean;
  onChange: () => void;
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

function sensorText(language: Language, sensor: VehicleSoc): string {
  if (sensor.selected === null) {
    return translate(language, "settings.vehicle.socNone");
  }
  return sensor.source === "automatic"
    ? translate(language, "entity.automatic", { name: sensor.selected.friendlyName })
    : sensor.selected.friendlyName;
}

export function vehicleSummary(doc: Document, language: Language, input: VehicleSummaryInput): HTMLElement {
  const { row } = input;
  const card = element(doc, "section", C.settingsSection);
  card.dataset["section"] = "vehicle";
  card.dataset["vehicle"] = row.id;
  card.dataset["planned"] = String(input.planned);
  card.append(
    element(doc, "h4", C.settingsSectionHeading, row.name ?? translate(language, "settings.vehicle.unnamed")),
  );
  if (input.planned) {
    const mark = element(doc, "p", C.settingsNote, translate(language, "settings.vehicle.plannedHere"));
    mark.dataset["vehicleMark"] = "planned";
    card.append(mark);
  }
  const valueRow = (key: string, label: string, value: string): void => {
    const line = element(doc, "div", C.capabilityItem);
    line.dataset["row"] = key;
    line.append(element(doc, "span", C.capabilityLabel, label), element(doc, "span", C.settingsValue, value));
    card.append(line);
  };
  const notSet = translate(language, "entity.notSet");
  let sensor: string | null = null;
  if (input.sensor !== undefined && input.sensor !== null) {
    sensor = sensorText(language, input.sensor);
  } else if (row.soc_entity_id !== null) {
    sensor = row.soc_entity_id;
  }
  valueRow("vehicle_soc", translate(language, "entity.field.vehicleSoc"), sensor ?? notSet);
  if (input.properties) {
    valueRow(
      "capacity",
      translate(language, "settings.capacity.label"),
      row.capacity_kwh === null ? notSet : `${formatFixed(language, row.capacity_kwh, 1)} kWh`,
    );
    valueRow(
      "consumption",
      translate(language, "settings.consumption.label"),
      row.consumption_kwh_per_10km === null
        ? notSet
        : `${formatFixed(language, row.consumption_kwh_per_10km, 1)} ${translate(language, "settings.consumption.unit")}`,
    );
  }
  const button = element(doc, "button", `${C.button} ${C.settingsSectionConfigure}`, translate(language, "settings.vehicle.change"));
  (button as HTMLButtonElement).type = "button";
  (button as HTMLButtonElement).disabled = !input.isAdmin;
  button.dataset["editVehicle"] = row.id;
  button.addEventListener("click", input.onChange);
  card.append(button);
  return card;
}
