// The house card's history (`spotnav/get_house_history`): one day, month or year of the house, behind the "i" in the
// card's header. The decoded answer and the dialog's body: the card's own period picker (`house-tabs.ts`), the bars
// of the period (what the house cost per hour, day or month, with the battery's saving hatched on top: the comparison
// the card's tabs do not have), the money (what the period cost, the battery against no battery, what the sun gave),
// what SpotNav saved as sums (its bars are the card's Earnings tab), the battery's ways against the perfect plan (each
// way's share, saving and work once), the forecasts' errors, each reviewed day, and last how it all is counted. Every
// figure is the period's own and stands once. The energy flow is the card's own tab. Every figure is the backend's;
// text is written with `textContent` only.

import { dateLabel, formatFixed, formatNumber, kwhText, moneyMinorAsMajor as amount } from "./format";
import { decodeEnergyFlow, decodePerfect, HOUSE_HISTORY_API_VERSION, MODES, type EnergyFlow, type PerfectFigures } from "./house-model";
import { hasPerfect, onlyPartial, perfectNotes, perfectRows, perfectSaving, perfectTable, perfectTableNotes, perfectTemperature } from "./perfect-view";
import { periodPicker, type PeriodChoice } from "./house-tabs";
import { element } from "./dom";
import { HOUSE_CLASSES as H } from "./house-styles";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import {
  axisShown,
  figureRow,
  note,
  periodBars,
  periodStart,
  section,
  statusLine,
  type HistoryPeriod,
  type PeriodBar,
  type PeriodHandlers,
  type PeriodUi,
} from "./period-history";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { bad, record, list, text, textOrNull, number, numberOrNull, flag } from "./decode-primitives";

/** The battery's moves in a bucket: the energy given back, what it was worth (buy or sell price), what it cost out of
 * the stock (worth less cost is the arbitrage), and the energy charged with its cost. */
export interface BatteryFlow {
  discharged: number;
  worth: number;
  cost: number;
  charged: number;
  charge_cost: number;
}

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
  /** The battery's money over the bucket's reviewed days (negative: it made it dearer), `null` with none. */
  battery_saving: number | null;
  battery_days: number;
  /** What SpotNav saved over the bucket's days, in all and per part (`null` where none of its days has it; always on
   * a day's hours, and from a backend before it). */
  saving: number | null;
  saving_battery: number | null;
  saving_car: number | null;
  saving_heat: number | null;
  /** What the battery earned in the bucket (minor unit): its arbitrage, the card's headline measure, the same as the
   * battery's own app shows; `null` without a battery, where nothing is kept, and from a backend before it. */
  arbitrage: number | null;
  /** What that arbitrage is made of, for the readout (kWh, minor units); `null` where it is not kept (older days, a
   * month with such a day) and from a backend before it. */
  battery_flow: BatteryFlow | null;
  /** The bucket's mean buy and sell price over its priced periods (minor unit per kWh); `null` unpriced and from a
   * backend before them. */
  buy: number | null;
  sell: number | null;
  /** What the battery earned in the bucket under its own control (not SpotNav's), and what SpotNav's plan would
   * have earned on the same days (`site/spotnav_saving.py`); `null` where SpotNav controlled, where no day is
   * reviewed (the plan's), on a day's hours (the plan's) and from a backend before them. */
  own_battery: number | null;
  would_battery: number | null;
  /** The sun's value over the bucket's reviewed days (minor unit): the same nightly reviews' figure as the period's
   * `review.sun_gave` (or, without a battery, `sun.gave`); `null` on a battery site's day's hours, where no day has
   * it, and from a backend before it. A site without a battery has it on a day's hours too. */
  sun_gave: number | null;
}

/** The sun's value on a site without a battery (`site/sun_value.py`): over `days` of the period's `period_days` days
 * before today, `estimated_days` of them from the hourly statistics. `null` on a battery site (its `review` has it),
 * without the sun and from a backend before it. */
export interface HouseSun {
  gave: number;
  days: number;
  period_days: number;
  estimated_days: number;
}

/** Before SpotNav controls the battery: what the battery's own control earned over the period's such days, and what
 * SpotNav's plan would have earned on them (`null` with none reviewed; known for `would_days` of `days`). */
export interface HouseOwnControl {
  earned: number;
  would: number | null;
  days: number;
  would_days: number;
}

/** What SpotNav saved over the period: each part over the days that have it, with the days counted. */
export interface HouseSaving {
  total: number;
  battery: number | null;
  car: number | null;
  heat: number | null;
  battery_days: number;
  car_days: number;
  heat_days: number;
  days: number;
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
  /** The day's share of its perfect plan: the battery's own and the site's mode; `null` without one. */
  own_share: number | null;
  plan_share: number | null;
  /** Why a day with a perfect plan has no share: under 90 % measured, or too small a difference to compare. */
  share_note: "partial" | "small" | "loss" | null;
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
  /** The period's days against their perfect plans (`site/perfect_view.py`); `null` without one. */
  perfect: PerfectFigures | null;
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
  /** `null` without any part and from a backend before it. */
  saving: HouseSaving | null;
  /** `null` where SpotNav controlled the battery throughout, without a battery and from a backend before it. */
  own_control: HouseOwnControl | null;
  sun: HouseSun | null;
}

export type HouseHistoryDecode = { ok: true; value: HouseHistory } | { ok: false; failure: "unsupported" | "malformed" };

/** A field an older answer did not have: absent reads as `null`, present must be a number or `null`. */
function newNumber(source: Record<string, unknown>, key: string): number | null {
  return key in source ? numberOrNull(source[key]) : null;
}

function savingSums(value: unknown): HouseSaving | null {
  if (value === null || value === undefined) {
    return null;
  }
  const source = record(value);
  return {
    total: number(source.total),
    battery: numberOrNull(source.battery),
    car: numberOrNull(source.car),
    heat: numberOrNull(source.heat),
    battery_days: count(source.battery_days),
    car_days: count(source.car_days),
    heat_days: count(source.heat_days),
    days: count(source.days),
  };
}

function ownControl(value: unknown): HouseOwnControl | null {
  if (value === null || value === undefined) {
    return null;
  }
  const source = record(value);
  return { earned: number(source.earned), would: numberOrNull(source.would), days: count(source.days), would_days: count(source.would_days) };
}

function sunBlock(value: unknown): HouseSun | null {
  if (value === null || value === undefined) {
    return null;
  }
  const source = record(value);
  return { gave: number(source.gave), days: count(source.days), period_days: count(source.period_days), estimated_days: count(source.estimated_days) };
}

function count(value: unknown): number {
  return Number.isInteger(value) && (value as number) >= 0 ? (value as number) : bad();
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;

export function isoDay(value: unknown): string {
  const raw = text(value);
  return DATE.test(raw) ? raw : bad();
}

export function periodKind(value: unknown): HistoryPeriod {
  return value === "day" || value === "month" || value === "year" ? value : bad();
}

function batteryFlow(value: unknown): BatteryFlow | null {
  if (value === null || value === undefined) {
    return null;
  }
  const source = record(value);
  return {
    discharged: number(source.discharged),
    worth: number(source.worth),
    cost: number(source.cost),
    charged: number(source.charged),
    charge_cost: number(source.charge_cost),
  };
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
    battery_saving: numberOrNull(source.battery_saving),
    battery_days: count(source.battery_days),
    saving: newNumber(source, "saving"),
    saving_battery: newNumber(source, "saving_battery"),
    saving_car: newNumber(source, "saving_car"),
    saving_heat: newNumber(source, "saving_heat"),
    arbitrage: newNumber(source, "arbitrage"),
    battery_flow: batteryFlow(source.battery_flow),
    buy: newNumber(source, "buy"),
    sell: newNumber(source, "sell"),
    own_battery: newNumber(source, "own_battery"),
    would_battery: newNumber(source, "would_battery"),
    sun_gave: newNumber(source, "sun_gave"),
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

/** A day's `share_note`: a known reason, else none (a newer backend's word is not shown as a reason). */
function shareNote(value: unknown): HouseReviewDay["share_note"] {
  return value === "partial" || value === "small" || value === "loss" ? value : value === null ? null : (text(value), null);
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
        own_share: "own_share" in item ? numberOrNull(item.own_share) : null,
        plan_share: "plan_share" in item ? numberOrNull(item.plan_share) : null,
        share_note: "share_note" in item ? shareNote(item.share_note) : null,
      };
    }),
    perfect: "perfect" in source ? decodePerfect(source.perfect) : null,
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
        saving: savingSums(source.saving),
        own_control: ownControl(source.own_control),
        sun: sunBlock(source.sun),
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
  /** The first day the car's saving is counted from, where the card knows it. */
  carSince?: string | null;
  /** Where the figures come from and what was assumed (`house-view.ts`), shown last. */
  sources?: HTMLElement | null;
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
  // Without a price in the period there is no money to draw: the energy, in one colour.
  if (answer.buckets.every((item) => item.cost === null)) {
    const bars: PeriodBar[] = answer.buckets.map((item, index) => {
      const words = bucketWords(language, answer.period, item.key, index, answer.buckets.length);
      const figures =
        item.periods === 0
          ? translate(language, "hist.house.barNone", { when: words.when })
          : translate(language, "hist.house.barNoCost", { when: words.when, use: kwhText(language, item.use), bought: kwhText(language, item.grid_in) });
      return { key: item.key, value: item.periods === 0 ? null : item.use, dear: null, words: figures, axis: words.axis };
    });
    return periodBars(doc, language, translate(language, "hist.house.chart"), "kWh", bars, ui, translate(language, "hist.house.hint"));
  }
  const bars: PeriodBar[] = answer.buckets.map((item, index) => {
    const words = bucketWords(language, answer.period, item.key, index, answer.buckets.length);
    let figures: string;
    const saving = item.cost === null ? null : item.battery_saving;
    if (item.periods === 0) {
      figures = translate(language, "hist.house.barNone", { when: words.when });
    } else if (item.cost === null) {
      figures = translate(language, "hist.house.barNoCost", { when: words.when, use: kwhText(language, item.use), bought: kwhText(language, item.grid_in) });
    } else if (saving === null) {
      figures = translate(language, answer.period === "day" ? "hist.house.barHourly" : "hist.house.barUnreviewed", {
        when: words.when,
        cost: amount(language, item.cost, answer.money),
      });
    } else {
      figures = translate(language, saving < 0 ? "hist.house.barWorse" : "hist.house.barMoney", {
        when: words.when,
        cost: amount(language, item.cost, answer.money),
        without: amount(language, item.cost + saving, answer.money),
        saving: amount(language, saving, answer.money),
        extra: amount(language, -saving, answer.money),
      });
      // A year's month may be reviewed on some of its days only: say how many.
      if (answer.period === "year") {
        figures += ` (${translate(language, "hist.days", { days: String(item.battery_days) })})`;
      }
    }
    return {
      key: item.key,
      value: null,
      dear: null,
      words: figures,
      axis: words.axis,
      money: { cost: item.periods === 0 ? null : item.cost, saving },
    };
  });
  return periodBars(doc, language, translate(language, "hist.house.chart"), "", bars, ui, translate(language, "hist.house.hint"), {
    scaleText: (top) => amount(language, top, answer.money),
    cost: translate(language, "hist.legend.cost"),
    saving: translate(language, "hist.house.legend.saving"),
    extra: translate(language, "hist.house.legend.extra"),
  });
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
          kwhIn: kwhText(language, spent.kwh_in),
          sold: amount(language, spent.sold, answer.money),
          kwhOut: kwhText(language, spent.kwh_out),
        }),
      ),
    );
    if (spent.priced < spent.periods) {
      block.append(note(doc, translate(language, "hist.house.costPartial", { share: share(language, spent.priced, spent.periods) }), "cost-partial"));
    }
  }
  const reviewed = answer.review;
  const sun = answer.sun;
  if (reviewed === null && sun !== null) {
    // A site without a battery: the sun's value of its own, over the days valued.
    const figure = amount(language, sun.gave, answer.money);
    block.append(
      figureRow(
        doc,
        translate(language, "hist.house.saved.sun"),
        sun.days < sun.period_days ? translate(language, `hist.house.savedDays.${pluralForm(language, sun.days)}`, { figure, days: String(sun.days) }) : figure,
        "sun",
      ),
    );
    if (sun.estimated_days > 0) {
      block.append(note(doc, translate(language, "house.sun.estimated"), "sun-estimated"));
    }
    return block;
  }
  if (reviewed === null) {
    block.append(note(doc, translate(language, "hist.house.noReview"), "no-review"));
    return block;
  }
  // What each saved, and over how many days the saving is measured: one count in the note, and a row's own only where
  // it has fewer days than that.
  const rows = (
    [
      ["battery", reviewed.battery_saving, reviewed.battery_days],
      ["sun", reviewed.sun_gave, reviewed.sun_days],
      ["both", reviewed.against_nothing, reviewed.nothing_days],
    ] as const
  ).filter(([, value]) => value !== null);
  const measured = Math.max(0, ...rows.map(([, , days]) => days));
  for (const [key, value, days] of rows) {
    const figure = amount(language, value!, answer.money);
    block.append(
      figureRow(
        doc,
        translate(language, `hist.house.saved.${key}`),
        days < measured ? translate(language, `hist.house.savedDays.${pluralForm(language, days)}`, { figure, days: String(days) }) : figure,
        key,
      ),
    );
  }
  if (rows.length > 0) {
    block.append(note(doc, translate(language, "hist.house.baselinesNote")));
  }
  block.append(
    note(
      doc,
      translate(language, "hist.house.reviewed", { days: String(rows.length > 0 ? measured : reviewed.days), total: String(Math.max(reviewed.days, reviewed.period_days)) }),
      "reviewed",
    ),
  );
  if (reviewed.archived > 0) {
    block.append(note(doc, translate(language, "hist.house.archived", { days: String(reviewed.archived) })));
  }
  return block;
}

/** What SpotNav saved over the period: the total and a row per part the period has, each with its days where fewer
 * than the period's counted days; the heat said to be an estimate. `null` without any part. */
function spotnavSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement | null {
  const saving = answer.saving;
  if (saving === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.house.spotnav"), "spotnav-saving");
  block.append(figureRow(doc, translate(language, "hist.house.spotnav.total"), amount(language, saving.total, answer.money), "total"));
  const rows = (
    [
      ["battery", saving.battery, saving.battery_days],
      ["car", saving.car, saving.car_days],
      ["heat", saving.heat, saving.heat_days],
    ] as const
  ).filter(([, value]) => value !== null);
  for (const [key, value, days] of rows) {
    const figure = amount(language, value!, answer.money);
    block.append(
      figureRow(
        doc,
        translate(language, `hist.house.spotnav.${key}`),
        days < saving.days ? translate(language, `hist.house.savedDays.${pluralForm(language, days)}`, { figure, days: String(days) }) : figure,
        key,
      ),
    );
  }
  block.append(note(doc, translate(language, "hist.house.spotnav.note")));
  return block;
}

/** Before the first whole day with a perfect plan: SpotNav's modes and the battery's own way against plain
 * self-consumption, summed over the reviewed days. */
function modesSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement | null {
  const reviewed = answer.review;
  if (reviewed === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.house.modes"), "modes");
  // Self-consumption is what the others are measured against, so it is not a row of its own.
  for (const mode of MODES.filter((item) => item !== "self_consumption")) {
    const figures = reviewed.modes[mode];
    let figure = translate(language, "house.compare.none");
    if (figures !== undefined && figures.saving !== null) {
      figure = amount(language, figures.saving, answer.money);
      if (figures.days < reviewed.days) {
        figure += ` (${translate(language, "hist.days", { days: String(figures.days) })})`;
      }
    }
    const name =
      translate(language, "house.compare.spotnav", { mode: translate(language, `house.mode.${mode}`) }) +
      (mode === reviewed.mode ? ` · ${translate(language, "house.compare.inForce")}` : "");
    block.append(figureRow(doc, name, figure, mode, figures?.saving == null));
  }
  block.append(figureRow(doc, translate(language, "house.compare.own"), amount(language, reviewed.own_saving, answer.money), "own"));
  block.append(note(doc, translate(language, "hist.house.modesNote")));
  const perfect = reviewed.perfect;
  if (perfect !== null) {
    // Only incomplete days so far: say so, and the cells' temperature where the battery reports it.
    const ownName = translate(language, "house.compare.own");
    const notes = hasPerfect(perfect) ? perfectNotes(doc, language, perfect, ownName, answer.period === "day") : [];
    const temperature = perfectTemperature(doc, language, perfect);
    block.append(...notes, ...(temperature === null ? [] : [temperature]));
  }
  return block;
}

/** The battery's ways over the period, each figure once: the share of the perfect plan as the headline and a bar per
 * way, then one table of what each way saved against no battery and the battery's work. Summed over the period, the
 * day boundary washes out; a single day says it counted what was left at midnight. `null` before the first whole day
 * with a perfect plan (`modesSection` stands in). */
function perfectSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement | null {
  const reviewed = answer.review;
  const perfect = reviewed?.perfect ?? null;
  if (reviewed === null || !hasPerfect(perfect) || onlyPartial(perfect)) {
    return null;
  }
  const { doc, language } = input;
  const ownName = translate(language, "house.compare.own");
  const block = section(doc, translate(language, "perfect.headingPeriod"), "perfect");
  // No headline: the rows below give every way's share, and repeating two of them above was noise.
  block.append(perfectRows(doc, language, perfect, ownName, reviewed.mode, answer.money));
  // The figures first, then every word about them together.
  const table = perfectTable(doc, language, perfect, answer.money, ownName);
  if (table !== null) {
    block.append(table);
  }
  block.append(...perfectNotes(doc, language, perfect, ownName, answer.period === "day"));
  if (table !== null) {
    block.append(...perfectTableNotes(doc, language, answer.money));
  }
  // The same comparison stands under Money with another sum: say why, with both figures.
  const own = perfectSaving(language, perfect, "own", answer.money);
  if (own !== null && reviewed.battery_saving !== null) {
    block.append(
      note(
        doc,
        translate(language, "hist.house.perfectDiffers", {
          name: ownName,
          own,
          battery: amount(language, reviewed.battery_saving, answer.money),
          days: String(perfect.days),
          batteryDays: String(reviewed.battery_days),
        }),
        "perfect-differs",
      ),
    );
  }
  const temperature = perfectTemperature(doc, language, perfect);
  if (temperature !== null) {
    block.append(temperature);
  }
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
  // A table, as the house card's own "Day by day": a figure never breaks over two lines on a phone.
  const table = element(doc, "table", H.table);
  const headRow = element(doc, "tr");
  const shares = reviewed.by_day.some((day) => day.own_share !== null);
  for (const key of ["house.days.date", "hist.house.col.battery", "hist.house.col.plan", "hist.house.col.own"] as const) {
    headRow.append(element(doc, "th", undefined, translate(language, key)));
  }
  if (shares) {
    headRow.append(element(doc, "th", undefined, translate(language, "hist.house.col.share")));
  }
  const head = element(doc, "thead");
  head.append(headRow);
  const rows = element(doc, "tbody");
  for (const day of reviewed.by_day) {
    const row = element(doc, "tr");
    row.dataset["date"] = day.date;
    row.append(element(doc, "td", undefined, dateLabel(language, day.date)));
    for (const value of [day.battery_saving, day.plan_saving, day.own_saving]) {
      row.append(element(doc, "td", undefined, value === null ? "–" : amount(language, value, answer.money)));
    }
    if (shares) {
      // A day the battery's own way lost money says "loss", not "0 %".
      const cell =
        day.share_note === "loss"
          ? translate(language, "hist.house.share.loss")
          : day.own_share !== null
            ? `${formatNumber(language, day.own_share, 0)} %`
            : day.share_note === null
              ? "–"
              : translate(language, `hist.house.share.${day.share_note}`);
      row.append(element(doc, "td", day.own_share === null && day.share_note !== null ? C.muted : undefined, cell));
    }
    rows.append(row);
  }
  table.append(head, rows);
  block.append(table, note(doc, translate(language, "hist.house.daysNote")));
  return block;
}

/** Last: how the figures above are counted. What SpotNav's saving is compared with (a line per part the period has),
 * how the energy flow is allocated, and where the figures come from. */
function howSection(input: HouseHistoryInput, answer: HouseHistory): HTMLElement {
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.house.how"), "how");
  block.append(note(doc, translate(language, "hist.house.footnote")));
  const saving = answer.saving;
  if (saving !== null || answer.own_control !== null) {
    const lines: string[] = [];
    if (saving !== null && saving.battery !== null) {
      lines.push(translate(language, "house.saving.details.battery"));
    } else if (answer.own_control !== null) {
      lines.push(translate(language, "house.saving.details.batteryOwn"));
    }
    if (saving !== null && saving.car !== null) {
      // The sentence ends in its own full stop: an abbreviated month's ("10 nov.") is not doubled.
      const since = input.carSince == null ? "" : ` ${translate(language, "house.saving.details.carSince", { date: dateLabel(language, input.carSince).replace(/\.$/, "") })}`;
      lines.push(translate(language, "house.saving.details.car") + since);
    }
    if (saving !== null && saving.heat !== null) {
      lines.push(translate(language, "house.saving.details.heat"));
    }
    if (lines.length > 0) {
      block.append(note(doc, translate(language, "house.saving.details.head"), "saving-compared"), ...lines.map((line) => note(doc, line)));
    }
  }
  const flow = answer.energy_flow;
  if (flow !== null) {
    block.append(note(doc, translate(language, flow.step_min < 60 ? "house.flow.ruleQuarter" : "house.flow.rule"), "flow-rule"));
    if (flow.car_hours < flow.hours && flow.nodes.some((node) => node.role === "car")) {
      block.append(note(doc, translate(language, "house.flow.carPart")));
    }
    if (flow.nodes.some((node) => node.role === "heat")) {
      block.append(note(doc, translate(language, flow.heat_hours < flow.hours ? "house.flow.heatPart" : "house.flow.heatRule")));
    }
  }
  if (input.sources != null) {
    block.append(input.sources);
  }
  return block;
}

/** The dialog's body for one state; built fresh each time. */
export function houseHistoryBody(input: HouseHistoryInput): HTMLElement {
  const { doc, language, state, ui } = input;
  const body = element(doc, "div", C.historyBody);
  const answer = state.kind === "ready" ? state.answer : state.kind === "failed" ? (state.answer ?? null) : null;
  const today = answer?.today ?? input.today;
  const shown = answer !== null && ui.date === null && answer.period === ui.period ? answer.date : ui.date ?? today;
  // The card's own period picker (`house-tabs.ts`), so a period is chosen the same way in both places.
  const first = shown.length === 10 ? periodStart(ui.period, shown) : shown;
  const choice: PeriodChoice = { period: ui.period, date: first >= periodStart(ui.period, today) ? null : first };
  body.append(periodPicker(doc, language, choice, today, (next) => input.handlers.onPeriod(next.period, next.date)));
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
  // Every section is shown (the card itself is small now; nothing is behind a "Show details"), each figure once and
  // for the period chosen. The energy flow is the card's own tab, and SpotNav's saving per part is drawn in its
  // Earnings tab; here its sums stand as rows.
  for (const part of [
    houseBars(input, shownAnswer),
    perfectSection(input, shownAnswer) ?? modesSection(input, shownAnswer),
    moneySection(input, shownAnswer),
    spotnavSection(input, shownAnswer),
    forecastSection(input, shownAnswer),
    daysSection(input, shownAnswer),
    howSection(input, shownAnswer),
  ]) {
    if (part !== null) {
      period.append(part);
    }
  }
  body.append(period);
  return body;
}
