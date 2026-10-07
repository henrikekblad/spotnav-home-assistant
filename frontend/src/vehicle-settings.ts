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
  minimum?: () => void;
  capacity?: () => void;
  consumption?: () => void;
  onboard?: () => void;
  plug?: () => void;
  location?: () => void;
}

/** A car's reference pictures for the camera: the words, the kinds it has, their thumbnails, and its editor. */
export interface VehicleReference {
  text: string;
  kinds: ReadonlyArray<"day" | "night">;
  thumbnail: (kind: "day" | "night") => Promise<{ url: string } | null>;
  onTap?: () => void;
}

export interface VehicleSummaryInput {
  row: Vehicle;
  /** `false` for a vehicle only its sensors are known for: no capacity or consumption rows. */
  properties: boolean;
  planned: boolean;
  /** The vehicle's charge as the header line spells it (`~36 %`, `36 %`, or `No reading`). */
  charge: string;
  edits: VehicleEdits;
  /** With a camera chosen: the car's reference pictures. */
  reference?: VehicleReference | undefined;
  /** With two or more cars: the cars' tabs, shown under the section's "Car" heading in place of the name. */
  tabs?: HTMLElement;
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
  if (input.tabs !== undefined) {
    card.append(sectionHeading(doc, "car", translate(language, "settings.heading.car")), input.tabs);
  } else {
    card.append(
      sectionHeading(doc, "car", translate(language, "settings.heading.car"), row.name ?? translate(language, "settings.vehicle.unnamed")),
    );
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
    // The car's own charge limit, as the car reports it. Read-only here: the card has no request that writes it.
    if (row.max_percent !== null) {
      valueRow("charge_limit", translate(language, "settings.vehicle.limit"), `${formatNumber(language, row.max_percent, 0)} %`);
    }
    // The car's own target, the same at every charger, after its limit as in the app.
    if (row.target_percent !== undefined) {
      valueRow(
        "target",
        translate(language, "settings.soc.target"),
        row.target_percent === null ? notSet : `${formatNumber(language, row.target_percent, 0)} %`,
        edits.target,
      );
    }
    // The car's minimum charge level: below it SpotNav charges at once. It needs the car's level to act on.
    if (row.min_percent !== undefined) {
      const level =
        row.min_percent === null
          ? translate(language, "settings.vehicle.minimumOff")
          : `${formatNumber(language, row.min_percent, 0)} %`;
      valueRow(
        "min_percent",
        translate(language, "settings.vehicle.minimum"),
        row.min_percent !== null && row.soc_entity_id === null
          ? `${level} · ${translate(language, "settings.vehicle.minimumNeedsSoc")}`
          : level,
        edits.minimum,
      );
    }
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
  const reference = input.reference;
  if (reference !== undefined) {
    // The camera compares the car at the charger with these pictures; without one the car is not recognised.
    if (row.identification === undefined) {
      card.append(element(doc, "hr", C.settingsDivider));
    }
    valueRow(
      "reference",
      translate(language, "reference.label"),
      reference.text,
      reference.onTap,
    );
    if (reference.kinds.length > 0) {
      const thumbs = element(doc, "div", C.referenceThumbs);
      thumbs.dataset["referenceThumbs"] = row.id;
      for (const kind of reference.kinds) {
        const image = doc.createElement("img");
        image.className = C.referenceThumb;
        image.alt = translate(language, kind === "day" ? "reference.day" : "reference.night");
        image.dataset["referenceKind"] = kind;
        image.hidden = true;
        thumbs.append(image);
        void reference.thumbnail(kind).then((picture) => {
          if (picture !== null) {
            image.src = picture.url;
            image.hidden = false;
          }
        });
      }
      card.append(thumbs);
    }
  }
  return card;
}
