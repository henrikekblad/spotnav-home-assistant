// The house card's Now tab (SpotNav 3.0): a headline row of what is happening now, the live flows as a drawing, one
// sentence of what the battery's plan does next, and the day's money. The approved design is
// `plans/now_flow_mockup_2026-11-27.html` (2026-11-27).
//
// * The headline: "% egen el" (the share of what the site uses now that is not bought; hidden without sun and battery),
//   the price now in its band's colour (the cheapest third, the middle, the dearest third of the plan's hours), and one
//   sentence derived from the flows (`whyKey`): the battery and the sun covering the house in an expensive hour, a
//   cheap hour charging the battery or the car, the sun's surplus sold, the house taking everything from the grid. The
//   house's own kW is in the drawing, never repeated here.
// * The drawing: only the nodes the site has, around the house. One or two outer nodes stand in one row beside the
//   house (left, right) with horizontal lines; three or four at fixed corners (the sun top left, the grid top right,
//   the battery bottom left, the car bottom right) with diagonal lines, a missing corner left empty (`flowLayout`).
//   Each line is as wide as the square root of its power (`lineWidth`), ends at its nodes' outer edge (never under
//   the battery's or the house's ring), and carries white chevrons moving from source to target, faster with more
//   power (`chevronSpeed`); they stand still under `prefers-reduced-motion` and still show the way, and a line too
//   thin for them has none. A line without power is a thin dashed idle line. The kW sits beside its line
//   (`labelPlace`): half the line's width and 10 px off it, above a horizontal line, on the outer side of a diagonal,
//   with a halo in the card's colour.
// * The house: the whole house's kW in the middle, and a ring of its parts (the heat, the pool with the spa and the
//   sauna, the rest), so the lines in add up to the figure in the middle and the ring's parts to it too: the backend
//   balances the figures (`site/house_now.py`); the card rounds the lines to tenths by the largest remainder
//   (`balancedTenths`), then the parts to the house's tenths (`nowFigures`). A part the backend marks as estimated
//   (`estimated`) has "≈" before its figure in the ring's legend, and so has the middle, the balance of them.
// * The battery has its level as a ring around it and its icon filled to it; the car its level when known.
// * Two caption lines at each node: its state now, and today's kWh in its colour (the day's energy flow,
//   `site/energy_flow.py`): the sun's, bought and sold at the grid, in and out at the battery, charged at the car,
//   and the house's day (with its heat, without the car) under its ring. A caption near an edge is anchored inward.
// * Depth, subtle: a soft shadow under the nodes, rings and lines (filters in user space over the whole drawing, so a
//   horizontal line is never clipped), a light from the top left on the nodes, and a faint light stripe along lines.
// * The colours are the energy palette's (`energy-palette.ts`).
// * The live figures are `spotnav/get_house_now`'s, read every 30 s while the tab is shown, and patched into the
//   drawn tab (`patchNow`): the same drawing has its figures, widths and speeds changed in place, so the chevrons
//   never jump back; it is drawn anew only when a line turns or a part comes or goes. Without them (an older backend,
//   or unread) the lines are idle and the captions have the day's figures.
// * The plan's sentence: from the battery plan's hours (`HouseHour.battery_action`), what the battery does now and
//   until when, then what comes next; while SpotNav does not control the battery, who does and what SpotNav would do.

import { clock, dayCue, formatFixed, formatNumber, kwhText, type FormatContext } from "./format";
import { element } from "./dom";
import { HOUSE_NOW_API_VERSION, ownOptimiser, type EnergyFlow, type HouseAnswer } from "./house-model";
import { translate, type Language, type TranslationKey } from "./i18n";
import { record, text, numberOrNull, flag, textOrNull } from "./decode-primitives";

const SVG_NS = "http://www.w3.org/2000/svg";
const HOUR_MS = 3_600_000;
/** Power below this (kW, either way) is drawn as no flow. */
export const STILL_KW = 0.05;

export interface HouseNow {
  site_id: string;
  at: string;
  grid_kw: number | null;
  sun_kw: number | null;
  sun_from: string | null;
  battery_kw: number | null;
  /** The battery on the AC side, as the review and the plan count it (`site/house_now.py`); not drawn (the line is
   * `battery_kw`, see the module); `battery_kw` from a backend before it. */
  battery_ac_kw: number | null;
  soc_pct: number | null;
  car_kw: number | null;
  /** Whether a car is plugged in (`null` without chargers, unread, or from a backend before it), and its level. */
  car_connected: boolean | null;
  car_soc_pct: number | null;
  heat_kw: number | null;
  heat_from: string | null;
  house_kw: number | null;
  /** How the house is had: `plant` (the plant's own load) or `balance`; `null` unread or from a backend before it. */
  house_from: string | null;
  /** The house less its heat, the house's disc (`house_kw` less `heat_kw` from a backend before it). */
  rest_kw: number | null;
  /** The roles whose figure is estimated (`site/house_now.py`); none from a backend before it. */
  estimated: string[];
  /** The pool's, the spa's and the sauna's draw now (`null` without them or from a backend before it), and the house
   * less its heat and pool (`rest_kw` from a backend before it). */
  pool_kw: number | null;
  other_kw: number | null;
  has: { sun: boolean; battery: boolean; car: boolean; heat: boolean; pool: boolean };
}

function stringList(raw: unknown): string[] {
  if (!Array.isArray(raw)) {
    throw new Error("not a list");
  }
  return raw.map((item) => text(item));
}

export type HouseNowDecode = { ok: true; value: HouseNow } | { ok: false; failure: "unsupported" | "malformed" };

/** The answer, strictly typed; extra keys are allowed (additive fields), a missing or mistyped one is not. */
export function decodeHouseNow(raw: unknown): HouseNowDecode {
  try {
    const source = record(raw);
    if (source.api_version !== HOUSE_NOW_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    const has = record(source.has);
    const value = {
      site_id: text(source.site_id),
      at: text(source.at),
      grid_kw: numberOrNull(source.grid_kw),
      sun_kw: numberOrNull(source.sun_kw),
      sun_from: textOrNull(source.sun_from),
      battery_kw: numberOrNull(source.battery_kw),
      battery_ac_kw: source.battery_ac_kw === undefined ? numberOrNull(source.battery_kw) : numberOrNull(source.battery_ac_kw),
      soc_pct: numberOrNull(source.soc_pct),
      car_kw: numberOrNull(source.car_kw),
      car_connected: source.car_connected === undefined || source.car_connected === null ? null : flag(source.car_connected),
      car_soc_pct: source.car_soc_pct === undefined ? null : numberOrNull(source.car_soc_pct),
      heat_kw: numberOrNull(source.heat_kw),
      heat_from: textOrNull(source.heat_from),
      house_kw: numberOrNull(source.house_kw),
      house_from: source.house_from === undefined ? null : textOrNull(source.house_from),
      rest_kw:
        source.rest_kw === undefined
          ? restOf(numberOrNull(source.house_kw), numberOrNull(source.heat_kw))
          : numberOrNull(source.rest_kw),
      estimated: source.estimated === undefined ? [] : stringList(source.estimated),
      pool_kw: source.pool_kw === undefined ? null : numberOrNull(source.pool_kw),
      other_kw: source.other_kw === undefined ? null : numberOrNull(source.other_kw),
      has: { sun: flag(has.sun), battery: flag(has.battery), car: flag(has.car), heat: flag(has.heat), pool: has.pool === undefined ? false : flag(has.pool) },
    };
    return { ok: true, value: source.other_kw === undefined ? { ...value, other_kw: value.rest_kw } : value };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

function restOf(house: number | null, heat: number | null): number | null {
  return house === null ? null : Math.max(0, house - (heat ?? 0));
}

// ------------------------------------------------------------------------------------------------ the sentence

/** What the battery does in an hour of its plan: an action, or its own self-consumption (`self`). */
export type PlanAct = "grid_charge" | "hold" | "export_discharge" | "self";

export interface PlanRun {
  act: PlanAct;
  startMs: number;
}

/** The plan's runs from the running hour on: each change of what the battery does, until the plan's last hour. */
export function planRuns(answer: HouseAnswer, nowMs: number): PlanRun[] {
  const runs: PlanRun[] = [];
  for (const hour of answer.hours) {
    if (hour.startMs + HOUR_MS <= nowMs) {
      continue;
    }
    if (hour.soc_plan_pct === null && hour.battery_action === null) {
      // The plan ends here.
      if (runs.length > 0) {
        break;
      }
      continue;
    }
    const act: PlanAct = hour.battery_action ?? "self";
    if (runs.length === 0 || runs[runs.length - 1]!.act !== act) {
      runs.push({ act, startMs: hour.startMs });
    }
  }
  return runs;
}

function when(ctx: FormatContext, nowMs: number, instantMs: number): string {
  const same = dayCue(ctx, nowMs) === dayCue(ctx, instantMs);
  return same ? clock(ctx, instantMs) : `${dayCue(ctx, instantMs)} ${clock(ctx, instantMs)}`;
}

/** The Now tab's sentence (see the module); `null` without a battery. */
export function planSentence(language: Language, answer: HouseAnswer, nowMs: number): string | null {
  const battery = answer.battery;
  if (battery === null) {
    return null;
  }
  if (battery.status === "waiting" || battery.status === "no_plan") {
    return translate(language, battery.status === "waiting" ? "house.saving.waiting" : "house.saving.noPlan");
  }
  const runs = planRuns(answer, nowMs);
  const controlling = battery.control === "on";
  const verb = controlling ? "act" : "inf";
  const who =
    battery.management === "self_managed"
      ? translate(language, "house.plan.who.own", { name: ownOptimiser(battery.brand) ?? translate(language, "house.compare.own") })
      : translate(language, "house.plan.who.test");
  if (runs.length === 0) {
    return controlling ? null : who;
  }
  const ctx: FormatContext = { language, timeZone: answer.time_zone, unit: "kWh", currency: null, majorUnit: null };
  const first = translate(language, `house.plan.${verb}.${runs[0]!.act}` as TranslationKey);
  const next = runs[1];
  if (next === undefined) {
    return controlling ? translate(language, "house.plan.whole", { act: first }) : translate(language, "house.plan.wouldWhole", { who, act: first });
  }
  // Controlled, the second clause has its own subject ("sedan säljer det till nätet").
  const then = translate(language, `house.plan.${controlling ? "then" : "inf"}.${next.act}` as TranslationKey);
  const values = { act: first, time: when(ctx, nowMs, next.startMs), then };
  return controlling ? translate(language, "house.plan.next", values) : translate(language, "house.plan.wouldNext", { who, ...values });
}

// ------------------------------------------------------------------------------------------------ the figures

/**
 * Whole tenths for `values` (kW, never negative) that add up to `total` tenths: each rounded down, and the tenths
 * left given to the largest remainders (ties to the earlier). The written figures so add up as the backend's do.
 */
export function largestRemainder(values: number[], total: number): number[] {
  const scaled = values.map((value) => Math.max(0, value) * 10);
  const floors = scaled.map((value) => Math.floor(value + 1e-9));
  let left = total - floors.reduce((sum, value) => sum + value, 0);
  // Remainders compared to a millionth, so a float's last bit never breaks a tie.
  const order = scaled.map((value, index) => ({ index, rest: Math.round((value - floors[index]!) * 1e6) })).sort((a, b) => b.rest - a.rest || a.index - b.index);
  for (let step = 0; left > 0 && order.length > 0; step += 1) {
    floors[order[step % order.length]!.index]! += 1;
    left -= 1;
  }
  for (let step = order.length - 1; left < 0 && step >= 0; step -= 1) {
    const index = order[step]!.index;
    if (floors[index]! > 0) {
      floors[index]! -= 1;
      left += 1;
    }
  }
  return floors;
}

/** A node around the house. */
export type NodeRole = "sun" | "grid" | "battery" | "car";
/** The house's parts in its ring, in this order. */
export type PartRole = "heat" | "pool" | "other";
/** What a line or a part is drawn in: the energy palette's meanings (`energy-palette.ts`). */
export type Tone = "sun" | "import" | "export" | "battery" | "car" | "house" | PartRole;

/** One flow at the house: into it (`in`) or out of it, with its size (kW). */
interface Flow {
  key: NodeRole | "house";
  kw: number;
  in: boolean;
}

/**
 * The live figures in whole tenths that balance at the house as written (see the module): the flows in rounded to
 * their sum's tenths, the flows out (the house among them) to the same.
 */
export function balancedTenths(flows: Flow[]): Map<NodeRole | "house", number> {
  const inflows = flows.filter((flow) => flow.in);
  const outflows = flows.filter((flow) => !flow.in);
  const sumIn = inflows.reduce((sum, flow) => sum + flow.kw, 0);
  const sumOut = outflows.reduce((sum, flow) => sum + flow.kw, 0);
  // The backend's figures balance to the hundredth; a backend before it may not: each side then rounds alone.
  const meets = Math.abs(sumIn - sumOut) < 0.011;
  const total = (sum: number) => Math.round(sum * 10 + 1e-9);
  const shown = new Map<NodeRole | "house", number>();
  const place = (side: Flow[], tenths: number) => largestRemainder(side.map((flow) => flow.kw), tenths).forEach((value, index) => shown.set(side[index]!.key, value));
  place(inflows, total(sumIn));
  place(outflows, meets ? total(sumIn) : total(sumOut));
  return shown;
}

/** Whether a car is plugged in: the live answer's word; unknown without it. */
export function carPlugged(live: HouseNow | null): boolean {
  return live?.car_connected === true;
}

/** Which nodes the site has: the live answer's word, else what the day's energy flow and the answer show. */
export function present(live: HouseNow | null, answer: HouseAnswer): { sun: boolean; battery: boolean; car: boolean; heat: boolean; pool: boolean } {
  if (live !== null) {
    return live.has;
  }
  const roles = new Set((answer.energy_flow?.nodes ?? []).map((node) => node.role));
  return {
    sun: answer.sun !== null || roles.has("solar"),
    battery: answer.battery !== null,
    car: roles.has("car"),
    heat: answer.heat?.status === "found" || roles.has("heat"),
    pool: false,
  };
}

export interface NowLine {
  role: NodeRole;
  /** The power on the line as written (kW, never negative), `null` unread. */
  kw: number | null;
  /** Towards the house (`in`), away from it (`out`), or still (no power, or unread). */
  way: "in" | "out" | "still";
  tone: Tone;
  /** The figure is estimated (`HouseNow.estimated`): "≈" before it. */
  estimated: boolean;
}

export interface NowPart {
  role: PartRole;
  kw: number;
  estimated: boolean;
}

export interface NowFigures {
  lines: NowLine[];
  /** The whole house as written, `null` unread. */
  house: { kw: number | null; estimated: boolean };
  /** The house's parts as written, adding up to the house (an empty list without its figure). */
  parts: NowPart[];
}

function toneOf(role: NodeRole, way: NowLine["way"]): Tone {
  return role === "grid" ? (way === "out" ? "export" : "import") : role;
}

/** The lines, the house and its parts as written (see the module): every node the site has has a line. */
export function nowFigures(live: HouseNow | null, answer: HouseAnswer): NowFigures {
  const has = present(live, answer);
  const estimated = new Set(live?.estimated ?? []);
  // Each role's signed reading and the sign that flows into the house.
  const readings: [NodeRole, number | null, boolean][] = [];
  if (has.sun) {
    readings.push(["sun", live?.sun_kw ?? null, true]);
  }
  readings.push(["grid", live?.grid_kw ?? null, true]);
  if (has.battery) {
    // + charging: away from the house.
    readings.push(["battery", live?.battery_kw ?? answer.battery?.now?.power_kw ?? null, false]);
  }
  if (has.car) {
    // No car plugged in draws nothing, read or not (its charger may not be read yet after a restart).
    readings.push(["car", live?.car_kw ?? (live?.car_connected === false ? 0 : null), false]);
  }
  const inward = new Map<NodeRole, boolean>(readings.map(([role, signed, inPositive]) => [role, signed !== null && signed !== 0 && (signed > 0) === inPositive]));
  const raw: NowLine[] = readings.map(([role, signed]) => {
    if (signed === null) {
      return { role, kw: null, way: "still", tone: toneOf(role, "still"), estimated: false };
    }
    const kw = Math.abs(signed);
    const way = kw < STILL_KW ? "still" : inward.get(role) === true ? "in" : "out";
    return { role, kw, way, tone: toneOf(role, way), estimated: estimated.has(role) };
  });
  // The house from the lines where the answer has none but every line is read (a reading missing just after a
  // restart): what comes in less what goes out, never below 0.
  const signedIn = raw.reduce<number | null>((sum, line) => (sum === null || line.kw === null ? null : sum + (inward.get(line.role) === true ? line.kw : -line.kw)), 0);
  const houseKw = live === null ? null : (live.house_kw ?? (signedIn === null ? null : Math.round(Math.max(0, signedIn) * 100) / 100));
  const anyEstimated = estimated.size > 0;
  const partsRaw: NowPart[] = [];
  if (live !== null && houseKw !== null) {
    if (has.heat && live.heat_kw !== null) {
      partsRaw.push({ role: "heat", kw: live.heat_kw, estimated: estimated.has("heat") });
    }
    if (has.pool && live.pool_kw !== null) {
      partsRaw.push({ role: "pool", kw: live.pool_kw, estimated: estimated.has("pool") });
    }
    if (live.house_kw === null) {
      // Parts never larger than the house worked out from the lines.
      let room = houseKw;
      for (const part of partsRaw) {
        part.kw = Math.min(part.kw, room);
        room -= part.kw;
      }
    }
    const other = (live.house_kw === null ? null : live.other_kw) ?? Math.max(0, houseKw - partsRaw.reduce((sum, part) => sum + part.kw, 0));
    partsRaw.push({ role: "other", kw: other, estimated: anyEstimated });
  }
  if (live === null || houseKw === null || raw.some((line) => line.kw === null)) {
    const tenth = (kw: number) => Math.round(kw * 10) / 10;
    return {
      lines: raw.map((line) => (line.kw === null ? line : { ...line, kw: tenth(line.kw) })),
      house: { kw: houseKw === null ? null : tenth(houseKw), estimated: houseKw !== null && anyEstimated },
      parts: partsRaw.map((part) => ({ ...part, kw: tenth(part.kw) })),
    };
  }
  const flows: Flow[] = raw.map((line) => ({ key: line.role, kw: line.kw!, in: inward.get(line.role) === true }));
  flows.push({ key: "house", kw: houseKw, in: false });
  const tenths = balancedTenths(flows);
  const lines = raw.map((line): NowLine => {
    const kw = tenths.get(line.role)! / 10;
    // The way as written: still at 0.0 kW, else by the sign (a still figure given a tenth shows it).
    const way = kw === 0 ? "still" : inward.get(line.role) === true ? "in" : "out";
    return { ...line, kw, way, tone: toneOf(line.role, way) };
  });
  const houseTenths = tenths.get("house")!;
  const partTenths = largestRemainder(partsRaw.map((part) => part.kw), houseTenths);
  return {
    lines,
    house: { kw: houseTenths / 10, estimated: anyEstimated },
    parts: partsRaw.map((part, index) => ({ ...part, kw: partTenths[index]! / 10 })),
  };
}

// ------------------------------------------------------------------------------------------------ the headline

export type PriceBand = "cheap" | "mid" | "dear";

/** The running hour's buy price (minor unit per kWh) and its band among the plan's priced hours (the cheapest third,
 * the middle, the dearest third); `null` without a price now. */
export function priceNow(answer: HouseAnswer, nowMs: number): { minor: number; band: PriceBand } | null {
  const running = answer.hours.find((hour) => hour.startMs <= nowMs && nowMs < hour.startMs + HOUR_MS);
  if (running === undefined || running.buy === null) {
    return null;
  }
  const known = answer.hours.map((hour) => hour.buy).filter((price): price is number => price !== null).sort((a, b) => a - b);
  const low = known[Math.floor(known.length / 3)]!;
  const high = known[Math.floor((known.length * 2) / 3)]!;
  const band: PriceBand = running.buy < low ? "cheap" : running.buy < high ? "mid" : "dear";
  return { minor: running.buy, band };
}

/** The share (%) of what the site uses now (the house, the car and a charging battery) that is not bought; `null`
 * without the figures. */
export function ownShare(figures: NowFigures): number | null {
  if (figures.house.kw === null || figures.lines.some((line) => line.kw === null)) {
    return null;
  }
  const out = figures.lines.filter((line) => line.way === "out" && line.role !== "grid").reduce((sum, line) => sum + line.kw!, 0);
  const use = figures.house.kw + out;
  const bought = figures.lines.find((line) => line.role === "grid" && line.way === "in")?.kw ?? 0;
  return Math.max(0, Math.min(100, Math.round(100 * (1 - bought / Math.max(use, 0.01)))));
}

/** The headline's sentence (see the module), as its key; `null` where nothing is worth a sentence. */
export function whyKey(figures: NowFigures, band: PriceBand | null): TranslationKey | null {
  const way = (role: NodeRole): NowLine["way"] => figures.lines.find((line) => line.role === role)?.way ?? "still";
  // Without the grid's reading (no live figures) there is nothing to say.
  if (figures.lines.some((line) => line.kw === null)) {
    return null;
  }
  const sun = way("sun") === "in";
  const buying = way("grid") === "in";
  const selling = way("grid") === "out";
  const discharging = way("battery") === "in";
  const charging = way("battery") === "out";
  const car = way("car") === "out";
  if (discharging && band === "dear") {
    return sun ? "house.nowTab.why.batterySunDear" : "house.nowTab.why.batteryDear";
  }
  if (buying && band === "cheap" && (charging || car)) {
    return charging && car ? "house.nowTab.why.cheapBatteryCar" : charging ? "house.nowTab.why.cheapBattery" : "house.nowTab.why.cheapCar";
  }
  if (sun && selling) {
    return charging ? "house.nowTab.why.sunBatterySold" : "house.nowTab.why.sunSold";
  }
  if (sun && discharging) {
    return buying ? "house.nowTab.why.sunBatteryPart" : "house.nowTab.why.sunBatteryAll";
  }
  if (sun && charging && !buying) {
    return "house.nowTab.why.sunBattery";
  }
  if (sun && !buying && !discharging) {
    return "house.nowTab.why.sunAll";
  }
  if (sun) {
    return "house.nowTab.why.sunPart";
  }
  if (discharging) {
    return buying ? "house.nowTab.why.batteryPart" : "house.nowTab.why.battery";
  }
  if (car) {
    return "house.nowTab.why.car";
  }
  return buying ? "house.nowTab.why.gridAll" : null;
}

function priceWords(language: Language, answer: HouseAnswer, minor: number): string {
  const major = answer.money?.major_unit ?? null;
  if (major !== null && major !== "") {
    return `${formatFixed(language, Math.round(minor) / 100, 2)} ${major}`;
  }
  return `${formatNumber(language, minor, 0)}${answer.money?.minor_unit ? ` ${answer.money.minor_unit}` : ""}`;
}

/** The headline row (see the module). */
export function headline(doc: Document, language: Language, answer: HouseAnswer, live: HouseNow | null, nowMs: number, figures: NowFigures): HTMLElement {
  const row = element(doc, "div", "spotnav-now-head");
  row.dataset["k"] = "head";
  const has = present(live, answer);
  const own = has.sun || has.battery ? ownShare(figures) : null;
  if (own !== null) {
    const pill = element(doc, "span", "spotnav-now-pill-own", translate(language, "house.nowTab.own", { pct: formatNumber(language, own, 0) }));
    pill.dataset["tone"] = own >= 80 ? "high" : own >= 40 ? "mid" : "low";
    pill.dataset["part"] = "own";
    row.append(pill);
  }
  const price = priceNow(answer, nowMs);
  if (price !== null) {
    const pill = element(doc, "span", "spotnav-now-pill-price", translate(language, `house.nowTab.price.${price.band}` as TranslationKey, { price: priceWords(language, answer, price.minor) }));
    pill.dataset["band"] = price.band;
    pill.dataset["part"] = "price";
    row.append(pill);
  }
  const why = live === null ? null : whyKey(figures, price?.band ?? null);
  if (why !== null) {
    const sentence = element(doc, "p", "spotnav-now-why", translate(language, why));
    sentence.dataset["part"] = "why";
    row.append(sentence);
  }
  return row;
}

// ------------------------------------------------------------------------------------------------ the drawing

/** A line's width (px in the drawing) for its power: `(2 + √kW × 3.4) × k`, `k` the drawing's scale (`nowScale`);
 * a thin idle line below `STILL_KW`. */
export function lineWidth(kw: number | null, k = 1): number {
  return (kw === null || Math.abs(kw) < STILL_KW ? 1.5 : 2 + Math.sqrt(Math.abs(kw)) * 3.4) * k;
}

/** The chevrons' scale on a line (their height at most 70 % of its width), `null` on a line too thin for them. */
export function chevronScale(kw: number, k = 1): number | null {
  const scale = Math.min(1.25 * k, (lineWidth(kw, k) * 0.7) / 9);
  return scale < 0.3 * k ? null : scale;
}

/** The chevrons' speed (px/s along the line): faster with more power. */
export function chevronSpeed(kw: number, k = 1): number {
  return (0.12 + Math.sqrt(Math.abs(kw)) * 0.06) * 60 * k;
}

/** The drawing's scale for its width: the approved drawing is 360 px wide; a wider card draws its nodes, rings, icons,
 * lines and words larger, up to 1.6 times, the rest of the width spreading the nodes. */
export function nowScale(width: number): number {
  return Math.max(1, Math.min(1.6, width / 360));
}

/** The words' sizes (whole screen pixels, so they stay crisp at the drawing's scale of 1): captions, kW labels and
 * the house's figure. */
export function nowFontSizes(k: number): { caption: number; label: number; big: number } {
  return { caption: Math.round(11.5 * k), label: Math.round(13 * k), big: Math.round(16 * k) };
}

interface Spot {
  x: number;
  y: number;
}

export interface FlowLayout {
  kind: "row" | "corners";
  width: number;
  /** The scale of everything drawn (`nowScale`). */
  k: number;
  /** The panel's height; the drawing reaches 20 px above it and 30 px below for the captions. */
  height: number;
  spots: Partial<Record<NodeRole | "house", Spot>>;
}

/** The drawing's width for the card's: the card's whole width, between 300 and 640 px. */
export function drawingWidth(width: number): number {
  return Math.round(Math.max(300, Math.min(640, width)));
}

/** Where the nodes stand (see the module): one row for one or two outer nodes, fixed corners for three or four. */
export function flowLayout(roles: NodeRole[], width: number): FlowLayout {
  const order: NodeRole[] = ["sun", "grid", "battery", "car"];
  const outer = order.filter((role) => roles.includes(role));
  const k = nowScale(width);
  // As far out as the nodes go: each centre its drawing's reach (the battery's level ring the widest) and 6 px off
  // the edge; the captions are anchored inward from the edges.
  const side = Math.max(...outer.map((role) => NODE_OUTER[role]), NODE_OUTER.grid) * k + NODE_EDGE_PX;
  if (outer.length <= 2) {
    const spots: FlowLayout["spots"] = { house: { x: width / 2, y: 75 * k } };
    outer.forEach((role, index) => {
      spots[role] = { x: index === 0 ? side : width - side, y: 75 * k };
    });
    return { kind: "row", width, k, height: 150 * k, spots };
  }
  // The corners' rise to the house about 0.85 of their run, so the diagonals stay near 40°, never less than the
  // approved drawing's.
  const top = 52 * k;
  const rise = Math.max(98 * k, 0.85 * (width / 2 - side));
  const bottom = top + 2 * rise;
  const corners: Record<NodeRole, Spot> = { sun: { x: side, y: top }, grid: { x: width - side, y: top }, battery: { x: side, y: bottom }, car: { x: width - side, y: bottom } };
  const spots: FlowLayout["spots"] = { house: { x: width / 2, y: top + rise } };
  for (const role of outer) {
    spots[role] = corners[role];
  }
  return { kind: "corners", width, k, height: bottom + top, spots };
}

/** The room (px) between a corner node's drawing and the drawing's edge. */
export const NODE_EDGE_PX = 6;
/** The captions' room (px) from the drawing's edge. */
export const CAPTION_EDGE_PX = 4;

/** How far out from its centre each node's drawing reaches: a line ends there. */
export const NODE_OUTER: Record<NodeRole | "house", number> = { sun: 25, grid: 25, car: 25, battery: 31, house: 51 };
const NODE_R = 22;
const RING_R = 44;
const RING_W = 11;
const INNER_R = 37;
const SOC_R = 27;

/** A line between two nodes, ending at each one's outer edge. */
export function lineEnds(from: Spot, to: Spot, fromOuter: number, toOuter: number): { a: Spot; b: Spot; ux: number; uy: number; length: number } {
  const d = Math.hypot(to.x - from.x, to.y - from.y) || 1;
  const ux = (to.x - from.x) / d;
  const uy = (to.y - from.y) / d;
  const a = { x: from.x + ux * fromOuter, y: from.y + uy * fromOuter };
  const b = { x: to.x - ux * toOuter, y: to.y - uy * toOuter };
  return { a, b, ux, uy, length: Math.hypot(b.x - a.x, b.y - a.y) };
}

/** Where a line's kW stands (see the module): half its width and 10 px off its middle, above a horizontal line and on
 * the outer side of a diagonal (away from the drawing's middle). */
export function labelPlace(a: Spot, b: Spot, kw: number, centreX: number, k = 1): { x: number; y: number; anchor: "start" | "middle" | "end"; side: "above" | "outer" } {
  const d = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const ux = (b.x - a.x) / d;
  const uy = (b.y - a.y) / d;
  let nx = -uy;
  let ny = ux;
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const horizontal = Math.abs(uy) < 0.2;
  if (horizontal) {
    if (ny > 0) {
      nx = -nx;
      ny = -ny;
    }
  } else if ((mx < centreX && nx > 0) || (mx > centreX && nx < 0)) {
    nx = -nx;
    ny = -ny;
  }
  const off = lineWidth(kw, k) / 2 + 10 * k;
  return { x: mx + nx * off, y: my + ny * off + 4 * k, anchor: nx < -0.2 ? "end" : nx > 0.2 ? "start" : "middle", side: horizontal ? "above" : "outer" };
}

/** Where a node's captions start (see the module): anchored inward near an edge, centred in the middle. */
export function captionAnchor(x: number, width: number): { x: number; anchor: "start" | "middle" | "end" } {
  if (x < width / 3) {
    return { x: CAPTION_EDGE_PX, anchor: "start" };
  }
  if (x > (width * 2) / 3) {
    return { x: width - CAPTION_EDGE_PX, anchor: "end" };
  }
  return { x, anchor: "middle" };
}

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, name: K, attributes: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const created = doc.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    created.setAttribute(key, String(value));
  }
  return created;
}

const f1 = (value: number): string => value.toFixed(1);

/** A live figure with one decimal at any size, so the written figures add up; "≈" when estimated. */
function kwText(language: Language, kw: number, estimated = false): string {
  return `${estimated ? "≈ " : ""}${formatFixed(language, kw, 1)} kW`;
}

/** A figure without its unit ("0,1"). */
function bare(language: Language, kwh: number): string {
  return formatFixed(language, kwh, kwh >= 100 ? 0 : 1);
}

function flowKwh(flow: EnergyFlow | null, side: "source" | "use", role: string): number | null {
  const node = flow?.nodes.find((item) => item.side === side && item.role === role);
  return node === undefined ? null : node.kwh;
}

/** The line icons (a 24-unit box, stroked in the node's colour), as the approved drawing has them. */
function icon(doc: Document, role: NodeRole | "house", soc: number | null): SVGGElement {
  const group = svg(doc, "g", { class: "spotnav-now-icon" });
  const add = (name: "path" | "circle" | "rect" | "line", attributes: Record<string, string | number>): SVGElement => {
    const made = svg(doc, name, attributes);
    group.append(made);
    return made;
  };
  if (role === "sun") {
    add("circle", { cx: 12, cy: 12, r: 4.2 });
    for (let index = 0; index < 8; index += 1) {
      const angle = (index * Math.PI) / 4;
      add("line", { x1: f1(12 + Math.cos(angle) * 7), y1: f1(12 + Math.sin(angle) * 7), x2: f1(12 + Math.cos(angle) * 9.6), y2: f1(12 + Math.sin(angle) * 9.6) });
    }
  } else if (role === "grid") {
    add("path", { d: "M8 22 L12 3 L16 22 M9.2 16 H14.8 M10.2 11 H13.8 M5 7 H19 M7 7 L9.2 16 M17 7 L14.8 16" });
  } else if (role === "battery") {
    add("rect", { x: 6, y: 5, width: 12, height: 17, rx: 2.5 });
    add("rect", { x: 9.5, y: 2.5, width: 5, height: 2.5, rx: 1, class: "spotnav-now-icon-fill" });
    const h = Math.max(1, (13 * (soc ?? 0)) / 100);
    const level = add("rect", { x: 8.5, y: f1(7.5 + 13 - h), width: 7, height: f1(h), rx: 1, class: "spotnav-now-icon-fill spotnav-now-icon-level" });
    level.dataset["k"] = "battery-level";
  } else if (role === "car") {
    add("path", { d: "M3.5 15.5 V12.5 L6 7.5 Q6.6 6.5 7.8 6.5 H16.2 Q17.4 6.5 18 7.5 L20.5 12.5 V15.5 Q20.5 16.5 19.5 16.5 H4.5 Q3.5 16.5 3.5 15.5 Z M5 12.3 H19" });
    add("circle", { cx: 7.5, cy: 16.8, r: 1.9, class: "spotnav-now-icon-wheel" });
    add("circle", { cx: 16.5, cy: 16.8, r: 1.9, class: "spotnav-now-icon-wheel" });
  } else {
    add("path", { d: "M4 11 L12 4 L20 11 V20 H14.5 V14.5 H9.5 V20 H4 Z" });
  }
  return group;
}

export interface NowInput {
  doc: Document;
  language: Language;
  answer: HouseAnswer;
  live: HouseNow | null;
  /** The card's width (px). */
  width: number;
  /** A fixed box's height for the drawing (px), else its own. */
  height?: number | null;
  idPrefix: string;
  nowMs: number;
}

/** A node's two captions: its state now, and today's kWh in its colour. */
interface Captions {
  state: string;
  today: string;
}

function nodeCaptions(language: Language, role: NodeRole, answer: HouseAnswer, live: HouseNow | null, line: NowLine): Captions {
  const flow = answer.energy_flow;
  const value = (kwh: number | null): string => (kwh === null ? "–" : bare(language, kwh));
  const t = (key: string, values?: Record<string, string>): string => translate(language, `house.nowTab.${key}` as TranslationKey, values);
  const unread = line.kw === null;
  if (role === "sun") {
    const today = flowKwh(flow, "source", "solar");
    return { state: unread || line.way === "in" ? t("state.sun") : t("state.sunNone"), today: t("today.sun", { kwh: today === null ? "–" : kwhText(language, today) }) };
  }
  if (role === "grid") {
    const state = unread ? t("state.grid") : line.way === "in" ? t("state.buying") : line.way === "out" ? t("state.selling") : t("state.gridIdle");
    return { state, today: t("today.grid", { bought: value(flowKwh(flow, "source", "grid")), sold: value(flowKwh(flow, "use", "grid")) }) };
  }
  if (role === "battery") {
    const soc = live?.soc_pct ?? answer.battery?.soc_pct ?? null;
    const doing = unread ? null : line.way === "in" ? t("state.covers") : line.way === "out" ? t("state.charging") : t("state.holds");
    const level = soc === null ? null : `${formatNumber(language, soc, 0)} %`;
    const state = [level, doing].filter((part): part is string => part !== null).join(" · ") || t("state.battery");
    return { state, today: t("today.battery", { in: value(flowKwh(flow, "use", "battery")), out: value(flowKwh(flow, "source", "battery")) }) };
  }
  const soc = carPlugged(live) ? (live?.car_soc_pct ?? null) : null;
  const level = soc === null ? "" : ` · ${formatNumber(language, soc, 0)} %`;
  const state =
    line.way === "out" ? `${t("state.carCharging")}${level}` : carPlugged(live) ? `${t("state.carPlugged")}${level}` : live?.car_connected === false ? t("state.noCar") : t("state.car");
  const today = flowKwh(flow, "use", "car");
  return { state, today: t("today.car", { kwh: today === null ? "–" : kwhText(language, today) }) };
}

/** A caption's width estimate per character, in em (generous, so the words never meet). */
export const CAPTION_EM = 0.55;

/** The captions' font size: `wanted`, or smaller (not below 9 px) where the captions on one line (the top corners,
 * the bottom corners, or a row's) would not fit the width beside each other. */
export function captionSize(wanted: number, width: number, layout: FlowLayout, words: Map<NodeRole, Captions>): number {
  const house = layout.spots.house!;
  const groups: NodeRole[][] =
    layout.kind === "row"
      ? [Array.from(words.keys())]
      : [Array.from(words.keys()).filter((role) => layout.spots[role]!.y < house.y), Array.from(words.keys()).filter((role) => layout.spots[role]!.y >= house.y)];
  let size = wanted;
  for (const group of groups) {
    for (const kind of ["state", "today"] as const) {
      const chars = group.reduce((sum, role) => sum + words.get(role)![kind].length, 0);
      if (chars === 0) {
        continue;
      }
      // From the left caption's start to the right one's end (`captionAnchor`), a gap between them.
      const room = width - 2 * CAPTION_EDGE_PX - (group.length > 1 ? 16 : 0);
      size = Math.min(size, Math.floor(room / (CAPTION_EM * chars)));
    }
  }
  return Math.max(9, size);
}

/** What makes two drawings the same drawing (the layout, each line's way and chevrons, the ring's parts): a patch
 * keeps it. */
function signature(layout: FlowLayout, figures: NowFigures): string {
  return [
    layout.kind,
    layout.width,
    ...figures.lines.map((line) => `${line.role}:${line.way}:${line.kw !== null && line.way !== "still" && chevronScale(line.kw, layout.k) !== null ? "c" : ""}`),
    ...figures.parts.filter((part) => part.kw > 0).map((part) => part.role),
  ].join("|");
}

/** The drawing (see the module): an SVG at the card's width, its words in the reader's language. */
export function renderSchematic(input: NowInput, figures: NowFigures = nowFigures(input.live, input.answer)): SVGSVGElement {
  const { doc, language, answer, live } = input;
  const width = drawingWidth(input.width);
  const layout = flowLayout(
    figures.lines.map((line) => line.role),
    width,
  );
  const k = layout.k;
  // The captions' size: the scale's, smaller only where the captions sharing a line would not fit beside each other.
  const captionWords = new Map(figures.lines.map((line) => [line.role, nodeCaptions(language, line.role, answer, live, line)]));
  const fonts = { ...nowFontSizes(k), caption: captionSize(nowFontSizes(k).caption, width, layout, captionWords) };
  const top = Math.round(-20 * k);
  const total = Math.round(layout.height + 50 * k);
  const root = svg(doc, "svg", { viewBox: `0 ${top} ${width} ${total}`, class: "spotnav-now-schematic", role: "img", "data-layout": layout.kind });
  root.dataset["sig"] = signature(layout, figures);
  root.style.width = "100%";
  root.style.maxWidth = `${width}px`;
  root.style.height = input.height === null || input.height === undefined ? "auto" : `${Math.round(input.height)}px`;
  root.style.setProperty("--spotnav-now-k", String(Number(k.toFixed(3))));
  root.style.setProperty("--spotnav-now-caption", `${fonts.caption}px`);
  root.style.setProperty("--spotnav-now-label", `${fonts.label}px`);
  root.style.setProperty("--spotnav-now-big", `${fonts.big}px`);
  const title = svg(doc, "title", { id: `${input.idPrefix}-now-title` });
  root.append(title);
  root.setAttribute("aria-labelledby", title.id);
  const said: string[] = [];
  // Depth: shadows in user space over the whole drawing (an objectBoundingBox filter clips a horizontal line), wide
  // enough to cover it from a node's own translated frame too.
  const defs = svg(doc, "defs");
  const area = { filterUnits: "userSpaceOnUse", x: -width, y: -total, width: width * 3, height: total * 3 };
  const nodeShadow = svg(doc, "filter", { id: `${input.idPrefix}-now-node-shadow`, ...area });
  nodeShadow.append(svg(doc, "feDropShadow", { dx: 0, dy: 2.5, stdDeviation: 2.5, "flood-color": "#000", "flood-opacity": 0.28 }));
  const lineShadow = svg(doc, "filter", { id: `${input.idPrefix}-now-line-shadow`, ...area });
  lineShadow.append(svg(doc, "feDropShadow", { dx: 0, dy: 1.5, stdDeviation: 1.2, "flood-color": "#000", "flood-opacity": 0.22 }));
  const light = svg(doc, "radialGradient", { id: `${input.idPrefix}-now-light`, cx: "35%", cy: "30%", r: "75%" });
  light.append(svg(doc, "stop", { offset: "0%", "stop-color": "#fff", "stop-opacity": 0.16 }), svg(doc, "stop", { offset: "100%", "stop-color": "#000", "stop-opacity": 0.1 }));
  defs.append(nodeShadow, lineShadow, light);
  root.append(defs);
  const nodeFilter = `url(#${nodeShadow.id})`;
  const lightFill = `url(#${light.id})`;
  const house = layout.spots.house!;
  const wires = svg(doc, "g", { class: "spotnav-now-wires" });
  const labels = svg(doc, "g", { class: "spotnav-now-labels" });
  root.append(wires);
  for (const line of figures.lines) {
    const spot = layout.spots[line.role]!;
    const flowing = line.kw !== null && line.way !== "still";
    // Drawn from the source to the target, so the chevrons run the way the power flows.
    const ends = line.way === "out" ? lineEnds(house, spot, NODE_OUTER.house * k, NODE_OUTER[line.role] * k) : lineEnds(spot, house, NODE_OUTER[line.role] * k, NODE_OUTER.house * k);
    const d = `M${f1(ends.a.x)} ${f1(ends.a.y)}L${f1(ends.b.x)} ${f1(ends.b.y)}`;
    if (!flowing) {
      const idle = svg(doc, "path", { d, class: "spotnav-now-idle" });
      idle.dataset["role"] = line.role;
      wires.append(idle);
      continue;
    }
    const kw = line.kw!;
    const w = lineWidth(kw, k);
    const wire = svg(doc, "path", { d, class: "spotnav-now-line", "stroke-width": f1(w), filter: `url(#${lineShadow.id})` });
    wire.dataset["role"] = line.role;
    wire.dataset["tone"] = line.tone;
    wire.dataset["k"] = `line-${line.role}`;
    wires.append(wire);
    // A faint light stripe along the line's upper side, so it reads as a tube.
    let nx = -ends.uy;
    let ny = ends.ux;
    if (ny > 0) {
      nx = -nx;
      ny = -ny;
    }
    const sh = w * 0.18;
    const stripe = svg(doc, "path", {
      d: `M${f1(ends.a.x + nx * sh)} ${f1(ends.a.y + ny * sh)}L${f1(ends.b.x + nx * sh)} ${f1(ends.b.y + ny * sh)}`,
      class: "spotnav-now-stripe",
      "stroke-width": f1(Math.max(1, w * 0.28)),
    });
    stripe.dataset["k"] = `stripe-${line.role}`;
    wires.append(stripe);
    const scale = chevronScale(kw, k);
    if (scale !== null) {
      // Evenly spaced chevrons in the line's own frame, one spacing more than fit, the run moving one spacing per lap
      // and the frame clipped to the line, so they flow on without a jump.
      const count = Math.max(2, Math.min(7, Math.round(ends.length / (14 * k))));
      const step = ends.length / count;
      const angle = (Math.atan2(ends.uy, ends.ux) * 180) / Math.PI;
      const clip = svg(doc, "clipPath", { id: `${input.idPrefix}-now-clip-${line.role}`, clipPathUnits: "userSpaceOnUse" });
      clip.append(svg(doc, "rect", { x: 0, y: f1(-12 * k), width: f1(ends.length), height: f1(24 * k) }));
      defs.append(clip);
      const frame = svg(doc, "g", { class: "spotnav-now-chevrons", transform: `translate(${f1(ends.a.x)} ${f1(ends.a.y)}) rotate(${angle.toFixed(1)})`, "clip-path": `url(#${clip.id})` });
      frame.dataset["role"] = line.role;
      const run = svg(doc, "g", { class: "spotnav-now-run" });
      run.dataset["k"] = `run-${line.role}`;
      run.style.setProperty("--spotnav-now-step", `${f1(step)}px`);
      run.style.animationDuration = `${(step / chevronSpeed(kw, k)).toFixed(2)}s`;
      for (let index = -1; index < count; index += 1) {
        const chevron = svg(doc, "path", {
          d: "M-3,-4 L2,0 L-3,4",
          class: "spotnav-now-chevron",
          transform: `translate(${f1((index + 0.5) * step)} 0) scale(${scale.toFixed(2)})`,
          "stroke-width": ((2 / scale) * Math.min(1, (scale / k) * 1.4) * k).toFixed(2),
        });
        chevron.dataset["k"] = `chevron-${line.role}-${index + 1}`;
        run.append(chevron);
      }
      frame.append(run);
      wires.append(frame);
    }
    const place = labelPlace(ends.a, ends.b, kw, house.x, k);
    const label = Object.assign(svg(doc, "text", { x: f1(place.x), y: f1(place.y), "text-anchor": place.anchor, class: "spotnav-now-kw" }), { textContent: kwText(language, kw, line.estimated) });
    label.dataset["role"] = line.role;
    label.dataset["side"] = place.side;
    label.dataset["k"] = `kw-${line.role}`;
    labels.append(label);
    const estimate = line.estimated ? ` ${translate(language, "house.loads.estimated")}` : "";
    said.push(translate(language, `house.nowTab.line.${line.role}.${line.way}` as TranslationKey, { kw: kwText(language, kw) + estimate }));
  }
  // The outer nodes, each with its two captions.
  const captions = svg(doc, "g", { class: "spotnav-now-captions" });
  for (const line of figures.lines) {
    const spot = layout.spots[line.role]!;
    const group = svg(doc, "g", { class: "spotnav-now-node", transform: `translate(${f1(spot.x)} ${f1(spot.y)})` });
    group.dataset["role"] = line.role;
    group.dataset["tone"] = line.tone;
    group.dataset["k"] = `node-${line.role}`;
    const soc = line.role === "battery" ? (live?.soc_pct ?? answer.battery?.soc_pct ?? null) : null;
    if (line.role === "battery") {
      const around = 2 * Math.PI * SOC_R * k;
      group.append(svg(doc, "circle", { r: f1(SOC_R * k), class: "spotnav-now-soc-track" }));
      const arc = svg(doc, "circle", { r: f1(SOC_R * k), class: "spotnav-now-soc", transform: "rotate(-90)", "stroke-dasharray": `${f1((around * Math.max(0, Math.min(100, soc ?? 0))) / 100)} ${f1(around)}` });
      arc.dataset["k"] = "soc";
      group.append(arc);
    }
    group.append(svg(doc, "circle", { r: f1(NODE_R * k), class: "spotnav-now-disc", filter: nodeFilter }), svg(doc, "circle", { r: f1((NODE_R - 1.5) * k), fill: lightFill, class: "spotnav-now-light" }));
    const glyph = icon(doc, line.role, soc);
    glyph.setAttribute("transform", `translate(${f1(-12 * k)} ${f1(-12 * k)}) scale(${k.toFixed(3)})`);
    group.append(glyph);
    root.append(group);
    const words = captionWords.get(line.role)!;
    const below = spot.y >= house.y;
    const at = captionAnchor(spot.x, width);
    const step = fonts.caption + 3;
    [
      [words.state, "spotnav-now-state", "state"],
      [words.today, "spotnav-now-today", "today"],
    ].forEach(([textValue, className, kind], index) => {
      const y = below ? spot.y + 42 * k + index * step : spot.y - 31 * k - (1 - index) * step;
      const caption = Object.assign(svg(doc, "text", { x: f1(at.x), y: f1(y), "text-anchor": at.anchor, class: className! }), { textContent: textValue! });
      caption.dataset["role"] = line.role;
      caption.dataset["tone"] = line.role === "grid" ? "import" : line.tone;
      caption.dataset["k"] = `${kind}-${line.role}`;
      captions.append(caption);
    });
    said.push(`${words.state}, ${words.today}`);
  }
  // The house: the ring of its parts, the whole house in the middle and its day under the ring.
  const home = svg(doc, "g", { class: "spotnav-now-house", transform: `translate(${f1(house.x)} ${f1(house.y)})` });
  const ringR = RING_R * k;
  home.append(svg(doc, "circle", { r: f1(ringR), class: "spotnav-now-ring-track", "stroke-width": f1(RING_W * k), filter: nodeFilter }));
  const around = 2 * Math.PI * ringR;
  const whole = figures.parts.reduce((sum, part) => sum + part.kw, 0);
  let done = 0;
  for (const part of figures.parts) {
    if (part.kw <= 0 || whole <= 0) {
      continue;
    }
    const share = part.kw / whole;
    const segment = svg(doc, "circle", {
      r: f1(ringR),
      class: "spotnav-now-ring",
      "stroke-width": f1(RING_W * k),
      transform: "rotate(-90)",
      "stroke-dasharray": `${f1(Math.max(0.5, around * share - (share < 1 ? 2 : 0)))} ${f1(around)}`,
      "stroke-dashoffset": f1(-around * done),
    });
    segment.dataset["tone"] = part.role;
    segment.dataset["k"] = `ring-${part.role}`;
    home.append(segment);
    done += share;
  }
  home.append(
    svg(doc, "circle", { r: f1(INNER_R * k), class: "spotnav-now-inner" }),
    svg(doc, "circle", { r: f1(INNER_R * k), fill: lightFill, class: "spotnav-now-light" }),
    svg(doc, "circle", { r: f1(ringR), class: "spotnav-now-sheen", transform: "translate(-1 -1.5)" }),
  );
  const houseIcon = icon(doc, "house", null);
  houseIcon.setAttribute("transform", `translate(${f1(-7.44 * k)} ${f1(-24.44 * k)}) scale(${(0.62 * k).toFixed(3)})`);
  houseIcon.dataset["tone"] = "house";
  home.append(houseIcon);
  const houseKw = figures.house.kw;
  const big = Object.assign(svg(doc, "text", { x: 0, y: f1(8 * k), "text-anchor": "middle", class: "spotnav-now-house-kw" }), { textContent: houseKw === null ? "–" : kwText(language, houseKw, figures.house.estimated) });
  big.dataset["k"] = "kw-house";
  const name = Object.assign(svg(doc, "text", { x: 0, y: f1(8 * k + fonts.caption + 2), "text-anchor": "middle", class: "spotnav-now-house-name" }), { textContent: translate(language, "house.nowTab.houseName") });
  home.append(big, name);
  root.append(home);
  const flow = answer.energy_flow;
  const day = flow === null ? null : (flowKwh(flow, "use", "house") ?? 0) + (flowKwh(flow, "use", "heat") ?? 0);
  // In a row, under the outer nodes' two captions, so the three never meet.
  const houseTodayY = layout.kind === "row" ? house.y + 42 * k + 2 * (fonts.caption + 3) : house.y + ringR + 19 * k;
  const houseToday = Object.assign(svg(doc, "text", { x: f1(house.x), y: f1(houseTodayY), "text-anchor": "middle", class: "spotnav-now-today" }), {
    textContent: translate(language, "house.nowTab.today.house", { kwh: day === null ? "–" : kwhText(language, day) }),
  });
  houseToday.dataset["tone"] = "house";
  houseToday.dataset["k"] = "today-house";
  captions.append(houseToday);
  root.append(captions, labels);
  if (houseKw !== null) {
    said.unshift(`${translate(language, "house.nowTab.houseNow")} ${kwText(language, houseKw)}${figures.house.estimated ? ` ${translate(language, "house.loads.estimated")}` : ""}`);
  }
  said.push(houseToday.textContent ?? "");
  title.textContent = said.join(". ");
  title.dataset["k"] = "title";
  return root;
}

/** The ring's legend under the drawing: each part with its kW, "≈" where estimated. */
export function ringLegend(doc: Document, language: Language, figures: NowFigures): HTMLElement {
  const legend = element(doc, "div", "spotnav-now-legend");
  legend.dataset["k"] = "legend";
  for (const part of figures.parts) {
    if (part.kw <= 0) {
      continue;
    }
    const item = element(doc, "span", "spotnav-now-legend-item");
    item.dataset["part"] = part.role;
    const swatch = element(doc, "i", "spotnav-now-swatch");
    swatch.dataset["tone"] = part.role;
    swatch.setAttribute("aria-hidden", "true");
    item.append(swatch, doc.createTextNode(`${translate(language, `house.nowTab.part.${part.role}` as TranslationKey)} ${kwText(language, part.kw, part.estimated)}`));
    legend.append(item);
  }
  return legend;
}

/** The Now tab's live part: the headline, the drawing and the ring's legend. */
export function renderNow(input: NowInput): HTMLElement {
  const figures = nowFigures(input.live, input.answer);
  const block = element(input.doc, "div", "spotnav-now-flow");
  block.append(headline(input.doc, input.language, input.answer, input.live, input.nowMs, figures), renderSchematic(input, figures), ringLegend(input.doc, input.language, figures));
  return block;
}

function copyAttributes(target: Element, source: Element): void {
  for (const attribute of Array.from(target.attributes)) {
    if (!source.hasAttribute(attribute.name)) {
      target.removeAttribute(attribute.name);
    }
  }
  for (const attribute of Array.from(source.attributes)) {
    if (target.getAttribute(attribute.name) !== attribute.value) {
      target.setAttribute(attribute.name, attribute.value);
    }
  }
}

/**
 * Bring a drawn schematic up to a fresh one in place (a live refresh): the same drawing (`data-sig`) has its figures,
 * widths, places and speeds copied over, so the chevrons keep running; another (a line turned, a part came or went,
 * another size) replaces it. Whether it was patched in place.
 */
export function patchSchematic(drawn: SVGSVGElement, fresh: SVGSVGElement): boolean {
  if (drawn.dataset["sig"] !== fresh.dataset["sig"]) {
    drawn.replaceWith(fresh);
    return false;
  }
  const old = new Map(Array.from(drawn.querySelectorAll<SVGElement>("[data-k]")).map((item) => [item.dataset["k"], item]));
  for (const item of Array.from(fresh.querySelectorAll<SVGElement>("[data-k]"))) {
    const target = old.get(item.dataset["k"]);
    if (target === undefined) {
      continue;
    }
    copyAttributes(target, item);
    if ((item.tagName.toLowerCase() === "text" || item.tagName.toLowerCase() === "title") && target.textContent !== item.textContent) {
      target.textContent = item.textContent;
    }
  }
  return true;
}

/** `patchSchematic` for the whole live part: the headline and the legend replaced, the drawing patched. Whether the
 * drawing was patched in place. */
export function patchNow(drawn: HTMLElement, fresh: HTMLElement): boolean {
  for (const part of ["head", "legend"]) {
    const was = drawn.querySelector<HTMLElement>(`:scope > [data-k='${part}']`);
    const now = fresh.querySelector<HTMLElement>(`:scope > [data-k='${part}']`);
    if (was !== null && now !== null) {
      was.replaceWith(now);
    }
  }
  const svgDrawn = drawn.querySelector<SVGSVGElement>("svg.spotnav-now-schematic");
  const svgFresh = fresh.querySelector<SVGSVGElement>("svg.spotnav-now-schematic");
  if (svgDrawn === null || svgFresh === null) {
    drawn.replaceWith(fresh);
    return false;
  }
  return patchSchematic(svgDrawn, svgFresh);
}

const TONES: [string, string][] = [
  ["sun", "sun"],
  ["import", "import"],
  ["export", "export"],
  ["battery", "battery"],
  ["car", "car"],
  ["house", "house"],
  ["heat", "heat"],
  ["pool", "pool"],
  ["other", "other"],
];

export const NOW_STYLES = `
  .spotnav-now-flow {
    display: grid;
    gap: 10px;
    min-width: 0;
  }
${TONES.map(([tone, meaning]) => `  .spotnav-now-flow [data-tone="${tone}"], .spotnav-now-schematic [data-tone="${tone}"] { --spotnav-now-tone: var(--spotnav-energy-${meaning}); }`).join("\n")}
  .spotnav-now-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
    min-width: 0;
  }
  .spotnav-now-pill-own, .spotnav-now-pill-price {
    display: inline-flex;
    align-items: center;
    padding: 2px 10px;
    border-radius: 999px;
    border: 1px solid currentColor;
    font-size: 0.82rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  /* The pills' words in their colour mixed a third toward the text colour, so they read on a light and a dark card. */
  .spotnav-now-pill-own[data-tone="high"] { color: color-mix(in srgb, var(--spotnav-energy-export) 68%, var(--primary-text-color, #212121)); }
  .spotnav-now-pill-own[data-tone="mid"] { color: color-mix(in srgb, var(--spotnav-energy-mid) 68%, var(--primary-text-color, #212121)); }
  .spotnav-now-pill-own[data-tone="low"] { color: var(--secondary-text-color, #727272); }
  .spotnav-now-pill-price[data-band="cheap"] { color: color-mix(in srgb, var(--spotnav-energy-cheap) 68%, var(--primary-text-color, #212121)); }
  .spotnav-now-pill-price[data-band="mid"] { color: color-mix(in srgb, var(--spotnav-energy-mid) 68%, var(--primary-text-color, #212121)); }
  .spotnav-now-pill-price[data-band="dear"] { color: color-mix(in srgb, var(--spotnav-energy-expensive) 68%, var(--primary-text-color, #212121)); }
  .spotnav-now-why {
    flex-basis: 100%;
    margin: 0;
    font-size: 0.92rem;
    color: var(--secondary-text-color, #727272);
    overflow-wrap: break-word;
  }
  /* No stroke to inherit: a card-wide rule on an ancestor (the charts' now line is "spotnav-now") must never outline
     the drawing's words. */
  .spotnav-now-schematic {
    display: block;
    margin: 0 auto;
    overflow: visible;
    stroke: none;
  }
  .spotnav-now-idle {
    fill: none;
    stroke: var(--spotnav-energy-idle);
    stroke-width: 1.5;
    stroke-dasharray: 3 5;
    stroke-linecap: round;
  }
  .spotnav-now-line {
    fill: none;
    stroke: var(--spotnav-now-tone);
    stroke-linecap: round;
    opacity: 0.9;
  }
  .spotnav-now-stripe {
    fill: none;
    stroke: #ffffff;
    stroke-linecap: round;
    opacity: 0.28;
  }
  .spotnav-now-chevron {
    fill: none;
    stroke: #ffffff;
    stroke-linecap: round;
    stroke-linejoin: round;
    opacity: 0.85;
  }
  .spotnav-now-run {
    animation: spotnav-now-run 2s linear infinite;
  }
  @keyframes spotnav-now-run {
    from { transform: translateX(0); }
    to { transform: translateX(var(--spotnav-now-step, 14px)); }
  }
  /* Still under reduced motion: the chevrons still show the way. */
  @media (prefers-reduced-motion: reduce) {
    .spotnav-now-run { animation: none; }
  }
  .spotnav-now-disc {
    fill: var(--ha-card-background, var(--card-background-color, #fff));
    stroke: var(--spotnav-now-tone);
    stroke-width: calc(2.5px * var(--spotnav-now-k, 1));
  }
  .spotnav-now-icon path, .spotnav-now-icon circle, .spotnav-now-icon rect, .spotnav-now-icon line {
    fill: none;
    stroke: var(--spotnav-now-tone);
    stroke-width: 1.9;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .spotnav-now-icon .spotnav-now-icon-fill {
    fill: var(--spotnav-now-tone);
    stroke: none;
  }
  .spotnav-now-icon .spotnav-now-icon-level { opacity: 0.85; }
  .spotnav-now-icon .spotnav-now-icon-wheel { fill: var(--ha-card-background, var(--card-background-color, #fff)); }
  .spotnav-now-soc-track, .spotnav-now-ring-track {
    fill: none;
    stroke: var(--spotnav-energy-ring);
  }
  .spotnav-now-soc-track { stroke-width: calc(4px * var(--spotnav-now-k, 1)); }
  .spotnav-now-soc {
    fill: none;
    stroke: var(--spotnav-energy-battery);
    stroke-width: calc(4px * var(--spotnav-now-k, 1));
    stroke-linecap: round;
  }
  .spotnav-now-ring {
    fill: none;
    stroke: var(--spotnav-now-tone);
  }
  .spotnav-now-inner { fill: var(--ha-card-background, var(--card-background-color, #fff)); }
  .spotnav-now-sheen {
    fill: none;
    stroke: #ffffff;
    stroke-width: 2;
    opacity: 0.18;
  }
  .spotnav-now-schematic text {
    font-variant-numeric: tabular-nums;
    text-rendering: geometricPrecision;
  }
  .spotnav-now-kw, .spotnav-now-house-kw {
    fill: var(--primary-text-color, #212121);
    font-weight: 600;
  }
  .spotnav-now-kw {
    font-size: var(--spotnav-now-label, 13px);
    stroke: var(--ha-card-background, var(--card-background-color, #fff));
    stroke-width: 5px;
    stroke-linejoin: round;
    paint-order: stroke;
  }
  .spotnav-now-house-kw { font-size: var(--spotnav-now-big, 16px); font-weight: 700; }
  /* The captions: plain, crisp words, never outlined or shadowed. */
  .spotnav-now-house-name, .spotnav-now-state, .spotnav-now-today, .spotnav-now-house-kw {
    stroke: none;
    filter: none;
  }
  .spotnav-now-house-name, .spotnav-now-state {
    font-size: var(--spotnav-now-caption, 12px);
    font-weight: 400;
    fill: var(--secondary-text-color, #727272);
  }
  /* Today's figures in their node's colour, a third toward the text colour, so they keep 4.5:1 on both cards. */
  .spotnav-now-today {
    font-size: var(--spotnav-now-caption, 12px);
    font-weight: 600;
    fill: color-mix(in srgb, var(--spotnav-now-tone, var(--secondary-text-color, #727272)) 68%, var(--primary-text-color, #212121));
  }
  .spotnav-now-legend {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 4px 14px;
    font-size: 0.82rem;
    color: var(--secondary-text-color, #727272);
    font-variant-numeric: tabular-nums;
  }
  .spotnav-now-legend:empty { display: none; }
  .spotnav-now-swatch {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 3px;
    margin-inline-end: 5px;
    vertical-align: -1px;
    background: var(--spotnav-now-tone);
  }
  @media (forced-colors: active) {
    .spotnav-now-line, .spotnav-now-disc, .spotnav-now-ring, .spotnav-now-soc, .spotnav-now-icon path, .spotnav-now-icon circle, .spotnav-now-icon rect, .spotnav-now-icon line { stroke: CanvasText; }
    .spotnav-now-kw, .spotnav-now-house-kw, .spotnav-now-today, .spotnav-now-state { fill: CanvasText; }
    .spotnav-now-swatch { background: CanvasText; }
  }
`;
