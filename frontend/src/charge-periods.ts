// Charge periods, a charger setting: automatic (`max_periods` null: the planner weighs a start cost per period and
// keeps each one at least half an hour) or a hard cap of 1 to 8. The words for the value row and its editor's
// options, and the settings replacement a choice sends. The card owns the row and the request.

import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { encodeBody, PERIODS_MAX, PERIODS_MIN, type ReplacementCheck } from "./settings";
import type { SettingsRecord } from "./types";
import type { ChoiceOption } from "./value-editors";

/** The option that stands for automatic periods. */
export const PERIODS_AUTO = "auto";

/** "Automatic", or "3 periods" in the reader's own plural rule. */
export function chargePeriodsLabel(language: Language, count: number | null): string {
  if (count === null) {
    return translate(language, "settings.periods.auto");
  }
  return translate(language, `settings.periods.value.${pluralForm(language, count)}` as TranslationKey, {
    count: String(count),
  });
}

/** Automatic first (with what it does under it), then 1 to 8. */
export function chargePeriodsOptions(language: Language): ChoiceOption[] {
  const options: ChoiceOption[] = [
    {
      value: PERIODS_AUTO,
      label: chargePeriodsLabel(language, null),
      help: translate(language, "settings.periods.autoHelp"),
    },
  ];
  for (let count = PERIODS_MIN; count <= PERIODS_MAX; count += 1) {
    options.push({ value: String(count), label: chargePeriodsLabel(language, count) });
  }
  return options;
}

/** The option a record stands at. */
export function chargePeriodsValue(record: SettingsRecord): string {
  return record.max_periods === null ? PERIODS_AUTO : String(record.max_periods);
}

/** What choosing `option` sends: the accepted record unchanged except `max_periods`. */
export function chargePeriodsReplacement(record: SettingsRecord, option: string): ReplacementCheck {
  let count: number | null = null;
  if (option !== PERIODS_AUTO) {
    count = Number(option);
    if (!Number.isInteger(count) || count < PERIODS_MIN || count > PERIODS_MAX) {
      return { ok: false, errorKey: "settings.error.invalid" };
    }
  }
  return {
    ok: true,
    body: { ...encodeBody(record), max_periods: count },
    changed: count !== record.max_periods,
  };
}
