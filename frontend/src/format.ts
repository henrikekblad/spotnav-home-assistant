// Numbers, money, clock times and periods via `Intl`. Prices use the market's own
// `minor_unit`, never one inferred from the currency code. Every instant is rendered in the
// market's zone with no browser-timezone fallback: an empty `FormatContext.timeZone` means the
// caller must show "unavailable".

import { localDayKey, zoneClock } from "./chart";
import type { Language } from "./i18n";

export interface FormatContext {
  language: Language;
  timeZone: string;
  unit: string;
  currency: string | null;
  majorUnit: string | null;
}

export function hasZone(context: FormatContext): boolean {
  return context.timeZone !== "";
}

const numberFormats = new Map<string, Intl.NumberFormat>();
const dateFormats = new Map<string, Intl.DateTimeFormat>();

function numberFormat(language: Language, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${language}|${JSON.stringify(options)}`;
  const existing = numberFormats.get(key);
  if (existing !== undefined) {
    return existing;
  }
  const created = new Intl.NumberFormat(language, options);
  numberFormats.set(key, created);
  return created;
}

function dateFormat(language: Language | "en-GB", options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${language}|${JSON.stringify(options)}`;
  const existing = dateFormats.get(key);
  if (existing !== undefined) {
    return existing;
  }
  const created = new Intl.DateTimeFormat(language, options);
  dateFormats.set(key, created);
  return created;
}

export function formatFixed(language: Language, value: number, digits = 1): string {
  return numberFormat(language, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatNumber(language: Language, value: number, digits = 1): string {
  return numberFormat(language, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  }).format(value);
}

export function pricePerKwh(context: FormatContext, value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "";
  }
  return `${formatNumber(context.language, value)} ${context.unit}/kWh`;
}

export function money(context: FormatContext, value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "";
  }
  const amount = formatNumber(context.language, value, 2);
  return context.majorUnit === null ? amount : `${amount} ${context.majorUnit}`;
}

/**
 * One stored energy amount in kWh, rounded to at most three decimals (past what any control
 * can set, but enough to hide binary artefacts like `41.60000000000001`); `Intl` trims zeros.
 * The unit is written here because kWh is physical, unlike a price's market-defined minor unit.
 */
export function energyAmount(language: Language, value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "";
  }
  return `${formatNumber(language, value, 3)} kWh`;
}

export function percentAmount(language: Language, value: number): string {
  return `${formatNumber(language, value, 1)} %`;
}

export function clock(context: FormatContext, instantMs: number): string {
  if (!hasZone(context) || !Number.isFinite(instantMs)) {
    return "";
  }
  return dateFormat(context.language, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: context.timeZone,
  }).format(new Date(instantMs));
}

export function weekdayDate(context: FormatContext, instantMs: number): string {
  if (!hasZone(context) || !Number.isFinite(instantMs)) {
    return "";
  }
  return dateFormat(context.language, {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: context.timeZone,
  }).format(new Date(instantMs));
}

function wallReading(context: FormatContext, instantMs: number): string {
  return dateFormat("en", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: context.timeZone,
  }).format(new Date(instantMs));
}

export function offsetLabel(context: FormatContext, instantMs: number): string {
  if (!hasZone(context)) {
    return "";
  }
  return zoneClock(context.timeZone).offsetLabel(instantMs);
}

/**
 * Whether an instant's local wall time occurs twice in the market's zone: true when any
 * instant within an hour and a half reads the same wall clock (endpoint-local, not day-level).
 */
export function wallTimeRepeats(context: FormatContext, instantMs: number): boolean {
  if (!hasZone(context) || !Number.isFinite(instantMs)) {
    return false;
  }
  const target = wallReading(context, instantMs);
  for (let minutes = 1; minutes <= 90; minutes += 1) {
    if (wallReading(context, instantMs - minutes * 60_000) === target) {
      return true;
    }
    if (wallReading(context, instantMs + minutes * 60_000) === target) {
      return true;
    }
  }
  return false;
}

export function periodIsAmbiguous(context: FormatContext, startMs: number, endMs: number): boolean {
  return wallTimeRepeats(context, startMs) || wallTimeRepeats(context, endMs);
}

/**
 * One period, e.g. "Mon 21:00-23:15", with the end's day across local midnight and a UTC
 * offset on an endpoint only when its wall time would be ambiguous.
 */
export function periodLabel(context: FormatContext, startMs: number, endMs: number): string {
  if (!hasZone(context)) {
    return "";
  }
  const day = weekdayDate(context, startMs);
  const from = clock(context, startMs);
  const to = clock(context, endMs);
  const crosses = localDayKey(startMs, context.timeZone) !== localDayKey(endMs, context.timeZone);
  const end = crosses ? `${weekdayDate(context, endMs)} ${to}` : to;
  const ambiguousStart = wallTimeRepeats(context, startMs);
  const ambiguousEnd = wallTimeRepeats(context, endMs);
  const startSuffix = ambiguousStart ? ` (${offsetLabel(context, startMs)})` : "";
  const endSuffix = ambiguousEnd ? ` (${offsetLabel(context, endMs)})` : "";
  return `${day} ${from}${startSuffix}-${end}${endSuffix}`;
}

/**
 * A distance given in Scandinavian miles (1 mil = 10 km), written in the unit the language uses:
 * mil for Swedish and Norwegian, km otherwise (as the Android app does).
 */
export function distanceText(language: Language, mil: number): string {
  if (language === "sv" || language === "nb") {
    return `${formatNumber(language, mil, 1)} mil`;
  }
  return `${formatNumber(language, mil * 10, 0)} km`;
}

/** The ISO weekday (1 = Monday) as a reference date: 2024-01-01 was a Monday. */
function referenceWeekday(isoWeekday: number): number {
  return Date.UTC(2024, 0, isoWeekday, 12);
}

/**
 * A calendar date `YYYY-MM-DD` as people read it in the card language, e.g. `Sun 4 Oct`: the same short
 * weekday-day-month shape a period uses. The date is zone-free, so it is formatted in UTC from noon.
 */
export function dateLabel(language: Language, isoDate: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (match === null) {
    return "";
  }
  const instant = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12);
  // English is read day-first (`Sun 4 Oct`), like the other four languages, not `Sun, Oct 4`.
  return dateFormat(language === "en" ? "en-GB" : language, {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(new Date(instant));
}

/**
 * A departure date named for the reader: today and tomorrow in words (the caller's, already translated),
 * a later day by its weekday and date. `null` for a day that has gone by (planning ignores it) or text
 * that is not a date. With no known `today` the date is just spelled.
 */
export function departureDayLabel(
  language: Language,
  isoDate: string,
  today: string | null,
  words: { today: string; tomorrow: string },
): string | null {
  const label = dateLabel(language, isoDate);
  if (label === "") {
    return null;
  }
  if (today === null) {
    return label;
  }
  if (isoDate < today) {
    return null;
  }
  if (isoDate === today) {
    return words.today;
  }
  const next = new Date(`${today}T12:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return isoDate === next.toISOString().slice(0, 10) ? words.tomorrow : label;
}

/**
 * A weekday in the plural the language uses for "every Sunday" (`Sundays`, `söndagar`, `søndager`,
 * `søndage`, `sunnuntaisin`), from the weekday name `Intl` gives. `null` for a number that is no ISO weekday.
 */
export function weekdayPlural(language: Language, isoWeekday: number): string | null {
  if (!Number.isInteger(isoWeekday) || isoWeekday < 1 || isoWeekday > 7) {
    return null;
  }
  const name = dateFormat(language, { weekday: "long", timeZone: "UTC" })
    .format(new Date(referenceWeekday(isoWeekday)))
    .toLocaleLowerCase(language);
  switch (language) {
    case "sv":
      return `${name}ar`;
    case "nb":
      return `${name}er`;
    case "da":
      return `${name}e`;
    case "fi":
      return name.endsWith("i") ? `${name}sin` : `${name}isin`;
    default:
      return `${name.charAt(0).toLocaleUpperCase(language)}${name.slice(1)}s`;
  }
}
