// The house chart's series a viewer may hide from its legend, per view, remembered in this browser only (the same
// per-viewer convenience as `chart-preference.ts`: every access guarded, nothing stored means the defaults).
//
// A series is hidden by its key; the chart leaves it out (and rescales the kW axis to what is left), the readout under
// the chart leaves its figures out, and the legend item stays, struck through and faded, to bring it back.

import { browserStore, type PreferenceStore } from "./chart-preference";
import type { HouseChartView } from "./house-chart";

export type HouseSeries = "house" | "heat" | "forecast" | "sun" | "car" | "battery" | "buy" | "sellPrice" | "charge" | "sell";

/** What a view starts with hidden: the car, which would otherwise crowd the house's bars. */
export const HIDDEN_BY_DEFAULT: Readonly<Record<HouseChartView, readonly HouseSeries[]>> = {
  energy: ["car"],
  price: [],
};

/** Hidden series per view, as the card keeps them; a view without an entry has its defaults. */
export type HiddenSeries = Partial<Record<HouseChartView, readonly string[]>>;

export function hiddenSeriesKey(view: HouseChartView): string {
  return `spotnav-card.house.hidden.${view}`;
}

export function hiddenIn(hidden: HiddenSeries | undefined, view: HouseChartView): ReadonlySet<string> {
  return new Set(hidden?.[view] ?? HIDDEN_BY_DEFAULT[view]);
}

export function readHiddenSeries(store: () => PreferenceStore | null = browserStore): HiddenSeries {
  const out: Record<string, readonly string[]> = {};
  for (const view of ["energy", "price"] as const) {
    try {
      const raw = store()?.getItem(hiddenSeriesKey(view)) ?? null;
      const parsed: unknown = raw === null ? null : JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.every((item) => typeof item === "string")) {
        out[view] = parsed as string[];
      }
    } catch {
      // Nothing usable stored: the view's defaults.
    }
  }
  return out;
}

export function writeHiddenSeries(view: HouseChartView, hidden: readonly string[], store: () => PreferenceStore | null = browserStore): void {
  try {
    store()?.setItem(hiddenSeriesKey(view), JSON.stringify(hidden));
  } catch {
    // Nothing is remembered; the choice still holds until the card reloads.
  }
}
