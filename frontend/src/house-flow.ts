// Today's energy flow as a Sankey diagram, like the battery app's daily flow: the sources on the left (sun, battery,
// grid), the uses on the right (battery, house, heat where the heat pump's meter is, car, grid export), each a block as tall as its share of the day's
// energy, and a curved band from each source to each use as thick as the energy between them. Every figure is the
// backend's (`site/energy_flow.py`, which states how the flows are allocated); this only places them.
//
// * Every block has its name and its share of the period's energy in a pill at its top ("Bil · 35 %"), one rule for
//   a short and a tall block; a column grows as wide as its widest pill needs (measured by the card where it can),
//   taking the width from the bands between the columns before any word is squeezed.
// * A block is never shorter than `MIN_NODE` (its words must fit), the rest of the height is shared by energy; every
//   band has one scale (the fullest block's), so a band is as thick at both ends, and a block made taller for its
//   words holds its bands centred.
// * A block below `MIN_SHOWN_KWH` is left out with its bands (a use with no energy today, a car that did not charge),
//   and so is a block left with no band (all its energy went to or came from blocks left out): no block stands
//   alone. A band is never thinner than `MIN_BAND` px, so none is drawn without width.
// * A role the card does not know (a newer backend's) is drawn in a neutral colour under its own name; a car giving
//   energy back would come as `car` on the source side and is drawn as the car.
// * The colours are the energy palette's (`energy-palette.ts`) a step lighter, as custom properties a theme may set
//   (`--spotnav-flow-solar` and so on), each fill bright enough for the dark words on it on a light and a dark card;
//   the grid as a use (what is sold) is the export's green.
// * With `onHouse`, the house block is a button (tap, Enter or Space): the card opens what the house used
//   (`house-loads.ts`); so is the heat block where the heat holds more than one source (`heat_sources`: the heat
//   pump's meter, floor heating, an air-to-air heat pump), opening its own pie.

import { formatFixed, formatNumber } from "./format";
import type { EnergyFlow, FlowLink, FlowNode } from "./house-model";
import { translate, type Language, type TranslationKey } from "./i18n";

/** Which block's pie is opened. */
export type LoadsKind = "house" | "heat";

const SVG_NS = "http://www.w3.org/2000/svg";

/** The shortest block (px): the name's pill and one line. */
export const MIN_NODE = 44;
/** From this height a block has the kWh big under its pill (and the battery's level at its bottom). */
const TALL_NODE = 64;
const PAD = 6;
const PILL_H = 17;
/** The room each side of the pill's words (px). */
export const PILL_PAD = 8;
/** The bands between the columns keep at least this share of the width, and this many px. */
const BAND_SHARE = 0.22;
const BAND_MIN = 56;
/** A pill's words where they cannot be measured (px per character of the 12 px semi-bold name). */
const NAME_CHAR_PX = 6.3;
/** The height each block of the fuller side asks of the diagram. */
const BLOCK_ROOM = 80;
/** Blocks with less energy (kWh) are not drawn. */
export const MIN_SHOWN_KWH = 0.05;
const GAP = 6;
/** The thinnest band (px). */
export const MIN_BAND = 1.5;

const ROLE_WORDS: Record<string, TranslationKey> = {
  solar: "house.flow.solar",
  battery: "house.flow.battery",
  grid: "house.flow.grid",
  house: "house.flow.house",
  car: "house.flow.car",
  heat: "house.flow.heat",
};

/** The diagram's height: by the width, and tall enough that each block of the fuller side (most often the uses: battery,
 * house, car, export) may get the tall form with the kWh big; bounded. */
export function flowHeight(width: number, blocks = 3): number {
  const bounded = Number.isFinite(width) && width > 0 ? width : 340;
  const byWidth = Math.min(340, Math.max(250, bounded * 0.72));
  return Math.round(Math.min(440, Math.max(byWidth, blocks * BLOCK_ROOM)));
}

function nodeWidth(width: number): number {
  return Math.round(Math.min(120, Math.max(84, width * 0.27)));
}

/** The width of a pill's words (px) as drawn; `null` where it cannot be measured (the card measures real text). */
export type PillMeasure = (words: string) => number | null;

function wordsWidth(words: string, measure: PillMeasure | null): number {
  const measured = measure?.(words) ?? null;
  return measured !== null && measured > 0 ? measured : words.length * NAME_CHAR_PX;
}

/** The two columns' widths: each at least the usual one and wide enough for its widest pill with its padding, taken
 * from the bands between them; where the bands would get too narrow both give way in proportion. */
export function columnWidths(width: number, widestLeft: number, widestRight: number): [number, number] {
  const usual = nodeWidth(width);
  const want = (widest: number): number => Math.max(usual, Math.ceil(widest + 2 * PILL_PAD + 2 * PAD));
  const left = want(widestLeft);
  const right = want(widestRight);
  const room = width - Math.max(BAND_MIN, width * BAND_SHARE);
  if (left + right <= room) {
    return [left, right];
  }
  const over = left + right - room;
  const spare = left + right - 2 * usual;
  if (spare <= 0) {
    return [usual, usual];
  }
  return [Math.max(usual, Math.floor(left - (over * (left - usual)) / spare)), Math.max(usual, Math.floor(right - (over * (right - usual)) / spare))];
}

function svg<K extends keyof SVGElementTagNameMap>(doc: Document, name: K, attributes: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const created = doc.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    created.setAttribute(key, String(value));
  }
  return created;
}

export function roleWord(language: Language, role: string): string {
  const key = ROLE_WORDS[role];
  return key === undefined ? role : translate(language, key);
}

/** A block's colour (the energy palette's, `energy-palette.ts`): the grid as a use is what is sold, in the export's. */
export function roleColour(role: string, side: "source" | "use" = "source"): string {
  if (role === "grid" && side === "use") {
    return "var(--spotnav-flow-export)";
  }
  return ROLE_WORDS[role] === undefined ? "var(--spotnav-flow-other)" : `var(--spotnav-flow-${role})`;
}

/** Each block's height: at least `MIN_NODE`, the rest by energy. */
export function blockHeights(values: number[], height: number): number[] {
  const room = height - GAP * Math.max(0, values.length - 1);
  const fixed = new Set<number>();
  for (;;) {
    const free = values.reduce((sum, value, index) => (fixed.has(index) ? sum : sum + value), 0);
    const left = room - fixed.size * MIN_NODE;
    let changed = false;
    values.forEach((value, index) => {
      if (!fixed.has(index) && (free <= 0 || (value / free) * left < MIN_NODE)) {
        fixed.add(index);
        changed = true;
      }
    });
    if (!changed) {
      return values.map((value, index) => (fixed.has(index) ? MIN_NODE : (value / free) * left));
    }
  }
}

interface Placed {
  node: FlowNode;
  y: number;
  h: number;
}

function place(nodes: FlowNode[], height: number): Map<string, Placed> {
  const heights = blockHeights(nodes.map((node) => node.kwh), height);
  const placed = new Map<string, Placed>();
  let y = 0;
  nodes.forEach((node, index) => {
    const h = heights[index]!;
    placed.set(node.id, { node, y, h });
    y += h + GAP;
  });
  return placed;
}

export interface FlowLabels {
  kwh(value: number): string;
  share(part: number): string;
  soc(value: number): string;
}

export function flowLabels(language: Language, total: number, past = false): FlowLabels {
  return {
    // No decimal from 100 kWh up, so a month's or a year's figures fit a narrow block.
    kwh: (value) => `${formatFixed(language, value, value >= 100 ? 0 : 1)} kWh`,
    share: (part) => (total > 0 ? `${formatNumber(language, (part / total) * 100, 0)} %` : "–"),
    soc: (value) => translate(language, past ? "house.flow.socEnd" : "house.flow.socNow", { pct: `${formatNumber(language, value, 0)} %` }),
  };
}

/** The diagram, `width` px wide; `null` when nothing is left to draw. */
export function renderFlow(
  doc: Document,
  language: Language,
  flow: EnergyFlow,
  width: number,
  idPrefix: string,
  past = false,
  onHouse: ((opener: HTMLElement, kind: LoadsKind) => void) | null = null,
  /** A height of its own (a box of fixed height gives its spare room); never below what the blocks' words need. */
  fitHeight: number | null = null,
  /** The pills' words measured as drawn (the card's own); without it they are estimated. */
  measure: PillMeasure | null = null,
): SVGSVGElement | null {
  const big = new Set(flow.nodes.filter((node) => node.kwh >= MIN_SHOWN_KWH).map((node) => node.id));
  const linked = new Set(flow.links.filter((link) => link.kwh > 0 && big.has(link.from) && big.has(link.to)).flatMap((link) => [link.from, link.to]));
  const shown = flow.nodes.filter((node) => linked.has(node.id));
  const sources = shown.filter((node) => node.side === "source");
  const uses = shown.filter((node) => node.side === "use");
  if (sources.length === 0 || uses.length === 0) {
    return null;
  }
  const blocks = Math.max(sources.length, uses.length);
  const height = fitHeight === null ? flowHeight(width, blocks) : Math.round(Math.max(fitHeight, blocks * (MIN_NODE + GAP) + GAP));
  const left = place(sources, height);
  const right = place(uses, height);
  const total = sources.reduce((sum, node) => sum + node.kwh, 0);
  // A past day's battery level is the one at its end, not now.
  const labels = flowLabels(language, total, past);
  // Every block's pill says its name and its share; a column is as wide as its widest pill needs.
  const pillWords = (node: FlowNode): string => `${roleWord(language, node.role)} · ${labels.share(node.kwh)}`;
  const pillWidths = new Map<string, number>(shown.map((node) => [node.id, wordsWidth(pillWords(node), measure)]));
  const widest = (nodes: FlowNode[]): number => Math.max(0, ...nodes.map((node) => pillWidths.get(node.id) ?? 0));
  const [leftW, rightW] = columnWidths(width, widest(sources), widest(uses));
  const root = svg(doc, "svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", class: "spotnav-house-flow-chart" });
  root.setAttribute(
    "aria-label",
    [...sources, ...uses].map((node) => `${roleWord(language, node.role)} ${labels.kwh(node.kwh)} (${labels.share(node.kwh)})`).join(", "),
  );
  const defs = svg(doc, "defs");
  const sheenId = `${idPrefix}-flow-sheen`;
  const sheen = svg(doc, "linearGradient", { id: sheenId, x1: 0, y1: 0, x2: 0, y2: 1 });
  for (const [offset, opacity] of [
    ["0", "0.32"],
    ["0.55", "0"],
    ["1", "0"],
  ] as const) {
    sheen.append(svg(doc, "stop", { offset, "stop-color": "#ffffff", "stop-opacity": opacity }));
  }
  defs.append(sheen);
  const bands = svg(doc, "g", { class: "spotnav-house-flow-bands" });
  root.append(defs, bands);
  const links = flow.links.filter((link) => left.has(link.from) && right.has(link.to) && link.kwh > 0);
  // One scale for every band (px per kWh), the most the fullest block allows, so a band is as thick at both ends;
  // a block made taller for its words holds its bands centred.
  const scale = Math.min(...[...left.values(), ...right.values()].filter((b) => b.node.kwh > 0).map((b) => b.h / b.node.kwh));
  const outAt = new Map<FlowLink, [number, number]>();
  const inAt = new Map<FlowLink, [number, number]>();
  for (const [placedMap, key, otherMap, otherKey, target] of [
    [left, "from", right, "to", outAt],
    [right, "to", left, "from", inAt],
  ] as const) {
    const otherOrder = [...otherMap.keys()];
    for (const [id, block] of placedMap) {
      const mine = links.filter((link) => link[key] === id).sort((a, b) => otherOrder.indexOf(a[otherKey]) - otherOrder.indexOf(b[otherKey]));
      const used = mine.reduce((sum, link) => sum + Math.max(MIN_BAND, link.kwh * scale), 0);
      let y = block.y + Math.max(0, (block.h - used) / 2);
      for (const link of mine) {
        const thick = Math.max(MIN_BAND, link.kwh * scale);
        target.set(link, [y, y + thick]);
        y += thick;
      }
    }
  }
  const x0 = leftW;
  const x1 = width - rightW;
  const mid = (x0 + x1) / 2;
  links.forEach((link, index) => {
    const [a0, a1] = outAt.get(link)!;
    const [b0, b1] = inAt.get(link)!;
    const from = left.get(link.from)!.node;
    const to = right.get(link.to)!.node;
    const id = `${idPrefix}-flow-${index}`;
    const gradient = svg(doc, "linearGradient", { id, gradientUnits: "userSpaceOnUse", x1: x0, y1: 0, x2: x1, y2: 0 });
    for (const [offset, role, side] of [
      ["0", from.role, "source"],
      ["1", to.role, "use"],
    ] as const) {
      const stop = svg(doc, "stop", { offset });
      stop.style.setProperty("stop-color", roleColour(role, side));
      gradient.append(stop);
    }
    defs.append(gradient);
    const f = (value: number) => value.toFixed(1);
    const path = svg(doc, "path", {
      class: "spotnav-house-flow-band",
      d: `M${f(x0)},${f(a0)} C${f(mid)},${f(a0)} ${f(mid)},${f(b0)} ${f(x1)},${f(b0)} L${f(x1)},${f(b1)} C${f(mid)},${f(b1)} ${f(mid)},${f(a1)} ${f(x0)},${f(a1)} Z`,
      fill: `url(#${id})`,
      "data-from": from.role,
      "data-to": to.role,
    });
    const title = svg(doc, "title");
    title.textContent = `${roleWord(language, from.role)} → ${roleWord(language, to.role)} ${labels.kwh(link.kwh)}`;
    path.append(title);
    bands.append(path);
  });
  for (const [placedMap, x, nodeW] of [
    [left, 0, leftW],
    [right, x1, rightW],
  ] as const) {
    for (const { node, y, h } of placedMap.values()) {
      const group = svg(doc, "g", { class: "spotnav-house-flow-node", "data-role": node.role, "data-side": node.side });
      const rect = svg(doc, "rect", { x, y: y.toFixed(1), width: nodeW, height: h.toFixed(1), rx: 6, class: "spotnav-house-flow-block" });
      rect.style.setProperty("fill", roleColour(node.role, node.side));
      // A soft light from the top over the block's own colour.
      const sheen = svg(doc, "rect", { x, y: y.toFixed(1), width: nodeW, height: h.toFixed(1), rx: 6, fill: `url(#${sheenId})`, "aria-hidden": "true" });
      group.append(rect, sheen);
      const name = roleWord(language, node.role);
      const text = (kind: string, words: string, tx: number, ty: number, anchor = "start"): void => {
        const created = svg(doc, "text", { x: tx.toFixed(1), y: ty.toFixed(1), class: `spotnav-house-flow-${kind}`, "text-anchor": anchor });
        created.textContent = words;
        group.append(created);
      };
      // The name and the share in a lighter pill at the top, with room each side of the words; in a short block the
      // pill and the kWh under it are centred, in a tall one the pill sits a margin from the top with the kWh big
      // below it (smaller from 1 000 kWh) and the battery's level a margin from the bottom.
      const short = h < TALL_NODE;
      const top = short ? y + (h - (PILL_H + 4 + 12)) / 2 : y + PAD;
      const pill = svg(doc, "rect", { x: x + PAD, y: top.toFixed(1), width: nodeW - 2 * PAD, height: PILL_H, rx: PILL_H / 2, class: "spotnav-house-flow-pill" });
      group.append(pill);
      text("name", pillWords(node), x + nodeW / 2, top + PILL_H - 5, "middle");
      // Words still wider than the pill's room (the columns could not grow more) are drawn narrower, never clipped.
      const pillRoom = nodeW - 2 * PAD - 2 * PILL_PAD;
      if ((pillWidths.get(node.id) ?? 0) > pillRoom) {
        const words = group.lastElementChild!;
        words.setAttribute("textLength", pillRoom.toFixed(1));
        words.setAttribute("lengthAdjust", "spacingAndGlyphs");
      }
      if (short) {
        text("share", labels.kwh(node.kwh), x + PAD + 1, top + PILL_H + 4 + 10);
      } else {
        text(node.kwh >= 1000 ? "value-long" : "value", labels.kwh(node.kwh), x + PAD + 1, top + PILL_H + 19);
        if (node.soc_pct !== null) {
          text("share", labels.soc(node.soc_pct), x + PAD + 1, y + h - PAD - 1);
        }
      }
      const title = svg(doc, "title");
      title.textContent = `${name} ${labels.kwh(node.kwh)} (${labels.share(node.kwh)})`;
      group.append(title);
      if (onHouse !== null && node.side === "use" && (node.role === "house" || (node.role === "heat" && flow.heat_sources > 1))) {
        const kind: LoadsKind = node.role === "heat" ? "heat" : "house";
        houseButton(group, language, `${name} ${labels.kwh(node.kwh)} (${labels.share(node.kwh)})`, (opener) => onHouse(opener, kind), kind);
      }
      root.append(group);
    }
  }
  return root;
}

/** The house (or heat) block as a button: focusable, named, opened by a tap, Enter or Space. */
function houseButton(group: SVGGElement, language: Language, figures: string, onHouse: (opener: HTMLElement) => void, kind: LoadsKind): void {
  group.setAttribute("role", "button");
  group.setAttribute("tabindex", "0");
  group.setAttribute("aria-label", translate(language, kind === "heat" ? "house.loads.openHeat" : "house.loads.open", { figures }));
  group.classList.add("spotnav-house-flow-action");
  // An SVG element focuses and is told apart from a removed one as an HTML element is.
  const opener = group as unknown as HTMLElement;
  group.addEventListener("click", () => onHouse(opener));
  group.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onHouse(opener);
    }
  });
}
