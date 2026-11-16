// The house card's history (`spotnav/get_house_history`): one day, month or year of the house, behind the clock in the
// card's header. The decoded answer and the dialog's body: the bars of the period (the house's use per hour, day or
// month, coloured by what its import cost), the energy flow summed from the backend's per-period flows, the money
// (what the period cost, the battery against no battery, what the sun gave) and, with Detailed on, the battery's
// modes side by side, the forecasts' errors and each reviewed day. Every figure is the backend's; text is written
// with `textContent` only.

import { dateLabel, formatFixed, formatNumber } from "./format";
import { renderFlow } from "./house-flow";
import { decodeEnergyFlow, HOUSE_HISTORY_API_VERSION, MODES, type EnergyFlow } from "./house-model";
import { amount } from "./house-view";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  axisShown,
  element,
  figureRow,
  note,
  periodBars,
  periodNav,
  periodStart,
  pricePositions,
  section,
  statusLine,
  type HistoryPeriod,
  type PeriodBar,
  type PeriodHandlers,
  type PeriodUi,
} from "./period-history";
import { VISUAL_CLASSES as C } from "./visual-styles";

export interface HouseBucket {
  key: string;
  grid_in: number;
  grid_out: number;
  solar: number | null;
  battery_in: number | null;
  battery_out: number | null;
  car: number;
  heat: number | null;
  house: number;
  use: number;
  cost: number | null;
  periods: number;
}

export interface HouseCost {
  net: number;
  bought: number;
  sold: number;
  kwh_in: number;
  kwh_out: number;
  periods: number;
  priced: number;
}

export interface HouseReviewDay {
  date: string;
  battery_saving: number | null;
  plan_saving: number;
  own_saving: number;
}

export interface HouseReview {
  days: number;
  period_days: number;
  archived: number;
  actual_cost: number;
  battery_saving: number | null;
  battery_days: number;
  sun_gave: number | null;
  sun_days: number;
  against_nothing: number | null;
  nothing_days: number;
  plan_saving: number;
  own_saving: number;
  modes: Record<string, { saving: number | null; days: number }>;
  mode: string | null;
  by_day: HouseReviewDay[];
}

export interface ForecastError {
  mae: number;
  days: number;
}

export interface HouseHistory {
  site_id: string;
  time_zone: string;
  period: HistoryPeriod;
  date: string;
  end: string;
  today: string;
  complete: boolean;
  money: { currency: string; major_unit: string | null; minor_unit: string | null } | null;
  energy_flow: EnergyFlow | null;
  buckets: HouseBucket[];
  cost: HouseCost | null;
  review: HouseReview | null;
  forecast: { house: ForecastError | null; sun: ForecastError | null; sun_corrected: ForecastError | null; soc: ForecastError | null };
}

export type HouseHistoryDecode = { ok: true; value: HouseHistory } | { ok: false; failure: "unsupported" | "malformed" };

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

const DATE = /^\d{4}-\d{2}-\d{2}$/;

export function isoDay(value: unknown): string {
  const raw = text(value);
  return DATE.test(raw) ? raw : bad();
}

export function periodKind(value: unknown): HistoryPeriod {
  return value === "day" || value === "month" || value === "year" ? value : bad();
}

function bucket(value: unknown): HouseBucket {
  const source = record(value);
  return {
    key: text(source.key),
    grid_in: number(source.grid_in),
    grid_out: number(source.grid_out),
    solar: numberOrNull(source.solar),
    battery_in: numberOrNull(source.battery_in),
    battery_out: numberOrNull(source.battery_out),
    car: number(source.car),
    heat: numberOrNull(source.heat),
    house: number(source.house),
    use: number(source.use),
    cost: numberOrNull(source.cost),
    periods: count(source.periods),
  };
}

function cost(value: unknown): HouseCost | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  return {
    net: number(source.net),
    bought: number(source.bought),
    sold: number(source.sold),
    kwh_in: number(source.kwh_in),
    kwh_out: number(source.kwh_out),
    periods: count(source.periods),
    priced: count(source.priced),
  };
}

function review(value: unknown): HouseReview | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  const modes: Record<string, { saving: number | null; days: number }> = {};
  for (const [mode, raw] of Object.entries(record(source.modes))) {
    const item = record(raw);
    modes[mode] = { saving: numberOrNull(item.saving), days: count(item.days) };
  }
  return {
    days: count(source.days),
    period_days: count(source.period_days),
    archived: count(source.archived),
    actual_cost: number(source.actual_cost),
    battery_saving: numberOrNull(source.battery_saving),
    battery_days: count(source.battery_days),
    sun_gave: numberOrNull(source.sun_gave),
    sun_days: count(source.sun_days),
    against_nothing: numberOrNull(source.against_nothing),
    nothing_days: count(source.nothing_days),
    plan_saving: number(source.plan_saving),
    own_saving: number(source.own_saving),
    modes,
    mode: textOrNull(source.mode),
    by_day: list(source.by_day).map((raw) => {
      const item = record(raw);
      return {
        date: isoDay(item.date),
        battery_saving: numberOrNull(item.battery_saving),
        plan_saving: number(item.plan_saving),
        own_saving: number(item.own_saving),
      };
    }),
  };
}

function forecastError(value: unknown): ForecastError | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  return { mae: number(source.mae), days: count(source.days) };
}

/** The answer, strictly typed; extra keys are allowed (additive fields), a missing or mistyped one is not. */
export function decodeHouseHistory(raw: unknown): HouseHistoryDecode {
  try {
    const source = record(raw);
    if (source.api_version !== HOUSE_HISTORY_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    const money = source.money === null ? null : record(source.money);
    const forecast = record(source.forecast);
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
        money: money === null ? null : { currency: text(money.currency), major_unit: textOrNull(money.major_unit), minor_unit: textOrNull(money.minor_unit) },
        energy_flow: decodeEnergyFlow(source.energy_flow),
        buckets: list(source.buckets).map(bucket),
        cost: cost(source.cost),
        review: review(source.review),
        forecast: {
          house: forecastError(forecast.house),
          sun: forecastError(forecast.sun),
          sun_corrected: forecastError(forecast.sun_corrected),
          soc: forecastError(forecast.soc),
        },
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

// ------------------------------------------------------------------------------------------------ view

export type HouseHistoryState =
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null; answer?: HouseHistory | null }
  | { kind: "ready"; answer: HouseHistory };

export interface HouseHistoryInput {
  doc: Document;
  language: Language;
  state: HouseHistoryState;
  ui: PeriodUi;
  handlers: PeriodHandlers;
  width: number;
  idPrefix: string;
  /** The site's local date before the first answer (the browser's, for the picker only). */
  today: string;
}

function kwh(language: Language, value: number): string {
  return `${formatFixed(language, value, value >= 100 ? 0 : 1)} kWh`;
}

const monthNames = new Map<string, Intl.DateTimeFormat>();

function narrowMonth(language: Language, month: number): string {
  let format = monthNames.get(language);
  if (format === undefined) {
    format = new Intl.DateTimeFormat(language, { month: "narrow", timeZone: "UTC" });
    monthNames.set(language, format);
  }
  return format.format(new Date(Date.UTC(2026, month - 1, 15, 12)));
}

/** A bucket's name in words and on the axis: an hour of a day, a day of a month, a month of a year. */
export function bucketWords(language: Language, period: HistoryPeriod, key: string, index: number, total: number): { when: string; axis: string } {
  if (period === "day") {
    // `2026-11-15T07:00:00+01:00`: the local wall clock as the backend wrote it.
    const clock = /T(\d{2}):(\d{2})/.exec(key);
    const hour = clock === null ? "" : clock[1]!;
    return { when: clock === null ? key : `${hour}:${clock[2]}`, axis: Number(hour) % 6 === 0 ? hour : "" };
  }
  if (period === "month") {
    return { when: dateLabel(language, key), axis: axisShown(index, total, 5) ? String(index + 1) : "" };
  }
  const month = Number(key.slice(5, 7));
  const label = new Intl.DateTimeFormat(language, { month: "long", timeZone: "UTC" }).format(new Date(Date.UTC(2026, month - 1, 15, 12)));
  return { when: label, axis: narrowMonth(language, month) };
}

function houseBars(input: HouseHistoryInput, answer: HouseHistory): HTMLElement | null {
  const { doc, language, ui } = input;
  if (answer.buckets.length === 0 || answer.buckets.every((item) => item.periods === 0)) {
    return null;
  }
  const prices = pricePositions(answer.buckets.map((item) => (item.cost !== null && item.grid_in > 0.05 ? item.cost / item.grid_in : null)));
  const bars: PeriodBar[] = answer.buckets.map((item, index) => {
    const words = bucketWords(language, answer.period, item.key, index, answer.buckets.length);
    let figures: string;
    if (item.periods === 0) {
      figures = translate(language, "hist.house.barNone", { when: words.when });
    } else {
      figures = translate(language, item.cost === null ? "hist.house.barNoCost" : "hist.house.bar", {
        when: words.when,
        use: kwh(language, item.use),
        bought: kwh(language, item.grid_in),
        cost: item.cost === null ? "" : amount(language, item.cost, answer.money),
      });
    }
    return { key: item.key, value: item.periods === 0 ? null : item.use, dear: prices[index] ?? null, words: figures, axis: words.axis };
  });
  return periodBars(doc, language, translate(language, "hist.house.chart"), "kWh", bars, ui, translate(language, "hist.house.hint"));
}

function flowSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement | null {
  const flow = answer.energy_flow;
  if (flow === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.house.flow"), "energy-flow");
  const diagram = renderFlow(doc, language, flow, Math.max(240, Math.floor(input.width) - 22), `${input.idPrefix}-history`, true);
  if (diagram === null) {
    block.append(note(doc, translate(language, "hist.house.noEnergy"), "no-energy"));
    return block;
  }
  block.append(diagram, note(doc, translate(language, flow.step_min < 60 ? "house.flow.ruleQuarter" : "house.flow.rule")));
  if (flow.nodes.some((node) => node.role === "heat")) {
    block.append(note(doc, translate(language, flow.heat_hours < flow.hours ? "house.flow.heatPart" : "house.flow.heatRule")));
  }
  return block;
}

function share(language: Language, part: number, whole: number): string {
  return whole > 0 ? `${formatNumber(language, (part / whole) * 100, 0)} %` : "–";
}

function moneySection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement {
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.house.money"), "money");
  const spent = answer.cost;
  if (spent === null || spent.priced === 0) {
    block.append(note(doc, translate(language, answer.money === null ? "hist.house.noPriceSource" : "hist.house.noPrices"), "no-cost"));
  } else {
    block.append(
      figureRow(doc, translate(language, "hist.house.cost"), amount(language, spent.net, answer.money), "cost"),
      note(
        doc,
        translate(language, "hist.house.costParts", {
          bought: amount(language, spent.bought, answer.money),
          kwhIn: kwh(language, spent.kwh_in),
          sold: amount(language, spent.sold, answer.money),
          kwhOut: kwh(language, spent.kwh_out),
        }),
      ),
    );
    if (spent.priced < spent.periods) {
      block.append(note(doc, translate(language, "hist.house.costPartial", { share: share(language, spent.priced, spent.periods) }), "cost-partial"));
    }
  }
  const reviewed = answer.review;
  if (reviewed === null) {
    block.append(note(doc, translate(language, "hist.house.noReview"), "no-review"));
    return block;
  }
  for (const [key, value, days] of [
    ["battery", reviewed.battery_saving, reviewed.battery_days],
    ["sun", reviewed.sun_gave, reviewed.sun_days],
    ["both", reviewed.against_nothing, reviewed.nothing_days],
  ] as const) {
    if (value === null) {
      continue;
    }
    const figure = amount(language, value, answer.money);
    block.append(
      figureRow(
        doc,
        translate(language, `house.baselines.${key}`),
        days < reviewed.days ? `${figure} (${translate(language, "hist.days", { days: String(days) })})` : figure,
        key,
      ),
    );
  }
  block.append(note(doc, translate(language, "hist.house.baselinesNote")));
  block.append(
    note(
      doc,
      translate(language, "hist.house.reviewed", { days: String(reviewed.days), total: String(Math.max(reviewed.days, reviewed.period_days)) }),
      "reviewed",
    ),
  );
  if (reviewed.archived > 0) {
    block.append(note(doc, translate(language, "hist.house.archived", { days: String(reviewed.archived) })));
  }
  return block;
}

function modesSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement | null {
  const reviewed = answer.review;
  if (reviewed === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.house.modes"), "modes");
  for (const mode of MODES) {
    const figures = reviewed.modes[mode];
    const reference = mode === "self_consumption";
    let figure = reference ? translate(language, "house.compare.reference") : translate(language, "house.compare.none");
    if (!reference && figures !== undefined && figures.saving !== null) {
      figure = amount(language, figures.saving, answer.money);
      if (figures.days < reviewed.days) {
        figure += ` (${translate(language, "hist.days", { days: String(figures.days) })})`;
      }
    }
    const name = translate(language, `house.mode.${mode}`) + (mode === reviewed.mode ? ` · ${translate(language, "house.compare.inForce")}` : "");
    block.append(figureRow(doc, name, figure, mode, reference || figures?.saving == null));
  }
  block.append(figureRow(doc, translate(language, "house.modes.own"), amount(language, reviewed.own_saving, answer.money), "own"));
  block.append(note(doc, translate(language, "hist.house.modesNote")));
  return block;
}

function forecastSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement {
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.forecast.heading"), "forecast");
  const rows: Array<[string, TranslationKey, ForecastError | null, string]> = [
    ["house", "hist.forecast.house", answer.forecast.house, "kW"],
    ["sun", "hist.forecast.sun", answer.forecast.sun, "kWh"],
    ["sun_corrected", "hist.forecast.sunCorrected", answer.forecast.sun_corrected, "kWh"],
    ["soc", "hist.forecast.soc", answer.forecast.soc, "%"],
  ];
  let any = false;
  for (const [key, label, error, unit] of rows) {
    if (error === null) {
      continue;
    }
    any = true;
    const value = unit === "%" ? `±${formatNumber(language, error.mae, 0)} %` : `±${formatFixed(language, error.mae, error.mae < 1 ? 2 : 1)} ${unit}`;
    block.append(figureRow(doc, translate(language, label), `${value} (${translate(language, "hist.days", { days: String(error.days) })})`, key));
  }
  block.append(note(doc, translate(language, any ? "hist.forecast.note" : "hist.forecast.none")));
  return block;
}

function daysSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement | null {
  const reviewed = answer.review;
  if (reviewed === null || answer.period === "day" || reviewed.by_day.length === 0) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.house.days"), "days");
  const listNode = element(doc, "ul", "spotnav-period-runs");
  for (const day of reviewed.by_day) {
    const row = element(doc, "li");
    row.dataset["date"] = day.date;
    row.append(
      element(doc, "span", undefined, dateLabel(language, day.date)),
      element(
        doc,
        "span",
        "spotnav-period-figure",
        translate(language, "hist.house.dayRow", {
          battery: day.battery_saving === null ? "–" : amount(language, day.battery_saving, answer.money),
          plan: amount(language, day.plan_saving, answer.money),
          own: amount(language, day.own_saving, answer.money),
        }),
      ),
    );
    listNode.append(row);
  }
  block.append(listNode, note(doc, translate(language, "hist.house.daysNote")));
  return block;
}

/** The dialog's body for one state; built fresh each time. */
export function houseHistoryBody(input: HouseHistoryInput): HTMLElement {
  const { doc, language, state, ui } = input;
  const body = element(doc, "div", C.historyBody);
  const answer = state.kind === "ready" ? state.answer : state.kind === "failed" ? (state.answer ?? null) : null;
  const today = answer?.today ?? input.today;
  const shown = answer !== null && ui.date === null && answer.period === ui.period ? answer.date : ui.date ?? today;
  body.append(periodNav(doc, language, ui, today, shown.length === 10 ? periodStart(ui.period, shown) : shown, input.handlers));
  if (state.kind === "loading") {
    body.append(statusLine(doc, language, "hist.loading"));
    return body;
  }
  if (state.kind === "failed") {
    body.append(statusLine(doc, language, state.sentenceKey, state.code));
    return body;
  }
  const period = element(doc, "div", C.historyMonth);
  period.dataset["period"] = state.answer.period;
  period.dataset["date"] = state.answer.date;
  if (ui.pending) {
    period.setAttribute("aria-busy", "true");
  }
  const shownAnswer = state.answer;
  if (shownAnswer.energy_flow === null && shownAnswer.buckets.length === 0) {
    period.append(note(doc, translate(language, "hist.house.noMeters"), "no-meters"));
  }
  for (const part of [houseBars(input, shownAnswer), flowSection(input, shownAnswer), moneySection(input, shownAnswer)]) {
    if (part !== null) {
      period.append(part);
    }
  }
  if (ui.detailed) {
    for (const part of [modesSection(input, shownAnswer), forecastSection(input, shownAnswer), daysSection(input, shownAnswer)]) {
      if (part !== null) {
        period.append(part);
      }
    }
  }
  body.append(period);
  body.append(note(doc, translate(language, "hist.house.footnote")));
  return body;
}
