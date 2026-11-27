// The price chart, its marks, bands, strip and the plan preview.

import { VISUAL_CLASSES, CHART_DENSITY, PLAN_BAND_STYLE, focusLineColour } from "./visual-classes";

/** A CSS gradient runs across its stripes, so its angle is the line angle plus a quarter turn. */
const STRIP_HATCH_GRADIENT_ANGLE = PLAN_BAND_STYLE.hatchAngle + 90;

/**
 * The collapsed strip's hatch for one kind: the band colour in lines `hatchWidth` wide every
 * `hatchSpacing`, at the shared angle. The strip has no marks over it, so the lines are full strength.
 */
function stripHatch(kind: "installed" | "proposal"): string {
  const { hatchSpacing, hatchWidth } = PLAN_BAND_STYLE[kind];
  return (
    `repeating-linear-gradient(${STRIP_HATCH_GRADIENT_ANGLE}deg, ` +
    `var(--primary-color, #03a9f4) 0 ${hatchWidth}px, transparent ${hatchWidth}px ${hatchSpacing}px)`
  );
}

export const CHART_STYLES = `\
  .${VISUAL_CLASSES.graphSurface} {
    position: relative;
    width: 100%;
    max-width: 640px;
    min-width: 0;
    margin-top: 10px;
    user-select: none;
    -webkit-user-select: none;
    touch-action: pan-y;
    border-radius: 8px;
  }
  /*
   * The viewport is the measured box: the SVG fits it exactly and its height comes from
   * chartHeightForWidth in chart-render.ts, applied in JavaScript. Nothing below it can change its
   * size. No backticks here: this comment lives inside a template literal.
   */
  .${VISUAL_CLASSES.viewport} {
    display: block;
    width: 100%;
    min-width: 0;
  }
  .${VISUAL_CLASSES.viewport}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.svg} {
    display: block;
    width: 100%;
    height: 100%;
  }
  .${VISUAL_CLASSES.gridline} {
    stroke: var(--divider-color, #e0e0e0);
    stroke-width: ${CHART_DENSITY.normal.gridWidth};
  }
  .${VISUAL_CLASSES.axisLabel},
  .${VISUAL_CLASSES.tickLabel} {
    fill: var(--secondary-text-color, #727272);
    font-size: ${CHART_DENSITY.normal.axisFontPx}px;
  }
  /* A midnight inside the real-time axis: a quiet dashed divider, the new day named under it. */
  .${VISUAL_CLASSES.dayDivider} {
    stroke: var(--secondary-text-color, #727272);
    stroke-opacity: 0.55;
    stroke-dasharray: 3 3;
  }
  .${VISUAL_CLASSES.dayLabel} {
    font-weight: 600;
  }
  .${VISUAL_CLASSES.bands} {
    pointer-events: none;
  }
  .${VISUAL_CLASSES.marks} {
    pointer-events: none;
  }
  /* The sun: a light amber area with a line on its own kW scale, behind the price marks. Measured is solid, the
   * forecast dashed. The amber is the house card's sun, readable on a light and on a dark card. */
  .${VISUAL_CLASSES.sun} {
    pointer-events: none;
  }
  .${VISUAL_CLASSES.sunArea} {
    fill: var(--spotnav-energy-sun, #c18b00);
    fill-opacity: 0.2;
    stroke: none;
  }
  .${VISUAL_CLASSES.sunLine} {
    fill: none;
    stroke: var(--spotnav-energy-sun, #c18b00);
    stroke-width: 1.6;
    stroke-linejoin: round;
    stroke-linecap: round;
    vector-effect: non-scaling-stroke;
  }
  .${VISUAL_CLASSES.sunPeak} {
    fill: var(--spotnav-energy-sun, #c18b00);
    font-size: ${CHART_DENSITY.normal.axisFontPx}px;
    opacity: 0.85;
  }
  .${VISUAL_CLASSES.sunLine}.${VISUAL_CLASSES.sunForecast} {
    stroke-dasharray: 4 3;
  }
  .${VISUAL_CLASSES.summarySun} {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.sunSwatch} {
    display: inline-block;
    width: 0.8em;
    height: 0.6em;
    border-radius: 2px;
    background: color-mix(in srgb, var(--spotnav-energy-sun, #c18b00) 35%, transparent);
    border-top: 2px solid var(--spotnav-energy-sun, #c18b00);
  }
  .${VISUAL_CLASSES.cheap} {
    --spotnav-mark-colour: var(--spotnav-energy-cheap, #2e7d32);
  }
  /* Today's runs are solid; another day's are filled with a hatch pattern by the renderer instead. */
  .${VISUAL_CLASSES.bandInstalled}:not(.${VISUAL_CLASSES.bandTomorrow}) {
    fill: var(--primary-color, #03a9f4);
    opacity: ${PLAN_BAND_STYLE.installed.fillOpacity};
  }
  .${VISUAL_CLASSES.bandProposal}:not(.${VISUAL_CLASSES.bandTomorrow}) {
    fill: var(--primary-color, #03a9f4);
    opacity: ${PLAN_BAND_STYLE.proposal.fillOpacity};
  }
  .${VISUAL_CLASSES.bandHatch} {
    stroke: var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.bandHatch}[data-kind="installed"] {
    stroke-opacity: ${PLAN_BAND_STYLE.installed.hatchOpacity};
  }
  .${VISUAL_CLASSES.bandHatch}[data-kind="proposal"] {
    stroke-opacity: ${PLAN_BAND_STYLE.proposal.hatchOpacity};
  }
  .${VISUAL_CLASSES.now} {
    stroke: ${focusLineColour("now")};
    stroke-width: ${CHART_DENSITY.normal.focusWidth};
  }
  .${VISUAL_CLASSES.selection} {
    stroke: ${focusLineColour("selection")};
    stroke-width: ${CHART_DENSITY.normal.focusWidth};
    stroke-dasharray: 3 3;
  }
  .${VISUAL_CLASSES.point},
  .${VISUAL_CLASSES.segment} {
    fill: var(--spotnav-energy-cheap, #2e7d32);
    stroke: var(--spotnav-energy-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.segment} {
    stroke-linecap: round;
  }
  /* The chart_detail card option: the finer lines and text of CHART_DENSITY.detail. */
  .${VISUAL_CLASSES.svg}[data-density="detail"] .${VISUAL_CLASSES.gridline} {
    stroke-width: ${CHART_DENSITY.detail.gridWidth};
  }
  .${VISUAL_CLASSES.svg}[data-density="detail"] .${VISUAL_CLASSES.axisLabel},
  .${VISUAL_CLASSES.svg}[data-density="detail"] .${VISUAL_CLASSES.sunPeak},
  .${VISUAL_CLASSES.svg}[data-density="detail"] .${VISUAL_CLASSES.tickLabel} {
    font-size: ${CHART_DENSITY.detail.axisFontPx}px;
  }
  .${VISUAL_CLASSES.svg}[data-density="detail"] .${VISUAL_CLASSES.now},
  .${VISUAL_CLASSES.svg}[data-density="detail"] .${VISUAL_CLASSES.selection} {
    stroke-width: ${CHART_DENSITY.detail.focusWidth};
  }
  .${VISUAL_CLASSES.svg}[data-density="detail"] .${VISUAL_CLASSES.point} {
    stroke-width: ${CHART_DENSITY.detail.markStrokeWidth};
  }
  .${VISUAL_CLASSES.expensive},
  .${VISUAL_CLASSES.expensive}:is(circle) {
    fill: var(--spotnav-energy-expensive, #c62828);
    stroke: var(--spotnav-energy-expensive, #c62828);
  }
  .${VISUAL_CLASSES.tomorrow},
  .${VISUAL_CLASSES.tomorrow}:is(circle) {
    /*
     * Tomorrow keeps its cheap/expensive semantics for readout and accessibility, but the overlaid
     * series is neutral so colour distinguishes the foreground (today).
     */
    fill: var(--spotnav-tomorrow, var(--secondary-text-color, #727272));
    stroke: var(--spotnav-tomorrow, var(--secondary-text-color, #727272));
    opacity: 0.62;
  }
  .${VISUAL_CLASSES.current} {
    /* A colour accent only: the current mark keeps the ordinary radius so it cannot cover its neighbours. */
    fill: var(--spotnav-current, #03a9f4);
    stroke: var(--primary-color, #03a9f4);
  }
  @media (forced-colors: active) {
    .${VISUAL_CLASSES.now},
    .${VISUAL_CLASSES.selection} {
      stroke: CanvasText;
    }
  }
  .${VISUAL_CLASSES.readout} {
    box-sizing: border-box;
    height: 1.4em;
    margin: 2px 0 0;
    padding: 0 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.78rem;
    line-height: 1.4;
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.readoutHint} {
    margin-top: 2px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
  }
  .${VISUAL_CLASSES.legend} {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    margin-top: 4px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
  }
  /*
   * Max, min and now sit on one line above the plot (never over the y-axis labels), without wrapping
   * at 340 px; the line clips rather than wraps for a very long translation.
   */
  .${VISUAL_CLASSES.summary} {
    display: flex;
    flex-wrap: nowrap;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    margin: 0;
    padding: 0 2px;
    overflow: hidden;
    white-space: nowrap;
    font-size: 0.78rem;
    line-height: 1.2;
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.summaryExtremes} {
    display: flex;
    flex-wrap: nowrap;
    gap: 10px;
    min-width: 0;
  }
  /* The day's dearest and cheapest price as text: the price colours mixed a third toward the text colour, darker on a
   * light card and lighter on a dark one, so both read at 4.5:1. */
  .${VISUAL_CLASSES.summaryMax} {
    color: color-mix(in srgb, var(--spotnav-energy-expensive, #c62828) 65%, var(--primary-text-color, #212121));
  }
  .${VISUAL_CLASSES.summaryMin} {
    color: color-mix(in srgb, var(--spotnav-energy-cheap, #2e7d32) 65%, var(--primary-text-color, #212121));
  }
  .${VISUAL_CLASSES.summary}[role="button"] {
    cursor: pointer;
    border-radius: 4px;
  }
  .${VISUAL_CLASSES.summary}[role="button"]:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.summaryCurrent} {
    /* Pushed right with the chevron after it; the chevron is always there, so toggling moves nothing. */
    margin-left: auto;
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    font-weight: 600;
    color: var(--primary-text-color, #212121);
  }
  .${VISUAL_CLASSES.summaryArrow} {
    display: inline-block;
  }
  .${VISUAL_CLASSES.chartToggle} {
    flex: none;
    display: inline-block;
    width: 1em;
    text-align: center;
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.chartToggleGlyph} {
    display: inline-block;
    width: 1em;
  }
  .${VISUAL_CLASSES.viewport}[hidden],
  .${VISUAL_CLASSES.strip}[hidden] {
    display: none;
  }
  /*
   * The collapsed chart: a slim rounded track spanning the chart's own 24-hour axis, the plan's periods
   * as bars in the primary colour (the ones already over dimmed, tomorrow's hatched like the graph's),
   * the now line as the chart draws it, and the hour labels in the chart's label style.
   */
  .${VISUAL_CLASSES.strip} {
    margin: 6px 0 0;
    padding: 0 2px;
  }
  .${VISUAL_CLASSES.stripArea} {
    position: relative;
    padding: 3px 0;
  }
  .${VISUAL_CLASSES.stripTrack} {
    position: relative;
    height: 0.7rem;
    overflow: hidden;
    border-radius: 0.35rem;
    background: var(--secondary-background-color, #e5e5e5);
  }
  .${VISUAL_CLASSES.stripBar} {
    position: absolute;
    top: 0;
    bottom: 0;
    border-radius: 2px;
    background: var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.stripBar}[data-past="true"] {
    opacity: 0.4;
  }
  .${VISUAL_CLASSES.stripBar}[data-tomorrow="true"][data-kind="installed"] {
    background: ${stripHatch("installed")};
  }
  .${VISUAL_CLASSES.stripBar}[data-tomorrow="true"][data-kind="proposal"] {
    background: ${stripHatch("proposal")};
  }
  .${VISUAL_CLASSES.stripNow} {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 0;
    margin-left: -1px;
    border-left: 2px solid ${focusLineColour("now")};
  }
  .${VISUAL_CLASSES.stripLabels} {
    position: relative;
    height: 1.3em;
    margin-top: 2px;
    color: var(--secondary-text-color, #727272);
    font-size: 11px;
    line-height: 1.3;
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.stripLabel} {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
  }
  .${VISUAL_CLASSES.stripLabel}:first-child {
    transform: none;
  }
  .${VISUAL_CLASSES.stripLabel}:last-child {
    transform: translateX(-100%);
  }
  /* On the real-time axis the first label is not on the edge, so it centres like the others. */
  .${VISUAL_CLASSES.stripLabel}:first-child:not([data-edge]) {
    transform: translateX(-50%);
  }
  .${VISUAL_CLASSES.stripDivider} {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 0;
    border-left: 1px dashed var(--secondary-text-color, #727272);
    opacity: 0.6;
  }
  @media (forced-colors: active) {
    .${VISUAL_CLASSES.stripNow} {
      border-left-color: CanvasText;
    }
    .${VISUAL_CLASSES.stripBar} {
      background: Highlight;
    }
    .${VISUAL_CLASSES.stripBar}[data-tomorrow="true"][data-kind] {
      background: repeating-linear-gradient(${STRIP_HATCH_GRADIENT_ANGLE}deg, Highlight 0 2px, transparent 2px 5px);
    }
  }
  /*
   * The Plan dialog's preview: the plan a Save would make, a line and a strip of the hours to the departure under
   * it. Each quarter is coloured by price as the chart colours it; the planned ones are solid, the rest faint.
   * Dimmed while a newer draft's preview is awaited.
   */
  .${VISUAL_CLASSES.planPreview} {
    margin: 12px 0 0;
    transition: opacity 120ms ease-in-out;
  }
  .${VISUAL_CLASSES.planPreview}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.planPreview}[data-pending="true"] {
    opacity: 0.5;
  }
  .${VISUAL_CLASSES.planPreviewLine} {
    margin: 0;
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.planPreviewNote} {
    margin: 4px 0 0;
  }
  .${VISUAL_CLASSES.planPreviewNote}[data-tone="warning"] {
    color: var(--warning-color, #b26a00);
    font-weight: 500;
  }
  .${VISUAL_CLASSES.planPreviewStrip} {
    margin-top: 6px;
  }
  .${VISUAL_CLASSES.planPreviewCell} {
    position: absolute;
    top: 0;
    bottom: 0;
    background: var(--secondary-text-color, #727272);
    opacity: 0.22;
  }
  .${VISUAL_CLASSES.planPreviewCell}[data-price="cheap"] {
    background: var(--spotnav-energy-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.planPreviewCell}[data-price="dear"] {
    background: var(--spotnav-energy-expensive, #c62828);
  }
  .${VISUAL_CLASSES.planPreviewCell}[data-planned="true"] {
    opacity: 1;
  }
  .${VISUAL_CLASSES.planPreviewTick} {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
  }
  .${VISUAL_CLASSES.planPreviewTick}[data-edge="start"] {
    transform: none;
  }
  .${VISUAL_CLASSES.planPreviewTick}[data-edge="end"] {
    transform: translateX(-100%);
  }
  /*
   * The planned quarters as a band in the accent colour right under the price track: what is planned reads by
   * place and colour alone, in either theme and without telling red from green.
   */
  .${VISUAL_CLASSES.planPreviewBand} {
    position: relative;
    height: 0.3rem;
    margin-top: 3px;
  }
  .${VISUAL_CLASSES.planPreviewRun} {
    position: absolute;
    top: 0;
    bottom: 0;
    border-radius: 3px;
    background: var(--primary-color, #03a9f4);
  }
  @media (forced-colors: active) {
    .${VISUAL_CLASSES.planPreviewCell} {
      background: GrayText;
    }
    .${VISUAL_CLASSES.planPreviewCell}[data-planned="true"] {
      background: Highlight;
    }
    .${VISUAL_CLASSES.planPreviewRun} {
      background: Highlight;
    }
  }
`;
