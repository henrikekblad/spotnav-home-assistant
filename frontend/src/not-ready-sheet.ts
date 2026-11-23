// What will not be ready in time across the site (spec `plans/v3_pool_spa_spec.md` sections 8 and 10.4; the backend's
// `site/not_ready.py`, `get_plan`'s top-level `not_ready`): the cars' departures under the ceiling, the spa's baths and
// the sauna's sessions, with every choice ranked by how much it frees. The choice sheet shows them all; the phones'
// combined notice carries the first three and a tap on it opens this sheet (`#spotnav-not-ready`).
//
// A press is the same answer as from each load's own line: a car's goes to its charger (`spotnav/take_what_fits`,
// `spotnav/later_departure`, `spotnav/release_import_target`), a bath's or a session's to `spotnav/spa` or
// `spotnav/sauna`; "Allow the peak" sets the ceiling aside for every load under it, so it is asked again first.

import { exactKeys, isRecord, list, Malformed, numberOrNull, record, text, textOrNull } from "./decode-primitives";
import { choiceLabel, type DoesNotFitChoiceCode } from "./does-not-fit";
import { clock, formatFixed, kwhText, type FormatContext, type MoneyUnits } from "./format";
import { translate, type TranslationKey } from "./i18n";
import { element } from "./dom";

const BLOCK_KEYS = ["items", "ranked"] as const;
const ITEM_KEYS = ["load_id", "kind", "name", "deadline", "short_kwh", "short_c", "reach_c", "cause", "chosen", "choices"] as const;
const CHOICE_KEYS = ["code", "frees_kwh", "kwh", "reach_c", "later", "peak_kw", "cost"] as const;
const RANKED_KEYS = ["load_id", "code", "frees_kwh"] as const;
const KINDS = ["car", "spa", "sauna"] as const;
const CODES: readonly DoesNotFitChoiceCode[] = ["takes_what_fits", "later", "accept_peak"];
/** The fragment a tap on the phones' notice opens the card with. */
export const NOT_READY_HASH = "#spotnav-not-ready";

export type NotReadyKind = (typeof KINDS)[number];

export interface NotReadyChoice {
  code: DoesNotFitChoiceCode;
  frees_kwh: number;
  kwh: number | null;
  reach_c: number | null;
  /** "Later": the time it moves to (epoch ms). */
  laterMs: number | null;
  peak_kw: number | null;
  cost: number | null;
}

export interface NotReadyItem {
  load_id: string;
  kind: NotReadyKind;
  name: string | null;
  deadlineMs: number | null;
  short_kwh: number | null;
  short_c: number | null;
  reach_c: number | null;
  cause: string | null;
  chosen: "takes_what_fits" | "accept_peak" | null;
  choices: NotReadyChoice[];
}

export interface NotReadyRanked {
  /** The load the choice is for; `null` for "Allow the peak", which is for every load under the ceiling. */
  load_id: string | null;
  code: DoesNotFitChoiceCode;
  frees_kwh: number;
}

export interface NotReady {
  items: NotReadyItem[];
  ranked: NotReadyRanked[];
}

/** A press on the sheet: the choice and the loads it answers for (one, or every one under the ceiling). */
export interface NotReadyPress {
  code: DoesNotFitChoiceCode;
  loads: Array<{ load_id: string; kind: NotReadyKind }>;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T {
  const found = text(value);
  if (!(allowed as readonly string[]).includes(found)) {
    throw new Malformed();
  }
  return found as T;
}

function instantOrNull(value: unknown): number | null {
  const raw = textOrNull(value);
  if (raw === null) {
    return null;
  }
  const ms = Date.parse(raw);
  if (!Number.isFinite(ms)) {
    throw new Malformed();
  }
  return ms;
}

/** The block (`null` stays `null`), checked; throws `Malformed` on anything that does not fit. */
export function decodeNotReady(raw: unknown): NotReady | null {
  if (raw === null) {
    return null;
  }
  const source = record(raw);
  exactKeys(source, BLOCK_KEYS);
  return {
    items: list(source.items).map((entry) => {
      const item = record(entry);
      exactKeys(item, ITEM_KEYS);
      return {
        load_id: text(item.load_id),
        kind: oneOf(item.kind, KINDS),
        name: textOrNull(item.name),
        deadlineMs: instantOrNull(item.deadline),
        short_kwh: numberOrNull(item.short_kwh),
        short_c: numberOrNull(item.short_c),
        reach_c: numberOrNull(item.reach_c),
        cause: textOrNull(item.cause),
        chosen: item.chosen === null ? null : oneOf(item.chosen, ["takes_what_fits", "accept_peak"] as const),
        choices: list(item.choices).map((raw) => {
          const choice = record(raw);
          exactKeys(choice, CHOICE_KEYS);
          return {
            code: oneOf(choice.code, CODES),
            frees_kwh: numberOrNull(choice.frees_kwh) ?? 0,
            kwh: numberOrNull(choice.kwh),
            reach_c: numberOrNull(choice.reach_c),
            laterMs: instantOrNull(choice.later),
            peak_kw: numberOrNull(choice.peak_kw),
            cost: numberOrNull(choice.cost),
          };
        }),
      };
    }),
    ranked: list(source.ranked).map((entry) => {
      const ranked = record(entry);
      exactKeys(ranked, RANKED_KEYS);
      return {
        load_id: textOrNull(ranked.load_id),
        code: oneOf(ranked.code, CODES),
        frees_kwh: numberOrNull(ranked.frees_kwh) ?? 0,
      };
    }),
  };
}

/** The block, or `null` when it is missing or unreadable (the card then shows no sheet). */
export function notReadyOrNull(value: unknown): NotReady | null {
  if (!isRecord(value)) {
    return null;
  }
  try {
    return decodeNotReady(value);
  } catch {
    return null;
  }
}

/** Whether the page was opened by a tap on the phones' "not ready in time" notice (its url's fragment). */
export function openedForNotReady(): boolean {
  try {
    return globalThis.location?.hash === NOT_READY_HASH;
  } catch {
    return false;
  }
}

/** Whether a load in the block still asks (a choice to make). */
export function notReadyAsks(block: NotReady | null): boolean {
  return block !== null && block.ranked.length > 0;
}

function degrees(format: FormatContext, value: number | null): string {
  return `${formatFixed(format.language, value ?? 0, 0)} °C`;
}

/** A load's name in the sheet: the car's charger, "Spa", "Bastu". */
export function notReadyLoadName(format: FormatContext, item: NotReadyItem): string {
  if (item.kind === "car") {
    return item.name ?? translate(format.language, "notReady.car");
  }
  return translate(format.language, `heat.tab.${item.kind}` as TranslationKey);
}

/** One item's line: "Garage 07:30: 4 kWh saknas", "Spa 19:00: når 35 °C", or what was chosen. */
export function notReadyLine(format: FormatContext, item: NotReadyItem): string {
  const name = notReadyLoadName(format, item);
  const time = item.deadlineMs === null ? "" : clock(format, item.deadlineMs);
  if (item.chosen === "takes_what_fits") {
    return translate(format.language, "notReady.line.chosen", { name, time });
  }
  if (item.kind === "car") {
    return translate(format.language, "notReady.line.car", { name, time, kwh: kwhText(format.language, item.short_kwh ?? 0) });
  }
  return translate(format.language, "notReady.line.heat", { name, time, reach: degrees(format, item.reach_c) });
}

/** A choice's words for its load ("Senare avresa (08:00)", "Senare badtid (20:00)", "Ta det som ryms (35 °C)"). */
function choiceWords(format: FormatContext, item: NotReadyItem, choice: NotReadyChoice, money: MoneyUnits | null): string {
  if (item.kind === "car") {
    return choiceLabel(format, { code: choice.code, kwh: choice.kwh, peak_kw: choice.peak_kw, cost: choice.cost, departureMs: choice.laterMs }, money);
  }
  if (choice.code === "later") {
    const time = choice.laterMs === null ? "" : clock(format, choice.laterMs);
    return translate(format.language, `heat.${item.kind}.choice.later` as TranslationKey, { time });
  }
  if (choice.code === "takes_what_fits") {
    return translate(format.language, "heat.spa.choice.fits", { reach: degrees(format, item.reach_c ?? choice.reach_c) });
  }
  return translate(format.language, "heat.spa.choice.peak");
}

/** Every choice of the sheet in the backend's rank (the most freed first), with its words and what it answers. */
export function notReadyButtons(format: FormatContext, block: NotReady, money: MoneyUnits | null): Array<{ label: string; press: NotReadyPress }> {
  const asking = block.items.filter((item) => item.chosen === null);
  const several = asking.length > 1;
  const out: Array<{ label: string; press: NotReadyPress }> = [];
  for (const entry of block.ranked) {
    if (entry.load_id === null) {
      const under = asking.filter((item) => item.choices.some((choice) => choice.code === "accept_peak"));
      if (under.length === 0) {
        continue;
      }
      const label =
        under.length === 1
          ? choiceWords(format, under[0]!, under[0]!.choices.find((choice) => choice.code === "accept_peak")!, money)
          : translate(format.language, "notReady.peakAll");
      out.push({ label, press: { code: "accept_peak", loads: under.map((item) => ({ load_id: item.load_id, kind: item.kind })) } });
      continue;
    }
    const item = asking.find((candidate) => candidate.load_id === entry.load_id);
    const choice = item?.choices.find((candidate) => candidate.code === entry.code);
    if (item === undefined || choice === undefined || (choice.code === "later" && choice.laterMs === null)) {
      continue;
    }
    const words = choiceWords(format, item, choice, money);
    out.push({
      label: several ? translate(format.language, "notReady.button", { load: notReadyLoadName(format, item), choice: words }) : words,
      press: { code: choice.code, loads: [{ load_id: item.load_id, kind: item.kind }] },
    });
  }
  return out;
}

/** The sheet's classes. */
export const SHEET_CLASSES = {
  sheet: "spotnav-not-ready",
  title: "spotnav-not-ready-title",
  lede: "spotnav-not-ready-lede",
  items: "spotnav-not-ready-items",
  choices: "spotnav-not-ready-choices",
} as const;
const S = SHEET_CLASSES;

/** The sheet's styles, in the house card's and the heat card's style nodes. */
export const NOT_READY_STYLES = `
  .${S.sheet} { display: grid; gap: 6px; margin: 4px 0 8px; padding: 8px 10px; border: 1px solid var(--warning-color, #ffa600); border-radius: 8px; font-size: 0.88rem; }
  .${S.title} { margin: 0; font-size: 0.95rem; font-weight: 600; }
  .${S.items} { margin: 0; padding-left: 18px; display: grid; gap: 2px; }
  .${S.lede} { margin: 0; color: var(--secondary-text-color); font-size: 0.82rem; }
  .${S.choices} { display: flex; flex-wrap: wrap; gap: 6px; }
  .${S.choices} button { font: inherit; font-size: 0.82rem; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--divider-color, #e0e0e0); background: transparent; color: var(--primary-color); cursor: pointer; }
  .${S.choices} button:disabled { opacity: 0.5; cursor: default; }
`;

/**
 * The choice sheet: its heading, one line per load and every choice as a button, the most freed first ("Allow the
 * peak" asked again before it is sent). Without `onChoose` the buttons are left out (a past day, a read-only view).
 */
export function notReadySheet(
  doc: Document,
  format: FormatContext,
  block: NotReady,
  money: MoneyUnits | null,
  onChoose?: (press: NotReadyPress) => void,
): HTMLElement {
  const language = format.language;
  const box = element(doc, "div", SHEET_CLASSES.sheet);
  box.id = NOT_READY_HASH.slice(1);
  box.dataset["part"] = "not-ready";
  box.append(element(doc, "h4", SHEET_CLASSES.title, translate(language, "notReady.title")));
  const lines = element(doc, "ul", SHEET_CLASSES.items);
  for (const item of block.items) {
    const row = element(doc, "li", undefined, notReadyLine(format, item));
    row.dataset["load"] = item.kind;
    lines.append(row);
  }
  box.append(lines);
  if (onChoose === undefined) {
    return box;
  }
  const buttons = notReadyButtons(format, block, money);
  if (buttons.length === 0) {
    return box;
  }
  box.append(element(doc, "p", SHEET_CLASSES.lede, translate(language, "notReady.lede")));
  const row = element(doc, "div", SHEET_CLASSES.choices);
  for (const { label, press } of buttons) {
    const button = element(doc, "button", undefined, label) as HTMLButtonElement;
    button.type = "button";
    button.dataset["choice"] = press.code;
    button.dataset["loads"] = press.loads.map((load) => load.load_id).join(" ");
    let asked = press.code !== "accept_peak";
    button.addEventListener("click", () => {
      if (!asked) {
        asked = true;
        button.textContent = translate(language, "doesNotFit.choice.acceptPeakAgain");
        return;
      }
      button.disabled = true;
      onChoose(press);
    });
    row.append(button);
  }
  box.append(row);
  return box;
}
