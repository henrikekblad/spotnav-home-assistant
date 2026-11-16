// The heat card's own classes and styles, on top of the charging card's (`visual-styles.ts`) and the house card's
// (`house-styles.ts`: the header, the chip, the lines and notes), which the heat card loads too. The band's actions
// have a colour each, as custom properties a theme may set; each keeps 3:1 against a light and a dark card.

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
  band: "spotnav-heat-band",
  bandCell: "spotnav-heat-band-cell",
  bandReadout: "spotnav-heat-band-readout",
  bandMark: "spotnav-heat-band-mark",
  bandRelay: "spotnav-heat-band-relay",
  bandAxis: "spotnav-heat-band-axis",
  bandNow: "spotnav-heat-band-now",
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
    --spotnav-heat-lower: var(--spotnav-heat-lower-colour, #2f80c8);
    --spotnav-heat-preheat: var(--spotnav-heat-preheat-colour, var(--spotnav-heat-colour));
    --spotnav-heat-night: var(--spotnav-heat-night-colour, #7b61d1);
    --spotnav-heat-away: var(--spotnav-heat-away-colour, #8a8f98);
    --spotnav-heat-return: var(--spotnav-heat-return-colour, #e8890c);
    --spotnav-heat-normal: color-mix(in srgb, var(--secondary-text-color, #727272) 22%, transparent);
    --spotnav-heat-water: var(--spotnav-heat-water-colour, #1e88e5);
    --spotnav-heat-car: var(--spotnav-flow-car-colour, #f08c3a);
    /* The relay that really lowers the heat now (the blueprint or anyone): teal, apart from the plan's blue. */
    --spotnav-heat-relay: var(--spotnav-heat-relay-colour, #14808f);
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
  .${X.band} {
    display: block;
    width: 100%;
    margin-top: 10px;
    overflow: visible;
  }
  .${X.bandCell}[data-action="lower"] { fill: var(--spotnav-heat-lower); }
  .${X.bandCell}[data-action="preheat"] { fill: var(--spotnav-heat-preheat); }
  .${X.bandCell}[data-action="night"] { fill: var(--spotnav-heat-night); }
  .${X.bandCell}[data-action="away"] { fill: var(--spotnav-heat-away); }
  .${X.bandCell}[data-action="return"] { fill: var(--spotnav-heat-return); }
  /* An ordinary hour: faint, with a thin outline so the band's hours still show on the card. */
  .${X.bandCell}[data-action="normal"] { fill: var(--spotnav-heat-normal); stroke: var(--divider-color, #e0e0e0); stroke-width: 1; }
  .${X.bandCell} { cursor: pointer; }
  .${X.bandReadout} { margin: 2px 0 0; font-size: 0.85rem; }
  .${X.bandReadout}:empty { display: none; }
  .${X.bandCell}[data-past="true"] { opacity: 0.45; }
  .${X.bandMark}[data-mark="hot_water"] { fill: var(--spotnav-heat-water); }
  .${X.bandMark}[data-mark="car"] { fill: var(--spotnav-heat-car); }
  /* What the relay did (solid) and is expected to do (outlined), under SpotNav's plan. */
  .${X.bandRelay}[data-mark="relay"] { fill: var(--spotnav-heat-relay); }
  .${X.bandRelay}[data-mark="relay_expected"] { fill: none; stroke: var(--spotnav-heat-relay); stroke-width: 1.5; }
  .${X.bandAxis} {
    fill: var(--secondary-text-color, #727272);
    font-size: 10px;
  }
  .${X.bandNow} {
    stroke: var(--primary-text-color, #212121);
    stroke-width: 1.5;
  }
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
  .${X.swatch}[data-action="lower"] { background: var(--spotnav-heat-lower); }
  .${X.swatch}[data-action="preheat"] { background: var(--spotnav-heat-preheat); }
  .${X.swatch}[data-action="night"] { background: var(--spotnav-heat-night); }
  .${X.swatch}[data-action="away"] { background: var(--spotnav-heat-away); }
  .${X.swatch}[data-action="return"] { background: var(--spotnav-heat-return); }
  .${X.swatch}[data-action="hot_water"] { background: var(--spotnav-heat-water); border-radius: 50%; }
  .${X.swatch}[data-action="car"] { background: var(--spotnav-heat-car); }
  .${X.swatch}[data-action="relay"] { background: var(--spotnav-heat-relay); height: 6px; }
  .${X.swatch}[data-action="relay_expected"] { border: 1.5px solid var(--spotnav-heat-relay); height: 6px; box-sizing: border-box; }
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
    .${X.bandCell}, .${X.bandMark}, .${X.bandRelay}, .${X.dialValue}, .${X.swatch} {
      fill: CanvasText;
      stroke: CanvasText;
      background: CanvasText;
    }
  }
`;
