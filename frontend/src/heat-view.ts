// The heat card's body: a header (the live status after the name, the details' "i" and, for an administrator, the
// settings button) and its tabs (`heat-tabs.ts`). Now: the thermostat dial with the comfort band and the measured
// room, home or away, what SpotNav would do and the relay, a thin strip of the plan's next 24 hours, today's heat
// and hot water, and the notes the household can act on. Plan: the day's chart (`heat-day-chart.ts`), today's or a
// past day's, with the pool's, the spa's and the sauna's next hours under it. Use: the period's energy or cost by what
// is measured (`heat-use.ts`). Pool, Spa and Sauna where the site has one (`pool-view.ts`, `spa-view.ts`). Below, the
// "Heating" settings section (`heatSettingsSection`), the rows the settings dialog shows. Built fresh for each state
// with `textContent` only; every figure is the backend's.

import { cardHeader, headerButton, headerStatus, headerTitle } from "./card-header";
import { clock, dateLabel, formatFixed, kwhText, kwText, pad2, type FormatContext } from "./format";
import {
  heatFound,
  heatSteers,
  isRelayPath,
  roomName,
  type HeatAheadHour,
  type HeatAnswer,
  type HeatBandHour,
  type HeatBlockingCut,
  type HeatChoice,
  type HeatHotWaterBlocking,
  type HeatZone,
} from "./heat-model";
import { dayChart, hoursOf, type DayNote, type DayPeak, type DayPlannedWater, type DaySpan } from "./heat-day-chart";
import type { HeatHistory } from "./heat-history";
import { HEAT_CLASSES as X } from "./heat-styles";
import { BASE_TABS, heatTabList, heatTabPanel, type HeatTab, type HeatTabFit, type UseUnit } from "./heat-tabs";
import { usePanel } from "./heat-use";
import { HOUSE_CLASSES as H } from "./house-styles";
import { periodPicker, type PeriodChoice } from "./house-tabs";
import { translate, type Language, type TranslationKey } from "./i18n";
import { sectionHeading, settingRow } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";
import { poolPanel, poolPlanLine, poolStatusLine, poolWouldLine } from "./pool-view";
import { deadlinePanel, deadlinePlanLine, deadlineWouldLine } from "./spa-view";

export const TARGET_MIN = 16;
export const TARGET_MAX = 25;
export const TARGET_STEP = 0.5;
const HOUR_MS = 3_600_000;
const SVG = "http://www.w3.org/2000/svg";

export type HeatState =
  | { kind: "unconfigured" }
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null }
  | { kind: "ready"; answer: HeatAnswer };

/** Something read apart from the card's own answer: on its way, unreadable, or in. */
export type HeatLoadable<A> = { kind: "loading" } | { kind: "failed"; code: string | null } | { kind: "ready"; answer: A };

/** What the tabs show besides the card's own answer. */
export interface HeatTabsData {
  /** The period the shown tab needs (`spotnav/get_heat_history`): Use's period, Plan's past day; else `null`. */
  history: HeatLoadable<HeatHistory> | null;
  /** The site's local date. */
  today: string;
}

export interface HeatUi {
  /** The dial's figure while a change waits to be written (`null`: the stored one). */
  pendingTarget: number | null;
  /** A sentence under the dial after a refused write. */
  error: string | null;
  /** The header's test chip opened to its explanation. */
  testOpen?: boolean;
  /** The time chosen on the day's chart (a step's start, ms), kept across the card's redraws. */
  chartSelectedMs?: number | null;
  /** The tab shown (Now without one). */
  tab?: HeatTab;
  /** The one tab the card's YAML shows (no tab row); `null` for all. */
  locked?: HeatTab | null;
  /** The tab row's wording as last measured. */
  tabFit?: HeatTabFit;
  /** The period Use shows, and Plan while it is a day. */
  choice?: PeriodChoice;
  /** Plan's own day while the shared period is a month or a year (`null`: today). */
  planDate?: string | null;
  /** Use: kWh or money, and the parts hidden in its legend. */
  useUnit?: UseUnit;
  useHidden?: readonly string[];
  /** The bar chosen in Use (its key). */
  bar?: string | null;
  /** In a box of fixed height: the height (px) the shown tab's picture takes; `null` in a plain column. */
  fill?: number | null;
  /** A sentence beside the Boost button after a refused press (`null`: none). */
  boostError?: string | null;
  /** A press of the Boost button is on its way. */
  boostBusy?: boolean;
  /** The pool's, the spa's or the sauna's dial figure while a change waits to be written. */
  pendingLoad?: Partial<Record<LoadKind, number>>;
  /** A sentence in the pool's, the spa's or the sauna's tab after a refused press. */
  loadError?: Partial<Record<LoadKind, string | null>>;
  /** A press in the pool's, the spa's or the sauna's tab is on its way. */
  loadBusy?: LoadKind | null;
}

/** The loads with a tab of their own. */
export type LoadKind = "pool" | "spa" | "sauna";

/** A press in the pool's tab (`spotnav/pool`): the season, an extra filtering, or its end. */
export type PoolPress = { action: "season"; open: boolean } | { action: "extra_filtering"; hours: 2 | 4 } | { action: "cancel_extra" };

export interface HeatHandlers {
  onStep(delta: number): void;
  onHome(): void;
  onAway(): void;
  onSettings(): void;
  /** The clock in the header: the history dialog (`history-dialog.ts`). Without it the card has no clock. */
  onHistory?(opener: HTMLElement): void;
  /** The test chip tapped: its explanation shown or hidden. Without it the chip opens and closes in place. */
  onTest?(open: boolean): void;
  /** A time chosen on the day's chart (`null`: let go). */
  onChartSelect?(ms: number | null): void;
  /** A tab chosen, and whether by the keyboard. */
  onTab?(tab: HeatTab, byKey: boolean): void;
  /** A period chosen in Use, or a day in Plan (`plan`). */
  onPeriod?(choice: PeriodChoice, plan: boolean): void;
  onUnit?(unit: UseUnit): void;
  onUsePart?(hidden: string[]): void;
  onBar?(key: string | null): void;
  /** The Boost button: start the hot water now, or stop the household's own boost. */
  onBoost?(action: "start" | "stop"): void;
  /** The pool's, the spa's or the sauna's dial stepped. */
  onLoadStep?(kind: LoadKind, delta: number): void;
  /** A press in the pool's tab. */
  onPool?(press: PoolPress): void;
  /** A press in the spa's or the sauna's tab (`spotnav/spa`, `spotnav/sauna`): its action and value. */
  onDeadline?(kind: "spa" | "sauna", action: string, value?: unknown): void;
  /** The spa's or the sauna's times, or the spa's idle temperature, to change (an editor of their own). */
  onLoadEdit?(kind: "spa" | "sauna", what: "times" | "idle"): void;
}

export interface HeatRenderInput {
  doc: Document;
  language: Language;
  state: HeatState;
  ui: HeatUi;
  handlers: HeatHandlers;
  width: number;
  nowMs: number;
  /** Ids in the card's shadow root start with this. */
  idPrefix?: string;
  /** What the shown tab needs besides the answer. */
  tabs?: HeatTabsData;
}

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, tag: K, attributes: Record<string, string | number>): SVGElementTagNameMap[K] {
  const node = doc.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, String(value));
  }
  return node;
}

// ------------------------------------------------------------------------------------------------ time

/** The offset of `timeZone` from UTC at an instant (ms), from `Intl`. */
function zoneOffsetMs(timeZone: string, instantMs: number): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(instantMs));
  const value = (type: string): number => Number(parts.find((part) => part.type === type)?.value ?? "0");
  const asUtc = Date.UTC(value("year"), value("month") - 1, value("day"), value("hour"), value("minute"), value("second"));
  return asUtc - Math.floor(instantMs / 1000) * 1000;
}

/** A local date and time in `timeZone` as an instant (ms); `null` for text that is not one. */
export function localToInstant(date: string, time: string, timeZone: string): number | null {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  const t = /^(\d{2}):(\d{2})$/.exec(time);
  if (d === null || t === null) {
    return null;
  }
  const wall = Date.UTC(Number(d[1]), Number(d[2]) - 1, Number(d[3]), Number(t[1]), Number(t[2]));
  let guess = wall - zoneOffsetMs(timeZone, wall);
  guess = wall - zoneOffsetMs(timeZone, guess);
  return Number.isFinite(guess) ? guess : null;
}

/** An instant as the local date and time in `timeZone` ("2026-11-20", "16:00"). */
export function instantToLocal(instantMs: number, timeZone: string): { date: string; time: string } {
  const shifted = new Date(instantMs + zoneOffsetMs(timeZone, instantMs));
  return {
    date: `${shifted.getUTCFullYear()}-${pad2(shifted.getUTCMonth() + 1)}-${pad2(shifted.getUTCDate())}`,
    time: `${pad2(shifted.getUTCHours())}:${pad2(shifted.getUTCMinutes())}`,
  };
}

// ------------------------------------------------------------------------------------------------ words

export function degrees(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} °C`;
}

function signedDegrees(language: Language, value: number): string {
  if (value === 0) {
    return "0 °C";
  }
  return `${value > 0 ? "+" : "−"}${formatFixed(language, Math.abs(value), 1)} °C`;
}

/** A heating curve offset as the pump shows it: signed, no unit ("0", "+1", "−2", "−1.5"). */
export function offsetValue(language: Language, value: number): string {
  if (value === 0) {
    return "0";
  }
  return `${value > 0 ? "+" : "−"}${formatFixed(language, Math.abs(value), Number.isInteger(value) ? 0 : 1)}`;
}

function context(language: Language, answer: HeatAnswer): FormatContext {
  return { language, timeZone: answer.time_zone, unit: "kWh", currency: answer.today?.money?.currency ?? null, majorUnit: answer.today?.money?.major_unit ?? null };
}

/** Money given in the minor unit, shown in the major one: whole units from 10 up ("19 kr"), else two decimals. */
export function amount(language: Language, minor: number, money: { major_unit: string; minor_unit: string } | null): string {
  if (money === null) {
    return formatFixed(language, minor, 0);
  }
  const major = minor / 100;
  return `${formatFixed(language, major, Math.abs(major) >= 10 ? 0 : 2)} ${money.major_unit}`;
}

/** A local moment as the card says it: the time today, else the day and the time ("fre 20 nov. 16:00"). */
export function whenLabel(language: Language, answer: HeatAnswer, instantMs: number): string {
  const time = clock(context(language, answer), instantMs);
  const day = instantToLocal(instantMs, answer.time_zone).date;
  return day === instantToLocal(Date.parse(answer.now), answer.time_zone).date ? time : `${dateLabel(language, day)} ${time}`;
}

/** A local time ("18:40") in the site's zone. */
export function heatClock(language: Language, answer: HeatAnswer, instantMs: number): string {
  return clock(context(language, answer), instantMs);
}

/** Hours as the card says them: whole, else one decimal ("8 h", "8,3 h"). */
export function hoursText(language: Language, hours: number): string {
  return hoursWord(language, hours);
}

/** The entity names the answer knows (choices, sensors, zones, meter), for the settings rows. */
export function namesOf(answer: HeatAnswer): Map<string, string> {
  const names = new Map<string, string>();
  const choices = answer.choices;
  const groups: unknown[] = Object.values(choices ?? {});
  if (choices?.pool) {
    groups.push(choices.pool.heat_pump, choices.pool.circulation, choices.pool.water_sensor);
  }
  for (const group of groups) {
    // Only the lists of entities: the pool's block and the phones are not one.
    if (!Array.isArray(group)) {
      continue;
    }
    for (const choice of group as Partial<HeatChoice>[]) {
      if (typeof choice.entity_id === "string" && typeof choice.name === "string") {
        names.set(choice.entity_id, choice.name);
      }
    }
  }
  for (const sensor of answer.indoor.sensors) {
    names.set(sensor.entity_id, sensor.name);
  }
  for (const zone of answer.zones) {
    names.set(zone.entity_id, zone.name);
  }
  if (answer.meter?.entity_id && answer.meter.name) {
    names.set(answer.meter.entity_id, answer.meter.name);
  }
  if (answer.outdoor?.entity_id && answer.outdoor.name) {
    names.set(answer.outdoor.entity_id, answer.outdoor.name);
  }
  return names;
}

function nameOf(names: Map<string, string>, entityId: string | null, fallback: string): string {
  return entityId === null ? fallback : names.get(entityId) ?? entityId;
}

// ------------------------------------------------------------------------------------------------ header

/** The house and heat cards' status after the name, muted (`card-header.ts`), named by `code` for tests. */
export function statusLine(doc: Document, content: string | Node, code: string): HTMLElement {
  const { line } = headerStatus(doc, content, true);
  line.dataset["line"] = "header-status";
  line.dataset["status"] = code;
  return line;
}

/**
 * The "· Test" status after the house and heat cards' names while SpotNav controls nothing yet: a button whose tooltip
 * says so ("SpotNav styr inget än") and that shows or hides that sentence with what test means, under the header.
 * Returns the status and the explanation, which the caller places under the header.
 */
export function testStatus(
  doc: Document,
  language: Language,
  explainKey: TranslationKey,
  open: boolean,
  id: string,
  onToggle?: (open: boolean) => void,
): { status: HTMLElement; explain: HTMLElement } {
  const lead = translate(language, "header.testExplain");
  const button = element(doc, "button", C.statusButton, translate(language, "header.test")) as HTMLButtonElement;
  button.type = "button";
  button.title = lead;
  button.dataset["action"] = "test";
  button.setAttribute("aria-expanded", String(open));
  button.setAttribute("aria-controls", id);
  const explain = element(doc, "p", `${C.muted} ${H.note} ${H.testExplain}`);
  explain.append(element(doc, "strong", undefined, lead), doc.createTextNode(` ${translate(language, explainKey)}`));
  explain.id = id;
  explain.dataset["line"] = "test-explain";
  explain.hidden = !open;
  button.addEventListener("click", () => {
    const next = button.getAttribute("aria-expanded") !== "true";
    if (onToggle !== undefined) {
      onToggle(next);
      return;
    }
    button.setAttribute("aria-expanded", String(next));
    explain.hidden = !next;
  });
  return { status: statusLine(doc, button, "test"), explain };
}

/**
 * What the heating does now, as the header says it after the name: while SpotNav's control holds the heat down or up,
 * that and until when ("Sänkt till 06:00"), else what the heat pump does by its own facts or its meter ("Värmer
 * 2,1 kW"); `null` when neither is known.
 */
export function heatLive(language: Language, answer: HeatAnswer): { code: string; text: string } | null {
  const control = answer.control;
  if (control !== null && control.enabled && control.state === "running") {
    if (control.raised) {
      return control.raise_until === null
        ? { code: "raise", text: translate(language, "heat.now.raised") }
        : { code: "raise", text: translate(language, "heat.now.raisedUntil", { until: whenLabel(language, answer, Date.parse(control.raise_until)) }) };
    }
    if (control.lowered) {
      return control.until === null
        ? { code: "lower", text: translate(language, "heat.now.lowered") }
        : { code: "lower", text: translate(language, "heat.now.loweredUntil", { until: whenLabel(language, answer, Date.parse(control.until)) }) };
    }
  }
  const state = answer.status.pump_state;
  if (state === null || !["heating", "hot_water", "idle"].includes(state)) {
    return null;
  }
  const power = answer.status.kw;
  const words = translate(language, `heat.now.${state}` as TranslationKey);
  return { code: state, text: state === "idle" || power === null ? words : `${words} ${kwText(language, power).replace(" ", "\u00a0")}` };
}

/** Whether SpotNav's own heat control runs (else the header says Test). */
function controls(answer: HeatAnswer): boolean {
  return answer.control !== null && answer.control.enabled && answer.control.state === "running";
}

/** The heat card's header (`card-header.ts`): "Värme · Test · Värmer 2,1 kW" in shadow, "Värme · Sänkt till 06:00"
 * while it controls; the details' "i" and, for an administrator, the settings. */
function header(doc: Document, language: Language, answer: HeatAnswer | null, handlers: HeatHandlers | null, testOpen = false, idPrefix = "spotnav-heat"): HTMLElement[] {
  const now = answer === null ? null : heatLive(language, answer);
  const test = answer === null || controls(answer) ? null : testStatus(doc, language, "heat.test.explain", testOpen, `${idPrefix}-test`, handlers?.onTest?.bind(handlers));
  let status: HTMLElement | null = test?.status ?? null;
  if (now !== null) {
    if (status === null) {
      status = statusLine(doc, now.text, now.code);
    } else {
      // After the test chip, in the same muted line.
      const { word } = headerStatus(doc, now.text, true);
      word.prepend(doc.createTextNode(" "));
      status.append(word);
    }
    status.dataset["live"] = now.code;
  }
  const buttons: HTMLButtonElement[] = [];
  const onHistory = handlers?.onHistory;
  if (answer !== null && onHistory !== undefined) {
    const button = headerButton(doc, "info", translate(language, "house.details.open"));
    button.addEventListener("click", () => onHistory.call(handlers, button));
    buttons.push(button);
  }
  if (answer?.writable === true && handlers !== null) {
    const button = headerButton(doc, "settings", translate(language, "heat.settings.open"));
    button.dataset["action"] = "settings";
    button.addEventListener("click", () => handlers.onSettings());
    buttons.push(button);
  }
  const { header: head } = cardHeader(doc, {
    idPrefix,
    productName: translate(language, "card.title"),
    title: headerTitle(doc, translate(language, "heat.title"), status).block,
    buttons,
  });
  return test === null ? [head] : [head, test.explain];
}

// ------------------------------------------------------------------------------------------------ the dial

const DIAL = { cx: 100, cy: 96, r: 78, start: 135, sweep: 270 } as const;

function point(angle: number): [number, number] {
  const radians = (angle * Math.PI) / 180;
  return [DIAL.cx + DIAL.r * Math.cos(radians), DIAL.cy + DIAL.r * Math.sin(radians)];
}

function arc(from: number, to: number): string {
  const a = DIAL.start + DIAL.sweep * from;
  const b = DIAL.start + DIAL.sweep * to;
  const [x0, y0] = point(a);
  const [x1, y1] = point(b);
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${DIAL.r} ${DIAL.r} 0 ${b - a > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

/** What a dial shows and how it steps: the house's comfort target, the pool's water, the spa's bath, the sauna's. */
export interface DialSpec {
  target: number;
  min: number;
  max: number;
  step: number;
  /** The figure's decimals (the sauna's whole degrees: 0). */
  decimals: number;
  /** The reading drawn as a dot on the arc; `null` without one. */
  measured: number | null;
  /** The shaded band around the target, `null` for none. */
  band: [number, number] | null;
  /** The dial's accessible name's lead ("Måltemperatur"). */
  label: string;
  /** The line under the figure. */
  sub: string;
  lowerLabel: string;
  raiseLabel: string;
  onStep: (delta: number) => void;
  /** No buttons: the figure is shown, with nothing to press. */
  readOnly: boolean;
  /** A sentence under the dial after a refused write. */
  error: string | null;
  /** In a box of fixed height: the dial's height (px). */
  height: number | null;
}

/** A dial with − and + beside it (the heat card's thermostat, and the pool's, the spa's and the sauna's). */
export function dialBlock(doc: Document, language: Language, spec: DialSpec): HTMLElement {
  const wrap = element(doc, "div", X.dialWrap);
  const span = spec.max - spec.min;
  const share = (value: number): number => Math.min(1, Math.max(0, (value - spec.min) / span));
  const step = (delta: number, label: string, text: string, disabled: boolean): HTMLButtonElement => {
    const button = element(doc, "button", X.dialStep, text);
    button.type = "button";
    button.dataset["step"] = delta > 0 ? "up" : "down";
    button.setAttribute("aria-label", label);
    button.disabled = disabled;
    button.addEventListener("click", () => spec.onStep(delta));
    return button;
  };
  const picture = svg(doc, "svg", { viewBox: "0 0 200 170", class: X.dial, role: "img" });
  if (spec.height !== null) {
    // In a box of fixed height the dial takes the height it is given, as wide as that makes it.
    picture.style.height = `${Math.round(spec.height)}px`;
    picture.style.maxWidth = `${Math.round((spec.height * 200) / 170)}px`;
  }
  const figure = (value: number): string => formatFixed(language, value, spec.decimals);
  const label = [spec.label, `${figure(spec.target)} °C`];
  if (spec.measured !== null) {
    label.push(`${formatFixed(language, spec.measured, 1)} °C`);
  }
  picture.setAttribute("aria-label", label.join(", "));
  picture.append(svg(doc, "path", { d: arc(0, 1), class: X.dialTrack }));
  if (spec.band !== null) {
    picture.append(svg(doc, "path", { d: arc(share(spec.band[0]), share(spec.band[1])), class: X.dialBand }));
  }
  picture.append(svg(doc, "path", { d: arc(0, Math.max(0.002, share(spec.target))), class: X.dialValue }));
  if (spec.measured !== null) {
    const [x, y] = point(DIAL.start + DIAL.sweep * share(spec.measured));
    picture.append(svg(doc, "circle", { cx: x.toFixed(2), cy: y.toFixed(2), r: 6, class: X.dialMeasured, "data-measured": "true" }));
  }
  const big = svg(doc, "text", { x: DIAL.cx, y: DIAL.cy + 8, class: X.dialFigure });
  big.textContent = `${figure(spec.target)}°`;
  const sub = svg(doc, "text", { x: DIAL.cx, y: DIAL.cy + 32, class: X.dialSub });
  sub.textContent = spec.sub;
  sub.dataset["line"] = "band";
  picture.append(big, sub);
  wrap.append(
    ...(spec.readOnly
      ? [picture]
      : [
          step(-spec.step, spec.lowerLabel, "−", spec.target - spec.step < spec.min - 1e-9),
          picture,
          step(spec.step, spec.raiseLabel, "+", spec.target + spec.step > spec.max + 1e-9),
        ]),
  );
  const block = element(doc, "div");
  block.append(wrap);
  if (spec.error !== null) {
    const error = element(doc, "p", X.error, spec.error);
    error.setAttribute("role", "alert");
    block.append(error);
  }
  return block;
}

function dial(doc: Document, language: Language, answer: HeatAnswer, target: number, ui: HeatUi, handlers: HeatHandlers, height: number | null = null, readOnly = false): HTMLElement {
  const low = target - 0.5;
  const high = target + 0.5;
  return dialBlock(doc, language, {
    target,
    min: TARGET_MIN,
    max: TARGET_MAX,
    step: TARGET_STEP,
    decimals: 1,
    measured: answer.indoor.value_c,
    band: [low, high],
    label: translate(language, "heat.dial.label"),
    // The big figure is the target; the line under it says what it keeps, so the same number is not said twice.
    sub: translate(language, "heat.dial.band", { low: formatFixed(language, low, 1), high: formatFixed(language, high, 1) }),
    lowerLabel: translate(language, "heat.dial.lower"),
    raiseLabel: translate(language, "heat.dial.raise"),
    onStep: (delta) => handlers.onStep(delta),
    // Read-only (SpotNav cannot steer this heating): the target is shown, with nothing to press.
    readOnly,
    error: ui.error,
    height,
  });
}

/** "Inne 20,4 °C (kallast: Toalett NV)" (the coldest of several), with the outdoor reading after it. A figure and its
 * unit never part at a line break. */
export function measuredLine(language: Language, answer: HeatAnswer): string {
  const indoor = answer.indoor;
  const parts: string[] = [];
  if (indoor.value_c === null) {
    parts.push(translate(language, "heat.noSensor"));
  } else {
    const counted = indoor.sensors.filter((sensor) => sensor.value_c !== null).length;
    parts.push(
      translate(language, counted > 1 ? "heat.measuredColdest" : "heat.measured", {
        value: degrees(language, indoor.value_c).replace(" ", "\u00a0"),
        room: roomName(indoor.coldest_name ?? indoor.coldest ?? ""),
      }),
    );
  }
  if (answer.outdoor?.value_c !== null && answer.outdoor !== null) {
    parts.push(translate(language, "heat.outdoor", { value: degrees(language, answer.outdoor.value_c!).replace(" ", "\u00a0") }));
  }
  return parts.join(" · ");
}

function modes(doc: Document, language: Language, answer: HeatAnswer, handlers: HeatHandlers): HTMLElement[] {
  const row = element(doc, "div", X.modes);
  row.setAttribute("role", "group");
  const away = answer.comfort.mode === "away";
  for (const [mode, key, pressed, act] of [
    ["home", "heat.mode.home", !away, handlers.onHome],
    ["away", "heat.mode.away", away, handlers.onAway],
  ] as const) {
    const button = element(doc, "button", X.mode, translate(language, key));
    button.type = "button";
    button.dataset["mode"] = mode;
    button.setAttribute("aria-pressed", pressed ? "true" : "false");
    button.addEventListener("click", () => act());
    row.append(button);
  }
  const out: HTMLElement[] = [row];
  if (away && answer.comfort.away_until !== null) {
    const line = element(doc, "p", X.awayLine, translate(language, "heat.mode.awayUntil", { when: whenLabel(language, answer, Date.parse(answer.comfort.away_until)) }));
    line.dataset["line"] = "away";
    const change = element(doc, "button", undefined, translate(language, "heat.mode.change"));
    change.type = "button";
    change.dataset["action"] = "away-change";
    change.addEventListener("click", () => handlers.onAway());
    line.append(change);
    out.push(line);
  }
  return out;
}

// ------------------------------------------------------------------------------------------------ status

/** What the heat pump does now, by its own facts or its meter ("Värmepumpen värmer · 1,1 kW"). */
export function pumpLine(language: Language, answer: HeatAnswer): string | null {
  const state = answer.status.pump_state;
  const power = answer.status.kw;
  if (state === null) {
    return answer.meter === null && answer.pump === null ? null : translate(language, "heat.pump.unknown");
  }
  const key = (["heating", "hot_water", "idle"].includes(state) ? `heat.pump.${state}` : "heat.pump.unknown") as TranslationKey;
  return translate(language, key, { kw: power === null ? "–" : kwText(language, power) }).replace(/ · –$/, "");
}

/** What SpotNav would do now, in shadow ("Skulle sänka till 20,5 °C till 20:00: dyra timmar"). */
export function wouldLine(language: Language, answer: HeatAnswer): string | null {
  const would = answer.status.would;
  const guard = answer.status.guard;
  if (guard !== null && (would === null || would.action === "normal")) {
    return translate(language, `heat.guard.${guard}` as TranslationKey);
  }
  if (would === null) {
    return null;
  }
  const actions = ["lower", "preheat", "night", "away", "return", "normal"];
  if (!actions.includes(would.action)) {
    return null;
  }
  // A lowering's own reason; the dear hours are the plain one.
  const lowerKey = would.reason === "tight_hour" ? "heat.would.lowerTight" : would.reason === "room_warm" ? "heat.would.lowerWarm" : "heat.would.lower";
  // On SG Ready with both contacts, or through its API, the pre-heat is a raise of the pump by SG Ready (the words
  // name it; an offset's raise keeps the pre-heat's own words).
  const { control_path: path, control_entity: first, control_entity_b: second } = answer.config;
  const sgRaise = (path === "sg_ready" || path === "sg_ready_api") && first !== null && second !== null;
  const key = would.action === "lower" ? lowerKey : would.action === "preheat" && sgRaise ? "heat.would.raise" : `heat.would.${would.action}`;
  return translate(language, key as TranslationKey, {
    temp: degrees(language, would.target_c),
    until: would.until === null ? "" : whenLabel(language, answer, Date.parse(would.until)),
  });
}

/** The relay's run of hours from `index` on, by the band's minutes: when it ends (ms). */
function relayRunEnd(hours: HeatBandHour[], index: number): number {
  let end = hours[index]!.startMs + (hours[index]!.relay_min ?? 0) * 60_000;
  for (let next = index + 1; next < hours.length && (hours[next]!.relay_min ?? 0) > 0; next += 1) {
    end = hours[next]!.startMs + (hours[next]!.relay_min ?? 0) * 60_000;
  }
  return end;
}

/**
 * What the heat pump's relay does, whoever closes it ("Reläet sänker värmen sedan 16:00, öppnar 19:00"), or when it is
 * expected to lower next ("Reläet väntas sänka värmen 16:00–19:00"); `null` without a relay or anything to say.
 */
export function relayLine(language: Language, answer: HeatAnswer, nowMs: number): string | null {
  const relay = answer.relay;
  if (relay === null) {
    return null;
  }
  const hours = answer.band.hours;
  const current = hours.findIndex((hour) => hour.startMs <= nowMs && nowMs < hour.startMs + HOUR_MS);
  if (relay.closed === true) {
    const since = relay.since === null ? null : whenLabel(language, answer, Date.parse(relay.since));
    const ahead = current >= 0 && hours[current]!.relay_expected ? relayRunEnd(hours, current) : null;
    if (ahead !== null && ahead > nowMs) {
      return translate(language, "heat.relay.closedUntil", { since: since ?? "", until: whenLabel(language, answer, ahead) });
    }
    return translate(language, "heat.relay.closed", { since: since ?? "" });
  }
  const next = hours.findIndex((hour, index) => index > current && hour.relay_expected && (hour.relay_min ?? 0) > 0);
  if (next < 0 || current < 0) {
    return null;
  }
  const first = hours[next]!;
  // A closing that begins in its hour runs to the hour's end: it began its minutes before that.
  const from = first.startMs + (60 - Math.min(60, first.relay_min ?? 0)) * 60_000;
  return translate(language, "heat.relay.next", {
    from: whenLabel(language, answer, from),
    to: whenLabel(language, answer, relayRunEnd(hours, next)),
  });
}

function hoursWord(language: Language, hours: number): string {
  return `${formatFixed(language, hours, Number.isInteger(hours) ? 0 : 1)} h`;
}

/**
 * Yesterday's review in two lines: what the relay really did and what SpotNav's plan would have done, each with what it
 * saved and the coldest room ("I går sänkte reläet 3 h: 3,60 kr · kallast 20,3 °C · 30 min under 20,5 °C").
 */
export function reviewLines(language: Language, answer: HeatAnswer): { key: string; text: string }[] {
  const review = answer.review;
  if (review === null) {
    return [];
  }
  const money = answer.today?.money ?? null;
  const comfort = (part: { coldest_min_c: number | null; minutes_below: number }): string[] => {
    const out: string[] = [];
    if (part.coldest_min_c !== null) {
      out.push(translate(language, "heat.review.coldest", { temp: degrees(language, part.coldest_min_c).replace(" ", "\u00a0") }));
      if (part.minutes_below > 0) {
        out.push(translate(language, "heat.review.below", { minutes: String(part.minutes_below), low: degrees(language, review.low_c).replace(" ", "\u00a0") }));
      }
    }
    return out;
  };
  const relay = review.relay.hours > 0
    ? [translate(language, "heat.review.relay", { hours: hoursWord(language, review.relay.hours), value: amount(language, review.relay.value, money) }), ...comfort(review.relay)]
    : [translate(language, "heat.review.relayNone")];
  const plan = review.plan.hours > 0
    ? [translate(language, "heat.review.plan", { hours: hoursWord(language, review.plan.hours), value: amount(language, review.plan.value, money) }), ...comfort(review.plan)]
    : [translate(language, "heat.review.planNone")];
  if (review.plan.hours > 0 && review.plan.overlap_hours > 0) {
    plan.push(translate(language, "heat.review.overlap", { hours: hoursWord(language, review.plan.overlap_hours) }));
  }
  return [
    { key: "review-relay", text: relay.join(" · ") },
    { key: "review-plan", text: plan.join(" · ") },
  ];
}

/** A sentence's first letter in lower case, for a part after "·". */
function lowerFirst(language: Language, text: string): string {
  return text.length === 0 ? text : text.charAt(0).toLocaleLowerCase(language) + text.slice(1);
}

/**
 * The hot water's lines under the heat's review: where the plan stands (in shadow, the gate's progress: "Varmvatten i
 * skuggning: dag 5 av 7, 8 av 10 toppar träffade"; steered; back in shadow), yesterday's review ("Varmvatten i går:
 * 1,1 kWh flyttade · 0,61 kr sparade (skuggning)") and each peak against what the pump did. Nothing where the pump has
 * no way to make hot water at SpotNav's word.
 */
export function hotWaterLines(language: Language, answer: HeatAnswer): { key: string; text: string }[] {
  const hotWater = answer.hot_water;
  if (hotWater === null || hotWater.level === null || hotWater.mode === "watch") {
    return [];
  }
  const out: { key: string; text: string }[] = [];
  const gate = hotWater.gate;
  if (hotWater.mode === "live") {
    out.push({ key: "water-live", text: translate(language, "heat.water.live") });
  } else if (hotWater.level === "B") {
    if (gate.open) {
      out.push({ key: "water-gate", text: translate(language, "heat.water.gateReady") });
    } else {
      let text = translate(language, "heat.water.gate", {
        days: String(Math.min(gate.days, gate.days_needed)),
        needed: String(gate.days_needed),
        hits: String(gate.hits),
        peaks: String(Math.max(gate.peaks, gate.peaks_needed)),
      });
      if (gate.days >= gate.days_needed && gate.weekdays < 7) {
        text += ` · ${translate(language, "heat.water.gateWeekdays", { weekdays: String(gate.weekdays) })}`;
      }
      out.push({ key: "water-gate", text });
    }
    if (gate.backed_off !== null) {
      out.push({ key: "water-backed-off", text: translate(language, "heat.water.backedOff") });
    }
  }
  const blocking = hotWater.blocking;
  if (blocking !== null && blocking.mode !== "watch") {
    out.push(...blockingLines(language, blocking));
  }
  const review = hotWater.review;
  if (review === null) {
    return out;
  }
  const money = answer.today?.money ?? null;
  let line = review.moved_kwh > 0
    ? translate(language, "heat.water.review", { kwh: kwhText(language, review.moved_kwh), value: amount(language, review.saved, money) })
    : translate(language, "heat.water.reviewNone");
  if (review.mode === "shadow") {
    line += ` ${translate(language, "heat.water.shadow")}`;
  }
  out.push({ key: "water-review", text: line });
  const ctx = context(language, answer);
  const peaks = review.per_peak.map((item) => {
    const key = item.cold_risk
      ? "heat.water.peakCold"
      : item.miss
        ? "heat.water.peakMiss"
        : item.hit
          ? "heat.water.peakHit"
          : "heat.water.peakNoUse";
    const parts = [translate(language, key, { peak: item.peak })];
    if (item.pick !== null && review.mode === "shadow") {
      parts.push(lowerFirst(language, translate(language, "heat.chart.waterWouldPast", { time: clock(ctx, Date.parse(item.pick)) })));
      if (item.pump_run !== null) {
        parts.push(translate(language, "heat.chart.waterPumpRan", { time: clock(ctx, Date.parse(item.pump_run)) }));
      }
    }
    return parts.join(" · ");
  });
  if (peaks.length > 0) {
    out.push({ key: "water-peaks", text: translate(language, "heat.water.peaks", { list: peaks.join("; ") }) });
  }
  if (blocking !== null && (review.blocks_cut > 0 || review.cold_risks > 0)) {
    out.push({
      key: "water-blocking-review",
      text: translate(language, review.a_mode === "live" ? "heat.water.blockingReview" : "heat.water.blockingReviewWould", {
        hours: String(review.blocks_cut),
        risks: String(review.cold_risks),
      }),
    });
  }
  return out;
}

/** Level A's lines: steered, or in shadow its gate's progress ("Blockeringar kring varmvattnet i skuggning: dag 5 av
 * 14, 8 av 10 toppar träffade"), and its stricter rules after too many late reheats. */
function blockingLines(language: Language, blocking: HeatHotWaterBlocking): { key: string; text: string }[] {
  const out: { key: string; text: string }[] = [];
  const gate = blocking.gate;
  if (blocking.mode === "live") {
    out.push({ key: "water-blocking-live", text: translate(language, "heat.water.blockingLive") });
  } else if (gate.open) {
    out.push({ key: "water-blocking-gate", text: translate(language, "heat.water.blockingReady") });
  } else {
    out.push({
      key: "water-blocking-gate",
      text: translate(language, "heat.water.blockingGate", {
        days: String(Math.min(gate.days, gate.days_needed)),
        needed: String(gate.days_needed),
        hits: String(gate.hits),
        peaks: String(Math.max(gate.peaks, gate.peaks_needed)),
      }),
    });
  }
  if (blocking.strict) {
    out.push({ key: "water-blocking-backed-off", text: translate(language, "heat.water.blockingBackedOff") });
  }
  return out;
}

/** The Boost button's words now, and what pressing does (`null`: no button, only the words). */
export function boostWords(language: Language, answer: HeatAnswer, nowMs: number): { text: string | null; action: "start" | "stop" | null; label: string | null } | null {
  const boost = answer.hot_water?.boost ?? null;
  if (boost === null) {
    return null;
  }
  const left = boost.ends === null ? null : Math.max(1, Math.ceil((Date.parse(boost.ends) - nowMs) / 60_000));
  switch (boost.state) {
    case "starting":
      return { text: null, action: null, label: translate(language, "heat.boost.starting") };
    case "running": {
      if (boost.source === "user") {
        const text = left === null
          ? translate(language, "heat.boost.on")
          : translate(language, boost.ends_estimated ? "heat.boost.left" : "heat.boost.leftExact", { minutes: String(left) });
        return { text, action: boost.stoppable ? "stop" : null, label: boost.stoppable ? translate(language, "heat.boost.stop") : null };
      }
      const text = left === null ? translate(language, "heat.boost.heatingOn") : translate(language, "heat.boost.heating", { minutes: String(left) });
      return { text, action: null, label: null };
    }
    case "failed":
      return { text: translate(language, "heat.boost.failed"), action: "start", label: translate(language, "heat.boost.start") };
    default:
      return { text: null, action: "start", label: translate(language, "heat.boost.start") };
  }
}

const DROP_PATH = "M7 1 C8.5 4 12 6.5 12 9.5 A5 5 0 0 1 2 9.5 C2 6.5 5.5 4 7 1 Z";

/** The Boost button with its state (Now, under the status lines); `null` where the pump has no self-ending boost. */
function boostRow(doc: Document, language: Language, answer: HeatAnswer, ui: HeatUi, handlers: HeatHandlers, nowMs: number): HTMLElement | null {
  const words = boostWords(language, answer, nowMs);
  if (words === null || handlers.onBoost === undefined) {
    return null;
  }
  const row = element(doc, "div", X.boost);
  row.dataset["block"] = "boost";
  row.dataset["state"] = answer.hot_water?.boost?.state ?? "idle";
  if (words.text !== null) {
    const line = element(doc, "p", X.boostLine, words.text);
    line.dataset["line"] = "boost";
    row.append(line);
  }
  if (words.label !== null) {
    const button = element(doc, "button", X.boostButton);
    button.type = "button";
    const icon = svg(doc, "svg", { viewBox: "0 0 14 14", "aria-hidden": "true" });
    icon.append(svg(doc, "path", { d: DROP_PATH }));
    button.append(icon, doc.createTextNode(ui.boostBusy === true && words.action === "start" ? translate(language, "heat.boost.starting") : words.label));
    const action = words.action;
    button.disabled = action === null || ui.boostBusy === true;
    if (action !== null) {
      button.dataset["action"] = `boost-${action}`;
      button.addEventListener("click", () => handlers.onBoost?.(action));
    }
    row.append(button);
  }
  if (ui.boostError !== undefined && ui.boostError !== null) {
    const line = element(doc, "p", `${X.boostLine} ${C.muted}`, ui.boostError);
    line.dataset["line"] = "boost-error";
    row.append(line);
  }
  return row;
}

// ------------------------------------------------------------------------------------------------ the hours ahead

/** How many hours the strip under the dial spans. */
export const AHEAD_HOURS = 24;
const AHEAD_ACTIONS = ["lower", "preheat", "night", "away"] as const;

/**
 * Whether a pre-heat is a raise of the pump: through SG Ready's second contact, the SG Ready API's low-price mode
 * (which needs its activation switch), or a curve offset above the value found.
 */
function raisable(answer: HeatAnswer): boolean {
  const { control_path: path, control_entity: first, control_entity_b: second } = answer.config;
  if (first === null) {
    return false;
  }
  return path === "offset" || ((path === "sg_ready" || path === "sg_ready_api") && second !== null);
}

/**
 * The plan's next hours in words: the first run of each thing it does ("sänkning 17:00–19:00 · svalare natt
 * 23:00–05:00 · varmvatten 04:00"), or that it lowers nothing; `null` without hours ahead.
 */
export function aheadLine(language: Language, answer: HeatAnswer): string | null {
  const hours = answer.ahead;
  if (hours.length === 0) {
    return null;
  }
  const ctx = context(language, answer);
  const parts: { at: number; text: string }[] = [];
  for (const action of AHEAD_ACTIONS) {
    const first = hours.findIndex((hour) => hour.action === action);
    if (first < 0) {
      continue;
    }
    let last = first;
    while (last + 1 < hours.length && hours[last + 1]!.action === action) {
      last += 1;
    }
    const key = action === "preheat" && raisable(answer) ? "raise" : action;
    parts.push({
      at: hours[first]!.startMs,
      text: translate(language, `heat.ahead.${key}` as TranslationKey, { from: clock(ctx, hours[first]!.startMs), to: clock(ctx, hours[last]!.startMs + HOUR_MS) }),
    });
  }
  // Hot water at its planned boost's own time where there is one ("varmvatten 04:15"), else at its hour.
  const nowMs = Date.parse(answer.now);
  const lastMs = hours[hours.length - 1]!.startMs + HOUR_MS;
  const planned = (answer.hot_water?.planned ?? []).map((item) => Date.parse(item.start)).filter((ms) => ms >= nowMs && ms < lastMs);
  const water = hours.find((hour) => hour.hot_water);
  const waterMs = planned.length > 0 ? Math.min(...planned) : water?.startMs;
  if (waterMs !== undefined) {
    parts.push({ at: waterMs, text: translate(language, "heat.ahead.water", { time: clock(ctx, waterMs) }) });
  }
  if (parts.length === 0) {
    return translate(language, "heat.ahead.none");
  }
  return translate(language, "heat.ahead.lead", { parts: parts.sort((a, b) => a.at - b.at).map((part) => part.text).join(" · ") });
}

/** The thin strip under the dial: the plan's next 24 hours, a cell an hour (a lowering, a pre-heat, the cooler
 * night, away, hot water as a dot), with the hours under it and the same in words (`aheadLine`). Hours the plan does
 * not reach yet (tomorrow before its prices) are left open. `null` without hours ahead. */
function aheadStrip(doc: Document, language: Language, answer: HeatAnswer): HTMLElement | null {
  const words = aheadLine(language, answer);
  const hours: (HeatAheadHour | null)[] = answer.ahead.slice(0, AHEAD_HOURS);
  if (words === null || hours.length === 0) {
    return null;
  }
  const first = hours[0]!.startMs;
  while (hours.length < AHEAD_HOURS) {
    hours.push(null);
  }
  const block = element(doc, "div", X.ahead);
  block.dataset["block"] = "ahead";
  const row = element(doc, "div", X.aheadRow);
  row.setAttribute("aria-hidden", "true");
  const axis = element(doc, "div", `${X.aheadAxis} ${C.muted}`);
  axis.setAttribute("aria-hidden", "true");
  const ctx = context(language, answer);
  hours.forEach((hour, index) => {
    const cell = element(doc, "span", X.aheadCell);
    cell.dataset["action"] = hour === null ? "open" : hour.action === "preheat" && raisable(answer) ? "raise" : hour.action;
    if (hour?.hot_water === true) {
      cell.dataset["water"] = "true";
    }
    row.append(cell);
    if (index % 3 === 0) {
      axis.append(element(doc, "span", undefined, clock(ctx, first + index * HOUR_MS)));
    }
  });
  const line = element(doc, "p", `${X.aheadLine} ${C.muted}`, words);
  line.dataset["line"] = "ahead";
  block.append(row, axis, line);
  return block;
}

/** A run of a load's plan as its band draws it: what it does ("filter", "heat", "extra", "hold") from start to end. */
export interface BandRun {
  start: string;
  end: string;
  what: string;
}

/**
 * The pool's, the spa's or the sauna's day band: the next 24 hours from the running one, a cell an hour coloured by
 * what the load does (or would do) in it (the run that covers most of the hour, at least a quarter), the hours under
 * it and `words` under them. Drawn as the Now tab's strip of the heat plan.
 */
export function runBand(doc: Document, language: Language, answer: HeatAnswer, runs: readonly BandRun[], words: string | null, block: string): HTMLElement {
  const nowMs = Date.parse(answer.now);
  const first = Math.floor(nowMs / HOUR_MS) * HOUR_MS;
  const band = element(doc, "div", X.ahead);
  band.dataset["block"] = block;
  const row = element(doc, "div", X.aheadRow);
  row.setAttribute("aria-hidden", "true");
  const axis = element(doc, "div", `${X.aheadAxis} ${C.muted}`);
  axis.setAttribute("aria-hidden", "true");
  for (let index = 0; index < AHEAD_HOURS; index += 1) {
    const from = first + index * HOUR_MS;
    let best: { what: string; overlap: number } | null = null;
    for (const run of runs) {
      const overlap = Math.min(from + HOUR_MS, Date.parse(run.end)) - Math.max(from, Date.parse(run.start));
      if (overlap >= HOUR_MS / 4 && (best === null || overlap > best.overlap)) {
        best = { what: run.what, overlap };
      }
    }
    const cell = element(doc, "span", X.aheadCell);
    if (best !== null) {
      cell.dataset["run"] = best.what;
    }
    row.append(cell);
    if (index % 3 === 0) {
      axis.append(element(doc, "span", undefined, heatClock(language, answer, from)));
    }
  }
  band.append(row, axis);
  if (words !== null) {
    const line = element(doc, "p", `${X.aheadLine} ${C.muted}`, words);
    line.dataset["line"] = block;
    band.append(line);
  }
  return band;
}

/** The first runs of a plan in words, each as its time span ("22:00–02:00, 12:00–14:00"), at most `count`. */
export function runsText(language: Language, answer: HeatAnswer, runs: readonly { start: string; end: string }[], count = 3): string {
  return windowsText(language, answer, [...runs], count);
}

// ------------------------------------------------------------------------------------------------ the day

/** A price per kWh in the minor unit ("42 öre"). */
function perKwh(language: Language, minor: number, money: { minor_unit: string } | null): string {
  return money === null ? formatFixed(language, minor, 0) : `${formatFixed(language, minor, 0)} ${money.minor_unit}`;
}

/** One planned boost in words: what SpotNav does or would do, and once past in shadow, what the pump did itself
 * ("Skulle ha värmt varmvatten 04:15 · pumpen körde själv 06:20"). */
export function plannedWaterText(language: Language, answer: HeatAnswer, item: { start: string; end: string; price: number; reason: string; peak: string; live: boolean; pump_run: string | null }): string {
  const ctx = context(language, answer);
  const startMs = Date.parse(item.start);
  const past = startMs <= Date.parse(answer.now);
  const parts: string[] = [];
  if (!item.live && past) {
    parts.push(translate(language, "heat.chart.waterWouldPast", { time: clock(ctx, startMs) }));
  } else {
    parts.push(
      translate(language, item.live ? "heat.chart.water" : "heat.chart.waterWould", {
        from: clock(ctx, startMs),
        to: clock(ctx, Date.parse(item.end)),
        price: perKwh(language, item.price, answer.today?.money ?? null),
        peak: item.peak,
      }),
    );
  }
  if (item.reason === "sun") {
    parts.push(translate(language, "heat.chart.waterSun"));
  }
  if (!item.live && item.pump_run !== null) {
    parts.push(translate(language, "heat.chart.waterPumpRan", { time: clock(ctx, Date.parse(item.pump_run)) }));
  }
  return parts.join(" · ");
}

/** The day chart's hot water: the measured runs (filled droplets), the boosts planned (hollow ones) and the peaks
 * (ticks); without the hot-water block, the band's hours marked hot water. */
function waterOf(
  language: Language,
  answer: HeatAnswer,
  hours: HeatBandHour[],
  startMs: number,
  endMs: number,
): { water: DaySpan[]; planned: DayPlannedWater[]; peaks: DayPeak[]; notes: DayNote[] } {
  const hotWater = answer.hot_water;
  if (hotWater === null) {
    return {
      water: hours.filter((hour) => hour.hot_water).map((hour) => ({ startMs: hour.startMs, endMs: hour.startMs + HOUR_MS })),
      planned: [],
      peaks: [],
      notes: [],
    };
  }
  const runs = hotWater.runs_today.length > 0 ? hotWater.runs_today : (answer.today?.runs ?? []);
  const water = runs.map((run) => ({ startMs: Date.parse(run.start), endMs: Date.parse(run.end) }));
  const planned = hotWater.planned
    .map((item) => ({ startMs: Date.parse(item.start), endMs: Date.parse(item.end), text: plannedWaterText(language, answer, item) }))
    .filter((item) => item.endMs > startMs && item.startMs < endMs);
  const date = instantToLocal(Date.parse(answer.now), answer.time_zone).date;
  const ctx = context(language, answer);
  const peaks: DayPeak[] = [];
  for (const peak of hotWater.peaks) {
    const ms = localToInstant(date, peak.ready_by, answer.time_zone);
    if (ms !== null && ms >= startMs && ms < endMs) {
      peaks.push({ ms, label: translate(language, "heat.chart.peak", { time: clock(ctx, ms) }) });
    }
  }
  const notes = (hotWater.blocking?.cuts ?? [])
    .map((cut) => ({ startMs: Date.parse(cut.start), endMs: Date.parse(cut.end), text: cutText(language, cut) }))
    .filter((note) => note.endMs > startMs && note.startMs < endMs);
  return { water, planned, peaks, notes };
}

const CUT_KEYS: Record<string, [TranslationKey, TranslationKey]> = {
  peak: ["heat.chart.cutPeak", "heat.chart.cutPeakWould"],
  day_cap: ["heat.chart.cutDay", "heat.chart.cutDayWould"],
  hygiene: ["heat.chart.cutHygiene", "heat.chart.cutHygieneWould"],
  moved: ["heat.chart.cutMoved", "heat.chart.cutMovedWould"],
};

/** An hour level A took out of the heat plan, in words: "blockering förkortad före duschen 06:00" (live) or "skulle
 * förkorta blockeringen före duschen 06:00" (shadow). */
export function cutText(language: Language, cut: HeatBlockingCut): string {
  const keys = CUT_KEYS[cut.reason] ?? CUT_KEYS["day_cap"]!;
  return translate(language, cut.live ? keys[0] : keys[1], { time: cut.peak ?? "" });
}

/** Today's chart (`heat-day-chart.ts`) from the answer's band and rooms; `null` with nothing to draw. */
function todayChart(input: HeatRenderInput, answer: HeatAnswer): HTMLElement | null {
  const { doc, language, ui, handlers } = input;
  const hours = answer.band.hours;
  const indoor = answer.indoor_day;
  const startMs = indoor !== null ? Date.parse(indoor.start) : hours.length > 0 ? hours[0]!.startMs : Number.NaN;
  const endMs = indoor !== null ? Date.parse(indoor.end) : hours.length > 0 ? hours[hours.length - 1]!.startMs + HOUR_MS : Number.NaN;
  return dayChart({
    doc,
    language,
    ctx: context(language, answer),
    startMs,
    endMs,
    day: indoor,
    low_c: indoor?.low_c ?? answer.comfort.low_c,
    high_c: indoor?.high_c ?? answer.comfort.high_c,
    hours: hours.map((hour) => ({
      startMs: hour.startMs,
      target_c: hour.target_c,
      action: hour.action,
      reason: hour.reason,
      executed_min: hour.executed_min,
      raised_min: hour.raised_min,
      relay_min: hour.relay_min,
      relay_expected: hour.relay_expected,
      car: hour.car,
    })),
    ...waterOf(language, answer, hours, startMs, endMs),
    width: input.width,
    nowMs: input.nowMs,
    shadow: answer.shadow,
    selectedMs: ui.chartSelectedMs ?? null,
    onSelect: (ms) => handlers.onChartSelect?.(ms),
    height: ui.fill ?? null,
  });
}

/** A past day's chart from its history: the rooms by the hour, SpotNav's setpoint and the hot-water runs. */
function pastChart(input: HeatRenderInput, history: HeatHistory): HTMLElement | null {
  const day = history.indoor_day;
  if (day === null) {
    return null;
  }
  const { doc, language } = input;
  return dayChart({
    doc,
    language,
    ctx: { language, timeZone: history.time_zone, unit: "kWh", currency: null, majorUnit: null },
    startMs: Date.parse(day.start),
    endMs: Date.parse(day.end),
    day,
    low_c: day.low_c,
    high_c: day.high_c,
    hours: hoursOf(day),
    water: history.runs.map((run) => ({ startMs: Date.parse(run.start), endMs: Date.parse(run.end) })),
    width: input.width,
    nowMs: null,
    height: input.ui.fill ?? null,
  });
}

// ------------------------------------------------------------------------------------------------ today and notes

/** "I dag · värme 13,9 kWh · varav zoner 0,8 kWh · varmvatten 1,7 kWh · 19 kr": heat holds the zones' own energy. */
export function todayLine(language: Language, answer: HeatAnswer): string | null {
  const today = answer.today;
  if (today === null) {
    return null;
  }
  const parts = [translate(language, "heat.today.label"), translate(language, "heat.today.heat", { kwh: kwhText(language, today.heat_kwh) })];
  if (today.zones_kwh !== null && today.zones_kwh > 0) {
    parts.push(translate(language, "heat.today.zones", { kwh: kwhText(language, today.zones_kwh) }));
  }
  if (today.hot_water_kwh !== null) {
    parts.push(translate(language, "heat.today.water", { kwh: kwhText(language, today.hot_water_kwh) }));
  }
  if (today.cost !== null) {
    parts.push(amount(language, today.cost, today.money));
  }
  return parts.join(" · ");
}

const OTHER_CONTROLLERS: Record<string, string> = { effektguard: "EffektGuard" };
const ROLES = [
  "outdoor_temp", "supply_temp", "return_temp", "room_temp", "hot_water_temp", "degree_minutes",
  "compressor_speed", "compressor_state", "immersion_power", "immersion_state", "priority",
];

/** One sentence per note the household can act on; an unknown code is left out. */
export function noteLines(language: Language, answer: HeatAnswer): { code: string; text: string }[] {
  const out: { code: string; text: string }[] = [];
  for (const note of answer.notes) {
    const params = note.params;
    if (note.code === "pump_price_control") {
      const where = typeof params["where"] === "string" && ["nibe_smart_price", "ctc_smartgrid"].includes(params["where"])
        ? translate(language, `heat.where.${params["where"]}` as TranslationKey)
        : "";
      const text = translate(language, "heat.note.pump_price_control", { label: String(params["label"] ?? ""), where });
      out.push({ code: note.code, text: where === "" ? text.replace(" ()", "") : text });
    } else if (note.code === "readings_disabled") {
      const roles = Array.isArray(params["entities"]) ? (params["entities"] as unknown[]) : [];
      const names = roles.map((role) => (typeof role === "string" && ROLES.includes(role) ? translate(language, `heat.role.${role}` as TranslationKey) : String(role)));
      out.push({ code: note.code, text: translate(language, "heat.note.readings_disabled", { names: names.join(", ") }) });
    } else if (note.code === "other_controller") {
      const names = Array.isArray(params["names"]) ? (params["names"] as unknown[]).map((name) => OTHER_CONTROLLERS[String(name)] ?? String(name)) : [];
      out.push({ code: note.code, text: translate(language, "heat.note.other_controller", { names: names.join(", ") }) });
    } else if (note.code === "automation_steers") {
      const name = String(params["name"] ?? "");
      const targets = Array.isArray(params["targets"]) ? (params["targets"] as unknown[]).map(String) : [];
      const key: TranslationKey = params["blocks"] === true
        ? "heat.note.automation_blocks"
        : params["relay"] === true ? "heat.note.automation_relay" : "heat.note.automation_zone";
      out.push({ code: note.code, text: translate(language, key, { name, targets: targets.join(", ") }) });
    } else if (note.code === "water_heater_not_full") {
      out.push({ code: note.code, text: translate(language, "heat.note.water_heater_not_full") });
    }
  }
  return out;
}

// ------------------------------------------------------------------------------------------------ zones and the water heater

const ZONE_WAITS: readonly string[] = ["automation", "foreign_change", "no_read_back", "check_failed", "not_qualified", "unavailable", "write_cap"];
const HEATER_WAITS: readonly string[] = ["automation", "foreign_on", "foreign_off", "no_read_back", "check_failed", "not_full", "unavailable"];
const HEATER_WHY: readonly string[] = ["cheap", "full_heat", "max_off", "sensor_lost", "no_plan", "tank_low", "write_cap"];

/** A zone's line under its switch: what SpotNav holds on the thermostat, or what it waits for; `null` while the zone
 * is watched only. */
export function zoneLine(language: Language, answer: HeatAnswer, zone: HeatZone): string | null {
  const control = zone.control;
  if (control === null || !control.enabled || control.state === "off") {
    return null;
  }
  if (control.state === "verifying") {
    return translate(language, "heat.zone.verifying");
  }
  if (control.state === "running") {
    if (control.held_c !== null) {
      const value = degrees(language, control.held_c);
      return control.until === null
        ? translate(language, "heat.zone.heldNow", { value })
        : translate(language, "heat.zone.held", { value, until: whenLabel(language, answer, Date.parse(control.until)) });
    }
    return control.home_c === null ? null : translate(language, "heat.zone.own", { value: degrees(language, control.home_c) });
  }
  const reason = control.reason ?? "";
  return ZONE_WAITS.includes(reason)
    ? translate(language, `heat.zone.wait.${reason}` as TranslationKey, { name: control.automation?.name ?? "" })
    : translate(language, "heat.zone.waiting");
}

/** The water heater's windows ahead as the strip says hours ("01:00–04:00, 14:00–17:00"), the first `count`. */
export function windowsText(language: Language, answer: HeatAnswer, windows: { start: string; end: string }[], count = 3): string {
  const nowMs = Date.parse(answer.now);
  return windows
    .filter((window) => Date.parse(window.end) > nowMs)
    .slice(0, count)
    .map((window) => `${whenLabel(language, answer, Math.max(Date.parse(window.start), nowMs))}–${clock(context(language, answer), Date.parse(window.end))}`)
    .join(", ");
}

/** The water heater's line under its switch: watched (the windows SpotNav would allow), on and why, off until when,
 * or what it waits for. `null` without a water heater. */
export function waterHeaterLine(language: Language, answer: HeatAnswer): string | null {
  const line = waterHeaterBaseLine(language, answer);
  const heater = answer.water_heater;
  if (line === null || heater === null || !heater.meter_waiting) {
    return line;
  }
  return `${line} ${translate(language, "heat.waterHeater.meterWaiting")}`;
}

function waterHeaterBaseLine(language: Language, answer: HeatAnswer): string | null {
  const heater = answer.water_heater;
  if (heater === null) {
    return null;
  }
  if (!answer.config.water_heater?.control || heater.state === "off") {
    const windows = windowsText(language, answer, heater.windows);
    return windows === "" ? translate(language, "heat.waterHeater.watchedNone") : translate(language, "heat.waterHeater.watched", { windows });
  }
  if (heater.state === "verifying" && heater.reason === "check") {
    return translate(language, "heat.waterHeater.verifying");
  }
  if (heater.state === "running" || heater.state === "verifying") {
    if (heater.on === false && heater.until !== null) {
      // Paused by the shed order under the ceiling ("Pausad för taket till 18:00"), else off by the plan.
      const key = heater.reason === "ceiling" ? "heat.waterHeater.ceiling" : "heat.waterHeater.off";
      return translate(language, key, { until: whenLabel(language, answer, Date.parse(heater.until)) });
    }
    const reason = heater.reason ?? "";
    if (heater.on === true && HEATER_WHY.includes(reason)) {
      return translate(language, "heat.waterHeater.on", { why: translate(language, `heat.waterHeater.why.${reason}` as TranslationKey) });
    }
    return null;
  }
  const reason = heater.reason ?? "";
  return HEATER_WAITS.includes(reason)
    ? translate(language, `heat.waterHeater.wait.${reason}` as TranslationKey, { name: heater.automation?.name ?? "" })
    : translate(language, "heat.zone.waiting");
}

/** Now: one line per thing SpotNav holds now beside the pump ("Badrumsgolv 20 °C till 19:00", "Varmvattenberedaren av
 * till 01:00"), and the water heater's next windows. */
export function holdLines(language: Language, answer: HeatAnswer): { key: string; text: string }[] {
  const out: { key: string; text: string }[] = [];
  for (const zone of answer.zones) {
    const control = zone.control;
    if (control === null || control.state !== "running" || control.held_c === null) {
      continue;
    }
    const value = degrees(language, control.held_c);
    out.push({
      key: `zone:${zone.entity_id}`,
      text: control.until === null
        ? translate(language, "heat.now.zoneNow", { name: zone.name, value })
        : translate(language, "heat.now.zone", { name: zone.name, value, until: whenLabel(language, answer, Date.parse(control.until)) }),
    });
  }
  const heater = answer.water_heater;
  if (heater !== null) {
    if (heater.live && heater.on === false && heater.until !== null) {
      const key = heater.reason === "ceiling" ? "heat.now.waterHeaterCeiling" : "heat.now.waterHeaterOff";
      out.push({ key: "waterHeater", text: translate(language, key, { until: whenLabel(language, answer, Date.parse(heater.until)) }) });
    }
    // The windows within the strip's 24 hours.
    const horizon = Date.parse(answer.now) + AHEAD_HOURS * 3_600_000;
    const ahead = heater.windows.filter((window) => Date.parse(window.start) < horizon);
    const windows = windowsText(language, answer, ahead);
    if (windows !== "") {
      out.push({ key: "waterHeaterWindows", text: translate(language, heater.live ? "heat.now.waterHeaterWindows" : "heat.now.waterHeaterWould", { windows }) });
    }
  }
  return out;
}

// ------------------------------------------------------------------------------------------------ the body

function boxed(input: HeatRenderInput): boolean {
  return input.ui.fill !== null && input.ui.fill !== undefined;
}

function statusParagraph(doc: Document, language: Language, key: TranslationKey, line: string): HTMLElement {
  const paragraph = element(doc, "p", `${C.muted} ${H.line}`, translate(language, key));
  paragraph.setAttribute("role", "status");
  paragraph.dataset["line"] = line;
  return paragraph;
}

/** The shown period's history, or the line saying it is on its way or could not be read. */
function historyOrStatus(input: HeatRenderInput, panel: HTMLElement): HeatHistory | null {
  const history = input.tabs?.history ?? null;
  if (history === null || history.kind === "loading") {
    const line = statusParagraph(input.doc, input.language, "hist.loading", "loading");
    line.setAttribute("aria-busy", "true");
    panel.append(line);
    return null;
  }
  if (history.kind === "failed") {
    const unsupported = history.code === "unknown_command" || history.code === "spotnav_unsupported_api_version";
    const line = statusParagraph(input.doc, input.language, unsupported ? "hist.unsupported" : "hist.failed", "failed");
    if (history.code !== null) {
      line.dataset["code"] = history.code;
    }
    panel.append(line);
    return null;
  }
  return history.answer;
}

/** The day Plan shows: the shared period while it is a day, else Plan's own day (today by default). */
export function planChoice(ui: HeatUi): PeriodChoice {
  const choice = ui.choice ?? { period: "day", date: null };
  return choice.period === "day" ? choice : { period: "day", date: ui.planDate ?? null };
}

/** Now: the dial, the measured room, home or away, what SpotNav would do and the relay, the strip of the hours
 * ahead, today's figures and the notes. In a box of fixed height the dial takes the spare room and the explaining
 * lines are left out. */
function nowPanel(input: HeatRenderInput, answer: HeatAnswer, panel: HTMLElement): void {
  const { doc, language } = input;
  const target = input.ui.pendingTarget ?? answer.comfort.target_c;
  // On heating SpotNav can only read, the target is shown read-only and Home/Away is left out: neither could act.
  const readOnly = !heatSteers(answer.config);
  panel.append(dial(doc, language, answer, target, input.ui, input.handlers, input.ui.fill ?? null, readOnly));
  const measured = element(doc, "p", X.measured, measuredLine(language, answer));
  measured.dataset["line"] = "measured";
  panel.append(measured, ...(readOnly ? [] : modes(doc, language, answer, input.handlers)));
  if (readOnly) {
    // One line on where a control path is chosen; the gear is an administrator's.
    const line = element(
      doc,
      "p",
      `${X.shadow} ${C.muted}`,
      translate(language, answer.writable ? "heat.readOnly.admin" : "heat.readOnly"),
    );
    line.dataset["line"] = "readOnly";
    panel.append(line);
  } else if (answer.shadow && !boxed(input)) {
    // While nothing is controlled, what the dial and home or away are for: what SpotNav follows once it controls.
    const line = element(doc, "p", `${X.shadow} ${C.muted}`, translate(language, "heat.shadow"));
    line.dataset["line"] = "shadow";
    panel.append(line);
  }
  const lines = element(doc, "div", X.statusBlock);
  // The header says what the heat pump does; here only while the header is taken by SpotNav's own lowering or raise.
  const live = heatLive(language, answer);
  const pump = live === null || live.code === "lower" || live.code === "raise" ? pumpLine(language, answer) : null;
  for (const [key, text] of [
    ["pump", pump],
    ["would", wouldLine(language, answer)],
    ["relay", relayLine(language, answer, input.nowMs)],
  ] as const) {
    if (text !== null) {
      const line = element(doc, "p", X.status, text);
      line.dataset["line"] = key;
      lines.append(line);
    }
  }
  for (const hold of holdLines(language, answer)) {
    const line = element(doc, "p", X.status, hold.text);
    line.dataset["line"] = hold.key;
    lines.append(line);
  }
  if (lines.childElementCount > 0) {
    panel.append(lines);
  }
  const boost = boostRow(doc, language, answer, input.ui, input.handlers, input.nowMs);
  if (boost !== null) {
    panel.append(boost);
  }
  const strip = aheadStrip(doc, language, answer);
  if (strip !== null) {
    panel.append(strip);
  }
  const today = todayLine(language, answer);
  if (today !== null) {
    const line = element(doc, "p", H.line, today);
    line.dataset["line"] = "today";
    panel.append(line);
  }
  if (boxed(input)) {
    return;
  }
  for (const note of noteLines(language, answer)) {
    const line = element(doc, "p", `${H.note} ${C.muted}`, note.text);
    line.dataset["note"] = note.code;
    panel.append(line);
  }
}

/** Plan: a day's chart, today's (with the plan ahead) or a past day's, under a picker of days alone; under today's,
 * yesterday's review. */
function planPanel(input: HeatRenderInput, answer: HeatAnswer, panel: HTMLElement, today: string): void {
  const { doc, language } = input;
  const choice = planChoice(input.ui);
  panel.append(periodPicker(doc, language, choice, today, (next) => input.handlers.onPeriod?.(next, true), null, ["day"]));
  if (choice.date !== null) {
    const history = historyOrStatus(input, panel);
    if (history !== null) {
      panel.append(pastChart(input, history) ?? statusParagraph(doc, language, "heat.plan.none", "no-day"));
    }
    return;
  }
  const chart = todayChart(input, answer);
  if (chart === null) {
    panel.append(statusParagraph(doc, language, "heat.plan.none", "no-day"));
    return;
  }
  const block = element(doc, "section", X.daySection);
  block.dataset["block"] = "band";
  block.append(chart);
  panel.append(block);
  panel.append(...loadBands(doc, language, answer));
  if (boxed(input)) {
    return;
  }
  for (const review of [...reviewLines(language, answer), ...hotWaterLines(language, answer)]) {
    const line = element(doc, "p", `${H.line} ${C.muted}`, review.text);
    line.dataset["line"] = review.key;
    panel.append(line);
  }
}

/** Plan: under the day's chart, the pool's, the spa's and the sauna's next 24 hours, each a band named by its load
 * ("Pool: Filtrerar 22:00–02:00 · Värmer 12:00–14:00"). */
function loadBands(doc: Document, language: Language, answer: HeatAnswer): HTMLElement[] {
  const out: HTMLElement[] = [];
  const named = (kind: LoadKind, words: string | null): string => `${translate(language, `heat.tab.${kind}` as TranslationKey)}: ${words ?? translate(language, "heat.pool.plan.none")}`;
  if (answer.pool !== null && answer.pool.season_open) {
    out.push(runBand(doc, language, answer, answer.pool.planned, named("pool", poolPlanLine(language, answer, answer.pool)), "plan-pool"));
  }
  if (answer.spa !== null) {
    out.push(runBand(doc, language, answer, answer.spa.planned, named("spa", deadlinePlanLine(language, answer, "spa", answer.spa)), "plan-spa"));
  }
  if (answer.sauna !== null) {
    out.push(runBand(doc, language, answer, answer.sauna.planned, named("sauna", deadlinePlanLine(language, answer, "sauna", answer.sauna)), "plan-sauna"));
  }
  return out;
}

/** The Use tab's bars' height (px) outside a box. */
function barsHeight(input: HeatRenderInput): number {
  return Math.round(input.ui.fill ?? Math.min(200, Math.max(130, input.width * 0.36)));
}

/** Use: the period picker and the period's use by what is measured (`heat-use.ts`). */
function useTab(input: HeatRenderInput, panel: HTMLElement, today: string): void {
  const { doc, language, handlers } = input;
  const choice = input.ui.choice ?? { period: "day", date: null };
  panel.append(periodPicker(doc, language, choice, today, (next) => handlers.onPeriod?.(next, false)));
  const history = historyOrStatus(input, panel);
  if (history === null) {
    return;
  }
  panel.append(
    ...usePanel({
      doc,
      language,
      answer: history,
      hidden: input.ui.useHidden ?? [],
      unit: input.ui.useUnit ?? "kwh",
      height: barsHeight(input),
      selected: input.ui.bar ?? null,
      boxed: boxed(input),
      onUnit: (unit) => handlers.onUnit?.(unit),
      onToggle: (hidden) => handlers.onUsePart?.(hidden),
      ...(handlers.onBar === undefined ? {} : { onSelect: (key: string | null) => handlers.onBar?.(key) }),
    }),
  );
}

export function heatBody(input: HeatRenderInput): HTMLElement {
  const { doc, language, state } = input;
  const card = element(doc, "div", `${C.card} ${H.card} ${X.card}`);
  card.dataset["state"] = state.kind;
  if (state.kind !== "ready") {
    card.append(...header(doc, language, null, null, false, input.idPrefix));
    const key: TranslationKey = state.kind === "unconfigured" ? "heat.unconfigured" : state.kind === "loading" ? "heat.loading" : state.sentenceKey;
    const line = element(doc, "p", C.muted, translate(language, key));
    line.setAttribute("role", "status");
    if (state.kind === "failed" && state.code !== null) {
      line.dataset["code"] = state.code;
    }
    card.append(line);
    return card;
  }
  const answer = state.answer;
  const idPrefix = input.idPrefix ?? "spotnav-heat";
  card.append(...header(doc, language, answer, input.handlers, input.ui.testOpen === true, input.idPrefix));
  if (!heatFound(answer)) {
    // Nothing heat-related on the site: no dial or tabs to act on, one line on where heating is added.
    card.dataset["empty"] = "true";
    const line = element(doc, "p", C.muted, translate(language, answer.writable ? "heat.empty.admin" : "heat.empty"));
    line.setAttribute("role", "status");
    line.dataset["line"] = "empty";
    card.append(line);
    return card;
  }
  const locked = input.ui.locked ?? null;
  const tabs = heatTabsOf(answer);
  const wanted: HeatTab = locked ?? input.ui.tab ?? "now";
  // A tab remembered for a pool, spa or sauna that is no longer there falls back to Now.
  const tab: HeatTab = locked !== null || tabs.includes(wanted) ? wanted : "now";
  card.dataset["tab"] = tab;
  if (locked === null) {
    card.append(heatTabList(doc, language, tab, idPrefix, (next, byKey) => input.handlers.onTab?.(next, byKey), input.ui.tabFit ?? "long", tabs));
  }
  const panel = heatTabPanel(doc, tab, idPrefix, locked === null);
  card.append(panel);
  const today = input.tabs?.today ?? instantToLocal(input.nowMs, answer.time_zone).date;
  if (tab === "now") {
    nowPanel(input, answer, panel);
  } else if (tab === "plan") {
    planPanel(input, answer, panel, today);
  } else if (tab === "use") {
    useTab(input, panel, today);
  } else {
    loadTab(input, answer, tab, panel);
  }
  return card;
}

/** What the site has of a load with a tab: set up (its block in the answer), detected (an administrator's choices
 * hold a sure candidate, not set up yet), or nothing. */
export function loadFound(answer: HeatAnswer, kind: LoadKind): "set_up" | "detected" | null {
  if (answer[kind] !== null) {
    return "set_up";
  }
  const choices = answer.choices;
  if (choices === null || answer.config[kind] !== null) {
    return null;
  }
  const detected = kind === "pool" ? choices.pool?.suggested != null : choices[kind].some((choice) => !choice.ask);
  return detected ? "detected" : null;
}

/** The tabs this card shows: Now, Plan and Use, then the pool's, the spa's and the sauna's where the site has one. */
export function heatTabsOf(answer: HeatAnswer): HeatTab[] {
  return [...BASE_TABS, ...(["pool", "spa", "sauna"] as const).filter((kind) => loadFound(answer, kind) !== null)];
}

/** The pool's, the spa's or the sauna's tab; a detected one not set up yet says where it is set up. */
function loadTab(input: HeatRenderInput, answer: HeatAnswer, kind: LoadKind, panel: HTMLElement): void {
  const { doc, language } = input;
  const fill = input.ui.fill ?? null;
  const loadInput = { doc, language, answer, ui: input.ui, handlers: input.handlers, fill };
  if (kind === "pool" && answer.pool !== null) {
    poolPanel(loadInput, answer.pool, panel);
    return;
  }
  if (kind === "spa" && answer.spa !== null) {
    deadlinePanel(loadInput, "spa", answer.spa, panel);
    return;
  }
  if (kind === "sauna" && answer.sauna !== null) {
    deadlinePanel(loadInput, "sauna", answer.sauna, panel);
    return;
  }
  const found = loadFound(answer, kind);
  const line = element(doc, "p", `${C.muted} ${H.line}`, translate(language, found === "detected" ? `heat.${kind}.detected` as TranslationKey : `heat.${kind}.none` as TranslationKey));
  line.setAttribute("role", "status");
  line.dataset["line"] = `${kind}-none`;
  panel.append(line);
  if (found === "detected" && answer.writable) {
    const button = element(doc, "button", X.boostButton, translate(language, "heat.load.setUp")) as HTMLButtonElement;
    button.type = "button";
    button.dataset["action"] = `${kind}-set-up`;
    button.addEventListener("click", () => input.handlers.onSettings());
    panel.append(button);
  }
}

// ------------------------------------------------------------------------------------------------ settings

const CONTROL_WAITS = [
  "automation", "other_controller", "pump_price_control", "relay_unavailable", "relay_closed", "no_relay", "no_signal",
  "check_failed", "foreign_change", "no_read_back",
];
const CONTROL_SAFETY = ["too_cold", "room_cold", "stale", "sg_ready_b", "hot_water", "hot_water_low"];
/** The waits whose words name the relay: the other paths have their own (`heat.control.path.wait.*`). */
const PATH_WAITS = ["relay_unavailable", "relay_closed", "no_relay", "no_signal", "check_failed", "foreign_change", "no_read_back"];

/**
 * The line under "Styr värmen": off, checking the pump answers the relay, controlling (lowered until when, or the
 * pump's normal curve and why), or what it waits for ("Väntar: stäng av automationen '…' så att SpotNav kan styra
 * värmen."). `null` without the control's status.
 */
export function controlLine(language: Language, answer: HeatAnswer): string | null {
  const control = answer.control;
  if (control === null) {
    return null;
  }
  if (!control.enabled || control.state === "off") {
    return translate(language, "heat.control.off");
  }
  // The relay's words name the relay; the paths through the pump's own settings have their own.
  const own = isRelayPath(answer.config.control_path) ? "heat.control" : "heat.control.path";
  if (control.state === "verifying") {
    return translate(language, `${own}.${control.reason === "wait" ? "verifyingWait" : "verifying"}` as TranslationKey);
  }
  if (control.state === "running") {
    if (control.raised) {
      return control.raise_until === null
        ? translate(language, "heat.control.raisedNow")
        : translate(language, "heat.control.raised", { until: whenLabel(language, answer, Date.parse(control.raise_until)) });
    }
    if (control.lowered) {
      const warm = answer.status.would?.reason === "room_warm";
      // Lowered one step by the shed order under the ceiling where the plan alone would not.
      const lowered = control.reason === "ceiling" ? "heat.control.loweredCeiling" : warm ? "heat.control.loweredWarm" : "heat.control.lowered";
      return control.until === null
        ? translate(language, "heat.control.loweredNow")
        : translate(language, lowered, { until: whenLabel(language, answer, Date.parse(control.until)) });
    }
    const reason = control.reason ?? "";
    return translate(language, (CONTROL_SAFETY.includes(reason) ? `heat.control.${reason}` : "heat.control.normal") as TranslationKey);
  }
  const reason = control.reason ?? "";
  if (!CONTROL_WAITS.includes(reason)) {
    return translate(language, "heat.control.waiting");
  }
  // A pause lasts to the next day: the line says "tomorrow".
  const scope = PATH_WAITS.includes(reason) ? own : "heat.control";
  return translate(language, `${scope}.wait.${reason}` as TranslationKey, { name: control.automation?.name ?? "" });
}

/** A kW figure as a settings row says it, or "not given". */
function kwOrNone(language: Language, kw: number | null): string {
  return kw === null ? translate(language, "heat.settings.kwNone") : kwText(language, kw);
}

/**
 * The pool's, the spa's and the sauna's rows in the "Heating" section, and the heat card's phones: a detected pool
 * not set up yet is one row that asks once; a set-up one has its heat pump, circulation, water sensor, filtering hours
 * and powers. The spa's and the sauna's entity, the spa's lowest temperature, the heaters' power, a relay sauna's
 * safety declaration; the phones and their events while any of them is set up. Each row says what SpotNav would do
 * under it where it has something to say (SpotNav only watches them).
 */
function loadSettingRows(
  doc: Document,
  language: Language,
  answer: HeatAnswer,
  names: Map<string, string>,
  none: string,
  row: (key: HeatSettingKey, label: string, value: string, help?: string, editable?: boolean) => void,
  section: HTMLElement,
): void {
  const config = answer.config;
  const choices = answer.choices;
  const t = (key: TranslationKey, params?: Record<string, string>): string => translate(language, key, params);
  const pool = config.pool;
  const poolChoices = choices?.pool ?? null;
  const helpLine = (key: string, text: string | null): void => {
    if (text !== null) {
      const help = element(doc, "p", C.settingRowHelp, text);
      help.dataset["help"] = key;
      section.append(help);
    }
  };
  if (pool === null && poolChoices?.suggested != null) {
    // Detected, not set up: one row that asks once ("Är detta poolens värmepump?").
    row("poolSuggested", t("heat.settings.pool"), t("heat.settings.poolFound", { name: nameOf(names, poolChoices.suggested.heat_pump.entity_id, none) }));
  } else if (pool !== null || (poolChoices?.heat_pump.length ?? 0) > 0) {
    row("poolHeatPump", t("heat.settings.poolHeatPump"), nameOf(names, pool?.heat_pump?.entity_id ?? null, none));
    if (pool !== null) {
      row("poolCirculation", t("heat.settings.poolCirculation"), nameOf(names, pool.circulation_id, none));
      row("poolSensor", t("heat.settings.poolSensor"), pool.water_sensor === null ? t("heat.settings.poolSensorPump") : nameOf(names, pool.water_sensor, none));
      row(
        "poolFilterHours",
        t("heat.settings.poolFilterHours"),
        pool.filter_hours === null ? t("heat.settings.poolFilterAuto") : hoursWord(language, pool.filter_hours),
      );
      if (pool.heat_pump !== null) {
        row("poolHeatPumpKw", t("heat.settings.poolHeatPumpKw"), kwOrNone(language, pool.heat_pump_kw));
      }
      if (pool.circulation_id !== null) {
        row("poolCirculationKw", t("heat.settings.poolCirculationKw"), kwOrNone(language, pool.circulation_kw));
      }
      helpLine("pool", answer.pool === null ? null : poolWouldLine(language, answer, answer.pool) ?? poolStatusLine(language, answer.pool));
    }
  }
  const spa = config.spa;
  if (spa !== null || (choices?.spa.length ?? 0) > 0) {
    row("spa", t("heat.settings.spa"), nameOf(names, spa?.entity_id ?? null, none));
    if (spa !== null) {
      row("spaFloor", t("heat.settings.spaFloor"), degrees(language, spa.floor_c));
      row("spaKw", t("heat.settings.spaKw"), kwOrNone(language, spa.heater_kw));
      helpLine("spa", answer.spa === null ? null : deadlineWouldLine(language, answer, "spa", answer.spa));
    }
  }
  const sauna = config.sauna;
  if (sauna !== null || (choices?.sauna.length ?? 0) > 0) {
    row("sauna", t("heat.settings.sauna"), nameOf(names, sauna?.entity_id ?? null, none));
    if (sauna !== null) {
      row("saunaKw", t("heat.settings.saunaKw"), kwOrNone(language, sauna.heater_kw));
      if (sauna.family === "relay") {
        row("saunaSafety", t("heat.settings.saunaSafety"), t(sauna.safety_declared ? "heat.settings.saunaSafetyYes" : "heat.settings.saunaSafetyNo"));
      }
      helpLine("sauna", answer.sauna === null ? null : deadlineWouldLine(language, answer, "sauna", answer.sauna));
    }
  }
  if (pool !== null || spa !== null || sauna !== null) {
    const phones = config.heat_notifications.targets;
    const phoneNames = new Map((choices?.phones ?? []).map((phone) => [phone.service, phone.name]));
    row(
      "heatPhones",
      t("heat.settings.phones"),
      phones.length === 0 ? t("heat.settings.phonesNone") : phones.map((service) => phoneNames.get(service) ?? service).join(", "),
    );
    row(
      "heatEvents",
      t("heat.settings.events"),
      HEAT_EVENTS.filter((event) => config.heat_notifications.events.includes(event)).map((event) => t(`heat.settings.event.${event}` as TranslationKey)).join(", ") || t("heat.settings.eventsNone"),
    );
  }
}

/** The events the heat card's phones can hear (`heat_notifications.events`). */
export const HEAT_EVENTS = ["plan_at_risk", "pool_season"] as const;

export type HeatSettingKey =
  | "indoor"
  | "outdoor"
  | "meter"
  | "path"
  | "control"
  | "controlB"
  | "hotWater"
  | "zones"
  | `zone:${string}`
  | `sensor:${string}`
  | "steer"
  | "offsetHome"
  | "night"
  | "awayDelta"
  | "waterHeater"
  | "waterHeaterControl"
  | "tankSensor"
  | "poolSuggested"
  | "poolHeatPump"
  | "poolCirculation"
  | "poolSensor"
  | "poolFilterHours"
  | "poolHeatPumpKw"
  | "poolCirculationKw"
  | "spa"
  | "spaFloor"
  | "spaKw"
  | "sauna"
  | "saunaKw"
  | "saunaSafety"
  | "heatPhones"
  | "heatEvents";

/** The "Heating" section of the settings dialog: one row per setting, each opening its editor (`onEdit`). */
export function heatSettingsSection(doc: Document, language: Language, answer: HeatAnswer, onEdit: (key: HeatSettingKey) => void): HTMLElement {
  const section = element(doc, "section", `${C.settingsSection} ${X.section}`);
  section.append(sectionHeading(doc, "heat", translate(language, "heat.settings.title")));
  const config = answer.config;
  if (!config.control_enabled) {
    section.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, "heat.settings.intro")));
  }
  const names = namesOf(answer);
  const none = translate(language, "heat.settings.none");
  const row = (key: HeatSettingKey, label: string, value: string, help?: string, editable = true): void => {
    section.append(
      ...settingRow(doc, {
        key,
        label,
        value,
        ...(help === undefined ? {} : { help }),
        ...(editable ? { onTap: () => onEdit(key), changeableText: `${label}, ${value}` } : {}),
      }),
    );
  };
  // "Styr värmen": on the paths SpotNav can steer, with one line on what it does or waits for.
  const steers = heatSteers(config);
  if (steers) {
    row(
      "steer",
      translate(language, "heat.settings.steer"),
      translate(language, config.control_enabled ? "settings.value.on" : "settings.value.off"),
    );
    const line = controlLine(language, answer);
    if (line !== null) {
      const help = element(doc, "p", C.settingRowHelp, line);
      help.dataset["help"] = "steer";
      help.dataset["controlState"] = answer.control?.state ?? "off";
      section.append(help);
    }
    if (answer.control?.untested === true) {
      // A path that has not run on a real heat pump yet says so, on or off.
      const untested = element(doc, "p", C.settingRowHelp, translate(language, "heat.settings.untested"));
      untested.dataset["help"] = "untested";
      section.append(untested);
    }
    const home = answer.control?.offset_home ?? null;
    if (home !== null) {
      // The stored offset was not put back (a lost store, a change while Home Assistant was down): one row that
      // asks, gone once the offset reads the household's own value again.
      row(
        "offsetHome",
        translate(language, "heat.settings.offsetHome"),
        translate(language, "heat.settings.offsetHomeValue", { value: offsetValue(language, home.value), home: offsetValue(language, home.home) }),
      );
    }
  }
  const sensors = config.indoor_sensors;
  const indoorValue = sensors.length === 0
    ? none
    : sensors.length <= 2
      ? sensors.map((id) => nameOf(names, id, id)).join(", ")
      : translate(language, "heat.settings.indoorCount", { count: String(sensors.length) });
  // Each row's help is in its own editor, so the section is a short list; only a row that cannot be opened keeps it.
  row("indoor", translate(language, "heat.settings.indoor"), indoorValue);
  // Each chosen sensor's room aim against the house, as the zones' offsets: a cooler upstairs, a sensor that reads warm.
  // The offsets, night and away only act through SpotNav's steering: without a path it can steer they are left out.
  for (const sensor of steers ? sensors : []) {
    const offset = config.indoor_offsets.find((item) => item.entity_id === sensor)?.offset_c ?? 0;
    row(`sensor:${sensor}`, roomName(nameOf(names, sensor, sensor)), signedDegrees(language, offset));
  }
  row(
    "outdoor",
    translate(language, "heat.settings.outdoor"),
    config.outdoor_sensor === null ? translate(language, "heat.settings.outdoorAuto") : nameOf(names, config.outdoor_sensor, none),
  );
  const meter = answer.meter;
  const meterEditable = meter?.setting_offered === true && answer.choices?.meter !== null && answer.choices !== null;
  row(
    "meter",
    translate(language, "heat.settings.meter"),
    meter?.entity_id ? nameOf(names, meter.entity_id, none) : translate(language, "heat.settings.meterNone"),
    meterEditable ? undefined : translate(language, "heat.settings.meterHelp"),
    meterEditable,
  );
  row("path", translate(language, "heat.settings.path"), translate(language, `heat.path.${config.control_path}` as TranslationKey));
  if (config.control_path !== "none") {
    row("control", translate(language, "heat.settings.control"), nameOf(names, config.control_entity, none));
  }
  if (config.control_path === "sg_ready") {
    row("controlB", translate(language, "heat.settings.controlB"), nameOf(names, config.control_entity_b, none));
  } else if (config.control_path === "sg_ready_api") {
    row("controlB", translate(language, "heat.settings.controlApiSwitch"), nameOf(names, config.control_entity_b, none));
  }
  row("hotWater", translate(language, "heat.settings.hotWater"), nameOf(names, config.hot_water_entity, none));
  row(
    "zones",
    translate(language, "heat.settings.zones"),
    config.zones.length === 0 ? translate(language, "heat.settings.zonesNone") : String(config.zones.length),
  );
  // Each zone: watched, or set by SpotNav from the person's own setting (its offset no longer acts, so it is not shown).
  for (const zone of config.zones) {
    const shown = answer.zones.find((item) => item.entity_id === zone.entity_id);
    row(
      `zone:${zone.entity_id}`,
      nameOf(names, zone.entity_id, zone.entity_id),
      translate(language, zone.control ? "heat.settings.zoneControlled" : "heat.settings.zoneWatched"),
    );
    const line = shown === undefined ? null : zoneLine(language, answer, shown);
    if (line !== null) {
      const help = element(doc, "p", C.settingRowHelp, line);
      help.dataset["help"] = `zone:${zone.entity_id}`;
      help.dataset["controlState"] = shown?.control?.state ?? "off";
      section.append(help);
    }
  }
  // The plain water heater: its switch, then the opt-in and the optional tank sensor.
  const heater = config.water_heater;
  row("waterHeater", translate(language, "heat.settings.waterHeater"), nameOf(names, heater?.entity_id ?? null, none));
  if (heater !== null) {
    row(
      "waterHeaterControl",
      translate(language, "heat.settings.waterHeaterControl"),
      translate(language, heater.control ? "settings.value.on" : "settings.value.off"),
    );
    const line = waterHeaterLine(language, answer);
    if (line !== null) {
      const help = element(doc, "p", C.settingRowHelp, line);
      help.dataset["help"] = "waterHeater";
      help.dataset["controlState"] = answer.water_heater?.state ?? "off";
      section.append(help);
    }
    row("tankSensor", translate(language, "heat.settings.tankSensor"), nameOf(names, heater.tank_sensor, none));
  }
  loadSettingRows(doc, language, answer, names, none, row, section);
  if (!steers) {
    return section;
  }
  const night = config.night;
  row(
    "night",
    translate(language, "heat.settings.night"),
    night.on
      ? translate(language, "heat.settings.nightValue", { delta: degrees(language, night.delta_c), start: night.start, end: night.end })
      : translate(language, "settings.value.off"),
  );
  row("awayDelta", translate(language, "heat.settings.away"), translate(language, "heat.settings.awayValue", { delta: degrees(language, config.away_delta_c) }));
  return section;
}

export { signedDegrees };
