// Framework-free config, choices and small view model, so the rules are unit-testable without a DOM or a fake Home Assistant.

import { CARD_TYPE, type ChargerList, type ChargerSummary } from "./types";

export interface CardConfig {
  type: string;
  charger: string;
}

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

/**
 * Strict validation: only `type` (`custom:spotnav-card`) and `charger` are accepted, besides the keys
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
  if (
    Object.keys(candidate).some(
      (key) => key !== "type" && key !== "charger" && !HOME_ASSISTANT_CARD_KEYS.has(key),
    )
  ) {
    throw new Error("SpotNav card: only the type and charger options are supported");
  }
  const charger = candidate.charger;
  if (charger === undefined || charger === "") {
    return { type: `custom:${CARD_TYPE}`, charger: "" };
  }
  if (typeof charger !== "string" || charger.trim() === "" || charger.trim() !== charger) {
    throw new Error(
      "SpotNav card: charger must be a config entry id, with no surrounding whitespace",
    );
  }
  return { type: `custom:${CARD_TYPE}`, charger };
}

export function editorConfig(chargerId: string): CardConfig {
  return { type: `custom:${CARD_TYPE}`, charger: chargerId };
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
