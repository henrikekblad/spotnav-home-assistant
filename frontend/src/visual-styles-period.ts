// The history and power bars with their scale, legends and readouts.

import { VISUAL_CLASSES } from "./visual-classes";

export const PERIOD_CHART_STYLES = `\
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
    min-height: 40px;
    padding: 4px 12px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: transparent;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 20px;
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
    min-height: 40px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.historyMonthPicker} > button {
    flex: none;
    min-width: 40px;
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
    touch-action: pan-y;
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
      var(--spotnav-energy-expensive, #c62828) var(--spotnav-day-dear, 0%),
      var(--spotnav-energy-cheap, #2e7d32)
    );
  }
  /* A money bar: the filled cost with, above it, the hatched saving. The hatch is a diagonal stripe in the text colour
     over a translucent fill, so it reads on a light and a dark card alike; a saving below zero hatches in the warning
     colour. */
  .${VISUAL_CLASSES.historyBarFill}[data-money="true"] {
    display: flex;
    flex-direction: column;
    background: none;
    overflow: hidden;
  }
  .${VISUAL_CLASSES.historyBarPart} {
    display: block;
    min-height: 1px;
    flex-basis: 0;
    flex-shrink: 0;
  }
  .${VISUAL_CLASSES.historyBarPart}[data-part="cost"] {
    background: var(--spotnav-history-cost, var(--primary-color, #03a9f4));
  }
  .${VISUAL_CLASSES.historyBarPart}[data-part="saving"] {
    background:
      repeating-linear-gradient(
        -45deg,
        var(--spotnav-history-cost, var(--primary-color, #03a9f4)) 0 2px,
        transparent 2px 5px
      ),
      color-mix(in srgb, var(--spotnav-history-cost, var(--primary-color, #03a9f4)) 18%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--spotnav-history-cost, var(--primary-color, #03a9f4)) 55%, transparent);
  }
  .${VISUAL_CLASSES.historyBarPart}[data-part="extra"] {
    background:
      repeating-linear-gradient(-45deg, var(--warning-color, #ffa600) 0 2px, transparent 2px 5px),
      color-mix(in srgb, var(--warning-color, #ffa600) 22%, transparent);
    box-shadow: inset 0 0 0 1px var(--warning-color, #ffa600);
  }
  .${VISUAL_CLASSES.historyLegend} {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 14px;
    margin: 2px 0 0;
    padding: 0;
    list-style: none;
    font-size: 0.78rem;
  }
  .${VISUAL_CLASSES.historyLegend} > li {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .${VISUAL_CLASSES.historyLegend} .${VISUAL_CLASSES.historyBarPart} {
    flex: none;
    width: 0.9rem;
    height: 0.65rem;
    border-radius: 2px;
  }
  /* A text button that reads as a link (show or hide details): no frame, underlined, a finger-sized target. */
  .${VISUAL_CLASSES.linkButton} {
    display: inline-flex;
    align-items: center;
    min-height: 40px;
    margin: 0;
    padding: 0 2px;
    border: 0;
    background: none;
    font: inherit;
    color: var(--primary-text-color, #212121);
    text-decoration: underline;
    text-underline-offset: 3px;
    cursor: pointer;
  }
  /* Settings that are off, folded under one link ("More fees"); a row opened from it is a normal row. */
  .${VISUAL_CLASSES.settingsMore} > summary {
    list-style: none;
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.settingsMore} > summary::-webkit-details-marker {
    display: none;
  }
  .${VISUAL_CLASSES.settingsMore} > summary::after {
    content: " ▾";
    text-decoration: none;
  }
  .${VISUAL_CLASSES.settingsMore}[open] > summary::after {
    content: " ▴";
  }
  .${VISUAL_CLASSES.linkButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
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
  /*
   * The history dialog's Power view. The car and the house are two shades of the theme's primary colour (the house
   * the lighter, mixed with the card's background so it holds in light and dark); a counted peak is outlined in the
   * text colour and tagged with its part's number; the import target is a dashed line in the text colour, as the
   * price chart's now line; a held or paused day is a small dot under its bar in the warning colour.
   */
  .${VISUAL_CLASSES.historyViews} {
    display: flex;
    gap: 6px;
  }
  .${VISUAL_CLASSES.powerChart} {
    position: relative;
  }
  /* The y axis beside the bars: the marks row and the day axis keep its column empty, so every row lines up. */
  .${VISUAL_CLASSES.powerFrame} {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    column-gap: 4px;
    margin-top: 1rem;
    min-width: 0;
  }
  .${VISUAL_CLASSES.powerTicks} {
    position: relative;
    min-width: 2.6rem;
    font-size: 0.7rem;
    font-variant-numeric: tabular-nums;
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.powerTicks} > span {
    position: absolute;
    right: 0;
    transform: translateY(50%);
    line-height: 1;
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.powerGrid} {
    position: absolute;
    left: 0;
    right: 0;
    height: 0;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    pointer-events: none;
  }
  .${VISUAL_CLASSES.powerPlot} {
    position: relative;
    height: 8rem;
  }
  /* A bar under the pointer or chosen is underlined, not filled behind: no grey cap that reads as data. */
  .${VISUAL_CLASSES.historyBar}.${VISUAL_CLASSES.powerBar}:hover,
  .${VISUAL_CLASSES.historyBar}.${VISUAL_CLASSES.powerBar}[aria-pressed="true"] {
    background: transparent;
  }
  .${VISUAL_CLASSES.powerBar}:hover > .${VISUAL_CLASSES.powerStack},
  .${VISUAL_CLASSES.powerBar}[aria-pressed="true"] > .${VISUAL_CLASSES.powerStack} {
    box-shadow: 0 4px 0 var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.powerBar} {
    position: relative;
    align-items: stretch;
    flex-direction: column;
    justify-content: flex-end;
  }
  .${VISUAL_CLASSES.powerStack} {
    position: relative;
    display: flex;
    flex-direction: column;
    min-height: 2px;
    border-radius: 3px 3px 0 0;
  }
  .${VISUAL_CLASSES.powerStack}[data-counted] {
    outline: 2px solid var(--primary-text-color, #212121);
    outline-offset: 0;
  }
  .${VISUAL_CLASSES.powerStack}[data-incomplete="true"] {
    opacity: 0.45;
  }
  /* A day or hour from Home Assistant's statistics: its colours paler. Hatching means saved money in the cards' charts,
   * nothing else. */
  .${VISUAL_CLASSES.powerStack}[data-imported="true"] {
    opacity: 0.6;
  }
  .${VISUAL_CLASSES.powerSwatch}.${VISUAL_CLASSES.powerImportedSwatch} {
    background-color: color-mix(in srgb, var(--primary-color, #03a9f4) 60%, var(--ha-card-background, var(--card-background-color, #ffffff)));
  }
  /* The month's peak where it lifted the target: a thin solid line in the warning colour, across each bar it held
   * for (the bars' gaps bridged), distinct from the target's dashed line. */
  .${VISUAL_CLASSES.powerPeak} {
    position: absolute;
    left: -1px;
    right: -1px;
    height: 0;
    border-top: 2px solid var(--warning-color, #ffa600);
    pointer-events: none;
    z-index: 1;
  }
  .${VISUAL_CLASSES.powerSwatch}.${VISUAL_CLASSES.powerPeakSwatch} {
    height: 0;
    border-radius: 0;
    border-top: 2px solid var(--warning-color, #ffa600);
  }
  .${VISUAL_CLASSES.powerCar},
  .${VISUAL_CLASSES.powerHouse},
  .${VISUAL_CLASSES.powerUnsplit} {
    display: block;
    flex-basis: 0;
    min-height: 0;
  }
  .${VISUAL_CLASSES.powerCar} {
    background: var(--primary-color, #03a9f4);
    border-radius: 3px 3px 0 0;
  }
  .${VISUAL_CLASSES.powerHouse} {
    background: color-mix(in srgb, var(--primary-color, #03a9f4) 50%, var(--ha-card-background, var(--card-background-color, #ffffff)));
  }
  .${VISUAL_CLASSES.powerUnsplit} {
    background: color-mix(in srgb, var(--secondary-text-color, #727272) 55%, var(--ha-card-background, var(--card-background-color, #ffffff)));
    border-radius: 3px 3px 0 0;
  }
  .${VISUAL_CLASSES.powerTag} {
    position: absolute;
    bottom: calc(100% + 3px);
    left: 50%;
    transform: translateX(-50%);
    font-size: 0.65rem;
    font-weight: 600;
    line-height: 1;
    white-space: nowrap;
    color: var(--primary-text-color, #212121);
    pointer-events: none;
  }
  .${VISUAL_CLASSES.powerEmpty} {
    height: 0.125rem;
    background: var(--divider-color, #e0e0e0);
  }
  .${VISUAL_CLASSES.powerTarget} {
    position: absolute;
    left: 0;
    right: 0;
    height: 0;
    border-top: 1.5px dashed var(--primary-text-color, #212121);
    pointer-events: none;
    z-index: 1;
  }
  .${VISUAL_CLASSES.powerTargetLabel} {
    position: absolute;
    right: 0;
    bottom: 2px;
    padding: 0 3px;
    font-size: 0.68rem;
    font-variant-numeric: tabular-nums;
    color: var(--primary-text-color, #212121);
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    border-radius: 3px;
  }
  .${VISUAL_CLASSES.powerTargetLabel}[data-placement="below"] {
    bottom: auto;
    top: 2px;
  }
  .${VISUAL_CLASSES.powerMarks} {
    display: flex;
    gap: 2px;
    height: 0.45rem;
  }
  .${VISUAL_CLASSES.powerMarks} > span {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    justify-content: center;
    align-items: center;
  }
  .${VISUAL_CLASSES.powerMarks} > span[data-mark]::after {
    content: "";
    width: 0.32rem;
    height: 0.32rem;
    border-radius: 50%;
    background: var(--warning-color, #ffa600);
  }
  .${VISUAL_CLASSES.powerMarks} > span[data-mark="paused"]::after {
    background: transparent;
    box-shadow: inset 0 0 0 1.5px var(--warning-color, #ffa600);
  }
  .${VISUAL_CLASSES.powerLegend} {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 0.8rem;
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.powerLegendItem} {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.powerSwatch} {
    flex: none;
    width: 0.75rem;
    height: 0.75rem;
    border-radius: 3px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 0.6rem;
    font-weight: 600;
    line-height: 1;
    color: var(--primary-text-color, #212121);
  }
  .${VISUAL_CLASSES.powerSwatch}.${VISUAL_CLASSES.powerTargetSwatch} {
    height: 0;
    border-radius: 0;
    border-top: 1.5px dashed var(--primary-text-color, #212121);
  }
  .${VISUAL_CLASSES.powerSwatch}.${VISUAL_CLASSES.powerCountedSwatch} {
    box-shadow: inset 0 0 0 1.5px var(--primary-text-color, #212121);
  }
  .${VISUAL_CLASSES.powerSwatch}.${VISUAL_CLASSES.powerMarkSwatch}::after {
    content: "";
    width: 0.32rem;
    height: 0.32rem;
    border-radius: 50%;
    background: var(--warning-color, #ffa600);
  }
  .${VISUAL_CLASSES.powerDayHead} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
  }
  .${VISUAL_CLASSES.powerDayHead} > h4 {
    margin: 0;
    flex: 1 1 12rem;
    min-width: 0;
  }
`;
