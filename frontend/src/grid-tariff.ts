// The electricity price's grid tariff in the price section: the grid fee as a percentage of spot, the grid fee in
// high-load hours and those hours (the dashboard's `grid_tariff` block). The rows as the section shows them, the
// write's body built over the block as it is stored, and the editor for the high-load hours.
//
// A write replaces the three parts in full (`spotnav/update_grid_tariff`), so every change is made on the block
// the card holds when it is sent: a change to one part keeps the other two as stored. The high-load fee and its
// hours are one dialog (`highLoadEditor`) and one write: the fee needs its window, and no window is ever made up
// (with none stored the hours are prefilled with a common window, `SUGGESTED_WINDOW`, said to be a suggestion, and
// nothing is written until Save). "Remove the high-load fee" turns the fee off and clears the hours in one write.

import { formatNumber } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  HIGH_LOAD_DAYS,
  type DemandComponent,
  type GridTariffBlock,
  type GridTariffFee,
  type HighLoadDays,
  type HighLoadWindow,
} from "./validate";
import { shell, type EditorHandlers, type SaveValue } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** One fee as a write states it. */
export interface GridTariffFeeBody {
  enabled: boolean;
  value: number | null;
}

/**
 * The body of `spotnav/update_grid_tariff`: all three parts, every key present, and the demand-charge components
 * whenever the backend states them (an older one refuses the key, and keeps its components without it).
 */
export interface GridTariffBody {
  transfer_percent: GridTariffFeeBody;
  transfer_high: GridTariffFeeBody;
  high_load_window: HighLoadWindow | null;
  demand_components?: DemandComponent[];
}

/** The write's refusal when the electricity price was written since the block was read. */
export const GRID_TARIFF_CONFLICT = "revision_conflict";

/** The window the editor offers when none is stored: weekdays 07-19, November to March. Never written unconfirmed. */
export const SUGGESTED_WINDOW: HighLoadWindow = { days: "weekdays", from_month: 11, to_month: 3, from_hour: 7, to_hour: 19 };

/** Whether two windows are the same hours. */
export function sameWindow(a: HighLoadWindow, b: HighLoadWindow): boolean {
  return (
    a.days === b.days &&
    a.from_month === b.from_month &&
    a.to_month === b.to_month &&
    a.from_hour === b.from_hour &&
    a.to_hour === b.to_hour
  );
}

/** Whether the area's published price already holds the grid fee: the tariff's rows are then locked. */
export function gridTariffIncluded(block: GridTariffBlock): boolean {
  return block.transfer_percent.policy === "included" || block.transfer_high.policy === "included";
}

function feeBody(fee: GridTariffFee): GridTariffFeeBody {
  return { enabled: fee.policy === "manual", value: fee.explicit_value };
}

function copyComponent(component: DemandComponent): DemandComponent {
  return {
    window: component.window === null ? null : { ...component.window },
    top_n: component.top_n,
    price_per_kw: { ...component.price_per_kw },
  };
}

/** The tariff as stored, as a write would state it. */
export function storedBody(block: GridTariffBlock): GridTariffBody {
  const body: GridTariffBody = {
    transfer_percent: feeBody(block.transfer_percent),
    transfer_high: feeBody(block.transfer_high),
    high_load_window: block.high_load_window === null ? null : { ...block.high_load_window },
  };
  return block.demand === null ? body : { ...body, demand_components: block.demand.components.map(copyComponent) };
}

/** The demand-charge components set, the rest as stored. */
export function withComponents(block: GridTariffBlock, components: readonly DemandComponent[]): GridTariffBody {
  return { ...storedBody(block), demand_components: components.map(copyComponent) };
}

/** The component at `index` replaced (`null` removes it; an index past the end adds one), the rest as stored. */
export function withComponent(block: GridTariffBlock, index: number, component: DemandComponent | null): GridTariffBody {
  const components = [...(block.demand?.components ?? [])];
  if (component === null) {
    components.splice(index, 1);
  } else if (index >= components.length) {
    components.push(component);
  } else {
    components[index] = component;
  }
  return withComponents(block, components);
}

/** Whether two components are the same part of the charge. */
export function sameComponent(a: DemandComponent, b: DemandComponent): boolean {
  const window = a.window === null || b.window === null ? a.window === b.window : sameWindow(a.window, b.window);
  return (
    window &&
    a.top_n === b.top_n &&
    a.price_per_kw.winter === b.price_per_kw.winter &&
    a.price_per_kw.summer === b.price_per_kw.summer
  );
}

/** A fee turned on with `value`, or off (`null`) keeping its stored figure, so turning it on again is one step. */
function feeChange(fee: GridTariffFee, value: number | null): GridTariffFeeBody {
  return value === null ? { enabled: false, value: fee.explicit_value } : { enabled: true, value };
}

/** The percentage of spot set (`null` is off), the rest as stored. */
export function withPercent(block: GridTariffBlock, value: number | null): GridTariffBody {
  return { ...storedBody(block), transfer_percent: feeChange(block.transfer_percent, value) };
}

/** The high-load hours set, the fees as stored. */
export function withWindow(block: GridTariffBlock, window: HighLoadWindow): GridTariffBody {
  return { ...storedBody(block), high_load_window: { ...window } };
}

/** The high-load fee on with `value` over `window`, the rest as stored: the one dialog's Save. */
export function withHighLoad(block: GridTariffBlock, value: number, window: HighLoadWindow): GridTariffBody {
  return { ...storedBody(block), transfer_high: { enabled: true, value }, high_load_window: { ...window } };
}

/** The high-load fee off (its figure kept, as any fee turned off) and the hours cleared: the dialog's Remove. */
export function withoutHighLoad(block: GridTariffBlock): GridTariffBody {
  return { ...storedBody(block), transfer_high: feeChange(block.transfer_high, null), high_load_window: null };
}

export function sameBody(left: GridTariffBody, right: GridTariffBody): boolean {
  const fee = (a: GridTariffFeeBody, b: GridTariffFeeBody): boolean => a.enabled === b.enabled && a.value === b.value;
  const window = (a: HighLoadWindow | null, b: HighLoadWindow | null): boolean =>
    a === null || b === null ? a === b : sameWindow(a, b);
  const components = (a: DemandComponent[] | undefined, b: DemandComponent[] | undefined): boolean =>
    a === undefined || b === undefined
      ? a === b
      : a.length === b.length && a.every((component, index) => sameComponent(component, b[index]!));
  return (
    fee(left.transfer_percent, right.transfer_percent) &&
    fee(left.transfer_high, right.transfer_high) &&
    window(left.high_load_window, right.high_load_window) &&
    components(left.demand_components, right.demand_components)
  );
}

function hour(value: number): string {
  return String(value).padStart(2, "0");
}

export const DAY_WORDS = {
  all: "gridFee.window.everyDay",
  weekdays: "gridFee.window.weekdays",
  mon_sat: "gridFee.window.monSat",
} as const satisfies Record<HighLoadDays, TranslationKey>;

function monthWord(language: Language, month: number): string {
  return translate(language, `gridFee.window.month.${month}` as TranslationKey);
}

/** The months: "nov–mar", one month on its own, or "hela året" when they take in all twelve. */
export function monthsText(language: Language, window: HighLoadWindow): string {
  if ((window.to_month % 12) + 1 === window.from_month) {
    return translate(language, "gridFee.window.allYear");
  }
  if (window.from_month === window.to_month) {
    return monthWord(language, window.from_month);
  }
  return `${monthWord(language, window.from_month)}–${monthWord(language, window.to_month)}`;
}

/** The hours on one line: "Vardagar 07–19, nov–mar", "Mån–lör 07–22, nov–mar", "Vardagar 22–06, hela året". */
export function windowText(language: Language, window: HighLoadWindow): string {
  return translate(language, "gridFee.window.line", {
    days: translate(language, DAY_WORDS[window.days]),
    hours: `${hour(window.from_hour)}–${hour(window.to_hour)}`,
    months: monthsText(language, window),
  });
}

function feeText(language: Language, fee: GridTariffFee): string {
  if (fee.policy === "included") {
    return translate(language, "market.included");
  }
  if (fee.policy === "off" || fee.effective_value === null) {
    return translate(language, "settings.value.off");
  }
  const figure = formatNumber(language, fee.effective_value, 2);
  return fee.unit === null ? figure : `${figure} ${fee.unit}`;
}

export type GridTariffRowKey = "transfer_percent" | "transfer_high" | "high_load_window";

export interface GridTariffRow {
  key: GridTariffRowKey;
  label: string;
  value: string;
  /** Locked: the area's price already includes the grid fee. */
  included: boolean;
}

/**
 * The tariff's rows beside the grid fee: the percentage of spot and the high-load fee always, the high-load hours
 * only while that fee is on or hours are stored (and never while the price already includes the grid fee).
 */
export function gridTariffRows(language: Language, block: GridTariffBlock | null): GridTariffRow[] {
  if (block === null) {
    return [];
  }
  const included = gridTariffIncluded(block);
  const rows: GridTariffRow[] = [
    {
      key: "transfer_percent",
      label: translate(language, "gridFee.percent"),
      value: feeText(language, block.transfer_percent),
      included,
    },
    {
      key: "transfer_high",
      label: translate(language, "gridFee.high"),
      value: feeText(language, block.transfer_high),
      included,
    },
  ];
  const window = block.high_load_window;
  if (!included && (block.transfer_high.policy === "manual" || window !== null)) {
    rows.push({
      key: "high_load_window",
      label: translate(language, "gridFee.window"),
      value: window === null ? translate(language, "settings.value.unset") : windowText(language, window),
      included: false,
    });
  }
  return rows;
}

function hourSelect(doc: Document, id: string, from: number, to: number, selected: number): HTMLSelectElement {
  const select = doc.createElement("select");
  select.className = C.settingsInput;
  select.id = id;
  for (let value = from; value <= to; value += 1) {
    const option = doc.createElement("option");
    option.value = String(value);
    option.textContent = `${hour(value)}:00`;
    select.append(option);
  }
  select.value = String(selected);
  return select;
}

export function labelled(doc: Document, text: string, control: HTMLElement): HTMLElement {
  const wrapper = doc.createElement("div");
  wrapper.className = C.settingsField;
  const label = doc.createElement("label");
  label.className = C.settingsLabel;
  label.textContent = text;
  label.setAttribute("for", control.id);
  wrapper.append(label, control);
  return wrapper;
}

export function choiceSelect(
  doc: Document,
  id: string,
  choices: ReadonlyArray<{ value: string; text: string }>,
  selected: string,
): HTMLSelectElement {
  const select = doc.createElement("select");
  select.className = C.settingsInput;
  select.id = id;
  for (const choice of choices) {
    const option = doc.createElement("option");
    option.value = choice.value;
    option.textContent = choice.text;
    select.append(option);
  }
  select.value = selected;
  return select;
}

/** The months by name in the reader's language, for the month selects. */
function monthChoices(language: Language): Array<{ value: string; text: string }> {
  const format = new Intl.DateTimeFormat(language, { month: "long", timeZone: "UTC" });
  return Array.from({ length: 12 }, (_, index) => {
    const name = format.format(new Date(Date.UTC(2026, index, 15)));
    return { value: String(index + 1), text: name.charAt(0).toLocaleUpperCase(language) + name.slice(1) };
  });
}

function row(doc: Document, ...fields: HTMLElement[]): HTMLElement {
  const line = doc.createElement("div");
  line.className = C.settingsRow;
  line.append(...fields);
  return line;
}

/** The window's fields, ready to place in a form, and how to read what they hold. */
export interface WindowFields {
  nodes: HTMLElement[];
  read: () => HighLoadWindow;
}

/** The days, the first and last month, and from and to hour (an end before the start runs over midnight). */
export function windowFields(doc: Document, language: Language, idPrefix: string, shown: HighLoadWindow): WindowFields {
  const days = choiceSelect(
    doc,
    `${idPrefix}-window-days`,
    HIGH_LOAD_DAYS.map((value) => ({ value, text: translate(language, DAY_WORDS[value]) })),
    shown.days,
  );
  days.dataset["window"] = "days";
  const months = monthChoices(language);
  const fromMonth = choiceSelect(doc, `${idPrefix}-window-from-month`, months, String(shown.from_month));
  fromMonth.dataset["window"] = "fromMonth";
  const toMonth = choiceSelect(doc, `${idPrefix}-window-to-month`, months, String(shown.to_month));
  toMonth.dataset["window"] = "toMonth";
  const from = hourSelect(doc, `${idPrefix}-window-from`, 0, 23, shown.from_hour);
  from.dataset["window"] = "from";
  const to = hourSelect(doc, `${idPrefix}-window-to`, 1, 24, shown.to_hour);
  to.dataset["window"] = "to";
  return {
    nodes: [
      labelled(doc, translate(language, "gridFee.window.days"), days),
      row(
        doc,
        labelled(doc, translate(language, "gridFee.window.fromMonth"), fromMonth),
        labelled(doc, translate(language, "gridFee.window.toMonth"), toMonth),
      ),
      row(
        doc,
        labelled(doc, translate(language, "gridFee.window.from"), from),
        labelled(doc, translate(language, "gridFee.window.to"), to),
      ),
    ],
    read: () => ({
      days: HIGH_LOAD_DAYS.find((value) => value === days.value) ?? shown.days,
      from_month: Number(fromMonth.value),
      to_month: Number(toMonth.value),
      from_hour: Number(from.value),
      to_hour: Number(to.value),
    }),
  };
}

export interface HighLoadEditorInput {
  /** The fee as stored: its figure fills the field (also one kept while the fee is off). */
  fee: GridTariffFee;
  /** The hours as stored, or `null`: the fields then hold `SUGGESTED_WINDOW` and say it is a suggestion. */
  window: HighLoadWindow | null;
  idPrefix: string;
}

/** What the dialog's Save hands on: the fee's figure and its hours; `null` is Remove. */
export interface HighLoadChoice {
  value: number;
  window: HighLoadWindow;
}

/** The highest figure the fee takes (the area's minor unit per kWh). */
export const HIGH_LOAD_MAX = 100000;

/**
 * The grid fee in high-load hours, in one dialog: the fee's figure and its hours (`windowFields`), one Save that
 * hands both on, and, while the fee is on or hours are stored, "Remove the high-load fee", which hands on `null`.
 * Saving what is stored unchanged writes nothing.
 */
export function highLoadEditor(
  doc: Document,
  language: Language,
  input: HighLoadEditorInput,
  save: SaveValue<HighLoadChoice | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "highLoad", handlers);
  const body = editor.body;
  const help = (text: string, key: string): void => {
    const line = doc.createElement("p");
    line.className = C.entityHelp;
    line.textContent = text;
    line.dataset["windowHelp"] = key;
    body.append(line);
  };
  help(translate(language, "gridFee.high.help"), "fee");
  const field = doc.createElement("input");
  field.type = "text";
  field.inputMode = "decimal";
  field.className = C.settingsInput;
  field.id = `${input.idPrefix}-high-load-fee`;
  field.dataset["highLoad"] = "fee";
  const figure = input.fee.effective_value ?? input.fee.explicit_value;
  field.value = figure === null ? "" : formatNumber(language, figure, 2).replace(/\s/g, "");
  const unit = input.fee.unit === null ? "" : ` (${input.fee.unit})`;
  body.append(labelled(doc, `${translate(language, "gridFee.high.fee")}${unit}`, field));
  help(translate(language, "gridFee.window.help"), "what");
  if (input.window === null) {
    help(translate(language, "gridFee.window.suggestion"), "suggestion");
  }
  const shown = input.window ?? SUGGESTED_WINDOW;
  const fields = windowFields(doc, language, input.idPrefix, shown);
  body.append(...fields.nodes);
  const on = input.fee.policy === "manual";
  if (on || input.window !== null) {
    const remove = doc.createElement("button");
    remove.type = "button";
    remove.className = C.button;
    remove.dataset["highLoad"] = "remove";
    remove.textContent = translate(language, "gridFee.high.remove");
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
    const text = field.value.trim().replace(/\s/g, "").replace(",", ".");
    const value = text === "" ? Number.NaN : Number(text);
    if (!Number.isFinite(value)) {
      return translate(language, "gridFee.high.needsFee");
    }
    if (value < 0 || value > HIGH_LOAD_MAX) {
      return translate(language, "settings.error.range", {
        min: formatNumber(language, 0, 2),
        max: formatNumber(language, HIGH_LOAD_MAX, 2),
      });
    }
    const rounded = Math.round(value * 100) / 100;
    const window = fields.read();
    if (window.to_hour === window.from_hour) {
      return translate(language, "gridFee.window.sameHours");
    }
    if (on && input.fee.explicit_value === rounded && input.window !== null && sameWindow(input.window, window)) {
      return null;
    }
    return await save({ value: rounded, window });
  });
  return editor.form;
}

