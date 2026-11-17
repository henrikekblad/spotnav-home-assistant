// The heat card's day chart: one chart, one time axis, for what the day did and does. The measured rooms up to now
// (their mean on the house's scale, and the coldest room where it differs; `indoor_day`,
// `custom_components/spotnav/heat/indoor_day.py`), the comfort band shaded, SpotNav's setpoint as a dashed step, the
// hours SpotNav lowered (solid columns) and plans to lower (light columns), on SG Ready with both contacts the hours it
// raised and plans to raise (warm columns; "would raise" while SpotNav only watches), the relay as anyone else closed it, hot
// water as droplets on the time axis and the car's charging as a thin bar under it. A pointer, a tap or the arrow keys
// put a crosshair on a time and say its figures in words under the chart. Drawn on the live card (from the band's
// hours) and in a history day (from the day's setpoint and hot-water runs). Every figure is the backend's; text is
// written with `textContent` only.

import { attachScrub } from "./chart-scrub";
import { clock, formatFixed, hourLabel, type FormatContext } from "./format";
import { roomName, type IndoorDay, type IndoorPoint } from "./heat-model";
import { HEAT_CLASSES as X } from "./heat-styles";
import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

const SVG = "http://www.w3.org/2000/svg";
const HOUR_MS = 3_600_000;
const HEIGHT = 140;
const TOP = 8;
/** The plot's floor: the time axis, with the hot-water droplets standing on it. */
const PLOT_BOTTOM = 114;
/** The car's bar, just under the axis. */
const CAR_Y = 118;
const CAR_HEIGHT = 4;
/** The hour labels' baseline. */
const LABEL_Y = 136;
/** The least span of the temperature axis (°C), so half a degree never fills the chart. */
const MIN_SPAN_C = 2;
/** The coldest room is drawn (and said) only where it differs from the mean by at least this (°C). */
const DIFFERS_C = 0.05;

/** One hour of the day as the chart takes it: the setpoint and what lowered the heat, the hot water and the car. */
export interface DayHour {
  startMs: number;
  /** The target the heat plan uses in the hour (°C). */
  target_c: number;
  /** The plan's action: `lower`, `preheat`, `night`, `away`, `return` or `normal`. */
  action: string;
  /** Why the plan lowers or keeps the hour (`dear_hour`, `tight_hour`, `room_warm`, …); absent where not known. */
  reason?: string;
  /** Minutes SpotNav itself held the relay closed in the hour; `null` where nothing is known. */
  executed_min: number | null;
  /** Minutes SpotNav held SG Ready's B (the raise) closed; `null` (or absent) where the pump cannot be raised. */
  raised_min?: number | null;
  /** Minutes the relay was (or is expected to be) closed by anyone; `null` without a relay. */
  relay_min: number | null;
  relay_expected: boolean;
  car: boolean;
}

/** A span of the day: a hot-water run, or an hour the plan heats water in. */
export interface DaySpan {
  startMs: number;
  endMs: number;
}

export interface DayChartInput {
  doc: Document;
  language: Language;
  ctx: FormatContext;
  /** The local day's bounds (ms). */
  startMs: number;
  endMs: number;
  /** The measured rooms, `null` without indoor sensors. */
  day: IndoorDay | null;
  /** The comfort band's edges (°C). */
  low_c: number;
  high_c: number;
  hours: DayHour[];
  water: DaySpan[];
  width: number;
  nowMs: number | null;
  /** SpotNav only watches (its control does not run): a planned raise reads "would raise". */
  shadow?: boolean;
  /** The time the reader chose (a step's start), kept across a redraw; `null` for none. */
  selectedMs?: number | null;
  /** A time was chosen (`null`: let go), so the card can keep it across its next redraw. */
  onSelect?: (ms: number | null) => void;
}

/** The history's setpoint hours (`indoor_day.setpoint`) as the chart takes them: the kept plan, no relay. */
export function hoursOf(day: IndoorDay): DayHour[] {
  return (day.setpoint ?? []).map((hour) => ({
    startMs: Date.parse(hour.start),
    target_c: hour.target_c,
    action: hour.action,
    executed_min: null,
    relay_min: null,
    relay_expected: false,
    car: false,
  }));
}

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, tag: K, attributes: Record<string, string | number>): SVGElementTagNameMap[K] {
  const node = doc.createElementNS(SVG, tag) as SVGElementTagNameMap[K];
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, String(value));
  }
  return node;
}

function degrees(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} °C`;
}

function legendItem(doc: Document, action: string, text: string): HTMLElement {
  const item = doc.createElement("li");
  item.className = X.legendItem;
  const swatch = doc.createElement("span");
  swatch.className = X.swatch;
  swatch.dataset["action"] = action;
  swatch.setAttribute("aria-hidden", "true");
  item.append(swatch, doc.createTextNode(text));
  return item;
}

/** Whether the coldest room is worth its own line: it differs from the mean somewhere. */
export function coldestDiffers(day: IndoorDay): boolean {
  return day.points.some((point) => Math.abs(point.coldest_c - point.mean_c) >= DIFFERS_C);
}

/** The setpoint's legend words: it says when the step down is SpotNav's lowering. */
export function setpointWords(language: Language, hours: DayHour[]): string {
  return translate(language, hours.some((hour) => hour.action === "lower") ? "heat.indoor.setpointLowers" : "heat.indoor.setpoint");
}

/** What lowered the heat in an hour, as the chart's columns draw it. */
interface Lowering {
  /** SpotNav's own lowering, minutes (solid). */
  executed: number;
  /** SpotNav's plan lowers the hour (light, the whole hour). */
  planned: boolean;
  /** The relay closed by anyone else, minutes, and whether that is only expected. */
  relay: number;
  relayExpected: boolean;
  /** SpotNav's own raise on SG Ready's B, minutes (solid, warm). */
  raised: number;
  /** SpotNav's plan raises the hour (light, warm): a pre-heat or return hour where the pump can be raised. */
  raisePlanned: boolean;
}

/** The plan's actions SG Ready's B raises in (`relay_control.RAISE_ACTIONS`). */
const RAISE_ACTIONS = ["preheat", "return"];

function loweringOf(hour: DayHour): Lowering {
  const executed = Math.min(60, Math.max(0, hour.executed_min ?? 0));
  const relay = executed > 0 ? 0 : Math.min(60, Math.max(0, hour.relay_min ?? 0));
  const raisable = hour.raised_min !== undefined && hour.raised_min !== null;
  const raised = raisable ? Math.min(60, Math.max(0, hour.raised_min ?? 0)) : 0;
  return {
    executed,
    planned: hour.action === "lower",
    relay,
    relayExpected: relay > 0 && hour.relay_expected,
    raised,
    raisePlanned: raisable && RAISE_ACTIONS.includes(hour.action),
  };
}

const ACTION_KEYS: Record<string, TranslationKey> = {
  preheat: "heat.chart.preheat",
  return: "heat.chart.preheat",
  night: "heat.chart.night",
  away: "heat.chart.away",
};

/** The step a time falls in: its start (ms). */
function stepStart(input: DayChartInput, stepMs: number, ms: number): number {
  const count = Math.max(1, Math.ceil((input.endMs - input.startMs) / stepMs));
  const index = Math.min(count - 1, Math.max(0, Math.floor((ms - input.startMs) / stepMs)));
  return input.startMs + index * stepMs;
}

function stepOf(input: DayChartInput): number {
  return (input.day?.step_min ?? 60) * 60_000;
}

function hourAt(input: DayChartInput, ms: number): DayHour | undefined {
  return input.hours.find((hour) => hour.startMs <= ms && ms < hour.startMs + HOUR_MS);
}

function pointAt(input: DayChartInput, ms: number): IndoorPoint | undefined {
  return input.day?.points.find((point) => Date.parse(point.start) === ms);
}

/** One step's figures in words: "17:15 · rooms 21.0 °C · coldest Bathroom 20.6 °C · setpoint 20.5 °C · …". */
export function dayFigures(input: DayChartInput, ms: number): string {
  const { language, ctx } = input;
  const at = stepStart(input, stepOf(input), ms);
  const parts = [clock(ctx, at)];
  const point = pointAt(input, at);
  if (point !== undefined) {
    parts.push(translate(language, "heat.chart.mean", { temp: degrees(language, point.mean_c) }));
    if (Math.abs(point.coldest_c - point.mean_c) >= DIFFERS_C) {
      const name = input.day?.rooms.find((room) => room.entity_id === point.coldest)?.name;
      parts.push(translate(language, "heat.chart.coldest", { room: roomName(name ?? point.coldest), temp: degrees(language, point.coldest_c) }));
    }
  }
  const hour = hourAt(input, at);
  if (hour !== undefined) {
    parts.push(translate(language, "heat.chart.setpoint", { temp: degrees(language, hour.target_c) }));
    const lowering = loweringOf(hour);
    if (lowering.executed > 0) {
      parts.push(translate(language, "heat.band.executedHour", { minutes: String(Math.round(lowering.executed)) }));
    } else if (lowering.relay > 0) {
      parts.push(translate(language, lowering.relayExpected ? "heat.band.relayHourExpected" : "heat.band.relayHour", { minutes: String(Math.round(lowering.relay)) }));
    }
    if (lowering.planned && lowering.executed === 0) {
      parts.push(translate(language, "heat.chart.plannedHour"));
    }
    if (lowering.raised > 0) {
      parts.push(translate(language, "heat.band.raisedHour", { minutes: String(Math.round(lowering.raised)) }));
    } else if (lowering.raisePlanned) {
      parts.push(translate(language, input.shadow === true ? "heat.chart.wouldRaiseHour" : "heat.chart.plannedRaiseHour"));
    }
    if (lowering.planned && hour.reason === "room_warm") {
      parts.push(translate(language, "heat.chart.roomWarm"));
    }
    const key = ACTION_KEYS[hour.action];
    if (key !== undefined) {
      parts.push(translate(language, key));
    }
  }
  const end = at + stepOf(input);
  if (input.water.some((span) => span.startMs < end && at < span.endMs)) {
    parts.push(translate(language, "heat.chart.hotWater"));
  }
  if (hour?.car === true) {
    parts.push(translate(language, "heat.chart.car"));
  }
  return parts.join(" · ");
}

/** A water droplet standing on the axis at `x`, its tip up. */
function droplet(x: number): string {
  const base = PLOT_BOTTOM - 1;
  return `M${x.toFixed(2)} ${base - 10} C${(x + 1.5).toFixed(2)} ${base - 7} ${(x + 4).toFixed(2)} ${base - 5} ${(x + 4).toFixed(2)} ${base - 3.5}`
    + ` A4 4 0 0 1 ${(x - 4).toFixed(2)} ${base - 3.5} C${(x - 4).toFixed(2)} ${base - 5} ${(x - 1.5).toFixed(2)} ${base - 7} ${x.toFixed(2)} ${base - 10} Z`;
}

/**
 * The chart with its readout and legend; `null` with nothing to draw (no measured room and no hour of the plan).
 * The element is a `section` with `data-block="day"`.
 */
export function dayChart(input: DayChartInput): HTMLElement | null {
  const { doc, language, ctx, day, hours } = input;
  const points = day?.points ?? [];
  if (points.length === 0 && hours.length === 0) {
    return null;
  }
  const firstMs = input.startMs;
  const lastMs = input.endMs;
  if (!Number.isFinite(firstMs) || !Number.isFinite(lastMs) || lastMs <= firstMs) {
    return null;
  }
  const drawWidth = Math.max(200, Math.round(input.width));
  const stepMs = stepOf(input);
  const temperatures = [input.low_c, input.high_c, ...points.flatMap((point) => [point.mean_c, point.coldest_c]), ...hours.map((hour) => hour.target_c)];
  let low = Math.min(...temperatures) - 0.3;
  let high = Math.max(...temperatures) + 0.3;
  if (high - low < MIN_SPAN_C) {
    const middle = (high + low) / 2;
    low = middle - MIN_SPAN_C / 2;
    high = middle + MIN_SPAN_C / 2;
  }
  const x = (ms: number): number => ((Math.min(lastMs, Math.max(firstMs, ms)) - firstMs) / (lastMs - firstMs)) * drawWidth;
  const y = (celsius: number): number => TOP + ((high - celsius) / (high - low)) * (PLOT_BOTTOM - TOP);

  const block = doc.createElement("section");
  block.dataset["block"] = "day";
  const surface = doc.createElement("div");
  surface.className = X.daySurface;
  surface.tabIndex = 0;
  surface.setAttribute("role", "group");
  surface.setAttribute("aria-label", translate(language, "heat.chart.label"));
  const picture = svg(doc, "svg", { viewBox: `0 0 ${drawWidth} ${HEIGHT}`, class: X.day, "aria-hidden": "true" });
  surface.append(picture);
  const readout = doc.createElement("p");
  readout.className = `${C.muted} ${X.dayReadout}`;
  readout.setAttribute("aria-live", "polite");
  readout.dataset["line"] = "day-readout";

  // The comfort band, shaded across the day.
  picture.append(svg(doc, "rect", {
    x: 0,
    y: y(input.high_c).toFixed(2),
    width: drawWidth,
    height: Math.max(1, y(input.low_c) - y(input.high_c)).toFixed(2),
    class: X.indoorComfort,
  }));

  // What lowered the heat, hour by hour: SpotNav's plan light over the whole hour, what SpotNav did solid over its
  // minutes, the relay as anyone else closed it in teal.
  const sorted = [...hours].sort((a, b) => a.startMs - b.startMs);
  const lowerings = sorted.map(loweringOf);
  sorted.forEach((hour, index) => {
    const lowering = lowerings[index]!;
    const from = x(hour.startMs);
    const whole = x(hour.startMs + HOUR_MS) - from;
    const column = (kind: string, share: number): void => {
      const rect = svg(doc, "rect", { x: from.toFixed(2), y: TOP, width: Math.max(1, whole * share).toFixed(2), height: PLOT_BOTTOM - TOP, class: X.dayColumn });
      rect.dataset["mark"] = kind;
      picture.append(rect);
    };
    if (lowering.planned) {
      column("planned", 1);
    }
    if (lowering.executed > 0) {
      column("executed", lowering.executed / 60);
    } else if (lowering.relay > 0) {
      column(lowering.relayExpected ? "relay_expected" : "relay", lowering.relay / 60);
    }
    // SG Ready's raise: the plan light over the whole hour, what SpotNav did solid over its minutes.
    if (lowering.raisePlanned) {
      column("raise_planned", 1);
    }
    if (lowering.raised > 0) {
      column("raised", lowering.raised / 60);
    }
  });

  // The setpoint: a dashed step over the whole day.
  if (sorted.length > 0) {
    const parts: string[] = [];
    sorted.forEach((hour, index) => {
      const from = x(hour.startMs);
      const to = x(Math.min(index + 1 < sorted.length ? sorted[index + 1]!.startMs : hour.startMs + HOUR_MS, lastMs));
      const level = y(hour.target_c).toFixed(2);
      parts.push(`${index === 0 ? "M" : "L"}${from.toFixed(2)} ${level}`, `L${to.toFixed(2)} ${level}`);
    });
    const step = svg(doc, "path", { d: parts.join(" "), class: X.indoorSetpoint });
    step.dataset["line"] = "setpoint";
    picture.append(step);
  }

  // The measured rooms up to now: a gap in the readings breaks the line.
  const line = (value: (point: IndoorPoint) => number, kind: string): void => {
    const parts: string[] = [];
    let previous: number | null = null;
    for (const point of points) {
      const startMs = Date.parse(point.start);
      if (!Number.isFinite(startMs)) {
        continue;
      }
      const at = `${x(startMs + stepMs / 2).toFixed(2)} ${y(value(point)).toFixed(2)}`;
      parts.push(`${previous !== null && startMs - previous <= stepMs ? "L" : "M"}${at}`);
      previous = startMs;
    }
    const path = svg(doc, "path", { d: parts.join(" "), class: X.indoorLine });
    path.dataset["line"] = kind;
    picture.append(path);
  };
  const withColdest = day !== null && coldestDiffers(day);
  if (withColdest) {
    line((point) => point.coldest_c, "coldest");
  }
  if (points.length > 0) {
    line((point) => point.mean_c, "mean");
  }

  // The band's edges, written inside it at the right: outside, they would sit on the setpoint's step.
  for (const edge of [input.high_c, input.low_c]) {
    const label = svg(doc, "text", { x: drawWidth - 2, y: (y(edge) + (edge === input.high_c ? 9 : -2)).toFixed(2), class: `${X.dayAxis} ${X.indoorEdge}` });
    label.textContent = degrees(language, edge);
    picture.append(label);
  }

  // The time axis, the car's charging under it and the hot water standing on it.
  picture.append(svg(doc, "line", { x1: 0, x2: drawWidth, y1: PLOT_BOTTOM, y2: PLOT_BOTTOM, class: X.dayBaseline }));
  // The car's hours, a run of them as one bar.
  const runs: DaySpan[] = [];
  for (const hour of sorted) {
    if (!hour.car) {
      continue;
    }
    const last = runs[runs.length - 1];
    if (last !== undefined && last.endMs === hour.startMs) {
      last.endMs = hour.startMs + HOUR_MS;
    } else {
      runs.push({ startMs: hour.startMs, endMs: hour.startMs + HOUR_MS });
    }
  }
  for (const run of runs) {
    const from = x(run.startMs);
    const mark = svg(doc, "rect", { x: from.toFixed(2), y: CAR_Y, width: Math.max(1, x(run.endMs) - from).toFixed(2), height: CAR_HEIGHT, rx: CAR_HEIGHT / 2, class: X.dayMark });
    mark.dataset["mark"] = "car";
    picture.append(mark);
  }
  for (const span of input.water) {
    const mark = svg(doc, "path", { d: droplet(x((span.startMs + span.endMs) / 2)), class: X.dayMark });
    mark.dataset["mark"] = "hot_water";
    picture.append(mark);
  }
  const every = drawWidth >= 480 ? 3 : 6;
  for (let ms = firstMs; ms < lastMs; ms += HOUR_MS) {
    const hourOfDay = Number(hourLabel(ctx, ms));
    if (!Number.isFinite(hourOfDay) || hourOfDay % every !== 0) {
      continue;
    }
    const tick = svg(doc, "text", { x: (x(ms) + 1).toFixed(2), y: LABEL_Y, class: X.dayAxis });
    tick.textContent = hourLabel(ctx, ms);
    picture.append(tick);
  }
  if (input.nowMs !== null && input.nowMs >= firstMs && input.nowMs < lastMs) {
    const at = x(input.nowMs).toFixed(2);
    picture.append(svg(doc, "line", { x1: at, x2: at, y1: TOP - 4, y2: CAR_Y + CAR_HEIGHT, class: X.dayNow }));
  }

  // The crosshair: a line at the chosen step and a dot on each room line there.
  const cross = svg(doc, "line", { x1: 0, x2: 0, y1: TOP - 4, y2: CAR_Y + CAR_HEIGHT, class: X.dayCross, display: "none" });
  const meanDot = svg(doc, "circle", { r: 4, class: X.dayDot, display: "none" });
  meanDot.dataset["line"] = "mean";
  const coldDot = svg(doc, "circle", { r: 3, class: X.dayDot, display: "none" });
  coldDot.dataset["line"] = "coldest";
  picture.append(cross, coldDot, meanDot);

  const hint = translate(language, "heat.chart.hint");
  let chosen = input.selectedMs ?? null;
  const show = (ms: number | null): void => {
    if (ms === null) {
      readout.textContent = hint;
      for (const node of [cross, meanDot, coldDot]) {
        node.setAttribute("display", "none");
      }
      return;
    }
    const at = stepStart(input, stepMs, ms);
    const middle = x(at + stepMs / 2).toFixed(2);
    cross.setAttribute("x1", middle);
    cross.setAttribute("x2", middle);
    cross.removeAttribute("display");
    const point = pointAt(input, at);
    for (const [dot, value] of [[meanDot, point?.mean_c], [coldDot, withColdest && point !== undefined && Math.abs(point.coldest_c - point.mean_c) >= DIFFERS_C ? point.coldest_c : undefined]] as const) {
      if (value === undefined) {
        dot.setAttribute("display", "none");
      } else {
        dot.setAttribute("cx", middle);
        dot.setAttribute("cy", y(value).toFixed(2));
        dot.removeAttribute("display");
      }
    }
    readout.textContent = dayFigures(input, at);
  };
  const choose = (ms: number | null): void => {
    chosen = ms === null ? null : stepStart(input, stepMs, ms);
    show(chosen);
    input.onSelect?.(chosen);
  };
  const timeAt = (event: MouseEvent): number | null => {
    const box = picture.getBoundingClientRect();
    if (box.width <= 0) {
      return null;
    }
    return firstMs + ((event.clientX - box.left) / box.width) * (lastMs - firstMs);
  };
  // By the pointer's x alone, anywhere in the chart's height; a horizontal touch drag scrubs and keeps the
  // step it ends on, a vertical one scrolls the page (`touch-action: pan-y`).
  let scrubbed: number | null = null;
  attachScrub(surface, {
    hover: (event) => show(timeAt(event)),
    leave: () => show(chosen),
    drag: (event) => {
      scrubbed = timeAt(event) ?? scrubbed;
      show(scrubbed);
    },
    end: () => {
      if (scrubbed !== null) {
        choose(scrubbed);
      }
      scrubbed = null;
    },
  });
  surface.addEventListener("click", (event) => choose(timeAt(event)));
  surface.addEventListener("keydown", (event) => {
    const count = Math.max(1, Math.ceil((lastMs - firstMs) / stepMs));
    const base = chosen ?? (input.nowMs !== null && input.nowMs >= firstMs && input.nowMs < lastMs ? stepStart(input, stepMs, input.nowMs) : null);
    // A step at a time; Page Up and Page Down an hour.
    const by = (steps: number): number => {
      const index = base === null ? (steps > 0 ? -1 : count) : Math.round((base - firstMs) / stepMs);
      return firstMs + Math.min(count - 1, Math.max(0, index + steps)) * stepMs;
    };
    const perHour = Math.max(1, Math.round(HOUR_MS / stepMs));
    const keys: Record<string, () => number | null> = {
      ArrowRight: () => by(1),
      ArrowLeft: () => by(-1),
      PageDown: () => by(perHour),
      PageUp: () => by(-perHour),
      Home: () => firstMs,
      End: () => firstMs + (count - 1) * stepMs,
      Escape: () => null,
    };
    const next = keys[event.key];
    if (next === undefined) {
      return;
    }
    event.preventDefault();
    choose(next());
  });
  show(chosen);

  // The legend: only what the day has.
  const legend = doc.createElement("ul");
  legend.className = X.legend;
  if (points.length > 0) {
    legend.append(legendItem(doc, "indoor_mean", translate(language, "heat.indoor.mean")));
  }
  if (withColdest) {
    legend.append(legendItem(doc, "indoor_coldest", translate(language, "heat.indoor.coldest")));
  }
  if (sorted.length > 0) {
    legend.append(legendItem(doc, "setpoint", setpointWords(language, sorted)));
  }
  legend.append(
    legendItem(doc, "comfort", translate(language, "heat.indoor.comfort", { low: formatFixed(language, input.low_c, 1), high: formatFixed(language, input.high_c, 1) })),
  );
  if (lowerings.some((lowering) => lowering.executed > 0)) {
    legend.append(legendItem(doc, "executed", translate(language, "heat.band.executed")));
  }
  if (lowerings.some((lowering) => lowering.planned)) {
    legend.append(legendItem(doc, "planned", translate(language, "heat.chart.planned")));
  }
  if (lowerings.some((lowering) => lowering.raised > 0)) {
    legend.append(legendItem(doc, "raised", translate(language, "heat.band.raised")));
  }
  if (lowerings.some((lowering) => lowering.raisePlanned)) {
    legend.append(legendItem(doc, "raise_planned", translate(language, input.shadow === true ? "heat.chart.wouldRaise" : "heat.chart.plannedRaise")));
  }
  if (lowerings.some((lowering) => lowering.relay > 0 && !lowering.relayExpected)) {
    legend.append(legendItem(doc, "relay", translate(language, "heat.band.relay")));
  }
  if (lowerings.some((lowering) => lowering.relayExpected)) {
    legend.append(legendItem(doc, "relay_expected", translate(language, "heat.band.relayExpected")));
  }
  if (input.water.length > 0) {
    legend.append(legendItem(doc, "hot_water", translate(language, "heat.band.hotWater")));
  }
  if (sorted.some((hour) => hour.car)) {
    legend.append(legendItem(doc, "car", translate(language, "heat.band.car")));
  }
  // The readout right under the chart, where the eye is when a finger or the pointer is on it.
  block.append(surface, readout, legend);
  return block;
}
