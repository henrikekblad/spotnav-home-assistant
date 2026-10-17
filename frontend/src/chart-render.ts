// The graph as SVG, drawn from the accepted geometry only. The scale comes from `chartScale`, every
// x from `xForWallClock`, every y from `yAt` inside `hitTargets`, and the returned `targets` are
// the ones drawn, so interaction can never hit-test a different chart. Positions come from the
// series' time mapping (`ChartSeries.time`): the wall-clock overlay, where today and tomorrow share
// one 24-hour axis, or the real-time range, where a midnight inside is drawn as a day divider and
// labelled with the new day. Everything is created with `createElementNS` and `textContent` /
// `setAttribute`; no backend code is drawn.
//
// Paint order equals DOM order (SVG has no z-index): metadata, band patterns, bands, grid and axes,
// now and selection lines, then marks with tomorrow first so today paints above it.

import {
  chartScale,
  gutterLabels,
  hitTargets,
  xForWallClock,
  type ChartHitTarget,
  type ChartMark,
  type ChartNow,
  type ChartScale,
  type ChartSeries,
  type PlanBand,
  type PlanBands,
} from "./chart";
import { CHART_DENSITY, PLAN_BAND_STYLE, VISUAL_CLASSES, type ChartDensityStyle } from "./visual-styles";

const SVG_NS = "http://www.w3.org/2000/svg";

/** The localized strings the SVG needs; the renderer formats nothing itself. */
export interface ChartLabels {
  time(instantMs: number): string;
  /** The new day at a midnight inside the range axis (weekday and date); the time label when omitted. */
  day?(instantMs: number): string;
  /** One price-axis label from a value in the minor unit and the axis's span; whole minor units when omitted. */
  price?(value: number, span: number): string;
  /** An hour label ("00" to "23") for the `chart_detail` axis; the time label when omitted. */
  hour?(instantMs: number): string;
}

export interface ChartRenderInput {
  series: ChartSeries;
  bands: PlanBands;
  /**
   * The injected now fact: the current interval and the identity that stands for it. Equal identity
   * draws the same focus; the now line moves when the containing published interval changes.
   */
  now: ChartNow;
  width: number;
  height: number;
  selected: ChartMark | null;
  title: string;
  description: string;
  labels: ChartLabels;
  /** How dense the chart is drawn; the ordinary card when omitted, `CHART_DENSITY.detail` for `chart_detail`. */
  density?: ChartDensityStyle;
  /**
   * Document-unique ids. `hatch` prefixes the band patterns' ids (`<hatch>-installed`,
   * `<hatch>-proposal`), so several cards on one page never reference each other's pattern.
   */
  ids: { title: string; description: string; hatch: string };
}

export interface ChartRenderResult {
  element: SVGSVGElement;
  /** Exactly the geometry drawn; hit testing must reuse it. */
  targets: ChartHitTarget[];
  scale: ChartScale;
  nowLine: SVGLineElement;
  selectionLine: SVGLineElement;
}

function svg<K extends keyof SVGElementTagNameMap>(
  name: K,
  attributes: Record<string, string | number> = {},
): SVGElementTagNameMap[K] {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    element.setAttribute(key, String(value));
  }
  return element;
}

/**
 * The chart height for a viewport width: bounded (120-190 CSS px for the ordinary card) and proportional
 * to the width, so the geometry never depends on content height such as a longer readout.
 */
export function chartHeightForWidth(width: number, density: ChartDensityStyle = CHART_DENSITY.normal): number {
  const bounded = Number.isFinite(width) && width > 0 ? width : 320;
  return Math.round(Math.min(density.maxHeight, Math.max(density.minHeight, bounded * density.heightRatio)));
}

/**
 * The classes one mark carries: cheap/expensive against its own day's mean (equal to the mean is
 * cheap), plus tomorrow, estimated and current treatments.
 */
export function markClassNames(target: ChartHitTarget, series: ChartSeries): string[] {
  const day = series.days[target.mark.dayIndex];
  const mean = day?.mean ?? null;
  const cheap = mean === null ? true : target.mark.price <= mean;
  const classes: string[] = [cheap ? VISUAL_CLASSES.cheap : VISUAL_CLASSES.expensive];
  if (target.mark.tomorrow) {
    classes.push(VISUAL_CLASSES.tomorrow);
  }
  if (target.current) {
    classes.push(VISUAL_CLASSES.current);
  }
  return classes;
}

type BandKind = "installed" | "proposal";

/**
 * The hatch another day's runs are filled with: one line per tile in the band colour (set by the
 * stylesheet, so it follows the theme), rotated to the shared angle. The line sits mid-tile so the
 * tile edge never clips it.
 */
function hatchPattern(id: string, kind: BandKind): SVGPatternElement {
  const { hatchSpacing, hatchWidth } = PLAN_BAND_STYLE[kind];
  const pattern = svg("pattern", {
    id,
    patternUnits: "userSpaceOnUse",
    width: hatchSpacing,
    height: hatchSpacing,
    patternTransform: `rotate(${PLAN_BAND_STYLE.hatchAngle})`,
  });
  pattern.append(
    svg("line", {
      class: VISUAL_CLASSES.bandHatch,
      "data-kind": kind,
      x1: hatchSpacing / 2,
      x2: hatchSpacing / 2,
      y1: 0,
      y2: hatchSpacing,
      "stroke-width": hatchWidth,
    }),
  );
  return pattern;
}

/**
 * One shaded span on the shared wall-clock axis. `data-day-index`/`data-day-key` keep the date
 * the run belongs to, which the geometry alone cannot say. Today's run is a solid tint from the
 * stylesheet; another day's is filled with that kind's hatch pattern.
 */
function bandRect(
  band: PlanBand,
  scale: ChartScale,
  kind: BandKind,
  hatchId: string,
): SVGRectElement {
  const startX = xForWallClock(scale, band.startPosition);
  const endX = xForWallClock(scale, band.endPosition);
  const className = kind === "installed" ? VISUAL_CLASSES.bandInstalled : VISUAL_CLASSES.bandProposal;
  return svg("rect", {
    class: band.tomorrow ? `${className} ${VISUAL_CLASSES.bandTomorrow}` : className,
    x: startX,
    y: scale.top,
    width: Math.max(0, endX - startX),
    height: Math.max(0, scale.bottom - scale.top),
    "data-day-index": band.dayIndex,
    "data-day-key": band.dayKey,
    ...(band.tomorrow ? { fill: `url(#${hatchId})` } : {}),
  });
}

export function renderChart(input: ChartRenderInput): ChartRenderResult {
  const { series, bands, labels, ids } = input;
  const density = input.density ?? CHART_DENSITY.normal;
  // A detailed range axis names a new day on a second line, under that midnight's hour label.
  const dayLine = density.hourLabels && series.axis.some((tick) => tick.dayStart === true);
  const scale = chartScale(
    input.width,
    input.height,
    series.marks.map((mark) => mark.price),
    density,
    dayLine ? 2 : 1,
  );
  const root = svg("svg", {
    class: VISUAL_CLASSES.svg,
    ...(density === CHART_DENSITY.detail ? { "data-density": "detail" } : {}),
    viewBox: `0 0 ${scale.width} ${scale.height}`,
    preserveAspectRatio: "none",
    "aria-hidden": "true",
    focusable: "false",
  });
  const title = svg("title", { id: ids.title });
  title.textContent = input.title;
  const description = svg("desc", { id: ids.description });
  description.textContent = input.description;
  root.append(title, description);

  // Shading: installed is authoritative, proposal only where nothing is installed. A kind's hatch
  // pattern is defined only when one of its runs is on another day.
  const bandLayer = svg("g", { class: VISUAL_CLASSES.bands });
  const defs = svg("defs");
  for (const [kind, list] of [
    ["installed", bands.installed],
    ["proposal", bands.proposal],
  ] as const) {
    const hatchId = `${ids.hatch}-${kind}`;
    if (list.some((band) => band.tomorrow)) {
      defs.append(hatchPattern(hatchId, kind));
    }
    for (const band of list) {
      bandLayer.append(bandRect(band, scale, kind, hatchId));
    }
  }
  if (defs.childElementCount > 0) {
    root.append(defs);
  }
  root.append(bandLayer);

  const axis = svg("g", { class: VISUAL_CLASSES.axis });
  for (const label of gutterLabels(scale, labels.price)) {
    axis.append(
      svg("line", {
        class: VISUAL_CLASSES.gridline,
        x1: scale.left,
        x2: scale.right,
        y1: label.y,
        y2: label.y,
      }),
    );
    const text = svg("text", {
      class: VISUAL_CLASSES.axisLabel,
      x: scale.pad,
      y: label.y + scale.axisTextSize * 0.35,
      "text-anchor": "start",
    });
    text.textContent = label.label;
    axis.append(text);
  }
  // Gridlines (and the day divider) stay at the six-hour ticks; the detailed axis labels every hour.
  for (const tick of series.axis) {
    const x = xForWallClock(scale, tick.position);
    const dayStart = tick.dayStart === true;
    axis.append(
      svg("line", {
        class: dayStart ? `${VISUAL_CLASSES.gridline} ${VISUAL_CLASSES.dayDivider}` : VISUAL_CLASSES.gridline,
        x1: x,
        x2: x,
        y1: scale.top,
        y2: scale.bottom,
      }),
    );
    if (density.hourLabels) {
      continue;
    }
    const text = svg("text", {
      class: dayStart ? `${VISUAL_CLASSES.tickLabel} ${VISUAL_CLASSES.dayLabel}` : VISUAL_CLASSES.tickLabel,
      x,
      y: scale.bottom + scale.axisTextSize,
      "text-anchor": tick.position === 0 ? "start" : tick.position === 1 ? "end" : "middle",
    });
    text.textContent = dayStart && labels.day !== undefined ? labels.day(tick.instantMs) : labels.time(tick.instantMs);
    axis.append(text);
  }
  if (density.hourLabels) {
    for (const tick of series.hours) {
      const x = xForWallClock(scale, tick.position);
      const dayStart = tick.dayStart === true;
      const text = svg("text", {
        class: VISUAL_CLASSES.tickLabel,
        x,
        y: scale.bottom + scale.axisTextSize,
        // Centred under its hour, the left edge's too: two-digit labels fit in the gutter beside it.
        "text-anchor": "middle",
      });
      text.textContent = (labels.hour ?? labels.time)(tick.instantMs);
      axis.append(text);
      if (dayStart && labels.day !== undefined) {
        const cue = svg("text", {
          class: `${VISUAL_CLASSES.tickLabel} ${VISUAL_CLASSES.dayLabel}`,
          x,
          y: scale.bottom + scale.axisTextSize * 2.2,
          "text-anchor": "middle",
        });
        cue.textContent = labels.day(tick.instantMs);
        axis.append(cue);
      }
    }
  }
  root.append(axis);
  // Focus lines come before every mark in the DOM so they sit behind them; `applyFocus` decides
  // which one shows.
  const nowLine = svg("line", {
    class: VISUAL_CLASSES.now,
    x1: scale.left,
    x2: scale.left,
    y1: scale.top,
    y2: scale.bottom,
    ...(input.now.identity === null ? {} : { "data-now-identity": input.now.identity }),
    // SVG has no `hidden` semantics, so absence is the `display` attribute.
    display: "none",
  });
  const selectionLine = svg("line", {
    class: VISUAL_CLASSES.selection,
    x1: scale.left,
    x2: scale.left,
    y1: scale.top,
    y2: scale.bottom,
    display: "none",
  });
  root.append(nowLine, selectionLine);

  // Ordinary ink: quarter-hours are filled circles, hours short centred segments. Tomorrow paints
  // first; the current mark is drawn like its neighbours and the now line marks it.
  const targets = hitTargets(series, scale, input.now.mark);
  const markLayer = svg("g", { class: VISUAL_CLASSES.marks });
  const selectedMs = input.selected?.startMs ?? null;
  const paintOrder = [
    ...targets.filter((target) => target.mark.tomorrow),
    ...targets.filter((target) => !target.mark.tomorrow),
  ];
  for (const target of paintOrder) {
    const classes = markClassNames(target, series);
    const shared = {
      class: classes.join(" "),
      "data-day-index": target.mark.dayIndex,
      "data-start-ms": target.mark.startMs,
      "data-selected": target.mark.startMs === selectedMs ? "true" : "false",
      ...(target.current ? { "data-current": "true" } : {}),
    };
    if (target.mark.hourly) {
      markLayer.append(
        svg("line", {
          ...shared,
          class: `${VISUAL_CLASSES.segment} ${shared.class}`,
          x1: target.x - target.halfLength,
          x2: target.x + target.halfLength,
          y1: target.y,
          y2: target.y,
          "stroke-width": Math.max(1, target.halfThickness * 2),
        }),
      );
      continue;
    }
    markLayer.append(
      svg("circle", {
        ...shared,
        class: `${VISUAL_CLASSES.point} ${shared.class}`,
        cx: target.x,
        cy: target.y,
        r: target.radius,
      }),
    );
  }
  root.append(markLayer);

  const result: ChartRenderResult = { element: root, targets, scale, nowLine, selectionLine };
  applyFocus(result, selectedMs);
  return result;
}

/**
 * Move the one focus line from the drawn targets. With an interval selected the now line is hidden
 * and one dashed selection line sits at that record's x; with none, the solid now line sits at the
 * current record's x (the same x hit testing and the readout use). Records sharing an x are found
 * by instant, never by coordinate.
 */
export function applyFocus(drawn: ChartRenderResult, selectedMs: number | null): void {
  const { nowLine, selectionLine, targets } = drawn;
  const selected =
    selectedMs === null ? undefined : targets.find((entry) => entry.mark.startMs === selectedMs);
  if (selected !== undefined) {
    nowLine.setAttribute("display", "none");
    selectionLine.setAttribute("x1", String(selected.x));
    selectionLine.setAttribute("x2", String(selected.x));
    selectionLine.removeAttribute("display");
    return;
  }
  selectionLine.setAttribute("display", "none");
  const current = targets.find((entry) => entry.current);
  if (current === undefined) {
    nowLine.setAttribute("display", "none");
    return;
  }
  nowLine.setAttribute("x1", String(current.x));
  nowLine.setAttribute("x2", String(current.x));
  nowLine.removeAttribute("display");
}

/**
 * A client coordinate in the viewBox's units, using the size the caller rendered with so an
 * unlaid-out element (jsdom, hidden card) maps deterministically.
 */
export function chartLocalPoint(
  element: SVGSVGElement,
  scale: ChartScale,
  clientX: number,
  clientY: number,
): { x: number; y: number } {
  const rect = element.getBoundingClientRect();
  const width = rect.width === 0 ? scale.width : rect.width;
  const height = rect.height === 0 ? scale.height : rect.height;
  return {
    x: ((clientX - rect.left) / width) * scale.width,
    y: ((clientY - rect.top) / height) * scale.height,
  };
}
