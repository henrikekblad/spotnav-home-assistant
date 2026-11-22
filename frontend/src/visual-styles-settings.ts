// The compact settings row, the field editors, the market and car rows, the setup steps and the notices that sit among them.

import { VISUAL_CLASSES } from "./visual-classes";

export const SETTINGS_FORM_STYLES = `
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
  /* A text field and its button on one line (the beta e-mail and its Save), wrapping on a narrow card. */
  .spotnav-settings-input-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .spotnav-settings-input-row > .spotnav-settings-input {
    flex: 1 1 12rem;
    min-width: 0;
  }
  .spotnav-settings-input-row > button {
    flex: none;
  }
  .spotnav-settings-check-row > input[type="checkbox"] {
    flex: none;
    margin: 0;
    min-height: 0;
    padding: 0;
    border: 0;
    background: none;
  }
  /* Every field's label, the deadline checkbox's beside its box included: one size, one weight. */
  .spotnav-settings-label {
    font-size: 0.85em;
    font-weight: 400;
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
  .spotnav-settings-textarea {
    width: 100%;
    min-height: 6rem;
    resize: vertical;
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
  .spotnav-settings-slider {
    width: 100%;
    min-width: 0;
    /* A fixed box, so the "full" mark below can cross the track at its middle in every browser. */
    height: 1.25rem;
    margin: 0;
    accent-color: var(--primary-color, #03a9f4);
  }
  .spotnav-settings-slider:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .spotnav-settings-slider:disabled {
    opacity: 0.55;
  }
  /*
   * A slider's label row (energy, charge target, charging periods, current): the label, and the value the slider stands for (an
   * amount, "Fill" or a percentage) at its end, as the app writes "Charging ........ 20.0 kWh". The slider
   * under it takes the full width.
   */
  .spotnav-settings-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }
  .spotnav-settings-amount {
    white-space: nowrap;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    color: var(--primary-color, #03a9f4);
  }
  /*
   * The energy slider's track with its "full" mark: a thin line across the track at the battery's room
   * (\`--spotnav-mark\`, 0..1 of the track, inset by half a thumb at each end) and its word under it. The
   * plan's target slider marks the level now and the car's limit the same way; a word that would touch
   * another goes a line lower (\`--spotnav-mark-level\`, the track as deep as \`--spotnav-mark-lines\`), and
   * one at an end is moved inside the track (\`--spotnav-mark-shift\`) while its line stays at the tick.
   */
  .spotnav-settings-track {
    position: relative;
    min-width: 0;
  }
  .spotnav-settings-track:has(> .spotnav-settings-full-mark:not([hidden])) {
    padding-bottom: calc(0.4rem + (var(--spotnav-mark-lines, 1) - 1) * 0.825em);
  }
  .spotnav-settings-full-mark {
    position: absolute;
    top: 0;
    left: calc(8px + (100% - 16px) * var(--spotnav-mark, 0));
    transform: translateX(calc(-50% + var(--spotnav-mark-shift, 0px)));
    padding-top: calc(1.125rem + var(--spotnav-mark-level, 0) * 1.1em);
    font-size: 0.75em;
    line-height: 1;
    white-space: nowrap;
    color: var(--secondary-text-color, #727272);
    pointer-events: none;
  }
  .spotnav-settings-full-mark::before {
    content: "";
    position: absolute;
    /* Across the 1.25rem slider's track, centred on it. */
    top: 0.125rem;
    left: calc(50% - var(--spotnav-mark-shift, 0px));
    height: 1rem;
    transform: translateX(-50%);
    border-left: 2px solid var(--primary-text-color, #212121);
  }
  .spotnav-settings-full-mark[hidden] {
    display: none;
  }
  /*
   * The minimum charge level on the plan's target slider: the track from 0 to it in a darker tone of the
   * fill, and "min 30 %" under the middle of that part (no line across the track, unlike the "full" mark).
   */
  .spotnav-settings-floor-segment {
    position: absolute;
    top: 0.4375rem;
    left: 8px;
    width: calc((100% - 16px) * var(--spotnav-mark, 0));
    height: 0.375rem;
    border-radius: 0.1875rem 0 0 0.1875rem;
    background: color-mix(in srgb, var(--primary-color, #03a9f4) 55%, #000);
    pointer-events: none;
  }
  .spotnav-settings-floor-segment[hidden] {
    display: none;
  }
  .spotnav-settings-floor-mark::before {
    display: none;
  }
  /*
   * The minimum's own slider: past the car's target it cannot go, so that part of the track is hatched
   * (from \`--spotnav-from\`, 0..1 of the track) and the target is marked where it starts.
   */
  .spotnav-slider-blocked {
    position: absolute;
    top: 0.4375rem;
    left: calc(8px + (100% - 16px) * var(--spotnav-from, 1));
    right: 8px;
    height: 0.375rem;
    border-radius: 0 0.1875rem 0.1875rem 0;
    background: repeating-linear-gradient(
      135deg,
      var(--card-background-color, #fff) 0 3px,
      var(--secondary-text-color, #727272) 3px 6px
    );
    pointer-events: none;
  }
  .spotnav-slider-ends {
    display: flex;
    justify-content: space-between;
    padding: 0 2px;
    font-size: 0.75em;
    color: var(--secondary-text-color, #727272);
  }
  /* A percent editor's value, large above its slider; "(default)" muted after a target none stored. */
  .spotnav-value-amount {
    margin: 8px 0 4px;
    font-size: 1.75em;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    color: var(--primary-color, #03a9f4);
  }
  .spotnav-value-default {
    font-size: 0.5em;
    font-weight: 400;
    color: var(--secondary-text-color, #727272);
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
  /*
   * The help line right under a slider (what fills the battery, the car ends the charge) sits on it. Only
   * that note: the others follow a fieldset, a date or a checkbox row and keep their space above.
   */
  /* The plan's info line under the vehicle: it breaks between its parts, never inside one. */
  .spotnav-settings-fact {
    white-space: nowrap;
  }
  .spotnav-settings-field:has(> .spotnav-settings-track) + .spotnav-settings-note {
    margin: 0;
  }
  .spotnav-site-fieldset[data-part="mode"] {
    margin-top: 12px;
  }
  .spotnav-capability[data-soc-row] {
    padding: 2px 0;
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
    font-size: 0.85em;
    color: var(--primary-text-color, #212121);
  }
  /* Support's buttons side by side, wrapping on a narrow card. */
  .spotnav-settings-support-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .spotnav-market-source {
    margin: 4px 0 0;
    font-size: 0.85em;
  }
  .spotnav-market-source a,
  [data-entsoe-howto] a {
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
  /* Out of sight but read aloud: a 1 px box, so a long hidden text never widens the page. */
  .spotnav-visually-hidden {
    position: absolute;
    max-width: 1px;
    max-height: 1px;
    margin: -1px;
    padding: 0;
    border: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
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
  /* The car over the cells (as in the app): the cells' look, the row's full width, the name in bold. */
  .spotnav-car-button {
    width: 100%;
    margin: 0 0 8px;
    padding: 6px 12px;
  }
  .spotnav-car-button > .spotnav-bar-value > .spotnav-action-icon {
    width: 1.35em;
    height: 1.35em;
    color: var(--primary-color, #03a9f4);
  }
  .spotnav-car-button-name {
    font-weight: 600;
  }
  /* The open question "which car is plugged in?": the caption asks, in the accent, with the accent outline. */
  .spotnav-car-button[data-asking="true"] {
    border: 2px solid var(--primary-color, #03a9f4);
    background: color-mix(in srgb, var(--primary-color, #03a9f4) 12%, transparent);
  }
  .spotnav-car-button[data-asking="true"] > .spotnav-bar-caption {
    color: var(--primary-color, #03a9f4);
    opacity: 1;
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
    min-height: 40px;
    padding: 6px 14px;
    border-radius: 20px;
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
    color: var(--primary-text-color, #212121);
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
  /** "Get started" in the attention dialog: a heading over its rows, apart from the issues above it. */
  .spotnav-setup-heading {
    margin: 12px 0 0;
    font-size: 0.9rem;
    font-weight: 500;
  }
  section:first-child > .spotnav-setup-heading {
    margin-top: 0;
  }
  /** "Get started": a box per thing left, apart from each other. */
  .spotnav-suggestion[data-banner="setup-row"] {
    margin-top: 10px;
  }
  .spotnav-suggestion[data-banner="setup-row"]:first-child {
    margin-top: 0;
  }
  /** "Automations": a blueprint's name over its sentence, each blueprint apart from the one above. */
  /** "SpotNav has been updated" (self-heal.ts): first in the card, apart from what follows. */
  .spotnav-suggestion[data-card-updated]:first-child {
    margin: 0 0 8px;
  }
  .spotnav-suggestion[data-blueprint] {
    margin-top: 14px;
  }
  .spotnav-automation-name {
    margin: 0 0 2px;
    font-weight: 500;
  }
  .spotnav-automation-use {
    margin: 4px 0 0;
    font-size: 0.9em;
  }
  .spotnav-automation-use a {
    color: var(--primary-color, #03a9f4);
  }
  .spotnav-pause-choices,
  .spotnav-strategy-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-start;
    margin-top: 8px;
  }
  .spotnav-choice-button {
    font: inherit;
    min-height: 40px;
    padding: 6px 14px;
    border-radius: 20px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--secondary-background-color, transparent);
    color: var(--primary-text-color, #212121);
    cursor: pointer;
  }
  /** A choice's second, smaller line (the same-area question's fees per price). */
  .spotnav-choice-label,
  .spotnav-choice-detail {
    display: block;
  }
  .spotnav-choice-detail {
    font-size: 0.8em;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-choice-button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  /* The strategy in force: an outline in the primary colour and a check mark, never pale like one not to be had. */
  .spotnav-strategy-row > .spotnav-choice-button[aria-pressed="true"] {
    opacity: 1;
    border: 2px solid var(--primary-color, #03a9f4);
    font-weight: 500;
  }
  .spotnav-strategy-row > .spotnav-choice-button[aria-pressed="true"]::before {
    content: "✓ ";
  }
  /**
   * "Which car is plugged in?": a question that decides the plan, so it stands out from the status. The same-area
   * electricity price question in Settings stands out the same way.
   */
  .spotnav-suggestion[data-banner="identify"],
  .spotnav-suggestion[data-banner="price"],
  .spotnav-suggestion[data-banner="setup-row"] {
    padding: 8px 10px;
    border-left: 3px solid var(--primary-color, #03a9f4);
    border-radius: 4px;
    background: var(--secondary-background-color, transparent);
  }
  .spotnav-suggestion[data-banner="identify"] .spotnav-choice-button[data-likely="true"] {
    border-color: var(--primary-color, #03a9f4);
    font-weight: 500;
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
`;
