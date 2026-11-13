// The house card's answer (`spotnav/get_house`, API v1), decoded strictly. The backend's shape is
// `custom_components/spotnav/site/house_view.py`; its fixtures are `tests/fixtures/house/`. Extra keys are allowed
// (additive fields); a missing or mistyped one makes the whole answer unreadable, never half-shown.

export const HOUSE_API_VERSION = 1;

export type BatteryAction = "grid_charge" | "export_discharge" | "hold";
const BATTERY_ACTIONS: readonly BatteryAction[] = ["grid_charge", "export_discharge", "hold"];

export interface HouseHour {
  /** The hour's start, an ISO instant with its offset. */
  start: string;
  startMs: number;
  /** The house's measured load (past hours only). */
  house_kw: number | null;
  /** Past: the first forecast made for the hour; from the running hour on: the latest. */
  house_forecast_kw: number | null;
  sun_kwh: number | null;
  sun_forecast_kwh: number | null;
  sun_corrected_kwh: number | null;
  /** The battery's SoC at the hour's end (past hours only). */
  soc_pct: number | null;
  /** The SoC the plan expected (past: the first plan) or expects (ahead) at the hour's end. */
  soc_plan_pct: number | null;
  battery_action: BatteryAction | null;
  /** The hour's mean buy price with every fee and sell price (minor unit per kWh), where the battery plan priced it. */
  buy: number | null;
  sell: number | null;
}

export interface Score {
  hours: number;
  mae: number;
  under_share: number;
  big_under_share: number;
}

export interface ModeFigures {
  planned_saving: number | null;
  reviewed_saving: number | null;
  reviewed_days: number;
}

export const MODES = ["self_consumption", "balanced", "max_profit"] as const;
export type BatteryMode = (typeof MODES)[number];

export interface BatteryView {
  brand: string | null;
  label: string | null;
  soc_pct: number | null;
  usable_kwh: number | null;
  management: string;
  status: string;
  reason: string | null;
  mode: string;
  made_at: string | null;
  horizon_end: string | null;
  modes: Partial<Record<BatteryMode, ModeFigures>>;
  review: {
    days: number;
    first: string;
    last: string;
    plan_saving: number;
    own_saving: number;
    self_consumption_net: number;
  } | null;
  soc_review: Score | null;
  estimates: string[];
}

export interface ReviewDay {
  date: string;
  house_mae: number | null;
  sun_mae: number | null;
  sun_corrected_mae: number | null;
  soc_mae: number | null;
  plan_saving: number | null;
  own_saving: number | null;
}

export interface TodayMoney {
  date: string;
  sold: number;
  arbitrage: number;
  bought: number;
  net: number;
  mode: string;
  /** The battery's stored energy now (kWh) and its average cost (minor unit per kWh); `estimated` while the
   * starting estimate is still in it. */
  stored_kwh: number | null;
  average_cost: number | null;
  estimated: boolean;
}

export interface DayFlow {
  date: string;
  sources: { solar: number | null; battery: number; grid: number };
  uses: { battery: number; load: number; grid: number };
  sun_hours: number;
  hours: number;
}

export interface CalendarDay {
  date: string;
  plan_saving: number;
  own_saving: number;
  mode: string | null;
  arbitrage: number | null;
}

export interface HouseAnswer {
  site_id: string;
  time_zone: string;
  generated_at: string;
  money: { currency: string | null; major_unit: string | null; minor_unit: string | null } | null;
  hours: HouseHour[];
  house: {
    made_at: string | null;
    samples: number;
    temperature: { active: boolean; reason: string } | null;
    basis: string;
    counts_battery: boolean;
    review: Score | null;
  };
  sun: { status: string; review: Score | null; review_corrected: Score | null } | null;
  battery: BatteryView | null;
  today: TodayMoney | null;
  flow: DayFlow | null;
  calendar: { month: string; days: CalendarDay[] } | null;
  days: ReviewDay[];
  sources: {
    weather_entity: string | null;
    weather: string | null;
    outdoor: { source: string | null; sensor: string | null };
    holiday_calendar: string | null;
    solar_forecasts: string[];
  };
}

export type HouseDecodeResult = { ok: true; value: HouseAnswer } | { ok: false; failure: "unsupported" | "malformed" };

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

/** A field an older answer did not have: absent reads as `fallback`, present must be its shape. */
function additive<T>(source: Record<string, unknown>, key: string, read: (value: unknown) => T, fallback: T): T {
  return key in source ? read(source[key]) : fallback;
}

function count(value: unknown): number {
  return Number.isInteger(value) && (value as number) >= 0 ? (value as number) : bad();
}

function flag(value: unknown): boolean {
  return typeof value === "boolean" ? value : bad();
}

const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;

function instant(value: unknown): string {
  const raw = text(value);
  return INSTANT.test(raw) && Number.isFinite(Date.parse(raw)) ? raw : bad();
}

function instantOrNull(value: unknown): string | null {
  return value === null ? null : instant(value);
}

function day(value: unknown): string {
  const raw = text(value);
  return DATE.test(raw) ? raw : bad();
}

function score(value: unknown): Score | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  return {
    hours: count(source.hours),
    mae: number(source.mae),
    under_share: number(source.under_share),
    big_under_share: number(source.big_under_share),
  };
}

function hour(value: unknown): HouseHour {
  const source = record(value);
  const start = instant(source.start);
  const action = source.battery_action;
  return {
    start,
    startMs: Date.parse(start),
    house_kw: numberOrNull(source.house_kw),
    house_forecast_kw: numberOrNull(source.house_forecast_kw),
    sun_kwh: numberOrNull(source.sun_kwh),
    sun_forecast_kwh: numberOrNull(source.sun_forecast_kwh),
    sun_corrected_kwh: numberOrNull(source.sun_corrected_kwh),
    soc_pct: numberOrNull(source.soc_pct),
    soc_plan_pct: numberOrNull(source.soc_plan_pct),
    // An action a newer backend names is drawn as none.
    battery_action: BATTERY_ACTIONS.includes(action as BatteryAction) ? (action as BatteryAction) : action === null || typeof action === "string" ? null : bad(),
    buy: additive(source, "buy", numberOrNull, null),
    sell: additive(source, "sell", numberOrNull, null),
  };
}

function modes(value: unknown): Partial<Record<BatteryMode, ModeFigures>> {
  const source = record(value);
  const out: Partial<Record<BatteryMode, ModeFigures>> = {};
  for (const mode of MODES) {
    if (!(mode in source)) {
      continue;
    }
    const figures = record(source[mode]);
    out[mode] = {
      planned_saving: numberOrNull(figures.planned_saving),
      reviewed_saving: numberOrNull(figures.reviewed_saving),
      reviewed_days: count(figures.reviewed_days),
    };
  }
  return out;
}

function battery(value: unknown): BatteryView | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  const review = source.review === null ? null : record(source.review);
  return {
    brand: textOrNull(source.brand),
    label: textOrNull(source.label),
    soc_pct: numberOrNull(source.soc_pct),
    usable_kwh: numberOrNull(source.usable_kwh),
    management: text(source.management),
    status: text(source.status),
    reason: textOrNull(source.reason),
    mode: text(source.mode),
    made_at: instantOrNull(source.made_at),
    horizon_end: instantOrNull(source.horizon_end),
    modes: modes(source.modes),
    review:
      review === null
        ? null
        : {
            days: count(review.days),
            first: day(review.first),
            last: day(review.last),
            plan_saving: number(review.plan_saving),
            own_saving: number(review.own_saving),
            self_consumption_net: number(review.self_consumption_net),
          },
    soc_review: score(source.soc_review),
    estimates: list(source.estimates).map(text),
  };
}

function reviewDay(value: unknown): ReviewDay {
  const source = record(value);
  return {
    date: day(source.date),
    house_mae: numberOrNull(source.house_mae),
    sun_mae: numberOrNull(source.sun_mae),
    sun_corrected_mae: numberOrNull(source.sun_corrected_mae),
    soc_mae: numberOrNull(source.soc_mae),
    plan_saving: numberOrNull(source.plan_saving),
    own_saving: numberOrNull(source.own_saving),
  };
}

function today(value: unknown): TodayMoney | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  return {
    date: day(source.date),
    sold: number(source.sold),
    arbitrage: number(source.arbitrage),
    bought: number(source.bought),
    net: number(source.net),
    mode: text(source.mode),
    stored_kwh: additive(source, "stored_kwh", numberOrNull, null),
    average_cost: additive(source, "average_cost", numberOrNull, null),
    estimated: additive(source, "estimated", flag, false),
  };
}

function flow(value: unknown): DayFlow | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  const from = record(source.sources);
  const to = record(source.uses);
  const kwh = (raw: unknown): number => (typeof raw === "number" && Number.isFinite(raw) && raw >= 0 ? raw : bad());
  return {
    date: day(source.date),
    sources: { solar: from.solar === null ? null : kwh(from.solar), battery: kwh(from.battery), grid: kwh(from.grid) },
    uses: { battery: kwh(to.battery), load: kwh(to.load), grid: kwh(to.grid) },
    sun_hours: count(source.sun_hours),
    hours: count(source.hours),
  };
}

function calendar(value: unknown): { month: string; days: CalendarDay[] } | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  const month = text(source.month);
  if (!/^\d{4}-\d{2}$/.test(month)) {
    bad();
  }
  return {
    month,
    days: list(source.days).map((entry) => {
      const item = record(entry);
      return {
        date: day(item.date),
        plan_saving: number(item.plan_saving),
        own_saving: number(item.own_saving),
        mode: textOrNull(item.mode),
        arbitrage: additive(item, "arbitrage", numberOrNull, null),
      };
    }),
  };
}

export function decodeHouse(raw: unknown): HouseDecodeResult {
  try {
    const source = record(raw);
    if (source.api_version !== HOUSE_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    const money = source.money === null ? null : record(source.money);
    const house = record(source.house);
    const temperature = house.temperature === null ? null : record(house.temperature);
    const sun = source.sun === null ? null : record(source.sun);
    const sources = record(source.sources);
    const outdoor = record(sources.outdoor);
    const hours = list(source.hours).map(hour);
    if (hours.length === 0) {
      bad();
    }
    return {
      ok: true,
      value: {
        site_id: text(source.site_id),
        time_zone: text(source.time_zone),
        generated_at: instant(source.generated_at),
        money:
          money === null
            ? null
            : {
                currency: textOrNull(money.currency),
                major_unit: textOrNull(money.major_unit),
                minor_unit: textOrNull(money.minor_unit),
              },
        hours,
        house: {
          made_at: instantOrNull(house.made_at),
          samples: count(house.samples),
          temperature: temperature === null ? null : { active: flag(temperature.active), reason: text(temperature.reason) },
          basis: additive(house, "basis", text, "net"),
          counts_battery: flag(house.counts_battery),
          review: score(house.review),
        },
        sun: sun === null ? null : { status: text(sun.status), review: score(sun.review), review_corrected: score(sun.review_corrected) },
        battery: battery(source.battery),
        today: additive(source, "today", today, null),
        flow: additive(source, "flow", flow, null),
        calendar: additive(source, "calendar", calendar, null),
        days: list(source.days).map(reviewDay),
        sources: {
          weather_entity: textOrNull(sources.weather_entity),
          weather: textOrNull(sources.weather),
          outdoor: { source: textOrNull(outdoor.source), sensor: textOrNull(outdoor.sensor) },
          holiday_calendar: textOrNull(sources.holiday_calendar),
          solar_forecasts: list(sources.solar_forecasts).map(text),
        },
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}
