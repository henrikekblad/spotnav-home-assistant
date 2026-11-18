// The heat card's body: a header (the live status after the name, the details' "i" and, for an administrator, the
// settings button) and three tabs (`heat-tabs.ts`). Now: the thermostat dial with the comfort band and the measured
// room, home or away, what SpotNav would do and the relay, a thin strip of the plan's next twelve hours, today's heat
// and hot water, and the notes the household can act on. Plan: the day's chart (`heat-day-chart.ts`), today's or a
// past day's. Use: the period's energy or cost by what is measured (`heat-use.ts`). Below, the "Heating" settings
// section (`heatSettingsSection`), the rows the settings dialog shows. Built fresh for each state with `textContent`
// only; every figure is the backend's.

import { cardHeader, headerButton, headerStatus, headerTitle } from "./card-header";
import { clock, dateLabel, formatFixed, type FormatContext } from "./format";
import { roomName, type HeatAheadHour, type HeatAnswer, type HeatBandHour, type HeatChoice } from "./heat-model";
import { dayChart, hoursOf } from "./heat-day-chart";
import type { HeatHistory } from "./heat-history";
import { HEAT_CLASSES as X } from "./heat-styles";
import { heatTabList, heatTabPanel, type HeatTab, type HeatTabFit, type UseUnit } from "./heat-tabs";
import { usePanel } from "./heat-use";
import { HOUSE_CLASSES as H } from "./house-styles";
import { periodPicker, type PeriodChoice } from "./house-tabs";
import { translate, type Language, type TranslationKey } from "./i18n";
import { sectionHeading, settingRow } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

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
}

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
  const pad = (n: number): string => String(n).padStart(2, "0");
  return {
    date: `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`,
    time: `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`,
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

function kw(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} kW`;
}

function kwh(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} kWh`;
}

function context(language: Language, answer: HeatAnswer): FormatContext {
  return { language, timeZone: answer.time_zone, unit: "kWh", currency: answer.today?.money?.currency ?? null, majorUnit: answer.today?.money?.major ?? null };
}

/** Money given in the minor unit, shown in the major one: whole units from 10 up ("19 kr"), else two decimals. */
export function amount(language: Language, minor: number, money: { major: string; minor: string } | null): string {
  if (money === null) {
    return formatFixed(language, minor, 0);
  }
  const major = minor / 100;
  return `${formatFixed(language, major, Math.abs(major) >= 10 ? 0 : 2)} ${money.major}`;
}

/** A local moment as the card says it: the time today, else the day and the time ("fre 20 nov. 16:00"). */
export function whenLabel(language: Language, answer: HeatAnswer, instantMs: number): string {
  const time = clock(context(language, answer), instantMs);
  const day = instantToLocal(instantMs, answer.time_zone).date;
  return day === instantToLocal(Date.parse(answer.now), answer.time_zone).date ? time : `${dateLabel(language, day)} ${time}`;
}

/** The entity names the answer knows (choices, sensors, zones, meter), for the settings rows. */
export function namesOf(answer: HeatAnswer): Map<string, string> {
  const names = new Map<string, string>();
  for (const group of Object.values(answer.choices ?? {})) {
    for (const choice of (group as HeatChoice[] | null) ?? []) {
      names.set(choice.entity_id, choice.name);
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
  return { code: state, text: state === "idle" || power === null ? words : `${words} ${kw(language, power).replace(" ", "\u00a0")}` };
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

function share(value: number): number {
  return Math.min(1, Math.max(0, (value - TARGET_MIN) / (TARGET_MAX - TARGET_MIN)));
}

function arc(from: number, to: number): string {
  const a = DIAL.start + DIAL.sweep * from;
  const b = DIAL.start + DIAL.sweep * to;
  const [x0, y0] = point(a);
  const [x1, y1] = point(b);
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${DIAL.r} ${DIAL.r} 0 ${b - a > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

function dial(doc: Document, language: Language, answer: HeatAnswer, target: number, ui: HeatUi, handlers: HeatHandlers, height: number | null = null): HTMLElement {
  const wrap = element(doc, "div", X.dialWrap);
  const step = (delta: number, label: TranslationKey, text: string, disabled: boolean): HTMLButtonElement => {
    const button = element(doc, "button", X.dialStep, text);
    button.type = "button";
    button.dataset["step"] = delta > 0 ? "up" : "down";
    button.setAttribute("aria-label", translate(language, label));
    button.disabled = disabled;
    button.addEventListener("click", () => handlers.onStep(delta));
    return button;
  };
  const picture = svg(doc, "svg", { viewBox: "0 0 200 170", class: X.dial, role: "img" });
  if (height !== null) {
    // In a box of fixed height the dial takes the height it is given, as wide as that makes it.
    picture.style.height = `${Math.round(height)}px`;
    picture.style.maxWidth = `${Math.round((height * 200) / 170)}px`;
  }
  const low = target - 0.5;
  const high = target + 0.5;
  const measured = answer.indoor.value_c;
  const label = [translate(language, "heat.dial.label"), degrees(language, target)];
  if (measured !== null) {
    label.push(degrees(language, measured));
  }
  picture.setAttribute("aria-label", label.join(", "));
  picture.append(
    svg(doc, "path", { d: arc(0, 1), class: X.dialTrack }),
    svg(doc, "path", { d: arc(share(low), share(high)), class: X.dialBand }),
    svg(doc, "path", { d: arc(0, Math.max(0.002, share(target))), class: X.dialValue }),
  );
  if (measured !== null) {
    const [x, y] = point(DIAL.start + DIAL.sweep * share(measured));
    picture.append(svg(doc, "circle", { cx: x.toFixed(2), cy: y.toFixed(2), r: 6, class: X.dialMeasured, "data-measured": "true" }));
  }
  const figure = svg(doc, "text", { x: DIAL.cx, y: DIAL.cy + 8, class: X.dialFigure });
  figure.textContent = `${formatFixed(language, target, 1)}°`;
  const sub = svg(doc, "text", { x: DIAL.cx, y: DIAL.cy + 32, class: X.dialSub });
  // The big figure is the target; the line under it says what it keeps, so the same number is not said twice.
  sub.textContent = translate(language, "heat.dial.band", {
    low: formatFixed(language, low, 1),
    high: formatFixed(language, high, 1),
  });
  sub.dataset["line"] = "band";
  picture.append(figure, sub);
  wrap.append(
    step(-TARGET_STEP, "heat.dial.lower", "−", target - TARGET_STEP < TARGET_MIN - 1e-9),
    picture,
    step(TARGET_STEP, "heat.dial.raise", "+", target + TARGET_STEP > TARGET_MAX + 1e-9),
  );
  const block = element(doc, "div");
  block.append(wrap);
  if (ui.error !== null) {
    const error = element(doc, "p", X.error, ui.error);
    error.setAttribute("role", "alert");
    block.append(error);
  }
  return block;
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
  return translate(language, key, { kw: power === null ? "–" : kw(language, power) }).replace(/ · –$/, "");
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
  // On SG Ready with both contacts, the pre-heat is a raise of the pump through its second contact.
  const raisable = answer.config.control_path === "sg_ready" && answer.config.control_entity !== null && answer.config.control_entity_b !== null;
  const key = would.action === "lower" ? lowerKey : would.action === "preheat" && raisable ? "heat.would.raise" : `heat.would.${would.action}`;
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

// ------------------------------------------------------------------------------------------------ the hours ahead

/** How many hours the strip under the dial spans. */
export const AHEAD_HOURS = 12;
const AHEAD_ACTIONS = ["lower", "preheat", "night", "away"] as const;

/** Whether a pre-heat is a raise of the pump through SG Ready's second contact. */
function raisable(answer: HeatAnswer): boolean {
  return answer.config.control_path === "sg_ready" && answer.config.control_entity !== null && answer.config.control_entity_b !== null;
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
  const water = hours.find((hour) => hour.hot_water);
  if (water !== undefined) {
    parts.push({ at: water.startMs, text: translate(language, "heat.ahead.water", { time: clock(ctx, water.startMs) }) });
  }
  if (parts.length === 0) {
    return translate(language, "heat.ahead.none");
  }
  return translate(language, "heat.ahead.lead", { parts: parts.sort((a, b) => a.at - b.at).map((part) => part.text).join(" · ") });
}

/** The thin strip under the dial: the plan's next twelve hours, a cell an hour (a lowering, a pre-heat, the cooler
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

// ------------------------------------------------------------------------------------------------ the day

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
    water: hours.filter((hour) => hour.hot_water).map((hour) => ({ startMs: hour.startMs, endMs: hour.startMs + HOUR_MS })),
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
  const parts = [translate(language, "heat.today.label"), translate(language, "heat.today.heat", { kwh: kwh(language, today.heat_kwh) })];
  if (today.zones_kwh !== null && today.zones_kwh > 0) {
    parts.push(translate(language, "heat.today.zones", { kwh: kwh(language, today.zones_kwh) }));
  }
  if (today.hot_water_kwh !== null) {
    parts.push(translate(language, "heat.today.water", { kwh: kwh(language, today.hot_water_kwh) }));
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
  panel.append(dial(doc, language, answer, target, input.ui, input.handlers, input.ui.fill ?? null));
  const measured = element(doc, "p", X.measured, measuredLine(language, answer));
  measured.dataset["line"] = "measured";
  panel.append(measured, ...modes(doc, language, answer, input.handlers));
  if (answer.shadow && !boxed(input)) {
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
  if (lines.childElementCount > 0) {
    panel.append(lines);
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
  if (boxed(input)) {
    return;
  }
  for (const review of reviewLines(language, answer)) {
    const line = element(doc, "p", `${H.line} ${C.muted}`, review.text);
    line.dataset["line"] = review.key;
    panel.append(line);
  }
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
  const locked = input.ui.locked ?? null;
  const tab: HeatTab = locked ?? input.ui.tab ?? "now";
  card.dataset["tab"] = tab;
  if (locked === null) {
    card.append(heatTabList(doc, language, tab, idPrefix, (next, byKey) => input.handlers.onTab?.(next, byKey), input.ui.tabFit ?? "long"));
  }
  const panel = heatTabPanel(doc, tab, idPrefix, locked === null);
  card.append(panel);
  const today = input.tabs?.today ?? instantToLocal(input.nowMs, answer.time_zone).date;
  if (tab === "now") {
    nowPanel(input, answer, panel);
  } else if (tab === "plan") {
    planPanel(input, answer, panel, today);
  } else {
    useTab(input, panel, today);
  }
  return card;
}

// ------------------------------------------------------------------------------------------------ settings

const CONTROL_WAITS = [
  "automation", "other_controller", "pump_price_control", "relay_unavailable", "relay_closed", "no_relay", "no_signal",
  "check_failed", "foreign_change", "no_read_back",
];
const CONTROL_SAFETY = ["too_cold", "room_cold", "stale", "sg_ready_b"];

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
  if (control.state === "verifying") {
    return translate(language, control.reason === "wait" ? "heat.control.verifyingWait" : "heat.control.verifying");
  }
  if (control.state === "running") {
    if (control.raised) {
      return control.raise_until === null
        ? translate(language, "heat.control.raisedNow")
        : translate(language, "heat.control.raised", { until: whenLabel(language, answer, Date.parse(control.raise_until)) });
    }
    if (control.lowered) {
      const warm = answer.status.would?.reason === "room_warm";
      return control.until === null
        ? translate(language, "heat.control.loweredNow")
        : translate(language, warm ? "heat.control.loweredWarm" : "heat.control.lowered", { until: whenLabel(language, answer, Date.parse(control.until)) });
    }
    const reason = control.reason ?? "";
    return translate(language, (CONTROL_SAFETY.includes(reason) ? `heat.control.${reason}` : "heat.control.normal") as TranslationKey);
  }
  const reason = control.reason ?? "";
  if (!CONTROL_WAITS.includes(reason)) {
    return translate(language, "heat.control.waiting");
  }
  // A pause lasts to the next day: the line says "tomorrow".
  return translate(language, `heat.control.wait.${reason}` as TranslationKey, { name: control.automation?.name ?? "" });
}

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
  | "night";

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
  // "Styr värmen": on the relay path (and SG Ready's) only, with one line on what it does or waits for.
  if (["relay", "sg_ready"].includes(config.control_path) || config.control_enabled) {
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
  for (const sensor of sensors) {
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
  }
  row("hotWater", translate(language, "heat.settings.hotWater"), nameOf(names, config.hot_water_entity, none));
  row(
    "zones",
    translate(language, "heat.settings.zones"),
    config.zones.length === 0 ? translate(language, "heat.settings.zonesNone") : String(config.zones.length),
  );
  for (const zone of config.zones) {
    const shown = answer.zones.find((item) => item.entity_id === zone.entity_id);
    const kind = shown?.kind ?? answer.choices?.zones.find((item) => item.entity_id === zone.entity_id)?.kind ?? "floor";
    // An air-to-air heat pump says so under its row; a floor thermostat's own name says what it is.
    row(
      `zone:${zone.entity_id}`,
      nameOf(names, zone.entity_id, zone.entity_id),
      signedDegrees(language, zone.offset_c),
      kind === "air" ? translate(language, "heat.settings.airHelp") : undefined,
    );
  }
  const night = config.night;
  row(
    "night",
    translate(language, "heat.settings.night"),
    night.on
      ? translate(language, "heat.settings.nightValue", { delta: degrees(language, night.delta_c), start: night.start, end: night.end })
      : translate(language, "settings.value.off"),
  );
  return section;
}

export { signedDegrees };
