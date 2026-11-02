// The history dialog's Power view: the decoded `spotnav/get_power_month` answer and the body that shows it.
//
// One bar per day of the month is the day's highest hourly mean of import from the grid, split into the house's part
// and the car's (two shades of one hue); the days that hold a demand-charge part's counted peak are outlined and
// tagged with the part's number; the site's import target is a dashed line across; a day the target held or paused a
// charger has a small mark under its bar. A tap on a day shows its hours the same way (for a month whose hours are
// still kept; an older month keeps its days only). Hover, focus or a tap shows the figures in the readout under the
// chart, as the charge history's day chart does. Nothing is recomputed: every figure is the backend's.

import { dateLabel, formatFixed } from "./format";
import { windowText } from "./grid-tariff";
import { translate, type Language, type TranslationKey } from "./i18n";
import { decodeDemandComponent, type DemandComponent } from "./validate";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { POWER_MONTH_API_VERSION } from "./types";

export interface PowerDay {
  /** The local date, `YYYY-MM-DD`. */
  date: string;
  peak_kw: number;
  /** The peak hour's start, local time with its offset. */
  peak_start: string;
  house_kw: number | null;
  /** The parts (indexes into `components`) whose counted peaks fall on this day. */
  counted: number[];
  held: boolean;
  paused: boolean;
}

export interface PowerHour {
  start: string;
  kw: number;
  house_kw: number | null;
  incomplete: boolean;
  held: boolean;
  paused: boolean;
  counted: number[];
}

export interface PowerMonth {
  month: string;
  /** Newest first. */
  available_months: string[];
  import_target_kw: number | null;
  components: DemandComponent[];
  days: PowerDay[];
  /** `null` for a month that keeps its days only. */
  hours: PowerHour[] | null;
  bars: Array<number | null>;
}

export type PowerDecodeResult = { ok: true; value: PowerMonth } | { ok: false; failure: "unsupported" | "malformed" };

class Malformed extends Error {}

function bad(): never {
  throw new Malformed();
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : bad();
}

function text(source: Record<string, unknown>, key: string, pattern?: RegExp): string {
  const value = source[key];
  return typeof value === "string" && (pattern === undefined || pattern.test(value)) ? value : bad();
}

function power(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : bad();
}

function powerOrNull(value: unknown): number | null {
  return value === null ? null : power(value);
}

function flag(value: unknown): boolean {
  return typeof value === "boolean" ? value : bad();
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : bad();
}

const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function indexes(value: unknown, count: number): number[] {
  return list(value).map((entry) => (typeof entry === "number" && Number.isInteger(entry) && entry >= 0 && entry < count ? entry : bad()));
}

/** The answer, strictly typed; extra keys are allowed (additive fields), a missing or mistyped one is not. */
export function decodePowerMonth(raw: unknown): PowerDecodeResult {
  try {
    const source = record(raw);
    if (source.api_version !== POWER_MONTH_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    if (source.unit !== "kW") {
      bad();
    }
    const components = list(source.components).map(decodeDemandComponent);
    const count = components.length;
    const bars = list(source.bars).map(powerOrNull);
    if (bars.length !== count) {
      bad();
    }
    const days = list(source.days).map((entry): PowerDay => {
      const day = record(entry);
      return {
        date: text(day, "date", DATE),
        peak_kw: power(day.peak_kw),
        peak_start: text(day, "peak_start", INSTANT),
        house_kw: powerOrNull(day.house_kw),
        counted: indexes(day.counted, count),
        held: flag(day.held),
        paused: flag(day.paused),
      };
    });
    const hours =
      source.hours === null
        ? null
        : list(source.hours).map((entry): PowerHour => {
            const hour = record(entry);
            return {
              start: text(hour, "start", INSTANT),
              kw: power(hour.kw),
              house_kw: powerOrNull(hour.house_kw),
              incomplete: flag(hour.incomplete),
              held: flag(hour.held),
              paused: flag(hour.paused),
              counted: indexes(hour.counted, count),
            };
          });
    return {
      ok: true,
      value: {
        month: text(source, "month", MONTH),
        available_months: list(source.available_months).map((entry) => (typeof entry === "string" && MONTH.test(entry) ? entry : bad())),
        import_target_kw: powerOrNull(source.import_target_kw),
        components,
        days,
        hours,
        bars,
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

// ----------------------------------------------------------------------------------------------- state

export type PowerState =
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null }
  | { kind: "ready"; answer: PowerMonth };

export interface PowerUi {
  /** The day whose hours are shown (`YYYY-MM-DD`), or `null` for the month. */
  day: string | null;
  /** A month is being read: the old figures stay, dimmed. */
  pending: boolean;
}

export interface PowerHandlers {
  onDay: (day: string | null) => void;
}

// ------------------------------------------------------------------------------------------- rendering

function element<K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  className?: string,
  content?: string,
): HTMLElementTagNameMap[K] {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (content !== undefined) {
    created.textContent = content;
  }
  return created;
}

/** A power to one decimal: "5,9 kW". */
export function kw(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} kW`;
}

/** The wall clock as the backend wrote it (the area's own zone): `2026-09-22T19:00:00+02:00` -> `19:00`. */
function clockOf(iso: string): string {
  return /T(\d{2}:\d{2})/.exec(iso)?.[1] ?? "";
}

function daysInMonth(month: string): number {
  const [year, number] = month.split("-").map(Number) as [number, number];
  return new Date(Date.UTC(year, number, 0)).getUTCDate();
}

/** The top of the scale: the highest bar or the target, with a little headroom, to the next whole kW (at least 1). */
export function scaleTop(values: number[], target: number | null): number {
  const highest = Math.max(0, ...values, target ?? 0);
  return Math.max(1, Math.ceil(highest * 1.1));
}

/** One bar's figures as words: "huset 2,3 kW, bilen 3,6 kW · räknas för effektavgift 1 · effektmålet höll…". */
function notes(
  language: Language,
  item: { kw: number; house_kw: number | null; counted: number[]; held: boolean; paused: boolean; incomplete?: boolean },
  parts: number,
): string[] {
  const words: string[] = [];
  if (item.house_kw !== null) {
    words.push(
      translate(language, "power.split", {
        house: kw(language, Math.min(item.house_kw, item.kw)),
        car: kw(language, Math.max(0, item.kw - item.house_kw)),
      }),
    );
  }
  if (item.counted.length > 0) {
    words.push(
      parts > 1
        ? translate(language, "power.countedPart", { n: item.counted.map((index) => String(index + 1)).join(", ") })
        : translate(language, "power.counted"),
    );
  }
  if (item.paused) {
    words.push(translate(language, "power.paused"));
  } else if (item.held) {
    words.push(translate(language, "power.held"));
  }
  if (item.incomplete === true) {
    words.push(translate(language, "power.incomplete"));
  }
  return words;
}

export function dayFigures(language: Language, day: PowerDay, parts: number): string {
  const head = translate(language, "power.day", {
    date: dateLabel(language, day.date),
    kw: kw(language, day.peak_kw),
    time: clockOf(day.peak_start),
  });
  return [head, ...notes(language, { ...day, kw: day.peak_kw }, parts)].join(" · ");
}

export function hourFigures(language: Language, hour: PowerHour, parts: number): string {
  const head = translate(language, "power.hour", { time: clockOf(hour.start), kw: kw(language, hour.kw) });
  return [head, ...notes(language, hour, parts)].join(" · ");
}

interface Column {
  key: string;
  label: string;
  kw: number | null;
  house: number | null;
  counted: number[];
  held: boolean;
  paused: boolean;
  incomplete: boolean;
  figures: string;
}

/**
 * The bars, the target line, the marks row, the axis and the readout. `choose` is a tap on a column (`null`: a
 * tap only shows its figures in the readout).
 */
function barChart(
  doc: Document,
  language: Language,
  columns: Column[],
  target: number | null,
  label: string,
  hint: string,
  selected: string | null,
  choose: ((key: string) => void) | null,
): HTMLElement {
  const wrap = element(doc, "div", `${C.historyChart} ${C.powerChart}`);
  const top = scaleTop(
    columns.map((column) => column.kw ?? 0),
    target,
  );
  const readout = element(doc, "p", `${C.muted} ${C.historyReadout}`);
  readout.setAttribute("aria-live", "polite");
  const buttons: HTMLButtonElement[] = [];
  let pinned: Column | null = columns.find((column) => column.key === selected) ?? null;
  const show = (column: Column | null): void => {
    readout.textContent = column === null ? hint : column.figures;
    buttons.forEach((button) => button.setAttribute("aria-pressed", String(column !== null && button.dataset["key"] === column.key)));
  };
  const plot = element(doc, "div", `${C.historyBars} ${C.powerPlot}`);
  plot.setAttribute("role", "group");
  plot.setAttribute("aria-label", label);
  if (target !== null) {
    const line = element(doc, "span", C.powerTarget);
    line.setAttribute("aria-hidden", "true");
    line.style.bottom = `${(target / top) * 100}%`;
    line.append(element(doc, "span", C.powerTargetLabel, kw(language, target)));
    plot.append(line);
  }
  const marks = element(doc, "div", C.powerMarks);
  marks.setAttribute("aria-hidden", "true");
  for (const column of columns) {
    const bar = element(doc, "button", `${C.historyBar} ${C.powerBar}`);
    bar.type = "button";
    bar.dataset["key"] = column.key;
    bar.setAttribute("aria-label", column.figures);
    if (column.kw !== null && column.kw > 0) {
      const stack = element(doc, "span", C.powerStack);
      stack.style.height = `${Math.max(1.5, (column.kw / top) * 100)}%`;
      if (column.incomplete) {
        stack.dataset["incomplete"] = "true";
      }
      if (column.house === null) {
        const whole = element(doc, "span", C.powerUnsplit);
        whole.style.flexGrow = "1";
        stack.append(whole);
      } else {
        const house = Math.min(column.house, column.kw);
        const car = element(doc, "span", C.powerCar);
        car.style.flexGrow = String(Math.max(0, column.kw - house));
        const houseSpan = element(doc, "span", C.powerHouse);
        houseSpan.style.flexGrow = String(house);
        stack.append(car, houseSpan);
      }
      if (column.counted.length > 0) {
        stack.dataset["counted"] = column.counted.join(" ");
        const tag = element(doc, "span", C.powerTag, column.counted.map((index) => String(index + 1)).join("·"));
        tag.setAttribute("aria-hidden", "true");
        stack.append(tag);
      }
      bar.append(stack);
    } else {
      bar.append(element(doc, "span", `${C.historyBarFill} ${C.powerEmpty}`));
    }
    bar.addEventListener("mouseenter", () => show(column));
    bar.addEventListener("focus", () => show(column));
    bar.addEventListener("click", () => {
      if (choose !== null && column.kw !== null) {
        choose(column.key);
        return;
      }
      pinned = column;
      show(column);
    });
    buttons.push(bar);
    plot.append(bar);
    const mark = element(doc, "span");
    if (column.paused || column.held) {
      mark.dataset["mark"] = column.paused ? "paused" : "held";
    }
    marks.append(mark);
  }
  plot.addEventListener("mouseleave", () => show(pinned));
  const axis = element(doc, "div", C.historyAxis);
  axis.setAttribute("aria-hidden", "true");
  columns.forEach((column) => axis.append(element(doc, "span", undefined, column.label)));
  const scale = element(doc, "span", `${C.muted} ${C.historyScale}`, kw(language, top));
  scale.setAttribute("aria-hidden", "true");
  wrap.append(scale, plot, marks, axis, readout);
  show(pinned);
  return wrap;
}

function swatch(doc: Document, className: string, label: string): HTMLElement {
  const item = element(doc, "li", C.powerLegendItem);
  const key = element(doc, "span", `${C.powerSwatch} ${className}`);
  key.setAttribute("aria-hidden", "true");
  item.append(key, element(doc, "span", undefined, label));
  return item;
}

/** The legend: house, car, the target, the counted peaks (per part when there are several), the target's marks. */
function legend(doc: Document, language: Language, answer: PowerMonth, columns: Column[]): HTMLElement {
  const list = element(doc, "ul", C.powerLegend);
  list.setAttribute("aria-label", translate(language, "power.legend"));
  list.append(
    swatch(doc, C.powerHouse, translate(language, "power.legend.house")),
    swatch(doc, C.powerCar, translate(language, "power.legend.car")),
  );
  if (columns.some((column) => column.kw !== null && column.house === null)) {
    list.append(swatch(doc, C.powerUnsplit, translate(language, "power.legend.unsplit")));
  }
  if (answer.import_target_kw !== null) {
    list.append(swatch(doc, C.powerTargetSwatch, translate(language, "power.legend.target", { kw: kw(language, answer.import_target_kw) })));
  }
  answer.components.forEach((component, index) => {
    const hours = component.window === null ? translate(language, "demand.allHours") : windowText(language, component.window);
    const label =
      answer.components.length > 1
        ? translate(language, "power.legend.countedPart", { n: String(index + 1), part: hours })
        : translate(language, "power.legend.counted", { part: hours });
    const item = swatch(doc, C.powerCountedSwatch, label);
    item.firstElementChild!.textContent = String(index + 1);
    list.append(item);
  });
  if (columns.some((column) => column.held || column.paused)) {
    list.append(swatch(doc, C.powerMarkSwatch, translate(language, "power.legend.marks")));
  }
  return list;
}

/** The month's figure per part: "Effektavgift 1: 5,9 kW · Effektavgift 2: 4,6 kW". */
function summary(doc: Document, language: Language, answer: PowerMonth): HTMLElement | null {
  if (answer.components.length === 0) {
    return null;
  }
  const parts = answer.bars.map((bar, index) => {
    const name =
      answer.components.length > 1 ? translate(language, "demand.part", { n: String(index + 1) }) : translate(language, "demand.title");
    return `${name}: ${bar === null ? translate(language, "power.noPeak") : kw(language, bar)}`;
  });
  const tile = element(doc, "section", C.historyTile);
  tile.dataset["tile"] = "power";
  tile.append(element(doc, "p", C.historyFigures, parts.join(" · ")));
  return tile;
}

function monthColumns(language: Language, answer: PowerMonth): Column[] {
  const byDate = new Map(answer.days.map((day) => [day.date, day]));
  const length = daysInMonth(answer.month);
  const parts = answer.components.length;
  return Array.from({ length }, (_unused, index) => {
    const number = index + 1;
    const date = `${answer.month}-${String(number).padStart(2, "0")}`;
    const day = byDate.get(date);
    const labelled = number === 1 || number === length || (number % 5 === 0 && length - number >= 3);
    return {
      key: date,
      label: labelled ? String(number) : "",
      kw: day?.peak_kw ?? null,
      house: day?.house_kw ?? null,
      counted: day?.counted ?? [],
      held: day?.held ?? false,
      paused: day?.paused ?? false,
      incomplete: false,
      figures: day === undefined ? translate(language, "power.noData", { date: dateLabel(language, date) }) : dayFigures(language, day, parts),
    };
  });
}

function hourColumns(language: Language, answer: PowerMonth, date: string): Column[] {
  const parts = answer.components.length;
  return (answer.hours ?? [])
    .filter((hour) => hour.start.slice(0, 10) === date)
    .map((hour, index) => {
      const time = clockOf(hour.start);
      const labelled = ["00", "06", "12", "18"].includes(time.slice(0, 2)) && time.endsWith(":00");
      return {
        key: `${hour.start}#${index}`,
        label: labelled ? time.slice(0, 2) : "",
        kw: hour.kw,
        house: hour.house_kw,
        counted: hour.counted,
        held: hour.held,
        paused: hour.paused,
        incomplete: hour.incomplete,
        figures: hourFigures(language, hour, parts),
      };
    });
}

/** The Power view's body under the month picker, for one state. Built fresh each time. */
export function powerBody(doc: Document, language: Language, state: PowerState, ui: PowerUi, handlers: PowerHandlers): HTMLElement {
  const body = element(doc, "div", C.historyMonth);
  body.dataset["view"] = "power";
  if (state.kind === "loading") {
    const loading = element(doc, "p", C.muted, translate(language, "power.loading"));
    loading.setAttribute("role", "status");
    body.append(loading);
    return body;
  }
  if (state.kind === "failed") {
    const failed = element(doc, "p", C.settingsNotice, translate(language, state.sentenceKey));
    failed.setAttribute("role", "status");
    if (state.code !== null) {
      failed.dataset["code"] = state.code;
    }
    body.append(failed);
    return body;
  }
  const answer = state.answer;
  body.dataset["month"] = answer.month;
  if (ui.pending) {
    body.setAttribute("aria-busy", "true");
    body.dataset["pending"] = "true";
  }
  body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(language, "power.intro")));
  if (answer.days.length === 0) {
    body.append(element(doc, "p", C.muted, translate(language, "power.empty")));
    return body;
  }
  const tile = summary(doc, language, answer);
  if (tile !== null) {
    body.append(tile);
  }
  const parts = answer.components.length;
  const day = ui.day !== null && answer.hours !== null ? answer.days.find((entry) => entry.date === ui.day) ?? null : null;
  let columns: Column[];
  if (day !== null) {
    const head = element(doc, "div", C.powerDayHead);
    const back = element(doc, "button", C.historyToggleButton, `‹ ${translate(language, "power.back")}`);
    back.type = "button";
    back.dataset["action"] = "power-month";
    back.addEventListener("click", () => handlers.onDay(null));
    head.append(back, element(doc, "h4", C.historyHeading, dayFigures(language, day, parts)));
    body.append(head);
    columns = hourColumns(language, answer, day.date);
    body.append(
      barChart(
        doc,
        language,
        columns,
        answer.import_target_kw,
        translate(language, "power.dayChart.label", { date: dateLabel(language, day.date) }),
        translate(language, "power.hint.day"),
        null,
        null,
      ),
    );
  } else {
    columns = monthColumns(language, answer);
    const drill = answer.hours !== null;
    body.append(
      barChart(
        doc,
        language,
        columns,
        answer.import_target_kw,
        translate(language, "power.chart.label"),
        translate(language, drill ? "power.hint.month" : "power.hint.monthDaysOnly"),
        null,
        drill ? (key) => handlers.onDay(key) : null,
      ),
    );
  }
  body.append(legend(doc, language, answer, columns));
  return body;
}
