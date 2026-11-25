// What the house used: the popover behind House in the energy flow (the card's front and the history's flow). It reads
// `spotnav/get_house_loads` when opened (the backend's shape is `custom_components/spotnav/site/house_loads.py`, its
// fixtures `tests/fixtures/house_loads/`) and shows the house's energy for the period as a pie: each of the Energy
// dashboard's individual devices by name, "Other metered" (the small meters) and "Unknown" (the house less all meters).
//
// * A meter under `SMALL_SHARE` of the whole, or past the `SLOTS` colours, goes into Other metered (a light grey) and
//   is named under the pie, so nothing is hidden; a meter with nothing in the period is named there too.
// * The whole is the house, or the meters where they add up to more (Unknown is then 0, and a line says so).
// * The colours are one validated categorical order (`--spotnav-pie-1` …, readable on a light and a dark card), given
//   to the shown meters by their statistic id, so a meter keeps its colour while the same meters are shown; Other is
//   two neutral greys. Every figure is also in the list beside the pie (name, kWh, %), so colour never carries it alone.
//
// * `kind: "heat"` is the same pie behind Heat (where the heat holds more than one source): the heat pump's meter,
//   each heat zone by its thermostat's name and each meter moved to heat; its centre says Heat.
// * Where a meter belongs (administrators only): a meter SpotNav could not tie to a load by itself is the house's
//   until it is moved. Tapping its slice, or its row's button by the keyboard, opens "Belongs to: House (default) /
//   Heat / Car / Don't count", written with `spotnav/update_site_settings` (`meter_belongs`, compare-and-set on the
//   mapping last read). A moved meter is marked "changed by you" in the pie it went to, and the meters moved out of
//   the house pie are listed under it with a button to bring each back.
// * A meter with hours that cannot be (above what the block used in the hour, or above the main fuse) is counted
//   without them, and one line per meter says so and where Home Assistant repairs its statistics.
//
// Text is written with `textContent` only.

import { getHouseLoads, SpotnavApiError, UNSUPPORTED_API_VERSION, updateMeterBelongs } from "./api";
import { createDialog, type DialogHandle } from "./dialog";
import { dateLabel, formatNumber, kwhFineText } from "./format";
import { HOUSE_LOADS_API_VERSION } from "./house-model";
import { translate, type Language, type TranslationKey } from "./i18n";
import type { HomeAssistantLike } from "./types";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { bad, record, text, number, numberOrNull } from "./decode-primitives";
import { element } from "./dom";
import { LOAD_COLOURS, type OwnLoadKind } from "./load-colours";

const SVG_NS = "http://www.w3.org/2000/svg";

/** A meter under this share of the whole is put into Other metered. */
export const SMALL_SHARE = 0.015;
/** The colours a pie has for meters; any further meter goes into Other metered. */
export const SLOTS = 8;
/** The pie's size (px). */
const PIE = 148;
const OUTER = 72;
const INNER = 44;

export const LOADS_CLASSES = {
  root: "spotnav-house-loads",
  caption: "spotnav-house-loads-caption",
  figure: "spotnav-house-loads-figure",
  pie: "spotnav-house-loads-pie",
  slice: "spotnav-house-loads-slice",
  centre: "spotnav-house-loads-centre",
  centreSmall: "spotnav-house-loads-centre-small",
  list: "spotnav-house-loads-list",
  row: "spotnav-house-loads-row",
  swatch: "spotnav-house-loads-swatch",
  name: "spotnav-house-loads-name",
  value: "spotnav-house-loads-value",
  note: "spotnav-house-loads-note",
  choose: "spotnav-house-loads-choose",
  mark: "spotnav-house-loads-mark",
  menu: "spotnav-house-loads-menu",
  option: "spotnav-house-loads-option",
  moved: "spotnav-house-loads-moved",
  action: "spotnav-house-loads-action",
} as const;

const L = LOADS_CLASSES;

export const LOADS_STYLES = `
  .${L.root} {
    --spotnav-pie-1: var(--spotnav-pie-1-colour, #3987e5);
    --spotnav-pie-2: var(--spotnav-pie-2-colour, #d95926);
    --spotnav-pie-3: var(--spotnav-pie-3-colour, #199e70);
    --spotnav-pie-4: var(--spotnav-pie-4-colour, #c98500);
    --spotnav-pie-5: var(--spotnav-pie-5-colour, #d55181);
    --spotnav-pie-6: var(--spotnav-pie-6-colour, #9085e9);
    --spotnav-pie-7: var(--spotnav-pie-7-colour, #2aa3c4);
    --spotnav-pie-8: var(--spotnav-pie-8-colour, #8aa02c);
    --spotnav-pie-other-metered: var(--spotnav-pie-other-metered-colour, #b9bdc4);
    --spotnav-pie-unknown: var(--spotnav-pie-unknown-colour, #747982);
    --spotnav-pie-inverter: var(--spotnav-pie-inverter-colour, #3f7f7a);
    min-width: 0;
  }
  .${L.caption} {
    margin: 0 0 8px;
    font-size: 0.9rem;
  }
  .${L.figure} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 12px 16px;
    margin: 0;
  }
  .${L.pie} {
    flex: 0 0 auto;
    display: block;
  }
  .${L.slice} {
    stroke: var(--ha-card-background, var(--card-background-color, #ffffff));
    stroke-width: 2px;
  }
  .${L.centre} {
    fill: var(--primary-text-color, #212121);
    font-size: 15px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .${L.centreSmall} {
    fill: var(--secondary-text-color, #727272);
    font-size: 12px;
  }
  .${L.list} {
    flex: 1 1 200px;
    min-width: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .${L.row} {
    display: grid;
    grid-template-columns: 10px minmax(0, 1fr) auto;
    align-items: baseline;
    gap: 8px;
    padding: 3px 0;
    font-size: 0.88rem;
  }
  .${L.swatch} {
    width: 10px;
    height: 10px;
    border-radius: 3px;
    align-self: center;
  }
  .${L.name} {
    overflow-wrap: anywhere;
  }
  .${L.value} {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    text-align: right;
  }
  .${L.note} {
    margin: 8px 0 0;
    font-size: 0.82rem;
  }
  .${L.choose} {
    all: unset;
    cursor: pointer;
    overflow-wrap: anywhere;
    text-decoration: underline dotted;
    text-underline-offset: 3px;
  }
  .${L.choose}:focus-visible,
  .${L.option}:focus-visible,
  .${L.action}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${L.mark} {
    margin-left: 6px;
    font-size: 0.75rem;
    color: var(--secondary-text-color, #727272);
  }
  .${L.slice}[data-choosable] {
    cursor: pointer;
  }
  .${L.menu} {
    margin: 10px 0 0;
    padding: 8px 10px;
    border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
    border-radius: 8px;
    min-width: 0;
  }
  .${L.menu} legend {
    font-size: 0.85rem;
    padding: 0 4px;
  }
  .${L.menu} div {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .${L.option},
  .${L.action} {
    font: inherit;
    font-size: 0.85rem;
    padding: 4px 10px;
    border-radius: 14px;
    border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.2));
    background: transparent;
    color: var(--primary-text-color, #212121);
    cursor: pointer;
  }
  .${L.option}[aria-pressed="true"] {
    background: var(--primary-color, #03a9f4);
    border-color: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #ffffff);
  }
  .${L.moved} {
    margin: 8px 0 0;
    padding: 0;
    list-style: none;
    font-size: 0.85rem;
  }
  .${L.moved} li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
    padding: 2px 0;
  }
`;

// ------------------------------------------------------------------------------------------------ the answer

export type LoadsPeriod = "day" | "month" | "year";
export type LoadsKind = "house" | "heat";
/** Where a meter belongs: the house (the default, never stored), the heat, the car, or nowhere. */
export type Belongs = "house" | "heat" | "car" | "none";
export const BELONGS: readonly Belongs[] = ["house", "heat", "car", "none"];

export interface HouseLoad {
  statistic_id: string;
  name: string;
  kwh: number;
  /** An Energy dashboard device (`meter`, whose belonging may be chosen), the heat pump's meter, a heat zone, or a
   * part of heat SpotNav counted itself (`pool_heat`, `pool_circulation`, `spa`, `sauna`: drawn in their own colours,
   * `load-colours.ts`). */
  source: "meter" | "heat_pump" | "zone" | OwnLoadKind;
  /** The household moved it here (its belonging is not the default). */
  changed: boolean;
  /** Some of its energy is an estimate (a part without its own trusted power sensor, on time at its power): always
   * named "(estimated)". `false` from a backend before it. */
  estimated: boolean;
  /** The estimate the house had no room for (left in the house), 0 when it fit or from a backend before it. */
  clamped_kwh: number;
}

export interface MovedLoad {
  statistic_id: string;
  name: string;
  belongs: Exclude<Belongs, "house">;
}

export interface ImplausibleLoad {
  statistic_id: string;
  name: string;
  /** The hours not counted. */
  hours: number;
  /** No plausible hour was left: the meter is not in the pie. */
  left_out: boolean;
}

export interface HouseLoads {
  site_id: string;
  kind: LoadsKind;
  period: LoadsPeriod;
  date: string;
  end: string;
  through: string | null;
  house_kwh: number | null;
  meters_kwh: number;
  other_kwh: number | null;
  over_kwh: number;
  /** What a hybrid's inverter used of the house (its losses and standby), taken out of what no meter explains;
   * `null` where the backend cannot count it, or from a backend before it. */
  inverter_kwh: number | null;
  /** What no meter explains, the inverter left out: `other_kwh` from a backend before it. */
  unknown_kwh: number | null;
  /** The estimates left out because the house had no room for them, summed; 0 from a backend before it. */
  clamped_kwh: number;
  loads: HouseLoad[];
  moved: MovedLoad[];
  /** The stored choices, as read (the compare-and-set's `expected`). */
  belongs: Record<string, Exclude<Belongs, "house">>;
  implausible: ImplausibleLoad[];
}

export type HouseLoadsDecode = { ok: true; value: HouseLoads } | { ok: false; failure: "unsupported" | "malformed" };

function day(value: unknown): string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : bad();
}

function period(value: unknown): LoadsPeriod {
  return value === "day" || value === "month" || value === "year" ? value : bad();
}

function movedBelongs(value: unknown): Exclude<Belongs, "house"> {
  return value === "heat" || value === "car" || value === "none" ? value : bad();
}

function sourceOf(value: unknown): HouseLoad["source"] {
  if (value === undefined || value === "meter") {
    return "meter";
  }
  return value === "heat_pump" || value === "zone" || (typeof value === "string" && Object.prototype.hasOwnProperty.call(LOAD_COLOURS, value))
    ? (value as HouseLoad["source"])
    : bad();
}

/** A list a backend before it did not send: empty. */
function optionalList(value: unknown): unknown[] {
  return value === undefined ? [] : Array.isArray(value) ? value : bad();
}

/** The answer, strictly typed; extra keys are allowed (additive fields), a missing or mistyped one is not. */
export function decodeHouseLoads(raw: unknown): HouseLoadsDecode {
  try {
    const source = record(raw);
    if (source.api_version !== HOUSE_LOADS_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    if (!Array.isArray(source.loads)) {
      bad();
    }
    return {
      ok: true,
      value: {
        site_id: text(source.site_id),
        kind: source.kind === undefined || source.kind === "house" ? "house" : source.kind === "heat" ? "heat" : bad(),
        period: period(source.period),
        date: day(source.date),
        end: day(source.end),
        through: source.through === null ? null : text(source.through),
        house_kwh: numberOrNull(source.house_kwh),
        meters_kwh: number(source.meters_kwh),
        other_kwh: numberOrNull(source.other_kwh),
        over_kwh: number(source.over_kwh),
        inverter_kwh: source.inverter_kwh === undefined ? null : numberOrNull(source.inverter_kwh),
        unknown_kwh: source.unknown_kwh === undefined ? numberOrNull(source.other_kwh) : numberOrNull(source.unknown_kwh),
        clamped_kwh: source.clamped_kwh === undefined ? 0 : Math.max(0, number(source.clamped_kwh)),
        loads: (source.loads as unknown[]).map((item) => {
          const load = record(item);
          return {
            statistic_id: text(load.statistic_id),
            name: text(load.name),
            kwh: Math.max(0, number(load.kwh)),
            source: sourceOf(load.source),
            changed: load.changed === undefined ? false : load.changed === true ? true : load.changed === false ? false : bad(),
            estimated: load.estimated === undefined ? false : load.estimated === true ? true : load.estimated === false ? false : bad(),
            clamped_kwh: load.clamped_kwh === undefined ? 0 : Math.max(0, number(load.clamped_kwh)),
          };
        }),
        moved: optionalList(source.moved).map((item) => {
          const row = record(item);
          return { statistic_id: text(row.statistic_id), name: text(row.name), belongs: movedBelongs(row.belongs) };
        }),
        belongs: Object.fromEntries(
          Object.entries(source.belongs === undefined ? {} : record(source.belongs)).map(([key, value]) => [key, movedBelongs(value)]),
        ),
        implausible: optionalList(source.implausible).map((item) => {
          const row = record(item);
          const hours = number(row.hours);
          return { statistic_id: text(row.statistic_id), name: text(row.name), hours: Math.max(0, Math.round(hours)), left_out: row.left_out === true };
        }),
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

// ------------------------------------------------------------------------------------------------ the slices

export interface PieSlice {
  /** The meter's statistic id, or `other-metered` (the small meters), `inverter` (a hybrid's inverter, its losses and
   * standby) or `unknown` (no meter explains it). */
  key: string;
  name: string;
  kwh: number;
  /** Of the whole, 0–1. */
  share: number;
  /** A CSS colour (a custom property). */
  colour: string;
}

export interface PieModel {
  /** The house, or the meters where they add up to more. */
  whole: number;
  slices: PieSlice[];
  /** The meters put into Other metered (small, empty or past the colours), largest first. */
  merged: HouseLoad[];
}

/** The pie: the meters at or over `SMALL_SHARE` of the whole (at most `SLOTS`, largest first), then Other metered with
 * the rest, the inverter (what a hybrid's inverter used, where the backend counts it) and Unknown (the house less every
 * meter and the inverter); an empty slice is left out. The heat pie shows every source with energy however small (a floor heating loop
 * is a few per cent of the heat pump), up to `SLOTS`. */
export function pieModel(language: Language, answer: HouseLoads): PieModel {
  const whole = Math.max(answer.house_kwh ?? 0, answer.meters_kwh);
  const loads = [...answer.loads].sort((a, b) => b.kwh - a.kwh);
  const shown: HouseLoad[] = [];
  const merged: HouseLoad[] = [];
  for (const load of loads) {
    const big = answer.kind === "heat" ? load.kwh > 0 : load.kwh / whole >= SMALL_SHARE;
    if (whole > 0 && big && shown.length < SLOTS) {
      shown.push(load);
    } else {
      merged.push(load);
    }
  }
  // A meter's colour by its statistic id among those shown, not by its size; the pool's, the spa's and the sauna's
  // parts keep their own (as in the Plan tab and the heat card's Use tab).
  const own = (load: HouseLoad): string | null => (Object.prototype.hasOwnProperty.call(LOAD_COLOURS, load.source) ? LOAD_COLOURS[load.source as OwnLoadKind] : null);
  const byId = shown.filter((load) => own(load) === null).map((load) => load.statistic_id).sort();
  const slices: PieSlice[] = shown.map((load) => ({
    key: load.statistic_id,
    name: load.name,
    kwh: load.kwh,
    share: whole > 0 ? load.kwh / whole : 0,
    colour: own(load) ?? `var(--spotnav-pie-${byId.indexOf(load.statistic_id) + 1})`,
  }));
  const metered = merged.reduce((sum, load) => sum + load.kwh, 0);
  const inverter = answer.kind === "house" ? Math.max(0, answer.inverter_kwh ?? 0) : 0;
  const unknown = answer.unknown_kwh ?? answer.other_kwh ?? 0;
  if (metered > 0) {
    slices.push({
      key: "other-metered",
      name: translate(language, "house.loads.otherMetered"),
      kwh: metered,
      share: whole > 0 ? metered / whole : 0,
      colour: "var(--spotnav-pie-other-metered)",
    });
  }
  if (inverter > 0) {
    slices.push({
      key: "inverter",
      name: translate(language, "house.loads.inverter"),
      kwh: inverter,
      share: whole > 0 ? inverter / whole : 0,
      colour: "var(--spotnav-pie-inverter)",
    });
  }
  if (unknown > 0 || slices.length === 0) {
    slices.push({
      key: "unknown",
      name: translate(language, "house.loads.unknown"),
      kwh: unknown,
      share: whole > 0 ? unknown / whole : 0,
      colour: "var(--spotnav-pie-unknown)",
    });
  }
  return { whole, slices, merged };
}

// ------------------------------------------------------------------------------------------------ the view

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, name: K, attributes: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const created = doc.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    created.setAttribute(key, String(value));
  }
  return created;
}

export function shareText(language: Language, share: number): string {
  if (share > 0 && share < 0.005) {
    return "<1 %";
  }
  return `${formatNumber(language, share * 100, 0)} %`;
}

function point(angle: number, radius: number): string {
  const x = PIE / 2 + radius * Math.sin(angle);
  const y = PIE / 2 - radius * Math.cos(angle);
  return `${x.toFixed(2)},${y.toFixed(2)}`;
}

/** One ring segment from `from` to `to` (radians, clockwise from the top). */
function arc(from: number, to: number): string {
  const large = to - from > Math.PI ? 1 : 0;
  return `M${point(from, OUTER)} A${OUTER},${OUTER} 0 ${large} 1 ${point(to, OUTER)} L${point(to, INNER)} A${INNER},${INNER} 0 ${large} 0 ${point(from, INNER)} Z`;
}

/** The pie as a ring, the house's kWh (`house`) in its middle; `role="img"` with every slice in its label. */
export function renderPie(
  doc: Document,
  language: Language,
  model: PieModel,
  house: number,
  kind: LoadsKind = "house",
  onSlice: ((key: string) => void) | null = null,
  choosable: ReadonlySet<string> = new Set(),
): SVGSVGElement {
  const root = svg(doc, "svg", { viewBox: `0 0 ${PIE} ${PIE}`, width: PIE, height: PIE, role: "img", class: L.pie });
  root.setAttribute(
    "aria-label",
    translate(language, "house.loads.chart", {
      list: model.slices.map((slice) => `${slice.name} ${kwhFineText(language, slice.kwh)} (${shareText(language, slice.share)})`).join(", "),
    }),
  );
  const drawn = model.slices.filter((slice) => slice.kwh > 0);
  const total = drawn.reduce((sum, slice) => sum + slice.kwh, 0);
  let angle = 0;
  const titled = (mark: SVGElement, slice: PieSlice): void => {
    const title = svg(doc, "title");
    title.textContent = `${slice.name} ${kwhFineText(language, slice.kwh)} (${shareText(language, slice.share)})`;
    mark.append(title);
  };
  if (drawn.length <= 1) {
    // One slice, or none, is the whole ring (an arc cannot close on itself); none is a faint ring.
    const ring = svg(doc, "circle", { cx: PIE / 2, cy: PIE / 2, r: (OUTER + INNER) / 2, fill: "none", "stroke-width": OUTER - INNER, "data-key": drawn[0]?.key ?? "none" });
    ring.style.setProperty("stroke", drawn[0]?.colour ?? "var(--spotnav-pie-unknown)");
    if (drawn[0] === undefined) {
      ring.style.setProperty("stroke-opacity", "0.35");
    } else {
      titled(ring, drawn[0]);
      if (onSlice !== null && choosable.has(drawn[0].key)) {
        const key = drawn[0].key;
        ring.setAttribute("data-choosable", "");
        ring.addEventListener("click", () => onSlice(key));
      }
    }
    root.append(ring);
  }
  for (const slice of drawn.length > 1 ? drawn : []) {
    const sweep = total > 0 ? (slice.kwh / total) * 2 * Math.PI : 0;
    const path = svg(doc, "path", { d: arc(angle, angle + sweep), class: L.slice, "data-key": slice.key });
    path.style.setProperty("fill", slice.colour);
    titled(path, slice);
    if (onSlice !== null && choosable.has(slice.key)) {
      // A pointer's way in; the keyboard's is the row's button beside the pie.
      path.setAttribute("data-choosable", "");
      path.addEventListener("click", () => onSlice(slice.key));
    }
    root.append(path);
    angle += sweep;
  }
  const value = svg(doc, "text", { x: PIE / 2, y: PIE / 2 + 2, "text-anchor": "middle", class: L.centre, "aria-hidden": "true" });
  value.textContent = kwhFineText(language, house);
  const label = svg(doc, "text", { x: PIE / 2, y: PIE / 2 + 17, "text-anchor": "middle", class: L.centreSmall, "aria-hidden": "true" });
  label.textContent = translate(language, kind === "heat" ? "house.flow.heat" : "house.flow.house");
  root.append(value, label);
  return root;
}

export interface LoadsRequest {
  /** Which block's pie (absent: the house's). */
  kind?: LoadsKind;
  period: LoadsPeriod;
  /** A day inside the period (`YYYY-MM-DD`); `null` for today's. */
  date: string | null;
  /** The period runs now (today, this month, this year): its caption says "so far". */
  running: boolean;
  /** The flow shows the car or the heat on its own: one line says they are not in the pie. */
  car: boolean;
  heat: boolean;
}

export type LoadsState =
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey }
  | { kind: "ready"; answer: HouseLoads };

const PERIOD_FORMATS = new Map<string, Intl.DateTimeFormat>();

/** The period in words: a day by its date, a month by its name and year, a year by its number. */
export function periodCaption(language: Language, answer: Pick<HouseLoads, "period" | "date">, running: boolean): string {
  if (answer.period === "day") {
    return running ? translate(language, "house.loads.today") : dateLabel(language, answer.date);
  }
  if (answer.period === "year") {
    return answer.date.slice(0, 4);
  }
  const key = language;
  let format = PERIOD_FORMATS.get(key);
  if (format === undefined) {
    format = new Intl.DateTimeFormat(language === "en" ? "en-GB" : language, { month: "long", year: "numeric", timeZone: "UTC" });
    PERIOD_FORMATS.set(key, format);
  }
  const [year, month] = answer.date.split("-").map(Number);
  return format.format(new Date(Date.UTC(year!, month! - 1, 15, 12)));
}

/** An administrator's way to move a meter (`null`: the pie without the menu). */
export interface LoadsControls {
  /** The meter whose menu is open, or `null`. */
  chosen: string | null;
  /** A write is on its way (the options wait). */
  saving: boolean;
  /** The last write's refusal, as a sentence key, or `null`. */
  failure: TranslationKey | null;
  onChoose(statisticId: string | null): void;
  onBelong(statisticId: string, belongs: Belongs): void;
}

const BELONG_WORDS: Record<Belongs, TranslationKey> = {
  house: "house.loads.belongHouse",
  heat: "house.loads.belongHeat",
  car: "house.loads.belongCar",
  none: "house.loads.belongNone",
};

const MOVED_WORDS: Record<Exclude<Belongs, "house">, TranslationKey> = {
  heat: "house.loads.movedHeat",
  car: "house.loads.movedCar",
  none: "house.loads.movedNone",
};

/** Where a meter of this answer belongs now. */
export function belongsOf(answer: HouseLoads, statisticId: string): Belongs {
  return answer.belongs[statisticId] ?? "house";
}

/** The popover's body for one state. */
export function loadsBody(
  doc: Document,
  language: Language,
  request: LoadsRequest,
  state: LoadsState,
  controls: LoadsControls | null = null,
): HTMLElement {
  const root = element(doc, "div", L.root);
  root.dataset["part"] = "house-loads";
  const note = (key: TranslationKey, values?: Record<string, string>, line?: string): HTMLElement => {
    const paragraph = element(doc, "p", `${C.muted} ${L.note}`, translate(language, key, values));
    if (line !== undefined) {
      paragraph.dataset["line"] = line;
    }
    return paragraph;
  };
  if (state.kind === "loading") {
    root.append(note("house.loads.reading", undefined, "status"));
    return root;
  }
  if (state.kind === "failed") {
    root.append(note(state.sentenceKey, undefined, "status"));
    return root;
  }
  const answer = state.answer;
  if (answer.house_kwh === null) {
    root.append(note("house.loads.noFlow", undefined, "status"));
    return root;
  }
  const model = pieModel(language, answer);
  const heat = answer.kind === "heat";
  root.append(element(doc, "p", L.caption, periodCaption(language, answer, request.running)));
  const figure = element(doc, "div", L.figure);
  const list = element(doc, "ul", L.list);
  const byKey = new Map(answer.loads.map((load) => [load.statistic_id, load]));
  // Only an Energy dashboard device the household may move (never the heat pump's meter or a zone).
  const choosable = new Set(controls === null ? [] : answer.loads.filter((load) => load.source === "meter").map((load) => load.statistic_id));
  for (const slice of model.slices) {
    const row = element(doc, "li", L.row);
    row.dataset["key"] = slice.key;
    const swatch = element(doc, "span", L.swatch);
    swatch.style.setProperty("background", slice.colour);
    swatch.setAttribute("aria-hidden", "true");
    const load = byKey.get(slice.key);
    let name: HTMLElement;
    if (controls !== null && choosable.has(slice.key)) {
      const button = element(doc, "button", L.choose, slice.name);
      button.type = "button";
      button.dataset["choose"] = slice.key;
      button.setAttribute("aria-expanded", controls.chosen === slice.key ? "true" : "false");
      button.setAttribute("aria-label", translate(language, "house.loads.chooseFor", { name: slice.name }));
      button.addEventListener("click", () => controls.onChoose(controls.chosen === slice.key ? null : slice.key));
      name = element(doc, "span", L.name);
      name.append(button);
    } else {
      name = element(doc, "span", L.name, slice.name);
    }
    if (load?.changed === true) {
      const mark = element(doc, "span", L.mark, translate(language, "house.loads.changed"));
      mark.dataset["mark"] = "changed";
      name.append(mark);
    }
    if (load?.estimated === true) {
      const mark = element(doc, "span", L.mark, translate(language, "house.loads.estimated"));
      mark.dataset["mark"] = "estimated";
      name.append(mark);
    }
    row.append(swatch, name, element(doc, "span", L.value, `${kwhFineText(language, slice.kwh)} · ${shareText(language, slice.share)}`));
    list.append(row);
  }
  const onSlice = controls === null ? null : (key: string) => controls.onChoose(controls.chosen === key ? null : key);
  figure.append(renderPie(doc, language, model, answer.house_kwh, answer.kind, onSlice, choosable), list);
  root.append(figure);
  if (controls !== null && controls.chosen !== null) {
    const chosen = byKey.get(controls.chosen) ?? answer.loads.find((load) => load.statistic_id === controls.chosen);
    if (chosen !== undefined) {
      root.append(belongMenu(doc, language, answer, chosen, controls));
    }
  }
  if (controls?.failure != null) {
    root.append(note(controls.failure, undefined, "write"));
  }
  for (const item of answer.implausible) {
    root.append(
      note(
        item.left_out ? "house.loads.implausibleOut" : "house.loads.implausible",
        { name: item.name, hours: formatNumber(language, item.hours, 0) },
        "implausible",
      ),
    );
  }
  if (model.merged.length > 0) {
    const line = note("house.loads.otherHolds", { list: model.merged.map((load) => `${load.name} ${kwhFineText(language, load.kwh)}`).join(", ") }, "merged");
    root.append(line);
    // A meter too small for a slice may be moved too: its name as a button under the line.
    const movable = controls === null ? [] : model.merged.filter((load) => choosable.has(load.statistic_id));
    if (controls !== null && movable.length > 0) {
      const buttons = element(doc, "p", L.note);
      buttons.dataset["line"] = "merged-choose";
      for (const load of movable) {
        const button = element(doc, "button", L.action, load.name);
        button.type = "button";
        button.dataset["choose"] = load.statistic_id;
        button.setAttribute("aria-expanded", controls.chosen === load.statistic_id ? "true" : "false");
        button.setAttribute("aria-label", translate(language, "house.loads.chooseFor", { name: load.name }));
        button.addEventListener("click", () => controls.onChoose(controls.chosen === load.statistic_id ? null : load.statistic_id));
        buttons.append(button, " ");
      }
      root.append(buttons);
    }
  }
  if (answer.over_kwh > 0) {
    root.append(note("house.loads.over", { kwh: kwhFineText(language, answer.over_kwh) }, "over"));
  }
  if (answer.clamped_kwh > 0) {
    root.append(note("house.loads.clamped", { kwh: kwhFineText(language, answer.clamped_kwh) }, "clamped"));
  }
  if (answer.loads.length === 0 && !heat) {
    root.append(note("house.loads.none", undefined, "none"));
  }
  if (!heat && answer.moved.length > 0) {
    root.append(movedList(doc, language, answer, controls));
  }
  const apart: TranslationKey | null = heat
    ? null
    : request.car && request.heat
      ? "house.loads.carHeat"
      : request.car
        ? "house.loads.car"
        : request.heat
          ? "house.loads.heat"
          : null;
  if (apart !== null) {
    root.append(note(apart, undefined, "apart"));
  }
  return root;
}

/** "Belongs to": the four places, the meter's own pressed; a write disables them until it is answered. */
function belongMenu(doc: Document, language: Language, answer: HouseLoads, load: HouseLoad, controls: LoadsControls): HTMLElement {
  const menu = element(doc, "fieldset", L.menu);
  menu.dataset["part"] = "belongs";
  menu.dataset["key"] = load.statistic_id;
  menu.append(element(doc, "legend", undefined, translate(language, "house.loads.belongs", { name: load.name })));
  const options = element(doc, "div");
  const current = belongsOf(answer, load.statistic_id);
  for (const belongs of BELONGS) {
    const button = element(doc, "button", L.option, translate(language, BELONG_WORDS[belongs]));
    button.type = "button";
    button.dataset["belongs"] = belongs;
    button.setAttribute("aria-pressed", belongs === current ? "true" : "false");
    button.disabled = controls.saving;
    button.addEventListener("click", () => {
      if (belongs === current) {
        controls.onChoose(null);
      } else {
        controls.onBelong(load.statistic_id, belongs);
      }
    });
    options.append(button);
  }
  menu.append(options);
  return menu;
}

/** The meters moved out of the house pie, each with where it went and (administrators) a way back. */
function movedList(doc: Document, language: Language, answer: HouseLoads, controls: LoadsControls | null): HTMLElement {
  const list = element(doc, "ul", `${C.muted} ${L.moved}`);
  list.dataset["line"] = "moved";
  for (const item of answer.moved) {
    const row = element(doc, "li");
    row.dataset["key"] = item.statistic_id;
    row.append(element(doc, "span", undefined, translate(language, MOVED_WORDS[item.belongs], { name: item.name })));
    if (controls !== null) {
      const back = element(doc, "button", L.action, translate(language, "house.loads.bringBack"));
      back.type = "button";
      back.dataset["back"] = item.statistic_id;
      back.disabled = controls.saving;
      back.setAttribute("aria-label", translate(language, "house.loads.bringBackNamed", { name: item.name }));
      back.addEventListener("click", () => controls.onBelong(item.statistic_id, "house"));
      row.append(back);
    }
    list.append(row);
  }
  return list;
}

// ------------------------------------------------------------------------------------------------ the popover

export interface LoadsPopoverOptions {
  doc: Document;
  owner: HTMLElement;
  background: () => HTMLElement | null;
  idPrefix: string;
  hass: () => HomeAssistantLike | null;
  target: () => { site: string } | { charger: string } | null;
  language: () => Language;
}

/** The popover: one dialog, read when opened; a newer opening wins over an older read. */
export class LoadsPopover {
  private readonly options: LoadsPopoverOptions;
  private dialog: DialogHandle | null = null;
  private request: LoadsRequest | null = null;
  private state: LoadsState = { kind: "loading" };
  private back: (() => void) | null = null;
  private read = 0;
  private chosen: string | null = null;
  private saving = false;
  private failure: TranslationKey | null = null;

  constructor(options: LoadsPopoverOptions) {
    this.options = options;
  }

  private ensure(): DialogHandle {
    if (this.dialog === null) {
      this.dialog = createDialog({
        owner: this.options.owner,
        idPrefix: `${this.options.idPrefix}-loads`,
        labels: { close: translate(this.options.language(), "dialog.close") },
        background: this.options.background,
        onClose: () => {
          this.read += 1;
        },
        onDismiss: () => {
          const back = this.back;
          this.back = null;
          // After the close has given the background back, so the dialog returned to hides it again.
          if (back !== null) {
            queueMicrotask(back);
          }
        },
      });
    }
    return this.dialog;
  }

  isOpen(): boolean {
    return this.dialog?.isOpen() ?? false;
  }

  /** Open for `request`; `back`: called when the reader closes it (the history dialog it was opened from). */
  open(request: LoadsRequest, opener: HTMLElement | null, back: (() => void) | null = null): void {
    this.request = request;
    this.back = back;
    this.state = { kind: "loading" };
    this.chosen = null;
    this.saving = false;
    this.failure = null;
    this.ensure().show({ title: this.title(), body: this.body(), opener });
    void this.load();
  }

  /** Paint again (a new language); nothing is read. */
  repaint(): void {
    if (this.isOpen()) {
      const focused = this.focusKey();
      this.dialog?.show({ title: this.title(), body: this.body() });
      if (focused !== null) {
        this.dialog?.element.querySelector<HTMLElement>(focused)?.focus();
      }
    }
  }

  destroy(): void {
    this.read += 1;
    this.back = null;
    this.dialog?.destroy();
    this.dialog = null;
  }

  private title(): string {
    return translate(this.options.language(), this.request?.kind === "heat" ? "house.loads.titleHeat" : "house.loads.title");
  }

  /** The control focused before a repaint, as a selector, so the keyboard stays where it was. */
  private focusKey(): string | null {
    const active = this.options.doc.activeElement as HTMLElement | null;
    const root = this.dialog?.element;
    if (active === null || root === undefined || !root.contains(active)) {
      return null;
    }
    for (const key of ["choose", "belongs", "back"]) {
      const value = active.dataset?.[key];
      if (value !== undefined) {
        return `[data-${key}="${CSS.escape(value)}"]`;
      }
    }
    return null;
  }

  private admin(): boolean {
    return this.options.hass()?.user?.is_admin === true;
  }

  private body(): HTMLElement {
    const controls: LoadsControls | null = this.admin()
      ? {
          chosen: this.chosen,
          saving: this.saving,
          failure: this.failure,
          onChoose: (statisticId) => {
            this.chosen = statisticId;
            this.failure = null;
            this.repaint();
            if (statisticId !== null) {
              this.dialog?.element.querySelector<HTMLElement>(`[data-part="belongs"] [aria-pressed="true"]`)?.focus();
            }
          },
          onBelong: (statisticId, belongs) => void this.write(statisticId, belongs),
        }
      : null;
    return loadsBody(this.options.doc, this.options.language(), this.request!, this.state, controls);
  }

  /** Move a meter (compare-and-set on the choices last read), then read the pie again. */
  private async write(statisticId: string, belongs: Belongs): Promise<void> {
    const hass = this.options.hass();
    const target = this.options.target();
    if (hass === null || target === null || this.state.kind !== "ready" || this.saving) {
      return;
    }
    this.saving = true;
    this.failure = null;
    this.repaint();
    let failure: TranslationKey | null = null;
    try {
      const answer = (await updateMeterBelongs(hass, target, this.state.answer.belongs, { [statisticId]: belongs })) as { ok?: unknown; error?: unknown } | null;
      if (answer === null || answer.ok !== true) {
        failure = answer?.error === "spotnav_conflict" ? "house.loads.writeConflict" : "house.loads.writeFailed";
      }
    } catch {
      failure = "house.loads.writeFailed";
    }
    this.saving = false;
    this.failure = failure;
    if (failure === null) {
      this.chosen = null;
    }
    this.repaint();
    // Read again either way: a conflict means the choices changed elsewhere.
    await this.load();
  }

  private async load(): Promise<void> {
    const hass = this.options.hass();
    const target = this.options.target();
    const request = this.request;
    if (hass === null || target === null || request === null) {
      return;
    }
    const read = ++this.read;
    let next: LoadsState;
    try {
      const decoded = decodeHouseLoads(await getHouseLoads(hass, target, request.period, request.date, request.kind ?? "house"));
      next = decoded.ok
        ? { kind: "ready", answer: decoded.value }
        : { kind: "failed", sentenceKey: decoded.failure === "unsupported" ? "house.loads.unsupported" : "house.loads.failed" };
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      next = { kind: "failed", sentenceKey: code === UNSUPPORTED_API_VERSION || code === "unknown_command" ? "house.loads.unsupported" : "house.loads.failed" };
    }
    if (read !== this.read) {
      return;
    }
    this.state = next;
    this.repaint();
  }
}
