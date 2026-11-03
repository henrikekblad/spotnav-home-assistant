// The demand charge in the price section and the import target in the site's rows (the dashboard's
// `grid_tariff.demand` block): the words for a component, what the site measured for it this month, the running
// hour against the import target, and the editor of one component.
//
// Nothing is shown until the person adds a component or a target: the price section then has one row, "Demand
// charge: None", and the site one, "Import target: Off". A component is written with the tariff
// (`grid-tariff.ts:withComponent`), the target with the site's settings.

import { clock, dateLabel, formatFixed, formatNumber, type FormatContext } from "./format";
import { choiceSelect, labelled, SUGGESTED_WINDOW, windowFields, windowText } from "./grid-tariff";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  DEMAND_TOP_N,
  MAX_DEMAND_COMPONENTS,
  type DemandBlock,
  type DemandComponent,
  type DemandMonth,
  type DemandTopN,
} from "./validate";
import { shell, type EditorHandlers, type SaveValue } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

const TOP_WORDS = {
  1: "demand.top.1",
  3: "demand.top.3",
  5: "demand.top.5",
} as const satisfies Record<DemandTopN, TranslationKey>;

/** The reasons the backend gives for not measuring, worded; another one reads as the generic line. */
export const DEMAND_REASON_KEYS: Record<string, TranslationKey> = {
  no_site: "demand.reason.no_site",
  needs_total_grid_power: "demand.reason.needs_total_grid_power",
};

/** A power in kW to one decimal ("7,0 kW"). */
export function kwText(language: Language, kw: number): string {
  return `${formatFixed(language, kw, 1)} kW`;
}

/** What the component counts, in one line: "Highest hour, Every day 06–23, all year" or "…, All hours". */
export function componentText(language: Language, component: DemandComponent): string {
  return translate(language, "demand.value", {
    shape: translate(language, TOP_WORDS[component.top_n]),
    hours: component.window === null ? translate(language, "demand.allHours") : windowText(language, component.window),
  });
}

/** The prices as "45 / 23 kr/kW" (winter / summer), or `null` when neither is given. */
export function priceText(language: Language, component: DemandComponent, unit: string | null): string | null {
  const { winter, summer } = component.price_per_kw;
  if (winter === null && summer === null) {
    return null;
  }
  const figure = (value: number | null): string => (value === null ? "–" : formatNumber(language, value, 2));
  return translate(language, "demand.prices", { winter: figure(winter), summer: figure(summer), unit: unit ?? "" }).trim();
}

/** One peak: "tis 22 sep 09:00 · 7,0 kW". */
function peakText(format: FormatContext, peak: { date: string; start: string; kw: number }): string {
  const at = clock(format, Date.parse(peak.start));
  const day = dateLabel(format.language, peak.date);
  return `${[day, at].filter((part) => part !== "").join(" ")} · ${kwText(format.language, peak.kw)}`;
}

/** What one component bills so far this month: the mean, then its peaks; "No peak yet this month" before one. */
export function monthText(format: FormatContext, month: DemandMonth): string {
  const language = format.language;
  if (month.bar_kw === null) {
    return translate(language, "demand.month.none");
  }
  return translate(language, "demand.month", {
    kw: kwText(language, month.bar_kw),
    peaks: month.peaks.map((peak) => peakText(format, peak)).join(", "),
  });
}

export interface DemandRow {
  /** The component's index, or `add` for the row that adds one. */
  key: number | "add";
  label: string;
  value: string;
  /** Lines under the row: the prices and what the month measured, or why nothing is measured. */
  lines: string[];
}

/**
 * The demand charge's rows: "Demand charge: None" while there is no component; else one row per component (with
 * its prices and this month's mean and peaks beneath) and, below three, a row that adds one. `null` block (an
 * older backend): no rows at all.
 */
export function demandRows(format: FormatContext, demand: DemandBlock | null): DemandRow[] {
  if (demand === null) {
    return [];
  }
  const language = format.language;
  const title = translate(language, "demand.title");
  if (demand.components.length === 0) {
    return [{ key: "add", label: title, value: translate(language, "settings.value.none"), lines: [] }];
  }
  const rows: DemandRow[] = demand.components.map((component, index) => {
    const lines: string[] = [];
    const prices = priceText(language, component, demand.price_unit);
    if (prices !== null) {
      lines.push(prices);
    }
    const month = demand.per_component[index];
    if (month !== undefined) {
      lines.push(monthText(format, month));
    }
    return {
      key: index,
      label: demand.components.length === 1 ? title : translate(language, "demand.part", { n: String(index + 1) }),
      value: componentText(language, component),
      lines,
    };
  });
  if (!demand.available) {
    rows[rows.length - 1]!.lines.push(reasonText(language, demand.reason));
  }
  if (demand.components.length < MAX_DEMAND_COMPONENTS) {
    rows.push({ key: "add", label: translate(language, "demand.addPart"), value: translate(language, "demand.add"), lines: [] });
  }
  return rows;
}

/** Why the site cannot measure, worded. */
export function reasonText(language: Language, reason: string | null): string {
  const key = reason === null ? undefined : DEMAND_REASON_KEYS[reason];
  return translate(language, key ?? "demand.reason.unknown");
}

/**
 * The running hour against the import target: "This hour: 4,2 of 6,0 kW"; where the month's peak lifts the target
 * this hour, against that level: "This hour: 2,8 of 6,6 kW – the month's peak (import target 6,0 kW)". `null`
 * without a target; without a reading yet, or where the site cannot measure, it says so.
 */
export function hourLine(language: Language, demand: DemandBlock | null): string | null {
  if (demand === null || demand.import_target_kw === null) {
    return null;
  }
  if (!demand.available) {
    return reasonText(language, demand.reason);
  }
  if (demand.hour === null) {
    return translate(language, "site.importTarget.noHour");
  }
  const effective = demand.effective_target_kw;
  if (effective !== null && effective > demand.import_target_kw) {
    return translate(language, "site.importTarget.hourPeak", {
      kw: formatFixed(language, demand.hour.kw_so_far, 1),
      target: kwText(language, effective),
      set: kwText(language, demand.import_target_kw),
    });
  }
  return translate(language, "site.importTarget.hour", {
    kw: formatFixed(language, demand.hour.kw_so_far, 1),
    target: kwText(language, demand.import_target_kw),
  });
}

export interface ComponentEditorInput {
  /** The component as stored, or `null` to add one. */
  current: DemandComponent | null;
  idPrefix: string;
  /** The area's major unit per kW ("kr/kW"). */
  unit: string | null;
}

function priceInput(doc: Document, id: string, value: number | null): HTMLInputElement {
  const field = doc.createElement("input");
  field.className = C.settingsInput;
  field.id = id;
  field.type = "text";
  field.inputMode = "decimal";
  field.value = value === null ? "" : String(value);
  return field;
}

/** A price as typed: empty is unknown (`null`), a figure not below zero, else `NaN`. */
function parsePrice(text: string): number | null {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") {
    return null;
  }
  const value = Number(trimmed);
  return Number.isFinite(value) && value >= 0 ? value : Number.NaN;
}

/**
 * One component: which hours count (all, or a window of the same shape as the high-load hours), how many peaks a
 * month on different days, and the price per kW in winter and summer (either may stay empty). Save hands
 * `save` the component; an existing one also has Remove, which hands it `null`.
 */
export function componentEditor(
  doc: Document,
  language: Language,
  input: ComponentEditorInput,
  save: SaveValue<DemandComponent | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "demand", handlers);
  const body = editor.body;
  const help = doc.createElement("p");
  help.className = C.entityHelp;
  help.textContent = translate(language, "demand.help");
  body.append(help);
  const current = input.current;
  const counted = choiceSelect(
    doc,
    `${input.idPrefix}-demand-counted`,
    [
      { value: "all", text: translate(language, "demand.counted.all") },
      { value: "window", text: translate(language, "demand.counted.window") },
    ],
    current?.window ? "window" : "all",
  );
  counted.dataset["demand"] = "counted";
  const window = windowFields(doc, language, `${input.idPrefix}-demand`, current?.window ?? SUGGESTED_WINDOW);
  const windowBox = doc.createElement("div");
  windowBox.dataset["demand"] = "window";
  windowBox.append(...window.nodes);
  windowBox.hidden = counted.value !== "window";
  counted.addEventListener("change", () => {
    windowBox.hidden = counted.value !== "window";
  });
  const topN = choiceSelect(
    doc,
    `${input.idPrefix}-demand-top`,
    DEMAND_TOP_N.map((value) => ({ value: String(value), text: translate(language, TOP_WORDS[value]) })),
    String(current?.top_n ?? 1),
  );
  topN.dataset["demand"] = "topN";
  const unit = input.unit === null ? "" : ` (${input.unit})`;
  const winter = priceInput(doc, `${input.idPrefix}-demand-winter`, current?.price_per_kw.winter ?? null);
  winter.dataset["demand"] = "winter";
  const summer = priceInput(doc, `${input.idPrefix}-demand-summer`, current?.price_per_kw.summer ?? null);
  summer.dataset["demand"] = "summer";
  body.append(
    labelled(doc, translate(language, "demand.counted"), counted),
    windowBox,
    labelled(doc, translate(language, "demand.topN"), topN),
    labelled(doc, `${translate(language, "demand.priceWinter")}${unit}`, winter),
    labelled(doc, `${translate(language, "demand.priceSummer")}${unit}`, summer),
  );
  if (current !== null) {
    const remove = doc.createElement("button");
    remove.type = "button";
    remove.className = C.button;
    remove.dataset["demand"] = "remove";
    remove.textContent = translate(language, "demand.remove");
    remove.addEventListener("click", () => {
      remove.disabled = true;
      void save(null)
        .then((message) => {
          if (message === null) {
            handlers.onDone();
          } else {
            editor.setError(message);
          }
        })
        .finally(() => {
          remove.disabled = false;
        });
    });
    // Beside Save and Cancel: the form's last part is its actions line.
    (editor.form.lastElementChild ?? body).append(remove);
  }
  editor.submit(async () => {
    const hours = counted.value === "window" ? window.read() : null;
    if (hours !== null && hours.from_hour === hours.to_hour) {
      return translate(language, "gridFee.window.sameHours");
    }
    const prices = { winter: parsePrice(winter.value), summer: parsePrice(summer.value) };
    if (Number.isNaN(prices.winter) || Number.isNaN(prices.summer)) {
      return translate(language, "demand.error.price");
    }
    const chosen: DemandComponent = {
      window: hours,
      top_n: DEMAND_TOP_N.find((value) => String(value) === topN.value) ?? 1,
      price_per_kw: prices,
    };
    return await save(chosen);
  });
  return editor.form;
}
