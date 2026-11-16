// The house card's own classes and styles, on top of the charging card's (`visual-styles.ts`), which the house card
// loads too: the card's frame, the chart's gridlines, axis text and now line, the muted text and the toggle button
// are the same. The series have a colour each, as custom properties a theme may set: the house in the theme's
// primary colour, the sun in amber, the battery in teal and the grid in blue-violet (as in the energy flow), selling
// in raspberry; each keeps 3:1 against a light and a dark card.

import { CHART_DENSITY, PLAN_BAND_STYLE } from "./visual-styles";

export const HOUSE_CLASSES = {
  card: "spotnav-house",
  head: "spotnav-house-head",
  title: "spotnav-house-title",
  chip: "spotnav-house-chip",
  chipButton: "spotnav-house-chip-button",
  testExplain: "spotnav-house-test-explain",
  sub: "spotnav-house-sub",
  surface: "spotnav-house-surface",
  chart: "spotnav-house-chart",
  bands: "spotnav-house-bands",
  band: "spotnav-house-band",
  bars: "spotnav-house-bars",
  bar: "spotnav-house-bar",
  barForecast: "spotnav-house-bar-forecast",
  forecastTick: "spotnav-house-forecast-tick",
  carBar: "spotnav-house-car-bar",
  heatBar: "spotnav-house-heat-bar",
  heatForecast: "spotnav-house-heat-forecast",
  sunArea: "spotnav-house-sun-area",
  sunRaw: "spotnav-house-sun-raw",
  soc: "spotnav-house-soc",
  socPlan: "spotnav-house-soc-plan",
  legend: "spotnav-house-legend",
  legendItem: "spotnav-house-legend-item",
  legendButton: "spotnav-house-legend-button",
  swatch: "spotnav-house-swatch",
  line: "spotnav-house-line",
  detailsToggle: "spotnav-house-details-toggle",
  details: "spotnav-house-details",
  section: "spotnav-house-section",
  heading: "spotnav-house-heading",
  table: "spotnav-house-table",
  facts: "spotnav-house-facts",
  note: "spotnav-house-note",
  priceBuy: "spotnav-house-price-buy",
  priceSell: "spotnav-house-price-sell",
  nowLabel: "spotnav-house-now-label",
  views: "spotnav-house-views",
  today: "spotnav-house-today",
  todayDetail: "spotnav-house-today-detail",
  flow: "spotnav-house-flow",
  flowRow: "spotnav-house-flow-row",
  flowLabel: "spotnav-house-flow-label",
  flowBar: "spotnav-house-flow-bar",
  flowPart: "spotnav-house-flow-part",
  flowKey: "spotnav-house-flow-key",
  flowChart: "spotnav-house-flow-chart",
  readout: "spotnav-house-readout",
  compare: "spotnav-house-compare",
  compareHead: "spotnav-house-compare-head",
  compareGrid: "spotnav-house-compare-grid",
  compareRow: "spotnav-house-compare-row",
  compareName: "spotnav-house-compare-name",
  compareTrack: "spotnav-house-compare-track",
  compareFill: "spotnav-house-compare-fill",
  compareValue: "spotnav-house-compare-value",
  base: "spotnav-house-base",
  baseRow: "spotnav-house-base-row",
  baseValue: "spotnav-house-base-value",
  baseNote: "spotnav-house-base-note",
} as const;

const H = HOUSE_CLASSES;

export const HOUSE_STYLES = `
  /* The colours, also in the history dialog, which lives outside the card. */
  .${H.card},
  .spotnav-history-body {
    --spotnav-house-colour: var(--spotnav-house, var(--primary-color, #03a9f4));
    --spotnav-sun-colour: var(--spotnav-sun, #e8a200);
    --spotnav-battery-colour: var(--spotnav-battery, #129c90);
    --spotnav-sell-colour: var(--spotnav-sell, #c2185b);
    --spotnav-grid-colour: var(--spotnav-grid, #5c6bd6);
    --spotnav-flow-solar: var(--spotnav-flow-sun, #f2c94c);
    --spotnav-flow-battery: var(--spotnav-flow-battery-colour, #4fd1c5);
    --spotnav-flow-grid: var(--spotnav-flow-grid-colour, #8c9cf5);
    --spotnav-flow-house: var(--spotnav-flow-house-colour, #b98cf0);
    --spotnav-flow-car: var(--spotnav-flow-car-colour, #ffa86b);
    --spotnav-heat-colour: var(--spotnav-heat, #e0453a);
    --spotnav-flow-heat: var(--spotnav-flow-heat-colour, #ff8f87);
    --spotnav-flow-other: #b0bec5;
  }
  .${H.compare} {
    margin-top: 8px;
  }
  .${H.compareHead} {
    margin: 0 0 2px;
    font-size: 0.78rem;
  }
  .${H.compareRow} {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(40px, 22%) auto;
    align-items: center;
    gap: 8px;
    padding: 2px 0;
    font-size: 0.86rem;
  }
  .${H.compareRow}[data-in-force] .${H.compareName} {
    font-weight: 600;
  }
  .${H.compareName} {
    min-width: 0;
    overflow-wrap: break-word;
  }
  .${H.compareTrack} {
    height: 6px;
    border-radius: 3px;
    background: var(--divider-color, #e0e0e0);
    overflow: hidden;
  }
  .${H.compareFill} {
    display: block;
    height: 100%;
    border-radius: 3px;
    background: linear-gradient(90deg, color-mix(in srgb, var(--spotnav-battery-colour) 60%, transparent), var(--spotnav-battery-colour));
  }
  .${H.compareFill}[data-sign="minus"] {
    background: var(--warning-color, #ffa600);
  }
  .${H.compareValue} {
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .${H.base} {
    margin-top: 8px;
  }
  .${H.baseRow} {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    padding-top: 4px;
    font-size: 0.86rem;
  }
  .${H.baseValue} {
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .${H.baseNote} {
    margin: 0;
    font-size: 0.78rem;
  }
  .${H.readout} {
    height: auto;
    min-height: 2.8em;
    margin: 4px 0 2px;
    white-space: normal;
    text-overflow: clip;
  }
  .${H.flow}[data-part="energy-flow"] {
    margin-top: 12px;
  }
  .${H.flowChart} {
    display: block;
    max-width: 100%;
    margin-top: 6px;
  }
  .${H.flowChart} .spotnav-house-flow-band {
    opacity: 0.55;
  }
  .${H.flowChart} .spotnav-house-flow-block {
    filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.28));
  }
  .${H.flowChart} .spotnav-house-flow-pill {
    fill: #ffffff;
    fill-opacity: 0.62;
  }
  .${H.flowChart} text {
    fill: #15151c;
    font-variant-numeric: tabular-nums;
  }
  .${H.flowChart} .spotnav-house-flow-name {
    font-size: 11px;
    font-weight: 600;
  }
  .${H.flowChart} .spotnav-house-flow-value {
    font-size: 15px;
    font-weight: 700;
  }
  .${H.flowChart} .spotnav-house-flow-value-long {
    font-size: 13px;
    font-weight: 700;
  }
  .${H.flowChart} .spotnav-house-flow-share {
    font-size: 11px;
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
  /* The test chip is a button: tapping it shows what test means. */
  .${H.chipButton} {
    position: relative;
    background: none;
    font-family: inherit;
    line-height: 1.4;
    cursor: pointer;
  }
  /* A finger-sized target around the small chip. */
  .${H.chipButton}::after {
    content: "";
    position: absolute;
    inset: -10px -4px;
  }
  .${H.chipButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${H.testExplain} {
    margin: 6px 0 0;
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
    fill: var(--spotnav-grid-colour);
    opacity: ${PLAN_BAND_STYLE.installed.fillOpacity};
  }
  .${H.band}[data-action="export_discharge"] {
    fill: var(--spotnav-sell-colour);
    opacity: ${PLAN_BAND_STYLE.installed.fillOpacity + 0.04};
  }
  .${H.priceBuy},
  .${H.priceSell} {
    fill: none;
    stroke-linejoin: round;
  }
  .${H.priceBuy} {
    stroke: var(--primary-text-color, #212121);
    stroke-width: 1.5;
    opacity: 0.8;
  }
  .${H.priceSell} {
    stroke: var(--spotnav-sell-colour);
    stroke-width: 1.25;
  }
  .${H.nowLabel} {
    font-weight: 600;
    fill: var(--primary-text-color, #212121);
  }
  .${H.views} {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }
  .${H.views} > button {
    min-height: 30px;
    padding: 2px 10px;
    font-size: 0.82rem;
  }
  /* The day's headline: what the electricity cost so far, with the battery's earnings under it. */
  .${H.today} {
    margin: 6px 0 0;
    font-size: 1.25rem;
    font-weight: 500;
    line-height: 1.3;
    overflow-wrap: break-word;
  }
  .${H.todayDetail} {
    margin: 2px 0 0;
    overflow-wrap: break-word;
  }
  .${H.flowRow} {
    display: grid;
    grid-template-columns: 3.2rem 1fr;
    align-items: center;
    gap: 6px;
    margin-top: 4px;
  }
  .${H.flowLabel} {
    font-size: 0.78rem;
    color: var(--secondary-text-color, #727272);
  }
  .${H.flowBar} {
    display: flex;
    height: 14px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--divider-color, #e0e0e0);
  }
  .${H.flowPart}[data-part="solar"] {
    background: var(--spotnav-sun-colour);
  }
  .${H.flowPart}[data-part="battery"] {
    background: var(--spotnav-battery-colour);
  }
  .${H.flowPart}[data-part="grid"] {
    background: var(--spotnav-grid-colour);
  }
  .${H.flowPart}[data-part="load"] {
    background: var(--spotnav-house-colour);
  }
  .${H.flowKey} {
    margin: 2px 0 0 3.6rem;
    font-size: 0.76rem;
    color: var(--secondary-text-color, #727272);
    font-variant-numeric: tabular-nums;
  }
  .${H.bar} {
    fill: var(--spotnav-house-colour);
    opacity: 0.85;
  }
  /* The forecast ahead: lighter than the measured bars, yet plain to see on a light and a dark card. */
  .${H.barForecast} {
    opacity: 0.55;
  }
  .${H.carBar} {
    fill: var(--spotnav-flow-car);
    opacity: 0.9;
  }
  .${H.heatBar} {
    fill: var(--spotnav-heat-colour);
    opacity: 0.9;
  }
  .${H.heatBar}.${H.heatForecast} {
    opacity: 0.55;
  }
  .${H.forecastTick} {
    stroke: var(--primary-text-color, #212121);
    stroke-width: 1.5;
    stroke-linecap: round;
    opacity: 0.75;
  }
  .${H.sunArea} {
    fill: var(--spotnav-sun-colour);
    opacity: 0.6;
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
  .${H.legendButton} {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin: 0;
    padding: 4px 2px;
    border: 0;
    border-radius: 4px;
    background: none;
    color: inherit;
    font: inherit;
    cursor: pointer;
  }
  .${H.legendButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .${H.legendButton}[aria-pressed="false"] {
    text-decoration: line-through;
    opacity: 0.5;
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
  .${H.swatch}[data-series="heat"] {
    background: var(--spotnav-heat-colour);
  }
  .${H.swatch}[data-series="car"] {
    background: var(--spotnav-flow-car);
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
    background: var(--spotnav-grid-colour);
    opacity: 0.3;
  }
  .${H.swatch}[data-series="sell"] {
    background: var(--spotnav-sell-colour);
    opacity: 0.35;
  }
  .${H.swatch}[data-series="buy"] {
    height: 0;
    border-top: 2px solid var(--primary-text-color, #212121);
    border-radius: 0;
    opacity: 0.8;
  }
  .${H.swatch}[data-series="sellPrice"] {
    height: 0;
    border-top: 2px solid var(--spotnav-sell-colour);
    border-radius: 0;
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
    .${H.heatBar},
    .${H.carBar},
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
