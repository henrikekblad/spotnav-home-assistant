// Today's energy flow as a Sankey diagram, like the battery app's daily flow: the sources on the left (sun, battery,
// grid), the uses on the right (battery, house, car, grid export), each a block as tall as its share of the day's
// energy, and a curved band from each source to each use as thick as the energy between them. Every figure is the
// backend's (`site/energy_flow.py`, which states how the flows are allocated); this only places them.
//
// * A block is never shorter than `MIN_NODE` (its words must fit), the rest of the height is shared by energy; every
//   band has one scale (the fullest block's), so a band is as thick at both ends, and a block made taller for its
//   words holds its bands centred.
// * A block below `MIN_SHOWN_KWH` is left out with its bands (a use with no energy today, a car that did not charge).
// * A role the card does not know (a newer backend's) is drawn in a neutral colour under its own name; a car giving
//   energy back would come as `car` on the source side and is drawn as the car.
// * The colours are custom properties a theme may set (`--spotnav-flow-solar` and so on), each fill bright enough for
//   the dark words on it on a light and a dark card.

import { formatFixed, formatNumber } from "./format";
import type { EnergyFlow, FlowLink, FlowNode } from "./house-model";
import { translate, type Language, type TranslationKey } from "./i18n";

const SVG_NS = "http://www.w3.org/2000/svg";

/** The shortest block (px): two lines of words. */
export const MIN_NODE = 34;
/** Blocks with less energy (kWh) are not drawn. */
export const MIN_SHOWN_KWH = 0.05;
const GAP = 6;

const ROLE_WORDS: Record<string, TranslationKey> = {
  solar: "house.flow.solar",
  battery: "house.flow.battery",
  grid: "house.flow.grid",
  house: "house.flow.house",
  car: "house.flow.car",
};

export function flowHeight(width: number): number {
  const bounded = Number.isFinite(width) && width > 0 ? width : 340;
  return Math.round(Math.min(340, Math.max(250, bounded * 0.72)));
}

function nodeWidth(width: number): number {
  return Math.round(Math.min(120, Math.max(84, width * 0.27)));
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

function roleColour(role: string): string {
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

export function flowLabels(language: Language, total: number): FlowLabels {
  return {
    kwh: (value) => `${formatFixed(language, value, 1)} kWh`,
    share: (part) => (total > 0 ? `${formatNumber(language, (part / total) * 100, 0)} %` : "–"),
    soc: (value) => translate(language, "house.flow.socNow", { pct: `${formatNumber(language, value, 0)} %` }),
  };
}

/** The diagram, `width` px wide; `null` when nothing is left to draw. */
export function renderFlow(doc: Document, language: Language, flow: EnergyFlow, width: number, idPrefix: string): SVGSVGElement | null {
  const shown = flow.nodes.filter((node) => node.kwh >= MIN_SHOWN_KWH);
  const sources = shown.filter((node) => node.side === "source");
  const uses = shown.filter((node) => node.side === "use");
  if (sources.length === 0 || uses.length === 0) {
    return null;
  }
  const height = flowHeight(width);
  const nodeW = nodeWidth(width);
  const left = place(sources, height);
  const right = place(uses, height);
  const total = sources.reduce((sum, node) => sum + node.kwh, 0);
  const labels = flowLabels(language, total);
  const root = svg(doc, "svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", class: "spotnav-house-flow-chart" });
  root.setAttribute(
    "aria-label",
    [...sources, ...uses].map((node) => `${roleWord(language, node.role)} ${labels.kwh(node.kwh)} (${labels.share(node.kwh)})`).join(", "),
  );
  const defs = svg(doc, "defs");
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
      const used = mine.reduce((sum, link) => sum + link.kwh * scale, 0);
      let y = block.y + Math.max(0, (block.h - used) / 2);
      for (const link of mine) {
        const thick = link.kwh * scale;
        target.set(link, [y, y + thick]);
        y += thick;
      }
    }
  }
  const x0 = nodeW;
  const x1 = width - nodeW;
  const mid = (x0 + x1) / 2;
  links.forEach((link, index) => {
    const [a0, a1] = outAt.get(link)!;
    const [b0, b1] = inAt.get(link)!;
    const from = left.get(link.from)!.node;
    const to = right.get(link.to)!.node;
    const id = `${idPrefix}-flow-${index}`;
    const gradient = svg(doc, "linearGradient", { id, gradientUnits: "userSpaceOnUse", x1: x0, y1: 0, x2: x1, y2: 0 });
    for (const [offset, role] of [
      ["0", from.role],
      ["1", to.role],
    ] as const) {
      const stop = svg(doc, "stop", { offset });
      stop.style.setProperty("stop-color", roleColour(role));
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
  for (const [placedMap, x] of [
    [left, 0],
    [right, x1],
  ] as const) {
    for (const { node, y, h } of placedMap.values()) {
      const group = svg(doc, "g", { class: "spotnav-house-flow-node", "data-role": node.role, "data-side": node.side });
      const rect = svg(doc, "rect", { x, y: y.toFixed(1), width: nodeW, height: h.toFixed(1), rx: 6 });
      rect.style.setProperty("fill", roleColour(node.role));
      group.append(rect);
      const name = roleWord(language, node.role);
      const lines: Array<[string, string]> = [];
      if (h >= 52) {
        lines.push(["name", name], ["value", labels.kwh(node.kwh)], ["share", labels.share(node.kwh)]);
        if (node.soc_pct !== null && h >= 70) {
          lines.push(["share", labels.soc(node.soc_pct)]);
        }
      } else {
        lines.push(["name", name], ["share", `${labels.kwh(node.kwh)} · ${labels.share(node.kwh)}`]);
      }
      const size: Record<string, number> = { name: 11, value: 15, share: 11 };
      let baseline = y + 6;
      for (const [kind, words] of lines) {
        baseline += size[kind]! + 2;
        const text = svg(doc, "text", { x: x + 7, y: baseline.toFixed(1), class: `spotnav-house-flow-${kind}` });
        text.textContent = words;
        group.append(text);
      }
      const title = svg(doc, "title");
      title.textContent = `${name} ${labels.kwh(node.kwh)} (${labels.share(node.kwh)})`;
      group.append(title);
      root.append(group);
    }
  }
  return root;
}
