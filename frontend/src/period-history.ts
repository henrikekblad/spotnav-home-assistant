// The parts the house and heat cards' history dialogs share (SpotNav 3.0): the clock icon in a card's header (the
// charger card's history icon), the Day / Month / Year tabs with ‹ › and a date picker, the "Detailed" switch
// remembered in this browser, and the bar chart in the charger history's look (`history.ts`). Built with
// `textContent` only; every figure is the backend's.

import { browserStore, type PreferenceStore } from "./chart-preference";
import { scrubBars } from "./chart-scrub";
import { element } from "./dom";
import { dateLabel, formatFixed } from "./format";
import { monthLabel } from "./history";
import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

export type HistoryPeriod = "day" | "month" | "year";
export const HISTORY_PERIODS: readonly HistoryPeriod[] = ["day", "month", "year"];
/** How far back the pickers offer (the backend answers ten years back). */
export const PICKER_MONTHS = 36;
export const PICKER_YEARS = 10;

export interface PeriodUi {
  period: HistoryPeriod;
  /** A day inside the period shown (`YYYY-MM-DD`), `null` for today's. */
  date: string | null;
  /** The Detailed switch. */
  detailed: boolean;
  /** A period is being read: the old figures stay, dimmed. */
  pending: boolean;
  /** The bar whose figures the readout shows, or `null`. */
  selected: string | null;
}

export interface PeriodHandlers {
  /** Another period kind or day was chosen: the card reads it. */
  onPeriod(period: HistoryPeriod, date: string | null): void;
  onDetailed(detailed: boolean): void;
}

// ------------------------------------------------------------------------------------------------ dates

const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

function parts(isoDate: string): [number, number, number] | null {
  const match = DATE.exec(isoDate);
  return match === null ? null : [Number(match[1]), Number(match[2]), Number(match[3])];
}

function iso(year: number, month: number, day: number): string {
  return new Date(Date.UTC(year, month - 1, day, 12)).toISOString().slice(0, 10);
}

/** The first day of the period that holds `isoDate`. */
export function periodStart(period: HistoryPeriod, isoDate: string): string {
  const p = parts(isoDate);
  if (p === null) {
    return isoDate;
  }
  return period === "day" ? isoDate : period === "month" ? iso(p[0], p[1], 1) : iso(p[0], 1, 1);
}

/** The first day of the period `steps` periods from the one holding `isoDate`. */
export function shiftPeriod(period: HistoryPeriod, isoDate: string, steps: number): string {
  const p = parts(isoDate);
  if (p === null) {
    return isoDate;
  }
  if (period === "day") {
    return iso(p[0], p[1], p[2] + steps);
  }
  if (period === "month") {
    return iso(p[0], p[1] + steps, 1);
  }
  return iso(p[0] + steps, 1, 1);
}

/** The period as the reader's language names it: a date, a month and year, a year. */
export function periodLabel(language: Language, period: HistoryPeriod, isoDate: string): string {
  if (period === "day") {
    return dateLabel(language, isoDate);
  }
  if (period === "month") {
    return monthLabel(language, isoDate.slice(0, 7));
  }
  return isoDate.slice(0, 4);
}

// ------------------------------------------------------------------------------------------------ storage

export function detailedKey(card: "house" | "heat"): string {
  return `spotnav-card.${card}.history.detailed`;
}

/** The Detailed switch as this browser last left it (off when nothing usable is stored). */
export function readDetailed(card: "house" | "heat", store: () => PreferenceStore | null = browserStore): boolean {
  try {
    return store()?.getItem(detailedKey(card)) === "1";
  } catch {
    return false;
  }
}

export function writeDetailed(card: "house" | "heat", detailed: boolean, store: () => PreferenceStore | null = browserStore): void {
  try {
    store()?.setItem(detailedKey(card), detailed ? "1" : "0");
  } catch {
    // Nothing is remembered; the choice holds until the card reloads.
  }
}

// ------------------------------------------------------------------------------------------------ parts

const SVG_NS = "http://www.w3.org/2000/svg";

/** The history clock (a clock with an arrow running back), the charger card's history icon, `size` px. */
export function historyIcon(doc: Document, size = 16, className?: string): SVGElement {
  const svg = doc.createElementNS(SVG_NS, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  if (className !== undefined) {
    svg.classList.add(className);
  }
  for (const d of ["M3.5 12a8.5 8.5 0 1 0 2.6-6.1", "M3.5 4.5v4.5H8", "M12 7.5V12l3 2"]) {
    const path = doc.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
  }
  return svg;
}

/** The months the month picker offers, newest first: back `PICKER_MONTHS` from today's, and the one shown. */
export function pickerMonths(today: string, shown: string): string[] {
  const months = new Set<string>([periodStart("month", shown)]);
  for (let back = 0; back < PICKER_MONTHS; back += 1) {
    months.add(shiftPeriod("month", today, -back));
  }
  return [...months].filter((month) => month <= today).sort().reverse();
}

export function pickerYears(today: string, shown: string): string[] {
  const years = new Set<string>([periodStart("year", shown)]);
  for (let back = 0; back <= PICKER_YEARS; back += 1) {
    years.add(shiftPeriod("year", today, -back));
  }
  return [...years].sort().reverse();
}

/** One bar of the period's chart: its height and colour's position between cheap (0) and dear (1) or none, or, with
 * `money`, its cost and saving; and its figures in words. */
export interface PeriodBar {
  key: string;
  value: number | null;
  dear: number | null;
  words: string;
  axis: string;
  /** A money bar (house and heat history): the cost filled, `saving` hatched with it (`null`: no figure). With this
   * the bar's height is money and `value` and `dear` are not used. */
  money?: { cost: number | null; saving: number | null };
}

/** A money bar's parts, in the same unit as `cost` and `saving`:
 * - `solid`: the filled part, what it cost;
 * - `hatch`: the hatched part on top, what the saving adds (the cost without the saving is the whole bar);
 * - `warn`: a saving below zero (it made it dearer): the top of the cost itself, hatched in the warning colour, so the
 *   bar is never taller than what was paid and the extra is plain to see;
 * - `whole`: their sum, the bar's height. */
export interface MoneyParts {
  solid: number;
  hatch: number;
  warn: number;
  whole: number;
}

export function moneyParts(cost: number | null, saving: number | null): MoneyParts {
  const paid = Math.max(0, cost ?? 0);
  if (cost === null || saving === null) {
    return { solid: paid, hatch: 0, warn: 0, whole: paid };
  }
  const without = cost + saving;
  if (without >= paid) {
    return { solid: paid, hatch: without - paid, warn: 0, whole: without };
  }
  const solid = Math.max(0, without);
  return { solid, hatch: 0, warn: paid - solid, whole: paid };
}

/** The bars in the charger history's look: hover, focus or a tap shows a bar's figures under the chart. */
export function periodBars(
  doc: Document,
  language: Language,
  label: string,
  unit: string,
  bars: PeriodBar[],
  ui: PeriodUi,
  hint: string,
  /** Money bars: the scale's text for the tallest bar (in the bars' own unit) and the legend's words for each part. */
  moneyKey?: { scaleText: (top: number) => string; cost: string; saving: string; extra: string },
): HTMLElement {
  const wrap = element(doc, "div", C.historyChart);
  const top = Math.max(0, ...bars.map((bar) => (bar.money === undefined ? bar.value ?? 0 : moneyParts(bar.money.cost, bar.money.saving).whole)));
  const readout = element(doc, "p", `${C.muted} ${C.historyReadout}`);
  readout.setAttribute("aria-live", "polite");
  const buttons: HTMLButtonElement[] = [];
  const show = (bar: PeriodBar | null): void => {
    readout.textContent = bar === null ? hint : bar.words;
    buttons.forEach((button) => button.setAttribute("aria-pressed", String(bar !== null && button.dataset["bar"] === bar.key)));
  };
  const plot = element(doc, "div", C.historyBars);
  plot.setAttribute("role", "group");
  plot.setAttribute("aria-label", label);
  for (const bar of bars) {
    const button = element(doc, "button", C.historyBar);
    button.type = "button";
    button.dataset["bar"] = bar.key;
    button.setAttribute("aria-label", bar.words);
    const fill = element(doc, "span", C.historyBarFill);
    if (bar.money !== undefined) {
      const parts = moneyParts(bar.money.cost, bar.money.saving);
      if (parts.whole > 0 && top > 0) {
        fill.dataset["money"] = "true";
        fill.style.height = `${Math.max(4, (parts.whole / top) * 100)}%`;
        // From the top: the saving (hatched), the extra cost (hatched, warning), the filled cost.
        for (const [kind, size] of [["saving", parts.hatch], ["extra", parts.warn], ["cost", parts.solid]] as const) {
          if (size > 0) {
            const piece = element(doc, "span", C.historyBarPart);
            piece.dataset["part"] = kind;
            piece.style.flexGrow = String(size);
            fill.append(piece);
          }
        }
      } else {
        fill.dataset["empty"] = "true";
      }
    } else if (bar.value !== null && bar.value > 0 && top > 0) {
      fill.style.height = `${Math.max(4, (bar.value / top) * 100)}%`;
      if (bar.dear === null) {
        fill.dataset["price"] = "none";
      } else {
        fill.style.setProperty("--spotnav-day-dear", `${Math.round(bar.dear * 100)}%`);
      }
    } else {
      fill.dataset["empty"] = "true";
    }
    button.append(fill);
    button.addEventListener("mouseenter", () => show(bar));
    button.addEventListener("focus", () => show(bar));
    button.addEventListener("click", () => {
      ui.selected = bar.key;
      show(bar);
    });
    buttons.push(button);
    plot.append(button);
  }
  plot.addEventListener("mouseleave", () => show(bars.find((bar) => bar.key === ui.selected) ?? null));
  scrubBars(
    plot,
    buttons,
    (index) => show(bars[index] ?? null),
    (index) => {
      const bar = bars[index];
      if (bar !== undefined) {
        ui.selected = bar.key;
        show(bar);
      }
    },
  );
  const axis = element(doc, "div", C.historyAxis);
  axis.setAttribute("aria-hidden", "true");
  for (const bar of bars) {
    axis.append(element(doc, "span", undefined, bar.axis));
  }
  const scale = element(doc, "span", `${C.muted} ${C.historyScale}`, moneyKey !== undefined ? moneyKey.scaleText(top) : `${formatFixed(language, top, top >= 10 ? 0 : 1)} ${unit}`);
  scale.setAttribute("aria-hidden", "true");
  wrap.append(scale, plot, axis);
  if (moneyKey !== undefined) {
    // A swatch per part the bars hold: the hatched saving and the warning's extra only where a bar has one.
    const shown = bars.map((bar) => (bar.money === undefined ? null : moneyParts(bar.money.cost, bar.money.saving)));
    const legend = element(doc, "ul", `${C.muted} ${C.historyLegend}`);
    legend.dataset["part"] = "legend";
    for (const [part, words, present] of [
      ["cost", moneyKey.cost, shown.some((parts) => parts !== null && parts.solid > 0)],
      ["saving", moneyKey.saving, shown.some((parts) => parts !== null && parts.hatch > 0)],
      ["extra", moneyKey.extra, shown.some((parts) => parts !== null && parts.warn > 0)],
    ] as const) {
      if (!present || words === "") {
        continue;
      }
      const item = element(doc, "li");
      const swatch = element(doc, "span", C.historyBarPart);
      swatch.dataset["part"] = part;
      swatch.setAttribute("aria-hidden", "true");
      item.append(swatch, element(doc, "span", undefined, words));
      legend.append(item);
    }
    if (legend.childElementCount > 0) {
      wrap.append(legend);
    }
  }
  wrap.append(readout);
  show(bars.find((bar) => bar.key === ui.selected) ?? null);
  return wrap;
}

/** Which axis labels to write: the first, the last, and every `every`th not too near the last. */
export function axisShown(index: number, count: number, every: number): boolean {
  const number = index + 1;
  return number === 1 || number === count || (number % every === 0 && count - number >= Math.ceil(every / 2));
}

/** A section of the dialog with its heading. */
export function section(doc: Document, heading: string, part: string): HTMLElement {
  const block = element(doc, "section", C.historyTile);
  block.dataset["part"] = part;
  block.append(element(doc, "h4", C.historyTileHeading, heading));
  return block;
}

/** One `name … figure` row of a section. */
export function figureRow(doc: Document, name: string, figure: string, key: string, muted = false): HTMLElement {
  const row = element(doc, "div", "spotnav-period-row");
  row.dataset["row"] = key;
  row.append(element(doc, "span", undefined, name), element(doc, "span", muted ? `spotnav-period-figure ${C.muted}` : "spotnav-period-figure", figure));
  return row;
}

export function note(doc: Document, text: string, line?: string): HTMLElement {
  const paragraph = element(doc, "p", `${C.muted} ${C.historyFootnote}`, text);
  if (line !== undefined) {
    paragraph.dataset["line"] = line;
  }
  return paragraph;
}

/** The status line while a period is read or when it could not be. */
export function statusLine(doc: Document, language: Language, key: TranslationKey, code: string | null = null): HTMLElement {
  const line = element(doc, "p", key === "hist.loading" ? C.muted : C.settingsNotice, translate(language, key));
  line.setAttribute("role", "status");
  if (code !== null) {
    line.dataset["code"] = code;
  }
  return line;
}

export const PERIOD_STYLES = `
  .spotnav-period-nav {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .spotnav-period-top {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
  }
  .${C.historyMonthPicker} > input {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 40px;
    box-sizing: border-box;
    padding: 0 8px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    color-scheme: light dark;
  }
  .spotnav-period-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    font-size: 0.9rem;
  }
  .spotnav-period-row > span:first-child {
    flex: 1 1 8rem;
    min-width: 0;
  }
  .spotnav-period-figure {
    flex: 0 1 auto;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .spotnav-period-runs {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 0.9rem;
  }
  .spotnav-period-runs > li {
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }
`;
