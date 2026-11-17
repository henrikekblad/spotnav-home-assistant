// The house card's Now tab (SpotNav 3.0): the live flows as a schematic and one sentence of what the battery does and
// what its plan does next.
//
// * The schematic: the house in the middle, and around it only what the site has: the sun (top left), the grid (top
//   right), the battery (bottom left), the car (bottom right) and the heat (below the house, part of it). A line
//   runs from each to the house, with dots running the way the power flows and the live kW on it; a small arrow on
//   the line says the way too, so the direction is never colour or motion alone. Under each node, its day so far:
//   the energy flow's own figures (`site/energy_flow.py`), the battery's level and stored energy.
// * The dots' colour says what flows: the sun's amber, the battery's teal, the car's and the heat's own, an import in
//   red and an export in green. They stand still under `prefers-reduced-motion`.
// * The live figures are `spotnav/get_house_now`'s (`site/house_now.py`), read every 30 s while the tab is shown. From
//   a backend before it, or while it cannot be read, the schematic has the day's figures and the battery's own
//   power from `spotnav/get_house`, and no other live kW.
// * The sentence: from the battery plan's hours (`HouseHour.battery_action`), what the battery does now and until
//   when, then what comes next; while SpotNav does not control the battery, who does and what SpotNav would do.

import { clock, dayCue, formatFixed, formatNumber, type FormatContext } from "./format";
import { HOUSE_NOW_API_VERSION, type EnergyFlow, type HouseAnswer } from "./house-model";
import { translate, type Language, type TranslationKey } from "./i18n";

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
  soc_pct: number | null;
  car_kw: number | null;
  heat_kw: number | null;
  heat_from: string | null;
  house_kw: number | null;
  has: { sun: boolean; battery: boolean; car: boolean; heat: boolean };
}

export type HouseNowDecode = { ok: true; value: HouseNow } | { ok: false; failure: "unsupported" | "malformed" };

class Malformed extends Error {}

function bad(): never {
  throw new Malformed();
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : bad();
}

function text(value: unknown): string {
  return typeof value === "string" ? value : bad();
}

function numberOrNull(value: unknown): number | null {
  return value === null ? null : typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function flag(value: unknown): boolean {
  return typeof value === "boolean" ? value : bad();
}

function textOrNull(value: unknown): string | null {
  return value === null ? null : text(value);
}

/** The answer, strictly typed; extra keys are allowed (additive fields), a missing or mistyped one is not. */
export function decodeHouseNow(raw: unknown): HouseNowDecode {
  try {
    const source = record(raw);
    if (source.api_version !== HOUSE_NOW_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    const has = record(source.has);
    return {
      ok: true,
      value: {
        site_id: text(source.site_id),
        at: text(source.at),
        grid_kw: numberOrNull(source.grid_kw),
        sun_kw: numberOrNull(source.sun_kw),
        sun_from: textOrNull(source.sun_from),
        battery_kw: numberOrNull(source.battery_kw),
        soc_pct: numberOrNull(source.soc_pct),
        car_kw: numberOrNull(source.car_kw),
        heat_kw: numberOrNull(source.heat_kw),
        heat_from: textOrNull(source.heat_from),
        house_kw: numberOrNull(source.house_kw),
        has: { sun: flag(has.sun), battery: flag(has.battery), car: flag(has.car), heat: flag(has.heat) },
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
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
      runs.push({ act, startMs: Math.max(hour.startMs, runs.length === 0 ? hour.startMs : hour.startMs) });
    }
  }
  return runs;
}

const OPTIMISERS: Record<string, string> = { sigenergy: "Sigenergy AI" };

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
      ? translate(language, "house.plan.who.own", { name: OPTIMISERS[battery.brand ?? ""] ?? translate(language, "house.compare.own") })
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
  const values = { act: first, time: when(ctx, nowMs, next.startMs), then: translate(language, `house.plan.${verb}.${next.act}` as TranslationKey) };
  return controlling ? translate(language, "house.plan.next", values) : translate(language, "house.plan.wouldNext", { who, ...values });
}

// ------------------------------------------------------------------------------------------------ the schematic

/** The dots' lap (s): faster with more power, from 3.3 s near nothing to 0.8 s from 12 kW (the owner's hall card's
 * own rule, after the battery app's). */
export function dotLap(kw: number): number {
  return (1 - Math.min(Math.abs(kw), 12) / 12) * 2.5 + 0.8;
}

export type NodeRole = "sun" | "grid" | "battery" | "car" | "heat";

export interface SchematicLine {
  role: NodeRole;
  /** The power on the line (kW, never negative), `null` unread. */
  kw: number | null;
  /** Towards the house (`in`), away from it (`out`), or still. */
  way: "in" | "out" | "still";
  /** What flows, for the dots' colour: the role's own, an import or an export. */
  tone: NodeRole | "import" | "export";
}

/** Each line of the schematic from the live figures (`null`: the day's figures only, the battery's own power). */
export function schematicLines(live: HouseNow | null, answer: HouseAnswer): SchematicLine[] {
  const has = present(live, answer);
  const lines: SchematicLine[] = [];
  const line = (role: NodeRole, signed: number | null, inPositive: boolean): SchematicLine => {
    if (signed === null) {
      return { role, kw: null, way: "still", tone: role };
    }
    const kw = Math.abs(signed);
    const way = kw < STILL_KW ? "still" : (signed > 0) === inPositive ? "in" : "out";
    const tone: SchematicLine["tone"] = role === "grid" ? (way === "out" ? "export" : "import") : role;
    return { role, kw, way, tone };
  };
  if (has.sun) {
    lines.push(line("sun", live?.sun_kw ?? null, true));
  }
  lines.push(line("grid", live?.grid_kw ?? null, true));
  if (has.battery) {
    // + charging: away from the house.
    lines.push(line("battery", live?.battery_kw ?? answer.battery?.now?.power_kw ?? null, false));
  }
  if (has.car) {
    lines.push(line("car", live?.car_kw ?? null, false));
  }
  if (has.heat) {
    lines.push(line("heat", live?.heat_kw ?? null, false));
  }
  return lines;
}

/** Which nodes the site has: the live answer's word, else what the day's energy flow and the answer show. */
export function present(live: HouseNow | null, answer: HouseAnswer): { sun: boolean; battery: boolean; car: boolean; heat: boolean } {
  if (live !== null) {
    return live.has;
  }
  const roles = new Set((answer.energy_flow?.nodes ?? []).map((node) => node.role));
  return {
    sun: answer.sun !== null || roles.has("solar"),
    battery: answer.battery !== null,
    car: roles.has("car"),
    heat: answer.heat?.status === "found" || roles.has("heat"),
  };
}

function flowKwh(flow: EnergyFlow | null, side: "source" | "use", role: string): number | null {
  const node = flow?.nodes.find((item) => item.side === side && item.role === role);
  return node === undefined ? null : node.kwh;
}

function kwText(language: Language, kw: number): string {
  return `${formatFixed(language, kw, kw >= 10 ? 0 : 1)} kW`;
}

function kwhText(language: Language, kwh: number): string {
  return `${formatFixed(language, kwh, kwh >= 100 ? 0 : 1)} kWh`;
}

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, name: K, attributes: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const created = doc.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    created.setAttribute(key, String(value));
  }
  return created;
}

/** The icons, drawn around (0, 0) in a 24-unit box, stroked in the node's colour. */
const ICONS: Record<NodeRole | "house", string[]> = {
  sun: ["M0 -4.5a4.5 4.5 0 1 0 0.01 0", "M0 -10V-7.5M0 7.5V10M-10 0H-7.5M7.5 0H10M-7 -7l1.8 1.8M5.2 5.2L7 7M-7 7l1.8-1.8M5.2 -5.2L7 -7"],
  grid: ["M-6 10L0 -10L6 10", "M-8 -4H8M-6.5 2.5H6.5", "M-4.5 5L4.5 10M4.5 5L-4.5 10"],
  battery: ["M-5.5 -8h11v17h-11z", "M-2.5 -10.5h5", "M-2 1l2.5 -4v3h2l-2.5 4v-3h-2"],
  car: ["M-10 4v-3.5l2.5-5h15l2.5 5V4z", "M-6.5 6.5a1.6 1.6 0 1 0 0.01 0M6.5 6.5a1.6 1.6 0 1 0 0.01 0", "M-6 -1h12"],
  heat: ["M0 -10c5 5 5.5 9 3 13a4 4 0 0 1-6 0c-1.5-2.5-1-5 1-7c0 2 1 3 2 3c0-3-1-6 0-9z"],
  house: ["M-12 0L0 -10.5L12 0", "M-8.5 -3V10H8.5V-3", "M-2.5 10V3.5h5V10"],
};

/** The schematic's height for its width (a fixed-height box may ask another). */
export function schematicHeight(width: number): number {
  return Math.round(Math.min(320, Math.max(270, width * 0.42)));
}

interface Spot {
  x: number;
  y: number;
}

/** Where each node's disc sits for a width and height: the house in the middle, the others in a band between the
 * words of the top row (sun, house, grid) and of the bottom row (battery, heat, car). */
export function nodeSpots(width: number, height: number): Record<NodeRole | "house", Spot> {
  const side = Math.max(48, Math.min(width * 0.17, 150));
  const top = 38 + (height - 76) * 0.2;
  const bottom = height - 38 - (height - 76) * 0.2;
  return {
    house: { x: width / 2, y: height / 2 },
    sun: { x: side, y: top },
    grid: { x: width - side, y: top },
    battery: { x: side, y: bottom },
    car: { x: width - side, y: bottom },
    heat: { x: width / 2, y: bottom + 12 },
  };
}

const NODE_R = 16;
const HOUSE_R = 30;

export interface SchematicInput {
  doc: Document;
  language: Language;
  answer: HouseAnswer;
  live: HouseNow | null;
  width: number;
  height?: number | null;
  idPrefix: string;
}

/** A corner's words: where they stand and which way they read from there. */
type Corner = "top-left" | "top" | "top-right" | "bottom-left" | "bottom" | "bottom-right";

const CORNER_OF: Record<NodeRole | "house", Corner> = {
  sun: "top-left",
  house: "top",
  grid: "top-right",
  battery: "bottom-left",
  heat: "bottom",
  car: "bottom-right",
};

/** The schematic (see the module): an SVG drawn at the card's width, its words in the reader's language. */
export function renderSchematic(input: SchematicInput): SVGSVGElement {
  const { doc, language, answer, live } = input;
  const width = Math.max(260, Math.round(input.width));
  const height = Math.max(220, Math.round(input.height ?? schematicHeight(width)));
  const spots = nodeSpots(width, height);
  const lines = schematicLines(live, answer);
  const flow = answer.energy_flow;
  const root = svg(doc, "svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "spotnav-now-schematic", role: "img" });
  root.style.width = "100%";
  root.style.height = "auto";
  const title = svg(doc, "title", { id: `${input.idPrefix}-now-title` });
  root.append(title);
  const said: string[] = [];
  const edge = (from: Spot, to: Spot, fromR: number, toR: number): [Spot, Spot] => {
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const length = Math.hypot(dx, dy) || 1;
    return [
      { x: from.x + (dx / length) * fromR, y: from.y + (dy / length) * fromR },
      { x: to.x - (dx / length) * toR, y: to.y - (dy / length) * toR },
    ];
  };
  const wires = svg(doc, "g", { class: "spotnav-now-wires" });
  const pills = svg(doc, "g", { class: "spotnav-now-pills" });
  for (const line of lines) {
    const [a, b] = edge(spots[line.role], spots.house, NODE_R + 3, HOUSE_R + 3);
    // Drawn the way the power flows, so the dashes run with it.
    const [from, to] = line.way === "out" ? [b, a] : [a, b];
    const d = `M${from.x.toFixed(1)} ${from.y.toFixed(1)}L${to.x.toFixed(1)} ${to.y.toFixed(1)}`;
    const wire = svg(doc, "path", { d, class: "spotnav-now-wire" });
    wire.dataset["role"] = line.role;
    wires.append(wire);
    if (line.way === "still" || line.kw === null) {
      continue;
    }
    const dots = svg(doc, "path", { d, class: "spotnav-now-dots" });
    dots.dataset["tone"] = line.tone;
    dots.dataset["role"] = line.role;
    dots.style.animationDuration = `${dotLap(line.kw).toFixed(2)}s`;
    wires.append(dots);
    // The arrow, near the house end: the direction without the colour or the motion.
    const angle = (Math.atan2(to.y - from.y, to.x - from.x) * 180) / Math.PI;
    const at = { x: from.x + (to.x - from.x) * 0.8, y: from.y + (to.y - from.y) * 0.8 };
    const arrow = svg(doc, "path", { d: "M-4 -4.5L2.5 0L-4 4.5", class: "spotnav-now-arrow", transform: `translate(${at.x.toFixed(1)} ${at.y.toFixed(1)}) rotate(${angle.toFixed(0)})` });
    arrow.dataset["tone"] = line.tone;
    wires.append(arrow);
    const label = kwText(language, line.kw);
    // Beside the line's middle, away from the house's centre line (above a top line, below a bottom one), on a halo
    // in the card's colour so the line stays whole; the heat's short line has it to its right.
    const mid = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };
    let normal = { x: -(to.y - from.y), y: to.x - from.x };
    const length = Math.hypot(normal.x, normal.y) || 1;
    normal = { x: normal.x / length, y: normal.y / length };
    if (line.role === "heat") {
      normal = { x: 1, y: 0 };
    } else if ((normal.y > 0) !== (mid.y > spots.house.y)) {
      normal = { x: -normal.x, y: -normal.y };
    }
    const offset = line.role === "heat" ? 10 : 12;
    const at2 = { x: mid.x + normal.x * offset, y: mid.y + normal.y * offset };
    const anchor = Math.abs(normal.x) < 0.35 ? "middle" : normal.x > 0 ? "start" : "end";
    const group = svg(doc, "g", { class: "spotnav-now-pill", transform: `translate(${at2.x.toFixed(1)} ${at2.y.toFixed(1)})` });
    group.dataset["role"] = line.role;
    group.dataset["tone"] = line.tone;
    group.append(Object.assign(svg(doc, "text", { x: 0, y: 4, "text-anchor": anchor }), { textContent: label }));
    pills.append(group);
  }
  root.append(wires);
  const disc = (role: NodeRole | "house", r: number): SVGGElement => {
    const spot = spots[role];
    const group = svg(doc, "g", { class: "spotnav-now-node", transform: `translate(${spot.x.toFixed(1)} ${spot.y.toFixed(1)})` });
    group.dataset["role"] = role;
    group.append(svg(doc, "circle", { r, class: "spotnav-now-disc" }));
    const icon = svg(doc, "g", { class: "spotnav-now-icon", transform: role === "house" ? "translate(0 -7) scale(0.95)" : "scale(0.85)" });
    for (const path of ICONS[role]) {
      icon.append(svg(doc, "path", { d: path }));
    }
    group.append(icon);
    root.append(group);
    return group;
  };
  // Each node's day, in its corner: a small name over a figure (and, for the grid, what was sold).
  const words = (role: NodeRole | "house", lines: string[]): void => {
    const corner = CORNER_OF[role];
    const anchor = corner.endsWith("left") ? "start" : corner.endsWith("right") ? "end" : "middle";
    const x = anchor === "start" ? 0 : anchor === "end" ? width : width / 2;
    const atTop = corner.startsWith("top");
    const [name, value, extra] = lines;
    const block = svg(doc, "g", { class: "spotnav-now-words" });
    block.dataset["corner"] = corner;
    block.dataset["role"] = role;
    const rows: Array<[string | undefined, string]> = atTop
      ? [[name, "spotnav-now-name"], [value, "spotnav-now-value"], [extra, "spotnav-now-extra"]]
      : [[name, "spotnav-now-name"], [value, "spotnav-now-value"], [extra, "spotnav-now-extra"]];
    const heights = [11, 18, 14];
    const total = rows.reduce((sum, [text], index) => sum + (text === undefined ? 0 : heights[index]!), 0);
    let y = atTop ? 0 : height - total;
    rows.forEach(([text, cls], index) => {
      if (text === undefined) {
        return;
      }
      y += heights[index]!;
      block.append(Object.assign(svg(doc, "text", { x, y: y - (index === 0 ? 1 : 3), "text-anchor": anchor, class: cls }), { textContent: text }));
    });
    root.append(block);
    said.push(lines.filter((item) => item !== undefined).join(" "));
  };
  const today = (key: TranslationKey, kwh: number | null): string[] => [translate(language, key), kwh === null ? "–" : kwhText(language, kwh)];
  const has = present(live, answer);
  if (has.sun) {
    disc("sun", NODE_R);
    words("sun", today("house.nowTab.sun", flowKwh(flow, "source", "solar")));
  }
  {
    disc("grid", NODE_R);
    const sold = flowKwh(flow, "use", "grid");
    const grid = today("house.nowTab.grid", flowKwh(flow, "source", "grid"));
    if (sold !== null && sold >= STILL_KW) {
      grid.push(translate(language, "house.nowTab.sold", { kwh: kwhText(language, sold) }));
    }
    words("grid", grid);
  }
  if (has.battery) {
    disc("battery", NODE_R);
    const soc = live?.soc_pct ?? answer.battery?.soc_pct ?? null;
    const usable = answer.battery?.usable_kwh ?? null;
    const level = soc === null ? "–" : `${formatNumber(language, soc, 0)} %`;
    const battery = [translate(language, "house.nowTab.battery"), level];
    if (soc !== null && usable !== null) {
      battery.push(kwhText(language, (soc / 100) * usable));
    }
    words("battery", battery);
  }
  if (has.car) {
    disc("car", NODE_R);
    words("car", today("house.nowTab.car", flowKwh(flow, "use", "car")));
  }
  if (has.heat) {
    disc("heat", NODE_R - 2);
    words("heat", today("house.nowTab.heat", flowKwh(flow, "use", "heat")));
  }
  // The house: its day at the top, its live power in its disc.
  const whole = flow === null ? null : (flowKwh(flow, "use", "house") ?? 0) + (flowKwh(flow, "use", "heat") ?? 0);
  const house = disc("house", HOUSE_R);
  words("house", today("house.nowTab.house", whole));
  const houseKw = live?.house_kw ?? null;
  if (houseKw !== null) {
    house.append(Object.assign(svg(doc, "text", { x: 0, y: 17, "text-anchor": "middle", class: "spotnav-now-house-kw" }), { textContent: kwText(language, houseKw) }));
    said.push(`${translate(language, "house.nowTab.houseNow")} ${kwText(language, houseKw)}`);
  }
  root.append(pills);
  for (const line of lines) {
    if (line.kw !== null && line.way !== "still") {
      said.push(translate(language, `house.nowTab.line.${line.role}.${line.way}` as TranslationKey, { kw: kwText(language, line.kw) }));
    }
  }
  title.textContent = said.join(". ");
  root.setAttribute("aria-labelledby", title.id);
  return root;
}

export const NOW_STYLES = `
  .spotnav-now-schematic {
    display: block;
    overflow: visible;
    max-width: 100%;
  }
  .spotnav-now-wire {
    fill: none;
    stroke: var(--divider-color, #e0e0e0);
    stroke-width: 3;
    stroke-linecap: round;
  }
  .spotnav-now-dots {
    fill: none;
    stroke-width: 4.5;
    stroke-linecap: round;
    stroke-dasharray: 0.1 9;
    animation: spotnav-now-run 1.6s linear infinite;
  }
  @keyframes spotnav-now-run {
    to { stroke-dashoffset: -9.1; }
  }
  @media (prefers-reduced-motion: reduce) {
    .spotnav-now-dots { animation: none; }
  }
  .spotnav-now-arrow {
    fill: none;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .spotnav-now-dots[data-tone="sun"], .spotnav-now-arrow[data-tone="sun"] { stroke: var(--spotnav-sun-colour); }
  .spotnav-now-dots[data-tone="battery"], .spotnav-now-arrow[data-tone="battery"] { stroke: var(--spotnav-battery-colour); }
  .spotnav-now-dots[data-tone="car"], .spotnav-now-arrow[data-tone="car"] { stroke: var(--spotnav-car-colour, #e07b2e); }
  .spotnav-now-dots[data-tone="heat"], .spotnav-now-arrow[data-tone="heat"] { stroke: var(--spotnav-heat-colour); }
  .spotnav-now-dots[data-tone="import"], .spotnav-now-arrow[data-tone="import"] { stroke: var(--spotnav-import-colour, #d64541); }
  .spotnav-now-dots[data-tone="export"], .spotnav-now-arrow[data-tone="export"] { stroke: var(--spotnav-export-colour, #2e9d57); }
  .spotnav-now-disc {
    fill: var(--card-background-color, var(--ha-card-background, #fff));
    stroke-width: 2;
  }
  .spotnav-now-icon path {
    fill: none;
    stroke-width: 1.7;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .spotnav-now-node[data-role="sun"] { color: var(--spotnav-sun-colour); }
  .spotnav-now-node[data-role="grid"] { color: var(--spotnav-grid-colour); }
  .spotnav-now-node[data-role="battery"] { color: var(--spotnav-battery-colour); }
  .spotnav-now-node[data-role="car"] { color: var(--spotnav-car-colour, #e07b2e); }
  .spotnav-now-node[data-role="heat"] { color: var(--spotnav-heat-colour); }
  .spotnav-now-node[data-role="house"] { color: var(--spotnav-house-colour); }
  .spotnav-now-disc, .spotnav-now-icon path { stroke: currentColor; }
  .spotnav-now-schematic text {
    stroke: none;
  }
  .spotnav-now-name {
    font-size: 10.5px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    fill: var(--secondary-text-color, #727272);
  }
  .spotnav-now-value {
    font-size: 16px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    fill: var(--primary-text-color, #212121);
  }
  .spotnav-now-extra {
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    fill: var(--secondary-text-color, #727272);
  }
  .spotnav-now-house-kw {
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    fill: var(--primary-text-color, #212121);
  }
  .spotnav-now-schematic .spotnav-now-pill text {
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    fill: var(--primary-text-color, #212121);
    stroke: var(--ha-card-background, var(--card-background-color, #fff));
    stroke-width: 4px;
    stroke-linejoin: round;
    paint-order: stroke;
  }
  @media (forced-colors: active) {
    .spotnav-now-dots, .spotnav-now-arrow, .spotnav-now-disc, .spotnav-now-icon path { stroke: CanvasText; }
    .spotnav-now-name, .spotnav-now-value, .spotnav-now-pill text { fill: CanvasText; }
  }
`;
