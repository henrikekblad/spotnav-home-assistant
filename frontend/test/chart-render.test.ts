// The SVG renderer: the accepted geometry, drawn, with no second geometry anywhere.

import { describe, expect, it } from "vitest";

import { chartNowAt, hitTargets, hourMarkers, intervalIdentity, xForWallClock } from "../src/chart";
import {
  applyFocus,
  chartHeightForWidth,
  chartLocalPoint,
  markClassNames,
} from "../src/chart-render";
import { buildModel } from "../src/model";
import { decodeDashboard } from "../src/validate";
import { rawDashboard } from "./dashboard-fixtures";
import {
  baseModel,
  crossClientModel,
  CROSS_CURRENT_FRACTION,
  CROSS_CURRENT_START,
  CROSS_NOW,
  CROSS_SELECTED_FRACTION,
  CROSS_SELECTED_START,
  CROSS_TODAY,
  CROSS_TOMORROW,
  dashboardWithRows,
  hourlyModel,
  MIDNIGHT_MS,
  midnightModel,
  NOW,
  quarterModel,
  renderFor,
  twoDayModel,
} from "./visual-fixtures";

const SVG_NS = "http://www.w3.org/2000/svg";

function circles(svg: SVGSVGElement): SVGCircleElement[] {
  return Array.from(svg.querySelectorAll("circle"));
}

function byStartMs(svg: SVGSVGElement, startMs: number): Element | null {
  return svg.querySelector(`[data-start-ms="${startMs}"]`);
}

describe("the drawn marks are the accepted geometry", () => {
  it("draws every hit target at exactly the coordinates hit testing will use", () => {
    const model = baseModel();
    const result = renderFor(model);
    const expected = hitTargets(model.chart, result.scale, model.chart.current);
    expect(result.targets).toHaveLength(expected.length);
    expect(result.targets).toEqual(expected);

    for (const target of result.targets) {
      const node = byStartMs(result.element, target.mark.startMs);
      expect(node, `mark ${target.mark.startMs}`).not.toBeNull();
      if (target.mark.hourly) {
        expect(Number(node?.getAttribute("x1"))).toBeCloseTo(target.x - target.halfLength, 10);
        expect(Number(node?.getAttribute("x2"))).toBeCloseTo(target.x + target.halfLength, 10);
        expect(Number(node?.getAttribute("y1"))).toBeCloseTo(target.y, 10);
        expect(Number(node?.getAttribute("stroke-width"))).toBeCloseTo(
          Math.max(1, target.halfThickness * 2),
          10,
        );
      } else {
        expect(Number(node?.getAttribute("cx"))).toBeCloseTo(target.x, 10);
        expect(Number(node?.getAttribute("cy"))).toBeCloseTo(target.y, 10);
        expect(Number(node?.getAttribute("r"))).toBeCloseTo(target.radius, 10);
      }
    }
  });


  it("draws a 15-minute value as a filled circle and a 60-minute value as a centred segment", () => {
    const quarter = renderFor(baseModel());
    expect(quarter.targets.every((target) => !target.mark.hourly)).toBe(true);
    expect(circles(quarter.element).length).toBe(quarter.targets.length);
    expect(quarter.element.querySelectorAll("line.spotnav-segment").length).toBe(0);
    expect(Number(circles(quarter.element)[0]?.getAttribute("r"))).toBeGreaterThan(0);

    const hourly = renderFor(hourlyModel());
    expect(hourly.targets.every((target) => target.mark.hourly)).toBe(true);
    const segments = hourly.element.querySelectorAll("line.spotnav-segment");
    expect(segments.length).toBe(hourly.targets.length);
    const first = hourly.targets[0];
    expect(first?.halfLength).toBeGreaterThan(0);
    // The half length is the mark's own duration against its own day's real length.
    expect(Number(segments[0]?.getAttribute("x2")) - Number(segments[0]?.getAttribute("x1"))).toBeCloseTo(
      (first?.halfLength ?? 0) * 2,
      10,
    );
    expect(hourly.element.querySelectorAll("circle.spotnav-point").length).toBe(0);
  });
});

describe("colour, tomorrow and estimates", () => {
  it("colours each mark against its own day's mean, with the accepted tie rule", () => {
    const model = baseModel();
    const result = renderFor(model);
    const plain = hitTargets(model.chart, result.scale, model.chart.current).filter((target) => !target.current);
    for (const target of plain) {
      const classes = markClassNames(target, model.chart).join(" ");
      const day = model.chart.days[target.mark.dayIndex];
      const cheap = (day?.mean ?? target.mark.price) >= target.mark.price;
      expect(classes.includes("spotnav-cheap"), `${target.mark.startMs}`).toBe(cheap);
      expect(classes.includes("spotnav-expensive")).toBe(!cheap);
    }
    // A mark exactly on the mean is cheap, never uniformly expensive.
    const flat = renderFor(quarterModel(() => ({}), [100, 100, 100, 100]));
    expect(flat.element.querySelectorAll(".spotnav-expensive").length).toBe(0);
  });

  it("keeps tomorrow's semantics but gives them a quieter treatment", () => {
    const model = twoDayModel();
    const result = renderFor(model);
    expect(model.chart.days).toHaveLength(2);
    const tomorrow = Array.from(result.element.querySelectorAll(".spotnav-tomorrow"));
    const dayIndexes = tomorrow.map((node) => node.getAttribute("data-day-index"));
    expect(tomorrow.length).toBeGreaterThan(0);
    expect(new Set(dayIndexes)).toEqual(new Set(["1"]));
    // Today's own marks carry the same cheap/expensive classes and no tomorrow class.
    expect(result.element.querySelectorAll('[data-day-index="0"].spotnav-tomorrow').length).toBe(0);
    expect(result.element.querySelectorAll('[data-day-index="0"].spotnav-cheap, [data-day-index="0"].spotnav-expensive').length).toBeGreaterThan(0);
  });
});

describe("bands, gutter and paint order", () => {
  it("shades only what is installed when both flags cover a run, and splits by day", () => {
    const model = quarterModel((index) => (index < 2 ? { proposal_planned: true, installed_planned: true } : { proposal_planned: true }));
    expect(model.bands.installed).toHaveLength(1);
    expect(model.bands.proposal).toHaveLength(1);
    const result = renderFor(model);
    const installed = result.element.querySelectorAll(".spotnav-band-installed");
    const proposal = result.element.querySelectorAll(".spotnav-band-proposal");
    expect(installed.length).toBe(1);
    expect(proposal.length).toBe(1);
    // The proposal run is the two later rows only: it never darkens what is already installed.
    const proposalWidth = Number(proposal[0]?.getAttribute("width"));
    const installedWidth = Number(installed[0]?.getAttribute("width"));
    expect(proposalWidth).toBeGreaterThan(0);
    expect(proposalWidth).toBeCloseTo(installedWidth, 10);
    expect(Number(proposal[0]?.getAttribute("x"))).toBeGreaterThan(Number(installed[0]?.getAttribute("x")));
  });

  it("splits a run that crosses local midnight into one band per day panel", () => {
    const today = dashboardWithRows("2026-09-22T21:45:00+00:00", [100], {
      day: "2026-09-22",
      flags: () => ({ installed_planned: true }),
    });
    const tomorrow = dashboardWithRows("2026-09-22T22:00:00+00:00", [100], {
      day: "2026-09-23",
      flags: () => ({ installed_planned: true }),
    });
    const rows = [...today.prices.intervals, ...tomorrow.prices.intervals];
    const payload = rawDashboard();
    (payload.prices as Record<string, unknown>).intervals = rows.map((row) => ({
      start: new Date(row.startMs).toISOString(),
      end: new Date(row.endMs).toISOString(),
      day: row.day,
      duration_minutes: row.duration_minutes,
      raw_price: row.effective_price,
      fallback_price: null,
      effective_price: row.effective_price,
      known: true,
      estimated: false,
      basis_day: null,
      proposal_planned: false,
      installed_planned: true,
    }));
    const decoded = decodeDashboard(payload);
    expect(decoded.ok).toBe(true);
    if (!decoded.ok) {
      return;
    }
    const model = buildModel({ dashboard: decoded.value, language: "en", nowMs: Date.parse("2026-09-22T22:30:00Z") });
    expect(model.bands.installed).toHaveLength(2);
    expect(model.bands.installed.map((band) => band.dayIndex)).toEqual([0, 1]);
    expect(model.bands.installed.map((band) => band.dayKey)).toEqual(["2026-09-22", "2026-09-23"]);
    // A run never crosses local midnight: today's last quarter ends at 24:00 and tomorrow's first
    // begins at 00:00, and on the one shared axis those are the right and left edges of the plot.
    expect(model.bands.installed[0]?.startPosition).toBeCloseTo(23.75 / 24, 12);
    expect(model.bands.installed[0]?.endPosition).toBeCloseTo(1, 12);
    expect(model.bands.installed[1]?.startPosition).toBeCloseTo(0, 12);
    expect(model.bands.installed[1]?.endPosition).toBeCloseTo(0.25 / 24, 12);
    const result = renderFor(model);
    const rects = Array.from(result.element.querySelectorAll(".spotnav-band-installed"));
    expect(rects.length).toBe(2);
    expect(Number(rects[0]?.getAttribute("x"))).toBeGreaterThan(Number(rects[1]?.getAttribute("x")));
    expect(rects.map((rect) => rect.getAttribute("data-day-key"))).toEqual([
      "2026-09-22",
      "2026-09-23",
    ]);
  });

  it("puts the value labels in the true left gutter, negative values included", () => {
    const model = quarterModel(() => ({}), [-50, 0, 100, -25]);
    const result = renderFor(model);
    const labels = Array.from(result.element.querySelectorAll(".spotnav-axis-label"));
    expect(labels.length).toBeGreaterThan(1);
    for (const label of labels) {
      expect(Number(label.getAttribute("x"))).toBeLessThan(result.scale.left);
      expect(label.getAttribute("text-anchor")).toBe("start");
    }
    const texts = labels.map((label) => label.textContent);
    expect(texts.some((text) => text !== null && text.startsWith("-"))).toBe(true);
    // Every drawn mark stays inside the plot, so no label can sit under ink.
    for (const target of result.targets) {
      expect(target.y).toBeGreaterThanOrEqual(result.scale.top - 1e-9);
      expect(target.y).toBeLessThanOrEqual(result.scale.bottom + 1e-9);
    }
  });

  it("draws the focus lines before every mark, so neither can paint over one", () => {
    const model = baseModel();
    const result = renderFor(model, { selected: model.chart.marks[1] ?? null });
    const children = Array.from(result.element.children);
    const selection = children.indexOf(result.selectionLine);
    const now = children.indexOf(result.nowLine);
    const marks = children.findIndex((child) => child.classList.contains("spotnav-marks"));
    expect(now).toBeGreaterThanOrEqual(0);
    expect(selection).toBeGreaterThan(now);
    expect(marks).toBeGreaterThan(selection);
    const bands = children.findIndex((child) => child.classList.contains("spotnav-bands"));
    const axis = children.findIndex((child) => child.classList.contains("spotnav-axis"));
    expect(bands).toBeGreaterThanOrEqual(0);
    expect(bands).toBeLessThan(axis);
    expect(axis).toBeLessThan(now);
    // Hidden by the `display` attribute (SVG has no `hidden` semantics), and one line at a time.
    expect(result.nowLine.getAttribute("class")).toBe("spotnav-now");
    expect(result.selectionLine.getAttribute("class")).toBe("spotnav-selection");
    applyFocus(result, model.chart.marks[1]?.startMs ?? null);
    const target = result.targets[1];
    expect(Number(result.selectionLine.getAttribute("x1"))).toBeCloseTo(target?.x ?? -1, 10);
    expect(result.selectionLine.hasAttribute("display")).toBe(false);
    expect(result.nowLine.getAttribute("display")).toBe("none");
    applyFocus(result, null);
    expect(result.selectionLine.getAttribute("display")).toBe("none");
  });
});

describe("the one focus line, drawn", () => {
  it("draws the solid now line at the current quarter-hour mark's own x", () => {
    const model = baseModel();
    const result = renderFor(model);
    const current = result.targets.find((target) => target.current);
    expect(current).toBeDefined();
    expect(result.nowLine.hasAttribute("display")).toBe(false);
    expect(result.selectionLine.getAttribute("display")).toBe("none");
    expect(Number(result.nowLine.getAttribute("x1"))).toBeCloseTo(current?.x ?? -1, 10);
    expect(Number(result.nowLine.getAttribute("x2"))).toBeCloseTo(current?.x ?? -1, 10);
    // The wall-clock fraction the whole chart agrees on, not a pixel-ish approximation.
    expect(current?.x).toBeCloseTo(xForWallClock(result.scale, (6 * 60 + 30) / 1440), 9);
    expect(result.nowLine.getAttribute("data-now-identity")).toBe(
      `${current?.mark.dayKey}@${current?.mark.startMs}`,
    );
  });

  it("draws an hourly now line at the rendered segment's own centre", () => {
    const model = hourlyModel();
    const result = renderFor(model, { nowMs: NOW });
    const current = result.targets.find((target) => target.current);
    expect(current?.mark.hourly).toBe(true);
    expect(current?.mark.wallClock).toBe("06:30");
    const segment = result.element.querySelector('line.spotnav-segment[data-current="true"]');
    const centre = (Number(segment?.getAttribute("x1")) + Number(segment?.getAttribute("x2"))) / 2;
    expect(centre).toBeCloseTo(current?.x ?? -1, 10);
    expect(Number(result.nowLine.getAttribute("x1"))).toBeCloseTo(centre, 10);
    expect(current?.x).toBeCloseTo(xForWallClock(result.scale, (6 * 60 + 30) / 1440), 9);
  });

  it("hides the now line and shows one dashed selection line while a mark is selected", () => {
    const model = baseModel();
    const selected = model.chart.marks[3] ?? null;
    expect(selected?.startMs).not.toBe(model.chart.current?.startMs);
    const result = renderFor(model, { selected });
    expect(result.nowLine.getAttribute("display")).toBe("none");
    expect(result.selectionLine.hasAttribute("display")).toBe(false);
    expect(Number(result.selectionLine.getAttribute("x1"))).toBeCloseTo(
      result.targets[3]?.x ?? -1,
      10,
    );
    // Selecting the current mark itself is still exactly one dashed line.
    applyFocus(result, model.chart.current?.startMs ?? null);
    expect(result.nowLine.getAttribute("display")).toBe("none");
    expect(result.selectionLine.hasAttribute("display")).toBe(false);
    expect(Number(result.selectionLine.getAttribute("x1"))).toBeCloseTo(
      result.targets.find((target) => target.current)?.x ?? -1,
      10,
    );
    // Clearing restores the now line and hides the selection line.
    applyFocus(result, null);
    expect(result.nowLine.hasAttribute("display")).toBe(false);
    expect(result.selectionLine.getAttribute("display")).toBe("none");
  });

  it("draws no line at all, and invents no current mark, when no interval contains now", () => {
    const model = baseModel();
    const result = renderFor(model, { nowMs: Date.parse("2026-09-22T12:00:00Z") });
    expect(result.targets.some((target) => target.current)).toBe(false);
    expect(result.element.querySelector('[data-current="true"]')).toBeNull();
    expect(result.nowLine.getAttribute("display")).toBe("none");
    expect(result.selectionLine.getAttribute("display")).toBe("none");
    expect(result.element.querySelector("[data-now-identity]")).toBeNull();
  });

  it("paints tomorrow under today where the two days share an x", () => {
    const model = twoDayModel();
    const result = renderFor(model);
    const nodes = Array.from(result.element.querySelectorAll(".spotnav-marks > *"));
    const dayIndexes = nodes.map((node) => node.getAttribute("data-day-index"));
    // Every tomorrow mark is appended before the first of today's, so today wins a coincidence.
    expect(dayIndexes.lastIndexOf("1")).toBeLessThan(dayIndexes.indexOf("0"));
  });
});

describe("the rollover in the drawn picture", () => {
  it("draws the date the clock is in as today's series, and the one it has left as the subdued series", () => {
    const model = midnightModel();
    const result = renderFor(model);
    const nodes = Array.from(result.element.querySelectorAll(".spotnav-marks > *"));
    const rows = nodes.map((node) => ({
      startMs: Number(node.getAttribute("data-start-ms")),
      subdued: (node.getAttribute("class") ?? "").includes("spotnav-tomorrow"),
    }));
    // Every row of the date the clock is in is an ordinary mark; the date it has left is the subdued one,
    // and that one is painted first, so the current day's ink wins wherever the two share an x.
    expect(rows.map((row) => row.subdued)).toEqual([true, false, false, false, false]);
    expect(rows.map((row) => row.startMs)).toEqual([
      Date.parse("2026-09-22T21:45:00Z"),
      Date.parse("2026-09-22T22:00:00Z"),
      Date.parse("2026-09-22T22:15:00Z"),
      Date.parse("2026-09-22T22:30:00Z"),
      Date.parse("2026-09-22T22:45:00Z"),
    ]);
    // The one current mark is the post-midnight interval the production card called `unknown`, and the
    // now line sits on that very target's own x.
    const current = result.targets.filter((target) => target.current);
    expect(current).toHaveLength(1);
    expect(current[0]?.mark.startMs).toBe(Date.parse("2026-09-22T22:00:00Z"));
    expect(result.nowLine.hasAttribute("display")).toBe(false);
    expect(Number(result.nowLine.getAttribute("x1"))).toBeCloseTo(current[0]?.x ?? -1, 9);
    expect(result.nowLine.getAttribute("data-now-identity")).toBe(
      intervalIdentity(current[0]?.mark ?? null),
    );
  });

  it("draws no line and invents no current mark when every accepted date is behind the clock", () => {
    const model = midnightModel(MIDNIGHT_MS + 26 * 60 * 60_000);
    const result = renderFor(model, { now: chartNowAt(model.chart.marks, MIDNIGHT_MS + 26 * 60 * 60_000) });
    expect(result.targets.some((target) => target.current)).toBe(false);
    expect(result.nowLine.getAttribute("display")).toBe("none");
    expect(result.element.querySelector("[data-now-identity]")).toBeNull();
    // Every drawn mark is the subdued series: no accepted date is the day the clock is in.
    const nodes = Array.from(result.element.querySelectorAll(".spotnav-marks > *"));
    expect(nodes.length).toBeGreaterThan(0);
    expect(nodes.every((node) => (node.getAttribute("class") ?? "").includes("spotnav-tomorrow"))).toBe(true);
  });
});


describe("the cross-client fixture", () => {
  it("agrees with Android and the web planner on the shared facts", () => {
    const model = crossClientModel();
    const now = chartNowAt(model.chart.marks, CROSS_NOW);
    const result = renderFor(model, { now, selected: model.chart.marks[2] ?? null });
    const scale = result.scale;

    // The same wall-clock x fractions, from the same values: 00:15 is 15/1440, 00:30 is 30/1440.
    const current = result.targets.find((target) => target.mark.startMs === CROSS_CURRENT_START);
    const selected = result.targets.find((target) => target.mark.startMs === CROSS_SELECTED_START);
    expect(current?.mark.wallClock).toBe("00:15");
    expect(selected?.mark.wallClock).toBe("00:30");
    expect(current?.x).toBeCloseTo(xForWallClock(scale, CROSS_CURRENT_FRACTION), 9);
    expect(selected?.x).toBeCloseTo(xForWallClock(scale, CROSS_SELECTED_FRACTION), 9);

    // The same focus visibility: a selected interval hides the now line, which was on 00:15.
    expect(result.nowLine.getAttribute("display")).toBe("none");
    expect(result.selectionLine.hasAttribute("display")).toBe(false);
    expect(Number(result.selectionLine.getAttribute("x1"))).toBeCloseTo(selected?.x ?? -1, 9);

    // The same today/tomorrow identity at one x.
    const todayMark = result.targets.find(
      (target) => !target.mark.tomorrow && target.mark.wallClock === "00:15",
    );
    const tomorrowMark = result.targets.find(
      (target) => target.mark.tomorrow && target.mark.wallClock === "00:15",
    );
    expect(todayMark?.x).toBeCloseTo(tomorrowMark?.x ?? -1, 12);
    expect(todayMark?.mark.startMs).not.toBe(tomorrowMark?.mark.startMs);
    expect(todayMark?.mark.dayKey).toBe(CROSS_TODAY);
    expect(tomorrowMark?.mark.dayKey).toBe(CROSS_TOMORROW);
    // With nothing selected, the now line is back on the current interval's own x.
    const unselected = renderFor(model, { now });
    expect(Number(unselected.nowLine.getAttribute("x1"))).toBeCloseTo(
      xForWallClock(scale, CROSS_CURRENT_FRACTION),
      9,
    );
  });

  it("puts the planned span of both days on that same axis, with its own date", () => {
    const model = crossClientModel({ planned: true });
    const bands = model.bands.installed;
    expect(bands).toHaveLength(2);
    const result = renderFor(model, { now: chartNowAt(model.chart.marks, CROSS_NOW) });
    const rects = Array.from(result.element.querySelectorAll(".spotnav-band-installed"));
    expect(rects).toHaveLength(2);
    // 00:15 to 00:45, twice: the same x span for today and for tomorrow...
    expect(Number(rects[0]?.getAttribute("x"))).toBeCloseTo(
      xForWallClock(result.scale, 15 / 1440),
      9,
    );
    expect(Number(rects[0]?.getAttribute("width"))).toBeCloseTo(
      xForWallClock(result.scale, 45 / 1440) - xForWallClock(result.scale, 15 / 1440),
      9,
    );
    expect(Number(rects[1]?.getAttribute("x"))).toBeCloseTo(Number(rects[0]?.getAttribute("x")), 9);
    // ...while each rectangle keeps the date it really belongs to.
    expect(rects.map((rect) => rect.getAttribute("data-day-key"))).toEqual([
      CROSS_TODAY,
      CROSS_TOMORROW,
    ]);
    expect(bands.map((band) => band.startPosition)).toEqual([15 / 1440, 15 / 1440]);
    expect(bands.map((band) => band.endPosition)).toEqual([45 / 1440, 45 / 1440]);
    expect(bands.map((band) => band.dayIndex)).toEqual([0, 1]);
  });
});

describe("accessibility and safety of the SVG", () => {
  it("carries one title/desc pair and no individually focusable mark", () => {
    const result = renderFor(baseModel(), { title: "plan-title", description: "plan-description" });
    const title = result.element.querySelector("title");
    const desc = result.element.querySelector("desc");
    expect(title?.textContent).toBe("plan-title");
    expect(desc?.textContent).toBe("plan-description");
    expect(title?.id).toBe("test-chart-title");
    // The JS-based accessibility path: numbers and booleans only, never a backend code.
    expect(result.element.querySelectorAll("[tabindex]").length).toBe(0);
    expect(result.element.getAttribute("aria-hidden")).toBe("true");
  });

  it("creates every node in the SVG namespace, with no foreignObject and no image", () => {
    const result = renderFor(baseModel());
    const nodes = [result.element, ...Array.from(result.element.querySelectorAll("*"))];
    for (const node of nodes) {
      expect(node.namespaceURI, node.tagName).toBe(SVG_NS);
    }
    expect(result.element.querySelector("foreignObject")).toBeNull();
    expect(result.element.querySelector("image")).toBeNull();
    // No text may be set from markup: hostile text stays text.
    const hostile = renderFor(baseModel(), { title: '<img src=x onerror="boom">', description: "<script>bad()</script>" });
    expect(hostile.element.querySelector("title")?.textContent).toBe('<img src=x onerror="boom">');
    expect(hostile.element.querySelector("script")).toBeNull();
  });

  it("draws the current interval at the ordinary size, marked only by the solid now line", () => {
    // A full local day of quarter-hours, starting at that day's own local midnight (22:00Z).
    const model = buildModel({
      dashboard: dashboardWithRows(
        "2026-09-21T22:00:00+00:00",
        Array.from({ length: 96 }, (_unused, index) => 100 + index),
        { day: "2026-09-22" },
      ),
      language: "en",
      nowMs: Date.parse("2026-09-22T04:30:00Z"),
    });
    const result = renderFor(model, { width: 320, height: 150 });
    const current = result.targets.find((target) => target.current);
    expect(current).toBeDefined();
    if (current === undefined) {
      return;
    }
    // No enlargement anywhere: the current mark is drawn at another quarter-hour's own radius.
    const neighbour = result.targets.find((target) => !target.current && !target.mark.tomorrow);
    expect(current.radius).toBe(neighbour?.radius);
    const drawn = result.element.querySelector('[data-current="true"]');
    expect(drawn?.getAttribute("data-start-ms")).toBe(String(current.mark.startMs));
    expect(Number(drawn?.getAttribute("r"))).toBeCloseTo(current.radius, 10);
    expect(drawn?.getAttribute("stroke-width")).toBeNull();
    // And the one solid now line is what says which interval is current.
    expect(result.nowLine.hasAttribute("display")).toBe(false);
    expect(Number(result.nowLine.getAttribute("x1"))).toBeCloseTo(current.x, 10);
    expect(Number(result.nowLine.getAttribute("y1"))).toBeCloseTo(result.scale.top, 10);
    expect(Number(result.nowLine.getAttribute("y2"))).toBeCloseTo(result.scale.bottom, 10);
    expect(result.selectionLine.getAttribute("display")).toBe("none");
  });

  it("draws one tick sequence for the shared axis, and no per-day heading", () => {
    const model = twoDayModel();
    const result = renderFor(model);
    const ticks = Array.from(result.element.querySelectorAll(".spotnav-tick-label"));
    // Five ticks, not ten: the two days share the clock axis instead of owning a panel each.
    expect(ticks).toHaveLength(hourMarkers().length);
    const xs = ticks.map((tick) => Number(tick.getAttribute("x")));
    const expected = hourMarkers().map((position) => xForWallClock(result.scale, position));
    expected.forEach((x, index) => {
      expect(xs[index]).toBeCloseTo(x, 9);
    });
    // The axis lives in one group and carries no Today/Tomorrow text node at all.
    expect(result.element.querySelectorAll(".spotnav-axis").length).toBe(1);
    const axisText = Array.from(result.element.querySelectorAll(".spotnav-axis text")).map(
      (node) => node.textContent ?? "",
    );
    expect(axisText.some((text) => text === "day-0" || text === "day-1")).toBe(false);
    // Both days' 06:00 marks are drawn at the very same x, and are still two nodes.
    const nodes = Array.from(result.element.querySelectorAll('[data-start-ms]'));
    const today = nodes.find((node) => node.getAttribute("data-day-index") === "0");
    const tomorrow = nodes.find((node) => node.getAttribute("data-day-index") === "1");
    expect(Number(tomorrow?.getAttribute("cx"))).toBeCloseTo(Number(today?.getAttribute("cx")), 9);
    expect(tomorrow?.getAttribute("data-start-ms")).not.toBe(today?.getAttribute("data-start-ms"));
  });

  it("maps a client coordinate into plot units deterministically without layout", () => {
    const result = renderFor(baseModel(), { width: 320, height: 150 });
    const point = chartLocalPoint(result.element, result.scale, 120, 80);
    expect(point.x).toBeCloseTo(120, 10);
    expect(point.y).toBeCloseTo(80, 10);
  });
});

describe("the responsive height policy", () => {
  it("is one pure function of the width, inside the dense range", () => {
    expect(chartHeightForWidth(320)).toBe(120);
    expect(chartHeightForWidth(390)).toBe(120);
    expect(chartHeightForWidth(768)).toBe(190);
    expect(chartHeightForWidth(2000)).toBe(190);
    for (const width of [1, 120, 320, 390, 480, 768, 1280, 4000]) {
      const height = chartHeightForWidth(width);
      expect(height, `${width}`).toBeGreaterThanOrEqual(120);
      expect(height, `${width}`).toBeLessThanOrEqual(190);
    }
    // A nonsensical width still answers with the nominal one, never with NaN.
    expect(chartHeightForWidth(Number.NaN)).toBe(chartHeightForWidth(320));
    expect(chartHeightForWidth(0)).toBe(chartHeightForWidth(320));
  });

  it("drives the viewBox exactly, with no dependency on any content height", () => {
    const width = 390;
    const result = renderFor(baseModel(), { width, height: chartHeightForWidth(width) });
    expect(result.element.getAttribute("viewBox")).toBe(`0 0 ${width} ${chartHeightForWidth(width)}`);
    expect(result.scale.height).toBe(chartHeightForWidth(width));
  });
});
