// The card's one pure, immutable view model: one decoded dashboard in, one model out.
//
// Rendering reads this alone (no `hass`, no DOM, no second response). Sources stay separate: proposal
// figures/periods come only from `plan.proposal`, installed periods and the active index only from
// `plan.installed`, `plan.relation` decides labelling, chart rows only from `prices.intervals`, and
// "charging" only from `live.charging`.

import {
  chartSeries,
  localDayKey,
  plannedBands,
  type ChartSeries,
  type PlanBands,
} from "./chart";
import {
  hasZone,
  money,
  periodIsAmbiguous,
  periodLabel,
  pricePerKwh,
  distanceText,
  formatNumber,
  type FormatContext,
} from "./format";
import { issuesOf, statusNote, statusText, type Issue } from "./status";
import { chargeCeiling, effectiveTarget } from "./target-need";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import {
  ACTION_PAUSE,
  ACTION_RESUME,
  ACTION_START,
  ACTION_STOP,
  CHARGE_PROGRESS_VEHICLE_NOT_REQUESTING_CURRENT,
  type ConnectionState,
  type ManualAction,
  type SettingsRecord,
} from "./types";
import type {
  Control,
  Dashboard,
  Fiscal,
  Period,
  Site,
  Soc,
  Vehicle,
} from "./validate";

export type { Issue } from "./status";
export type Severity = "blocking" | "notice";

export interface PlanPeriod {
  startMs: number;
  endMs: number;
  label: string;
  activeNow: boolean;
  ambiguous: boolean;
}

export interface PlanFigures {
  cost: string | null;
  energy: string | null;
  distance: string | null;
  power: string | null;
}

export interface CapabilityItem {
  key: "auto_price" | "current_limit" | "load_balancing" | "target_soc";
  labelKey: TranslationKey;
  state: "available" | "unavailable";
}

/**
 * How the proposal and the installed schedule stand to each other, as one explicit fact. Only
 * `plan.relation.applied` and the presence of the two sections are read; nothing is compared as text.
 *
 *   `none`                       neither section exists;
 *   `proposal_only`              a proposal and no installed schedule;
 *   `installed_only`             a schedule and no proposal (external planning, or Auto's own run);
 *   `applied_same`               both, and the proposal is what is installed: one block;
 *   `pending_beside_installed`   both, and they differ: installed first, proposal second.
 *
 * `applied === true` means the charger is doing what the proposal asks (identity or material
 * equivalence). `false` means it is not, but not that a change is queued: that is
 * `relation.pending_identity`, which Home Assistant reads into the status tone. Blocks are drawn the
 * same either way.
 */
export type PlanRelationKind =
  | "none"
  | "proposal_only"
  | "installed_only"
  | "applied_same"
  | "pending_beside_installed";

/**
 * The relation from the two sections and `plan.relation.applied`. `null` (no Auto snapshot) cannot claim
 * equality, so `false` and `null` both read as "they differ" when both sections exist.
 */
export function planRelationOf(dashboard: Dashboard): PlanRelationKind {
  const proposal = dashboard.plan.proposal;
  const installed = dashboard.plan.installed;
  const hasProposal = proposal !== null && proposal.periods.length > 0;
  const hasInstalled = installed !== null && installed.periods.length > 0;
  if (!hasProposal && !hasInstalled) {
    return "none";
  }
  if (hasProposal && !hasInstalled) {
    return "proposal_only";
  }
  if (!hasProposal && hasInstalled) {
    return "installed_only";
  }
  return dashboard.plan.relation.applied === true ? "applied_same" : "pending_beside_installed";
}

/**
 * One pause choice: `id` is the backend's choice id, sent back verbatim when taking the pause; the label
 * is the localized sentence.
 */
export interface ActionChoice {
  id: string;
  labelKey: TranslationKey;
}

/**
 * One axis of the control block: the action the backend named, and the facts around it. Nothing is
 * decided here; `action` is read verbatim and no branch consults `live.charging`, installed periods or
 * pause instants.
 */
export interface ControlAxisFacts {
  action: string;
  labelKey: TranslationKey | null;
  reason: string | null;
  reasonCode: string | null;
  pending: boolean;
}

/**
 * The two control axes and their shared facts. `immediate` is what the charger may be told to do now
 * (Start now / Stop); `automatic` is what may happen to Home Assistant's own execution (pause / resume).
 * Each is read from its own wire axis; an idle Auto charger offers a Start and a Pause.
 */
export interface ControlFacts {
  immediate: ControlAxisFacts;
  automatic: ControlAxisFacts;
  choices: ActionChoice[];
  notice: string | null;
  noticeCode: string | null;
  canAct: boolean;
}

export interface StrategyRowFacts {
  id: string;
  labelKey: TranslationKey;
  available: boolean;
  /** The sentence for why the strategy is held back, or `null`. */
  reason: string | null;
  /** The backend's stable code behind `reason`, or `null`. */
  reasonCode: string | null;
}

export interface StrategyFacts {
  selected: string | null;
  selectedId: string | null;
  rows: StrategyRowFacts[];
}

/**
 * The vehicle-side advisory as one subdued line, or `null`. `text` is localized; `code` is the backend's
 * stable code, shown as technical detail. Both exist only for the state
 * `vehicle_not_requesting_current`; pending, `unknown`, normal, or a null block give `null`, so "not yet"
 * and "cannot tell" never read as a warning.
 */
export interface AdvisoryFacts {
  text: string;
  code: string | null;
}

export interface CardModel {
  language: Language;
  format: FormatContext;
  timesAvailable: boolean;
  chargerName: string | null;
  /**
   * The dashboard's reduced settings section, for labelling the compact row only. It is a summary, nullable
   * and reduced, and must never be the base of an edit: a dialog reads the full record first.
   */
  dashboardSettings: SettingsRecord | null;
  /** Today's local date in the market's zone (`YYYY-MM-DD`), or `null` while the zone is unknown. */
  today: string | null;
  dashboardFiscal: Fiscal | null;
  status: string | null;
  /** The "suggested from your location and charger" note: its own muted line under the status. */
  statusNote: string | null;
  issues: Issue[];
  severity: Severity | null;
  chart: ChartSeries;
  bands: PlanBands;
  summary: {
    max: string | null;
    min: string | null;
    current: string | null;
    maxFigure: string | null;
    minFigure: string | null;
  };
  figures: PlanFigures;
  proposalPeriods: PlanPeriod[];
  installedPeriods: PlanPeriod[];
  control: ControlFacts;
  /**
   * The vehicle-side advisory, or `null`: one sentence from the backend's own observation, never from
   * `live.charging`, a current reading or a plan row.
   */
  advisory: AdvisoryFacts | null;

  strategy: StrategyFacts;
  site: SiteFacts | null;
  currentRange: CurrentRangeFacts;
  soc: Soc | null;
  /** The charger's connection state for the header line; `null` when the backend says nothing. */
  connection: ConnectionState | null;
  /** The start-up grace is on: the status says "Starting up…" and Start/Stop are offered disabled. */
  startingUp: boolean;
  /** The charger's priority in its site ("first", "normal", "last"); `null` without a site. */
  chargerPriority: string | null;
  vehicles: Vehicle[];
  targetVehicleId: string | null;
  planRelation: PlanRelationKind;
  capabilities: CapabilityItem[];
  contextArea: string | null;
  contextCurrency: string | null;
  /**
   * The configured area's public name and id, as the dashboard states them. Kept as two facts: the trigger
   * adds the id only when the name does not already carry it, and a charger with neither has no area yet.
   */
  contextAreaName: string | null;
  contextAreaId: string | null;
  /** Where the area's prices come from (relay contract v2), shown small and linked in the settings. */
  priceSource: { name: string; url: string } | null;
  technicalCodes: string[];
}

export function capabilitiesFor(dashboard: Dashboard): CapabilityItem[] {
  const capabilities = dashboard.charger.capabilities;
  const item = (
    key: CapabilityItem["key"],
    labelKey: TranslationKey,
    available: boolean = capabilities[key],
  ): CapabilityItem => {
    if (available) {
      return { key, labelKey, state: "available" };
    }
    return { key, labelKey, state: "unavailable" };
  };
  return [
    item("auto_price", "cap.autoPrice"),
    // "Dynamic current limit" is what SpotNav can do to this charger, not whether it has a limit
    // entity: a charger whose current SpotNav may not or cannot set is unavailable here.
    item("current_limit", "cap.currentLimit", capabilities.set_current),
    item("load_balancing", "cap.loadBalancing"),
    item("target_soc", "cap.targetSoc"),
  ];
}

function toPeriods(
  periods: readonly Period[],
  format: FormatContext,
  activeIndex: number | null,
): PlanPeriod[] {
  return periods.map((period, index) => ({
    startMs: period.startMs,
    endMs: period.endMs,
    label: periodLabel(format, period.startMs, period.endMs),
    activeNow: activeIndex !== null && index === activeIndex,
    ambiguous: periodIsAmbiguous(format, period.startMs, period.endMs),
  }));
}

function figuresFor(dashboard: Dashboard, format: FormatContext): PlanFigures {
  const proposal = dashboard.plan.proposal;
  if (proposal === null) {
    return { cost: null, energy: null, distance: null, power: null };
  }
  const plannedKwh = proposal.planned_kwh ?? proposal.requested_kwh;
  return {
    cost: proposal.cost === null ? null : money(format, proposal.cost.value),
    // A planned amount is the planner's estimate, stated to a tenth; `energyAmount` is for amounts a record
    // holds, not for this.
    energy: plannedKwh === null ? null : `${formatNumber(format.language, plannedKwh, 1)} kWh`,
    distance:
      proposal.distance_mil === null
        ? null
        : distanceText(format.language, proposal.distance_mil, format.countries),
    power: proposal.power_kw === null ? null : `${formatNumber(format.language, proposal.power_kw, 1)} kW`,
  };
}

const IMMEDIATE_LABELS: Record<string, TranslationKey> = {
  start: "action.start",
  stop: "action.stop",
};

/**
 * The automatic axis's button labels: a second map rather than a shared vocabulary, because `resume`
 * means "resume automatic charging" here and a `stop` is never a bare stop (the pause request carries a
 * typed choice).
 */
const AUTOMATIC_LABELS: Record<string, TranslationKey> = {
  pause: "action.pauseAutomatic",
  resume: "action.resume",
};

/**
 * The reason codes the backend may send, as sentences. A closed set: the decoder refuses a reason the
 * card cannot translate; the code travels beside the sentence as technical detail.
 */
export const REASON_KEYS: Record<string, TranslationKey> = {
  no_settings: "control.noSettings",
  pause_unsettled: "control.pauseUnsettled",
  pause_clear_failed: "control.pauseClearFailed",
  action_pending: "control.actionPending",
};

export const EXECUTION_ERROR_KEYS: Record<string, TranslationKey> = {
  pause_clear_failed: "control.pauseClearFailed",
  pause_stop_failed: "control.pauseStopFailed",
  action_failed: "control.startNotAcknowledged",
  reconcile_failed: "control.reconcileFailed",
};

const CHOICE_KEYS: Record<string, TranslationKey> = {
  next_period: "pause.nextPeriod",
  until_tomorrow: "pause.untilTomorrow",
  until_resumed: "pause.untilResumed",
};

const STRATEGY_KEYS: Record<string, TranslationKey> = {
  cheapest: "strategy.cheapest",
  solar: "strategy.solar",
  hybrid: "strategy.hybrid",
};

export const STRATEGY_REASON_KEYS: Record<string, TranslationKey> = {
  needs_solar_surplus_measurement: "strategy.reason.solar",
  needs_solar_and_price_control: "strategy.reason.hybrid",
  needs_total_grid_power: "strategy.reason.totalPower",
};

/**
 * The notice above the status line, built from the control block alone. A reason wins over a recorded
 * execution failure, being the backend's current explanation.
 */
function controlNotice(
  language: Language,
  reason: string | null,
  executionError: string | null,
): { text: string | null; code: string | null } {
  if (reason !== null) {
    const key = REASON_KEYS[reason];
    // The decoder guarantees a known reason; the fallback is for TypeScript callers.
    return { text: translate(language, key ?? "issue.unknown"), code: key === undefined ? reason : null };
  }
  if (executionError === null) {
    return { text: null, code: null };
  }
  const known = EXECUTION_ERROR_KEYS[executionError];
  if (known !== undefined) {
    return { text: translate(language, known), code: null };
  }
  // An execution failure with no sentence is reported as the generic sentence plus its stable code.
  return { text: translate(language, "control.executionError"), code: executionError };
}

/**
 * One axis as the row's facts: action, button label, and the sentence for having none. `none` always has
 * a reason (the decoder enforces it), so an axis renders either one sentence or a labelled button, never
 * both.
 */
function axisFactsFor(
  language: Language,
  action: string,
  reasonCode: string | null,
  labels: Record<string, TranslationKey>,
): ControlAxisFacts {
  if (action !== "none") {
    return { action, labelKey: labels[action] ?? null, reason: null, reasonCode: null, pending: false };
  }
  const key = reasonCode === null ? undefined : REASON_KEYS[reasonCode];
  return {
    action,
    labelKey: null,
    reason: translate(language, key ?? "issue.unknown"),
    // A reason with a sentence is not repeated as a code; the code is technical detail only when no sentence
    // exists.
    reasonCode: key === undefined ? reasonCode : null,
    pending: reasonCode === "action_pending",
  };
}

/**
 * The control block as the row's facts: both axes, read separately. The notice is chosen, not merged:
 * the immediate axis's reason wins when it has one, then the automatic axis's. Each axis still carries
 * its own reason.
 */
function controlFactsFor(dashboard: Dashboard, language: Language): ControlFacts {
  const control = dashboard.control;
  const reason = control.immediate_action_reason ?? control.automatic_action_reason;
  const notice = controlNotice(language, reason, control.execution_error);
  return {
    immediate: axisFactsFor(
      language,
      control.immediate_action,
      control.immediate_action_reason,
      IMMEDIATE_LABELS,
    ),
    automatic: axisFactsFor(
      language,
      control.automatic_action,
      control.automatic_action_reason,
      AUTOMATIC_LABELS,
    ),
    choices: control.pause_choices.map((id) => ({
      id,
      labelKey: CHOICE_KEYS[id] ?? "pause.untilResumed",
    })),
    notice: notice.text,
    noticeCode: notice.code,
    canAct: control.can_act,
  };
}

/**
 * The vehicle-side advisory, or `null`. Only the backend's `vehicle_not_requesting_current` state
 * produces a sentence; the card neither derives it nor keeps a second copy of the rule.
 */
function advisoryFor(dashboard: Dashboard, language: Language): AdvisoryFacts | null {
  if (dashboard.charge_progress === null) {
    return null;
  }
  const progress = dashboard.charge_progress;
  if (progress.state !== CHARGE_PROGRESS_VEHICLE_NOT_REQUESTING_CURRENT) {
    return null;
  }
  // A car that is full asks for no current, which is no fault: nothing to say beyond the normal status
  // line (which already says the need is met), so no advisory at all.
  if (carNeedsNoCharge(dashboard.soc) || needAlreadyMet(dashboard.status)) {
    return null;
  }
  const key: TranslationKey =
    progress.reason === "power_below_threshold"
      ? // A charger behind a smart plug is judged by its power; a connector status says it differently.
        "advisory.powerBelowThreshold"
      : "advisory.vehicleNotRequestingCurrent";
  return { text: translate(language, key), code: progress.reason };
}

/** Whether the planned car's state of charge is at its target or its own maximum (or the need is 0 kWh). */
function carNeedsNoCharge(soc: Dashboard["soc"]): boolean {
  if (soc === null || soc.value === null) {
    return false;
  }
  if (soc.need_kwh !== null && soc.need_kwh <= 0) {
    return true;
  }
  const ceiling = soc.vehicle_max_percent !== null ? chargeCeiling(soc.vehicle_max_percent) : null;
  const target = soc.target_percent !== null ? effectiveTarget(soc.target_percent, soc.vehicle_max_percent) : null;
  const stop = target ?? ceiling;
  return stop !== null && soc.value >= Math.min(stop, ceiling ?? stop);
}

/**
 * Whether the backend's own status says the charging need is already met (e.g. a manual kWh plan with a full
 * car), or that the car ends this charge itself at its own limit: a car that stops taking current then is full.
 */
function needAlreadyMet(status: Dashboard["status"]): boolean {
  return status.lines.some(
    (line) =>
      line.code === "hybrid_satisfied" || line.code === "charging_to_vehicle_limit" || line.code === "topping_off",
  );
}

function strategyFactsFor(dashboard: Dashboard, language: Language): StrategyFacts {
  const strategy = dashboard.strategy;
  return {
    selected:
      strategy.selected === null ? null : translate(language, STRATEGY_KEYS[strategy.selected] ?? "strategy.cheapest"),
    selectedId: strategy.selected,
    rows: strategy.rows.map((row) => ({
      id: row.strategy,
      labelKey: STRATEGY_KEYS[row.strategy] ?? "strategy.cheapest",
      available: row.available,
      reason:
        row.reason === null
          ? null
          : translate(language, STRATEGY_REASON_KEYS[row.reason] ?? "issue.unknown"),
      reasonCode: row.reason,
    })),
  };
}


export interface SiteForecastChoiceFacts {
  id: string;
  title: string;
  selected: boolean;
}

export interface SiteFacts {
  name: string;
  chargerCount: number;
  appliesToText: string;
  solarPriority: "car_first" | "battery_first";
  solarForecastChoices: SiteForecastChoiceFacts[];
  solarForecastNone: boolean;
  activeControlAvailable: boolean;
  activeControlWritable: boolean;
  activeControlEnabled: boolean;
  activeControlReason: { text: string; code: string | null } | null;
  writable: boolean;
}

/** The reason codes `active_control.reason` is known to carry. */
export const ACTIVE_CONTROL_REASON_KEYS: Record<string, TranslationKey> = {
  duplicate_membership: "site.activeControl.reason.duplicateMembership",
  no_commandable_charger: "site.activeControl.reason.noCommandableCharger",
};

export function activeControlReasonText(
  language: Language,
  reason: string | null,
): { text: string; code: string | null } | null {
  if (reason === null) {
    return null;
  }
  const known = ACTIVE_CONTROL_REASON_KEYS[reason];
  if (known !== undefined) {
    return { text: translate(language, known), code: null };
  }
  if (reason.startsWith("site_measurement_")) {
    // One family of codes (`HealthState`), one sentence: the site's power measurement is not healthy enough
    // for active control.
    return { text: translate(language, "site.activeControl.reason.measurement"), code: reason };
  }
  return { text: translate(language, "issue.unknown"), code: reason };
}

export interface CurrentRangeFacts {
  minA: number;
  maxA: number;
}

export const DEFAULT_CURRENT_RANGE: CurrentRangeFacts = { minA: 6, maxA: 32 };

export function currentRangeFor(dashboard: Dashboard): CurrentRangeFacts {
  const range = dashboard.current_range;
  return range === null ? DEFAULT_CURRENT_RANGE : { minA: range.min_a, maxA: range.max_a };
}

export function socFor(dashboard: Dashboard): Soc | null {
  return dashboard.soc;
}

export function vehiclesFor(dashboard: Dashboard): Vehicle[] {
  return dashboard.vehicles;
}

export function targetVehicleIdFor(dashboard: Dashboard): string | null {
  return dashboard.target_vehicle_id;
}

export function siteFactsFor(site: Site | null, language: Language): SiteFacts | null {
  if (site === null) {
    return null;
  }
  const count = site.charger_count;
  const key: TranslationKey = pluralForm(language, count) === "one" ? "site.applies.one" : "site.applies.other";
  const selected = new Set(site.solar_forecast.selected);
  return {
    name: site.name,
    chargerCount: count,
    appliesToText: translate(language, key, { count: String(count) }),
    solarPriority: site.solar_priority,
    solarForecastChoices: site.solar_forecast.choices.map((choice) => ({
      id: choice.id,
      title: choice.title,
      selected: selected.has(choice.id),
    })),
    solarForecastNone: site.solar_forecast.selected.length === 0,
    activeControlAvailable: site.active_control.available,
    activeControlWritable: site.active_control.writable,
    activeControlEnabled: site.active_control.enabled,
    activeControlReason: activeControlReasonText(language, site.active_control.reason),
    writable: site.writable,
  };
}


/**
 * Whether a snapshot's own control admits [action] with [choice]. A `start` or choice-less `stop` is a
 * command to the charger, judged by the immediate axis; a `stop` with a choice is Auto's pause; a
 * `resume` is the automatic resume. A programmatic call cannot ask for what the reader was never shown,
 * and neither button borrows the other axis's permission.
 */
export function admittedAction(
  control: Control,
  action: ManualAction,
  choice: string | null,
): boolean {
  if (action === ACTION_START) {
    return choice === null && control.immediate_action === ACTION_START;
  }
  if (action === ACTION_STOP) {
    if (choice === null) {
      return control.immediate_action === ACTION_STOP;
    }
    return control.automatic_action === ACTION_PAUSE && control.pause_choices.includes(choice);
  }
  return choice === null && control.automatic_action === ACTION_RESUME;
}

/**
 * The sentence key for a refused action, from its stable code. A key rather than a sentence because the
 * card has one more failure to name: a confirmation read that fails after the backend accepted the
 * action needs the reconcile wording. Rejection prose never reaches the view; an unknown code gets one
 * generic sentence. `null` is a transport failure with no code.
 */
export function actionErrorKey(code: string | null): TranslationKey {
  if (code === null) {
    return "action.error.generic";
  }
  if (code === "spotnav_action_unavailable") {
    return "action.error.unavailable";
  }
  if (code === "spotnav_action_failed") {
    return "action.error.failed";
  }
  if (code === "spotnav_action_reconcile_failed") {
    // This code means the backend saved the change and then failed to reconcile it. It must not be borrowed
    // for a failed read (`action.error.confirmationFailed`, chosen by the card).
    return "action.error.reconcileFailed";
  }
  if (code === "invalid_pause") {
    return "action.error.invalidPause";
  }
  if (code === "vehicle_not_connected") {
    return "action.error.notConnected";
  }
  if (code === "spotnav_unsupported_api_version") {
    return "action.error.version";
  }
  if ((code.startsWith("spotnav_") && code.endsWith("charger")) || code === "spotnav_charger_unloaded") {
    return "action.error.charger";
  }
  return "action.error.generic";
}

export interface BuildInput {
  dashboard: Dashboard;
  language: Language;
  nowMs: number;
}

export function buildModel(input: BuildInput): CardModel {
  const { dashboard, language } = input;
  const market = dashboard.market;
  const format: FormatContext = {
    language,
    timeZone: market?.timezone ?? "",
    unit: market?.minor_unit ?? "",
    currency: market?.currency ?? null,
    majorUnit: market?.major_unit ?? null,
    countries: market?.countries ?? [],
  };
  const rows = dashboard.prices.intervals;
  const chart = chartSeries(rows, format.timeZone, input.nowMs);
  const bands = plannedBands(rows, chart.days, format.timeZone);
  const status = dashboard.status;
  const issues = issuesOf(status, language);
  const zone = hasZone(format);
  // A fact nobody has is omitted from the plot, never shown as "unknown": this returns `null` rather than
  // `missing`'s text, and `card-view.ts` hides the whole line.
  const price = (value: number | null): string | null =>
    value === null || !zone ? null : pricePerKwh(format, value);
  const figure = (value: number | null): string | null =>
    value === null || !zone || !Number.isFinite(value) ? null : formatNumber(language, value);
  // Max/Min are for the day the now fact is in (by the chart's own role authority), never `days[0]`, which
  // is merely the earliest held date. When no accepted day is that day, both are unavailable.
  const nowDay = chart.days.find((day) => day.role === "today") ?? null;
  const installed = dashboard.plan.installed;
  const technicalCodes = issues
    .map((entry) => entry.technical)
    .filter((code): code is string => code !== null && code !== "");

  return {
    language,
    format,
    timesAvailable: zone,
    chargerName: dashboard.charger.charger_name ?? dashboard.charger.charger_id,
    dashboardSettings: dashboard.settings,
    today: zone ? localDayKey(input.nowMs, format.timeZone) : null,
    dashboardFiscal: dashboard.fiscal,
    status: statusText(status, format, input.nowMs),
    statusNote: statusNote(status, format, input.nowMs),
    issues,
    severity: status === null || status.tone === "normal" || issues.length === 0 ? null : status.tone,
    chart,
    bands,
    summary: {
      max: price(nowDay?.max ?? null),
      min: price(nowDay?.min ?? null),
      current: price(chart.current?.price ?? null),
      maxFigure: figure(nowDay?.max ?? null),
      minFigure: figure(nowDay?.min ?? null),
    },
    figures: figuresFor(dashboard, format),
    proposalPeriods: toPeriods(dashboard.plan.proposal?.periods ?? [], format, null),
    installedPeriods: toPeriods(
      installed?.periods ?? [],
      format,
      installed?.active_period_index ?? null,
    ),
    planRelation: planRelationOf(dashboard),
    capabilities: capabilitiesFor(dashboard),
    control: controlFactsFor(dashboard, language),
    advisory: advisoryFor(dashboard, language),
    strategy: strategyFactsFor(dashboard, language),
    site: siteFactsFor(dashboard.site, language),
    currentRange: currentRangeFor(dashboard),
    soc: socFor(dashboard),
    connection: dashboard.connection,
    startingUp: dashboard.starting_up?.active === true,
    chargerPriority: dashboard.charger_priority?.value ?? null,
    vehicles: vehiclesFor(dashboard),
    targetVehicleId: targetVehicleIdFor(dashboard),
    contextArea: market?.area_id ?? market?.area_name ?? null,
    contextCurrency: market?.currency ?? null,
    contextAreaName: market?.area_name ?? null,
    contextAreaId: market?.area_id ?? null,
    priceSource: market?.source ?? null,
    technicalCodes,
  };
}
