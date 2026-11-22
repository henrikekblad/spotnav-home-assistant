// The house card's Plan tab (plan `plans/v3_flexible_loads_design.md`, step 2): every load SpotNav plans for the site
// on one rolling time axis, from the running hour to the end of the prices held (`spotnav/get_plan`), against the
// ceiling that applies. Top to bottom: the summary (the period's cost, the highest hour against the ceiling, the
// month's peak), the stacked chart (the loads above zero, the sun and the battery's cover below, the net from the
// grid as a line, the ceiling labelled, a price strip), a lane per load with one line of why, and the plan in words.
// A load SpotNav only watches is drawn hatched and says "would": the view never claims an action that is not taken.
// "Per load" and "In words" fold away under their headings, folded until this browser opens them (per site); a car's "does not
// fit" choice is never folded away: while one is there, "Per load" is open with a plain heading.
// Read-only; every figure is the backend's. Built with `textContent` only.

import { exactKeys, flag, list, numberOrNull, record, text, textOrNull, Malformed } from "./decode-primitives";
import { clock, dayCue, formatFixed, hourLabel, kwText, kwhText, moneyMinorAsMajor, type FormatContext } from "./format";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { element } from "./dom";
import en from "./i18n/en.json";
import { browserStore, readPlanSectionOpen, writePlanSectionOpen, type PlanSection, type PreferenceStore } from "./chart-preference";
import { choiceLabel, decodeDoesNotFit, doesNotFitText, type DoesNotFit, type DoesNotFitChoiceCode } from "./does-not-fit";

export const PLAN_API_VERSION = 1;
const HOUR_MS = 3_600_000;
const SVG_NS = "http://www.w3.org/2000/svg";

const PLAN_KEYS = ["api_version", "site_id", "time_zone", "generated_at", "money", "window", "house", "power_tariff", "would", "month_peak", "summary", "hours", "loads"] as const;
const WINDOW_KEYS = ["start", "end", "hours", "prices_end"] as const;
const HOUSE_KEYS = ["basis", "sun_in_house"] as const;
const SUMMARY_KEYS = ["cost", "peak_kw", "peak_start", "peak_ceiling_kw", "peak_ceiling", "over"] as const;
const HOUR_KEYS = ["start", "price", "sell", "house_kw", "sun_kw", "net_kw", "ceiling_kw", "ceiling"] as const;
const LOAD_KEYS = ["id", "kind", "name", "state", "in_house", "kw", "level", "runs", "summary", "does_not_fit"] as const;
const RUN_KEYS = ["start", "end", "what", "reason", "kw", "kwh"] as const;
const MONEY_KEYS = ["currency", "major_unit", "minor_unit"] as const;

export type PlanLoadKind = "car" | "home_battery" | "heat" | "heat_zone" | "water_heater";
const LOAD_KINDS: readonly PlanLoadKind[] = ["car", "home_battery", "heat", "heat_zone", "water_heater"];
export type CeilingKind = "import_target" | "power_level" | "power_tariff" | "fuse";
const CEILINGS: readonly CeilingKind[] = ["import_target", "power_level", "power_tariff", "fuse"];

export interface PlanHour {
  startMs: number;
  price: number | null;
  sell: number | null;
  house_kw: number | null;
  sun_kw: number | null;
  net_kw: number | null;
  ceiling_kw: number | null;
  ceiling: CeilingKind | null;
}

export interface PlanRun {
  startMs: number;
  endMs: number;
  what: string;
  reason: string | null;
  kw: number | null;
  kwh: number | null;
}

export interface PlanLoad {
  id: string;
  kind: PlanLoadKind;
  name: string | null;
  would: boolean;
  in_house: boolean;
  kw: (number | null)[];
  level: number[] | null;
  runs: PlanRun[];
  summary: Record<string, number | string | null>;
  /** A car's charge that does not fit under the ceiling by its departure (`does-not-fit.ts`), else `null`. */
  does_not_fit: DoesNotFit | null;
}

export interface PlanAnswer {
  site_id: string;
  time_zone: string;
  money: { currency: string | null; major_unit: string | null; minor_unit: string | null } | null;
  hours: PlanHour[];
  loads: PlanLoad[];
  power_tariff: boolean;
  would: boolean;
  month_peak: { kw: number; startMs: number } | null;
  summary: {
    cost: number | null;
    peak_kw: number | null;
    peakMs: number | null;
    peak_ceiling_kw: number | null;
    peak_ceiling: CeilingKind | null;
    over: boolean;
  };
  sun_in_house: boolean;
}

export type PlanDecodeResult = { ok: true; value: PlanAnswer } | { ok: false; failure: "unsupported" | "malformed" };

function instant(value: unknown): number {
  const ms = Date.parse(text(value));
  return Number.isFinite(ms) ? ms : (() => { throw new Malformed(); })();
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T {
  const found = text(value);
  if (!(allowed as readonly string[]).includes(found)) {
    throw new Malformed();
  }
  return found as T;
}

function ceilingOrNull(value: unknown): CeilingKind | null {
  return value === null ? null : oneOf(value, CEILINGS);
}

function decodeHour(raw: unknown): PlanHour {
  const source = record(raw);
  exactKeys(source, HOUR_KEYS);
  return {
    startMs: instant(source.start),
    price: numberOrNull(source.price),
    sell: numberOrNull(source.sell),
    house_kw: numberOrNull(source.house_kw),
    sun_kw: numberOrNull(source.sun_kw),
    net_kw: numberOrNull(source.net_kw),
    ceiling_kw: numberOrNull(source.ceiling_kw),
    ceiling: ceilingOrNull(source.ceiling),
  };
}

function decodeRun(raw: unknown): PlanRun {
  const source = record(raw);
  exactKeys(source, RUN_KEYS);
  return {
    startMs: instant(source.start),
    endMs: instant(source.end),
    what: text(source.what),
    reason: textOrNull(source.reason),
    kw: numberOrNull(source.kw),
    kwh: numberOrNull(source.kwh),
  };
}

function decodeLoad(raw: unknown, count: number): PlanLoad {
  const source = record(raw);
  exactKeys(source, LOAD_KEYS);
  const kw = list(source.kw).map(numberOrNull);
  const level = source.level === null ? null : list(source.level).map((value) => numberOrNull(value) ?? 0);
  if (kw.length !== count || (level !== null && level.length !== count)) {
    throw new Malformed();
  }
  const summary: Record<string, number | string | null> = {};
  for (const [key, value] of Object.entries(record(source.summary))) {
    summary[key] = value === null || typeof value === "number" || typeof value === "string" ? value : (() => { throw new Malformed(); })();
  }
  return {
    id: text(source.id),
    kind: oneOf(source.kind, LOAD_KINDS),
    name: textOrNull(source.name),
    would: oneOf(source.state, ["live", "would"] as const) === "would",
    in_house: flag(source.in_house),
    kw,
    level,
    runs: list(source.runs).map(decodeRun),
    summary,
    does_not_fit: source.does_not_fit === null ? null : decodeDoesNotFit(source.does_not_fit),
  };
}

/** `spotnav/get_plan`'s answer, checked (unknown keys rejected in the tests, ignored in production). */
export function decodePlan(raw: unknown): PlanDecodeResult {
  try {
    const source = record(raw);
    if (source.api_version !== PLAN_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    exactKeys(source, PLAN_KEYS);
    const window = record(source.window);
    exactKeys(window, WINDOW_KEYS);
    const house = record(source.house);
    exactKeys(house, HOUSE_KEYS);
    const summary = record(source.summary);
    exactKeys(summary, SUMMARY_KEYS);
    const money = source.money === null ? null : record(source.money);
    if (money !== null) {
      exactKeys(money, MONEY_KEYS);
    }
    const hours = list(source.hours).map(decodeHour);
    if (hours.length === 0) {
      throw new Malformed();
    }
    const peak = source.month_peak === null ? null : record(source.month_peak);
    const peakStart = textOrNull(summary.peak_start);
    return {
      ok: true,
      value: {
        site_id: text(source.site_id),
        time_zone: text(source.time_zone),
        money: money === null ? null : { currency: textOrNull(money.currency), major_unit: textOrNull(money.major_unit), minor_unit: textOrNull(money.minor_unit) },
        hours,
        loads: list(source.loads).map((load) => decodeLoad(load, hours.length)),
        power_tariff: flag(source.power_tariff),
        would: flag(source.would),
        month_peak: peak === null ? null : { kw: numberOrNull(peak.kw) ?? 0, startMs: instant(peak.start) },
        summary: {
          cost: numberOrNull(summary.cost),
          peak_kw: numberOrNull(summary.peak_kw),
          peakMs: peakStart === null ? null : instant(peakStart),
          peak_ceiling_kw: numberOrNull(summary.peak_ceiling_kw),
          peak_ceiling: ceilingOrNull(summary.peak_ceiling),
          over: flag(summary.over),
        },
        sun_in_house: flag(house.sun_in_house),
      },
    };
  } catch (error) {
    if (error instanceof Malformed) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}

// ------------------------------------------------------------------------------------------------ the drawing

export const PLAN_CLASSES = {
  panel: "spotnav-plan",
  heading: "spotnav-plan-heading",
  lede: "spotnav-plan-lede",
  stats: "spotnav-plan-stats",
  stat: "spotnav-plan-stat",
  statValue: "spotnav-plan-stat-value",
  statNote: "spotnav-plan-stat-note",
  chart: "spotnav-plan-chart",
  legend: "spotnav-plan-legend",
  swatch: "spotnav-plan-swatch",
  note: "spotnav-plan-note",
  lanes: "spotnav-plan-lanes",
  lane: "spotnav-plan-lane",
  laneName: "spotnav-plan-lane-name",
  track: "spotnav-plan-track",
  block: "spotnav-plan-block",
  reason: "spotnav-plan-reason",
  words: "spotnav-plan-words",
  would: "spotnav-plan-would",
  fit: "spotnav-plan-fit",
  fitChoices: "spotnav-plan-fit-choices",
  toggle: "spotnav-plan-toggle",
  chevron: "spotnav-plan-chevron",
  hint: "spotnav-plan-hint",
} as const;
const P = PLAN_CLASSES;

/** A layer of the stacked chart: its colour (a custom property) and its words. */
type Layer = "house" | "heat" | "water_heater" | "car" | "battery_in" | "sun" | "battery_out";
const LAYER_COLOUR: Record<Layer, string> = {
  house: "var(--spotnav-house-colour)",
  heat: "var(--spotnav-heat-colour)",
  water_heater: "var(--spotnav-plan-water-colour)",
  car: "var(--spotnav-flow-car)",
  battery_in: "var(--spotnav-battery-colour)",
  sun: "var(--spotnav-sun-colour)",
  battery_out: "var(--spotnav-flow-battery)",
};
const LAYER_WORD: Record<Layer, TranslationKey> = {
  house: "house.plan.layer.house",
  heat: "house.plan.layer.heat",
  water_heater: "house.plan.layer.waterHeater",
  car: "house.plan.layer.car",
  battery_in: "house.plan.layer.batteryIn",
  sun: "house.plan.layer.sun",
  battery_out: "house.plan.layer.batteryOut",
};

export const PLAN_STYLES = `
  .${P.panel} {
    --spotnav-plan-water-colour: var(--spotnav-water-heater, #c8607a);
    --spotnav-plan-ceiling: var(--error-color, #db4437);
    --spotnav-plan-cheap: color-mix(in srgb, var(--success-color, #43a047) 35%, transparent);
    --spotnav-plan-mid: color-mix(in srgb, var(--warning-color, #ffa600) 35%, transparent);
    --spotnav-plan-dear: color-mix(in srgb, var(--error-color, #db4437) 35%, transparent);
    display: grid;
    gap: 14px;
    min-width: 0;
  }
  .${P.heading} { margin: 0; font-size: 1rem; font-weight: 600; overflow-wrap: break-word; }
  .${P.lede}, .${P.note}, .${P.reason}, .${P.statNote} { margin: 0; font-size: 0.82rem; color: var(--secondary-text-color); overflow-wrap: break-word; }
  .${P.stats} { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
  .${P.stat} { min-width: 0; display: grid; gap: 2px; }
  .${P.stat} > span:first-child { font-size: 0.8rem; color: var(--secondary-text-color); }
  .${P.statValue} { font-size: 1.25rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .${P.statValue}[data-over="true"] { color: var(--spotnav-plan-ceiling); }
  .${P.statValue}[data-over="false"] { color: var(--success-color, #43a047); }
  .${P.chart} { display: block; width: 100%; height: auto; overflow: visible; }
  .${P.chart} text { fill: var(--secondary-text-color); font-size: 10px; }
  .${P.chart} .spotnav-plan-grid { stroke: var(--divider-color, #e0e0e0); stroke-width: 1; }
  .${P.chart} .spotnav-plan-zero { stroke: var(--secondary-text-color); stroke-width: 1; }
  .${P.chart} .spotnav-plan-net { fill: none; stroke: var(--primary-text-color); stroke-width: 2; }
  .${P.chart} .spotnav-plan-ceiling { fill: none; stroke: var(--spotnav-plan-ceiling); stroke-width: 1.6; stroke-dasharray: 5 4; }
  .${P.chart} text.spotnav-plan-ceiling-label { fill: var(--spotnav-plan-ceiling); }
  .${P.chart} .spotnav-plan-over { fill: var(--spotnav-plan-ceiling); }
  .${P.chart} .spotnav-plan-midnight { stroke: var(--secondary-text-color); stroke-dasharray: 2 3; }
  .${P.chart} rect[data-would="true"], .${P.block}[data-would="true"] { fill-opacity: 0.45; opacity: 0.6; }
  .${P.legend} { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 0.8rem; }
  .${P.swatch} { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
  .${P.lanes} { display: grid; gap: 6px; }
  .${P.lane} { display: grid; grid-template-columns: minmax(64px, 96px) minmax(0, 1fr); gap: 2px 8px; align-items: center; font-size: 0.86rem; }
  .${P.laneName} { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .${P.track} { position: relative; height: 18px; border-radius: 5px; background: var(--divider-color, #e0e0e0); overflow: hidden; }
  .${P.block} { position: absolute; top: 2px; bottom: 2px; border-radius: 3px; }
  .${P.block}[data-level="-1"] { background: transparent !important; outline: 2px dashed var(--spotnav-heat-colour); outline-offset: -3px; }
  .${P.reason}, .${P.fit} { grid-column: 2; }
  .${P.fit} { display: grid; gap: 6px; margin: 2px 0 0; font-size: 0.86rem; }
  .${P.fitChoices} { display: flex; flex-wrap: wrap; gap: 6px; }
  .${P.fitChoices} button { font: inherit; font-size: 0.82rem; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--divider-color, #e0e0e0); background: transparent; color: var(--primary-color); cursor: pointer; }
  .${P.fitChoices} button:disabled { opacity: 0.5; cursor: default; }
  .${P.words} { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
  .${P.words} li { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 8px; font-size: 0.88rem; }
  .${P.words} time { color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .${P.would} { display: inline-block; margin-left: 6px; padding: 0 6px; border: 1px solid var(--divider-color, #e0e0e0); border-radius: 999px; font-size: 0.72rem; color: var(--secondary-text-color); }
  .${P.toggle} { display: inline-flex; align-items: center; gap: 8px; max-width: 100%; margin: 0; padding: 2px 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
  .${P.toggle}:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; border-radius: 4px; }
  .${P.chevron} { flex: none; width: 6px; height: 6px; border-right: 2px solid currentColor; border-bottom: 2px solid currentColor; transform: rotate(-45deg); transition: transform 0.15s; }
  .${P.toggle}[aria-expanded="true"] .${P.chevron} { transform: rotate(45deg); }
  .${P.hint} { font-weight: 400; color: var(--secondary-text-color); }
  @media (max-width: 480px) { .${P.words} li { grid-template-columns: 78px minmax(0, 1fr); } }
`;

export interface PlanRenderInput {
  doc: Document;
  language: Language;
  plan: PlanAnswer;
  width: number;
  /**
   * A choice on a car's "does not fit" line (`takes_what_fits` or `accept_peak`, for the charger `chargerId`); without
   * it the line is said with no buttons. "Later departure" is the charger card's: its departure editor is there.
   */
  onDoesNotFit?: (chargerId: string, code: DoesNotFitChoiceCode) => void;
  /** Where the folded sections are remembered; this browser's storage unless a test hands in its own. */
  store?: () => PreferenceStore | null;
}

/** The sections opened or folded on this page, so a choice holds across redraws where nothing can be stored. */
const pageOpen = new Map<string, boolean>();

/**
 * A section's heading as a button that folds `content` away, with a chevron and, while folded, a hint of what is
 * inside ("Per last · 4"). With `forced` the section is open under a plain heading and does not fold.
 */
function foldable(input: PlanRenderInput, section: PlanSection, title: string, hint: string, content: HTMLElement, forced: boolean): HTMLElement[] {
  const { doc } = input;
  const heading = element(doc, "h4", P.heading);
  heading.dataset["section"] = section;
  const wrap = element(doc, "div");
  wrap.dataset["section"] = section;
  wrap.append(content);
  if (forced) {
    heading.textContent = title;
    return [heading, wrap];
  }
  const store = input.store ?? browserStore;
  const site = input.plan.site_id;
  const open = pageOpen.get(`${section}.${site}`) ?? readPlanSectionOpen(section, site, store);
  const button = element(doc, "button", P.toggle) as HTMLButtonElement;
  button.type = "button";
  const hintNode = element(doc, "span", P.hint, ` · ${hint}`);
  button.append(element(doc, "span", P.chevron), element(doc, "span", undefined, title), hintNode);
  const show = (next: boolean): void => {
    button.setAttribute("aria-expanded", String(next));
    hintNode.hidden = next;
    wrap.hidden = !next;
  };
  show(open);
  button.addEventListener("click", () => {
    const next = button.getAttribute("aria-expanded") !== "true";
    show(next);
    pageOpen.set(`${section}.${site}`, next);
    writePlanSectionOpen(section, site, next, store);
  });
  heading.append(button);
  return [heading, wrap];
}

/** Forget this page's fold choices (tests). */
export function resetPlanSections(): void {
  pageOpen.clear();
}

function context(language: Language, plan: PlanAnswer): FormatContext {
  return { language, timeZone: plan.time_zone, unit: "kWh", currency: plan.money?.currency ?? null, majorUnit: plan.money?.major_unit ?? null };
}

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, tag: K, attributes: Record<string, string | number>, className?: string): SVGElementTagNameMap[K] {
  const node = doc.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, String(value));
  }
  if (className !== undefined) {
    node.setAttribute("class", className);
  }
  return node;
}

/** Whether the words have this key (the run and reason keys are built from the plan's codes). */
function known(key: string): key is TranslationKey {
  return Object.prototype.hasOwnProperty.call(en, key);
}

/** The ceiling's words ("Effekttak 5,0 kW"). */
export function ceilingWords(language: Language, kind: CeilingKind, kw: number): string {
  const key = `house.plan.ceiling.${kind}`;
  return known(key) ? translate(language, key, { kw: kwText(language, kw) }) : kwText(language, kw);
}

/** What a load is called: its own name, else its kind's word. */
function loadName(language: Language, load: PlanLoad): string {
  if (load.kind === "home_battery") {
    return translate(language, "house.plan.load.home_battery");
  }
  const key = `house.plan.load.${load.kind}`;
  return load.name ?? (known(key) ? translate(language, key) : load.kind);
}

/** A span of hours as the clock says it ("01–07"). */
function span(ctx: FormatContext, startMs: number, endMs: number): string {
  return `${hourLabel(ctx, startMs)}–${hourLabel(ctx, endMs)}`;
}

/** One run in words ("Bilen laddar 14,8 kWh, 3,7 kW"). */
export function runWords(language: Language, load: PlanLoad, run: PlanRun): string {
  const params: Record<string, string> = {
    name: loadName(language, load),
    kw: run.kw === null ? "" : kwText(language, Math.abs(run.kw)),
    kwh: run.kwh === null ? "" : kwhText(language, run.kwh),
  };
  const key = `house.plan.run.${load.kind}.${run.what}`;
  let words = translate(language, known(key) ? key : "house.plan.run.other", params);
  const why = run.reason === null ? null : `house.plan.reason.${run.reason}`;
  if (why !== null && known(why)) {
    words = `${words}: ${translate(language, why)}`;
  }
  return words;
}

/** A load's one line of why, from its plan's figures. */
export function laneReason(language: Language, ctx: FormatContext, load: PlanLoad): string {
  const s = load.summary;
  const num = (key: string): number => (typeof s[key] === "number" ? (s[key] as number) : 0);
  let line: string;
  if (load.runs.length === 0) {
    line = translate(language, "house.plan.lane.none");
  } else if (load.kind === "car") {
    const first = load.runs[0]!;
    const last = load.runs[load.runs.length - 1]!;
    line = translate(language, "house.plan.lane.car", { kwh: kwhText(language, num("kwh")), hours: span(ctx, first.startMs, last.endMs) });
  } else if (load.kind === "home_battery") {
    const parts = [translate(language, "house.plan.lane.batteryCover", { kwh: kwhText(language, num("cover_kwh")) })];
    if (num("grid_charge_kwh") > 0) {
      parts.push(translate(language, "house.plan.lane.batteryGrid", { kwh: kwhText(language, num("grid_charge_kwh")) }));
    }
    if (num("charge_kwh") - num("grid_charge_kwh") > 0.05) {
      parts.push(translate(language, "house.plan.lane.batteryCharge", { kwh: kwhText(language, num("charge_kwh") - num("grid_charge_kwh")) }));
    }
    line = parts.join(" · ");
  } else if (load.kind === "heat") {
    line = translate(language, "house.plan.lane.heat", { lowered: String(num("lowered_hours")), raised: String(num("raised_hours")) });
  } else if (load.kind === "heat_zone") {
    line = translate(language, "house.plan.lane.zone", { lowered: String(num("lowered_hours")) });
  } else {
    line = translate(language, "house.plan.lane.waterHeater", { hours: formatFixed(language, num("on_hours"), 0) });
  }
  return load.would ? `${translate(language, "house.plan.wouldPrefix")} ${line}` : line;
}

function layerOf(load: PlanLoad): Layer | null {
  switch (load.kind) {
    case "car":
      return "car";
    case "heat":
      return "heat";
    case "water_heater":
      return "water_heater";
    default:
      return null;
  }
}

/** The price's tone for the strip: the cheapest third, the middle, the dearest third of the window's prices. */
export function priceTones(hours: readonly PlanHour[]): ("cheap" | "mid" | "dear" | null)[] {
  const known = hours.map((hour) => hour.price).filter((price): price is number => price !== null).sort((a, b) => a - b);
  if (known.length === 0) {
    return hours.map(() => null);
  }
  const low = known[Math.floor(known.length / 3)]!;
  const high = known[Math.floor((known.length * 2) / 3)]!;
  return hours.map((hour) => (hour.price === null ? null : hour.price < low ? "cheap" : hour.price < high ? "mid" : "dear"));
}

function chart(input: PlanRenderInput, ctx: FormatContext): SVGSVGElement {
  const { doc, language, plan } = input;
  const width = Math.max(280, Math.round(input.width));
  const height = 230;
  const left = 30;
  const right = 6;
  const top = 18;
  const bottom = 44;
  const n = plan.hours.length;
  const step = (width - left - right) / n;
  const bar = Math.max(1, step - 2);
  const battery = plan.loads.find((load) => load.kind === "home_battery") ?? null;
  // Per hour: the stack above zero and below it.
  const above: { layer: Layer; kw: number; would: boolean }[][] = [];
  const below: { layer: Layer; kw: number; would: boolean }[][] = [];
  let max = 1;
  let min = 0;
  plan.hours.forEach((hour, index) => {
    const up: { layer: Layer; kw: number; would: boolean }[] = [];
    const down: { layer: Layer; kw: number; would: boolean }[] = [];
    const inside = plan.loads.filter((load) => load.in_house).reduce((sum, load) => sum + Math.max(0, load.kw[index] ?? 0), 0);
    up.push({ layer: "house", kw: Math.max(0, (hour.house_kw ?? 0) - inside), would: false });
    for (const load of plan.loads) {
      const layer = layerOf(load);
      const kw = load.kw[index] ?? 0;
      if (layer !== null && kw > 0) {
        up.push({ layer, kw, would: load.would });
      }
    }
    const batteryKw = battery?.kw[index] ?? 0;
    if (batteryKw > 0) {
      up.push({ layer: "battery_in", kw: batteryKw, would: battery!.would });
    }
    if (hour.sun_kw !== null && hour.sun_kw > 0 && !plan.sun_in_house) {
      down.push({ layer: "sun", kw: hour.sun_kw, would: false });
    }
    if (batteryKw < 0) {
      down.push({ layer: "battery_out", kw: -batteryKw, would: battery!.would });
    }
    above.push(up);
    below.push(down);
    max = Math.max(max, up.reduce((sum, part) => sum + part.kw, 0), hour.net_kw ?? 0, hour.ceiling_kw !== null && hour.ceiling_kw < 30 ? hour.ceiling_kw : 0);
    min = Math.min(min, -down.reduce((sum, part) => sum + part.kw, 0), hour.net_kw ?? 0);
  });
  max = Math.ceil(max * 1.08);
  min = min < 0 ? Math.floor(min * 1.08) : 0;
  const y = (kw: number): number => top + ((height - top - bottom) * (max - kw)) / (max - min);
  const x = (index: number): number => left + step * index;

  const root = svg(doc, "svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" }, P.chart);
  root.setAttribute("aria-label", translate(language, "house.plan.chartLabel"));
  const gridStep = max - min > 12 ? 4 : 2;
  for (let kw = Math.ceil(min / gridStep) * gridStep; kw <= max; kw += gridStep) {
    root.append(svg(doc, "line", { x1: left, x2: width - right, y1: y(kw), y2: y(kw) }, kw === 0 ? "spotnav-plan-zero" : "spotnav-plan-grid"));
    const label = svg(doc, "text", { x: left - 4, y: y(kw) + 3, "text-anchor": "end" });
    label.textContent = String(kw);
    root.append(label);
  }
  const unit = svg(doc, "text", { x: left - 4, y: top - 6, "text-anchor": "end" });
  unit.textContent = "kW";
  root.append(unit);
  const tones = priceTones(plan.hours);
  plan.hours.forEach((hour, index) => {
    const name = clock(ctx, hour.startMs);
    let acc = 0;
    for (const part of above[index]!) {
      const rect = svg(doc, "rect", { x: x(index) + 1, y: y(acc + part.kw), width: bar, height: Math.max(0, y(acc) - y(acc + part.kw)), fill: LAYER_COLOUR[part.layer] });
      rect.dataset["layer"] = part.layer;
      rect.dataset["would"] = String(part.would);
      const title = svg(doc, "title", {});
      title.textContent = `${name} ${translate(language, LAYER_WORD[part.layer])} ${kwText(language, part.kw)}`;
      rect.append(title);
      root.append(rect);
      acc += part.kw;
    }
    let neg = 0;
    for (const part of below[index]!) {
      const rect = svg(doc, "rect", { x: x(index) + 1, y: y(neg), width: bar, height: Math.max(0, y(neg - part.kw) - y(neg)), fill: LAYER_COLOUR[part.layer] });
      rect.dataset["layer"] = part.layer;
      rect.dataset["would"] = String(part.would);
      const title = svg(doc, "title", {});
      title.textContent = `${name} ${translate(language, LAYER_WORD[part.layer])} ${kwText(language, part.kw)}`;
      rect.append(title);
      root.append(rect);
      neg -= part.kw;
    }
    const tone = tones[index];
    if (tone !== null && tone !== undefined) {
      const strip = svg(doc, "rect", { x: x(index) + 1, y: height - bottom + 16, width: bar, height: 8, rx: 2, fill: `var(--spotnav-plan-${tone})` });
      strip.dataset["tone"] = tone;
      root.append(strip);
    }
    const local = hourLabel(ctx, hour.startMs);
    if (local === "00" && index > 0) {
      root.append(svg(doc, "line", { x1: x(index), x2: x(index), y1: top, y2: height - bottom + 4 }, "spotnav-plan-midnight"));
      const cue = svg(doc, "text", { x: x(index) + 3, y: top - 6 });
      cue.textContent = dayCue(ctx, hour.startMs);
      root.append(cue);
    }
    if (index % 3 === 0) {
      const tick = svg(doc, "text", { x: x(index) + step / 2, y: height - bottom + 12, "text-anchor": "middle" });
      tick.textContent = local;
      root.append(tick);
    }
  });
  const strip = svg(doc, "text", { x: left, y: height - 6 });
  strip.textContent = translate(language, "house.plan.priceStrip");
  root.append(strip);
  // The ceiling, a step line where it changes, labelled once at its last hour.
  let ceiling = "";
  plan.hours.forEach((hour, index) => {
    if (hour.ceiling_kw === null || hour.ceiling_kw > max) {
      return;
    }
    ceiling += `${ceiling === "" || plan.hours[index - 1]?.ceiling_kw === null ? "M" : "L"}${x(index)} ${y(hour.ceiling_kw)}L${x(index + 1)} ${y(hour.ceiling_kw)}`;
  });
  if (ceiling !== "") {
    root.append(svg(doc, "path", { d: ceiling }, "spotnav-plan-ceiling"));
    const lastHour = [...plan.hours].reverse().find((hour) => hour.ceiling_kw !== null && hour.ceiling !== null && hour.ceiling_kw <= max);
    if (lastHour !== undefined) {
      const label = svg(doc, "text", { x: width - right, y: y(lastHour.ceiling_kw!) - 4, "text-anchor": "end" }, "spotnav-plan-ceiling-label");
      label.textContent = ceilingWords(language, lastHour.ceiling!, lastHour.ceiling_kw!);
      root.append(label);
    }
  }
  // The net from the grid.
  let net = "";
  plan.hours.forEach((hour, index) => {
    if (hour.net_kw === null) {
      return;
    }
    net += `${net === "" ? "M" : "L"}${x(index) + step / 2} ${y(hour.net_kw)}`;
  });
  if (net !== "") {
    root.append(svg(doc, "path", { d: net }, "spotnav-plan-net"));
  }
  plan.hours.forEach((hour, index) => {
    if (hour.net_kw !== null && hour.ceiling_kw !== null && hour.net_kw > hour.ceiling_kw + 0.01) {
      root.append(svg(doc, "circle", { cx: x(index) + step / 2, cy: y(hour.net_kw), r: 3.5 }, "spotnav-plan-over"));
    }
  });
  return root;
}

function stat(doc: Document, label: string, value: string, note: string, over: boolean | null = null): HTMLElement {
  const box = element(doc, "div", P.stat);
  box.append(element(doc, "span", undefined, label));
  const figure = element(doc, "b", P.statValue, value);
  if (over !== null) {
    figure.dataset["over"] = String(over);
  }
  box.append(figure, element(doc, "span", P.statNote, note));
  return box;
}

function summary(input: PlanRenderInput, ctx: FormatContext): HTMLElement {
  const { doc, language, plan } = input;
  const s = plan.summary;
  const row = element(doc, "div", P.stats);
  row.dataset["part"] = "summary";
  row.append(
    stat(
      doc,
      translate(language, "house.plan.cost"),
      s.cost === null ? "–" : moneyMinorAsMajor(language, s.cost, plan.money),
      translate(language, "house.plan.costNote", { hours: String(plan.hours.length) }),
    ),
  );
  const peakNote =
    s.peakMs === null
      ? ""
      : translate(language, s.over ? "house.plan.peakOver" : s.peak_ceiling_kw === null ? "house.plan.peakAt" : "house.plan.peakUnder", { at: clock(ctx, s.peakMs) });
  row.append(stat(doc, translate(language, "house.plan.peak"), s.peak_kw === null ? "–" : kwText(language, s.peak_kw), peakNote, s.peak_ceiling_kw === null ? null : s.over));
  if (plan.month_peak !== null) {
    const at = plan.month_peak.startMs;
    row.append(
      stat(
        doc,
        translate(language, "house.plan.monthPeak"),
        kwText(language, plan.month_peak.kw),
        translate(language, "house.plan.monthPeakNote", { when: `${dayCue(ctx, at)} ${clock(ctx, at)}` }),
      ),
    );
  } else {
    row.append(stat(doc, translate(language, "house.plan.tariff"), "–", translate(language, "house.plan.noTariff")));
  }
  return row;
}

function legend(doc: Document, language: Language, plan: PlanAnswer): HTMLElement {
  const box = element(doc, "div", P.legend);
  const kinds = new Set(plan.loads.map((load) => load.kind));
  const layers: Layer[] = ["house"];
  if (kinds.has("heat")) layers.push("heat");
  if (kinds.has("water_heater")) layers.push("water_heater");
  if (kinds.has("car")) layers.push("car");
  if (kinds.has("home_battery")) layers.push("battery_in");
  if (plan.hours.some((hour) => hour.sun_kw !== null) && !plan.sun_in_house) layers.push("sun");
  if (kinds.has("home_battery")) layers.push("battery_out");
  for (const layer of layers) {
    const item = element(doc, "span");
    const swatch = element(doc, "i", P.swatch);
    swatch.style.background = LAYER_COLOUR[layer];
    item.append(swatch, doc.createTextNode(translate(language, LAYER_WORD[layer])));
    box.append(item);
  }
  const net = element(doc, "span");
  const line = element(doc, "i", P.swatch);
  line.style.background = "var(--primary-text-color)";
  line.style.height = "3px";
  net.append(line, doc.createTextNode(translate(language, "house.plan.layer.net")));
  box.append(net);
  return box;
}

function lanes(input: PlanRenderInput, ctx: FormatContext): HTMLElement {
  const { doc, language, plan } = input;
  const box = element(doc, "div", P.lanes);
  const n = plan.hours.length;
  for (const load of plan.loads) {
    const lane = element(doc, "div", P.lane);
    lane.dataset["load"] = load.kind;
    lane.dataset["would"] = String(load.would);
    lane.append(element(doc, "span", P.laneName, loadName(language, load)));
    const track = element(doc, "div", P.track);
    const layer = layerOf(load);
    for (let index = 0; index < n; index += 1) {
      const kw = load.kw[index] ?? 0;
      const level = load.level?.[index] ?? 0;
      let colour: string | null = null;
      if (load.kind === "home_battery") {
        colour = kw > 0.05 ? LAYER_COLOUR.battery_in : kw < -0.05 ? LAYER_COLOUR.battery_out : null;
      } else if (load.level !== null) {
        colour = level !== 0 ? LAYER_COLOUR.heat : null;
      } else if (kw > 0 || load.runs.some((run) => run.startMs <= plan.hours[index]!.startMs && plan.hours[index]!.startMs < run.endMs)) {
        colour = layer === null ? LAYER_COLOUR.house : LAYER_COLOUR[layer];
      }
      if (colour === null) {
        continue;
      }
      const block = element(doc, "div", P.block);
      block.style.left = `${(index / n) * 100}%`;
      block.style.width = `${100 / n}%`;
      block.style.background = colour;
      block.dataset["would"] = String(load.would);
      if (load.level !== null) {
        block.dataset["level"] = String(level);
      }
      track.append(block);
    }
    lane.append(track, element(doc, "p", P.reason, laneReason(language, ctx, load)));
    if (load.does_not_fit !== null) {
      lane.append(doesNotFitNode(input, ctx, load.id, load.does_not_fit));
    }
    box.append(lane);
  }
  const first = plan.hours[0]!.startMs;
  const marks = [0, 0.25, 0.5, 0.75].map((share) => clock(ctx, first + Math.round(share * n) * HOUR_MS));
  const foot = element(doc, "div", P.lane);
  foot.append(element(doc, "span"), element(doc, "p", P.note, `${marks.join(" · ")} → ${translate(language, "house.plan.rolling")}`));
  box.append(foot);
  return box;
}

/**
 * A car's "does not fit" line under its lane, with "Take what fits", "Later departure (14:00)" where a later hour fits
 * (without one the departure's editor is the charger card's) and "Allow the peak" (asked again before it is sent: it
 * may cost the month's peak).
 */
function doesNotFitNode(input: PlanRenderInput, ctx: FormatContext, chargerId: string, block: DoesNotFit): HTMLElement {
  const { doc, language, plan } = input;
  const box = element(doc, "div", P.fit);
  box.dataset["part"] = "does-not-fit";
  box.append(element(doc, "p", P.lede, doesNotFitText(ctx, block)));
  const choose = input.onDoesNotFit;
  if (choose === undefined) {
    return box;
  }
  const row = element(doc, "div", P.fitChoices);
  for (const choice of block.choices) {
    if (choice.code === "later" && choice.departureMs === null) {
      continue;
    }
    const button = element(doc, "button", undefined, choiceLabel(ctx, choice, plan.money));
    button.setAttribute("type", "button");
    button.dataset["choice"] = choice.code;
    let asked = choice.code !== "accept_peak";
    button.addEventListener("click", () => {
      if (!asked) {
        asked = true;
        button.textContent = translate(language, "doesNotFit.choice.acceptPeakAgain");
        return;
      }
      button.disabled = true;
      choose(chargerId, choice.code);
    });
    row.append(button);
  }
  if (row.childElementCount > 0) {
    box.append(row);
  }
  return box;
}

function words(input: PlanRenderInput, ctx: FormatContext): HTMLElement {
  const { doc, language, plan } = input;
  const items = plan.loads.flatMap((load) => load.runs.map((run) => ({ load, run }))).sort((a, b) => a.run.startMs - b.run.startMs);
  const listNode = element(doc, "ul", P.words);
  for (const { load, run } of items) {
    const item = element(doc, "li");
    item.dataset["load"] = load.kind;
    const time = element(doc, "time", undefined, span(ctx, run.startMs, run.endMs));
    time.setAttribute("datetime", new Date(run.startMs).toISOString());
    const sentence = element(doc, "span", undefined, runWords(language, load, run));
    if (load.would) {
      sentence.append(element(doc, "span", P.would, translate(language, "house.plan.would")));
    }
    item.append(time, sentence);
    listNode.append(item);
  }
  return listNode;
}

/** The Plan tab's content. */
export function planBody(input: PlanRenderInput): HTMLElement {
  const { doc, language, plan } = input;
  const ctx = context(language, plan);
  const panel = element(doc, "div", P.panel);
  panel.append(
    element(doc, "h3", P.heading, translate(language, "house.plan.title", { hours: String(plan.hours.length) })),
    element(doc, "p", P.lede, translate(language, "house.plan.lede")),
    summary(input, ctx),
  );
  if (plan.hours.every((hour) => hour.price === null)) {
    panel.append(element(doc, "p", P.note, translate(language, "house.plan.noPrices")));
  }
  panel.append(element(doc, "h4", P.heading, translate(language, "house.plan.fromGrid")), chart(input, ctx), legend(doc, language, plan));
  if (plan.would) {
    const note = element(doc, "p", P.note, translate(language, "house.plan.wouldNote"));
    note.dataset["note"] = "would";
    panel.append(note);
  }
  if (plan.loads.length > 0) {
    const choice = plan.loads.some((load) => load.does_not_fit !== null);
    panel.append(...foldable(input, "lanes", translate(language, "house.plan.perLoad"), String(plan.loads.length), lanes(input, ctx), choice));
  }
  const listed = words(input, ctx);
  const rows = listed.childElementCount;
  const content = rows === 0 ? element(doc, "p", P.note, translate(language, "house.plan.nothing")) : listed;
  const rowsKey: TranslationKey = `house.plan.rows.${pluralForm(language, rows)}`;
  panel.append(...foldable(input, "words", translate(language, "house.plan.inWords"), translate(language, rowsKey, { count: String(rows) }), content, false));
  return panel;
}
