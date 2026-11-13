// The house card's own classes and styles, on top of the charging card's (`visual-styles.ts`), which the house card
// loads too: the card's frame, the chart's gridlines, axis text and now line, the muted text and the toggle button
// are the same. The three series have a colour each, as custom properties a theme may set: the house in the theme's
// primary colour, the sun in amber, the battery in violet; each keeps 3:1 against a light and a dark card.

import { CHART_DENSITY, PLAN_BAND_STYLE } from "./visual-styles";

export const HOUSE_CLASSES = {
  card: "spotnav-house",
  head: "spotnav-house-head",
  title: "spotnav-house-title",
  chip: "spotnav-house-chip",
  sub: "spotnav-house-sub",
  surface: "spotnav-house-surface",
  chart: "spotnav-house-chart",
  bands: "spotnav-house-bands",
  band: "spotnav-house-band",
  bars: "spotnav-house-bars",
  bar: "spotnav-house-bar",
  barForecast: "spotnav-house-bar-forecast",
  forecastTick: "spotnav-house-forecast-tick",
  sunArea: "spotnav-house-sun-area",
  sunRaw: "spotnav-house-sun-raw",
  soc: "spotnav-house-soc",
  socPlan: "spotnav-house-soc-plan",
  legend: "spotnav-house-legend",
  legendItem: "spotnav-house-legend-item",
  swatch: "spotnav-house-swatch",
  line: "spotnav-house-line",
  detailsToggle: "spotnav-house-details-toggle",
  details: "spotnav-house-details",
  section: "spotnav-house-section",
  heading: "spotnav-house-heading",
  table: "spotnav-house-table",
  facts: "spotnav-house-facts",
  note: "spotnav-house-note",
} as const;

const H = HOUSE_CLASSES;

export const HOUSE_STYLES = `
  .${H.card} {
    --spotnav-house-colour: var(--spotnav-house, var(--primary-color, #03a9f4));
    --spotnav-sun-colour: var(--spotnav-sun, #e8a200);
    --spotnav-battery-colour: var(--spotnav-battery, #8e63ce);
  }
  .${H.head} {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 8px;
    min-width: 0;
  }
  .${H.title} {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 500;
    flex: 1 1 auto;
  }
  .${H.chip} {
    flex: none;
    padding: 1px 8px;
    border-radius: 10px;
    border: 1px solid var(--divider-color, #e0e0e0);
    color: var(--secondary-text-color, #727272);
    font-size: 0.75rem;
    white-space: nowrap;
  }
  .${H.sub} {
    margin: 2px 0 0;
  }
  .${H.surface} {
    position: relative;
    width: 100%;
    min-width: 0;
    margin-top: 8px;
    touch-action: pan-y;
    user-select: none;
    -webkit-user-select: none;
    border-radius: 8px;
  }
  .${H.surface}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${H.band} {
    fill: var(--spotnav-battery-colour);
    opacity: ${PLAN_BAND_STYLE.installed.fillOpacity};
  }
  .${H.band}[data-action="export_discharge"] {
    opacity: ${PLAN_BAND_STYLE.proposal.fillOpacity};
  }
  .${H.bar} {
    fill: var(--spotnav-house-colour);
    opacity: 0.85;
  }
  .${H.barForecast} {
    opacity: 0.32;
  }
  .${H.forecastTick} {
    stroke: var(--primary-text-color, #212121);
    stroke-width: 1.5;
    stroke-linecap: round;
    opacity: 0.75;
  }
  .${H.sunArea} {
    fill: var(--spotnav-sun-colour);
    opacity: 0.28;
  }
  .${H.sunRaw} {
    fill: none;
    stroke: var(--spotnav-sun-colour);
    stroke-width: 1.5;
    stroke-dasharray: 1 3;
    stroke-linecap: round;
  }
  .${H.soc} {
    fill: none;
    stroke: var(--spotnav-battery-colour);
    stroke-width: 2;
    stroke-linejoin: round;
  }
  .${H.socPlan} {
    stroke-dasharray: 5 3;
  }
  .${H.legend} {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 12px;
    margin: 4px 0 0;
    padding: 0;
    list-style: none;
    font-size: 0.78rem;
    color: var(--secondary-text-color, #727272);
  }
  .${H.legendItem} {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .${H.swatch} {
    display: inline-block;
    width: 14px;
    height: 8px;
    border-radius: 2px;
  }
  .${H.swatch}[data-series="house"] {
    background: var(--spotnav-house-colour);
    opacity: 0.85;
  }
  .${H.swatch}[data-series="forecast"] {
    height: 0;
    border-top: 2px solid var(--primary-text-color, #212121);
    border-radius: 0;
    opacity: 0.75;
  }
  .${H.swatch}[data-series="sun"] {
    background: var(--spotnav-sun-colour);
    opacity: 0.5;
  }
  .${H.swatch}[data-series="battery"] {
    height: 0;
    border-top: 2px solid var(--spotnav-battery-colour);
    border-radius: 0;
  }
  .${H.swatch}[data-series="charge"] {
    background: var(--spotnav-battery-colour);
    opacity: 0.3;
  }
  .${H.line} {
    margin: 6px 0 0;
    font-size: 0.88rem;
    overflow-wrap: break-word;
  }
  .${H.line} + .${H.line} {
    margin-top: 2px;
  }
  .${H.detailsToggle} {
    margin-top: 8px;
    min-height: 32px;
    padding: 2px 12px;
  }
  .${H.details} {
    margin-top: 8px;
    padding-top: 6px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .${H.section} + .${H.section} {
    margin-top: 10px;
  }
  .${H.heading} {
    margin: 0 0 4px;
    font-size: 0.85rem;
    font-weight: 600;
  }
  .${H.table} {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;
  }
  .${H.table} th,
  .${H.table} td {
    padding: 2px 4px;
    text-align: end;
    white-space: nowrap;
  }
  .${H.table} th:first-child,
  .${H.table} td:first-child {
    text-align: start;
    white-space: normal;
  }
  .${H.table} thead th {
    font-weight: 500;
    color: var(--secondary-text-color, #727272);
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .${H.table} tr[data-in-force] td:first-child {
    font-weight: 600;
  }
  .${H.facts} {
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: 0.8rem;
  }
  .${H.facts} li + li {
    margin-top: 2px;
  }
  .${H.note} {
    margin: 6px 0 0;
    font-size: 0.78rem;
  }
  @media (forced-colors: active) {
    .${H.bar},
    .${H.sunArea},
    .${H.band} {
      fill: CanvasText;
    }
    .${H.soc},
    .${H.sunRaw},
    .${H.forecastTick} {
      stroke: CanvasText;
    }
  }
  .${H.chart} text {
    font-size: ${CHART_DENSITY.normal.axisFontPx}px;
  }
`;
