// Whether the price chart is collapsed to its strip: remembered per charger and per browser.
//
// The one place the card touches browser storage, and only for this one per-viewer convenience. Every
// access is guarded: a private window, blocked site data or a storage that throws simply means nothing
// is remembered, and the card renders from the YAML default instead.

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
