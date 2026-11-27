// The energy palette (`energy-palette.ts`): one colour per meaning, light and dark, readable on both cards, and used
// for the same meaning by every picture in the cards.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ENERGY_PALETTE, ENERGY_PALETTE_STYLES, followTheme, type EnergyMeaning } from "../src/energy-palette";
import { roleColour } from "../src/house-flow";
import { NOW_STYLES } from "../src/house-now";
import { HOUSE_STYLES } from "../src/house-styles";
import { VISUAL_STYLES } from "../src/visual-styles";

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((at) => Number.parseInt(hex.slice(at, at + 2), 16) / 255);
  const [r, g, b] = channels.map((value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

function contrast(a: string, b: string): number {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high! + 0.05) / (low! + 0.05);
}

const MEANINGS = (Object.keys(ENERGY_PALETTE) as EnergyMeaning[]).filter((meaning) => meaning !== "idle" && meaning !== "ring");
const SRC = join(__dirname, "..", "src");

describe("the energy palette", () => {
  it("keeps 3:1 for every meaning on a light card in its light value and on a dark card in its dark one", () => {
    for (const meaning of MEANINGS) {
      expect(contrast(ENERGY_PALETTE[meaning].light, "#ffffff"), meaning).toBeGreaterThanOrEqual(3);
      expect(contrast(ENERGY_PALETTE[meaning].dark, "#1c1c1c"), meaning).toBeGreaterThanOrEqual(3);
    }
  });

  it("keeps the car magenta, apart from the house's violet and the heat's red", () => {
    const { car, house, heat } = ENERGY_PALETTE;
    expect(car.light).not.toBe(house.light);
    expect(car.light).not.toBe(heat.light);
    expect(car.light).toBe("#c2408f");
  });

  it("puts the tokens on each card's host, the dark values under Home Assistant's dark theme", () => {
    expect(ENERGY_PALETTE_STYLES).toMatch(/:host \{[^}]*--spotnav-energy-car: var\(--spotnav-car, #c2408f\);/);
    expect(ENERGY_PALETTE_STYLES).toMatch(/:host\(\[data-dark\]\) \{[^}]*--spotnav-energy-car: var\(--spotnav-car, #ec6fb5\);/);
    expect(VISUAL_STYLES.startsWith(ENERGY_PALETTE_STYLES)).toBe(true);
    const host = document.createElement("div");
    followTheme(host, { themes: { darkMode: true } });
    expect(host.hasAttribute("data-dark")).toBe(true);
    followTheme(host, { themes: { darkMode: false } });
    expect(host.hasAttribute("data-dark")).toBe(false);
    followTheme(host, {});
    expect(host.hasAttribute("data-dark")).toBe(false);
  });

  it("is what every picture draws a meaning in", () => {
    // The house card's series and the energy flow's blocks read the palette.
    for (const [series, meaning] of [
      ["house", "house"],
      ["sun", "sun"],
      ["battery", "battery"],
      ["sell", "export"],
      ["grid", "import"],
      ["car", "car"],
      ["heat", "heat"],
    ]) {
      expect(HOUSE_STYLES, series).toContain(`--spotnav-${series}-colour: var(--spotnav-energy-${meaning},`);
    }
    // What is sold is the export's in the energy flow, what is bought the grid's.
    expect(roleColour("grid", "use")).toBe("var(--spotnav-flow-export)");
    expect(roleColour("grid", "source")).toBe("var(--spotnav-flow-grid)");
    expect(HOUSE_STYLES).toContain("--spotnav-flow-export: var(--spotnav-flow-export-colour, color-mix(in srgb, var(--spotnav-sell-colour)");
    // The Now tab's tones.
    for (const meaning of ["sun", "import", "export", "battery", "car", "house", "heat", "pool", "other"]) {
      expect(NOW_STYLES, meaning).toContain(`--spotnav-now-tone: var(--spotnav-energy-${meaning});`);
    }
    // The charger card's graph: the sun and the price bands.
    expect(VISUAL_STYLES).toContain("var(--spotnav-energy-cheap, ");
    expect(VISUAL_STYLES).toContain("var(--spotnav-energy-expensive, ");
    expect(VISUAL_STYLES).toContain("var(--spotnav-energy-sun, ");
    expect(VISUAL_STYLES.slice(ENERGY_PALETTE_STYLES.length)).not.toMatch(/var\(--spotnav-(cheap|expensive|sun),/);
    // No card keeps the car's old orange or a price band in Home Assistant's status colours.
    for (const name of readdirSync(SRC).filter((file) => file.endsWith(".ts"))) {
      const source = readFileSync(join(SRC, name), "utf8");
      expect(source, name).not.toMatch(/#f08c3a|#ffa86b|#e07b2e/i);
      expect(source, name).not.toMatch(/--spotnav-plan-(cheap|mid|dear): color-mix\(in srgb, var\(--(success|warning|error)-color/);
    }
    const heat = readFileSync(join(SRC, "heat-styles.ts"), "utf8");
    expect(heat).toContain('[data-mark="car"] { fill: var(--spotnav-energy-car, #c2408f); }');
    const loads = readFileSync(join(SRC, "house-loads.ts"), "utf8");
    expect(loads).toContain('load.source === "heat_pump" ? "var(--spotnav-energy-heat)"');
  });
});
