// What the house used: the popover behind House in the energy flow (the card's front and the history's flow). It reads
// `spotnav/get_house_loads` when opened (the backend's shape is `custom_components/spotnav/site/house_loads.py`, its
// fixtures `tests/fixtures/house_loads/`) and shows the house's energy for the period as a pie: each of the Energy
// dashboard's individual devices by name and "Other", the house less them.
//
// * A meter under `SMALL_SHARE` of the whole, or past the `SLOTS` colours, goes into Other and is named under the pie,
//   so nothing is hidden; a meter with nothing in the period is named there too.
// * The whole is the house, or the meters where they add up to more (Other is then 0, and a line says so).
// * The colours are one validated categorical order (`--spotnav-pie-1` …, readable on a light and a dark card), given
//   to the shown meters by their statistic id, so a meter keeps its colour while the same meters are shown; Other is
//   a neutral grey. Every figure is also in the list beside the pie (name, kWh, %), so colour never carries it alone.
//
// Text is written with `textContent` only.

import { getHouseLoads, SpotnavApiError, UNSUPPORTED_API_VERSION } from "./api";
import { createDialog, type DialogHandle } from "./dialog";
import { dateLabel, formatFixed, formatNumber } from "./format";
import { HOUSE_LOADS_API_VERSION } from "./house-model";
import { translate, type Language, type TranslationKey } from "./i18n";
import type { HomeAssistantLike } from "./types";
import { VISUAL_CLASSES as C } from "./visual-styles";

const SVG_NS = "http://www.w3.org/2000/svg";

/** A meter under this share of the whole is put into Other. */
export const SMALL_SHARE = 0.03;
/** The colours a pie has for meters; any further meter goes into Other. */
export const SLOTS = 6;
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
    --spotnav-pie-other: var(--spotnav-pie-other-colour, #8a8f98);
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
    font-size: 11px;
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
`;

// ------------------------------------------------------------------------------------------------ the answer

export type LoadsPeriod = "day" | "month" | "year";

export interface HouseLoad {
  statistic_id: string;
  name: string;
  kwh: number;
}

export interface HouseLoads {
  site_id: string;
  period: LoadsPeriod;
  date: string;
  end: string;
  through: string | null;
  house_kwh: number | null;
  meters_kwh: number;
  other_kwh: number | null;
  over_kwh: number;
  loads: HouseLoad[];
}

export type HouseLoadsDecode = { ok: true; value: HouseLoads } | { ok: false; failure: "unsupported" | "malformed" };

function bad(): never {
  throw new Error("malformed");
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : bad();
}

function text(value: unknown): string {
  return typeof value === "string" ? value : bad();
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function numberOrNull(value: unknown): number | null {
  return value === null ? null : number(value);
}

function day(value: unknown): string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : bad();
}

function period(value: unknown): LoadsPeriod {
  return value === "day" || value === "month" || value === "year" ? value : bad();
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
        period: period(source.period),
        date: day(source.date),
        end: day(source.end),
        through: source.through === null ? null : text(source.through),
        house_kwh: numberOrNull(source.house_kwh),
        meters_kwh: number(source.meters_kwh),
        other_kwh: numberOrNull(source.other_kwh),
        over_kwh: number(source.over_kwh),
        loads: (source.loads as unknown[]).map((item) => {
          const load = record(item);
          return { statistic_id: text(load.statistic_id), name: text(load.name), kwh: Math.max(0, number(load.kwh)) };
        }),
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

// ------------------------------------------------------------------------------------------------ the slices

export interface PieSlice {
  /** The meter's statistic id, or `other`. */
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
  /** The meters put into Other (small, empty or past the colours), largest first. */
  merged: HouseLoad[];
}

/** The pie: the meters at or over `SMALL_SHARE` of the whole (at most `SLOTS`, largest first), then Other with the
 * rest; an empty slice is left out. */
export function pieModel(language: Language, answer: HouseLoads): PieModel {
  const whole = Math.max(answer.house_kwh ?? 0, answer.meters_kwh);
  const loads = [...answer.loads].sort((a, b) => b.kwh - a.kwh);
  const shown: HouseLoad[] = [];
  const merged: HouseLoad[] = [];
  for (const load of loads) {
    if (whole > 0 && load.kwh / whole >= SMALL_SHARE && shown.length < SLOTS) {
      shown.push(load);
    } else {
      merged.push(load);
    }
  }
  // A meter's colour by its statistic id among those shown, not by its size.
  const byId = [...shown].map((load) => load.statistic_id).sort();
  const slices: PieSlice[] = shown.map((load) => ({
    key: load.statistic_id,
    name: load.name,
    kwh: load.kwh,
    share: whole > 0 ? load.kwh / whole : 0,
    colour: `var(--spotnav-pie-${byId.indexOf(load.statistic_id) + 1})`,
  }));
  const other = (answer.other_kwh ?? 0) + merged.reduce((sum, load) => sum + load.kwh, 0);
  if (other > 0 || slices.length === 0) {
    slices.push({
      key: "other",
      name: translate(language, "house.loads.other"),
      kwh: other,
      share: whole > 0 ? other / whole : 0,
      colour: "var(--spotnav-pie-other)",
    });
  }
  return { whole, slices, merged };
}

// ------------------------------------------------------------------------------------------------ the view

function element<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, className?: string, content?: string): HTMLElementTagNameMap[K] {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (content !== undefined) {
    created.textContent = content;
  }
  return created;
}

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, name: K, attributes: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const created = doc.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    created.setAttribute(key, String(value));
  }
  return created;
}

export function kwhText(language: Language, value: number): string {
  return `${formatFixed(language, value, value >= 100 ? 0 : value > 0 && value < 1 ? 2 : 1)} kWh`;
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
export function renderPie(doc: Document, language: Language, model: PieModel, house: number): SVGSVGElement {
  const root = svg(doc, "svg", { viewBox: `0 0 ${PIE} ${PIE}`, width: PIE, height: PIE, role: "img", class: L.pie });
  root.setAttribute(
    "aria-label",
    translate(language, "house.loads.chart", {
      list: model.slices.map((slice) => `${slice.name} ${kwhText(language, slice.kwh)} (${shareText(language, slice.share)})`).join(", "),
    }),
  );
  const drawn = model.slices.filter((slice) => slice.kwh > 0);
  const total = drawn.reduce((sum, slice) => sum + slice.kwh, 0);
  let angle = 0;
  const titled = (mark: SVGElement, slice: PieSlice): void => {
    const title = svg(doc, "title");
    title.textContent = `${slice.name} ${kwhText(language, slice.kwh)} (${shareText(language, slice.share)})`;
    mark.append(title);
  };
  if (drawn.length <= 1) {
    // One slice, or none, is the whole ring (an arc cannot close on itself); none is a faint ring.
    const ring = svg(doc, "circle", { cx: PIE / 2, cy: PIE / 2, r: (OUTER + INNER) / 2, fill: "none", "stroke-width": OUTER - INNER, "data-key": drawn[0]?.key ?? "none" });
    ring.style.setProperty("stroke", drawn[0]?.colour ?? "var(--spotnav-pie-other)");
    if (drawn[0] === undefined) {
      ring.style.setProperty("stroke-opacity", "0.35");
    } else {
      titled(ring, drawn[0]);
    }
    root.append(ring);
  }
  for (const slice of drawn.length > 1 ? drawn : []) {
    const sweep = total > 0 ? (slice.kwh / total) * 2 * Math.PI : 0;
    const path = svg(doc, "path", { d: arc(angle, angle + sweep), class: L.slice, "data-key": slice.key });
    path.style.setProperty("fill", slice.colour);
    titled(path, slice);
    root.append(path);
    angle += sweep;
  }
  const value = svg(doc, "text", { x: PIE / 2, y: PIE / 2 + 2, "text-anchor": "middle", class: L.centre, "aria-hidden": "true" });
  value.textContent = kwhText(language, house);
  const label = svg(doc, "text", { x: PIE / 2, y: PIE / 2 + 17, "text-anchor": "middle", class: L.centreSmall, "aria-hidden": "true" });
  label.textContent = translate(language, "house.flow.house");
  root.append(value, label);
  return root;
}

export interface LoadsRequest {
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

/** The popover's body for one state. */
export function loadsBody(doc: Document, language: Language, request: LoadsRequest, state: LoadsState): HTMLElement {
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
  root.append(element(doc, "p", L.caption, periodCaption(language, answer, request.running)));
  const figure = element(doc, "div", L.figure);
  const list = element(doc, "ul", L.list);
  for (const slice of model.slices) {
    const row = element(doc, "li", L.row);
    row.dataset["key"] = slice.key;
    const swatch = element(doc, "span", L.swatch);
    swatch.style.setProperty("background", slice.colour);
    swatch.setAttribute("aria-hidden", "true");
    row.append(
      swatch,
      element(doc, "span", L.name, slice.name),
      element(doc, "span", L.value, `${kwhText(language, slice.kwh)} · ${shareText(language, slice.share)}`),
    );
    list.append(row);
  }
  figure.append(renderPie(doc, language, model, answer.house_kwh), list);
  root.append(figure);
  if (model.merged.length > 0) {
    root.append(
      note("house.loads.otherHolds", { list: model.merged.map((load) => `${load.name} ${kwhText(language, load.kwh)}`).join(", ") }, "merged"),
    );
  }
  if (answer.over_kwh > 0) {
    root.append(note("house.loads.over", { kwh: kwhText(language, answer.over_kwh) }, "over"));
  }
  if (answer.loads.length === 0) {
    root.append(note("house.loads.none", undefined, "none"));
  }
  const apart: TranslationKey | null = request.car && request.heat ? "house.loads.carHeat" : request.car ? "house.loads.car" : request.heat ? "house.loads.heat" : null;
  if (apart !== null) {
    root.append(note(apart, undefined, "apart"));
  }
  return root;
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
    this.ensure().show({ title: translate(this.options.language(), "house.loads.title"), body: this.body(), opener });
    void this.load();
  }

  /** Paint again (a new language); nothing is read. */
  repaint(): void {
    if (this.isOpen()) {
      this.dialog?.show({ title: translate(this.options.language(), "house.loads.title"), body: this.body() });
    }
  }

  destroy(): void {
    this.read += 1;
    this.back = null;
    this.dialog?.destroy();
    this.dialog = null;
  }

  private body(): HTMLElement {
    return loadsBody(this.options.doc, this.options.language(), this.request!, this.state);
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
      const decoded = decodeHouseLoads(await getHouseLoads(hass, target, request.period, request.date));
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
