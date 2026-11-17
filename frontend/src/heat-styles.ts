// The heat card's own classes and styles, on top of the charging card's (`visual-styles.ts`) and the house card's
// (`house-styles.ts`: the header, the chip, the lines and notes), which the heat card loads too. The day chart's
// colours are custom properties a theme may set (`--spotnav-heat-lower-colour`, `--spotnav-heat-relay-colour`, `--spotnav-heat-raise-colour`,
// `--spotnav-heat-water-colour`, `--spotnav-heat-indoor-colour`), each with its own fallback.

export const HEAT_CLASSES = {
  card: "spotnav-heat",
  settingsButton: "spotnav-heat-settings-button",
  dialWrap: "spotnav-heat-dial-wrap",
  dial: "spotnav-heat-dial",
  dialTrack: "spotnav-heat-dial-track",
  dialBand: "spotnav-heat-dial-band",
  dialValue: "spotnav-heat-dial-value",
  dialMeasured: "spotnav-heat-dial-measured",
  dialFigure: "spotnav-heat-dial-figure",
  dialSub: "spotnav-heat-dial-sub",
  dialStep: "spotnav-heat-dial-step",
  measured: "spotnav-heat-measured",
  modes: "spotnav-heat-modes",
  mode: "spotnav-heat-mode",
  awayLine: "spotnav-heat-away-line",
  status: "spotnav-heat-status",
  shadow: "spotnav-heat-shadow",
  daySection: "spotnav-heat-day-section",
  dayToggle: "spotnav-heat-day-toggle",
  dayTitle: "spotnav-heat-day-title",
  daySurface: "spotnav-heat-day-surface",
  day: "spotnav-heat-day",
  dayReadout: "spotnav-heat-day-readout",
  dayColumn: "spotnav-heat-day-column",
  dayMark: "spotnav-heat-day-mark",
  dayAxis: "spotnav-heat-day-axis",
  dayBaseline: "spotnav-heat-day-baseline",
  dayNow: "spotnav-heat-day-now",
  dayCross: "spotnav-heat-day-cross",
  dayDot: "spotnav-heat-day-dot",
  indoorComfort: "spotnav-heat-indoor-comfort",
  indoorEdge: "spotnav-heat-indoor-edge",
  indoorSetpoint: "spotnav-heat-indoor-setpoint",
  indoorLine: "spotnav-heat-indoor-line",
  legend: "spotnav-heat-legend",
  legendItem: "spotnav-heat-legend-item",
  swatch: "spotnav-heat-swatch",
  error: "spotnav-heat-error",
  section: "spotnav-heat-section",
  editorField: "spotnav-heat-editor-field",
} as const;

const X = HEAT_CLASSES;

export const HEAT_STYLES = `
  .${X.card} {
    --spotnav-heat-colour: var(--spotnav-heat, #e5554b);
  }
  .${X.settingsButton} {
    flex: none;
    min-width: 40px;
    min-height: 40px;
    padding: 8px;
    border: none;
    background: none;
    color: var(--secondary-text-color, #727272);
    cursor: pointer;
    border-radius: 50%;
  }
  .${X.settingsButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
  }
  .${X.dialWrap} {
    display: grid;
    grid-template-columns: 44px minmax(0, 1fr) 44px;
    align-items: center;
    gap: 4px;
    margin-top: 8px;
  }
  .${X.dial} {
    display: block;
    width: 100%;
    max-width: 220px;
    margin: 0 auto;
    overflow: visible;
  }
  .${X.dialTrack} {
    fill: none;
    stroke: var(--divider-color, #e0e0e0);
    stroke-width: 10;
    stroke-linecap: round;
  }
  .${X.dialBand} {
    fill: none;
    stroke: color-mix(in srgb, var(--spotnav-heat-colour) 30%, transparent);
    stroke-width: 18;
  }
  .${X.dialValue} {
    fill: none;
    stroke: var(--spotnav-heat-colour);
    stroke-width: 10;
    stroke-linecap: round;
  }
  .${X.dialMeasured} {
    fill: var(--card-background-color, #fff);
    stroke: var(--primary-text-color, #212121);
    stroke-width: 2.5;
  }
  .${X.dialFigure} {
    fill: var(--primary-text-color, #212121);
    font-size: 40px;
    font-weight: 500;
    text-anchor: middle;
    font-variant-numeric: tabular-nums;
  }
  .${X.dialSub} {
    fill: var(--secondary-text-color, #727272);
    font-size: 13px;
    text-anchor: middle;
  }
  .${X.dialStep} {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
    font-size: 1.4rem;
    line-height: 1;
    cursor: pointer;
  }
  .${X.dialStep}:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .${X.dialStep}:focus-visible,
  .${X.mode}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${X.measured} {
    margin: 0;
    text-align: center;
    font-size: 0.9rem;
    overflow-wrap: break-word;
  }
  .${X.modes} {
    display: flex;
    justify-content: center;
    gap: 0;
    margin-top: 10px;
  }
  .${X.mode} {
    min-height: 40px;
    padding: 4px 18px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: none;
    color: var(--primary-text-color, #212121);
    font: inherit;
    font-size: 0.9rem;
    cursor: pointer;
  }
  .${X.mode}:first-child {
    border-radius: 20px 0 0 20px;
  }
  .${X.mode}:last-child {
    border-radius: 0 20px 20px 0;
    border-left: none;
  }
  .${X.mode}[aria-pressed="true"] {
    background: color-mix(in srgb, var(--spotnav-heat-colour) 16%, transparent);
    border-color: var(--spotnav-heat-colour);
    font-weight: 500;
  }
  .${X.awayLine} {
    margin: 6px 0 0;
    text-align: center;
    font-size: 0.88rem;
  }
  .${X.awayLine} button {
    margin-left: 6px;
    padding: 0;
    border: none;
    background: none;
    color: var(--primary-color, #03a9f4);
    font: inherit;
    cursor: pointer;
    text-decoration: underline;
  }
  .${X.status} {
    margin: 10px 0 0;
    font-size: 0.88rem;
    overflow-wrap: break-word;
  }
  .${X.status} + .${X.status} {
    margin-top: 2px;
  }
  .${X.shadow} {
    margin: 2px 0 0;
    font-size: 0.78rem;
  }
  /* The day's chart (\`heat-day-chart.ts\`): one time axis for the rooms (the heat colour), the comfort band (a faint
     green), the setpoint (dashed, the text's colour), the lowered hours as columns (SpotNav's plan light, what it did
     solid, the relay as anyone else closed it teal), hot water as droplets on the axis and the car as a bar under it.
     It is drawn in the history dialog too, so each colour carries its own fallback. */
  .${X.daySection} { margin-top: 10px; }
  .${X.dayToggle} {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    width: 100%;
    min-height: 32px;
    padding: 0;
    border: none;
    background: none;
    color: var(--secondary-text-color, #727272);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .${X.dayToggle}:focus-visible,
  .${X.daySurface}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${X.dayTitle} { margin: 0; }
  .${X.daySurface} {
    touch-action: pan-y;
    cursor: crosshair;
    border-radius: 4px;
  }
  .${X.day} {
    display: block;
    width: 100%;
    margin-top: 2px;
    overflow: visible;
  }
  /* One line kept for the readout (the hint, or a short time) so the legend does not move on a hover; a longer time
   * wraps below it. */
  .${X.dayReadout} { margin: 2px 0 0; min-height: 1.4em; font-size: 0.85rem; line-height: 1.4; overflow-wrap: break-word; }
  .${X.dayColumn}[data-mark="planned"] { fill: color-mix(in srgb, var(--spotnav-heat-lower-colour, #2f80c8) 18%, transparent); }
  .${X.dayColumn}[data-mark="executed"] { fill: color-mix(in srgb, var(--spotnav-heat-lower-colour, #2f80c8) 38%, transparent); }
  .${X.dayColumn}[data-mark="raise_planned"] { fill: color-mix(in srgb, var(--spotnav-heat-raise-colour, #d9822b) 18%, transparent); }
  .${X.dayColumn}[data-mark="raised"] { fill: color-mix(in srgb, var(--spotnav-heat-raise-colour, #d9822b) 40%, transparent); }
  .${X.dayColumn}[data-mark="relay"] { fill: color-mix(in srgb, var(--spotnav-heat-relay-colour, #14808f) 34%, transparent); }
  .${X.dayColumn}[data-mark="relay_expected"] { fill: color-mix(in srgb, var(--spotnav-heat-relay-colour, #14808f) 18%, transparent); }
  .${X.dayMark}[data-mark="hot_water"] {
    fill: var(--spotnav-heat-water-colour, #c62828);
    stroke: var(--card-background-color, #fff);
    stroke-width: 1.5;
  }
  .${X.dayMark}[data-mark="car"] { fill: var(--spotnav-flow-car-colour, #f08c3a); }
  .${X.dayAxis} {
    fill: var(--secondary-text-color, #727272);
    font-size: 10px;
  }
  .${X.dayBaseline} {
    stroke: var(--divider-color, #e0e0e0);
    stroke-width: 1;
  }
  .${X.dayNow} {
    stroke: var(--primary-text-color, #212121);
    stroke-width: 1.5;
  }
  .${X.dayCross} {
    stroke: var(--secondary-text-color, #727272);
    stroke-width: 1;
    stroke-dasharray: 3 3;
  }
  .${X.dayDot} {
    fill: var(--spotnav-heat-indoor-colour, var(--spotnav-heat, #e5554b));
    stroke: var(--card-background-color, #fff);
    stroke-width: 2;
  }
  .${X.dayDot}[data-line="coldest"] { opacity: 0.7; }
  .${X.indoorComfort} { fill: color-mix(in srgb, var(--success-color, #43a047) 18%, transparent); }
  .${X.indoorEdge} { text-anchor: end; }
  .${X.indoorSetpoint} {
    fill: none;
    stroke: var(--primary-text-color, #212121);
    stroke-width: 1.25;
    stroke-dasharray: 4 3;
    opacity: 0.75;
  }
  .${X.indoorLine} {
    fill: none;
    stroke: var(--spotnav-heat-indoor-colour, var(--spotnav-heat, #e5554b));
    stroke-width: 2;
    stroke-linejoin: round;
    stroke-linecap: round;
  }
  .${X.indoorLine}[data-line="coldest"] { stroke-width: 1.25; opacity: 0.55; }
  .${X.legend} {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 12px;
    margin: 4px 0 0;
    padding: 0;
    list-style: none;
    font-size: 0.78rem;
    color: var(--secondary-text-color, #727272);
  }
  .${X.legendItem} {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .${X.swatch} {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 2px;
  }
  .${X.swatch}[data-action="hot_water"] { background: var(--spotnav-heat-water-colour, #c62828); border-radius: 50% 0 50% 50%; transform: rotate(-45deg) scale(0.85); }
  .${X.swatch}[data-action="car"] { background: var(--spotnav-flow-car-colour, #f08c3a); height: 4px; }
  .${X.swatch}[data-action="relay"] { background: color-mix(in srgb, var(--spotnav-heat-relay-colour, #14808f) 34%, transparent); }
  .${X.swatch}[data-action="relay_expected"] { background: color-mix(in srgb, var(--spotnav-heat-relay-colour, #14808f) 18%, transparent); }
  .${X.swatch}[data-action="executed"] { background: color-mix(in srgb, var(--spotnav-heat-lower-colour, #2f80c8) 38%, transparent); }
  .${X.swatch}[data-action="raised"] { background: color-mix(in srgb, var(--spotnav-heat-raise-colour, #d9822b) 40%, transparent); }
  .${X.swatch}[data-action="raise_planned"] { background: color-mix(in srgb, var(--spotnav-heat-raise-colour, #d9822b) 18%, transparent); }
  .${X.swatch}[data-action="planned"] { background: color-mix(in srgb, var(--spotnav-heat-lower-colour, #2f80c8) 18%, transparent); }
  .${X.swatch}[data-action="indoor_mean"] { background: var(--spotnav-heat-indoor-colour, var(--spotnav-heat, #e5554b)); height: 3px; }
  .${X.swatch}[data-action="indoor_coldest"] { background: var(--spotnav-heat-indoor-colour, var(--spotnav-heat, #e5554b)); height: 2px; opacity: 0.55; }
  .${X.swatch}[data-action="setpoint"] { border-top: 2px dashed var(--primary-text-color, #212121); height: 0; border-radius: 0; opacity: 0.75; }
  .${X.swatch}[data-action="comfort"] { background: color-mix(in srgb, var(--success-color, #43a047) 18%, transparent); }
  .${X.error} {
    margin: 6px 0 0;
    text-align: center;
    font-size: 0.85rem;
    color: var(--error-color, #db4437);
  }
  .${X.editorField} {
    display: grid;
    gap: 4px;
    margin-top: 8px;
    font-size: 0.9rem;
  }
  .${X.editorField} input {
    font: inherit;
    padding: 6px 8px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 4px;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
  }
  @media (forced-colors: active) {
    .${X.indoorLine}, .${X.indoorSetpoint} { stroke: CanvasText; }
    .${X.dayColumn}, .${X.dayMark}, .${X.dayDot}, .${X.dialValue}, .${X.swatch} {
      fill: CanvasText;
      stroke: CanvasText;
      background: CanvasText;
    }
  }
`;
