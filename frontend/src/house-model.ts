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
  /** The heat pump's measured use (past hours) and its forecast (past: the first; ahead: the latest), where its meter
   * is found or set; part of the house's figures, never added to them. `null` from a backend before heat. */
  heat_kw: number | null;
  heat_forecast_kw: number | null;
  /** The cars' measured energy in a past hour (kWh = mean kW; the energy flow's own figures); `null` ahead, where the
   * cars could not be measured and from a backend before it. */
  car_kw: number | null;
}

/** The heat pump's meter and the day's heat (`site/house_view.py`'s `heat` block). */
export interface HeatView {
  /** `found`, `ambiguous` (more than one could be it: the user chooses), `unusable` (the chosen sensor is not a power or
   * energy sensor) or a newer backend's word. */
  status: string;
  entity_id: string | null;
  name: string | null;
  kind: string | null;
  /** `setting`, `energy_dashboard` or `integration`. */
  source: string | null;
  candidates: string[];
  setting_offered: boolean;
  /** The shown day's heat beside the house's whole load over the same completed hours. */
  day: { date: string; hours: number; heat_kwh: number; house_kwh: number } | null;
  review: Score | null;
  /** The house forecast as heat plus the rest against the whole, over the same hours of the last week. */
  comparison: { hours: number; house_mae: number; split_mae: number } | null;
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
    /** The days' money against no battery and what the sun gave (minor unit), summed over the days that have each;
     * `null` from a backend before them. */
    battery_saving: number | null;
    sun_gave: number | null;
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
  battery_saving: number | null;
  sun_gave: number | null;
  against_nothing: number | null;
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

/** A node of today's energy flow: a source (left) or a use (right). `role` is open: a newer backend may name one this
 * card does not know (a car that gives energy back would be `car` on the `source` side), drawn in a neutral colour. */
export interface FlowNode {
  id: string;
  side: "source" | "use";
  role: string;
  kwh: number;
  soc_pct: number | null;
}

export interface FlowLink {
  from: string;
  to: string;
  kwh: number;
}

/** The day so far as a Sankey diagram (`site/energy_flow.py`): by the quarter hour within the recorder's short-term
 * statistics, else by the hour; `hours` counts the periods allocated (0: today's first minutes, nothing yet). */
export interface EnergyFlow {
  date: string;
  through: string;
  hours: number;
  /** The period the flows were allocated by (minutes): 15 or 60 (60 from a backend before quarters). */
  step_min: number;
  car_hours: number;
  /** The hours that had the heat pump meter's figure (0 without a heat node or from a backend before heat). */
  heat_hours: number;
  nodes: FlowNode[];
  links: FlowLink[];
}

/** The day shown: today or a past day asked for by date, how far back one may go, and whether it has data. */
export interface ShownDay {
  date: string;
  today: boolean;
  first: string;
  has_data: boolean;
}

/** A past day's own review: each mode's saving and the battery's own against plain self-consumption. */
export interface DayReviewFigures {
  date: string;
  mode: string | null;
  modes: Partial<Record<BatteryMode, number | null>>;
  own_saving: number;
  /** The day in the battery app's terms (minor unit; `null` where not known): what the battery saved against the day
   * with no battery, what the sun gave, and both against neither. */
  battery_saving: number | null;
  sun_gave: number | null;
  against_nothing: number | null;
  /** How much of the day the figures cover: the quarter hours measured, of the day's own, and those with the sun's
   * production. `day_quarters` is 0 from a backend before them. */
  covered_quarters: number;
  day_quarters: number;
  sun_quarters: number;
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
  energy_flow: EnergyFlow | null;
  /** `null` from a backend before the date choice: today only. */
  day: ShownDay | null;
  /** On a past day, that day's review; `null` on today, on a day without one and from an older backend. */
  day_review: DayReviewFigures | null;
  calendar: { month: string; days: CalendarDay[] } | null;
  days: ReviewDay[];
  sources: {
    weather_entity: string | null;
    weather: string | null;
    outdoor: { source: string | null; sensor: string | null };
    holiday_calendar: string | null;
    solar_forecasts: string[];
  };
  /** `null` without a heat pump meter found, set or ambiguous, and from a backend before heat. */
  heat: HeatView | null;
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
    heat_kw: additive(source, "heat_kw", numberOrNull, null),
    heat_forecast_kw: additive(source, "heat_forecast_kw", numberOrNull, null),
    car_kw: additive(source, "car_kw", numberOrNull, null),
  };
}

function heatView(value: unknown): HeatView | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  const dayRaw = source.day === null ? null : record(source.day);
  const comparison = source.comparison === null ? null : record(source.comparison);
  return {
    status: text(source.status),
    entity_id: textOrNull(source.entity_id),
    name: textOrNull(source.name),
    kind: textOrNull(source.kind),
    source: textOrNull(source.source),
    candidates: list(source.candidates).map(text),
    setting_offered: flag(source.setting_offered),
    day:
      dayRaw === null
        ? null
        : { date: day(dayRaw.date), hours: count(dayRaw.hours), heat_kwh: number(dayRaw.heat_kwh), house_kwh: number(dayRaw.house_kwh) },
    review: score(source.review),
    comparison:
      comparison === null
        ? null
        : { hours: count(comparison.hours), house_mae: number(comparison.house_mae), split_mae: number(comparison.split_mae) },
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
            battery_saving: additive(review, "battery_saving", numberOrNull, null),
            sun_gave: additive(review, "sun_gave", numberOrNull, null),
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
    battery_saving: additive(source, "battery_saving", numberOrNull, null),
    sun_gave: additive(source, "sun_gave", numberOrNull, null),
    against_nothing: additive(source, "against_nothing", numberOrNull, null),
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

function energyFlow(value: unknown): EnergyFlow | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  const kwh = (raw: unknown): number => (typeof raw === "number" && Number.isFinite(raw) && raw >= 0 ? raw : bad());
  const nodes = list(source.nodes).map((entry): FlowNode => {
    const item = record(entry);
    const side = item.side === "source" || item.side === "use" ? item.side : bad();
    return { id: text(item.id), side, role: text(item.role), kwh: kwh(item.kwh), soc_pct: numberOrNull(item.soc_pct) };
  });
  const ids = new Set(nodes.map((node) => node.id));
  const links = list(source.links).map((entry): FlowLink => {
    const item = record(entry);
    return { from: text(item.from), to: text(item.to), kwh: kwh(item.kwh) };
  });
  return {
    date: day(source.date),
    through: instant(source.through),
    hours: count(source.hours),
    step_min: additive(source, "step_min", count, 60),
    car_hours: count(source.car_hours),
    heat_hours: additive(source, "heat_hours", count, 0),
    nodes,
    // A link to a node the answer does not have is not drawn.
    links: links.filter((link) => ids.has(link.from) && ids.has(link.to)),
  };
}

function shownDay(value: unknown): ShownDay | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  return { date: day(source.date), today: flag(source.today), first: day(source.first), has_data: flag(source.has_data) };
}

function dayReview(value: unknown): DayReviewFigures | null {
  if (value === null) {
    return null;
  }
  const source = record(value);
  const raw = record(source.modes);
  const modes: Partial<Record<BatteryMode, number | null>> = {};
  for (const mode of MODES) {
    if (mode in raw) {
      modes[mode] = numberOrNull(raw[mode]);
    }
  }
  return {
    date: day(source.date),
    mode: textOrNull(source.mode),
    modes,
    own_saving: number(source.own_saving),
    battery_saving: additive(source, "battery_saving", numberOrNull, null),
    sun_gave: additive(source, "sun_gave", numberOrNull, null),
    against_nothing: additive(source, "against_nothing", numberOrNull, null),
    covered_quarters: additive(source, "covered_quarters", count, 0),
    day_quarters: additive(source, "day_quarters", count, 0),
    sun_quarters: additive(source, "sun_quarters", count, 0),
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
        energy_flow: additive(source, "energy_flow", energyFlow, null),
        day: additive(source, "day", shownDay, null),
        day_review: additive(source, "day_review", dayReview, null),
        calendar: additive(source, "calendar", calendar, null),
        days: list(source.days).map(reviewDay),
        sources: {
          weather_entity: textOrNull(sources.weather_entity),
          weather: textOrNull(sources.weather),
          outdoor: { source: textOrNull(outdoor.source), sensor: textOrNull(outdoor.sensor) },
          holiday_calendar: textOrNull(sources.holiday_calendar),
          solar_forecasts: list(sources.solar_forecasts).map(text),
        },
        heat: additive(source, "heat", heatView, null),
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}
