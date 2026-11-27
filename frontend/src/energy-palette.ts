/**
 * One palette of what energy is, for every card and every picture in it (2026-11-27): the sun, the grid's import and
 * what is sold, the battery, the car, the house and its parts (heat, the pool with the spa and the sauna, the rest),
 * and the price bands. The Now tab, the energy flow, the house and the heat pies, the Plan tab's layers, the period
 * charts, the heat card's charts and the charger card's graph all draw a meaning in its token, so a colour means one
 * thing wherever it is seen. The car is magenta, apart from the house's violet and the heat's red.
 *
 * Each token has a light and a dark value: the dark one where Home Assistant says its theme is dark
 * (`hass.themes.darkMode`, put on the card as `data-dark` by `followTheme`), brighter so it holds its contrast on a
 * dark card. A theme may set its own through the older custom properties each token reads first (`--spotnav-sun`,
 * `--spotnav-cheap` and so on). Words written in a token's colour are mixed toward the text colour where they are
 * drawn, so they read at 4.5:1 on both. Every meaning's own value keeps 3:1 against a white card (light) and a dark
 * one (dark); the approved drawing's amber sun, mid price and pool blue are a step deeper on a light card for it.
 */

export type EnergyMeaning =
  | "sun"
  | "import"
  | "export"
  | "battery"
  | "car"
  | "house"
  | "heat"
  | "pool"
  | "other"
  | "cheap"
  | "mid"
  | "expensive"
  | "idle"
  | "ring";

/** Each meaning's light and dark value, and the theme's own property read first where there is one. */
export const ENERGY_PALETTE: Record<EnergyMeaning, { light: string; dark: string; theme?: string }> = {
  sun: { light: "#c18b00", dark: "#f2b705", theme: "--spotnav-sun" },
  import: { light: "#3b82f6", dark: "#5b9bff", theme: "--spotnav-grid" },
  export: { light: "#16a34a", dark: "#34c26b", theme: "--spotnav-sell" },
  battery: { light: "#0f9f8f", dark: "#22c3b0", theme: "--spotnav-battery" },
  car: { light: "#c2408f", dark: "#ec6fb5", theme: "--spotnav-car" },
  house: { light: "#6d5bd0", dark: "#9a8cf0", theme: "--spotnav-house" },
  heat: { light: "#e2554a", dark: "#f06a5f", theme: "--spotnav-heat" },
  pool: { light: "#289ed0", dark: "#4bb9e6", theme: "--spotnav-pool" },
  other: { light: "#8796ab", dark: "#7d8a9a", theme: "--spotnav-other" },
  cheap: { light: "#16a34a", dark: "#34c26b", theme: "--spotnav-cheap" },
  mid: { light: "#bf8b00", dark: "#e7b416", theme: "--spotnav-mid" },
  expensive: { light: "#dc2626", dark: "#f2564f", theme: "--spotnav-expensive" },
  idle: { light: "#c9d1db", dark: "#36404c" },
  ring: { light: "#e6eaf0", dark: "#2a323d" },
};

function declarations(side: "light" | "dark"): string {
  return (Object.keys(ENERGY_PALETTE) as EnergyMeaning[])
    .map((meaning) => {
      const entry = ENERGY_PALETTE[meaning];
      const value = entry[side];
      return `    --spotnav-energy-${meaning}: ${entry.theme === undefined ? value : `var(${entry.theme}, ${value})`};`;
    })
    .join("\n");
}

/** The tokens on each card's host, the dark values where the theme is dark. */
export const ENERGY_PALETTE_STYLES = `
  :host {
${declarations("light")}
  }
  :host([data-dark]) {
${declarations("dark")}
  }
`;

/** Put Home Assistant's dark mode on a card's host (`data-dark`), so the palette takes its dark values. */
export function followTheme(host: HTMLElement, hass: { themes?: { darkMode?: boolean } | null } | null | undefined): void {
  host.toggleAttribute("data-dark", hass?.themes?.darkMode === true);
}
