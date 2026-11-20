// The Settings popover: its sections, rows, editors, car pictures, site fields and number fields.

import { VISUAL_CLASSES } from "./visual-classes";

export const POPOVER_STYLES = `\
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
  .${VISUAL_CLASSES.settingsSectionHeading} > svg {
    vertical-align: -3px;
    margin-right: 6px;
  }
  /*
   * A value row in the app's compact style: the label left, the value right-aligned at the same size. A
   * changeable row is one finger-sized button: the value's button covers the whole row, a chevron says it opens,
   * and the value stays in the normal text colour (the accent on white is too faint to read).
   */
  .${VISUAL_CLASSES.settingRow} {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 4px 12px;
    min-width: 0;
    padding: 3px 0;
  }
  .${VISUAL_CLASSES.settingRow}[data-tappable] {
    position: relative;
    align-items: center;
    min-height: 44px;
    padding: 4px 0;
    box-sizing: border-box;
    border-top: 1px solid var(--divider-color, #e0e0e0);
  }
  .${VISUAL_CLASSES.settingRow}[data-tappable]:has(> .${VISUAL_CLASSES.settingRowEditable}:focus-visible) {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
    border-radius: 4px;
  }
  .${VISUAL_CLASSES.settingRowLabel} {
    flex: 0 1 auto;
    min-width: 0;
    color: var(--secondary-text-color, #727272);
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.settingRowValue} {
    flex: 1 1 auto;
    min-width: 0;
    text-align: right;
    font: inherit;
    font-weight: 500;
    color: var(--primary-text-color, #212121);
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.settingRowEditable} {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    background: none;
    border: 0;
    padding: 0;
    cursor: pointer;
    color: var(--primary-text-color, #212121);
  }
  .${VISUAL_CLASSES.settingRowEditable}:focus-visible {
    outline: none;
  }
  /* The whole row is the button's target. */
  .${VISUAL_CLASSES.settingRowEditable}::before {
    content: "";
    position: absolute;
    inset: 0;
  }
  .${VISUAL_CLASSES.settingRowEditable}::after {
    content: "";
    flex: none;
    width: 0.45em;
    height: 0.45em;
    margin-right: 4px;
    border-top: 2px solid var(--secondary-text-color, #727272);
    border-right: 2px solid var(--secondary-text-color, #727272);
    transform: rotate(45deg);
  }
  .${VISUAL_CLASSES.settingRowEditable}:disabled {
    cursor: default;
  }
  .${VISUAL_CLASSES.settingRowEditable}:disabled::after {
    visibility: hidden;
  }
  /* What is left before battery control can run: numbered steps in a box, each with its own button. */
  .${VISUAL_CLASSES.batteryNeeds} {
    margin: 8px 0;
    padding: 10px 12px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.batteryNeedsHeading} {
    margin: 0 0 6px;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.batteryNeeds} ol {
    margin: 0;
    padding-left: 1.4em;
  }
  .${VISUAL_CLASSES.batteryNeeds} li + li {
    margin-top: 10px;
  }
  .${VISUAL_CLASSES.batteryNeedAction} {
    display: block;
    margin-top: 6px;
  }
  .${VISUAL_CLASSES.settingRowHelp} {
    margin: 0 0 4px;
    font-size: 0.82rem;
    color: var(--secondary-text-color, #727272);
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.settingsDivider} {
    border: 0;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    margin: 8px 0;
  }
  .${VISUAL_CLASSES.settingRowSuggestion} {
    display: block;
    margin: 4px 0;
    padding: 0;
    background: none;
    border: 0;
    font: inherit;
    cursor: pointer;
    color: var(--primary-color, #03a9f4);
    text-align: left;
  }
  /*
   * The camera's frame editor: the picture at the dialog's width, the frame over it (the rest dimmed), a corner
   * handle with a 44 px target in each corner, and the crop's preview below. Pointer events serve touch and
   * mouse alike, so the browser must not scroll or zoom under a finger on the picture.
   */
  .${VISUAL_CLASSES.cameraStage} {
    position: relative;
    max-width: 100%;
    margin: 4px 0 8px;
    overflow: hidden;
    line-height: 0;
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
  }
  .${VISUAL_CLASSES.cameraImage} {
    display: block;
    width: 100%;
    height: auto;
    pointer-events: none;
  }
  .${VISUAL_CLASSES.cameraFrame} {
    position: absolute;
    box-sizing: border-box;
    border: 2px solid var(--primary-color, #03a9f4);
    box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.45);
    cursor: move;
    touch-action: none;
  }
  .${VISUAL_CLASSES.cameraFrame}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.cameraHandle} {
    position: absolute;
    box-sizing: border-box;
    min-width: 44px;
    min-height: 44px;
    margin: -22px;
    background: radial-gradient(circle, var(--primary-color, #03a9f4) 0 7px, transparent 8px);
    touch-action: none;
  }
  .${VISUAL_CLASSES.cameraHandle}[data-corner="nw"] { left: 0; top: 0; cursor: nwse-resize; }
  .${VISUAL_CLASSES.cameraHandle}[data-corner="ne"] { right: 0; top: 0; cursor: nesw-resize; }
  .${VISUAL_CLASSES.cameraHandle}[data-corner="sw"] { left: 0; bottom: 0; cursor: nesw-resize; }
  .${VISUAL_CLASSES.cameraHandle}[data-corner="se"] { right: 0; bottom: 0; cursor: nwse-resize; }
  .${VISUAL_CLASSES.cameraPreview} {
    max-width: 100%;
    margin: 4px 0 8px;
    border-radius: 4px;
    background-color: var(--secondary-background-color, #f5f5f5);
    background-repeat: no-repeat;
  }
  .${VISUAL_CLASSES.cameraTools} {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 4px 0 8px;
  }
  /* The cars' tabs in the Car section's header: compact text tabs ("EV6 | Testbil"), the open one in the accent. */
  .${VISUAL_CLASSES.vehicleTabs} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    min-width: 0;
    margin: 0 0 4px;
  }
  .${VISUAL_CLASSES.vehicleTab} {
    min-height: 44px;
    padding: 0 10px;
    font: inherit;
    color: var(--secondary-text-color, #727272);
    background: none;
    border: 0;
    cursor: pointer;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.vehicleTab} + .${VISUAL_CLASSES.vehicleTab} {
    border-left: 1px solid var(--divider-color, #e0e0e0);
  }
  .${VISUAL_CLASSES.vehicleTab}:first-child {
    padding-left: 0;
  }
  .${VISUAL_CLASSES.vehicleTab}[aria-selected="true"] {
    color: var(--primary-color, #03a9f4);
    font-weight: 600;
  }
  /* A car's reference pictures under its row: two equal tiles, day and night, each the editor's slot picture. */
  .${VISUAL_CLASSES.referenceTiles} {
    margin: 0 0 8px;
  }
  .${VISUAL_CLASSES.referenceTile} {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
    padding: 0;
    font: inherit;
    color: inherit;
    text-align: start;
    background: none;
    border: 0;
  }
  button.${VISUAL_CLASSES.referenceTile} {
    cursor: pointer;
  }
  .${VISUAL_CLASSES.referenceTileLabel} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.referenceTile}[data-state="empty"] .${VISUAL_CLASSES.referenceSlotPicture} {
    border: 1px dashed var(--divider-color, #e0e0e0);
  }
  /* A car's reference pictures: two equal slots, day and night, each its picture, its button and a quiet Delete. */
  .${VISUAL_CLASSES.referenceSlots} {
    margin: 8px 0;
  }
  .${VISUAL_CLASSES.referenceSlots},
  .${VISUAL_CLASSES.referenceTiles} {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .${VISUAL_CLASSES.referenceSlot} {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.referenceSlotTitle} {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
  }
  .${VISUAL_CLASSES.referenceSlotPicture} {
    display: flex;
    align-items: center;
    justify-content: center;
    aspect-ratio: 16 / 9;
    overflow: hidden;
    border-radius: 6px;
    background: var(--secondary-background-color, #f2f2f2);
  }
  .${VISUAL_CLASSES.referenceSlot}[data-state="empty"] .${VISUAL_CLASSES.referenceSlotPicture},
  .${VISUAL_CLASSES.referenceSlot}[data-state="busy"] .${VISUAL_CLASSES.referenceSlotPicture} {
    border: 1px dashed var(--divider-color, #e0e0e0);
  }
  .${VISUAL_CLASSES.referenceSlot}[data-state="busy"] .${VISUAL_CLASSES.referenceSlotPicture} {
    opacity: 0.5;
  }
  .${VISUAL_CLASSES.referenceSlotImage} {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .${VISUAL_CLASSES.referenceSlotImage}[hidden],
  .${VISUAL_CLASSES.referenceSlotEmpty}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.referenceSlotEmpty} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.referenceSlot} > p {
    margin: 0;
  }
  .${VISUAL_CLASSES.referenceSlotStatus} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.referenceSlotStatus}.${VISUAL_CLASSES.referenceSlotError} {
    color: var(--error-color, #db4437);
  }
  .${VISUAL_CLASSES.referenceSlotTools} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 12px;
  }
  .${VISUAL_CLASSES.referenceDelete} {
    min-height: 36px;
    padding: 0 4px;
    font: inherit;
    font-size: 0.85rem;
    color: var(--secondary-text-color, #727272);
    background: none;
    border: 0;
    text-decoration: underline;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.referenceDelete}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.referenceDelete}:disabled {
    cursor: default;
    opacity: 0.5;
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
  /* Derived mode: each phase's legend heads several fields, so it stands clear of the phase above. */
  .${VISUAL_CLASSES.entityMeters}[data-mode="derived_phase_current"] .${VISUAL_CLASSES.entityLine} + .${VISUAL_CLASSES.entityLine} {
    margin-top: 12px;
  }
  .${VISUAL_CLASSES.entityLine} > .${VISUAL_CLASSES.siteLegend} {
    font-weight: 500;
    color: var(--primary-text-color, #212121);
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
  .${VISUAL_CLASSES.sendDebugRef} {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 8px 0 0;
  }
  .${VISUAL_CLASSES.sendDebugRef} > code {
    font-size: 1.2rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    user-select: all;
    -webkit-user-select: all;
    cursor: text;
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
  .${VISUAL_CLASSES.planDialog} .${VISUAL_CLASSES.settingsField} > .${VISUAL_CLASSES.settingsLabel},
  .${VISUAL_CLASSES.planDialog} .${VISUAL_CLASSES.settingsField} > .${VISUAL_CLASSES.settingsHead} > .${VISUAL_CLASSES.settingsLabel},
  .${VISUAL_CLASSES.planDialog} .${VISUAL_CLASSES.settingsCheckRow} > .${VISUAL_CLASSES.settingsLabel} {
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
`;
