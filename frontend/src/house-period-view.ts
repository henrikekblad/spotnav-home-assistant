// The house card's period tabs' own parts (SpotNav 3.0): the tiles of the Energy flow tab, the energy chart of the
// Energy chart tab for a month or a year (and a past day too far back for its hours), and the kr bars of the Price
// and battery tab: what the battery earned (its arbitrage, `HouseBucket.arbitrage`, the card's headline measure and
// the one the battery's own app shows), under a day's price chart on its own hours, and per day or month alone.
//
// * The tiles are the energy flow's own blocks (`site/energy_flow.py`): sun, house, discharged, charged, bought,
//   sold, and heat and car where the site has them. They are shown only where the period has no diagram to carry the
//   figures, and never one of 0 kWh.
// * The Earnings tab's bars (`house-earnings.ts`) are these bars too, a stack per part.
// * The bars are HTML buttons in one row with a single tab stop: the arrow keys, Home and End move between them, and
//   hover, focus, a tap or a drag shows a bar's figures in the readout under them. A kr bar grows up from the zero
//   line when the battery earned and down when it lost.
// * The energy chart is the day chart's look (`house-chart.ts`): a kWh axis with its gridlines, one full-width bar per
//   day or month with the house, the heat and the car stacked, and the sun as a stepped line over them.
// Every figure is the backend's; text is written with `textContent` only.

import { attachScrub, scrubBars } from "./chart-scrub";
import { formatFixed, formatNumber } from "./format";
import type { EnergyFlow } from "./house-model";
import type { BatteryFlow, HouseBucket, HouseHistory } from "./house-history";
import { bucketWords } from "./house-history";
import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

export const PERIOD_VIEW_CLASSES = {
  tiles: "spotnav-house-tiles",
  tile: "spotnav-house-tile",
  tileValue: "spotnav-house-tile-value",
  tileName: "spotnav-house-tile-name",
  bars: "spotnav-house-pbars",
  plot: "spotnav-house-pbars-plot",
  bar: "spotnav-house-pbar",
  lane: "spotnav-house-pbar-lane",
  part: "spotnav-house-pbar-part",
  zero: "spotnav-house-pbars-zero",
  axis: "spotnav-house-pbars-axis",
  scale: "spotnav-house-pbars-scale",
  key: "spotnav-house-pbars-key",
  readout: "spotnav-house-pbars-readout",
  total: "spotnav-house-total",
} as const;

const P = PERIOD_VIEW_CLASSES;

export function kwhText(language: Language, value: number): string {
  return `${formatFixed(language, value, Math.abs(value) >= 100 ? 0 : 1)} kWh`;
}

/** What a bucket's battery arbitrage is made of, in words for a readout, to be compared with the battery maker's
 * cash flow: "urladdat 3,5 kWh, värt 8,1 kr, minus 5,6 kr som energin kostade att ladda = 2,5 kr", and what it
 * charged ("laddade 4,2 kWh för 3,9 kr (räknas när energin används)"); both where it did both; `null` where it
 * moved nothing. `money` writes an amount in the major unit. */
export function flowWords(language: Language, flow: BatteryFlow, money: (minor: number) => string): string | null {
  const parts: string[] = [];
  if (flow.discharged >= 0.05) {
    parts.push(
      translate(language, "house.flow.out", {
        kwh: kwhText(language, flow.discharged),
        worth: money(flow.worth),
        cost: money(flow.cost),
        net: money(flow.worth - flow.cost),
      }),
    );
  }
  if (flow.charged >= 0.05) {
    parts.push(translate(language, "house.flow.in", { kwh: kwhText(language, flow.charged), cost: money(flow.charge_cost) }));
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

// ------------------------------------------------------------------------------------------------ tiles

export interface Tile {
  key: string;
  series: string;
  name: TranslationKey;
  kwh: number;
}

function node(flow: EnergyFlow, side: "source" | "use", role: string): number | null {
  const found = flow.nodes.find((item) => item.side === side && item.role === role);
  return found === undefined ? null : found.kwh;
}

/** Below this (kWh) a tile's figure would read "0,0 kWh": it is left out. */
export const MIN_TILE_KWH = 0.05;

/** The tiles of a period's energy flow (see the module); a part the site does not have, or with no energy, is left
 * out: never a tile of 0 kWh. */
export function flowTiles(flow: EnergyFlow): Tile[] {
  const tiles: Tile[] = [];
  const add = (key: string, series: string, name: TranslationKey, kwh: number | null): void => {
    if (kwh !== null && kwh >= MIN_TILE_KWH) {
      tiles.push({ key, series, name, kwh });
    }
  };
  add("sun", "sun", "house.tile.sun", node(flow, "source", "solar"));
  add("house", "house", "house.tile.house", node(flow, "use", "house"));
  add("discharged", "battery", "house.tile.discharged", node(flow, "source", "battery"));
  add("charged", "battery", "house.tile.charged", node(flow, "use", "battery"));
  add("bought", "grid", "house.tile.bought", node(flow, "source", "grid"));
  add("sold", "sell", "house.tile.sold", node(flow, "use", "grid"));
  add("heat", "heat", "house.tile.heat", node(flow, "use", "heat"));
  add("car", "car", "house.tile.car", node(flow, "use", "car"));
  return tiles;
}

/** The tiles as a list; `null` with none to show. */
export function tilesBlock(doc: Document, language: Language, flow: EnergyFlow): HTMLElement | null {
  const tiles = flowTiles(flow);
  if (tiles.length === 0) {
    return null;
  }
  const list = element(doc, "ul", P.tiles);
  list.dataset["part"] = "tiles";
  for (const tile of tiles) {
    const item = element(doc, "li", P.tile);
    item.dataset["tile"] = tile.key;
    item.dataset["series"] = tile.series;
    item.append(element(doc, "span", P.tileValue, kwhText(language, tile.kwh)), element(doc, "span", `${P.tileName} ${C.muted}`, translate(language, tile.name)));
    list.append(item);
  }
  return list;
}

// ------------------------------------------------------------------------------------------------ bars

export interface BarPart {
  series: string;
  value: number;
}

export interface Bar {
  key: string;
  /** One or two lanes side by side, each a stack of parts (all above zero, or all below it). */
  lanes: BarPart[][];
  /** Where each lane stands among `slotCount` places side by side (default: its own index among the lanes), so a
   * stack above the line and one below it share a place. */
  slots?: number[];
  slotCount?: number;
  words: string;
  axis: string;
}

export interface BarChartInput {
  doc: Document;
  label: string;
  bars: Bar[];
  height: number;
  /** The scale's words for a value (the tallest bar's, and the deepest's). */
  scale: (value: number) => string;
  hint: string;
  /** A swatch per series, in the order given; none for a single series. */
  key?: Array<{ series: string; words: string }>;
  /** The bar shown when drawn (its key), or none. */
  selected?: string | null;
  onSelect?: (key: string | null) => void;
}

function laneSum(lane: BarPart[]): number {
  return lane.reduce((sum, part) => sum + part.value, 0);
}

/** The bars (see the module). */
export function barChart(input: BarChartInput): HTMLElement {
  const { doc, bars } = input;
  const wrap = element(doc, "div", P.bars);
  const top = Math.max(0, ...bars.flatMap((bar) => bar.lanes.map((lane) => Math.max(0, laneSum(lane)))));
  const bottom = Math.max(0, ...bars.flatMap((bar) => bar.lanes.map((lane) => Math.max(0, -laneSum(lane)))));
  const span = top + bottom > 0 ? top + bottom : 1;
  const zero = (bottom / span) * 100;
  const readout = element(doc, "p", `${C.muted} ${P.readout}`);
  readout.setAttribute("aria-live", "polite");
  const plot = element(doc, "div", P.plot);
  plot.style.height = `${Math.round(input.height)}px`;
  plot.setAttribute("role", "group");
  plot.setAttribute("aria-label", input.label);
  if (bottom > 0) {
    const line = element(doc, "span", P.zero);
    line.style.bottom = `${zero}%`;
    line.setAttribute("aria-hidden", "true");
    plot.append(line);
  }
  const buttons: HTMLButtonElement[] = [];
  let selected = input.selected ?? null;
  const show = (bar: Bar | null): void => {
    readout.textContent = bar === null ? input.hint : bar.words;
    buttons.forEach((button) => button.setAttribute("aria-pressed", String(bar !== null && button.dataset["bar"] === bar.key)));
  };
  const choose = (index: number): void => {
    const bar = bars[index];
    if (bar === undefined) {
      return;
    }
    selected = bar.key;
    show(bar);
    buttons.forEach((button, at) => (button.tabIndex = at === index ? 0 : -1));
    input.onSelect?.(bar.key);
  };
  bars.forEach((bar, index) => {
    const button = element(doc, "button", P.bar);
    button.type = "button";
    button.dataset["bar"] = bar.key;
    button.setAttribute("aria-label", bar.words);
    const width = 100 / (bar.slotCount ?? bar.lanes.length);
    bar.lanes.forEach((lane, index_) => {
      const at = bar.slots?.[index_] ?? index_;
      const sum = laneSum(lane);
      if (sum === 0) {
        return;
      }
      const column = element(doc, "span", P.lane);
      column.style.left = `${at * width}%`;
      column.style.width = `${width}%`;
      column.style.height = `${(Math.abs(sum) / span) * 100}%`;
      if (sum > 0) {
        column.style.bottom = `${zero}%`;
      } else {
        column.style.top = `${100 - zero}%`;
        column.dataset["below"] = "true";
      }
      // From the top: the last part first (a stack reads bottom up).
      for (const part of [...lane].reverse()) {
        if (part.value === 0) {
          continue;
        }
        const piece = element(doc, "span", P.part);
        piece.dataset["series"] = part.series;
        // Its share of the lane, never the bare value: flex factors that add up to less than 1 (an hour of 0.7 kWh
        // alone) would fill only that fraction of the lane and leave the bar short of the baseline.
        piece.style.flexGrow = String(Math.max(1, Math.round((Math.abs(part.value) / Math.abs(sum)) * 1000)));
        column.append(piece);
      }
      button.append(column);
    });
    button.addEventListener("mouseenter", () => show(bar));
    button.addEventListener("focus", () => show(bar));
    button.addEventListener("click", () => choose(index));
    button.addEventListener("keydown", (event) => {
      const last = bars.length - 1;
      const next = event.key === "ArrowRight" ? Math.min(last, index + 1) : event.key === "ArrowLeft" ? Math.max(0, index - 1) : event.key === "Home" ? 0 : event.key === "End" ? last : null;
      if (next === null) {
        return;
      }
      event.preventDefault();
      choose(next);
      buttons[next]?.focus();
    });
    buttons.push(button);
    plot.append(button);
  });
  const chosen = Math.max(0, bars.findIndex((bar) => bar.key === selected));
  buttons.forEach((button, at) => (button.tabIndex = at === chosen ? 0 : -1));
  plot.addEventListener("mouseleave", () => show(bars.find((bar) => bar.key === selected) ?? null));
  scrubBars(plot, buttons, (index) => show(bars[index] ?? null), choose);
  const axis = element(doc, "div", P.axis);
  axis.setAttribute("aria-hidden", "true");
  for (const bar of bars) {
    axis.append(element(doc, "span", undefined, bar.axis));
  }
  // The scale: the tallest bar's figure at the top left, and the deepest's at the bottom left where bars go below zero.
  const scale = element(doc, "span", `${C.muted} ${P.scale}`, input.scale(top));
  scale.setAttribute("aria-hidden", "true");
  plot.append(scale);
  if (bottom > 0) {
    const low = element(doc, "span", `${C.muted} ${P.scale}`, input.scale(-bottom));
    low.dataset["low"] = "true";
    low.setAttribute("aria-hidden", "true");
    plot.append(low);
  }
  wrap.append(plot, axis);
  if (input.key !== undefined && input.key.length > 1) {
    const key = element(doc, "ul", `${C.muted} ${P.key}`);
    for (const item of input.key) {
      const entry = element(doc, "li");
      const swatch = element(doc, "span", P.part);
      swatch.dataset["series"] = item.series;
      swatch.setAttribute("aria-hidden", "true");
      entry.append(swatch, element(doc, "span", undefined, item.words));
      key.append(entry);
    }
    wrap.append(key);
  }
  wrap.append(readout);
  show(bars.find((bar) => bar.key === selected) ?? null);
  return wrap;
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgOf<K extends keyof SVGElementTagNameMap>(doc: Document, name: K, attributes: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const created = doc.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    created.setAttribute(key, String(value));
  }
  return created;
}

/** A round step for an axis up to `top` with at most `ticks` steps (1, 2 or 5 times a power of ten). */
export function niceStep(top: number, ticks = 4): number {
  if (!(top > 0)) {
    return 1;
  }
  const raw = top / ticks;
  const power = 10 ** Math.floor(Math.log10(raw));
  for (const factor of [1, 2, 2.5, 5, 10]) {
    if (factor * power >= raw) {
      return factor * power;
    }
  }
  return 10 * power;
}

/** Which series a period's energy chart has: the house always, the heat, the car and the sun where any bucket has them. */
export function energySeries(answer: HouseHistory): { heat: boolean; car: boolean; sun: boolean } {
  return {
    heat: answer.buckets.some((bucket) => (bucket.heat ?? 0) > 0),
    car: answer.buckets.some((bucket) => bucket.car > 0),
    sun: answer.buckets.some((bucket) => bucket.solar !== null),
  };
}

/** The energy chart of a period (see the module); `null` with nothing measured. */
export function energyChart(
  doc: Document,
  language: Language,
  answer: HouseHistory,
  width: number,
  height: number,
  selected: string | null,
  onSelect?: (key: string | null) => void,
  /** In a box of fixed height: no hint under it. */
  boxed = false,
): HTMLElement | null {
  const buckets = answer.buckets;
  if (buckets.length === 0 || buckets.every((bucket) => bucket.periods === 0)) {
    return null;
  }
  const series = energySeries(answer);
  const use = (bucket: HouseBucket): number => bucket.house + (series.heat ? (bucket.heat ?? 0) : 0) + (series.car ? bucket.car : 0);
  const peak = Math.max(0, ...buckets.map(use), ...(series.sun ? buckets.map((bucket) => bucket.solar ?? 0) : []));
  const step = niceStep(peak, 3);
  const top = Math.max(step, Math.ceil(peak / step) * step);
  const left = 40;
  const right = width - 4;
  const plotTop = 10;
  const bottom = height - 18;
  const y = (value: number): number => bottom - (value / top) * (bottom - plotTop);
  const slot = (right - left) / buckets.length;
  const gap = Math.min(2, slot * 0.25);
  const root = svgOf(doc, "svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "spotnav-house-period-chart", role: "img" });
  root.setAttribute("aria-label", translate(language, "house.graph.label"));
  const grid = svgOf(doc, "g");
  for (let value = 0; value <= top + 1e-9; value += step) {
    grid.append(
      svgOf(doc, "line", { class: C.gridline, x1: left, x2: right, y1: y(value), y2: y(value) }),
      Object.assign(svgOf(doc, "text", { class: C.tickLabel, x: left - 4, y: y(value) + 4, "text-anchor": "end" }), {
        textContent: `${formatNumber(language, value, step >= 1 ? 0 : 1)}${value === top ? " kWh" : ""}`,
      }),
    );
  }
  root.append(grid);
  const band = svgOf(doc, "rect", { class: "spotnav-house-period-band", x: 0, y: plotTop, width: slot, height: bottom - plotTop });
  band.style.display = "none";
  root.append(band);
  const bars = svgOf(doc, "g");
  buckets.forEach((bucket, index) => {
    const x = left + index * slot + gap / 2;
    const parts: Array<[string, number]> = [["house", bucket.house]];
    if (series.heat) {
      parts.push(["heat", bucket.heat ?? 0]);
    }
    if (series.car) {
      parts.push(["car", bucket.car]);
    }
    let base = 0;
    for (const [name, value] of parts) {
      if (value <= 0) {
        continue;
      }
      const rect = svgOf(doc, "rect", { class: "spotnav-house-period-part", x, y: y(base + value), width: Math.max(0.5, slot - gap), height: Math.max(0.5, y(base) - y(base + value)) });
      rect.dataset["series"] = name;
      bars.append(rect);
      base += value;
    }
    const words = bucketWords(language, answer.period, bucket.key, index, buckets.length);
    if (words.axis !== "") {
      bars.append(Object.assign(svgOf(doc, "text", { class: C.tickLabel, x: left + (index + 0.5) * slot, y: height - 4, "text-anchor": "middle" }), { textContent: words.axis }));
    }
  });
  root.append(bars);
  if (series.sun) {
    // A step per bucket measured; a gap (a new run) where one was not, and none after the last.
    let d = "";
    let joined = false;
    buckets.forEach((bucket, index) => {
      if (bucket.periods === 0 || bucket.solar === null) {
        joined = false;
        return;
      }
      const x0 = left + index * slot;
      const level = y(bucket.solar).toFixed(1);
      d += `${joined ? "L" : "M"}${x0.toFixed(1)},${level}L${(x0 + slot).toFixed(1)},${level}`;
      joined = true;
    });
    const sun = svgOf(doc, "path", { d, class: "spotnav-house-period-sun" });
    root.append(sun);
  }
  root.append(svgOf(doc, "line", { class: C.gridline, x1: left, x2: right, y1: bottom, y2: bottom }));
  const surface = element(doc, "div", "spotnav-house-period-surface");
  surface.tabIndex = 0;
  surface.setAttribute("role", "group");
  surface.setAttribute("aria-label", translate(language, "house.graph.label"));
  surface.append(root);
  const readout = element(doc, "p", `${C.muted} ${P.readout}`);
  readout.setAttribute("aria-live", "polite");
  const words = buckets.map((bucket, index) => energyWords(language, bucket, bucketWords(language, answer.period, bucket.key, index, buckets.length).when, series));
  let chosen = buckets.findIndex((bucket) => bucket.key === selected);
  const show = (index: number): void => {
    if (index < 0) {
      band.style.display = "none";
      readout.textContent = boxed ? "" : translate(language, "hist.house.hint");
      return;
    }
    band.style.display = "";
    band.setAttribute("x", String(left + index * slot));
    readout.textContent = words[index]!;
  };
  const indexAt = (event: PointerEvent | MouseEvent): number => {
    const box = root.getBoundingClientRect();
    if (box.width <= 0) {
      return -1;
    }
    const at = ((event.clientX - box.left) / box.width) * width;
    return at < left - 4 || at > right + 4 ? -1 : Math.min(buckets.length - 1, Math.max(0, Math.floor((at - left) / slot)));
  };
  const choose = (index: number): void => {
    chosen = index;
    show(index);
    onSelect?.(index < 0 ? null : buckets[index]!.key);
  };
  let scrubbed = -1;
  attachScrub(surface, {
    hover: (event) => show(indexAt(event) >= 0 ? indexAt(event) : chosen),
    leave: () => show(chosen),
    drag: (event) => {
      scrubbed = indexAt(event);
      show(scrubbed);
    },
    end: () => {
      if (scrubbed >= 0) {
        choose(scrubbed);
      }
      scrubbed = -1;
    },
  });
  surface.addEventListener("click", (event) => choose(indexAt(event)));
  surface.addEventListener("keydown", (event) => {
    const last = buckets.length - 1;
    const next = event.key === "ArrowRight" ? Math.min(last, chosen + 1) : event.key === "ArrowLeft" ? Math.max(0, chosen < 0 ? last : chosen - 1) : event.key === "Home" ? 0 : event.key === "End" ? last : event.key === "Escape" ? -1 : null;
    if (next === null) {
      return;
    }
    event.preventDefault();
    choose(next);
  });
  show(chosen);
  const key = element(doc, "ul", `${C.muted} ${P.key}`);
  const item = (name: string, words_: TranslationKey, line = false): void => {
    const entry = element(doc, "li");
    const swatch = element(doc, "span", line ? `${P.part} spotnav-house-key-line` : P.part);
    swatch.dataset["series"] = name;
    swatch.setAttribute("aria-hidden", "true");
    entry.append(swatch, element(doc, "span", undefined, translate(language, words_)));
    key.append(entry);
  };
  item("house", "house.legend.house");
  if (series.heat) {
    item("heat", "house.legend.heat");
  }
  if (series.car) {
    item("car", "house.legend.car");
  }
  if (series.sun) {
    item("sun", "house.legend.sun", true);
  }
  const wrap = element(doc, "div", P.bars);
  wrap.dataset["part"] = "period-energy";
  wrap.append(surface, readout, key);
  return wrap;
}

function energyWords(language: Language, bucket: HouseBucket, when: string, series: { heat: boolean; car: boolean; sun: boolean }): string {
  if (bucket.periods === 0) {
    return translate(language, "hist.house.barNone", { when });
  }
  const parts = [when, `${translate(language, "house.legend.house")} ${kwhText(language, bucket.house)}`];
  if (series.heat && bucket.heat !== null) {
    parts.push(`${translate(language, "house.legend.heat")} ${kwhText(language, bucket.heat)}`);
  }
  if (series.car && bucket.car > 0) {
    parts.push(`${translate(language, "house.legend.car")} ${kwhText(language, bucket.car)}`);
  }
  if (series.sun && bucket.solar !== null) {
    parts.push(`${translate(language, "house.legend.sun")} ${kwhText(language, bucket.solar)}`);
  }
  return parts.join(" · ");
}

export const PERIOD_VIEW_STYLES = `
  .${P.tiles} {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(98px, 1fr));
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .${P.tile} {
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: 4px 8px 4px 9px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-left: 3px solid var(--spotnav-tile-colour, var(--divider-color, #e0e0e0));
    border-radius: 8px;
    min-width: 0;
  }
  .${P.tileValue} {
    font-size: 1rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .${P.tileName} {
    font-size: 0.8rem;
  }
  [data-series="sun"] { --spotnav-tile-colour: var(--spotnav-sun-colour); }
  [data-series="house"] { --spotnav-tile-colour: var(--spotnav-house-colour); }
  [data-series="battery"] { --spotnav-tile-colour: var(--spotnav-battery-colour); }
  [data-series="grid"] { --spotnav-tile-colour: var(--spotnav-grid-colour); }
  [data-series="sell"] { --spotnav-tile-colour: var(--spotnav-sell-colour); }
  [data-series="heat"] { --spotnav-tile-colour: var(--spotnav-heat-colour); }
  [data-series="car"] { --spotnav-tile-colour: var(--spotnav-car-colour, var(--spotnav-flow-car)); }
  [data-series="earned"] { --spotnav-tile-colour: var(--spotnav-battery-colour); }
  [data-series="cost"] { --spotnav-tile-colour: var(--spotnav-sell-colour); }
  .${P.bars} {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .${P.scale} {
    position: absolute;
    left: 0;
    top: -2px;
    z-index: 1;
    padding: 0 3px;
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
    background: var(--ha-card-background, var(--card-background-color, #fff));
    border-radius: 3px;
    pointer-events: none;
  }
  .${P.scale}[data-low] {
    top: auto;
    bottom: 0;
  }
  .${P.plot} {
    position: relative;
    display: flex;
    align-items: stretch;
    gap: 2px;
    touch-action: pan-y;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .${P.zero} {
    position: absolute;
    left: 0;
    right: 0;
    height: 0;
    border-top: 1px solid var(--secondary-text-color, #727272);
    pointer-events: none;
  }
  .${P.bar} {
    position: relative;
    flex: 1 1 0;
    min-width: 0;
    padding: 0;
    background: transparent;
    border: 0;
    border-radius: 3px;
    cursor: pointer;
  }
  .${P.bar}:hover, .${P.bar}[aria-pressed="true"] {
    background: var(--secondary-background-color, #e5e5e5);
  }
  .${P.bar}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .${P.lane} {
    position: absolute;
    display: flex;
    flex-direction: column;
    gap: 1px;
    box-sizing: border-box;
    padding: 0 1px;
    min-height: 1px;
    overflow: hidden;
  }
  .${P.lane} > .${P.part} {
    flex-basis: 0;
    min-height: 1px;
    background: var(--spotnav-tile-colour);
  }
  .${P.lane} > .${P.part}:first-child { border-radius: 3px 3px 0 0; }
  .${P.lane}[data-below] > .${P.part}:first-child { border-radius: 0 0 3px 3px; }
  .${P.axis} {
    display: flex;
    gap: 2px;
    font-size: 0.72rem;
    color: var(--secondary-text-color, #727272);
  }
  .${P.axis} > span {
    flex: 1 1 0;
    min-width: 0;
    text-align: center;
    overflow: visible;
    white-space: nowrap;
  }
  .${P.key} {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 14px;
    margin: 2px 0 0;
    padding: 0;
    list-style: none;
    font-size: 0.78rem;
  }
  .${P.key} > li { display: inline-flex; align-items: center; gap: 6px; }
  .${P.key} .${P.part} {
    width: 0.9rem;
    height: 0.65rem;
    border-radius: 2px;
    background: var(--spotnav-tile-colour);
  }
  .${P.readout} {
    margin: 2px 0 0;
    min-height: 2.6em;
    font-size: 0.85rem;
  }
  .${P.total} {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 600;
  }
  .spotnav-house-total-sub {
    margin: -6px 0 0;
    font-size: 0.85rem;
  }
  .spotnav-house-period-surface {
    position: relative;
    touch-action: pan-y;
    outline: none;
    border-radius: 4px;
  }
  .spotnav-house-period-surface:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .spotnav-house-period-chart {
    display: block;
    overflow: visible;
  }
  .spotnav-house-period-part { fill: var(--spotnav-tile-colour); }
  .spotnav-house-period-band { fill: var(--secondary-background-color, rgba(127, 127, 127, 0.15)); opacity: 0.8; }
  .spotnav-house-period-sun {
    fill: none;
    stroke: var(--spotnav-sun-colour);
    stroke-width: 2;
    stroke-linejoin: round;
  }
  .${P.key} .spotnav-house-key-line {
    height: 0;
    border-top: 2px solid var(--spotnav-sun-colour);
    background: none;
  }
  @media (forced-colors: active) {
    .${P.lane} > .${P.part}, .${P.key} .${P.part} { background: CanvasText; }
    .${P.tile} { border-left-color: CanvasText; }
  }
`;
