// Status line and issue list, worded from the backend's status block. The backend decides which
// lines appear, in what order and at what tone; this module only words each stable code and
// formats its typed facts. `tone` alone decides banner colour (`blocking` red, `notice` neutral).

import { localDayKey } from "./chart";
import {
  clock,
  distanceText,
  formatNumber,
  hasZone,
  localeMoney,
  weekdayDate,
  weekdayPlural,
  type FormatContext,
} from "./format";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { STATUS_CODE_TABLE, type StatusCode, type StatusLine, type StatusParam, type StatusTone, type Status } from "./validate";

export interface Issue {
  code: string;
  severity: Exclude<StatusTone, "normal">;
  textKey: TranslationKey;
  params: Record<string, string>;
  technical: string | null;
  /** A sentence composed from the line's facts, shown instead of `textKey` when present. */
  text?: string;
}

/** What an issue says, in the reader's language. */
export function issueText(language: Language, issue: Issue): string {
  return issue.text ?? translate(language, issue.textKey, issue.params);
}

/** The facts behind a site measurement the card cannot use: which phases, read from which entities. */
export interface MeasurementFacts {
  noValuePhases: readonly string[];
  noValueEntities: readonly string[];
  stalePhases: readonly string[];
  maxAgeS: number | null;
}

function listOf(language: Language, items: readonly string[]): string {
  return new Intl.ListFormat(language, { style: "long", type: "conjunction" }).format(items);
}

/**
 * The sentence for a measurement that cannot be used: the phases with no value (and the entities they
 * are read from) and the phases older than the maximum age. A fact without a phase says it generally.
 */
export function measurementProblemText(language: Language, facts: MeasurementFacts): string {
  const parts: string[] = [];
  if (facts.noValuePhases.length > 0) {
    const where = facts.noValueEntities.length > 0 ? ` (${facts.noValueEntities.join(", ")})` : "";
    const key = `status.siteMeasurement.noValue.${pluralForm(language, facts.noValuePhases.length)}` as TranslationKey;
    parts.push(translate(language, key, { phases: listOf(language, facts.noValuePhases), where }));
  }
  if (facts.stalePhases.length > 0) {
    const key = `status.siteMeasurement.stale.${pluralForm(language, facts.stalePhases.length)}` as TranslationKey;
    parts.push(
      translate(language, key, {
        phases: listOf(language, facts.stalePhases),
        seconds: formatNumber(language, facts.maxAgeS ?? 0, 0),
      }),
    );
  }
  return parts.length === 0 ? translate(language, "issue.siteMeasurement") : parts.join(" ");
}

function strings(value: StatusParam | undefined): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

/** The measurement sentence of a `site_measurement_problem` line. */
function measurementLineText(language: Language, p: StatusLine["params"]): string {
  return measurementProblemText(language, {
    noValuePhases: strings(p["no_value_phases"]),
    noValueEntities: strings(p["no_value_entities"]),
    stalePhases: strings(p["stale_phases"]),
    maxAgeS: num(p["max_age_s"]),
  });
}

/** The meter's sensors unavailable together: an inverter's (it may be in standby), or every phase at once. */
export function meterUnavailableText(language: Language, entities: readonly string[], inverter: boolean): string {
  const line: StatusLine = {
    code: "site_meter_unavailable",
    params: { entities: [...entities], cause: inverter ? "inverter_standby" : "meter_unavailable" },
  };
  const wording = basisWording(line);
  return wording === null ? "" : translate(language, wording.key, wording.params);
}

/** The entity a line names, or `null`. */
function entityOf(p: StatusLine["params"]): string | null {
  return typeof p["entity"] === "string" && p["entity"] !== "" ? p["entity"] : null;
}

/** The wording of the lines that say why solar has no full basis, and of the meter's unavailable sensors. */
function basisWording(line: StatusLine): { key: TranslationKey; params: Record<string, string> } | null {
  const p = line.params;
  const entity = entityOf(p);
  switch (line.code) {
    case "solar_no_grid_power":
      return entity === null
        ? { key: "strategy.status.solar.noGridPower", params: {} }
        : { key: "strategy.status.solar.noGridPowerEntity", params: { entity } };
    case "solar_battery_unreadable":
      return { key: "strategy.status.solar.batteryUnreadable", params: { entity: entity ?? "" } };
    case "solar_charger_current_missing":
      return entity === null
        ? { key: "strategy.status.solar.chargerCurrentNotSet", params: {} }
        : { key: "strategy.status.solar.chargerCurrentUnreadable", params: { entity } };
    case "solar_site_incomplete":
      return { key: "strategy.status.solar.siteIncomplete", params: { phases: strings(p["phases"]).join(", ") } };
    case "site_meter_unavailable":
      return {
        key: p["cause"] === "inverter_standby" ? "status.meterUnavailable.inverter" : "status.meterUnavailable.meter",
        params: { entities: strings(p["entities"]).join(", ") },
      };
    default:
      return null;
  }
}

export const STATUS_WORDING: Readonly<Record<StatusCode, TranslationKey>> = {
  starting_up: "status.startingUp",
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
  topping_off: "status.toppingOff",
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
  solar_no_grid_power: "strategy.status.solar.noGridPower",
  solar_battery_unreadable: "strategy.status.solar.batteryUnreadable",
  solar_charger_current_missing: "strategy.status.solar.chargerCurrentNotSet",
  solar_site_incomplete: "strategy.status.solar.siteIncomplete",
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
  charger_disabled: "issue.chargerDisabled",
  held_until_window: "status.heldUntilWindow",
  hold_overridden: "issue.holdOverridden",
  stopped_by_person: "status.stoppedByPerson",
  need_limited_by_room: "status.needLimitedByRoom",
  charging_to_vehicle_limit: "status.chargingToVehicleLimit",
  remaining_need_estimated: "issue.needKept",
  site_measurement_problem: "issue.siteMeasurement",
  site_meter_unavailable: "status.meterUnavailable.meter",
  duplicate_charger: "issue.duplicateCharger",
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
  "status.proposalPendingAt",
  "status.chargingNowOpen",
  "status.scheduledNoTime",
  "status.waitingForPublicationNoTime",
  "status.waitingForHistoryNoDetail",
  "status.loadBalancingLimited",
  "strategy.status.solar.chargingUnknown",
  "strategy.status.hybrid.creditSuffix",
  "issue.needFromSessions",
  "strategy.status.solar.noGridPowerEntity",
  "strategy.status.solar.chargerCurrentUnreadable",
  "status.meterUnavailable.inverter",
];

const MISSING_FIELD_KEYS: Readonly<Record<string, TranslationKey>> = {
  area: "status.missing.area",
  phases: "status.missing.phases",
  amps: "status.missing.amps",
  vehicle: "status.missing.vehicle",
  target_percent: "status.missing.target_percent",
};

/** A manual need counted without the energy register: its kept remainder's wording, or the sessions'. */
function needEstimated(language: Language, p: StatusLine["params"]): { key: TranslationKey; params: Record<string, string> } {
  return {
    key: p["basis"] === "sessions" ? "issue.needFromSessions" : "issue.needKept",
    params: { kwh: formatNumber(language, num(p["kwh"]) ?? 0, 1) },
  };
}

/** The wording for why a charger is unavailable when its charge control is gone or disabled. */
function chargerProblemKey(problem: StatusParam | undefined): TranslationKey | null {
  if (problem === "control_missing") {
    return "issue.chargeControlMissing";
  }
  return problem === "control_disabled" ? "issue.chargeControlDisabled" : null;
}

/** The wording for a load-balancing limit: the cause when the server names one, else the plain limit. */
function loadBalancingLimitKey(cause: StatusParam | undefined): TranslationKey {
  if (cause === "battery_shares_fuse") {
    return "status.loadBalancingLimitedByBattery";
  }
  if (cause === "house_consumption") {
    return "status.loadBalancingLimitedByHouse";
  }
  return "status.loadBalancingLimitedTo";
}

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
  const basis = basisWording(line);
  if (basis !== null) {
    return say(basis.key, basis.params);
  }
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
    case "topping_off": {
      // Past the plan's last window, the car finishes a charge to its own limit: until it stops by itself.
      const until = ms(p["until"]);
      return until === null || !zoned
        ? say("status.toppingOffOpen")
        : say("status.toppingOff", { time: clock(format, until) });
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
    case "proposal_pending": {
      const at = ms(p["installs_at"]);
      return at === null || !zoned
        ? say("status.proposalPending")
        : say("status.proposalPendingAt", { time: moment(format, at, nowMs) });
    }
    case "held_until_window": {
      const time = ms(p["time"]);
      return time === null || !zoned
        ? say("status.scheduledNoTime")
        : say("status.heldUntilWindow", { time: clock(format, time) });
    }
    case "plan_energy":
      return say("status.planEnergy", { kwh: formatNumber(language, num(p["kwh"]) ?? 0, 1) });
    case "plan_cost": {
      const major = (num(p["amount_minor"]) ?? 0) / 100;
      const currency = typeof p["currency"] === "string" ? p["currency"] : "";
      if (currency === "GBP") {
        return say("status.planCost", { cost: localeMoney(language, major, currency, format.majorUnit) });
      }
      const unit = currency === format.currency && format.majorUnit !== null ? format.majorUnit : currency;
      return say("status.planCost", { cost: `${formatNumber(language, major, 2)} ${unit}`.trim() });
    }
    case "plan_distance":
      return say("status.planDistance", { distance: distanceText(language, num(p["mil"]) ?? 0, format.countries) });
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
        : say(loadBalancingLimitKey(p["cause"]), { limit: formatNumber(language, limit, 0) });
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
    case "site_measurement_problem":
      return measurementLineText(language, p);
    case "remaining_need_estimated": {
      const need = needEstimated(language, p);
      return say(need.key, need.params);
    }
    case "need_limited_by_room":
      return say("status.needLimitedByRoom", { kwh: formatNumber(language, num(p["kwh"]) ?? 0, 1) });
    case "charging_to_vehicle_limit":
      return say("status.chargingToVehicleLimit", { percent: formatNumber(language, num(p["percent"]) ?? 100, 0) });
    case "duplicate_charger":
      return say("issue.duplicateCharger", { other: typeof p["other"] === "string" ? p["other"] : "" });
    case "charger_unavailable": {
      const key = chargerProblemKey(p["problem"]);
      return key === null ? say("issue.chargerMissing") : say(key, { entity: typeof p["entity"] === "string" ? p["entity"] : "" });
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
 * A normal line is never an item to review, whatever the block's tone says.
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
    if (line.code === "site_measurement_problem") {
      issues.push({
        code: line.code,
        severity,
        textKey: STATUS_WORDING[line.code],
        params: {},
        technical: null,
        text: measurementLineText(language, line.params),
      });
      continue;
    }
    const basis = basisWording(line);
    if (basis !== null) {
      issues.push({ code: line.code, severity, textKey: basis.key, params: basis.params, technical: null });
      continue;
    }
    if (line.code === "remaining_need_estimated") {
      const need = needEstimated(language, line.params);
      issues.push({ code: line.code, severity, textKey: need.key, params: need.params, technical: null });
      continue;
    }
    if (line.code === "duplicate_charger") {
      const other = typeof line.params["other"] === "string" ? line.params["other"] : "";
      issues.push({ code: line.code, severity, textKey: STATUS_WORDING[line.code], params: { other }, technical: null });
      continue;
    }
    if (line.code === "charger_unavailable") {
      const key = chargerProblemKey(line.params["problem"]);
      const entity = typeof line.params["entity"] === "string" ? line.params["entity"] : "";
      issues.push({ code: line.code, severity, textKey: key ?? STATUS_WORDING[line.code], params: key === null ? {} : { entity }, technical: null });
      continue;
    }
    const limit = line.code === "load_balancing_limited" ? num(line.params["limit_a"]) : null;
    issues.push({
      code: line.code,
      severity,
      textKey:
        line.code === "load_balancing_limited"
          ? limit === null
            ? "status.loadBalancingLimited"
            : loadBalancingLimitKey(line.params["cause"])
          : STATUS_WORDING[line.code],
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
  return issues;
}
