// The house card's one chart, as SVG, from 12 hours back to 24 hours ahead on one real-time axis, in one of two
// views (like the battery app's two charts): "energy", the house's load per hour, the sun and the battery's level;
// "price", the buy and sell price per hour (left axis, minor unit per kWh) with the battery's level (right axis).
// The price view tints the hours the plan charges from the grid (violet) and sells to the grid (teal); both mark now
// ("Nu"). A viewer may hide any series from the legend (`hidden`, see `house-series.ts`): it is left out and the kW axis
// rescales to what is shown.
//
// * The house: a bar per hour, the measured load in the past and the forecast ahead (lighter); over a past bar a short
//   line stands at what was forecast for that hour first, so a miss shows at a glance. Where the heat pump's meter is,
//   the bar's lower part is heat (measured in the past, forecast ahead) in its own warm colour: part of the house, never
//   stacked on top of it.
// * The car: its own bar per past hour (the energy flow's car colour), beside the house's bar (the house's takes the left
//   part of the hour, the car's the right) so neither hides the other; hidden until toggled on in the legend.
// * The sun: a soft area, the production in the past and the corrected forecast ahead (the raw one where there is
//   none); the raw forecast as a dotted line over all of it, since the Hybrid strategy reads that one.
// * The battery: its charge level on its own scale (0-100 % over the plot's height), measured in the past and as the
//   plan expects it ahead (dashed); the hours the plan charges from the grid are tinted, the hours it sells lightly.
// * The now line at the running hour's present, and a dashed selection line at a chosen hour.
//
// The geometry is the price chart's (`chart.ts`): its metrics and nice steps for the kW axis, `rangeAxis` and
// `xForWallClock` for the time; the gridlines, axis text and now line use its classes. Nothing is computed from
// the figures beyond placing them; every figure is the backend's.

import { chartScale, gutterLabels, rangeAxis, xForWallClock, yAt, zoneClock, type ChartScale } from "./chart";
import type { HouseHour } from "./house-model";
import { HOUSE_CLASSES as H } from "./house-styles";
import { CHART_DENSITY, VISUAL_CLASSES as C, type ChartDensityStyle } from "./visual-styles";

const SVG_NS = "http://www.w3.org/2000/svg";
const HOUR_MS = 3_600_000;

/** The kW axis in nice steps (about three), the price chart's text and lines otherwise. */
export const HOUSE_DENSITY: ChartDensityStyle = { ...CHART_DENSITY.normal, priceSteps: 3, heightRatio: 0.44, minHeight: 150, maxHeight: 230 };

export function houseChartHeight(width: number): number {
  const bounded = Number.isFinite(width) && width > 0 ? width : 340;
  return Math.round(Math.min(HOUSE_DENSITY.maxHeight, Math.max(HOUSE_DENSITY.minHeight, bounded * HOUSE_DENSITY.heightRatio)));
}

export type HouseChartView = "energy" | "price";

export interface HouseChartLabels {
  /** A kW axis label. */
  kw(value: number): string;
  /** A price axis label (minor unit per kWh), and the unit's name for the top label. */
  price(value: number): string;
  priceUnit: string;
  /** The word on the now line. */
  now: string;
  /** The hour label under a gridline ("06"), or the day's cue at midnight ("lör 14"). */
  hour(instantMs: number): string;
  day(instantMs: number): string;
}

export interface HouseChartInput {
  hours: readonly HouseHour[];
  view?: HouseChartView;
  timeZone: string;
  nowMs: number;
  width: number;
  height: number;
  labels: HouseChartLabels;
  title: string;
  description: string;
  ids: { title: string; description: string };
  /** The series the viewer hid (`HouseSeries` keys); none by default. */
  hidden?: ReadonlySet<string>;
}

export interface HouseChart {
  element: SVGSVGElement;
  scale: ChartScale;
  /** The hour under an x in the SVG's own units, `null` outside the plot. */
  hourAt(x: number): number | null;
  /** Show the selection line at an hour (`null` hides it). */
  select(index: number | null): void;
}

function svg<K extends keyof SVGElementTagNameMap>(name: K, attributes: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const created = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    created.setAttribute(key, String(value));
  }
  return created;
}

/** The sun's figure drawn in an hour: the production in the past, the corrected forecast ahead (else the raw). */
export function sunShown(hour: HouseHour, nowMs: number): number | null {
  if (hour.startMs + HOUR_MS <= nowMs) {
    return hour.sun_kwh;
  }
  return hour.sun_corrected_kwh ?? hour.sun_forecast_kwh;
}

/** The house's figure drawn in an hour: the measured load in the past, else the forecast. */
export function houseShown(hour: HouseHour): { kw: number | null; measured: boolean } {
  return hour.house_kw !== null ? { kw: hour.house_kw, measured: true } : { kw: hour.house_forecast_kw, measured: false };
}

/** The heat pump's part of an hour's bar: the measured use beside a measured house figure, the forecast beside a
 * forecast one; never above the house's own figure. `null` without one. */
export function heatShown(hour: HouseHour): number | null {
  const house = houseShown(hour);
  const heat = house.measured ? hour.heat_kw : hour.heat_forecast_kw;
  if (heat === null || house.kw === null) {
    return null;
  }
  return Math.max(0, Math.min(heat, house.kw));
}

/** A path through points, broken where a point is missing; `area` closes each run down to `baseY`. */
function pathOf(points: Array<{ x: number; y: number } | null>, area: boolean, baseY: number): string {
  const runs: Array<Array<{ x: number; y: number }>> = [];
  let run: Array<{ x: number; y: number }> = [];
  for (const point of points) {
    if (point === null) {
      if (run.length > 0) {
        runs.push(run);
      }
      run = [];
      continue;
    }
    run.push(point);
  }
  if (run.length > 0) {
    runs.push(run);
  }
  const f = (value: number): string => value.toFixed(2);
  return runs
    .filter((points_) => points_.length > (area ? 0 : 1))
    .map((points_) => {
      const line = points_.map((point, index) => `${index === 0 ? "M" : "L"}${f(point.x)},${f(point.y)}`).join("");
      if (!area) {
        return line;
      }
      const first = points_[0]!;
      const last = points_[points_.length - 1]!;
      return `M${f(first.x)},${f(baseY)}L${line.slice(1)}L${f(last.x)},${f(baseY)}Z`;
    })
    .join("");
}

export function renderHouseChart(input: HouseChartInput): HouseChart {
  const { hours, nowMs, labels } = input;
  const view = input.view ?? "energy";
  const shown_ = (series: string): boolean => !(input.hidden?.has(series) ?? false);
  const count = hours.length;
  const startMs = hours[0]!.startMs;
  const endMs = hours[count - 1]!.startMs + HOUR_MS;
  const axis = rangeAxis(startMs, endMs);
  const values: number[] = [];
  for (const hour of hours) {
    const figures: Array<number | null> =
      view === "price"
        ? [shown_("buy") ? hour.buy : null, shown_("sellPrice") ? hour.sell : null]
        : [
            shown_("house") ? houseShown(hour).kw : null,
            shown_("forecast") && hour.house_kw !== null ? hour.house_forecast_kw : null,
            shown_("sun") ? sunShown(hour, nowMs) : null,
            shown_("sun") ? hour.sun_forecast_kwh : null,
            shown_("heat") ? heatShown(hour) : null,
            shown_("car") ? hour.car_kw : null,
          ];
    for (const value of figures) {
      if (value !== null) {
        values.push(value);
      }
    }
  }
  const base_ = chartScale(input.width, input.height, values, HOUSE_DENSITY);
  // The price view keeps a gutter on the right for the battery's 0-100 % axis.
  const scale: ChartScale = view === "price" ? { ...base_, right: base_.right - base_.axisTextSize * 2.6 } : base_;
  const x = (instantMs: number): number => xForWallClock(scale, axis.position(instantMs));
  const centre = (hour: HouseHour): number => x(hour.startMs + HOUR_MS / 2);
  const socY = (pct: number): number => scale.bottom - (Math.min(100, Math.max(0, pct)) / 100) * (scale.bottom - scale.top);

  const root = svg("svg", {
    class: `${C.svg} ${H.chart}`,
    viewBox: `0 0 ${scale.width} ${scale.height}`,
    preserveAspectRatio: "none",
    role: "img",
    "aria-labelledby": input.ids.title,
    "aria-describedby": input.ids.description,
  });
  const title = svg("title", { id: input.ids.title });
  title.textContent = input.title;
  const description = svg("desc", { id: input.ids.description });
  description.textContent = input.description;
  root.append(title, description);
  // Soft vertical gradients: the house's bars fade a little toward the base, the sun's area toward the axis.
  const defs = svg("defs");
  const gradient = (id: string, colour: string, stops: ReadonlyArray<readonly [string, string]>): string => {
    const created = svg("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 });
    for (const [offset, opacity] of stops) {
      const stop = svg("stop", { offset, "stop-opacity": opacity });
      stop.style.setProperty("stop-color", colour);
      created.append(stop);
    }
    defs.append(created);
    return `url(#${id})`;
  };
  const barFill = gradient(`${input.ids.title}-bar`, "var(--spotnav-house-colour)", [
    ["0", "1"],
    ["1", "0.62"],
  ]);
  const sunFill = gradient(`${input.ids.title}-sun`, "var(--spotnav-sun-colour)", [
    ["0", "0.55"],
    ["1", "0.08"],
  ]);
  root.append(defs);

  // The plan's grid charging and selling, behind everything.
  const bands = svg("g", { class: H.bands });
  for (const hour of view === "price" ? hours : []) {
    const wanted = hour.battery_action === "grid_charge" ? "charge" : hour.battery_action === "export_discharge" ? "sell" : null;
    if (wanted === null || !shown_(wanted)) {
      continue;
    }
    const left = x(hour.startMs);
    bands.append(
      svg("rect", {
        class: H.band,
        "data-action": hour.battery_action ?? "",
        x: left,
        y: scale.top,
        width: Math.max(0, x(hour.startMs + HOUR_MS) - left),
        height: scale.bottom - scale.top,
      }),
    );
  }
  root.append(bands);

  const grid = svg("g", { class: C.axis });
  const axisLabel = (value: number): string => {
    const text = view === "price" ? labels.price(value) : labels.kw(value);
    return value === scale.maxValue ? `${text} ${view === "price" ? labels.priceUnit : "kW"}` : text;
  };
  for (const label of gutterLabels(scale, axisLabel)) {
    grid.append(svg("line", { class: C.gridline, x1: scale.left, x2: scale.right, y1: label.y, y2: label.y }));
    const tick = svg("text", { class: C.axisLabel, x: scale.pad, y: label.y + scale.axisTextSize * 0.35, "text-anchor": "start" });
    tick.textContent = label.label;
    grid.append(tick);
  }
  const clock = zoneClock(input.timeZone);
  for (const hour of hours) {
    const local = Math.round(clock.msOfDay(hour.startMs) / HOUR_MS);
    if (local % 6 !== 0) {
      continue;
    }
    const left = x(hour.startMs);
    const midnight = local % 24 === 0;
    grid.append(
      svg("line", {
        class: midnight ? `${C.gridline} ${C.dayDivider}` : C.gridline,
        x1: left,
        x2: left,
        y1: scale.top,
        y2: scale.bottom,
      }),
    );
    const anchor = left - scale.left < scale.axisTextSize * 1.5 ? "start" : scale.right - left < scale.axisTextSize * 1.5 ? "end" : "middle";
    const text = svg("text", {
      class: midnight ? `${C.tickLabel} ${C.dayLabel}` : C.tickLabel,
      x: left,
      y: scale.bottom + scale.axisTextSize,
      "text-anchor": anchor,
    });
    text.textContent = midnight ? labels.day(hour.startMs) : labels.hour(hour.startMs);
    grid.append(text);
  }
  root.append(grid);

  if (view === "price" && shown_("battery")) {
    for (const pct of [0, 50, 100]) {
      const tick = svg("text", {
        class: C.axisLabel,
        x: scale.width - scale.pad * 0.4,
        y: socY(pct) + scale.axisTextSize * 0.35,
        "text-anchor": "end",
      });
      tick.textContent = `${pct} %`;
      grid.append(tick);
    }
  }

  // The sun: the area, then the raw forecast dotted over it.
  const base = yAt(scale, 0);
  if (view === "price") {
    for (const [key, className, series] of [
      ["sell", H.priceSell, "sellPrice"],
      ["buy", H.priceBuy, "buy"],
    ] as const) {
      if (!shown_(series)) {
        continue;
      }
      const points = hours.flatMap((hour) => {
        const value = hour[key];
        if (value === null) {
          return [null];
        }
        const y = yAt(scale, value);
        return [{ x: x(hour.startMs), y }, { x: x(hour.startMs + HOUR_MS), y }];
      });
      root.append(svg("path", { class: className, d: pathOf(points, false, base) }));
    }
  }
  const sunPoints = hours.map((hour) => {
    const value = sunShown(hour, nowMs);
    return value === null ? null : { x: centre(hour), y: yAt(scale, value) };
  });
  if (view === "energy" && shown_("sun") && sunPoints.some((point) => point !== null && point.y < base - 0.5)) {
    const area = svg("path", { class: H.sunArea, d: pathOf(sunPoints, true, base) });
    area.style.setProperty("fill", sunFill);
    root.append(area);
    // Only where the sun is up (and the hour either side, so the line starts and ends on the baseline).
    const up = (index: number): boolean => (hours[index]?.sun_forecast_kwh ?? 0) > 0;
    const raw = hours.map((hour, index) =>
      hour.sun_forecast_kwh === null || !(up(index - 1) || up(index) || up(index + 1))
        ? null
        : { x: centre(hour), y: yAt(scale, hour.sun_forecast_kwh) },
    );
    root.append(svg("path", { class: H.sunRaw, d: pathOf(raw, false, base) }));
  }

  // The house: a bar per hour, and over a measured bar the first forecast as a short line.
  const bars = svg("g", { class: H.bars });
  const carOn = view === "energy" && shown_("car");
  for (const hour of view === "energy" ? hours : []) {
    const shown = houseShown(hour);
    const left = x(hour.startMs);
    const width = x(hour.startMs + HOUR_MS) - left;
    const inset = width * 0.18;
    // With the car's bar on, the house's takes the left part of the hour and the car's the right.
    const room = Math.max(0.5, width - 2 * inset);
    const houseWidth = carOn ? room * 0.62 : room;
    if (shown.kw !== null && shown.kw > 0) {
      const top = yAt(scale, shown.kw);
      if (shown_("house")) {
        const bar = svg("rect", {
          class: shown.measured ? H.bar : `${H.bar} ${H.barForecast}`,
          x: left + inset,
          y: top,
          width: Math.max(0.5, houseWidth),
          height: Math.max(0.5, base - top),
          rx: Math.min(2, width / 6),
        });
        bar.style.setProperty("fill", barFill);
        bars.append(bar);
      }
      const heat = shown_("heat") ? heatShown(hour) : null;
      if (heat !== null && heat > 0) {
        const heatTop = yAt(scale, heat);
        bars.append(
          svg("rect", {
            class: shown.measured ? H.heatBar : `${H.heatBar} ${H.heatForecast}`,
            x: left + inset,
            y: heatTop,
            width: Math.max(0.5, houseWidth),
            height: Math.max(0.5, base - heatTop),
          }),
        );
      }
    }
    if (shown_("forecast") && shown.measured && hour.house_forecast_kw !== null) {
      const y = yAt(scale, hour.house_forecast_kw);
      const end = carOn ? left + inset + houseWidth + inset * 0.4 : left + width - inset * 0.4;
      bars.append(svg("line", { class: H.forecastTick, x1: left + inset * 0.4, x2: end, y1: y, y2: y }));
    }
    if (carOn && hour.car_kw !== null && hour.car_kw > 0) {
      const top = yAt(scale, hour.car_kw);
      const gap = room * 0.06;
      bars.append(
        svg("rect", {
          class: H.carBar,
          "data-series": "car",
          x: left + inset + houseWidth + gap,
          y: top,
          width: Math.max(0.5, room - houseWidth - gap),
          height: Math.max(0.5, base - top),
          rx: Math.min(2, width / 6),
        }),
      );
    }
  }
  root.append(bars);

  // The battery: measured, then planned (from the last measured point on, so the two lines meet).
  const hasSoc = hours.some((hour) => hour.soc_pct !== null || (hour.soc_plan_pct !== null && hour.startMs + HOUR_MS > nowMs));
  if (hasSoc && shown_("battery")) {
    const end = (hour: HouseHour): number => x(hour.startMs + HOUR_MS);
    const measured = hours.map((hour) => (hour.soc_pct === null ? null : { x: end(hour), y: socY(hour.soc_pct) }));
    let lastMeasured: { x: number; y: number } | null = null;
    for (const point of measured) {
      if (point !== null) {
        lastMeasured = point;
      }
    }
    const planned: Array<{ x: number; y: number } | null> = [];
    if (lastMeasured !== null) {
      planned.push(lastMeasured);
    }
    for (const hour of hours) {
      if (hour.startMs + HOUR_MS > nowMs && hour.soc_plan_pct !== null) {
        planned.push({ x: end(hour), y: socY(hour.soc_plan_pct) });
      }
    }
    root.append(svg("path", { class: H.soc, d: pathOf(measured, false, base) }));
    root.append(svg("path", { class: `${H.soc} ${H.socPlan}`, d: pathOf(planned, false, base) }));
  }

  const now = svg("line", { class: C.now, x1: x(nowMs), x2: x(nowMs), y1: scale.top, y2: scale.bottom });
  const nowLabel = svg("text", { class: `${C.tickLabel} ${H.nowLabel}`, x: x(nowMs) + 3, y: scale.top + scale.axisTextSize * 0.9, "text-anchor": "start" });
  nowLabel.textContent = labels.now;
  if (nowMs < startMs || nowMs > endMs) {
    now.setAttribute("display", "none");
    nowLabel.setAttribute("display", "none");
  }
  const selection = svg("line", { class: C.selection, x1: 0, x2: 0, y1: scale.top, y2: scale.bottom, display: "none" });
  root.append(now, nowLabel, selection);

  return {
    element: root,
    scale,
    hourAt(pointX: number): number | null {
      if (pointX < scale.left - 4 || pointX > scale.right + 4) {
        return null;
      }
      const share = (pointX - scale.left) / Math.max(1, scale.right - scale.left);
      return Math.min(count - 1, Math.max(0, Math.floor(share * count)));
    },
    select(index: number | null): void {
      const hour = index === null ? undefined : hours[index];
      if (hour === undefined) {
        selection.setAttribute("display", "none");
        return;
      }
      const at = centre(hour);
      selection.setAttribute("x1", String(at));
      selection.setAttribute("x2", String(at));
      selection.removeAttribute("display");
    },
  };
}
