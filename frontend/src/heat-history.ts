// The heat card's history (`spotnav/get_heat_history`): one day, month or year of the house's heating. The decoded
// answer (the card's Plan and Use tabs read it too) and the body of the details dialog behind the "i" in the card's
// header, on the card's own period picker and with every section shown: the heat pump's cost per hour, day or month
// with what SpotNav's plan would have saved hatched on top (where it was evaluated; the plain use is the card's Use
// tab), the use in figures with the immersion heater where its power is read, kWh per degree-day with its monthly
// trend, comfort (the hours under the band), what SpotNav's plan would have saved in shadow, a day's review (the
// relay's real lowering beside SpotNav's plan), how well the house model forecasts the heat, and a day's hot-water
// runs one by one. The day's chart is the card's Plan tab. Every figure is the backend's; text is written with
// `textContent` only.

import { clock, dateLabel, formatFixed, formatNumber, kwhText, moneyMinorAsMajor as money, type FormatContext } from "./format";
import { fits, HEAT_HISTORY_API_VERSION, INDOOR_DAY, REVIEW, roomName, type HeatReview, type IndoorDay } from "./heat-model";
import { bucketWords, isoDay, periodKind } from "./house-history";
import { periodPicker, type PeriodChoice } from "./house-tabs";
import { element } from "./dom";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
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
import { monthLabel } from "./history";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { bad, record, list, text, textOrNull, number, numberOrNull, flag } from "./decode-primitives";

export interface HeatBucket {
  key: string;
  kwh: number | null;
  hot_water_kwh: number | null;
  /** The heat zones' energy in the bucket, already in `kwh`; `null` without a zone counted. */
  zones_kwh: number | null;
  cost: number | null;
  degree_days: number | null;
  outdoor_c: number | null;
  /** What the shadow rule would have saved in the bucket's hours, at most; `null` where it was not evaluated. */
  shadow_value: number | null;
  /** The bucket's energy by what is measured (`HeatHistory.parts`), adding up to `kwh`; none on an older backend. */
  parts: { key: string; kwh: number; cost: number | null }[];
  /** What SpotNav saved on heat in the bucket (a day of a month, a month of a year); `null` without and in a day's hours. */
  saving: number | null;
}

/** A part of the use: the heat pump less its hot water, the hot water, a zone with a sensor of its own, the rest, and
 * the pool, the spa and the sauna as SpotNav counted them (when they ran, at their power). */
export interface HeatPart {
  key: string;
  kind: "pump" | "hot_water" | "zone" | "other" | "pool" | "spa" | "sauna";
  /** A zone's name as the household named it; `null` for the others (the card words them). */
  name: string | null;
  kwh: number;
  cost: number | null;
  /** Some of its energy is an estimate (the pool, the spa or the sauna without its own trusted power sensor): named
   * "(estimated)". `false` from a backend before it. */
  estimated: boolean;
  /** The estimate the house had no room for (left in the house, as the energy flow counts it); 0 when it fit. */
  clamped_kwh: number;
}

const PART_KINDS: readonly string[] = ["pump", "hot_water", "zone", "other", "pool", "spa", "sauna"];

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
  money: { currency: string; major_unit: string | null; minor_unit: string | null } | null;
  meter: { entity_id: string; name: string | null } | null;
  energy: {
    total_kwh: number;
    hours: number;
    hot_water_kwh: number | null;
    split_days: number;
    days: number;
    cost: number | null;
    priced_hours: number;
    /** The heat zones' energy, already in `total_kwh`, and the first day they had any (the use steps up there). */
    zones_kwh: number | null;
    zones_from: string | null;
  } | null;
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
  shadow: {
    days: number;
    lowered_hours: number;
    moved_kwh: number;
    value: number;
    /** Days counted on SpotNav's plan as kept hour by hour (the rest planned again). */
    kept_days: number;
    /** The lowering's learned effect: the share of the heat taken away, the share coming back, the blocks measured. */
    lower_share: number;
    rebound: number;
    blocks: number;
  } | null;
  immersion: { hours: number; kwh: number } | null;
  runs: HeatRun[];
  /** The day's review (a day only): the relay's real lowering beside SpotNav's plan. */
  review: HeatReview | null;
  /** A day's indoor curve by the hour with SpotNav's setpoint per hour; `null` for a month or a year and without
   * indoor sensors. */
  indoor_day: IndoorDay | null;
  /** The period's use by what is measured, in the order shown; none on an older backend. */
  parts: HeatPart[];
  /** What SpotNav saved on heat over the period (the house card's earnings' heat part), `null` without a day of it. */
  saving: { value: number; days: number } | null;
  /** The heat forecast's mean absolute error over the period's reviewed days (kW); `null` without one. */
  model: { mae_kw: number; days: number } | null;
}

export type HeatHistoryDecode = { ok: true; value: HeatHistory } | { ok: false; failure: "unsupported" | "malformed" };

function count(value: unknown): number {
  return Number.isInteger(value) && (value as number) >= 0 ? (value as number) : bad();
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
        money: nullable(source.money, (item) => ({ currency: text(item.currency), major_unit: textOrNull(item.major_unit), minor_unit: textOrNull(item.minor_unit) })),
        meter: nullable(source.meter, (item) => ({ entity_id: text(item.entity_id), name: textOrNull(item.name) })),
        energy: nullable(source.energy, (item) => ({
          total_kwh: number(item.total_kwh),
          hours: count(item.hours),
          hot_water_kwh: numberOrNull(item.hot_water_kwh),
          split_days: count(item.split_days),
          days: count(item.days),
          cost: numberOrNull(item.cost),
          priced_hours: count(item.priced_hours),
          zones_kwh: numberOrNull(item.zones_kwh ?? null),
          zones_from: item.zones_from === undefined || item.zones_from === null ? null : isoDay(item.zones_from),
        })),
        buckets: list(source.buckets).map((raw): HeatBucket => {
          const item = record(raw);
          return {
            key: text(item.key),
            kwh: numberOrNull(item.kwh),
            hot_water_kwh: numberOrNull(item.hot_water_kwh),
            zones_kwh: numberOrNull(item.zones_kwh ?? null),
            cost: numberOrNull(item.cost),
            degree_days: numberOrNull(item.degree_days),
            outdoor_c: numberOrNull(item.outdoor_c),
            shadow_value: numberOrNull(item.shadow_value),
            parts: list(item.parts ?? []).map((rawPart) => {
              const part = record(rawPart);
              return { key: text(part.key), kwh: number(part.kwh), cost: numberOrNull(part.cost) };
            }),
            saving: numberOrNull(item.saving ?? null),
          };
        }),
        parts: list(source.parts ?? []).map((rawPart): HeatPart => {
          const part = record(rawPart);
          const kind = text(part.kind);
          return {
            key: text(part.key),
            kind: PART_KINDS.includes(kind) ? (kind as HeatPart["kind"]) : bad(),
            name: textOrNull(part.name),
            kwh: number(part.kwh),
            cost: numberOrNull(part.cost),
            estimated: part.estimated === undefined ? false : part.estimated === true ? true : part.estimated === false ? false : bad(),
            clamped_kwh: part.clamped_kwh === undefined ? 0 : Math.max(0, number(part.clamped_kwh)),
          };
        }),
        saving: nullable(source.saving ?? null, (item) => ({ value: number(item.value), days: count(item.days) })),
        model: nullable(source.model ?? null, (item) => ({ mae_kw: number(item.mae_kw), days: count(item.days) })),
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
          kept_days: count(item.kept_days),
          lower_share: number(item.lower_share),
          rebound: number(item.rebound),
          blocks: count(item.blocks),
        })),
        immersion: nullable(source.immersion, (item) => ({ hours: count(item.hours), kwh: number(item.kwh) })),
        review: source.review === null ? null : fits(source.review, REVIEW) ? (source.review as HeatReview) : bad(),
        indoor_day: source.indoor_day === null ? null : fits(source.indoor_day, INDOOR_DAY) ? (source.indoor_day as IndoorDay) : bad(),
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

function celsius(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} °C`;
}

/** The cost bars with what SpotNav's plan would have saved hatched on top: drawn only where some bucket has that
 * figure (the bars alone are the card's Use tab). */
function heatBars(input: HeatHistoryInput, answer: HeatHistory): HTMLElement | null {
  const { doc, language, ui } = input;
  if (answer.buckets.length === 0 || answer.buckets.every((item) => item.kwh === null) || answer.buckets.every((item) => item.shadow_value === null || item.cost === null)) {
    return null;
  }
  const priced = answer.buckets.some((item) => item.cost !== null);
  const bars: PeriodBar[] = answer.buckets.map((item, index) => {
    const words = bucketWords(language, answer.period, item.key, index, answer.buckets.length);
    const parts: string[] = [];
    if (item.kwh === null) {
      parts.push(translate(language, "hist.heat.barNone", { when: words.when }));
    } else {
      parts.push(`${words.when}: ${kwhText(language, item.kwh)}`);
      if (item.cost !== null) {
        parts.push(money(language, item.cost, answer.money));
      }
      if (item.shadow_value !== null && item.cost !== null) {
        parts.push(translate(language, "hist.heat.barSaving", { saving: money(language, item.shadow_value, answer.money) }));
      }
      if (item.zones_kwh !== null && item.zones_kwh > 0) {
        parts.push(translate(language, "hist.heat.barZones", { kwh: kwhText(language, item.zones_kwh) }));
      }
      if (item.hot_water_kwh !== null) {
        parts.push(translate(language, "hist.heat.barHotWater", { kwh: kwhText(language, item.hot_water_kwh) }));
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
    cost: translate(language, "hist.legend.cost"),
    saving: translate(language, "hist.heat.legend.saving"),
    extra: "",
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
  block.append(figureRow(doc, translate(language, "hist.heat.total"), kwhText(language, energy.total_kwh), "total"));
  if (energy.zones_kwh !== null) {
    block.append(figureRow(doc, translate(language, "hist.heat.zones"), kwhText(language, energy.zones_kwh), "zones"));
    if (energy.zones_from !== null) {
      block.append(note(doc, translate(language, "hist.heat.zonesFrom", { date: dateLabel(language, energy.zones_from) }), "zones-from"));
    }
  }
  if (energy.hot_water_kwh !== null) {
    block.append(figureRow(doc, translate(language, "hist.heat.hotWater"), kwhText(language, energy.hot_water_kwh), "hot-water"));
    // Said only when some days are missing from the split.
    if (energy.split_days < energy.days) {
      block.append(note(doc, translate(language, "hist.heat.splitDays", { days: String(energy.split_days), total: String(energy.days) }), "split-days"));
    }
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
        translate(language, "hist.heat.immersionFigure", { hours: String(answer.immersion.hours), kwh: kwhText(language, answer.immersion.kwh) }),
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
  if (degree.kwh_per_degree_day === null) {
    // No figure yet: one line on when there will be one, not the arithmetic of none.
    block.append(note(doc, translate(language, "hist.heat.tooFewLine"), "too-few"));
    const trend = trendChart(input, answer);
    if (trend !== null) {
      block.append(element(doc, "h4", C.historyTileHeading, translate(language, "hist.heat.trend")), trend);
    }
    return block;
  }
  block.append(
    figureRow(
      doc,
      translate(language, "hist.heat.perDegreeDay"),
      `${formatFixed(language, degree.kwh_per_degree_day, 2)} ${translate(language, "hist.heat.perDegreeDayUnit")}`,
      "per-degree-day",
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
    // The room on its own row and its figures under it, so neither breaks mid-figure on a phone.
    figureRow(doc, translate(language, "hist.heat.coldest"), roomName(comfort.coldest.name), "coldest"),
    figureRow(
      doc,
      "",
      translate(language, "hist.heat.coldestFigure", { mean: celsius(language, comfort.coldest.mean_c), min: celsius(language, comfort.coldest.min_c) }),
      "coldest-figure",
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
  const percent = (share: number): string => `${formatFixed(language, share * 100, 0)} %`;
  block.append(
    figureRow(doc, translate(language, "hist.heat.shadowValue"), money(language, shadow.value, answer.money), "value"),
    note(
      doc,
      translate(language, "hist.heat.shadowLine", {
        kwh: kwhText(language, shadow.moved_kwh),
        hours: String(shadow.lowered_hours),
        days: String(shadow.days),
      }),
    ),
    note(
      doc,
      translate(language, "hist.heat.shadowNote", {
        kept: String(shadow.kept_days),
        days: String(shadow.days),
        share: percent(shadow.lower_share),
        rebound: percent(shadow.rebound),
      }),
    ),
    note(
      doc,
      shadow.blocks > 0
        ? translate(language, "hist.heat.shadowLearned", { blocks: String(shadow.blocks) })
        : translate(language, "hist.heat.shadowPrior"),
      "effect",
    ),
  );
  return block;
}

/** How well the house model forecasts the heat: its mean error against the meter over the reviewed days. */
function modelSection(input: HeatHistoryInput, answer: HeatHistory): HTMLElement | null {
  if (answer.meter === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.heat.model"), "model");
  const model = answer.model;
  if (model === null) {
    block.append(note(doc, translate(language, "hist.heat.modelNone"), "no-model"));
    return block;
  }
  block.append(
    figureRow(
      doc,
      translate(language, "hist.heat.modelError"),
      `±${formatFixed(language, model.mae_kw, model.mae_kw < 1 ? 2 : 1)} kW (${translate(language, "hist.days", { days: String(model.days) })})`,
      "model-error",
    ),
    note(doc, translate(language, "hist.heat.modelNote")),
  );
  return block;
}

/** The day's review: the relay's real lowering and SpotNav's plan, money and the coldest room, side by side. */

function reviewSection(input: HeatHistoryInput, answer: HeatHistory): HTMLElement | null {
  const review = answer.review;
  if (answer.period !== "day" || review === null) {
    return null;
  }
  const { doc, language } = input;
  const block = section(doc, translate(language, "hist.heat.review"), "review");
  const hours = (value: number): string => `${formatFixed(language, value, Number.isInteger(value) ? 0 : 1)} h`;
  const comfort = (part: { coldest_min_c: number | null; minutes_below: number }): string =>
    part.coldest_min_c === null
      ? translate(language, "hist.heat.reviewNoRooms")
      : translate(language, "hist.heat.reviewComfort", { min: celsius(language, part.coldest_min_c), minutes: String(part.minutes_below) });
  block.append(
    figureRow(
      doc,
      translate(language, "hist.heat.reviewRelay", { hours: hours(review.relay.hours) }),
      money(language, review.relay.value, answer.money),
      "review-relay",
    ),
    figureRow(doc, "", comfort(review.relay), "review-relay-comfort"),
    figureRow(
      doc,
      translate(language, "hist.heat.reviewPlan", { hours: hours(review.plan.hours) }),
      money(language, review.plan.value, answer.money),
      "review-plan",
    ),
    figureRow(doc, "", comfort(review.plan), "review-plan-comfort"),
    note(
      doc,
      translate(language, "hist.heat.reviewNote", { low: celsius(language, review.low_c), overlap: hours(review.plan.overlap_hours) }),
    ),
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
      element(doc, "span", "spotnav-period-figure", `${kwhText(language, run.kwh)} · ${translate(language, run.cause === null ? "hist.heat.causeUnknown" : CAUSE_KEYS[run.cause]!)}`),
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
  const shownAnswer = state.answer;
  const period = element(doc, "div", C.historyMonth);
  period.dataset["period"] = shownAnswer.period;
  period.dataset["date"] = shownAnswer.date;
  if (ui.pending) {
    period.setAttribute("aria-busy", "true");
  }
  for (const part of [
    // Every section is shown (the card itself is small now; nothing is behind a "Show details").
    heatBars(input, shownAnswer),
    useSection(input, shownAnswer),
    degreeSection(input, shownAnswer),
    comfortSection(input, shownAnswer),
    shadowSection(input, shownAnswer),
    reviewSection(input, shownAnswer),
    modelSection(input, shownAnswer),
    runsSection(input, shownAnswer),
  ]) {
    if (part !== null) {
      period.append(part);
    }
  }
  body.append(period);
  body.append(note(doc, translate(language, "hist.heat.footnote")));
  return body;
}
