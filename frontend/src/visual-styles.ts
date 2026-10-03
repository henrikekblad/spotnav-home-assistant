// The card's stylesheet and the class names it defines, shared with the renderers as data so a
// component cannot use a class the stylesheet lacks. Every value is a Home Assistant theme variable
// with a plain fallback or a relative/clamped size, so a 320 px column wraps instead of scrolling.
// Nothing external (no `@import`, `url(...)`, web font or image); dialogs are `position: fixed`
// overlays, so opening one never changes the card's height.

/** Values longer than this stack under their label (the Android app uses the same threshold). */
export const LONG_VALUE_LENGTH = 18;

/** The class list of a summary row's value span: long values stack, short ones sit beside the label. */
export function summaryValueClass(value: string): string {
  return value.length > LONG_VALUE_LENGTH
    ? `${VISUAL_CLASSES.settingsValue} ${VISUAL_CLASSES.settingsValueLong}`
    : VISUAL_CLASSES.settingsValue;
}

export const VISUAL_CLASSES = {
  shell: "spotnav-shell",
  card: "spotnav-card",
  header: "spotnav-header",
  identity: "spotnav-identity",
  visuallyHidden: "spotnav-visually-hidden",
  name: "spotnav-name",
  iconButton: "spotnav-icon-button",
  headerActions: "spotnav-header-actions",
  actionRow: "spotnav-action-row",
  button: "spotnav-button",
  actionButton: "spotnav-action-button",
  plannerButton: "spotnav-planner-button",
  actionHelp: "spotnav-action-help",
  strategyButton: "spotnav-strategy-button",
  actionError: "spotnav-action-error",
  controlNotice: "spotnav-control-notice",
  advisory: "spotnav-advisory",
  suggestion: "spotnav-suggestion",
  suggestionText: "spotnav-suggestion-text",
  suggestionAnswers: "spotnav-suggestion-answers",
  pauseChoices: "spotnav-pause-choices",
  choiceButton: "spotnav-choice-button",
  nameBlock: "spotnav-name-block",
  vehicleLine: "spotnav-vehicle-line",
  vehicleLineName: "spotnav-vehicle-line-name",
  vehicleLineCharge: "spotnav-vehicle-line-charge",
  connectionLine: "spotnav-connection-line",
  connectionError: "spotnav-connection-error",
  vehicleLineAge: "spotnav-vehicle-line-age",
  vehicleChoices: "spotnav-vehicle-choices",
  vehicleChoice: "spotnav-vehicle-choice",
  vehicleChoiceName: "spotnav-vehicle-choice-name",
  vehicleChoiceCharge: "spotnav-vehicle-choice-charge",
  strategyRow: "spotnav-strategy-row",
  strategyReason: "spotnav-strategy-reason",
  strategyLink: "spotnav-strategy-link",
  settingsRow: "spotnav-settings-row",
  settingsTrigger: "spotnav-settings-trigger",
  settingsSection: "spotnav-settings-section",
  settingsSectionHeading: "spotnav-settings-section-heading",
  settingsSectionValue: "spotnav-settings-section-value",
  settingsSectionConfigure: "spotnav-settings-section-configure",
  siteApplies: "spotnav-site-applies",
  siteFieldset: "spotnav-site-fieldset",
  siteLegend: "spotnav-site-legend",
  entityNumberLabel: "spotnav-entity-number-label",
  siteChoice: "spotnav-site-choice",
  entityMeters: "spotnav-entity-meters",
  entityLine: "spotnav-entity-line",
  entityLineCells: "spotnav-entity-line-cells",
  entityGroup: "spotnav-entity-group",
  entityRow: "spotnav-entity-row",
  entityRowLabel: "spotnav-entity-row-label",
  entityRowValue: "spotnav-entity-row-value",
  entityHelp: "spotnav-entity-help",
  entityWarning: "spotnav-entity-warning",
  entityNotices: "spotnav-entity-notices",
  entityChecks: "spotnav-entity-checks",
  entityAutomatic: "spotnav-entity-automatic",
  entityDialog: "spotnav-entity-dialog",
  planDialog: "spotnav-plan-dialog",
  actionIcon: "spotnav-action-icon",
  summaryExtremes: "spotnav-summary-extremes",
  summaryMax: "spotnav-summary-max",
  summaryMin: "spotnav-summary-min",
  summaryCurrent: "spotnav-summary-current",
  summaryArrow: "spotnav-summary-arrow",
  settingsIcon: "spotnav-settings-icon",
  settingsValue: "spotnav-settings-value",
  settingsValueLong: "spotnav-settings-value-long",
  settingsField: "spotnav-settings-field",
  settingsLabel: "spotnav-settings-label",
  settingsInput: "spotnav-settings-input",
  actionBar: "spotnav-action-bar",
  actionBarWrap: "spotnav-action-bar-wrap",
  barCell: "spotnav-bar-cell",
  barCaption: "spotnav-bar-caption",
  barValue: "spotnav-bar-value",
  barPart: "spotnav-bar-part",
  barWide: "spotnav-bar-wide",
  barBusy: "spotnav-bar-busy",
  settingsCheckRow: "spotnav-settings-check-row",
  settingsActions: "spotnav-settings-actions",
  settingsSave: "spotnav-settings-save",
  settingsReload: "spotnav-settings-reload",
  settingsReapply: "spotnav-settings-reapply",
  settingsReadOnly: "spotnav-settings-readonly",
  settingsNote: "spotnav-settings-note",
  capacityBlock: "spotnav-capacity-block",
  socLink: "spotnav-soc-link",
  settingsConflict: "spotnav-settings-conflict",
  settingsPair: "spotnav-settings-pair",
  settingsSlider: "spotnav-settings-slider",
  settingsUnit: "spotnav-settings-unit",
  settingsPower: "spotnav-settings-power",
  settingsError: "spotnav-settings-error",
  settingsNotice: "spotnav-settings-notice",
  marketArea: "spotnav-market-area",
  marketState: "spotnav-market-state",
  marketComponent: "spotnav-market-component",
  marketCheckbox: "spotnav-market-checkbox",
  marketReset: "spotnav-market-reset",
  marketSuggestion: "spotnav-market-suggestion",
  marketValue: "spotnav-market-value",
  marketIncluded: "spotnav-market-included",
  marketSource: "spotnav-market-source",
  marketPostcode: "spotnav-market-postcode",
  marketPostcodeRow: "spotnav-market-postcode-row",
  banner: "spotnav-banner",
  bannerBlocking: "spotnav-banner-blocking",
  bannerNotice: "spotnav-banner-notice",
  bannerCount: "spotnav-banner-count",
  status: "spotnav-status",
  graphSurface: "spotnav-graph",
  viewport: "spotnav-chart-viewport",
  svg: "spotnav-svg",
  bands: "spotnav-bands",
  bandInstalled: "spotnav-band-installed",
  bandProposal: "spotnav-band-proposal",
  axis: "spotnav-axis",
  gridline: "spotnav-gridline",
  axisLabel: "spotnav-axis-label",
  tickLabel: "spotnav-tick-label",
  now: "spotnav-now",
  selection: "spotnav-selection",
  marks: "spotnav-marks",
  point: "spotnav-point",
  segment: "spotnav-segment",
  current: "spotnav-current",
  cheap: "spotnav-cheap",
  expensive: "spotnav-expensive",
  tomorrow: "spotnav-tomorrow",
  readout: "spotnav-readout",
  readoutHint: "spotnav-readout-hint",
  legend: "spotnav-legend",
  summary: "spotnav-summary",
  figures: "spotnav-figures",
  figure: "spotnav-figure",
  figureLabel: "spotnav-figure-label",
  figureValue: "spotnav-figure-value",
  periods: "spotnav-periods",
  periodsProposal: "spotnav-periods-proposal",
  periodsInstalled: "spotnav-periods-installed",
  periodsHeading: "spotnav-periods-heading",
  period: "spotnav-period",
  periodActive: "spotnav-period-active",
  context: "spotnav-context",
  overlay: "spotnav-dialog-overlay",
  dialog: "spotnav-dialog",
  dialogHeader: "spotnav-dialog-header",
  dialogTitle: "spotnav-dialog-title",
  dialogClose: "spotnav-dialog-close",
  dialogIntro: "spotnav-dialog-intro",
  dialogBody: "spotnav-dialog-body",
  issueItem: "spotnav-issue",
  issueText: "spotnav-issue-text",
  capabilityItem: "spotnav-capability",
  capabilityLabel: "spotnav-capability-label",
  capabilityState: "spotnav-capability-state",
  capabilityNote: "spotnav-capability-note",
  switchGroup: "spotnav-switch-group",
  switchControl: "spotnav-switch",
  activeNotice: "spotnav-active-notice",
  activeNoticeWarning: "spotnav-active-notice-warning",
  historyBody: "spotnav-history-body",
  historyTiles: "spotnav-history-tiles",
  historyTile: "spotnav-history-tile",
  historyTileHeading: "spotnav-history-tile-heading",
  historyFigures: "spotnav-history-figures",
  historySavings: "spotnav-history-savings",
  historyOpen: "spotnav-history-open",
  historyToggle: "spotnav-history-toggle",
  historyToggleButton: "spotnav-history-toggle-button",
  historyList: "spotnav-history-list",
  historyRow: "spotnav-history-row",
  historyRowTitle: "spotnav-history-row-title",
  historyRowFigures: "spotnav-history-row-figures",
  historyRowNote: "spotnav-history-row-note",
  historyHeading: "spotnav-history-heading",
  historyFootnote: "spotnav-history-footnote",
  historyExport: "spotnav-history-export",
  historyMonthPicker: "spotnav-history-month-picker",
  historyMonth: "spotnav-history-month",
  historyChart: "spotnav-history-chart",
  historyBars: "spotnav-history-bars",
  historyBar: "spotnav-history-bar",
  historyBarFill: "spotnav-history-bar-fill",
  historyAxis: "spotnav-history-axis",
  historyScale: "spotnav-history-scale",
  historyReadout: "spotnav-history-readout",
  muted: "spotnav-muted",
  unavailable: "spotnav-unavailable",
} as const;

/**
 * The two focus lines' colours as custom properties. Each resolves through Home Assistant's text
 * variable (dark on a light card, light on a dark one) and falls back to a mid grey that clears 3:1
 * non-text contrast on both white and near-black. Declarations are generated from this table so the
 * stylesheet and the light/dark assertions cannot drift.
 */
export const FOCUS_LINE_TOKENS = {
  now: { property: "--spotnav-now-line", theme: "--primary-text-color", fallback: "#616161" },
  selection: {
    property: "--spotnav-selection-line",
    theme: "--primary-text-color",
    fallback: "#616161",
  },
} as const;

export function focusLineColour(kind: keyof typeof FOCUS_LINE_TOKENS): string {
  const token = FOCUS_LINE_TOKENS[kind];
  return `var(${token.property}, var(${token.theme}, ${token.fallback}))`;
}

export const VISUAL_STYLES = `
  /*
   * The compact settings row and the dialogs it opens: quiet icon-plus-value triggers that wrap on a
   * narrow dashboard. Dialogs are overlays, so they never change the card's height.
   */
  .spotnav-settings-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 8px 0 0;
  }
  .spotnav-settings-trigger {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .spotnav-settings-row > .spotnav-settings-input {
    width: 6rem;
    min-width: 0;
  }
  .spotnav-settings-icon {
    flex: none;
  }
  .spotnav-settings-value {
    font-variant-numeric: tabular-nums;
  }
  .spotnav-settings-field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 8px;
  }
  .spotnav-settings-check-row {
    display: flex;
    flex-direction: row;
    align-items: center;
    justify-content: flex-start;
    gap: 8px;
    margin-top: 8px;
  }
  .spotnav-settings-check-row > input[type="checkbox"] {
    flex: none;
    margin: 0;
    min-height: 0;
    padding: 0;
    border: 0;
    background: none;
  }
  .spotnav-settings-label {
    font-size: 0.85em;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-settings-input {
    font: inherit;
    min-height: 36px;
    max-width: 100%;
    box-sizing: border-box;
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--secondary-background-color, transparent);
    color: var(--primary-text-color, #212121);
  }
  .spotnav-settings-input:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .spotnav-settings-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
  }
  /*
   * A slider beside its exact number field. Grid sizing rather than flex: the slider takes what is left
   * (minmax(0, 1fr)), the field gets a bounded share wide enough for the longest accepted value, and
   * the unit is sized by its text. The out-of-domain note spans every column. Energy and current share
   * one layout.
   */
  .spotnav-settings-pair {
    display: grid;
    grid-template-columns: minmax(0, 1fr) clamp(4.5rem, 20%, 6rem) auto;
    align-items: center;
    gap: 8px;
  }
  .spotnav-settings-slider {
    width: 100%;
    min-width: 0;
    accent-color: var(--primary-color, #03a9f4);
  }
  .spotnav-settings-slider:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .spotnav-settings-slider:disabled {
    opacity: 0.55;
  }
  .spotnav-settings-pair > .spotnav-settings-input {
    width: 100%;
    min-width: 0;
  }
  .spotnav-settings-pair > .spotnav-settings-note {
    grid-column: 1 / -1;
    margin: 4px 0 0;
  }
  .spotnav-settings-unit {
    white-space: nowrap;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-settings-power {
    margin: 4px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-settings-save {
    background: var(--primary-color, #03a9f4);
    border-color: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    font-weight: 500;
  }
  .spotnav-settings-reload,
  .spotnav-settings-reapply {
    font-weight: 500;
  }
  .spotnav-settings-readonly,
  .spotnav-settings-note {
    margin: 8px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-site-fieldset[data-part="mode"] {
    margin-top: 12px;
  }
  .spotnav-capability[data-soc-row] {
    padding: 2px 0;
  }
  .spotnav-button.spotnav-soc-link {
    margin: 4px 0 0;
    padding: 0;
    border: 0;
    background: none;
    color: var(--primary-color, #03a9f4);
    text-decoration: underline;
    font-size: 0.9rem;
    min-height: 32px;
    text-align: left;
  }
  .spotnav-settings-conflict {
    margin: 8px 0 0;
    color: var(--error-color, #db4437);
  }
  .spotnav-settings-error,
  .spotnav-settings-notice {
    margin: 4px 0 0;
    color: var(--error-color, #db4437);
  }
  /*
   * The area/fiscal dialog: one selector, then one row per component (checkbox, name, number field,
   * unit) on the same grid as the slider pair, so long names or units never push the figure off the row.
   */
  .spotnav-market-area {
    width: 100%;
  }
  .spotnav-market-state {
    margin: 4px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-market-component {
    margin: 12px 0 0;
    padding: 8px 10px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  /*
   * The checkbox keeps its native size (no fixed pixel sizes here); its label is bound to it, so the
   * whole row toggles it.
   */
  .spotnav-market-checkbox {
    margin: 0;
    accent-color: var(--primary-color, #03a9f4);
  }
  .spotnav-market-value {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) clamp(4.5rem, 20%, 6rem) auto;
    align-items: center;
    gap: 8px;
  }
  .spotnav-market-value > .spotnav-settings-label {
    min-width: 0;
    overflow-wrap: break-word;
  }
  .spotnav-market-value > .spotnav-settings-input {
    width: 100%;
    min-width: 0;
  }
  .spotnav-market-reset {
    flex: none;
    font-size: 0.85em;
    padding: 2px 8px;
  }
  /* A component the price already includes: the checked, locked box, then the words, across the row. */
  .spotnav-market-included {
    grid-column: 3 / -1;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-market-source {
    margin: 4px 0 0;
    font-size: 0.85em;
  }
  .spotnav-market-source a {
    color: var(--primary-color, #03a9f4);
  }
  .spotnav-market-postcode-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .spotnav-market-postcode-row > .spotnav-settings-input {
    flex: 0 1 12rem;
    min-width: 0;
  }
  .spotnav-market-suggestion {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 4px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  /*
   * The compact action row: one primary button, one strategy trigger and a help line, with phone-sized
   * targets.
   */
  /* For a label that must exist for assistive technology and not be seen: the standard clip rectangle. */
  .spotnav-visually-hidden {
    position: absolute;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .spotnav-action-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 8px 0 0;
  }
  /*
   * The action bar: four equal cells (2x2 narrow, 4x1 wide). The wrapper is the size container, so the
   * layout follows the card's width, not the viewport.
   */
  .spotnav-action-bar-wrap {
    container-type: inline-size;
    margin: 8px 0 0;
  }
  .spotnav-action-bar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  @container (min-width: 520px) {
    .spotnav-action-bar {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
  }
  .spotnav-bar-cell {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    justify-content: flex-start;
    gap: 2px;
    min-width: 0;
    min-height: 52px;
    padding: 6px 2px;
    border-radius: 12px;
    text-align: center;
  }
  .spotnav-bar-cell.spotnav-bar-busy {
    opacity: 0.6;
    animation: spotnav-bar-busy 1.4s ease-in-out infinite;
  }
  @keyframes spotnav-bar-busy {
    50% {
      opacity: 0.35;
    }
  }
  .spotnav-bar-caption {
    font-size: 0.68rem;
    line-height: 1.1;
    opacity: 0.8;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .spotnav-bar-value {
    display: inline-flex;
    flex: 1 1 auto;
    flex-wrap: nowrap;
    align-items: center;
    justify-content: center;
    gap: 4px;
    min-width: 0;
    font-size: 0.85rem;
    font-weight: 500;
  }
  .spotnav-bar-value > .spotnav-settings-value {
    min-width: 0;
    /* A long value wraps inside its cell rather than being cut off. */
    overflow-wrap: break-word;
    line-height: 1.15;
  }
  /*
   * A cell whose value is a line of its own (Plan): the icon rides with the caption, the value is
   * smaller and wraps only between its parts.
   */
  .spotnav-bar-wide > .spotnav-bar-caption {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 3px;
  }
  .spotnav-bar-wide > .spotnav-bar-caption > svg {
    flex: none;
    width: 1.15em;
    height: 1.15em;
    margin: 0;
  }
  .spotnav-bar-wide {
    padding-left: 1px;
    padding-right: 1px;
  }
  .spotnav-bar-wide > .spotnav-bar-value {
    font-size: 0.66rem;
    gap: 0;
  }
  .spotnav-bar-part {
    white-space: nowrap;
  }
  .spotnav-bar-value > .spotnav-settings-icon,
  .spotnav-bar-value > .spotnav-action-icon {
    flex: none;
  }
  .spotnav-bar-value > .spotnav-action-icon {
    flex: none;
    margin-right: 0;
  }
  .spotnav-button {
    font: inherit;
    min-height: 36px;
    padding: 6px 14px;
    border-radius: 18px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--secondary-background-color, transparent);
    color: var(--primary-text-color, #212121);
    cursor: pointer;
  }
  .spotnav-button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .spotnav-button:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .spotnav-action-button {
    background: var(--primary-color, #03a9f4);
    border-color: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    font-weight: 500;
  }
  /*
   * The automatic control is outlined, not filled: it answers a different question from the immediate
   * one (suspend the plan versus command the charger) and reads as the quieter of the two.
   */
  .spotnav-planner-button {
    border-color: var(--primary-color, #03a9f4);
    color: var(--primary-color, #03a9f4);
    font-weight: 500;
  }
  .spotnav-strategy-button {
    font-weight: 500;
  }
  .spotnav-action-help {
    font-size: 0.8em;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-action-error,
  .spotnav-control-notice {
    margin: 4px 0 0;
  }
  .spotnav-control-notice {
    color: var(--secondary-text-color, #727272);
  }
  /**
   * The vehicle-side advisory: a subdued warning, not a blocking banner. It names an observation about
   * the car and offers no action. Tinted at text weight rather than as a filled block.
   */
  .spotnav-advisory {
    margin: 8px 0 0;
    overflow-wrap: break-word;
    color: var(--warning-color, #ffa600);
  }
  /** A question the card puts to the administrator: the sentence and its one-tap answers. */
  .spotnav-suggestion {
    margin: 8px 0 0;
  }
  .spotnav-suggestion-text {
    margin: 0;
    overflow-wrap: break-word;
  }
  .spotnav-suggestion-answers {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 6px;
  }
  .spotnav-pause-choices,
  .spotnav-strategy-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: flex-start;
    margin-top: 8px;
  }
  .spotnav-choice-button {
    font: inherit;
    min-height: 36px;
    padding: 6px 14px;
    border-radius: 18px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--secondary-background-color, transparent);
    color: var(--primary-text-color, #212121);
    cursor: pointer;
  }
  .spotnav-choice-button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .spotnav-strategy-reason {
    font-size: 0.8em;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-strategy-link {
    font: inherit;
    font-size: 0.8em;
    padding: 0;
    border: 0;
    background: none;
    color: var(--primary-color, #03a9f4);
    text-decoration: underline;
    cursor: pointer;
  }
  :host {
    display: block;
    box-sizing: border-box;
    max-width: 100%;
  }
  *, *::before, *::after {
    box-sizing: border-box;
  }
  .${VISUAL_CLASSES.card} {
    box-sizing: border-box;
    max-width: 100%;
    min-width: 0;
    padding: 12px 14px;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    color: var(--primary-text-color, #212121);
    border-radius: var(--ha-card-border-radius, 12px);
    font-size: 0.95rem;
    line-height: 1.35;
  }
  .${VISUAL_CLASSES.shell} {
    display: block;
    min-width: 0;
  }
  .${VISUAL_CLASSES.header} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.identity} {
    flex: 0 0 auto;
    font-weight: 600;
    letter-spacing: 0.01em;
    color: var(--primary-color, #03a9f4);
  }
  /* A modest basis in rem (not em of the large title font): a phone-wide card keeps name, vehicle line and
     both buttons on one row, and only a very narrow card moves the button group to a row of its own. */
  .${VISUAL_CLASSES.name} {
    flex: 1 1 8rem;
    min-width: 0;
    font-weight: 500;
    overflow-wrap: break-word;
  }
  /* The header's buttons wrap only as a unit, never one by one, aligned to the end. */
  .${VISUAL_CLASSES.headerActions} {
    display: flex;
    flex: none;
    gap: 8px;
    margin-left: auto;
  }
  /*
   * The header's history and cog buttons: a flex container centres the icon on both axes, and
   * line-height 0 stops the empty line box pushing it off centre. The retry button shares the class.
   */
  .${VISUAL_CLASSES.iconButton} {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    min-width: 44px;
    min-height: 44px;
    margin: 0;
    padding: 0;
    line-height: 0;
    box-sizing: border-box;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.iconButton} > svg {
    display: block;
    margin: 0;
    vertical-align: baseline;
    width: 1.25em;
    height: 1.25em;
    flex: none;
  }
  .${VISUAL_CLASSES.iconButton}:focus-visible,
  .${VISUAL_CLASSES.banner}:focus-visible,
  .${VISUAL_CLASSES.dialogClose}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.banner} {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    width: 100%;
    margin: 10px 0 0;
    padding: 8px 10px;
    font: inherit;
    text-align: start;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.bannerBlocking} {
    border-color: var(--error-color, #db4437);
    color: var(--error-color, #db4437);
  }
  .${VISUAL_CLASSES.bannerNotice} {
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.bannerCount} {
    flex: 0 0 auto;
    color: var(--secondary-text-color, #727272);
    font-size: 0.8rem;
  }
  .${VISUAL_CLASSES.nameBlock} {
    flex: 1 1 8rem;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
  }
  .${VISUAL_CLASSES.nameBlock} > .${VISUAL_CLASSES.name} {
    /* A two-line title: no heading margins, so the vehicle line reads as the name's subtitle. */
    margin: 0;
    line-height: 1.25;
    flex: 0 0 auto;
    max-width: 100%;
  }
  /* The planned vehicle: a quiet text button under the name; the header's 44 px icon buttons keep the row tall enough to tap. */
  .${VISUAL_CLASSES.vehicleLine} {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    max-width: 100%;
    min-height: 28px;
    margin: 0 0 -4px -4px;
    padding: 2px 4px;
    font: inherit;
    font-size: 0.85rem;
    color: var(--secondary-text-color, #727272);
    background: transparent;
    border: 0;
    border-radius: 6px;
    text-align: start;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.vehicleLine} > svg {
    flex: none;
  }
  .${VISUAL_CLASSES.vehicleLine}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  /* Never squeezed to nothing: the charge and status wrap to the next line first; only a name wider
     than the whole line is cut with an ellipsis. */
  .${VISUAL_CLASSES.vehicleLineName} {
    flex: 0 0 auto;
    max-width: 100%;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.vehicleLineCharge},
  .${VISUAL_CLASSES.vehicleLineAge},
  .${VISUAL_CLASSES.connectionLine} {
    flex: none;
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.vehicleLineAge} {
    opacity: 0.8;
  }
  /* The charger's status, after the charge (or alone, with no vehicle); an error in the warning colour. */
  .${VISUAL_CLASSES.connectionLine} {
    margin: 0;
    font-size: 0.85rem;
    color: var(--secondary-text-color, #727272);
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.connectionError} {
    color: var(--warning-color, #b26a00);
  }
  .${VISUAL_CLASSES.vehicleChoices} {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .${VISUAL_CLASSES.vehicleChoice} {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    padding: 4px 8px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.vehicleChoice} > input {
    flex: none;
    margin: 0;
  }
  .${VISUAL_CLASSES.vehicleChoiceName} {
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.vehicleChoiceCharge} {
    flex: none;
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.status} {
    margin: 8px 0 0;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.muted} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.unavailable} {
    color: var(--secondary-text-color, #727272);
    font-style: italic;
  }
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
    stroke-width: 1;
  }
  .${VISUAL_CLASSES.axisLabel},
  .${VISUAL_CLASSES.tickLabel} {
    fill: var(--secondary-text-color, #727272);
    font-size: 11px;
  }
  .${VISUAL_CLASSES.bands} {
    pointer-events: none;
  }
  .${VISUAL_CLASSES.marks} {
    pointer-events: none;
  }
  .${VISUAL_CLASSES.cheap} {
    --spotnav-mark-colour: var(--spotnav-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.bandInstalled} {
    fill: var(--primary-color, #03a9f4);
    opacity: 0.16;
  }
  .${VISUAL_CLASSES.bandProposal} {
    fill: var(--primary-color, #03a9f4);
    opacity: 0.07;
  }
  .${VISUAL_CLASSES.now} {
    stroke: ${focusLineColour("now")};
    stroke-width: 1.5;
  }
  .${VISUAL_CLASSES.selection} {
    stroke: ${focusLineColour("selection")};
    stroke-width: 1.5;
    stroke-dasharray: 3 3;
  }
  .${VISUAL_CLASSES.point},
  .${VISUAL_CLASSES.segment} {
    fill: var(--spotnav-cheap, #2e7d32);
    stroke: var(--spotnav-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.segment} {
    stroke-linecap: round;
  }
  .${VISUAL_CLASSES.expensive},
  .${VISUAL_CLASSES.expensive}:is(circle) {
    fill: var(--spotnav-expensive, #c62828);
    stroke: var(--spotnav-expensive, #c62828);
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
  .${VISUAL_CLASSES.summaryMax} {
    color: var(--spotnav-expensive, #c62828);
  }
  .${VISUAL_CLASSES.summaryMin} {
    color: var(--spotnav-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.summaryCurrent} {
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
  /* The Settings popover's sections: one bordered block per topic. */
  .${VISUAL_CLASSES.settingsSection} {
    margin: 12px 0 0;
    padding: 8px 10px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.settingsSection}:first-child {
    margin-top: 0;
  }
  .${VISUAL_CLASSES.settingsSectionHeading} {
    margin: 0 0 4px;
    font-size: 0.9rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.settingsSectionValue} {
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.settingsSectionConfigure} {
    margin-top: 8px;
  }
  .${VISUAL_CLASSES.siteApplies} {
    margin: 0 0 8px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.82rem;
  }
  .${VISUAL_CLASSES.siteApplies}[data-wired-phases] {
    margin: 12px 0 8px;
  }
  .${VISUAL_CLASSES.siteFieldset} {
    margin: 0 0 8px;
    padding: 0;
    border: 0;
  }
  .${VISUAL_CLASSES.siteLegend} {
    padding: 0;
    margin: 0 0 4px;
    font-size: 0.82rem;
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.siteChoice} {
    display: flex;
    align-items: center;
    gap: 6px;
    min-height: 28px;
  }
  /*
   * The entity editors. The three meter lines (L1-L3) stack as labelled groups; when the dialog is
   * wide enough the nine derived meters form a table. The wrapper is the size container, so the
   * layout follows the dialog, not the viewport.
   */
  .${VISUAL_CLASSES.entityMeters} {
    container-type: inline-size;
    margin: 4px 0 8px;
  }
  .${VISUAL_CLASSES.entityLine} {
    min-width: 0;
    margin: 0 0 4px;
    padding: 0;
    border: 0;
  }
  .${VISUAL_CLASSES.entityLineCells} {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 0 8px;
  }
  @container (min-width: 520px) {
    .${VISUAL_CLASSES.entityLineCells} {
      grid-auto-flow: column;
      grid-auto-columns: minmax(0, 1fr);
      grid-template-columns: none;
    }
  }
  .${VISUAL_CLASSES.entityGroup} {
    margin: 8px 0 0;
  }
  /*
   * One entity row: the label sits above its value so a long entity id cannot squeeze it. The value
   * may break anywhere; the label never mid-word.
   */
  .${VISUAL_CLASSES.entityRow} {
    display: block;
    min-width: 0;
    margin: 0 0 8px;
  }
  .${VISUAL_CLASSES.entityRowLabel} {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.82rem;
    overflow-wrap: normal;
    word-break: normal;
  }
  .${VISUAL_CLASSES.entityRowValue} {
    display: block;
    min-width: 0;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.entityHelp},
  .${VISUAL_CLASSES.entityAutomatic},
  .${VISUAL_CLASSES.entityWarning} {
    margin: 2px 0 0;
    font-size: 0.8rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.entityNotices} {
    margin: 8px 0 0;
    padding: 0 0 0 18px;
  }
  .${VISUAL_CLASSES.entityChecks} {
    margin: 4px 0 0;
    padding: 0 0 0 18px;
    font-size: 0.85rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.entityChecks} > li + li {
    margin-top: 4px;
  }
  .${VISUAL_CLASSES.entityHelp},
  .${VISUAL_CLASSES.entityAutomatic} {
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.entityWarning} {
    color: var(--warning-color, #b26a00);
    font-weight: 500;
  }
  .${VISUAL_CLASSES.settingsField} > .${VISUAL_CLASSES.settingsError} {
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.dialog} {
    max-width: 680px;
  }
  .${VISUAL_CLASSES.capacityBlock} {
    margin-top: 16px;
  }
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.settingsRow} {
    margin: 0;
  }
  .${VISUAL_CLASSES.siteFieldset}.${VISUAL_CLASSES.capacityBlock} > .${VISUAL_CLASSES.siteLegend} {
    font-weight: 600;
    font-size: 1rem;
    color: var(--primary-text-color, inherit);
  }
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.siteFieldset} > .${VISUAL_CLASSES.siteLegend},
  .${VISUAL_CLASSES.planDialog} .${VISUAL_CLASSES.siteFieldset} > .${VISUAL_CLASSES.siteLegend} {
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--primary-text-color, inherit);
  }
  /* A number field (main fuse, safety margin, measurement age) is headed like the groups around it. */
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.settingsField} > .${VISUAL_CLASSES.entityNumberLabel},
  .${VISUAL_CLASSES.planDialog} .${VISUAL_CLASSES.settingsField} > .${VISUAL_CLASSES.settingsLabel} {
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--primary-text-color, inherit);
  }
  .${VISUAL_CLASSES.entityGroup} > .${VISUAL_CLASSES.siteLegend} {
    display: block;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.actionIcon} {
    margin-right: 4px;
    vertical-align: -2px;
  }
  .${VISUAL_CLASSES.figures} {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    margin-top: 10px;
  }
  .${VISUAL_CLASSES.figure} {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .${VISUAL_CLASSES.figureLabel} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
  }
  .${VISUAL_CLASSES.figureValue} {
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.periods} {
    margin-top: 10px;
    padding: 8px 10px;
    border: 1px solid var(--primary-color, #03a9f4);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.periodsInstalled} {
    background: var(--secondary-background-color, transparent);
  }
  .${VISUAL_CLASSES.periodsProposal} {
    border-style: dashed;
  }
  .${VISUAL_CLASSES.periodsHeading} {
    margin: 0 0 4px;
    font-size: 0.9rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.period} {
    display: flex;
    gap: 8px;
    font-variant-numeric: tabular-nums;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.periodActive} {
    font-weight: 600;
  }
  .${VISUAL_CLASSES.context} {
    margin-top: 10px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.8rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.overlay} {
    position: fixed;
    inset: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    background: var(--spotnav-overlay, rgba(0, 0, 0, 0.45));
  }
  .${VISUAL_CLASSES.overlay}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.dialog} {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
    max-width: 420px;
    max-height: 80vh;
    overflow: auto;
    padding: 14px;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    color: var(--primary-text-color, #212121);
    border-radius: 12px;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.28);
  }
  /*
   * One header row: the title takes the width it needs and the close button keeps its 44x44 tap
   * target at the end of the same row; a long title wraps around the button, never under it.
   */
  .${VISUAL_CLASSES.dialogHeader} {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.dialogTitle} {
    flex: 1 1 auto;
    min-width: 0;
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.dialogClose} {
    flex: none;
    min-width: 44px;
    min-height: 44px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.dialogIntro} {
    margin: 0;
  }
  .${VISUAL_CLASSES.dialogBody} {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyBody} {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyTiles} {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .${VISUAL_CLASSES.historyTile} {
    flex: 1 1 12rem;
    padding: 8px 10px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyTile} > p,
  .${VISUAL_CLASSES.historyRow} > span {
    margin: 0;
  }
  .${VISUAL_CLASSES.historyTileHeading},
  .${VISUAL_CLASSES.historyHeading} {
    margin: 0 0 4px;
    font-size: 0.9rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.historyFigures},
  .${VISUAL_CLASSES.historyRowFigures} {
    font-variant-numeric: tabular-nums;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.historyFigures} {
    font-size: 1rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.historySavings},
  .${VISUAL_CLASSES.historyRowNote},
  .${VISUAL_CLASSES.historyFootnote} {
    font-size: 0.82rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.historyOpen} {
    margin: 0;
    padding: 6px 8px;
    border-inline-start: 3px solid var(--success-color, #43a047);
    font-size: 0.9rem;
  }
  .${VISUAL_CLASSES.historyToggle} {
    display: flex;
    gap: 6px;
  }
  .${VISUAL_CLASSES.historyToggleButton} {
    min-height: 36px;
    padding: 4px 12px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: transparent;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 18px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.historyToggleButton}[aria-pressed="true"] {
    background: var(--secondary-background-color, #e5e5e5);
    border-color: var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.historyToggleButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.historyList} {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    max-height: 16rem;
    overflow-y: auto;
  }
  .${VISUAL_CLASSES.historyRow} {
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: 6px 0;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyRowTitle} {
    font-weight: 500;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.historyExport} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .${VISUAL_CLASSES.historyMonthPicker} {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .${VISUAL_CLASSES.historyMonthPicker} > select {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 36px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.historyMonthPicker} > button {
    flex: none;
    min-width: 36px;
    padding: 4px 10px;
    font-size: 1.1rem;
    line-height: 1;
  }
  .${VISUAL_CLASSES.historyMonthPicker} > button:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .${VISUAL_CLASSES.historyMonth} {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyMonth} > .${VISUAL_CLASSES.historyTile} {
    flex: none;
  }
  .${VISUAL_CLASSES.historyMonth}[aria-busy="true"] {
    opacity: 0.5;
  }
  .${VISUAL_CLASSES.historyChart} {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyScale} {
    font-size: 0.75rem;
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.historyBars} {
    display: flex;
    align-items: stretch;
    gap: 2px;
    height: 7.5rem;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  .${VISUAL_CLASSES.historyBar} {
    flex: 1 1 0;
    min-width: 0;
    padding: 0;
    display: flex;
    align-items: flex-end;
    background: transparent;
    border: 0;
    border-radius: 3px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.historyBar}:hover,
  .${VISUAL_CLASSES.historyBar}[aria-pressed="true"] {
    background: var(--secondary-background-color, #e5e5e5);
  }
  .${VISUAL_CLASSES.historyBar}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .${VISUAL_CLASSES.historyBarFill} {
    display: block;
    width: 100%;
    min-height: 0;
    border-radius: 3px 3px 0 0;
    background: color-mix(
      in srgb,
      var(--spotnav-expensive, #c62828) var(--spotnav-day-dear, 0%),
      var(--spotnav-cheap, #2e7d32)
    );
  }
  .${VISUAL_CLASSES.historyBarFill}[data-price="none"] {
    background: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.historyBarFill}[data-empty="true"] {
    height: 0.125rem;
    background: var(--divider-color, #e0e0e0);
  }
  .${VISUAL_CLASSES.historyAxis} {
    display: flex;
    gap: 2px;
    font-size: 0.72rem;
    color: var(--secondary-text-color, #727272);
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.historyAxis} > span {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    justify-content: center;
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.historyReadout} {
    margin: 4px 0 0;
    min-height: 2.6em;
    font-size: 0.85rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.issueItem} {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.issueText} {
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.capabilityItem} {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.capabilityLabel} {
    overflow-wrap: break-word;
  }
  /*
   * A summary row on the Settings page: the label never breaks mid-label (ellipsis only as a last
   * resort); a short value sits right-aligned beside it. A value that does not fit beside the label
   * wraps to its own line (flex-wrap), and a value past LONG_VALUE_LENGTH characters is stacked
   * under the label left-aligned from the start (the Android app's threshold), so the two never
   * read as extra rows.
   */
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} {
    flex-wrap: wrap;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.capabilityLabel} {
    /* Muted, as in the app: a value stacked under its label must not read as one more label. */
    color: var(--secondary-text-color, #727272);
    flex: 0 1 auto;
    max-width: 100%;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.settingsValue} {
    flex: 1 1 auto;
    min-width: 0;
    text-align: right;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.settingsValueLong} {
    flex: 1 0 100%;
    text-align: left;
    margin: 1px 0 6px;
  }
  [data-slot='vehicles'] > .${VISUAL_CLASSES.settingsSection} {
    margin-top: 12px;
  }
  .${VISUAL_CLASSES.capabilityState} {
    flex: 0 0 auto;
    color: var(--secondary-text-color, #727272);
    font-size: 0.82rem;
  }
  .${VISUAL_CLASSES.capabilityNote} {
    display: block;
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.switchGroup} {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }
  .${VISUAL_CLASSES.switchControl} {
    appearance: none;
    -webkit-appearance: none;
    position: relative;
    box-sizing: border-box;
    flex: 0 0 auto;
    inline-size: 2.6em;
    block-size: 1.5em;
    margin: 0;
    border: 0;
    border-radius: 0.75em;
    background: var(--switch-unchecked-track-color, var(--disabled-text-color, #9e9e9e));
    cursor: pointer;
    transition: background-color 0.15s;
  }
  .${VISUAL_CLASSES.switchControl}::after {
    content: "";
    position: absolute;
    inset-block-start: 0.2em;
    inset-inline-start: 0.2em;
    inline-size: 1.1em;
    block-size: 1.1em;
    border-radius: 50%;
    background: var(--switch-unchecked-button-color, #ffffff);
    transition: transform 0.15s;
  }
  .${VISUAL_CLASSES.switchControl}:checked {
    background: var(--switch-checked-track-color, var(--primary-color, #03a9f4));
  }
  .${VISUAL_CLASSES.switchControl}:checked::after {
    transform: translateX(1.1em);
    background: var(--switch-checked-button-color, #ffffff);
  }
  .${VISUAL_CLASSES.switchControl}:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .${VISUAL_CLASSES.switchControl}[aria-busy="true"] {
    opacity: 0.6;
    cursor: progress;
  }
  .${VISUAL_CLASSES.switchControl}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.activeNotice} {
    margin: 4px 0 0;
    padding: 6px 8px;
    border-inline-start: 3px solid var(--success-color, #43a047);
    font-size: 0.82rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.activeNotice} > span {
    display: block;
  }
  .${VISUAL_CLASSES.activeNoticeWarning} {
    border-inline-start-color: var(--warning-color, #b26a00);
  }
  @media (max-width: 400px) {
    .${VISUAL_CLASSES.card} {
      padding: 10px 11px;
    }
    .${VISUAL_CLASSES.figures} {
      gap: 6px 12px;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    * {
      transition: none !important;
      animation: none !important;
    }
  }
`;
