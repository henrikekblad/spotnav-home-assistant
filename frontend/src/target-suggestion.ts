// The suggested import target in the target's editor (`spotnav/suggest_import_target`): the decoded answer and the
// sentence that offers it. The backend reads Home Assistant's statistics and does every sum; this only words them.

import { dateLabel, formatFixed } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";

/** `spotnav/suggest_import_target`'s own version. */
export const SUGGEST_IMPORT_TARGET_API_VERSION = 1;

export interface TargetSuggestion {
  available: boolean;
  /** Why there is no suggestion (`no_recorder`, `no_grid_statistics`, `too_few_days`, `no_chargers`). */
  reason: string | null;
  suggestion_kw: number | null;
  /** The house's typical daily peak: the median of the days' highest hours. */
  typical_kw: number | null;
  highest: { kw: number; date: string } | null;
  /** What the car gets at the suggestion in a typical evening or night hour. */
  car_kw: number | null;
  days: number;
  /** A member charger's own draw is not in the statistics: the grid alone stood for the house. */
  charger_missing: boolean;
}

/** What the editor shows: the sentence, and the figure "Use" fills in (`null`: nothing to fill). */
export interface TargetAdvice {
  text: string;
  value: number | null;
}

const REASONS: Record<string, TranslationKey> = {
  no_recorder: "target.suggest.none.no_recorder",
  no_grid_statistics: "target.suggest.none.no_grid_statistics",
  too_few_days: "target.suggest.none.too_few_days",
  no_chargers: "target.suggest.none.no_chargers",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function power(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

/** The answer, or `null` when it is not one this card reads (a newer version, or not its shape). */
export function decodeSuggestion(raw: unknown): TargetSuggestion | null {
  if (!isRecord(raw) || raw["api_version"] !== SUGGEST_IMPORT_TARGET_API_VERSION || typeof raw["available"] !== "boolean") {
    return null;
  }
  const highest = raw["highest"];
  const chargers = Array.isArray(raw["chargers"]) ? raw["chargers"] : [];
  const suggestion: TargetSuggestion = {
    available: raw["available"],
    reason: typeof raw["reason"] === "string" ? raw["reason"] : null,
    suggestion_kw: power(raw["suggestion_kw"]),
    typical_kw: power(raw["typical_kw"]),
    highest:
      isRecord(highest) && power(highest["kw"]) !== null && typeof highest["date"] === "string"
        ? { kw: highest["kw"] as number, date: highest["date"] }
        : null,
    car_kw: power(raw["car_kw"]),
    days: typeof raw["days"] === "number" && Number.isInteger(raw["days"]) ? raw["days"] : 0,
    charger_missing: chargers.some((charger) => isRecord(charger) && charger["has_statistics"] === false),
  };
  if (suggestion.available && (suggestion.suggestion_kw === null || suggestion.typical_kw === null || suggestion.highest === null || suggestion.car_kw === null)) {
    return null;
  }
  return suggestion;
}

function kw(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} kW`;
}

/**
 * The sentence under the target's field: "The house alone: usually at most 2,4 kW in an hour a day, at most 3,7 kW
 * (Sat 17 Oct). Suggestion: 6,5 kW – …", with the figure to fill in; without a suggestion, why. `null` for an answer
 * this card cannot read.
 */
export function suggestionAdvice(language: Language, raw: unknown): TargetAdvice | null {
  const suggestion = decodeSuggestion(raw);
  if (suggestion === null) {
    return null;
  }
  if (!suggestion.available) {
    const key = suggestion.reason === null ? undefined : REASONS[suggestion.reason];
    return key === undefined ? null : { text: translate(language, key, { days: String(suggestion.days) }), value: null };
  }
  const value = suggestion.suggestion_kw!;
  const parts = [
    translate(language, "target.suggest.text", {
      typical: kw(language, suggestion.typical_kw!),
      highest: kw(language, suggestion.highest!.kw),
      date: dateLabel(language, suggestion.highest!.date),
      suggestion: kw(language, value),
      car: kw(language, suggestion.car_kw!),
    }),
  ];
  if (suggestion.charger_missing) {
    parts.push(translate(language, "target.suggest.chargerMissing"));
  }
  return { text: parts.join(" "), value };
}
