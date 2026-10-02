// Status line and issue list, worded from the backend's status block. The backend decides which
// lines appear, in what order and at what tone; this module only words each stable code and
// formats its typed facts. `tone` alone decides banner colour (`blocking` red, `notice` neutral).

import { localDayKey } from "./chart";
import { clock, distanceText, formatNumber, hasZone, weekdayDate, weekdayPlural, type FormatContext } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import { STATUS_CODE_TABLE, type StatusCode, type StatusLine, type StatusParam, type StatusTone, type Status } from "./validate";

export interface Issue {
  code: string;
  severity: Exclude<StatusTone, "normal">;
  textKey: TranslationKey;
  params: Record<string, string>;
  technical: string | null;
}

export const STATUS_WORDING: Readonly<Record<StatusCode, TranslationKey>> = {
  charger_unavailable: "issue.chargerMissing",
  price_data_invalid: "issue.priceInvalid",
  price_data_unavailable: "issue.priceUnavailable",
  settings_incomplete: "issue.incompleteSettings",
  solar_unavailable: "issue.solarUnavailable",
  target_soc_unknown: "issue.targetSocUnknown",
  price_horizon_missing: "issue.priceHorizon",
  planning_unavailable: "issue.planningUnavailable",
  planning_error: "issue.error",
  paused: "control.pausedUntil",
  charging_now: "status.chargingNow",
  charging_without_prices: "issue.chargingWithoutPrices",
  waiting_for_publication: "status.waitingForPublication",
  waiting_for_history: "status.waitingForHistory",
  buying_before_publication: "status.buyingBeforePublication",
  auto_planned: "status.autoPlanned",
  auto_installed: "status.autoInstalled",
  proposal_pending: "status.proposalPending",
  waiting_for_tomorrow: "status.waitingForTomorrow",
  no_plan: "status.noPlan",
  nothing_to_charge: "status.nothingToCharge",
  plan_energy: "status.planEnergy",
  plan_cost: "status.planCost",
  plan_distance: "status.planDistance",
  solar_charging: "strategy.status.solar.charging",
  solar_arming: "strategy.status.solar.arming",
  solar_disarming: "strategy.status.solar.disarming",
  solar_no_reading_stopped: "strategy.status.solar.noReadingStopped",
  solar_no_reading_waiting: "strategy.status.solar.noReadingWaiting",
  solar_waiting_for_sun: "strategy.status.solar.waitingForSun",
  solar_unknown: "strategy.status.solar.unknown",
  hybrid_grid: "strategy.status.hybrid.grid",
  hybrid_no_forecast: "strategy.status.hybrid.noForecast",
  hybrid_no_price_data: "strategy.status.hybrid.noPriceData",
  hybrid_satisfied: "strategy.status.hybrid.satisfied",
  hybrid_unknown: "strategy.status.hybrid.unknown",
  settings_suggested: "status.settingsSuggested",
  target_reached: "status.targetStopped",
  target_unverifiable: "issue.targetUnverifiable",
  price_data_stale: "issue.priceStale",
  price_data_degraded: "issue.priceDegraded",
  unpriced: "issue.unpriced",
  load_balancing_limited: "status.loadBalancingLimitedTo",
  load_balancing_unavailable: "issue.loadBalancing",
  held_by_charger: "issue.heldByCharger",
};

export const STATUS_VARIANT_KEYS: readonly TranslationKey[] = [
  "status.finishSetupArea",
  "status.finishSetup",
  "status.missing.area",
  "status.missing.phases",
  "status.missing.amps",
  "status.missing.vehicle",
  "status.missing.target_percent",
  "control.pausedIndefinitely",
  "status.pausedShort",
  "status.targetStoppedAge",
  "status.targetStoppedNow",
  "status.targetStoppedEstimate",
  "status.targetStoppedEstimateAge",
  "status.targetAgeMinutes",
  "status.targetAgeHours",
  "status.chargingNowOpen",
  "status.scheduledNoTime",
  "status.waitingForPublicationNoTime",
  "status.waitingForHistoryNoDetail",
  "status.loadBalancingLimited",
  "strategy.status.solar.chargingUnknown",
  "strategy.status.hybrid.creditSuffix",
];

const MISSING_FIELD_KEYS: Readonly<Record<string, TranslationKey>> = {
  area: "status.missing.area",
  phases: "status.missing.phases",
  amps: "status.missing.amps",
  vehicle: "status.missing.vehicle",
  target_percent: "status.missing.target_percent",
};

function ms(value: StatusParam | undefined): number | null {
  if (typeof value !== "string") {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function num(value: StatusParam | undefined): number | null {
  return typeof value === "number" ? value : null;
}

function moment(format: FormatContext, instantMs: number, nowMs: number): string {
  const time = clock(format, instantMs);
  if (localDayKey(instantMs, format.timeZone) === localDayKey(nowMs, format.timeZone)) {
    return time;
  }
  return `${weekdayDate(format, instantMs)} ${time}`;
}

export function lineText(line: StatusLine, format: FormatContext, nowMs: number): string {
  const language = format.language;
  const say = (key: TranslationKey, params: Record<string, string> = {}): string =>
    translate(language, key, params);
  const zoned = hasZone(format);
  const p = line.params;
  switch (line.code) {
    case "paused": {
      const until = ms(p["until"]);
      if (until === null) {
        return say("control.pausedIndefinitely");
      }
      return zoned ? say("control.pausedUntil", { time: moment(format, until, nowMs) }) : say("status.pausedShort");
    }
    case "charging_now": {
      const until = ms(p["until"]);
      return until === null || !zoned
        ? say("status.chargingNowOpen")
        : say("status.chargingNow", { time: clock(format, until) });
    }
    case "waiting_for_publication": {
      const at = ms(p["publication_at"]);
      return at === null || !zoned
        ? say("status.waitingForPublicationNoTime")
        : say("status.waitingForPublication", { time: clock(format, at) });
    }
    case "waiting_for_history": {
      // The weekday's plural, the saving and the weeks behind it; without all three, only the plain fact.
      const weekday = weekdayPlural(language, num(p["weekday"]) ?? Number.NaN);
      const percent = num(p["percent"]);
      const weeks = num(p["weeks"]);
      return weekday === null || percent === null || weeks === null
        ? say("status.waitingForHistoryNoDetail")
        : say("status.waitingForHistory", {
            weekday,
            percent: formatNumber(language, percent, 0),
            weeks: formatNumber(language, weeks, 0),
          });
    }
    case "buying_before_publication":
      return say("status.buyingBeforePublication", { kwh: formatNumber(language, num(p["kwh"]) ?? 0, 1) });
    case "auto_planned":
    case "auto_installed": {
      const start = ms(p["start"]);
      return start === null || !zoned
        ? say("status.scheduledNoTime")
        : say(STATUS_WORDING[line.code], { time: clock(format, start) });
    }
    case "plan_energy":
      return say("status.planEnergy", { kwh: formatNumber(language, num(p["kwh"]) ?? 0, 1) });
    case "plan_cost": {
      const major = (num(p["amount_minor"]) ?? 0) / 100;
      const currency = typeof p["currency"] === "string" ? p["currency"] : "";
      const unit = currency === format.currency && format.majorUnit !== null ? format.majorUnit : currency;
      return say("status.planCost", { cost: `${formatNumber(language, major, 2)} ${unit}`.trim() });
    }
    case "plan_distance":
      return say("status.planDistance", { distance: distanceText(language, num(p["mil"]) ?? 0) });
    case "solar_charging": {
      const amps = num(p["requested_a"]);
      return amps === null
        ? say("strategy.status.solar.chargingUnknown")
        : say("strategy.status.solar.charging", { amps: formatNumber(language, amps, 0) });
    }
    case "hybrid_grid": {
      const start = ms(p["window_start"]);
      const end = ms(p["window_end"]);
      const window = start !== null && end !== null && zoned ? ` ${clock(format, start)}–${clock(format, end)}` : "";
      const grid = say("strategy.status.hybrid.grid", {
        grid: formatNumber(language, num(p["grid_kwh"]) ?? 0, 1),
        window,
      });
      const credit = num(p["credit_kwh"]);
      return credit !== null && credit > 0
        ? `${grid}${say("strategy.status.hybrid.creditSuffix", { credit: formatNumber(language, credit, 1) })}`
        : grid;
    }
    case "load_balancing_limited": {
      const limit = num(p["limit_a"]);
      return limit === null
        ? say("status.loadBalancingLimited")
        : say("status.loadBalancingLimitedTo", { limit: formatNumber(language, limit, 0) });
    }
    case "settings_incomplete": {
      // Setup, not a fault: name what is still needed when the fields are known words.
      const missing = Array.isArray(p["missing"]) ? p["missing"] : [];
      const names = missing.map((field) => MISSING_FIELD_KEYS[field]).filter((key): key is TranslationKey => key !== undefined);
      if (names.length === 0 || names.length !== missing.length) {
        return say("issue.incompleteSettings");
      }
      return missing.length === 1 && missing[0] === "area"
        ? say("status.finishSetupArea")
        : say("status.finishSetup", { fields: names.map((key) => say(key)).join(", ") });
    }
    case "target_reached": {
      // An estimate says so; a reading under a minute old is "just now".
      const soc = formatNumber(language, num(p["soc_percent"]) ?? 0, 0);
      const estimated = p["basis"] === "estimate";
      const ageS = num(p["reading_age_s"]);
      const age =
        ageS === null || ageS < 60
          ? null
          : ageS < 3600
            ? say("status.targetAgeMinutes", { n: formatNumber(language, Math.floor(ageS / 60), 0) })
            : say("status.targetAgeHours", { n: formatNumber(language, Math.floor(ageS / 3600), 0) });
      if (age === null) {
        if (estimated) {
          return say("status.targetStoppedEstimate", { soc });
        }
        return ageS === null ? say("status.targetStopped", { soc }) : say("status.targetStoppedNow", { soc });
      }
      return say(estimated ? "status.targetStoppedEstimateAge" : "status.targetStoppedAge", { soc, age });
    }
    default:
      return say(STATUS_WORDING[line.code]);
  }
}

/** The suggestion note has its own muted line, never a part of the plan line. */
const NOTE_CODE = "settings_suggested";

export function statusText(status: Status | null, format: FormatContext, nowMs: number): string | null {
  if (status === null) {
    return null;
  }
  const lines = status.lines.filter((line) => line.code !== NOTE_CODE);
  if (lines.length === 0) {
    return null;
  }
  const parts = lines.map((line) => lineText(line, format, nowMs));
  return parts
    .map((part, index) => (index < parts.length - 1 ? part.replace(/[.。]$/u, "") : part))
    .join(" · ");
}

/** The block's own "suggested from your location" note, or `null`. */
export function statusNote(status: Status | null, format: FormatContext, nowMs: number): string | null {
  const line = status?.lines.find((entry) => entry.code === NOTE_CODE);
  return line === undefined ? null : lineText(line, format, nowMs);
}

function reasonOf(line: StatusLine): string | null {
  const reason = line.params["reason"];
  return typeof reason === "string" && reason !== "" ? reason : null;
}

/**
 * The Info dialog's list: the block's own non-normal lines, worded. Severity is the line's tone.
 * A `notice` block with no notice line is a proposal waiting for a boundary; that one item is
 * added from the tone.
 */
export function issuesOf(status: Status | null, language: Language): Issue[] {
  if (status === null) {
    return [];
  }
  const issues: Issue[] = [];
  for (const line of status.lines) {
    const severity = STATUS_CODE_TABLE[line.code][0];
    if (severity === "normal") {
      continue;
    }
    const limit = line.code === "load_balancing_limited" ? num(line.params["limit_a"]) : null;
    issues.push({
      code: line.code,
      severity,
      textKey: line.code === "load_balancing_limited" && limit === null ? "status.loadBalancingLimited" : STATUS_WORDING[line.code],
      params: limit === null ? {} : { limit: formatNumber(language, limit, 0) },
      technical: reasonOf(line),
    });
    if (line.code === "settings_incomplete") {
      const missing = line.params["missing"];
      if (Array.isArray(missing) && missing.length > 0) {
        issues.push({
          code: "missing_settings",
          severity,
          textKey: "issue.missingSettings",
          params: { fields: missing.join(", ") },
          technical: null,
        });
      }
    }
  }
  if (status.tone === "notice" && issues.length === 0) {
    issues.push({ code: "pending_proposal", severity: "notice", textKey: "issue.pendingProposal", params: {}, technical: null });
  }
  return issues;
}
