// The electricity price's grid tariff in the price section: the grid fee as a percentage of spot, the grid fee in
// high-load hours and those hours (the dashboard's `grid_tariff` block). The rows as the section shows them, the
// write's body built over the block as it is stored, and the editor for the high-load hours.
//
// A write replaces the three parts in full (`spotnav/update_grid_tariff`), so every change is made on the block
// the card holds when it is sent: a change to one part keeps the other two as stored. The high-load fee needs its
// window, and no window is ever made up: turning the fee on with none stored asks for the hours first, prefilled
// with a common window the person confirms (`SUGGESTED_WINDOW`).

import { formatNumber } from "./format";
import { translate, type Language } from "./i18n";
import type { GridTariffBlock, GridTariffFee, HighLoadWindow } from "./validate";
import { shell, type EditorHandlers, type SaveValue } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** One fee as a write states it. */
export interface GridTariffFeeBody {
  enabled: boolean;
  value: number | null;
}

/** The body of `spotnav/update_grid_tariff`: all three parts, every key present. */
export interface GridTariffBody {
  transfer_percent: GridTariffFeeBody;
  transfer_high: GridTariffFeeBody;
  high_load_window: HighLoadWindow | null;
}

/** The write's refusal when the electricity price was written since the block was read. */
export const GRID_TARIFF_CONFLICT = "revision_conflict";

/** The window the editor offers when none is stored: weekdays 07-19, November to March. Never written unconfirmed. */
export const SUGGESTED_WINDOW: HighLoadWindow = { weekdays_only: true, from_hour: 7, to_hour: 19, winter_only: true };

/** Whether the area's published price already holds the grid fee: the tariff's rows are then locked. */
export function gridTariffIncluded(block: GridTariffBlock): boolean {
  return block.transfer_percent.policy === "included" || block.transfer_high.policy === "included";
}

function feeBody(fee: GridTariffFee): GridTariffFeeBody {
  return { enabled: fee.policy === "manual", value: fee.explicit_value };
}

/** The tariff as stored, as a write would state it. */
export function storedBody(block: GridTariffBlock): GridTariffBody {
  return {
    transfer_percent: feeBody(block.transfer_percent),
    transfer_high: feeBody(block.transfer_high),
    high_load_window: block.high_load_window === null ? null : { ...block.high_load_window },
  };
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
    a === null || b === null
      ? a === b
      : a.weekdays_only === b.weekdays_only &&
        a.from_hour === b.from_hour &&
        a.to_hour === b.to_hour &&
        a.winter_only === b.winter_only;
  return (
    fee(left.transfer_percent, right.transfer_percent) &&
    fee(left.transfer_high, right.transfer_high) &&
    window(left.high_load_window, right.high_load_window)
  );
}

function hour(value: number): string {
  return String(value).padStart(2, "0");
}

/** The hours on one line: "Vardagar 07–19, nov–mar", "Alla dagar 06–22, hela året". */
export function windowText(language: Language, window: HighLoadWindow): string {
  return translate(language, "gridFee.window.line", {
    days: translate(language, window.weekdays_only ? "gridFee.window.weekdays" : "gridFee.window.everyDay"),
    hours: `${hour(window.from_hour)}–${hour(window.to_hour)}`,
    season: translate(language, window.winter_only ? "gridFee.window.winter" : "gridFee.window.allYear"),
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

function labelled(doc: Document, text: string, control: HTMLElement): HTMLElement {
  const wrapper = doc.createElement("div");
  wrapper.className = C.settingsField;
  const label = doc.createElement("label");
  label.className = C.settingsLabel;
  label.textContent = text;
  label.setAttribute("for", control.id);
  wrapper.append(label, control);
  return wrapper;
}

function checkbox(doc: Document, text: string, checked: boolean, key: string): { label: HTMLElement; box: HTMLInputElement } {
  const label = doc.createElement("label");
  label.className = C.siteChoice;
  const box = doc.createElement("input");
  box.type = "checkbox";
  box.checked = checked;
  box.dataset["window"] = key;
  label.append(box, doc.createTextNode(text));
  return { label, box };
}

/**
 * The high-load hours: from hour, to hour, weekdays only and November–March only. With none stored it is
 * prefilled with `SUGGESTED_WINDOW` and says so; nothing is written until Save. Saving the stored hours unchanged
 * writes nothing.
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
  const from = hourSelect(doc, `${input.idPrefix}-window-from`, 0, 23, shown.from_hour);
  from.dataset["window"] = "from";
  const to = hourSelect(doc, `${input.idPrefix}-window-to`, 1, 24, shown.to_hour);
  to.dataset["window"] = "to";
  const hours = doc.createElement("div");
  hours.className = C.settingsRow;
  hours.append(
    labelled(doc, translate(language, "gridFee.window.from"), from),
    labelled(doc, translate(language, "gridFee.window.to"), to),
  );
  body.append(hours);
  const weekdays = checkbox(doc, translate(language, "gridFee.window.weekdaysOnly"), shown.weekdays_only, "weekdays");
  const winter = checkbox(doc, translate(language, "gridFee.window.winterOnly"), shown.winter_only, "winter");
  body.append(weekdays.label, winter.label);
  editor.submit(async () => {
    const chosen: HighLoadWindow = {
      weekdays_only: weekdays.box.checked,
      from_hour: Number(from.value),
      to_hour: Number(to.value),
      winter_only: winter.box.checked,
    };
    if (chosen.to_hour <= chosen.from_hour) {
      return translate(language, "gridFee.window.order");
    }
    const current = input.current;
    if (
      current !== null &&
      current.weekdays_only === chosen.weekdays_only &&
      current.from_hour === chosen.from_hour &&
      current.to_hour === chosen.to_hour &&
      current.winter_only === chosen.winter_only
    ) {
      return null;
    }
    return await save(chosen);
  });
  return editor.form;
}
