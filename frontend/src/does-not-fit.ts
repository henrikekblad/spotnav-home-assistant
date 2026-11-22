// A planned car charge that does not fit under the ceiling that acts now by its departure (plan
// `plans/v3_flexible_loads_design.md`, step 3a; the backend's `site/does_not_fit.py`): one line and the person's
// choices, in the charger card's status area and in the house card's Plan tab, from the same block.
//
// The choices map to what already exists: "Take what fits" is the plan's `if_not_ready` `hold` (a lasting setting),
// "Later departure" opens the departure's editor, "Allow the peak" sets the import target aside for this charge alone
// (`spotnav/release_import_target`, until the departure or the unplug). After a choice the line says what was chosen.

import { exactKeys, isRecord, list, Malformed, numberOrNull, record, text, textOrNull } from "./decode-primitives";
import { clock, formatFixed, kwText, kwhText, moneyMinorAsMajor, type FormatContext, type MoneyUnits } from "./format";
import { translate, type TranslationKey } from "./i18n";

export type DoesNotFitChoiceCode = "takes_what_fits" | "later" | "accept_peak";
const CHOICE_CODES: readonly DoesNotFitChoiceCode[] = ["takes_what_fits", "later", "accept_peak"];
export type DoesNotFitCeiling = "import_target" | "power_level";
const CEILINGS: readonly DoesNotFitCeiling[] = ["import_target", "power_level"];

const BLOCK_KEYS = ["departure", "short_kwh", "deliverable_kwh", "ceiling", "ceiling_kw", "chosen", "choices"] as const;
const CHOICE_KEYS = ["code", "kwh", "peak_kw", "cost"] as const;

export interface DoesNotFitChoice {
  code: DoesNotFitChoiceCode;
  kwh: number | null;
  peak_kw: number | null;
  /** What the peak adds to the month's demand charge, in the minor unit; `null` without one. */
  cost: number | null;
}

export interface DoesNotFit {
  departureMs: number | null;
  short_kwh: number | null;
  deliverable_kwh: number | null;
  ceiling: DoesNotFitCeiling;
  ceiling_kw: number;
  chosen: "takes_what_fits" | "accept_peak" | null;
  choices: DoesNotFitChoice[];
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T {
  const found = text(value);
  if (!(allowed as readonly string[]).includes(found)) {
    throw new Malformed();
  }
  return found as T;
}

/** The block, checked; throws `Malformed` on anything that does not fit. */
export function decodeDoesNotFit(raw: unknown): DoesNotFit {
  const source = record(raw);
  exactKeys(source, BLOCK_KEYS);
  const departure = textOrNull(source.departure);
  const departureMs = departure === null ? null : Date.parse(departure);
  if (departureMs !== null && !Number.isFinite(departureMs)) {
    throw new Malformed();
  }
  const ceilingKw = numberOrNull(source.ceiling_kw);
  if (ceilingKw === null) {
    throw new Malformed();
  }
  return {
    departureMs,
    short_kwh: numberOrNull(source.short_kwh),
    deliverable_kwh: numberOrNull(source.deliverable_kwh),
    ceiling: oneOf(source.ceiling, CEILINGS),
    ceiling_kw: ceilingKw,
    chosen: source.chosen === null ? null : oneOf(source.chosen, ["takes_what_fits", "accept_peak"] as const),
    choices: list(source.choices).map((entry) => {
      const item = record(entry);
      exactKeys(item, CHOICE_KEYS);
      return {
        code: oneOf(item.code, CHOICE_CODES),
        kwh: numberOrNull(item.kwh),
        peak_kw: numberOrNull(item.peak_kw),
        cost: numberOrNull(item.cost),
      };
    }),
  };
}

/** The dashboard's optional block, or `null` when it is missing or unreadable (the card then shows no line). */
export function doesNotFitOrNull(value: unknown): DoesNotFit | null {
  if (!isRecord(value)) {
    return null;
  }
  try {
    return decodeDoesNotFit(value);
  } catch {
    return null;
  }
}

/** The ceiling in words ("Effektmål 6,0 kW"), as the Plan tab labels it. */
function ceilingText(format: FormatContext, block: DoesNotFit): string {
  return translate(format.language, `house.plan.ceiling.${block.ceiling}` as TranslationKey, {
    kw: kwText(format.language, block.ceiling_kw),
  });
}

/**
 * The line: what does not fit and what still fits ("Ryms inte under taket (Effektmål 6,0 kW): 18 kWh till 07:00 –
 * 12 kWh hinner."), or what was chosen ("Bilen tar det som ryms till 07:00 (12 kWh).").
 */
export function doesNotFitText(format: FormatContext, block: DoesNotFit): string {
  const language = format.language;
  const time = block.departureMs === null ? null : clock(format, block.departureMs);
  if (block.chosen === "accept_peak") {
    const peak = block.choices.find((choice) => choice.code === "accept_peak")?.peak_kw ?? null;
    const base = translate(language, time === null ? "doesNotFit.chosen.acceptPeakUnplug" : "doesNotFit.chosen.acceptPeak", {
      ceiling: ceilingText(format, block),
      ...(time === null ? {} : { time }),
    });
    return peak === null ? base : `${base} ${translate(language, "doesNotFit.peakNote", { kw: formatFixed(language, peak, 0) })}`;
  }
  const fits = block.deliverable_kwh;
  if (block.chosen === "takes_what_fits") {
    if (time === null || fits === null) {
      return translate(language, "doesNotFit.chosen.takesWhatFitsNoTime");
    }
    return translate(language, "doesNotFit.chosen.takesWhatFits", { time, kwh: kwhText(language, fits) });
  }
  const short = block.short_kwh ?? 0;
  if (time === null) {
    return translate(language, "doesNotFit.shortNoTime", { ceiling: ceilingText(format, block), kwh: kwhText(language, short) });
  }
  if (fits === null) {
    return translate(language, "doesNotFit.short", { ceiling: ceilingText(format, block), kwh: kwhText(language, short), time });
  }
  return translate(language, "doesNotFit.line", {
    ceiling: ceilingText(format, block),
    need: kwhText(language, fits + short),
    time,
    fits: kwhText(language, fits),
  });
}

/** A choice's button label ("Ta det som ryms (≈ 12 kWh)", "Tillåt toppen (≈ 12 kW, ≈ 105 kr)"). */
export function choiceLabel(format: FormatContext, choice: DoesNotFitChoice, money: MoneyUnits | null): string {
  const language = format.language;
  if (choice.code === "takes_what_fits") {
    return choice.kwh === null
      ? translate(language, "doesNotFit.choice.takesWhatFits")
      : translate(language, "doesNotFit.choice.takesWhatFitsKwh", { kwh: kwhText(language, choice.kwh) });
  }
  if (choice.code === "later") {
    return translate(language, "doesNotFit.choice.later");
  }
  if (choice.peak_kw === null) {
    return translate(language, "doesNotFit.choice.acceptPeak");
  }
  const kw = formatFixed(language, choice.peak_kw, 0);
  return choice.cost === null
    ? translate(language, "doesNotFit.choice.acceptPeakKw", { kw })
    : translate(language, "doesNotFit.choice.acceptPeakCost", { kw, cost: moneyMinorAsMajor(language, choice.cost, money) });
}
