// The heat card's history (`spotnav/get_heat_history`): one day, month or year of the house's heating, behind the clock
// in the card's header. The decoded answer and the dialog's body: the heat pump's cost per hour, day or month
// (the shadow's saving, at most, hatched on top), use and cost with hot water split out where the recorder still has the
// 5-minute rows, kWh per degree-day with its monthly trend, comfort, what SpotNav would have moved in shadow, the
// immersion heater where its power is read and, with Detailed on, the day's hot-water runs one by one. Every figure is
// the backend's; text is written with `textContent` only.

import { clock, formatFixed, formatNumber, type FormatContext } from "./format";
import { HEAT_HISTORY_API_VERSION } from "./heat-model";
import { bucketWords, isoDay, periodKind } from "./house-history";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  element,
  figureRow,
  note,
  periodBars,
  periodNav,
  periodStart,
  section,
  statusLine,
  type HistoryPeriod,
  type PeriodBar,
  type PeriodHandlers,
  type PeriodUi,
} from "./period-history";
import { monthLabel } from "./history";
import { VISUAL_CLASSES as C } from "./visual-styles";

export interface HeatBucket {
  key: string;
  kwh: number | null;
  hot_water_kwh: number | null;
  cost: number | null;
  degree_days: number | null;
  outdoor_c: number | null;
  /** What the shadow rule would have saved in the bucket's hours, at most; `null` where it was not evaluated. */
  shadow_value: number | null;
}

export interface TrendMonth {
  month: string;
  days: number;
  kwh: number | null;
  degree_days: number | null;
  kwh_per_degree_day: number | null;
}

export interface HeatRun {
  start: string;
  end: string;
  kwh: number;
  cause: "draw" | "standby" | null;
}

export interface HeatHistory {
  site_id: string;
  time_zone: string;
  period: HistoryPeriod;
  date: string;
  end: string;
  today: string;
  complete: boolean;
  money: { currency: string; major: string | null; minor: string | null } | null;
  meter: { entity_id: string; name: string | null } | null;
  energy: { total_kwh: number; hours: number; hot_water_kwh: number | null; split_days: number; days: number; cost: number | null; priced_hours: number } | null;
  buckets: HeatBucket[];
  degree_days: {
    base_c: number;
    outdoor: { entity_id: string | null; source: string };
    sum: number;
    days: number;
    kwh: number;
    kwh_per_degree_day: number | null;
    trend: TrendMonth[];
  } | null;
  comfort: { low_c: number; sensors: number; coldest: { entity_id: string; name: string; mean_c: number; min_c: number } | null; hours: number; hours_below: number } | null;
  shadow: { days: number; lowered_hours: number; moved_kwh: number; value: number } | null;
  immersion: { hours: number; kwh: number } | null;
  runs: HeatRun[];
}

export type HeatHistoryDecode = { ok: true; value: HeatHistory } | { ok: false; failure: "unsupported" | "malformed" };

class Malformed extends Error {}

function bad(): never {
  throw new Malformed();
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : bad();
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : bad();
}

function text(value: unknown): string {
  return typeof value === "string" ? value : bad();
}

function textOrNull(value: unknown): string | null {
  return value === null ? null : text(value);
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function numberOrNull(value: unknown): number | null {
  return value === null ? null : number(value);
}

function count(value: unknown): number {
  return Number.isInteger(value) && (value as number) >= 0 ? (value as number) : bad();
}

function flag(value: unknown): boolean {
  return typeof value === "boolean" ? value : bad();
}

function nullable<T>(value: unknown, read: (source: Record<string, unknown>) => T): T | null {
  return value === null ? null : read(record(value));
}

const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)$/;

function instant(value: unknown): string {
  const raw = text(value);
  return INSTANT.test(raw) && Number.isFinite(Date.parse(raw)) ? raw : bad();
}

/** The answer, strictly typed; extra keys are allowed (additive fields), a missing or mistyped one is not. */
export function decodeHeatHistory(raw: unknown): HeatHistoryDecode {
  try {
    const source = record(raw);
    if (source.api_version !== HEAT_HISTORY_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    return {
      ok: true,
      value: {
        site_id: text(source.site_id),
        time_zone: text(source.time_zone),
        period: periodKind(source.period),
        date: isoDay(source.date),
        end: isoDay(source.end),
        today: isoDay(source.today),
        complete: flag(source.complete),
        money: nullable(source.money, (item) => ({ currency: text(item.currency), major: textOrNull(item.major), minor: textOrNull(item.minor) })),
        meter: nullable(source.meter, (item) => ({ entity_id: text(item.entity_id), name: textOrNull(item.name) })),
        energy: nullable(source.energy, (item) => ({
          total_kwh: number(item.total_kwh),
          hours: count(item.hours),
          hot_water_kwh: numberOrNull(item.hot_water_kwh),
          split_days: count(item.split_days),
          days: count(item.days),
          cost: numberOrNull(item.cost),
          priced_hours: count(item.priced_hours),
        })),
        buckets: list(source.buckets).map((raw): HeatBucket => {
          const item = record(raw);
          return {
            key: text(item.key),
            kwh: numberOrNull(item.kwh),
            hot_water_kwh: numberOrNull(item.hot_water_kwh),
            cost: numberOrNull(item.cost),
            degree_days: numberOrNull(item.degree_days),
            outdoor_c: numberOrNull(item.outdoor_c),
            shadow_value: numberOrNull(item.shadow_value),
          };
        }),
        degree_days: nullable(source.degree_days, (item) => {
          const outdoor = record(item.outdoor);
          return {
            base_c: number(item.base_c),
            outdoor: { entity_id: textOrNull(outdoor.entity_id), source: text(outdoor.source) },
            sum: number(item.sum),
            days: count(item.days),
            kwh: number(item.kwh),
            kwh_per_degree_day: numberOrNull(item.kwh_per_degree_day),
            trend: list(item.trend).map((raw): TrendMonth => {
              const month = record(raw);
              const name = text(month.month);
              return {
                month: /^\d{4}-\d{2}$/.test(name) ? name : bad(),
                days: count(month.days),
                kwh: numberOrNull(month.kwh),
                degree_days: numberOrNull(month.degree_days),
                kwh_per_degree_day: numberOrNull(month.kwh_per_degree_day),
              };
            }),
          };
        }),
        comfort: nullable(source.comfort, (item) => ({
          low_c: number(item.low_c),
          sensors: count(item.sensors),
          coldest: nullable(item.coldest, (cold) => ({ entity_id: text(cold.entity_id), name: text(cold.name), mean_c: number(cold.mean_c), min_c: number(cold.min_c) })),
          hours: count(item.hours),
          hours_below: count(item.hours_below),
        })),
        shadow: nullable(source.shadow, (item) => ({
          days: count(item.days),
          lowered_hours: count(item.lowered_hours),
          moved_kwh: number(item.moved_kwh),
          value: number(item.value),
        })),
        immersion: nullable(source.immersion, (item) => ({ hours: count(item.hours), kwh: number(item.kwh) })),
        runs: list(source.runs).map((raw): HeatRun => {
          const item = record(raw);
          const cause = item.cause;
          return {
            start: instant(item.start),
            end: instant(item.end),
            kwh: number(item.kwh),
            cause: cause === null ? null : cause === "draw" || cause === "standby" ? cause : bad(),
          };
        }),
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

// ------------------------------------------------------------------------------------------------ view

export type HeatHistoryState =
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null; answer?: HeatHistory | null }
  | { kind: "ready"; answer: HeatHistory };

export interface HeatHistoryInput {
  doc: Document;
  language: Language;
  state: HeatHistoryState;
  ui: PeriodUi;
  handlers: PeriodHandlers;
  width: number;
  idPrefix: string;
  today: string;
}

function kwh(language: Language, value: number): string {
  return `${formatFixed(language, value, value >= 100 ? 0 : 1)} kWh`;
}

function celsius(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} °C`;
}

/** Money in the minor unit shown in the major one (whole units from 10 up), as the heat card writes it. */
function money(language: Language, minor: number, units: HeatHistory["money"]): string {
  if (units === null || units.major === null) {
    return `${formatNumber(language, minor, 0)}${units?.minor ? ` ${units.minor}` : ""}`;
  }
  const major = minor / 100;
  return `${formatFixed(language, major, Math.abs(major) >= 10 ? 0 : 2)} ${units.major}`;
}

function heatBars(input: HeatHistoryInput, answer: HeatHistory): HTMLElement | null {
  const { doc, language, ui } = input;
  if (answer.buckets.length === 0 || answer.buckets.every((item) => item.kwh === null)) {
    return null;
  }
  const priced = answer.buckets.some((item) => item.cost !== null);
  const bars: PeriodBar[] = answer.buckets.map((item, index) => {
    const words = bucketWords(language, answer.period, item.key, index, answer.buckets.length);
    const parts: string[] = [];
    if (item.kwh === null) {
      parts.push(translate(language, "hist.heat.barNone", { when: words.when }));
    } else {
      parts.push(`${words.when}: ${kwh(language, item.kwh)}`);
      if (item.cost !== null) {
        parts.push(money(language, item.cost, answer.money));
      }
      if (item.shadow_value !== null && item.cost !== null) {
        parts.push(translate(language, "hist.heat.barSaving", { saving: money(language, item.shadow_value, answer.money) }));
      }
      if (item.hot_water_kwh !== null) {
        parts.push(translate(language, "hist.heat.barHotWater", { kwh: kwh(language, item.hot_water_kwh) }));
      }
      if (item.outdoor_c !== null) {
        parts.push(translate(language, "hist.heat.barOutdoor", { temp: celsius(language, item.outdoor_c) }));
      }
      if (item.degree_days !== null) {
        parts.push(translate(language, "hist.heat.barDegreeDays", { dd: formatFixed(language, item.degree_days, 1) }));
      }
    }
    if (!priced) {
      return { key: item.key, value: item.kwh, dear: null, words: parts.join(" · "), axis: words.axis };
    }
    return {
      key: item.key,
      value: null,
      dear: null,
      words: parts.join(" · "),
      axis: words.axis,
      money: { cost: item.kwh === null ? null : item.cost, saving: item.cost === null ? null : item.shadow_value },
    };
  });
  if (!priced) {
    return periodBars(doc, language, translate(language, "hist.heat.chart"), "kWh", bars, ui, translate(language, "hist.heat.hint"));
  }
  return periodBars(doc, language, translate(language, "hist.heat.chart"), "", bars, ui, translate(language, "hist.heat.hint"), {
    scaleText: (top) => money(language, top, answer.money),
    legend: translate(language, "hist.heat.legend"),
  });
}

function useSection(input: HeatHistoryInput, answer: HeatHistory): HTMLElement {
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.heat.use"), "use");
  const energy = answer.energy;
  if (answer.meter === null || energy === null) {
    block.append(note(doc, translate(language, "hist.heat.noMeter"), "no-meter"));
    return block;
  }
  if (energy.hours === 0) {
    block.append(note(doc, translate(language, "hist.heat.noEnergy"), "no-energy"));
    return block;
  }
  block.append(figureRow(doc, translate(language, "hist.heat.total"), kwh(language, energy.total_kwh), "total"));
  if (energy.hot_water_kwh !== null) {
    block.append(
      figureRow(doc, translate(language, "hist.heat.hotWater"), kwh(language, energy.hot_water_kwh), "hot-water"),
      note(doc, translate(language, "hist.heat.splitDays", { days: String(energy.split_days), total: String(energy.days) })),
    );
  } else {
    block.append(note(doc, translate(language, "hist.heat.noSplit"), "no-split"));
  }
  if (energy.cost !== null) {
    block.append(figureRow(doc, translate(language, "hist.heat.cost"), money(language, energy.cost, answer.money), "cost"));
    if (energy.priced_hours < energy.hours) {
      block.append(note(doc, translate(language, "hist.house.costPartial", { share: `${formatNumber(language, (energy.priced_hours / energy.hours) * 100, 0)} %` })));
    }
  } else {
    block.append(note(doc, translate(language, "hist.house.noPrices"), "no-cost"));
  }
  if (answer.immersion !== null) {
    block.append(
      figureRow(
        doc,
        translate(language, "hist.heat.immersion"),
        translate(language, "hist.heat.immersionFigure", { hours: String(answer.immersion.hours), kwh: kwh(language, answer.immersion.kwh) }),
        "immersion",
      ),
    );
  }
  return block;
}

function trendChart(input: HeatHistoryInput, answer: HeatHistory): HTMLElement | null {
  const degree = answer.degree_days;
  if (degree === null || degree.trend.length === 0 || degree.trend.every((month) => month.kwh_per_degree_day === null)) {
    return null;
  }
  const { doc, language, ui } = input;
  const bars: PeriodBar[] = degree.trend.map((month) => ({
    key: `trend-${month.month}`,
    value: month.kwh_per_degree_day,
    dear: null,
    words:
      month.kwh_per_degree_day === null
        ? translate(language, "hist.heat.trendNone", { month: monthLabel(language, month.month) })
        : translate(language, "hist.heat.trendBar", {
            month: monthLabel(language, month.month),
            value: formatFixed(language, month.kwh_per_degree_day, 2),
            dd: formatFixed(language, month.degree_days ?? 0, 0),
          }),
    axis: new Intl.DateTimeFormat(language, { month: "narrow", timeZone: "UTC" }).format(new Date(`${month.month}-15T12:00:00Z`)),
  }));
  return periodBars(doc, language, translate(language, "hist.heat.trend"), translate(language, "hist.heat.perDegreeDayUnit"), bars, ui, translate(language, "hist.heat.trendHint"));
}

function degreeSection(input: HeatHistoryInput, answer: HeatHistory): HTMLElement {
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.heat.degreeDays"), "degree-days");
  const degree = answer.degree_days;
  if (degree === null) {
    block.append(note(doc, translate(language, "hist.heat.noOutdoor"), "no-outdoor"));
    return block;
  }
  block.append(
    figureRow(
      doc,
      translate(language, "hist.heat.perDegreeDay"),
      degree.kwh_per_degree_day === null ? translate(language, "hist.heat.tooFew") : `${formatFixed(language, degree.kwh_per_degree_day, 2)} ${translate(language, "hist.heat.perDegreeDayUnit")}`,
      "per-degree-day",
      degree.kwh_per_degree_day === null,
    ),
    note(
      doc,
      translate(language, "hist.heat.degreeNote", {
        base: formatFixed(language, degree.base_c, 0),
        sum: formatFixed(language, degree.sum, 0),
        days: String(degree.days),
      }),
    ),
  );
  if (degree.outdoor.source === "house_forecast") {
    block.append(note(doc, translate(language, "hist.heat.fromForecast"), "from-forecast"));
  }
  const trend = trendChart(input, answer);
  if (trend !== null) {
    block.append(element(doc, "h4", C.historyTileHeading, translate(language, "hist.heat.trend")), trend);
  }
  return block;
}

function comfortSection(input: HeatHistoryInput, answer: HeatHistory): HTMLElement {
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.heat.comfort"), "comfort");
  const comfort = answer.comfort;
  if (comfort === null || comfort.coldest === null) {
    block.append(note(doc, translate(language, comfort === null ? "hist.heat.noRooms" : "hist.heat.noRoomHours"), "no-rooms"));
    return block;
  }
  block.append(
    figureRow(
      doc,
      translate(language, "hist.heat.coldest", { name: comfort.coldest.name }),
      translate(language, "hist.heat.coldestFigure", { mean: celsius(language, comfort.coldest.mean_c), min: celsius(language, comfort.coldest.min_c) }),
      "coldest",
    ),
    figureRow(
      doc,
      translate(language, "hist.heat.below", { low: celsius(language, comfort.low_c) }),
      translate(language, "hist.heat.belowFigure", { hours: String(comfort.hours_below), total: String(comfort.hours) }),
      "below",
    ),
  );
  return block;
}

function shadowSection(input: HeatHistoryInput, answer: HeatHistory): HTMLElement | null {
  if (answer.meter === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.heat.shadow"), "shadow");
  const shadow = answer.shadow;
  if (shadow === null) {
    block.append(note(doc, translate(language, "hist.heat.shadowNone"), "no-shadow"));
    return block;
  }
  block.append(
    figureRow(doc, translate(language, "hist.heat.shadowValue"), money(language, shadow.value, answer.money), "value"),
    note(
      doc,
      translate(language, "hist.heat.shadowLine", {
        kwh: kwh(language, shadow.moved_kwh),
        hours: String(shadow.lowered_hours),
        days: String(shadow.days),
      }),
    ),
    note(doc, translate(language, "hist.heat.shadowNote")),
  );
  return block;
}

const CAUSE_KEYS: Record<string, TranslationKey> = {
  draw: "hist.heat.causeDraw",
  standby: "hist.heat.causeStandby",
};

function runsSection(input: HeatHistoryInput, answer: HeatHistory): HTMLElement | null {
  if (answer.period !== "day" || answer.meter === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.heat.runs"), "runs");
  if (answer.runs.length === 0) {
    block.append(note(doc, translate(language, answer.energy?.hot_water_kwh === null ? "hist.heat.noSplit" : "hist.heat.noRuns"), "no-runs"));
    return block;
  }
  const context: FormatContext = { language, timeZone: answer.time_zone, unit: "kWh", currency: null, majorUnit: null };
  const rows = element(doc, "ul", "spotnav-period-runs");
  for (const run of answer.runs) {
    const row = element(doc, "li");
    row.dataset["run"] = run.start;
    row.append(
      element(doc, "span", undefined, `${clock(context, Date.parse(run.start))}–${clock(context, Date.parse(run.end))}`),
      element(doc, "span", "spotnav-period-figure", `${kwh(language, run.kwh)} · ${translate(language, run.cause === null ? "hist.heat.causeUnknown" : CAUSE_KEYS[run.cause]!)}`),
    );
    rows.append(row);
  }
  block.append(rows, note(doc, translate(language, "hist.heat.runsNote")));
  return block;
}

/** The dialog's body for one state; built fresh each time. */
export function heatHistoryBody(input: HeatHistoryInput): HTMLElement {
  const { doc, language, state, ui } = input;
  const body = element(doc, "div", C.historyBody);
  const answer = state.kind === "ready" ? state.answer : state.kind === "failed" ? (state.answer ?? null) : null;
  const today = answer?.today ?? input.today;
  const shown = answer !== null && ui.date === null && answer.period === ui.period ? answer.date : ui.date ?? today;
  body.append(periodNav(doc, language, ui, today, periodStart(ui.period, shown), input.handlers));
  if (state.kind === "loading") {
    body.append(statusLine(doc, language, "hist.loading"));
    return body;
  }
  if (state.kind === "failed") {
    body.append(statusLine(doc, language, state.sentenceKey, state.code));
    return body;
  }
  const shownAnswer = state.answer;
  const period = element(doc, "div", C.historyMonth);
  period.dataset["period"] = shownAnswer.period;
  period.dataset["date"] = shownAnswer.date;
  if (ui.pending) {
    period.setAttribute("aria-busy", "true");
  }
  for (const part of [
    heatBars(input, shownAnswer),
    useSection(input, shownAnswer),
    degreeSection(input, shownAnswer),
    comfortSection(input, shownAnswer),
    shadowSection(input, shownAnswer),
    ui.detailed ? runsSection(input, shownAnswer) : null,
  ]) {
    if (part !== null) {
      period.append(part);
    }
  }
  body.append(period);
  body.append(note(doc, translate(language, "hist.heat.footnote")));
  return body;
}
