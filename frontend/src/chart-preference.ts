// The chart's per-viewer display preferences, remembered in this browser only:
//   * whether the price chart is collapsed to its strip, per charger;
//   * the chart's time axis ("day" or "from_now"), one choice for every SpotNav card in this browser.
//
// The one place the card touches browser storage, and only for these per-viewer conveniences. Every
// access is guarded: a private window, blocked site data or a storage that throws simply means nothing
// is remembered, and the card renders from its default instead (the YAML option, or the "day" axis).

import { CHART_AXIS_MODES, DEFAULT_CHART_AXIS, type ChartAxisMode } from "./chart";
import type { ChartPresentation } from "./view";

/** The part of the Web Storage interface used here, so a test can hand in its own. */
export interface PreferenceStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function chartPreferenceKey(chargerId: string): string {
  return `spotnav-card.chart.${chargerId}`;
}

/** This browser's storage, or `null` where there is none or reaching it throws. */
export function browserStore(): PreferenceStore | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** The stored choice for this charger: `true` collapsed, `false` full, `null` when nothing usable is stored. */
export function readChartCollapsed(store: () => PreferenceStore | null, chargerId: string): boolean | null {
  try {
    const value = store()?.getItem(chartPreferenceKey(chargerId)) ?? null;
    return value === "compact" ? true : value === "full" ? false : null;
  } catch {
    return null;
  }
}

export function writeChartCollapsed(
  store: () => PreferenceStore | null,
  chargerId: string,
  collapsed: boolean,
): void {
  try {
    store()?.setItem(chartPreferenceKey(chargerId), collapsed ? "compact" : "full");
  } catch {
    // Nothing is remembered in this browser; the choice still holds for this card.
  }
}

/** The stored choice wins; without one, the YAML `chart` option; without that, the full chart. */
export function initialChartCollapsed(stored: boolean | null, configured: ChartPresentation | undefined): boolean {
  return stored ?? configured === "compact";
}

/** The chart axis is one choice for the whole browser, not per charger. */
export const CHART_AXIS_KEY = "spotnav-card.chart-axis";

/**
 * Sent on the window after this browser's axis choice changed, so every SpotNav card on the page redraws;
 * cards in other tabs hear the storage event instead.
 */
export const CHART_AXIS_EVENT = "spotnav-card-chart-axis";

/** The stored axis, or the wall-clock day when nothing usable is stored or storage is unavailable. */
export function readChartAxis(store: () => PreferenceStore | null): ChartAxisMode {
  try {
    const value = store()?.getItem(CHART_AXIS_KEY) ?? null;
    return CHART_AXIS_MODES.find((mode) => mode === value) ?? DEFAULT_CHART_AXIS;
  } catch {
    return DEFAULT_CHART_AXIS;
  }
}

/** Remember the axis in this browser; `false` when it could not be stored (it then holds for this page only). */
export function writeChartAxis(store: () => PreferenceStore | null, mode: ChartAxisMode): boolean {
  try {
    const target = store();
    if (target === null) {
      return false;
    }
    target.setItem(CHART_AXIS_KEY, mode);
    return true;
  } catch {
    return false;
  }
}
