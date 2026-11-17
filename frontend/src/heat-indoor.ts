// The heat card's indoor curve for one day (`indoor_day`, `custom_components/spotnav/heat/indoor_day.py`): what a
// lowering did to the rooms. The measured rooms up to now (their mean on the house's scale, and the coldest room where
// it differs), the comfort band shaded, and SpotNav's setpoint as a dashed step over the whole day (the band's
// `target_c` on the live card, the history's own per hour). Drawn under the day band on the card and in a history day.
// Every figure is the backend's; text is written with `textContent` only.

import { formatFixed, hourLabel, type FormatContext } from "./format";
import type { IndoorDay, IndoorPoint } from "./heat-model";
import { HEAT_CLASSES as X } from "./heat-styles";
import { translate, type Language } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

const SVG = "http://www.w3.org/2000/svg";
const HEIGHT = 96;
const TOP = 6;
const BOTTOM = 14;
/** The least span of the temperature axis (°C), so half a degree never fills the chart. */
const MIN_SPAN_C = 2;
/** The coldest room is drawn only where it differs from the mean by at least this (°C). */
const DIFFERS_C = 0.05;

/** One hour of the setpoint step: its start (ms), the target the heat plan uses and the band's action. */
export interface SetpointHour {
  startMs: number;
  target_c: number;
  action: string;
}


/** The history's setpoint hours (`indoor_day.setpoint`) as the chart takes them. */
export function setpointOf(day: IndoorDay): SetpointHour[] {
  return (day.setpoint ?? []).map((hour) => ({ startMs: Date.parse(hour.start), target_c: hour.target_c, action: hour.action }));
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
  item.append(swatch, doc.createTextNode(text));
  return item;
}

/** Whether the coldest room is worth its own line: it differs from the mean somewhere. */
export function coldestDiffers(day: IndoorDay): boolean {
  return day.points.some((point) => Math.abs(point.coldest_c - point.mean_c) >= DIFFERS_C);
}

/** The setpoint's legend words: it says when the step down is SpotNav's lowering. */
export function setpointWords(language: Language, setpoint: SetpointHour[]): string {
  return translate(language, setpoint.some((hour) => hour.action === "lower") ? "heat.indoor.setpointLowers" : "heat.indoor.setpoint");
}

/** The chart and its legend; `null` without a measured point (nothing to show what a lowering did). */
export function indoorChart(
  doc: Document,
  language: Language,
  ctx: FormatContext,
  day: IndoorDay,
  setpoint: SetpointHour[],
  width: number,
  nowMs: number | null,
): HTMLElement | null {
  if (day.points.length === 0) {
    return null;
  }
  const firstMs = Date.parse(day.start);
  const lastMs = Date.parse(day.end);
  if (!Number.isFinite(firstMs) || !Number.isFinite(lastMs) || lastMs <= firstMs) {
    return null;
  }
  const drawWidth = Math.max(200, Math.round(width));
  const stepMs = day.step_min * 60_000;
  const temperatures = [day.low_c, day.high_c, ...day.points.flatMap((point) => [point.mean_c, point.coldest_c]), ...setpoint.map((hour) => hour.target_c)];
  let low = Math.min(...temperatures) - 0.3;
  let high = Math.max(...temperatures) + 0.3;
  if (high - low < MIN_SPAN_C) {
    const middle = (high + low) / 2;
    low = middle - MIN_SPAN_C / 2;
    high = middle + MIN_SPAN_C / 2;
  }
  const x = (ms: number): number => ((ms - firstMs) / (lastMs - firstMs)) * drawWidth;
  const y = (celsius: number): number => TOP + ((high - celsius) / (high - low)) * (HEIGHT - TOP - BOTTOM);

  const block = doc.createElement("section");
  block.dataset["block"] = "indoor";
  const heading = doc.createElement("p");
  heading.className = `${C.muted} ${X.indoorHeading}`;
  heading.textContent = translate(language, "heat.indoor.title");
  const picture = svg(doc, "svg", { viewBox: `0 0 ${drawWidth} ${HEIGHT}`, class: X.indoor, role: "img" });
  picture.setAttribute("aria-label", translate(language, "heat.indoor.label"));

  // The comfort band, shaded across the day.
  const band = svg(doc, "rect", {
    x: 0,
    y: y(day.high_c).toFixed(2),
    width: drawWidth,
    height: Math.max(1, y(day.low_c) - y(day.high_c)).toFixed(2),
    class: X.indoorComfort,
  });
  picture.append(band);
  // The band's edges, written inside it at the right: outside, they would sit on the setpoint's step.
  for (const edge of [day.high_c, day.low_c]) {
    const label = svg(doc, "text", { x: drawWidth - 2, y: (y(edge) + (edge === day.high_c ? 9 : -2)).toFixed(2), class: `${X.bandAxis} ${X.indoorEdge}` });
    label.textContent = degrees(language, edge);
    picture.append(label);
  }

  // The setpoint: a dashed step over the whole day.
  if (setpoint.length > 0) {
    const hours = [...setpoint].sort((a, b) => a.startMs - b.startMs);
    const parts: string[] = [];
    hours.forEach((hour, index) => {
      const from = x(Math.max(hour.startMs, firstMs));
      const to = x(Math.min(index + 1 < hours.length ? hours[index + 1]!.startMs : hour.startMs + 3_600_000, lastMs));
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
    for (const point of day.points) {
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
  const withColdest = coldestDiffers(day);
  if (withColdest) {
    line((point) => point.coldest_c, "coldest");
  }
  line((point) => point.mean_c, "mean");

  for (let ms = firstMs; ms < lastMs; ms += 6 * 3_600_000) {
    const text = svg(doc, "text", { x: (x(ms) + 1).toFixed(2), y: HEIGHT - 2, class: X.bandAxis });
    text.textContent = hourLabel(ctx, ms);
    picture.append(text);
  }
  if (nowMs !== null && nowMs >= firstMs && nowMs < lastMs) {
    const at = x(nowMs).toFixed(2);
    picture.append(svg(doc, "line", { x1: at, x2: at, y1: TOP - 4, y2: HEIGHT - BOTTOM, class: X.bandNow }));
  }

  const legend = doc.createElement("ul");
  legend.className = X.legend;
  legend.append(legendItem(doc, "indoor_mean", translate(language, "heat.indoor.mean")));
  if (withColdest) {
    legend.append(legendItem(doc, "indoor_coldest", translate(language, "heat.indoor.coldest")));
  }
  if (setpoint.length > 0) {
    legend.append(legendItem(doc, "setpoint", setpointWords(language, setpoint)));
  }
  legend.append(
    legendItem(doc, "comfort", translate(language, "heat.indoor.comfort", { low: formatFixed(language, day.low_c, 1), high: formatFixed(language, day.high_c, 1) })),
  );
  block.append(heading, picture, legend);
  return block;
}
