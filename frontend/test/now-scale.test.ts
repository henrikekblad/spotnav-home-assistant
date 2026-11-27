// The Now tab's drawing at the card's width (`nowScale`): the nodes pushed out and everything drawn larger on a wider
// card, crisp at a scale of 1, nothing clipped and no captions meeting; and the house had from the lines when the
// answer has no house figure yet (just after a restart).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { CAPTION_EM, decodeHouseNow, flowLayout, lineWidth, nowFigures, nowFontSizes, nowScale, ownShare, renderNow, renderSchematic, type HouseNow } from "../src/house-now";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): unknown => JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8"));
const NOW = Date.parse("2026-11-13T16:20:00+00:00");

function house(name = "villa_battery_sun.json"): HouseAnswer {
  const decoded = decodeHouse(read("house", name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function live(name = "villa_now.json"): HouseNow {
  const decoded = decodeHouseNow(read("house_now", name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

/** A caption's box, from its anchor and the card's own estimate of its width (`CAPTION_EM` per character). */
function box(text: SVGTextElement, size: number): { x0: number; x1: number; y0: number; y1: number } {
  const x = Number(text.getAttribute("x"));
  const y = Number(text.getAttribute("y"));
  const w = (text.textContent ?? "").length * size * CAPTION_EM;
  const anchor = text.getAttribute("text-anchor");
  const x0 = anchor === "start" ? x : anchor === "end" ? x - w : x - w / 2;
  return { x0, x1: x0 + w, y0: y - size, y1: y + 2 };
}

const meet = (a: ReturnType<typeof box>, b: ReturnType<typeof box>): boolean => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

describe("the Now drawing at the card's width", () => {
  it("scales with the width from 360 px, at most 1.6 times, and writes in whole pixels of at least 12", () => {
    expect(nowScale(300)).toBe(1);
    expect(nowScale(360)).toBe(1);
    expect(nowScale(400)).toBeCloseTo(1.111, 3);
    expect(nowScale(560)).toBeCloseTo(1.556, 3);
    expect(nowScale(640)).toBe(1.6);
    expect(nowFontSizes(1)).toEqual({ caption: 12, label: 13, big: 16 });
    expect(nowFontSizes(nowScale(400)).caption).toBe(13);
    expect(lineWidth(4, 1.2)).toBeCloseTo(8.8 * 1.2);
    // Corner nodes pushed out with the width; the house in the middle.
    const narrow = flowLayout(["sun", "grid", "battery", "car"], 360);
    const wide = flowLayout(["sun", "grid", "battery", "car"], 400);
    expect(wide.spots.grid!.x - wide.spots.sun!.x).toBeGreaterThan(narrow.spots.grid!.x - narrow.spots.sun!.x);
    expect(wide.height).toBeGreaterThan(narrow.height);
  });

  for (const width of [300, 360, 400, 436, 500, 560, 640]) {
    for (const [name, answer, now] of [
      ["corners", "villa_battery_sun.json", "villa_now.json"],
      ["corners, export", "villa_battery_sun.json", "villa_export.json"],
      ["row", "house_learning.json", "house_alone_now.json"],
    ] as const) {
      it(`fits at ${width} px (${name}): a scale of 1, nothing outside, no captions meeting`, () => {
        for (const language of ["sv", "de"] as const) {
          const svg = renderSchematic({ doc: document, language, answer: house(answer), live: live(now), width, idPrefix: "s", nowMs: NOW });
          const [x, y, w, h] = svg.getAttribute("viewBox")!.split(" ").map(Number);
          expect(w).toBe(width);
          expect(svg.style.maxWidth).toBe(`${width}px`);
          // The corner nodes at the edges: the first one's centre its reach and 6 px in.
          const first = Math.min(...Array.from(svg.querySelectorAll<SVGGElement>(".spotnav-now-node")).map((node) => Number(/translate\(([\d.]+)/.exec(node.getAttribute("transform")!)![1])));
          expect(first).toBeLessThanOrEqual(31 * nowScale(width) + 6.1);
          const size = Number.parseInt(svg.style.getPropertyValue("--spotnav-now-caption"), 10);
          // At least 12 px, and 13 from 400 px, where the words fit beside each other (a long German pair on a
          // narrow card is written smaller rather than overlapping).
          // German's long grid caption is written smaller on a narrow card rather than overlapping.
          expect(size).toBeGreaterThanOrEqual(width >= 400 ? (language === "sv" ? 13 : 12) : language === "sv" ? (width >= 360 ? 12 : 10) : 9);
          const captions = Array.from(svg.querySelectorAll<SVGTextElement>(".spotnav-now-state, .spotnav-now-today"));
          const boxes = captions.map((text) => box(text, size));
          for (const [index, item] of boxes.entries()) {
            const words = captions[index]!.textContent;
            expect(item.x0, `${language} ${words}`).toBeGreaterThanOrEqual(x! - 0.5);
            expect(item.x1, `${language} ${words}`).toBeLessThanOrEqual(x! + w! + 0.5);
            expect(item.y0, `${language} ${words}`).toBeGreaterThanOrEqual(y!);
            expect(item.y1, `${language} ${words}`).toBeLessThanOrEqual(y! + h!);
            for (const other of boxes.slice(index + 1)) {
              expect(meet(item, other), `${language} ${words}`).toBe(false);
            }
          }
        }
      });
    }
  }
});

describe("the house when the answer has no house figure yet", () => {
  it("is had from the lines, with its ring and the own share, and is '–' only while a line is unread", () => {
    // Just after a restart: the grid, the sun and the battery read, the house not yet.
    const early: HouseNow = { ...live(), house_kw: null, rest_kw: null, other_kw: null, car_kw: null, car_connected: false };
    const figures = nowFigures(early, house());
    expect(figures.house.kw).toBe(3.1);
    expect(figures.parts.map((part) => [part.role, part.kw])).toEqual([
      ["heat", 1.1],
      ["other", 2.0],
    ]);
    expect(ownShare(figures)).toBe(87);
    const block = renderNow({ doc: document, language: "sv", answer: house(), live: early, width: 400, idPrefix: "h", nowMs: NOW });
    expect(block.querySelector(".spotnav-now-house-kw")?.textContent).toBe("≈ 3,1 kW");
    expect(block.querySelector("[data-part='own']")?.textContent).toBe("87 % egen el");
    expect(block.querySelectorAll(".spotnav-now-ring")).toHaveLength(2);
    // A heat larger than the house had from the lines is cut to it.
    expect(nowFigures({ ...early, heat_kw: 9 }, house()).parts.map((part) => part.kw)).toEqual([3.1, 0]);
    // The grid unread: no house.
    const unread = nowFigures({ ...early, grid_kw: null }, house());
    expect(unread.house.kw).toBeNull();
    expect(renderNow({ doc: document, language: "sv", answer: house(), live: { ...early, grid_kw: null }, width: 400, idPrefix: "h", nowMs: NOW }).querySelector(".spotnav-now-house-kw")?.textContent).toBe("–");
    // A car plugged in whose draw is unread: not known either.
    expect(nowFigures({ ...early, car_connected: true }, house()).house.kw).toBeNull();
  });
});
