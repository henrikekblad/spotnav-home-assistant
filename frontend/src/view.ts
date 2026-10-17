// Framework-free config, choices and small view model, so the rules are unit-testable without a DOM or a fake Home Assistant.

import { CARD_TYPE, type ChargerList, type ChargerSummary } from "./types";

/** How the price chart first shows: the full chart, or the slim 24-hour strip of charging periods. */
export type ChartPresentation = "full" | "compact";

export interface CardConfig {
  type: string;
  charger: string;
  /**
   * The chart's default when this browser has stored no choice for the charger; absent when the YAML
   * does not set it, so a config round-trips unchanged.
   */
  chart?: ChartPresentation;
  /**
   * Draw the price chart with finer detail (every hour labelled, more price steps, smaller marks and
   * text), for a card shown enlarged such as a wall screen's fullscreen view. Absent unless the YAML sets it.
   */
  chart_detail?: boolean;
}

/** The YAML options the editor keeps as they are when it changes the charger. */
export type CardPresentation = Pick<CardConfig, "chart" | "chart_detail">;

/**
 * The keys Home Assistant itself puts on any card's config (sizing in the sections view, the
 * visibility conditions, the masonry column). They are not card options: refusing them would make
 * `setConfig` throw for a valid card, and Home Assistant shows that as its bare "Configuration error".
 */
const HOME_ASSISTANT_CARD_KEYS: ReadonlySet<string> = new Set([
  "grid_options",
  "layout_options",
  "view_layout",
  "visibility",
  "card_mod",
]);

const CARD_KEYS: ReadonlySet<string> = new Set(["type", "charger", "chart", "chart_detail"]);

/**
 * Strict validation: only `type` (`custom:spotnav-card`), `charger`, `chart` and `chart_detail` are accepted, besides the keys
 * Home Assistant adds itself; other unknown options are refused rather than ignored. A missing or empty charger is the "pick one" state.
 * A supplied charger is a config-entry id and must be a non-empty string without surrounding
 * whitespace (trimming would point at a different charger). Error text is static.
 */
export function parseCardConfig(config: unknown): CardConfig {
  if (typeof config !== "object" || config === null || Array.isArray(config)) {
    throw new Error("SpotNav card: configuration must be an object");
  }
  const candidate = config as Record<string, unknown>;
  if (candidate.type !== `custom:${CARD_TYPE}`) {
    throw new Error(`SpotNav card: type must be "custom:${CARD_TYPE}"`);
  }
  if (Object.keys(candidate).some((key) => !CARD_KEYS.has(key) && !HOME_ASSISTANT_CARD_KEYS.has(key))) {
    throw new Error("SpotNav card: only the type, charger, chart and chart_detail options are supported");
  }
  const chart = candidate.chart;
  if (chart !== undefined && chart !== "full" && chart !== "compact") {
    throw new Error('SpotNav card: chart must be "full" or "compact"');
  }
  const detail = candidate.chart_detail;
  if (detail !== undefined && typeof detail !== "boolean") {
    throw new Error("SpotNav card: chart_detail must be true or false");
  }
  const presentation: CardPresentation = {
    ...(chart === undefined ? {} : { chart }),
    ...(detail === undefined ? {} : { chart_detail: detail }),
  };
  const charger = candidate.charger;
  if (charger === undefined || charger === "") {
    return { type: `custom:${CARD_TYPE}`, charger: "", ...presentation };
  }
  if (typeof charger !== "string" || charger.trim() === "" || charger.trim() !== charger) {
    throw new Error(
      "SpotNav card: charger must be a config entry id, with no surrounding whitespace",
    );
  }
  return { type: `custom:${CARD_TYPE}`, charger, ...presentation };
}

/** The config the editor emits: the chosen charger, keeping the chart options the YAML already set. */
export function editorConfig(chargerId: string, keep: CardPresentation = {}): CardConfig {
  return {
    type: `custom:${CARD_TYPE}`,
    charger: chargerId,
    ...(keep.chart === undefined ? {} : { chart: keep.chart }),
    ...(keep.chart_detail === undefined ? {} : { chart_detail: keep.chart_detail }),
  };
}

export interface ChargerOption {
  charger_id: string;
  label: string;
  available: boolean;
}

export function chargerOptions(list: ChargerList | null, current: string | null): ChargerOption[] {
  const options: ChargerOption[] = (list?.chargers ?? []).map((charger) => ({
    charger_id: charger.charger_id,
    label: charger.charger_name || charger.charger_id,
    available: charger.available,
  }));
  if (current !== null && current !== "" && !options.some((o) => o.charger_id === current)) {
    // An unresolved choice keeps the stable id as its label; the editor adds the "unavailable" wording.
    options.unshift({ charger_id: current, label: current, available: false });
  }
  return options;
}

export function stubChargerId(list: ChargerList | null): string | null {
  const available = (list?.chargers ?? []).filter((charger) => charger.available);
  return available.length === 1 ? (available[0] as ChargerSummary).charger_id : null;
}
