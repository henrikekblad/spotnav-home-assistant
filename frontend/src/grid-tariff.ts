// The electricity price's grid tariff in the price section: the grid fee as a percentage of spot, the grid fee in
// high-load hours and those hours (the dashboard's `grid_tariff` block). The rows as the section shows them, the
// write's body built over the block as it is stored, and the editor for the high-load hours.
//
// A write replaces the three parts in full (`spotnav/update_grid_tariff`), so every change is made on the block
// the card holds when it is sent: a change to one part keeps the other two as stored. The high-load fee needs its
// window, and no window is ever made up: turning the fee on with none stored asks for the hours first, prefilled
// with a common window the person confirms (`SUGGESTED_WINDOW`).

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

/**
 * The high-load fee set (`null` is off), with `window` when the person has just confirmed one, else the stored
 * window. The backend refuses a fee that is on without a window; this never supplies one.
 */
export function withHigh(block: GridTariffBlock, value: number | null, window: HighLoadWindow | null = null): GridTariffBody {
  const body = { ...storedBody(block), transfer_high: feeChange(block.transfer_high, value) };
  return window === null ? body : { ...body, high_load_window: { ...window } };
}

/** The high-load hours set, the fees as stored. */
export function withWindow(block: GridTariffBlock, window: HighLoadWindow): GridTariffBody {
  return { ...storedBody(block), high_load_window: { ...window } };
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

const DAY_WORDS = {
  all: "gridFee.window.everyDay",
  weekdays: "gridFee.window.weekdays",
  mon_sat: "gridFee.window.monSat",
} as const satisfies Record<HighLoadDays, TranslationKey>;

function monthWord(language: Language, month: number): string {
  return translate(language, `gridFee.window.month.${month}` as TranslationKey);
}

/** The months: "nov–mar", one month on its own, or "hela året" when they take in all twelve. */
function monthsText(language: Language, window: HighLoadWindow): string {
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

export interface WindowEditorInput {
  /** What is stored, or `null`: the editor then offers `SUGGESTED_WINDOW` and says it is a suggestion. */
  current: HighLoadWindow | null;
  idPrefix: string;
  /** The line above the fields; the turn-on flow says why the hours come first. */
  intro?: string;
  /** The Save button's word ("Next" when the fee's figure follows). */
  positive?: string;
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

/**
 * The high-load hours (`windowFields`). With none stored it is prefilled with `SUGGESTED_WINDOW` and says so;
 * nothing is written until Save. Saving the stored hours unchanged writes nothing.
 */
export function windowEditor(
  doc: Document,
  language: Language,
  input: WindowEditorInput,
  save: SaveValue<HighLoadWindow>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "window", handlers, input.positive);
  const body = editor.body;
  const help = (text: string, key: string): void => {
    const line = doc.createElement("p");
    line.className = C.entityHelp;
    line.textContent = text;
    line.dataset["windowHelp"] = key;
    body.append(line);
  };
  if (input.intro !== undefined) {
    help(input.intro, "intro");
  }
  help(translate(language, "gridFee.window.help"), "what");
  const shown = input.current ?? SUGGESTED_WINDOW;
  if (input.current === null) {
    help(translate(language, "gridFee.window.suggestion"), "suggestion");
  }
  const fields = windowFields(doc, language, input.idPrefix, shown);
  body.append(...fields.nodes);
  editor.submit(async () => {
    const chosen = fields.read();
    if (chosen.to_hour === chosen.from_hour) {
      return translate(language, "gridFee.window.sameHours");
    }
    if (input.current !== null && sameWindow(input.current, chosen)) {
      return null;
    }
    return await save(chosen);
  });
  return editor.form;
}
