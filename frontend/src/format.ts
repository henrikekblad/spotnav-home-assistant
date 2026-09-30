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

function dateFormat(language: Language, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
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
