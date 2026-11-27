// The Now tab's captions in the card's own stylesheet order: the state line grey, today's line in its node's colour,
// never outlined or shadowed (an ancestor's stroke, such as the charts' now line's, once outlined them all alike).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { EARN_STYLES } from "../src/house-earnings";
import { HEAT_STYLES } from "../src/heat-styles";
import { LOADS_STYLES } from "../src/house-loads";
import { decodeHouse, type HouseAnswer } from "../src/house-model";
import { decodeHouseNow, NOW_STYLES, type HouseNow } from "../src/house-now";
import { PERIOD_VIEW_STYLES } from "../src/house-period-view";
import { PLAN_STYLES } from "../src/house-plan";
import { HOUSE_STYLES } from "../src/house-styles";
import { TAB_STYLES } from "../src/house-tabs";
import { houseBody } from "../src/house-view";
import { NOT_READY_STYLES } from "../src/not-ready-sheet";
import { PERIOD_STYLES } from "../src/period-history";
import { VISUAL_STYLES } from "../src/visual-styles";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): unknown => JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8"));

function mounted(dark: boolean): ShadowRoot {
  const answer = decodeHouse(read("house", "villa_battery_sun.json"));
  const live = decodeHouseNow(read("house_now", "villa_now.json"));
  if (!answer.ok || !live.ok) {
    throw new Error("the fixtures must decode");
  }
  const host = document.createElement("div");
  host.toggleAttribute("data-dark", dark);
  document.body.append(host);
  const root = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  // The house card's own order (`house-card.ts`).
  style.textContent = VISUAL_STYLES + HOUSE_STYLES + HEAT_STYLES + PERIOD_STYLES + LOADS_STYLES + TAB_STYLES + NOW_STYLES + PERIOD_VIEW_STYLES + EARN_STYLES + PLAN_STYLES + NOT_READY_STYLES;
  root.append(style);
  root.append(
    houseBody({
      doc: document,
      language: "sv",
      state: { kind: "ready", answer: answer.value as HouseAnswer },
      ui: { selected: null, tab: "now" },
      handlers: { onSelect: () => undefined } as never,
      width: 340,
      nowMs: Date.parse("2026-11-13T16:20:00+00:00"),
      idPrefix: "c",
      tabs: { live: live.value as HouseNow, history: null, day: null, today: "2026-11-13" } as never,
    }),
  );
  return root;
}

const unstroked = (value: string): boolean => value === "none" || value === "" || value === "rgba(0, 0, 0, 0)" || value === "transparent";

describe("the Now tab's captions", () => {
  for (const dark of [false, true]) {
    it(`are grey for the state and the node's colour for today, crisp, on a ${dark ? "dark" : "light"} card`, () => {
      const root = mounted(dark);
      // No ancestor carries the charts' now line's class (its stroke is inherited).
      expect(root.querySelector(".spotnav-now")).toBeNull();
      expect(unstroked(getComputedStyle(root.querySelector(".spotnav-now-schematic")!).getPropertyValue("stroke"))).toBe(true);
      for (const role of ["sun", "grid", "battery", "car"]) {
        const state = getComputedStyle(root.querySelector(`[data-k='state-${role}']`)!);
        const today = getComputedStyle(root.querySelector(`[data-k='today-${role}']`)!);
        expect(state.getPropertyValue("fill"), role).toBe("var(--secondary-text-color, #727272)");
        expect(today.getPropertyValue("fill"), role).toMatch(/^color-mix\(in srgb, var\(--spotnav-now-tone/);
        expect(today.getPropertyValue("fill"), role).not.toBe(state.getPropertyValue("fill"));
        // The tone is the node's own.
        const tone = role === "grid" ? "import" : role;
        expect(today.getPropertyValue("--spotnav-now-tone").trim(), role).toBe(`var(--spotnav-energy-${tone})`);
        for (const style of [state, today]) {
          expect(unstroked(style.getPropertyValue("stroke")), role).toBe(true);
          expect(style.getPropertyValue("filter"), role).toBe("none");
          // Whole screen pixels, the drawing at its own width (no scaling to blur them).
          expect(style.getPropertyValue("font-size"), role).toBe("11px");
        }
      }
      const house = getComputedStyle(root.querySelector("[data-k='today-house']")!);
      expect(house.getPropertyValue("--spotnav-now-tone").trim()).toBe("var(--spotnav-energy-house)");
      expect(unstroked(house.getPropertyValue("stroke"))).toBe(true);
      const drawing = root.querySelector<SVGSVGElement>(".spotnav-now-schematic")!;
      expect(drawing.getAttribute("viewBox")!.split(" ")[2]).toBe("340");
      expect(drawing.style.maxWidth).toBe("340px");
    });
  }
});
