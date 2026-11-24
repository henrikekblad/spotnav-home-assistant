// The shared settings popover (SpotNav 3.0): one site settings surface reachable from every card. The house and heat
// cards open it with `SettingsPopover` (`{siteId | chargerId, focus}`); the charger card keeps its own Settings page
// and mounts the same battery and heat sections in it (`SiteSections`). Sections are built from what exists, never
// from who opened it: Get started (the site's own rows), price, site, solar, home battery (when one is found), heat
// (always a row; one line "Heat · nothing found" until something is, which opens the section), notifications (the
// phones, `notification-phones.ts`) and support. The price
// and solar sections are the charger card's own (`price-section.ts`), fed from the site's view.
//
// Writes go through the site-keyed commands: `spotnav/battery_control` (SpotNav's own options for the site; nothing is
// written to the battery from here), `spotnav/update_heat` (shadow only), `spotnav/update_entity_config`,
// `spotnav/update_grid_tariff`, `spotnav/choose_price_source` and `spotnav/update_site_settings` by `site_id`, and
// `spotnav/update_price` for the area and fees.
// Administrators only: the cards show no settings button to anyone else.

import { MODES } from "./house-model";
import {
  answerPriceDuplicate,
  batteryControl,
  chooseSitePriceSource,
  dismissSetupItem,
  findRegion,
  getCardInfo,
  getBlueprints,
  getDebugBundle,
  getHeat,
  betaCommand,
  getSiteEntityConfig,
  getSiteMarketOptions,
  getSiteSettingsView,
  listChargers,
  listSites,
  setEntsoeKey,
  SpotnavApiError,
  updateSiteEntityConfig,
  updateSiteGridTariff,
  updateSitePrice,
  updateSiteSettingsBySite,
  type BatteryChanges,
  type SiteTarget,
} from "./api";
import { cardOutdated, clientBlock, decodeCardInfo, ownCardBundleHash, servedHashFromBundle } from "./card-identity";
import { decodeDebugAnswer, saveDebugBundle } from "./debug-download";
import { sendDebugEditor, sendDebugReport, sendFailureText } from "./send-debug";
import { NotificationPhones } from "./notification-phones";
import { disconnectPhonesEditor, runDisconnectPhones } from "./phones";
import { BetaRows, type BetaCall } from "./beta";
import { automationsBody, decodeBlueprintUse } from "./automations";
import { cardOutdatedNote, supportAction, supportSection, type SupportAction } from "./support-section";
import { createDialog, takeActions, type DialogHandle } from "./dialog";
import {
  decodeEntityAnswer,
  entityChange,
  entityErrorKey,
  ENTITY_INVALID_VALUE,
  fieldErrorKey,
  type EntityConfig,
  type EntityDraft,
  type EntityField,
} from "./entity-config";
import { entityEditorBody, modeLabel } from "./entity-editor";
import { decodePriorityAnswer, priorityChange, priorityEditor, prioritySummary } from "./priority-list";
import { formatFixed, formatNumber, type FormatContext } from "./format";
import { GRID_TARIFF_CONFLICT, gridTariffIncluded, sameBody, storedBody } from "./grid-tariff";
import { heatEditor, writeHeat, type HeatWriteOptions } from "./heat-editors";
import { decodeHeat, heatFound, houseHeatFound, type HeatAnswer, type HeatConfig } from "./heat-model";
import { heatSettingsSection, loadSettingsSections, type HeatSettingKey } from "./heat-view";
import { daysLeft, numberWord, pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import {
  decodeMarketOptions,
  fiscalValues,
  marketEditable,
  marketFormFor,
  marketRow,
  marketSuggestion,
  switchArea,
  type MarketFormValues,
  type PriceRecord,
} from "./market";
import { marketEditorBody } from "./market-editor";
import { priceEntryFacts, siteFactsFor } from "./model";
import { addIntegrationPath, integrationPath } from "./price-provider";
import {
  priceSectionBody,
  solarSectionBody,
  type PriceWrite,
  type SectionHost,
} from "./price-section";
import { settingsErrorKey } from "./settings";
import { setupSettingsSection, type SetupRowId, setupRowsOf } from "./setup-checklist";
import {
  decodeBatteryControl,
  decodeSiteList,
  decodeSiteView,
  reportOutcomes,
  type BatteryPreheat,
  type BatteryStatus,
  type SitePriority,
  type ImportLimit,
  type PlanCosts,
  type SitePriceSettings,
  type SiteView,
} from "./site-model";
import { decodeSiteSettingsAnswer, siteSettingsErrorKey, solarSettingsChange } from "./site-settings";
import type { HomeAssistantLike, MarketOptionsV1 } from "./types";
import type { SetupBlock } from "./validate";
import {
  multiEditor,
  numberEditor,
  onOffEditor,
  choiceFieldset,
  sectionHeading,
  settingRow,
  shell,
  singleEditor,
  type EditorHandlers,
} from "./value-editors";
import type { FiscalComponentName } from "./value-writes";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

/** Where the popover opens: a section, or `house` for the top of the page (the house card's gear). */
/** The docs page that explains the wear and the margin. */
/** The most the wear may be set to, as a multiple of the currency's default (the backend's `gate.WEAR_MAX_FACTOR`). */
const WEAR_MAX_FACTOR = 10;

export const PLAN_DOCS_URL = "https://spotnav.sensnology.se/docs/home-assistant/battery-plan#the-wear-cost";

export type PopoverFocus =
  | "house"
  | "setup"
  | "price"
  | "site"
  | "solar"
  | "battery"
  | "heat"
  | "pool"
  | "spa"
  | "sauna"
  | "notifications"
  | "support";

/**
 * What a house or heat card in the card picker shows first: the first charger's site, else the first site (a home
 * without a charger), else nothing. Never an error in the picker; gives up after `timeoutMs`.
 */
export async function stubTarget(hass: HomeAssistantLike, timeoutMs: number): Promise<{ charger: string } | { site: string } | Record<string, never>> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), timeoutMs);
  });
  try {
    const chargers = await Promise.race([listChargers(hass), timeout]);
    const first = chargers?.chargers[0];
    if (first !== undefined) {
      return { charger: first.charger_id };
    }
    const sites = await Promise.race([listSites(hass), timeout]);
    const decoded = sites === null ? null : decodeSiteList(sites);
    const site = decoded?.ok === true ? decoded.value.sites[0] : undefined;
    if (site !== undefined) {
      return { site: site.site_id };
    }
  } catch {
    // An unconfigured card, never an error in the picker.
  } finally {
    clearTimeout(timer);
  }
  return {};
}

/** Open one of Home Assistant's own pages, the way its frontend moves between them. */
export function navigateTo(doc: Document, path: string): void {
  const view = doc.defaultView;
  if (view === null) {
    return;
  }
  view.history.pushState(null, "", path);
  view.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
}

// ------------------------------------------------------------------------------------------------ battery words

const LINE_KEYS: Record<string, TranslationKey> = {
  off: "battery.line.off",
  shadow_only: "battery.line.shadowOnly",
  checking: "battery.line.checking",
  checking_after_update: "battery.line.checkingAfterUpdate",
  on: "battery.line.on",
  "watching:ai_mode": "battery.line.watchingAiMode",
  "watching:strategy": "battery.line.watchingStrategy",
};

const WAITING_KEYS: Record<string, TranslationKey> = {
  firmware_unknown: "battery.waiting.firmwareUnknown",
  retry_gap: "battery.waiting.retryGap",
  no_battery_control: "battery.waiting.noBatteryControl",
  not_evening: "battery.waiting.notEvening",
  car_charging: "battery.waiting.carCharging",
  battery_not_discharging: "battery.waiting.notDischarging",
  soc_out_of_range: "battery.waiting.socOutOfRange",
  headroom_too_small: "battery.waiting.headroom",
  stale_data: "battery.waiting.staleData",
  executor_busy: "battery.waiting.busy",
  start_wait: "battery.waiting.startWait",
};

const MODE_KEYS: Record<string, TranslationKey> = {
  hold: "battery.mode.hold",
  charge: "battery.mode.charge",
  discharge_house: "battery.mode.dischargeHouse",
  normal: "battery.mode.normal",
};

const ACTION_KEYS: Record<string, TranslationKey> = {
  hold: "battery.action.hold",
  grid_charge: "battery.action.gridCharge",
  discharge_house: "battery.action.dischargeHouse",
  export_discharge: "battery.action.exportDischarge",
};

const PASSING = new Set(["obeyed", "restored"]);
const FAILING_ABORTS = [
  "write_not_taken", "no_effect", "changed_outside_spotnav", "restore_failed", "command_timeout", "not_obeyed",
  "control_not_found", "control_disabled", "action_not_supported", "no_restore_value",
];

/** A mode's outcome as passed, failed or still pending (`battery/gate.py:mode_verdict`). */
export function outcomeVerdict(outcome: string): "passed" | "failed" | "pending" {
  if (PASSING.has(outcome)) {
    return "passed";
  }
  if (outcome === "ignored" || outcome === "partial") {
    return "failed";
  }
  if (outcome.startsWith("aborted:") && FAILING_ABORTS.includes(outcome.slice("aborted:".length))) {
    return "failed";
  }
  return "pending";
}

/** A control's name as the battery's integration shows it ("Remote EMS", "Control mode"). */
function roleName(role: string): string {
  const words = role.replace(/_/g, " ");
  return role === "remote_ems" ? "Remote EMS" : words.charAt(0).toUpperCase() + words.slice(1);
}

function modeName(language: Language, mode: string): string {
  const key = MODE_KEYS[mode];
  return key === undefined ? mode.replace(/_/g, " ") : translate(language, key);
}

/** The one line under the switch (`status.line`) in words. */
export function batteryLine(language: Language, line: string): string {
  const known = LINE_KEYS[line];
  if (known !== undefined) {
    return translate(language, known);
  }
  const [kind, first, second] = line.split(":");
  if (kind === "waiting" || kind === "waiting_after_update") {
    const why = WAITING_KEYS[first ?? ""];
    const base = translate(language, kind === "waiting" ? "battery.line.waiting" : "battery.line.waitingAfterUpdate");
    return why === undefined ? base : `${base} ${translate(language, why)}`;
  }
  if (kind === "failed") {
    return translate(language, "battery.line.failed", { mode: modeName(language, first === "not_judgeable" || first === undefined ? "hold" : first) })
      + (second === "attempts" ? ` ${translate(language, "battery.line.failedAttempts")}` : "");
  }
  if (kind === "blocked") {
    return translate(language, first === "daily_write_cap" ? "battery.line.blockedWrites" : "battery.line.blocked");
  }
  return line;
}

/** A per-kWh figure of the plan in the area's money: the minor unit below one major unit ("15 öre"), the major one
 * from there, whole when it is or from 10 up ("1 kr", "12 kr"), else two decimals ("1,40 kr"). */
export function planMoney(language: Language, minor: number, costs: PlanCosts): string {
  if (Math.abs(minor) < 100 && minor !== 0) {
    return planMinor(language, minor, costs);
  }
  const major = minor / 100;
  return `${formatFixed(language, major, Number.isInteger(major) || Math.abs(major) >= 10 ? 0 : 2)} ${costs.major_unit}`;
}

/** A per-kWh figure of the plan in the area's minor unit, whatever its size ("15 öre", "1,4 cent", "540 fillér"):
 * whole when it is, else one decimal (two where the second is not zero). */
export function planMinor(language: Language, minor: number, costs: PlanCosts): string {
  return `${formatNumber(language, minor, Number.isInteger(minor) ? 0 : 2)} ${costs.minor_unit}`;
}

/** Balanced's selling margin as the mode dialog says it; `null` from a Home Assistant without it or a site with no
 * price area. A fixed figure, not a setting. */
export function planMarginLine(language: Language, status: BatteryStatus): string | null {
  const costs = status.plan_costs;
  return costs === null
    ? null
    : translate(language, "battery.planMargin", {
        margin: `${planMoney(language, costs.balanced_margin_minor_per_kwh, costs)}/kWh`,
      });
}

/** The wear field's label, in the area's minor unit ("Slitage (öre/kWh åt varje håll)"). */
export function wearLabel(language: Language, costs: PlanCosts): string {
  return translate(language, "battery.wear.label", { unit: `${costs.minor_unit}/kWh` });
}

/** The wear field's help line with the currency's default ("… Standard 15 öre."); `null` from a Home Assistant
 * that cannot take a wear of the site's own. */
export function wearHelp(language: Language, costs: PlanCosts): string | null {
  const fallback = costs.wear_default_minor_per_kwh;
  return fallback === null
    ? null
    : translate(language, "battery.wear.help", { default: planMinor(language, fallback, costs) });
}

/** What the wear field holds as the change to write: `null` for empty (the default), a figure from 0 to ten times the
 * default, or the sentence that refuses it. */
export function wearValue(language: Language, text: string, costs: PlanCosts): { value: number | null } | { error: string } {
  const typed = text.trim().replace(/\s/g, "").replace(",", ".");
  if (typed === "") {
    return { value: null };
  }
  const number = Number(typed);
  if (!Number.isFinite(number)) {
    return { error: translate(language, "settings.error.number") };
  }
  const max = WEAR_MAX_FACTOR * (costs.wear_default_minor_per_kwh ?? costs.wear_minor_per_kwh);
  if (number < 0 || number > max) {
    return {
      error: translate(language, "settings.error.range", { min: formatNumber(language, 0, 0), max: formatNumber(language, max, 2) }),
    };
  }
  return { value: Math.round(number * 100) / 100 };
}

/** One `status.needs` code in words. */
/** The one line under what SpotNav may do: the grid charge cap and what it rests on; `null` when charging from the
 * grid is not allowed or the cap could not be worked out. */
export function gridChargeCapLine(language: Language, status: BatteryStatus): string | null {
  const cap = status.grid_charge_cap;
  if (!status.allowed_actions.includes("grid_charge")) {
    return null;
  }
  if (cap.cap_kw === null) {
    if (cap.rule !== "import_limit") {
      return null;
    }
    const value = cap.import_limit?.value_kw ?? null;
    if (cap.import_limit?.per_phase?.holds === true && value !== null) {
      // Held per phase by the plant itself: a third of its limit on each phase, whatever the house does (rounded up,
      // so "under" stays true).
      return translate(language, "battery.cap.importLimitPhase", {
        phase: formatNumber(language, Math.ceil((value / 3) * 10 - 1e-9) / 10, 1),
        kw: formatNumber(language, value, 1),
      });
    }
    // The plant's limit is a total over the phases: said with its value, and why that is less than the fuse.
    return value === null
      ? translate(language, "battery.cap.importLimit")
      : translate(language, "battery.cap.importLimitTotal", { kw: formatNumber(language, value, 1) });
  }
  if (cap.rule === "accepted") {
    // The Home Assistant down risk accepted: the fuse with an empty house, the live clamp below it.
    return translate(language, "battery.cap.accepted", { kw: formatNumber(language, cap.cap_kw, 1) });
  }
  if (cap.rule !== "learned" && cap.rule !== "learning" && cap.rule !== "conservative") {
    return null;
  }
  const words = translate(language, `battery.cap.rule.${cap.rule}` as TranslationKey);
  // How long it still takes: while cautious, until the first days are learned ("försiktigt tills de första dygnen
  // är inlärda, 2 dygn kvar"); while learning, until the further margin is gone ("medan husets toppar lärs in,
  // 9 dygn kvar").
  const learned = cap.learned;
  const days =
    learned === null
      ? 0
      : cap.rule === "conservative"
        ? Math.max(learned.days_min - learned.days_spread, 1)
        : cap.rule === "learning"
          ? learned.days_left
          : 0;
  const left = days > 0 ? days : null;
  if (cap.rule === "learning" && cap.cap_kw === 0) {
    // One reason, not two: the learning's further margin where the learned peaks alone would leave a charge, the
    // house itself where they would not.
    return cap.cap_learned_kw !== null && cap.cap_learned_kw > 0 && left !== null
      ? translate(language, "battery.cap.zeroLearning", { left: daysLeft(language, left) })
      : translate(language, "battery.cap.zero", { rule: translate(language, "battery.cap.rule.learned") });
  }
  const rule = left === null ? words : `${words}, ${daysLeft(language, left)}`;
  return cap.cap_kw === 0
    ? translate(language, "battery.cap.zero", { rule })
    : translate(language, "battery.cap.line", { kw: formatNumber(language, cap.cap_kw, 1), rule });
}

/** The Home Assistant down acceptance (`battery_control_accept_ha_down`) as the battery section shows it: `row` (the
 * setting, for an administrator, only where accepting would change something or it is on: `status.grid_charge_cap
 * .ha_down.offered`) or `line` (it is on: one status line for anyone else); `null` for nothing at all. `help` is the
 * row's line under it. */
export function haDownView(
  language: Language,
  status: BatteryStatus,
  admin: boolean,
): { kind: "row"; help: string } | { kind: "line"; text: string } | null {
  const accepted = status.options.battery_control_accept_ha_down;
  const block = status.grid_charge_cap.ha_down;
  if (accepted) {
    const text = translate(language, "battery.haDown.accepted");
    return admin ? { kind: "row", help: text } : { kind: "line", text };
  }
  if (!admin || block === null || !block.offered || block.held_kw === null) {
    return null;
  }
  return { kind: "row", help: translate(language, "battery.haDown.offer", { kw: formatNumber(language, block.held_kw, 1) }) };
}

/** Under the cap's line, once the person said the plant's own import limit acts: why it does not guard the fuse as
 * it reads now, and the most it may read to do so (it limits the total over the three phases, so that is less than
 * the fuse). `null` when the limit guards, was not said to act, or no value can guard. Only the per-phase tap writes
 * the limit (`importLimitPhaseStep`). */
export function importLimitHint(language: Language, status: BatteryStatus): string | null {
  const limit = status.grid_charge_cap.import_limit;
  if (
    limit === null || limit.holds || limit.suggested_kw === null || limit.why !== null || phaseRuleApplies(limit)
    || !status.options.battery_control_import_limit_acts || !status.allowed_actions.includes("grid_charge")
  ) {
    return null;
  }
  const max = formatNumber(language, limit.suggested_kw, 1);
  return limit.value_kw === null
    ? translate(language, "battery.cap.importLimitUnset", { max })
    : translate(language, "battery.cap.importLimitTooHigh", { value: formatNumber(language, limit.value_kw, 1), max });
}

/** Refusals of the per-phase rule nobody on the site can act on: the line says nothing for them, and for codes this
 * card does not know. */
const PHASE_RULE_NOT_HERE = new Set(["not_supported", "not_field_tested", "output_type_not_supported"]);

/** What a person can do for each refusal of the per-phase rule that they can act on. */
const PHASE_RULE_STEPS: Record<string, TranslationKey> = {
  not_said_to_act: "battery.cap.phase.notSaidToAct",
  output_type_unread: "battery.cap.phase.outputTypeUnread",
  phase_control_off: "battery.cap.phase.phaseControlOff",
  grid_sensor_offline: "battery.cap.phase.gridSensorOffline",
  // The import limit not tested on the plant's firmware, or the firmware not read: the step is the field test on it,
  // never the HA-down acceptance (owner decision 7).
  firmware_not_tested: "battery.cap.phase.firmwareNotTested",
  firmware_unread: "battery.cap.phase.firmwareUnread",
};

/** Whether the plant's per-phase hold could guard the fuse on this plant (the recipe has it, field-tested, the
 * Output Type it acts with), so the total rule's advice to set the limit far under the fuse is not given. */
function phaseRuleApplies(limit: ImportLimit): boolean {
  const phase = limit.per_phase;
  return phase !== null && !PHASE_RULE_NOT_HERE.has(phase.why ?? "");
}

/** The per-phase import-limit rule's one actionable step (`grid_charge_cap.import_limit.per_phase`): what is missing
 * for the plant's own per-phase hold to guard the fuse, so grid charging is no longer capped. `null` while battery
 * control is off, grid charging is not allowed, the plant's limit already guards, or nothing here can be done
 * (`PHASE_RULE_NOT_HERE`, the total rule's own refusals, an unknown code, no value that guards). `code` is the
 * refusal (`limit` for a value to lower or set), `entityId` the plant's entity to open, `setKw` the value the
 * administrator's tap sets the limit to (`set_import_limit`: lowered, never raised, then said to act; `null` where
 * the value already guards and only saying it acts is left). Said to act with a value too high or unset is shown as
 * `limit`, so one tap does both. */
export function importLimitPhaseStep(
  language: Language,
  status: BatteryStatus,
  name?: (entityId: string) => string,
): { code: string; text: string; entityId: string | null; setKw: number | null } | null {
  const limit = status.grid_charge_cap.import_limit;
  const phase = limit?.per_phase ?? null;
  if (
    limit === null || phase === null || limit.holds || phase.holds || !status.options.battery_control_enabled
    || !status.allowed_actions.includes("grid_charge")
  ) {
    return null;
  }
  // A limit that reads nothing is never written over (it may hide a lower value set on purpose): it is set in the
  // maker's app, and the line says the most that guards.
  if (limit.value_kw === null && (phase.why === null || phase.why === "not_said_to_act")) {
    if (phase.max_kw === null || phase.max_kw < 0.1) {
      return null;
    }
    const max = formatNumber(language, Math.floor(phase.max_kw * 10 + 1e-9) / 10, 1);
    return { code: "limit_unread", text: translate(language, "battery.cap.phase.limitUnset", { max }), entityId: null, setKw: null };
  }
  // The backend's own figure (`set_kw`): what the tap writes, `null` when the limit already guards.
  const setKw = phase.set_kw;
  const tooHigh = setKw !== null;
  if (phase.why !== null && !(phase.why === "not_said_to_act" && tooHigh)) {
    const key = PHASE_RULE_STEPS[phase.why];
    if (key === undefined) {
      return null;
    }
    const entityId = phase.entity_id;
    const entity = entityId === null ? "" : (name?.(entityId) ?? entityId);
    // Said to act is the one step the card can take itself (`set_import_limit`), the value already low enough.
    return { code: phase.why, text: translate(language, key, { name: entity }), entityId, setKw: null };
  }
  if (setKw === null) {
    return null;
  }
  // The limit reads more than guards the fuse, or nothing: the card can set it (an administrator's tap), or the
  // person sets it in the maker's app.
  const max = formatNumber(language, setKw, 1);
  const text = limit.value_kw === null
    ? translate(language, "battery.cap.phase.limitUnset", { max })
    : translate(language, "battery.cap.phase.limitTooHigh", { value: formatNumber(language, limit.value_kw, 1), max });
  return { code: "limit", text, entityId: null, setKw };
}

/** The name a preheating switch goes by when Home Assistant has none to give (a disabled entity has no state). */
const PREHEAT_SWITCH_NAME = "ESS Preheating Enable";

/** The battery section's preheating line (`status.preheat`), `null` for none or a code this card does not know.
 * `friendlyName` is the switch's own name in Home Assistant, when it has a state. */
export function preheatLine(language: Language, block: BatteryPreheat | null, friendlyName?: unknown): string | null {
  if (block === null || (block.code !== "off" && block.code !== "disabled")) {
    return null;
  }
  const name = typeof friendlyName === "string" && friendlyName !== "" ? friendlyName : PREHEAT_SWITCH_NAME;
  const temp = block.cell_c === null ? "–" : formatNumber(language, block.cell_c, 1);
  return translate(language, block.code === "off" ? "battery.preheat.off" : "battery.preheat.disabled", { temp, name });
}

/** A grid charge per phase: what each phase is charged at while one runs ("Nätladdning per fas: L1 1,01 kW · L2 0
 * kW · L3 0,13 kW"), else the most each phase may take when a grid charge would run so; `null` otherwise. */
export function perPhaseLine(language: Language, status: BatteryStatus): string | null {
  const block = status.per_phase;
  if (block === null || block.state !== "verified" || !status.allowed_actions.includes("grid_charge")) {
    return null;
  }
  const values = block.charging_kw ?? (block.used ? block.caps_kw : null);
  if (values === null) {
    return null;
  }
  const kw = (value: number): string => formatNumber(language, value, 2);
  return translate(language, block.charging_kw !== null ? "battery.perPhase.charging" : "battery.perPhase.caps", {
    l1: kw(values.L1), l2: kw(values.L2), l3: kw(values.L3),
  });
}

/** The outside-control reasons a person may clear ("it was me"): the battery's own settings or behaviour, never an
 * integration or a planner (`outside_control.TOLD_REASONS`). */
const OUTSIDE_CLEARABLE = ["settings_changed", "grid_charge_behaviour", "export_discharge_behaviour"];

export function batteryOutsideClearable(need: string): boolean {
  return need.startsWith("outside_control:") && OUTSIDE_CLEARABLE.includes(need.slice("outside_control:".length));
}

export function batteryNeed(language: Language, need: string): string {
  const [kind, param] = need.split(":");
  switch (kind) {
    case "no_battery_control":
      return translate(language, "battery.need.noBatteryControl");
    case "firmware_unknown":
      return translate(language, "battery.need.firmwareUnknown");
    case "control_not_found":
      return translate(language, "battery.need.controlNotFound", { role: roleName(param ?? "") });
    case "needs_hacs_integration":
      // The repository is the recipe's (`BrandControl.hacs_repo`), after the first colon only: it holds a slash.
      return translate(language, "battery.need.needsHacs", { repo: need.slice(kind.length + 1) || "HACS" });
    case "control_disabled":
      return translate(language, "battery.need.controlDisabled", { role: roleName(param ?? "") });
    case "outside_control":
      return translate(language, "battery.need.outsideControl");
    case "ai_takeover_ack":
      return translate(language, "battery.need.aiTakeoverAck");
    case "strategy_ack":
      return translate(language, "battery.need.strategyAck");
    case "hardware_test_first":
      return translate(language, "battery.need.hardwareTestFirst");
    case "hardware_test_again":
      return translate(language, "battery.need.hardwareTestAgain");
    case "recipe_blocked":
      return translate(language, "battery.need.recipeBlocked");
    case "restore_by_hand": {
      // `<entity_id>=<value>|...` after the first colon (a value may hold one).
      const rows = need.slice(kind.length + 1).split("|").filter((row) => row.includes("="));
      const list = rows.map((row) => {
        const at = row.indexOf("=");
        return `${row.slice(0, at)} → ${row.slice(at + 1)}`;
      }).join(", ");
      return translate(language, "battery.need.restoreByHand", { list });
    }
    case "self_managed":
      return translate(language, "battery.need.selfManaged");
    case "inverter_power_zero":
      return translate(language, "battery.need.inverterPowerZero");
    case "grid_setpoint_selling":
      return translate(language, "battery.need.gridSetpointSelling");
    default:
      return need;
  }
}

// ------------------------------------------------------------------------------------------------ shared sections

export interface SiteSectionsInput {
  doc: Document;
  idPrefix: string;
  hass: () => HomeAssistantLike | null;
  target: () => SiteTarget | null;
  language: () => Language;
  isAdmin: () => boolean;
  /** Open one value's editor in the host's dialog; Save or Cancel returns to the settings page (`repaint`). */
  openEditor: (title: string, build: (handlers: EditorHandlers) => HTMLFormElement) => void;
  /** Paint the settings page again (after a read or a write). */
  repaint: () => void;
  /** A write took: the card reads its own view again. */
  onChanged?: (what: "battery" | "heat") => void;
}

/**
 * The home battery and heat sections, the same in every card. `load()` reads both for the site; the nodes are drawn
 * from what was read. A failed read leaves the section out (battery) or says so (heat).
 */
export class SiteSections {
  /** The site's priority list from the same view read (`loadSite`), `null` before it or where none is stated. */
  priority: SitePriority | null = null;
  battery: BatteryStatus | null = null;
  batteryFound = false;
  heat: HeatAnswer | null = null;
  heatFailed = false;
  /** "Heat · nothing found" opened by a tap (or the popover's focus on heat). */
  heatExpanded = false;
  private readonly input: SiteSectionsInput;

  constructor(input: SiteSectionsInput) {
    this.input = input;
  }

  private get language(): Language {
    return this.input.language();
  }

  /** Read whether the site has a battery (its settings view), then both sections: the charger card's Settings. */
  async loadSite(): Promise<void> {
    const hass = this.input.hass();
    const target = this.input.target();
    if (hass === null || target === null) {
      return;
    }
    let battery = this.batteryFound;
    try {
      const decoded = decodeSiteView(await getSiteSettingsView(hass, target));
      battery = decoded.ok && decoded.value.battery.found;
      this.priority = decoded.ok ? decoded.value.priority : null;
    } catch {
      // No site view: the battery section stays as it was.
    }
    await this.load({ battery });
  }

  async load(options: { battery: boolean }): Promise<void> {
    const hass = this.input.hass();
    const target = this.input.target();
    if (hass === null || target === null) {
      return;
    }
    this.batteryFound = options.battery;
    await Promise.all([
      (async () => {
        try {
          const decoded = decodeHeat(await getHeat(hass, target));
          this.heat = decoded.ok ? decoded.value : null;
          this.heatFailed = !decoded.ok;
        } catch {
          this.heat = null;
          this.heatFailed = true;
        }
      })(),
      (async () => {
        if (!options.battery) {
          this.battery = null;
          return;
        }
        try {
          const decoded = decodeBatteryControl(await batteryControl(hass, target));
          this.battery = decoded.ok && decoded.value.ok ? decoded.value.status : null;
        } catch {
          this.battery = null;
        }
      })(),
    ]);
  }

  /** Whether anything heat-related was found: the house's heat, a pool, a spa or a sauna (`heat-model.heatFound`). */
  heatFound(): boolean {
    return this.heat !== null && heatFound(this.heat);
  }

  // ---- battery

  async writeBattery(
    changes: BatteryChanges,
    enableControlEntities = false,
    clearOutsideControl = false,
    enableFactEntities = false,
    setImportLimit = false,
  ): Promise<string | null> {
    const hass = this.input.hass();
    const target = this.input.target();
    const language = this.language;
    if (hass === null || target === null) {
      return translate(language, "battery.error.generic");
    }
    try {
      const decoded = decodeBatteryControl(
        await batteryControl(hass, target, Object.keys(changes).length === 0 ? null : changes, enableControlEntities, clearOutsideControl, enableFactEntities, setImportLimit),
      );
      if (!decoded.ok) {
        return translate(language, "battery.error.generic");
      }
      if (!decoded.value.ok && decoded.value.error === "spotnav_import_limit_not_set") {
        if (decoded.value.status !== null) {
          this.battery = decoded.value.status;
          this.input.onChanged?.("battery");
        }
        return translate(language, "battery.cap.phase.setFailed");
      }
      if (!decoded.value.ok) {
        return translate(language, decoded.value.error === "spotnav_not_admin" ? "battery.error.notAdmin" : "battery.error.generic");
      }
      this.battery = decoded.value.status;
      this.input.onChanged?.("battery");
      return null;
    } catch {
      return translate(language, "battery.error.generic");
    }
  }

  /**
   * The opt-in's confirm for the brand's disabled control entities: how many, in which integration, the list folded.
   * Nothing is turned on before the button; `alsoOn` turns "Styr batteriet" on with it.
   */
  openEnableEntities(alsoOn: boolean): void {
    const found = this.battery?.enable_entities ?? null;
    if (found === null) {
      return;
    }
    const { doc, idPrefix } = this.input;
    const language = this.language;
    const count = found.entities.length;
    this.input.openEditor(translate(language, "battery.switch"), (handlers) => {
      const editor = shell(doc, language, "battery-enable", handlers, translate(language, alsoOn ? "battery.enable.confirm" : "battery.enable.confirmOnly"));
      const text = translate(language, `battery.enable.text.${pluralForm(language, count)}`, { count: String(count), integration: found.integration });
      editor.body.append(element(doc, "p", C.settingRowHelp, text));
      const folded = element(doc, "details");
      folded.dataset["enableEntities"] = "true";
      folded.append(element(doc, "summary", undefined, translate(language, "battery.enable.list")));
      const list = element(doc, "ul");
      for (const entity of found.entities) {
        const item = element(doc, "li", undefined, entity.name);
        item.dataset["entityId"] = entity.entity_id;
        item.title = entity.entity_id;
        list.append(item);
      }
      folded.append(list);
      editor.body.append(folded, element(doc, "p", C.settingRowHelp, translate(language, "battery.enable.note", { integration: found.integration })));
      editor.form.dataset["batteryEnable"] = idPrefix;
      editor.submit(async () => await this.writeBattery(alsoOn ? { battery_control_enabled: true } : {}, true));
      return editor.form;
    });
  }

  /** The AI-mode explanation; its button writes `battery_control_ai_takeover_ack`. */
  openTakeover(): void {
    const { doc, idPrefix } = this.input;
    const language = this.language;
    this.input.openEditor(translate(language, "battery.ack.title"), (handlers) => {
      const editor = shell(doc, language, "battery-ack", handlers, translate(language, "battery.ack.confirm"));
      editor.body.append(element(doc, "p", C.settingRowHelp, translate(language, "battery.ack.text")));
      editor.form.dataset["batteryAck"] = idPrefix;
      editor.submit(async () => await this.writeBattery({ battery_control_ai_takeover_ack: true }));
      return editor.form;
    });
  }

  /** For a battery whose own strategy SpotNav cannot read: the explanation that SpotNav's commands override it;
   * its button writes the same acknowledgement as the AI-mode takeover (`battery_control_ai_takeover_ack`). */
  openStrategyAck(): void {
    const { doc, idPrefix } = this.input;
    const language = this.language;
    this.input.openEditor(translate(language, "battery.strategyAck.title"), (handlers) => {
      const editor = shell(doc, language, "battery-strategy-ack", handlers, translate(language, "battery.ack.confirm"));
      editor.body.append(element(doc, "p", C.settingRowHelp, translate(language, "battery.strategyAck.text")));
      editor.form.dataset["batteryAck"] = idPrefix;
      editor.submit(async () => await this.writeBattery({ battery_control_ai_takeover_ack: true }));
      return editor.form;
    });
  }

  /**
   * The per-phase import limit's one tap, confirmed in a dialog: with `setKw` SpotNav lowers the plant's own import
   * limit to it first (`set_import_limit`: never raised, read back), then says that it acts; without, the value is
   * already low enough and only that is said.
   */
  private openImportLimit(status: BatteryStatus, setKw: number | null): void {
    const { doc } = this.input;
    const language = this.language;
    const value = status.grid_charge_cap.import_limit?.value_kw ?? null;
    const title = translate(language, "battery.cap.phase.actsLabel");
    this.input.openEditor(title, (handlers) => {
      const positive = translate(language, setKw === null ? "battery.cap.phase.turnOnConfirm" : "battery.cap.phase.setConfirm");
      const editor = shell(doc, language, "battery-import-limit", handlers, positive);
      const text = setKw === null
        ? translate(language, "battery.cap.phase.turnOnText", { value: value === null ? "–" : formatNumber(language, value, 1) })
        : translate(language, "battery.cap.phase.setText", { max: formatNumber(language, setKw, 1) });
      editor.body.append(element(doc, "p", C.settingRowHelp, text));
      editor.body.append(element(doc, "p", C.settingRowHelp, translate(language, "battery.cap.phase.checked")));
      editor.form.dataset["batteryImportLimit"] = setKw === null ? "acts" : "set";
      editor.submit(async () => await this.writeBattery({}, false, false, false, true));
      return editor.form;
    });
  }

  /** The Home Assistant down risk explained, with the checkbox that writes `battery_control_accept_ha_down`. */
  openHaDown(): void {
    const { doc, idPrefix } = this.input;
    const language = this.language;
    const accepted = this.battery?.options.battery_control_accept_ha_down === true;
    this.input.openEditor(translate(language, "battery.haDown.title"), (handlers) => {
      const editor = shell(doc, language, "battery-ha-down", handlers);
      editor.body.append(element(doc, "p", C.settingRowHelp, translate(language, "battery.haDown.text")));
      const list = element(doc, "ul", C.settingRowHelp);
      for (const key of ["battery.haDown.worst", "battery.haDown.needs", "battery.haDown.without"] as const) {
        list.append(element(doc, "li", undefined, translate(language, key)));
      }
      editor.body.append(list);
      const box = doc.createElement("input");
      box.type = "checkbox";
      box.id = `${idPrefix}-battery-ha-down`;
      box.checked = accepted;
      box.dataset["field"] = "accept_ha_down";
      const label = element(doc, "label", C.siteChoice);
      label.append(box, doc.createTextNode(translate(language, "battery.haDown.check")));
      editor.body.append(label);
      editor.form.dataset["batteryHaDown"] = idPrefix;
      editor.submit(async () =>
        box.checked === accepted ? null : await this.writeBattery({ battery_control_accept_ha_down: box.checked }),
      );
      return editor.form;
    });
  }

  /**
   * The battery mode: one of three, each with its help line; under them the wear per kWh each way (in the area's
   * minor unit, empty or Default for the currency's own), Balanced's selling margin, the link to the battery plan's
   * page and, while SpotNav does not control the battery, what the choice does meanwhile. Save writes what changed
   * (`battery_mode`, `battery_wear_minor_per_kwh`) in one call.
   */
  private openBatteryMode(title: string, current: string, status: BatteryStatus): void {
    const { doc, idPrefix } = this.input;
    const language = this.language;
    this.input.openEditor(title, (handlers) => {
      const editor = shell(doc, language, "battery-mode", handlers);
      let chosen = current;
      editor.body.append(
        choiceFieldset(
          doc,
          {
            options: MODES.map((mode) => ({
              value: mode,
              label: translate(language, `house.mode.${mode}` as TranslationKey),
              help: translate(language, `battery.modeHelp.${mode}` as TranslationKey),
            })),
            selected: current,
            idPrefix: `${idPrefix}-battery-mode`,
          },
          (value) => {
            chosen = value;
          },
        ),
      );
      const costs = status.plan_costs;
      const help = costs === null ? null : wearHelp(language, costs);
      const own = status.options.battery_wear_minor_per_kwh;
      let field: HTMLInputElement | null = null;
      if (costs !== null && help !== null) {
        const label = element(doc, "label", C.siteChoice, wearLabel(language, costs));
        label.htmlFor = `${idPrefix}-battery-wear`;
        editor.body.append(label);
        const wear = doc.createElement("input");
        wear.type = "text";
        wear.inputMode = "decimal";
        wear.className = C.settingsInput;
        wear.id = `${idPrefix}-battery-wear`;
        wear.dataset["field"] = "battery_wear";
        wear.value = own === null ? "" : formatNumber(language, own, 2).replace(/\s/g, "");
        wear.placeholder = formatNumber(language, costs.wear_default_minor_per_kwh ?? costs.wear_minor_per_kwh, 2);
        field = wear;
        const fallback = element(doc, "button", C.settingRowSuggestion, translate(language, "battery.wear.default")) as HTMLButtonElement;
        fallback.type = "button";
        fallback.dataset["wearDefault"] = "true";
        fallback.addEventListener("click", () => {
          wear.value = "";
          wear.focus();
        });
        const line = element(doc, "div", C.settingsRow);
        line.append(wear, element(doc, "span", C.settingsUnit, `${costs.minor_unit}/kWh`), fallback);
        editor.body.append(line, element(doc, "p", C.entityHelp, help));
      }
      const margin = planMarginLine(language, status);
      const words = element(doc, "p", C.settingRowHelp, margin === null ? "" : `${margin} `);
      words.dataset["planCosts"] = "true";
      const more = doc.createElement("a");
      more.href = PLAN_DOCS_URL;
      more.target = "_blank";
      more.rel = "noopener noreferrer";
      more.textContent = translate(language, "battery.planCostsDocs");
      words.append(more);
      editor.body.append(words);
      if (status.state !== "on") {
        // The mode counts even when nothing is controlled: it moves the comparison's marked row.
        const shadow = element(doc, "p", C.settingRowHelp, translate(language, "battery.modeShadow"));
        shadow.dataset["batteryModeShadow"] = "true";
        editor.body.append(shadow);
      }
      editor.submit(async () => {
        const changes: BatteryChanges = {};
        if (chosen !== current) {
          changes["battery_mode"] = chosen;
        }
        if (field !== null && costs !== null) {
          const read = wearValue(language, field.value, costs);
          if ("error" in read) {
            return read.error;
          }
          if (read.value !== own) {
            changes["battery_wear_minor_per_kwh"] = read.value;
          }
        }
        return Object.keys(changes).length === 0 ? null : await this.writeBattery(changes);
      });
      return editor.form;
    });
  }

  private onOff(title: string, key: string, on: boolean, intro?: string): void {
    const { doc, idPrefix } = this.input;
    const language = this.language;
    this.input.openEditor(title, (handlers) =>
      onOffEditor(
        doc,
        language,
        { ...(intro === undefined ? {} : { intro }), on, idPrefix: `${idPrefix}-${key}` },
        async (value) => (value === on ? null : await this.writeBattery({ [key]: value })),
        handlers,
      ),
    );
  }

  /**
   * What the person still has to do before control can run, as numbered steps in a box: the disabled controls as one
   * step with a link to the battery's device, the AI-mode takeover with its explanation. What nobody can act on (a
   * brand SpotNav cannot run, something else running the battery) is a plain line instead.
   */
  private batteryNeeds(needs: string[], deviceId: string | null, admin: boolean): HTMLElement | null {
    if (needs.length === 0) {
      return null;
    }
    const doc = this.input.doc;
    const language = this.language;
    const wrap = element(doc, "div");
    const disabled = needs.filter((need) => need.startsWith("control_disabled:")).map((need) => roleName(need.split(":")[1] ?? ""));
    const plain = needs.filter((need) => need === "no_battery_control" || need.startsWith("outside_control"));
    const steps: HTMLElement[] = [];
    for (const need of needs) {
      if (plain.includes(need) || (need.startsWith("control_disabled:") && need !== needs.find((item) => item.startsWith("control_disabled:")))) {
        continue;
      }
      const item = element(doc, "li");
      if (need.startsWith("control_disabled:")) {
        item.dataset["need"] = "control_disabled";
        const names = new Intl.ListFormat(language, { type: "conjunction" }).format(disabled);
        item.append(translate(language, `battery.need.controlsDisabled.${pluralForm(language, disabled.length)}`, { count: String(disabled.length), countWord: numberWord(language, disabled.length), names }));
        if (deviceId !== null && admin) {
          const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(language, "battery.device.open")) as HTMLButtonElement;
          button.type = "button";
          button.dataset["action"] = "open-battery-device";
          button.addEventListener("click", () => navigateTo(doc, `/config/devices/device/${encodeURIComponent(deviceId)}`));
          item.append(button);
        }
        if (admin && this.battery?.enable_entities != null) {
          const enable = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(language, "battery.enable.open")) as HTMLButtonElement;
          enable.type = "button";
          enable.dataset["action"] = "enable-battery-entities";
          enable.addEventListener("click", () => this.openEnableEntities(false));
          item.append(enable);
        }
      } else {
        item.dataset["need"] = need;
        item.append(batteryNeed(language, need));
        if (need === "ai_takeover_ack" && admin) {
          const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(language, "battery.ack.open")) as HTMLButtonElement;
          button.type = "button";
          button.dataset["edit"] = "battery_control_ai_takeover_ack";
          button.addEventListener("click", () => this.openTakeover());
          item.append(button);
        }
        if (need === "strategy_ack" && admin) {
          const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(language, "battery.ack.open")) as HTMLButtonElement;
          button.type = "button";
          button.dataset["edit"] = "battery_control_ai_takeover_ack";
          button.addEventListener("click", () => this.openStrategyAck());
          item.append(button);
        }
      }
      steps.push(item);
    }
    for (const need of plain) {
      const line = element(doc, "p", C.settingRowHelp, batteryNeed(language, need));
      line.dataset["need"] = need.split(":")[0]!;
      wrap.append(line);
      if (admin && batteryOutsideClearable(need)) {
        // The person's "it was me" (say a mode tried by hand): the mark ends, the changes counted are forgotten.
        const help = element(doc, "p", C.settingRowHelp, translate(language, "battery.outsideClear.help"));
        help.dataset["needHelp"] = "outside_control";
        const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(language, "battery.outsideClear.button")) as HTMLButtonElement;
        button.type = "button";
        button.dataset["action"] = "clear-outside-control";
        button.addEventListener("click", () => {
          button.disabled = true;
          void this.writeBattery({}, false, true).then((error) => {
            if (error !== null) {
              help.textContent = error;
              button.disabled = false;
            } else {
              this.input.repaint();
            }
          });
        });
        wrap.append(help, button);
      }
    }
    if (steps.length > 0) {
      const box = element(doc, "div", C.batteryNeeds);
      box.dataset["batteryNeeds"] = "true";
      const list = element(doc, "ol");
      list.append(...steps);
      box.append(
        element(doc, "p", C.batteryNeedsHeading, translate(language, `battery.needs.heading.${pluralForm(language, steps.length)}`, { count: String(steps.length), countWord: numberWord(language, steps.length, true) })),
        list,
      );
      wrap.append(box);
    }
    return wrap;
  }

  /** A fact the battery plan needs (its size, its lowest level) in a disabled entity: one line and a button that
   * turns on just those entities, for an administrator; `null` when none is. */
  private factsNotice(status: BatteryStatus, admin: boolean): HTMLElement | null {
    const found = status.enable_facts;
    if (!admin || found === null || found.entities.length === 0) {
      return null;
    }
    const { doc } = this.input;
    const language = this.language;
    const size = found.needs.includes("size");
    const floor = found.needs.includes("floor");
    const rating = found.needs.includes("rating");
    const which = rating ? (size || floor ? "several" : "rating") : size && floor ? "both" : size ? "size" : "floor";
    const wrap = element(doc, "div");
    wrap.dataset["enableFacts"] = "true";
    const line = element(doc, "p", C.settingRowHelp, translate(language, `battery.enableFacts.${which}`, { integration: found.integration }));
    line.title = found.entities.map((entity) => entity.entity_id).join(", ");
    const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(language, "battery.enableFacts.button")) as HTMLButtonElement;
    button.type = "button";
    button.dataset["action"] = "enable-battery-facts";
    button.addEventListener("click", () => {
      button.disabled = true;
      void this.writeBattery({}, false, false, true).then((error) => {
        if (error !== null) {
          line.textContent = error;
          button.disabled = false;
        } else {
          this.input.repaint();
        }
      });
    });
    wrap.append(line, button);
    return wrap;
  }

  /** A cold battery's preheating off or its switch disabled: one line, with the battery's device for an
   * administrator; `null` when none shows. */
  private preheatNotice(status: BatteryStatus, admin: boolean): HTMLElement | null {
    const block = status.preheat;
    const line = preheatLine(this.language, block, this.input.hass()?.states?.[block?.entity_id ?? ""]?.attributes["friendly_name"]);
    if (block === null || line === null) {
      return null;
    }
    const doc = this.input.doc;
    const note = element(doc, "p", C.settingsNotice, line);
    note.dataset["note"] = `preheat_${block.code}`;
    note.setAttribute("role", "status");
    const deviceId = block.device_id;
    if (deviceId !== null && admin) {
      const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(this.language, "battery.device.open")) as HTMLButtonElement;
      button.type = "button";
      button.dataset["action"] = "open-preheat-device";
      button.addEventListener("click", () => navigateTo(doc, `/config/devices/device/${encodeURIComponent(deviceId)}`));
      note.append(" ", button);
    }
    return note;
  }

  /** The per-phase import-limit rule's one step (`importLimitPhaseStep`), with its button for an administrator:
   * turn on the site's word that the limit acts, or open the plant's entity in Home Assistant. */
  private importLimitPhaseNotice(status: BatteryStatus, admin: boolean): HTMLElement | null {
    const language = this.language;
    const states = this.input.hass()?.states;
    const name = (entityId: string): string => {
      const friendly = states?.[entityId]?.attributes["friendly_name"];
      return typeof friendly === "string" && friendly !== "" ? friendly : entityId;
    };
    const step = importLimitPhaseStep(language, status, name);
    if (step === null) {
      return null;
    }
    const doc = this.input.doc;
    const note = element(doc, "p", C.settingsNotice, step.text);
    note.dataset["note"] = "import_limit_phase";
    note.dataset["code"] = step.code;
    note.setAttribute("role", "status");
    if (!admin) {
      return note;
    }
    if (step.setKw !== null && status.options.battery_control_shadow_only) {
      // Control only watches: SpotNav writes nothing to the plant, so the limit is set in the maker's app.
      note.append(" ", translate(language, "battery.cap.phase.setInApp"));
    } else if (step.code === "limit_unread") {
      note.append(" ", translate(language, "battery.cap.phase.setInApp"));
    } else if (step.code === "not_said_to_act" || step.setKw !== null) {
      const setKw = step.setKw;
      const label = setKw === null
        ? translate(language, "battery.cap.phase.turnOn")
        : translate(language, "battery.cap.phase.setTo", { max: formatNumber(language, setKw, 1) });
      const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, label) as HTMLButtonElement;
      button.type = "button";
      button.dataset["action"] = setKw === null ? "import-limit-acts" : "import-limit-set";
      button.addEventListener("click", () => this.openImportLimit(status, setKw));
      note.append(" ", button);
    } else if (step.entityId !== null) {
      const entityId = step.entityId;
      const button = element(doc, "button", `${C.button} ${C.batteryNeedAction}`, translate(language, "battery.cap.phase.open")) as HTMLButtonElement;
      button.type = "button";
      button.dataset["action"] = "open-import-limit-entity";
      // Home Assistant's own dialog for the entity: its settings let a disabled one be enabled.
      button.addEventListener("click", () =>
        button.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true })),
      );
      note.append(" ", button);
    }
    return note;
  }

  batteryNode(): HTMLElement | null {
    if (!this.batteryFound) {
      return null;
    }
    const doc = this.input.doc;
    const language = this.language;
    const admin = this.input.isAdmin();
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "battery";
    section.append(sectionHeading(doc, "battery", translate(language, "battery.title")));
    const status = this.battery;
    if (status === null) {
      section.append(element(doc, "p", C.muted, translate(language, "battery.failed")));
      return section;
    }
    section.append(element(doc, "p", C.siteApplies, translate(language, "battery.intro")));
    const options = status.options;
    const onText = (on: boolean): string => translate(language, on ? "settings.value.on" : "settings.value.off");
    const mode = (MODES as readonly string[]).includes(options.battery_mode) ? options.battery_mode : "balanced";
    const modeLabel = translate(language, "battery.modeLabel");
    section.append(
      ...settingRow(doc, {
        key: "battery_mode",
        label: modeLabel,
        value: translate(language, `house.mode.${mode}` as TranslationKey),
        ...(admin
          ? {
              onTap: () => this.openBatteryMode(modeLabel, mode, status),
              changeableText: translate(language, "settings.row.changeable", {
                label: modeLabel,
                value: translate(language, `house.mode.${mode}` as TranslationKey),
              }),
            }
          : {}),
      }),
    );
    const switchLabel = translate(language, "battery.switch");
    section.append(
      ...settingRow(doc, {
        key: "battery_control_enabled",
        label: switchLabel,
        value: onText(options.battery_control_enabled),
        help: batteryLine(language, status.line),
        ...(admin
          ? {
              onTap: () =>
                status.enable_entities !== null && !options.battery_control_enabled
                  ? this.openEnableEntities(true)
                  : this.onOff(switchLabel, "battery_control_enabled", options.battery_control_enabled, translate(language, "battery.switchHelp")),
              changeableText: translate(language, "settings.row.changeable", { label: switchLabel, value: onText(options.battery_control_enabled) }),
            }
          : {}),
      }),
    );
    const facts = this.factsNotice(status, admin);
    if (facts !== null) {
      section.append(facts);
    }
    const needs = this.batteryNeeds(status.needs, status.device_id, admin);
    if (needs !== null) {
      section.append(needs);
    }
    const verification = status.verification;
    if (verification !== null) {
      const progress = element(
        doc,
        "p",
        C.settingRowHelp,
        translate(language, "battery.check.progress", {
          done: String(verification.done.length),
          total: String(Math.max(verification.modes.length, verification.done.length)),
        }),
      );
      progress.dataset["batteryProgress"] = "true";
      section.append(progress);
    }
    const outcomes = reportOutcomes(status.report);
    const modes = ["hold", "charge", "discharge_house", "normal"].filter((mode) => mode in outcomes);
    if (modes.length > 0) {
      for (const mode of modes) {
        const verdict = outcomeVerdict(outcomes[mode] ?? "");
        const [row] = settingRow(doc, {
          key: `battery_mode_${mode}`,
          label: modeName(language, mode),
          value: translate(language, `battery.outcome.${verdict}` as TranslationKey),
        });
        row!.dataset["verdict"] = verdict;
        section.append(row!);
      }
    }
    // In the plan's own order (hold first), not the answer's alphabetical one.
    const allowed = Object.entries(ACTION_KEYS)
      .filter(([action]) => status.allowed_actions.includes(action))
      .map(([, key]) => key);
    if (allowed.length > 0) {
      section.append(
        element(doc, "p", C.settingRowHelp, translate(language, "battery.allowed", { actions: allowed.map((key) => translate(language, key)).join(", ") })),
      );
    }
    const capLine = gridChargeCapLine(language, status);
    if (capLine !== null) {
      const cap = element(doc, "p", C.settingRowHelp, capLine);
      cap.dataset["gridChargeCap"] = "true";
      section.append(cap);
    }
    const limitHint = importLimitHint(language, status);
    if (limitHint !== null) {
      const hint = element(doc, "p", C.settingRowHelp, limitHint);
      hint.dataset["importLimit"] = "true";
      section.append(hint);
    }
    const phaseStep = this.importLimitPhaseNotice(status, admin);
    if (phaseStep !== null) {
      section.append(phaseStep);
    }
    const haDown = haDownView(language, status, admin);
    if (haDown?.kind === "line") {
      const line = element(doc, "p", C.settingRowHelp, haDown.text);
      line.dataset["haDown"] = "accepted";
      section.append(line);
    } else if (haDown?.kind === "row") {
      const label = translate(language, "battery.haDown.label");
      const on = options.battery_control_accept_ha_down;
      const value = translate(language, on ? "battery.haDown.on" : "battery.haDown.off");
      section.append(
        ...settingRow(doc, {
          key: "battery_control_accept_ha_down",
          label,
          value,
          help: haDown.help,
          onTap: () => this.openHaDown(),
          changeableText: translate(language, "settings.row.changeable", { label, value }),
        }),
      );
    }
    const phaseLine = perPhaseLine(language, status);
    if (phaseLine !== null) {
      const phases = element(doc, "p", C.settingRowHelp, phaseLine);
      phases.dataset["perPhase"] = "true";
      section.append(phases);
    }
    const preheat = this.preheatNotice(status, admin);
    if (preheat !== null) {
      section.append(preheat);
    }
    if (status.notes.some((note) => note.code === "ai_mode_back")) {
      const note = element(doc, "p", C.settingsNotice, translate(language, "battery.note.aiModeBack"));
      note.dataset["note"] = "ai_mode_back";
      note.setAttribute("role", "status");
      section.append(note);
    }
    return section;
  }

  // ---- heat

  async writeHeatChanges(changes: Partial<Record<keyof HeatConfig, unknown>>, options?: HeatWriteOptions): Promise<string | null> {
    const hass = this.input.hass();
    const target = this.input.target();
    const answer = this.heat;
    const language = this.language;
    if (hass === null || target === null || answer === null) {
      return translate(language, "heat.error.generic");
    }
    const result = await writeHeat(hass, target, answer, changes, language, options);
    if (result.config !== null && this.heat !== null) {
      this.heat = { ...this.heat, config: result.config };
    }
    this.input.onChanged?.("heat");
    return result.error;
  }

  /** One heat setting's editor (also the heat card's away editor). */
  openHeatEditor(key: HeatSettingKey | "away"): void {
    const answer = this.heat;
    if (answer === null) {
      return;
    }
    const { doc, idPrefix } = this.input;
    const language = this.language;
    let built: { title: string; body: HTMLElement } | null = null;
    const probe = heatEditor({ doc, language, idPrefix, answer, write: (changes, options) => this.writeHeatChanges(changes, options) }, key, {
      onDone: () => undefined,
      onCancel: () => undefined,
    });
    if (probe === null) {
      return;
    }
    this.input.openEditor(probe.title, (handlers) => {
      built = heatEditor({ doc, language, idPrefix, answer, write: (changes, options) => this.writeHeatChanges(changes, options) }, key, handlers);
      return (built?.body ?? element(doc, "form")) as HTMLFormElement;
    });
  }

  /** The "Heating" section, then the pool's, the spa's and the sauna's own where each is set up or detected. */
  heatNodes(): HTMLElement[] {
    const nodes = [this.heatNode()];
    const answer = this.heat;
    if (answer !== null && (this.heatFound() || this.heatExpanded)) {
      for (const section of loadSettingsSections(this.input.doc, this.language, answer, (key) => this.openHeatEditor(key))) {
        nodes.push(this.readOnlyUnlessAdmin(section, answer));
      }
    }
    return nodes;
  }

  /** The section's rows open their editors only for an administrator on a writable answer. */
  private readOnlyUnlessAdmin(section: HTMLElement, answer: HeatAnswer): HTMLElement {
    if (!answer.writable || !this.input.isAdmin()) {
      section.querySelectorAll<HTMLButtonElement>("button[data-edit]").forEach((button) => {
        button.disabled = true;
      });
    }
    return section;
  }

  /** The "Heating" section (the house's heat): whole once anything of the house's heat is found, else one line. */
  heatNode(): HTMLElement {
    const doc = this.input.doc;
    const language = this.language;
    const answer = this.heat;
    if (answer !== null && (houseHeatFound(answer) || this.heatExpanded)) {
      const section = heatSettingsSection(doc, language, answer, (key) => this.openHeatEditor(key));
      section.dataset["section"] = "heat";
      if (!houseHeatFound(answer)) {
        section.insertBefore(element(doc, "p", C.settingRowHelp, translate(language, "heat.none.help")), section.children[1] ?? null);
      }
      return this.readOnlyUnlessAdmin(section, answer);
    }
    // Nothing heat-related found: one line that opens the section, so a relay-only water heater can be added.
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "heat";
    section.dataset["heatNone"] = "true";
    const title = translate(language, "heat.settings.title");
    const value = translate(language, this.heatFailed ? "heat.none.failed" : "heat.none.value");
    section.append(
      ...settingRow(doc, {
        key: "heat_none",
        label: title,
        value,
        ...(answer !== null && answer.writable && this.input.isAdmin()
          ? {
              onTap: () => {
                this.heatExpanded = true;
                this.input.repaint();
              },
              changeableText: translate(language, "settings.row.changeable", { label: title, value }),
            }
          : {}),
      }),
    );
    return section;
  }
}

// ------------------------------------------------------------------------------------------------ the popover

export interface SettingsPopoverInput {
  doc: Document;
  owner: ShadowRoot | HTMLElement;
  background: () => HTMLElement | null;
  idPrefix: string;
  hass: () => HomeAssistantLike | null;
  target: () => SiteTarget | null;
  language: () => Language;
  /** A write took: the card reads its own view again. */
  onChanged?: (what: "battery" | "heat" | "site") => void;
  /** Move to one of Home Assistant's own pages (tests replace it). */
  navigate?: (path: string) => void;
}

type ViewState = { kind: "loading" } | { kind: "failed" } | { kind: "ready"; view: SiteView };

/**
 * The popover for the house and heat cards: the site's settings, opened at a section (`focus`). Built from one site
 * read (`get_site_settings_view`) and the battery, heat and site entity reads, all on open.
 */
export class SettingsPopover {
  private readonly input: SettingsPopoverInput;
  private dialog: DialogHandle | null = null;
  private state: ViewState = { kind: "loading" };
  private entity: EntityConfig | null = null;
  private focus: PopoverFocus | null = null;
  private scrollTop: number | null = null;
  private generation = 0;
  readonly sections: SiteSections;
  /** One notification section for the whole site, per phone (`notification-phones.ts`). */
  readonly phones: NotificationPhones;

  constructor(input: SettingsPopoverInput) {
    this.input = input;
    this.phones = new NotificationPhones({
      doc: input.doc,
      idPrefix: input.idPrefix,
      hass: input.hass,
      language: input.language,
      openEditor: (title, build) => this.openEditor(title, build),
    });
    this.sections = new SiteSections({
      doc: input.doc,
      idPrefix: input.idPrefix,
      hass: input.hass,
      target: input.target,
      language: input.language,
      isAdmin: () => this.isAdmin(),
      openEditor: (title, build) => this.openEditor(title, build),
      repaint: () => this.paint(),
      onChanged: (what) => input.onChanged?.(what),
    });
  }

  /** The card bundle hash the backend serves (`get_card_info`), `null` when it cannot be read. */
  private async servedCardHash(hass: HomeAssistantLike): Promise<string | null> {
    try {
      return decodeCardInfo(await getCardInfo(hass))?.cardBundleHash ?? null;
    } catch {
      return null;
    }
  }

  private isAdmin(): boolean {
    return this.input.hass()?.user?.is_admin === true;
  }

  private get language(): Language {
    return this.input.language();
  }

  private ensureDialog(): DialogHandle {
    if (this.dialog === null) {
      this.dialog = createDialog({
        owner: this.input.owner,
        idPrefix: `${this.input.idPrefix}-popover`,
        labels: { close: translate(this.language, "dialog.close") },
        background: this.input.background,
        onClose: () => {
          this.scrollTop = null;
          this.leaveEditor = null;
        },
        // In an editor, the cross, Escape or the backdrop go back to the page, as Cancel does.
        onDismiss: () => {
          const back = this.leaveEditor;
          this.leaveEditor = null;
          if (back === null) {
            return false;
          }
          back();
          return true;
        },
      });
      this.dialog.element.classList.add(C.settingsWide);
    }
    return this.dialog;
  }

  isOpen(): boolean {
    return this.dialog?.isOpen() === true;
  }

  destroy(): void {
    this.generation += 1;
    this.dialog?.destroy();
    this.dialog = null;
  }

  private navigate(path: string): void {
    this.dialog?.hide({ restoreFocus: false });
    (this.input.navigate ?? ((to: string) => navigateTo(this.input.doc, to)))(path);
  }

  /** Open at `focus` (the whole page when `null`): drawn at once, then filled as the reads come back. */
  async open(focus: PopoverFocus | null = null): Promise<void> {
    this.focus = focus;
    if (focus === "heat" || focus === "pool" || focus === "spa" || focus === "sauna") {
      this.sections.heatExpanded = true;
    }
    if (!this.isOpen()) {
      this.scrollTop = null;
    }
    this.paint();
    await this.load();
    this.paint();
  }

  /** Read the site, its battery, heat and site entities again. */
  async load(): Promise<void> {
    const hass = this.input.hass();
    const target = this.input.target();
    if (hass === null || target === null) {
      this.state = { kind: "failed" };
      return;
    }
    const generation = ++this.generation;
    let next: ViewState;
    try {
      const decoded = decodeSiteView(await getSiteSettingsView(hass, target));
      next = decoded.ok ? { kind: "ready", view: decoded.value } : { kind: "failed" };
    } catch {
      next = { kind: "failed" };
    }
    if (generation !== this.generation) {
      return;
    }
    this.state = next;
    if (next.kind !== "ready") {
      return;
    }
    await Promise.all([this.sections.load({ battery: next.view.battery.found }), this.loadEntities(next.view.site_id)]);
  }

  private async loadEntities(siteId: string): Promise<void> {
    const hass = this.input.hass();
    if (hass === null || !this.isAdmin()) {
      this.entity = null;
      return;
    }
    try {
      const decoded = decodeEntityAnswer(await getSiteEntityConfig(hass, siteId));
      this.entity = decoded.ok && decoded.value.ok ? decoded.value.config : null;
    } catch {
      this.entity = null;
    }
  }

  private title(): string {
    return translate(this.language, "popover.open");
  }

  private panel(): HTMLElement | null {
    return this.dialog?.element.querySelector<HTMLElement>(`.${C.dialogScroll}`) ?? null;
  }

  /** Draw the page again, keeping its scroll (or scrolling to the focus once). */
  paint(): void {
    const dialog = this.ensureDialog();
    const keep = dialog.isOpen() ? (this.panel()?.scrollTop ?? null) : this.scrollTop;
    dialog.show({ title: this.title(), body: this.body() });
    const panel = this.panel();
    if (this.focus === "house" && this.state.kind === "ready") {
      if (panel !== null) {
        panel.scrollTop = 0;
      }
      this.focus = null;
    } else if (this.focus !== null && this.state.kind === "ready") {
      panel?.querySelector<HTMLElement>(`[data-section="${this.focus}"]`)?.scrollIntoView?.({ block: "start" });
      this.focus = null;
    } else if (keep !== null && panel !== null) {
      panel.scrollTop = keep;
    }
  }

  /** One heat editor on its own (the heat card's away button): Save or Cancel closes the dialog. */
  openHeatAlone(answer: HeatAnswer, key: HeatSettingKey | "away"): void {
    const dialog = this.ensureDialog();
    this.sections.heat = answer;
    const built = heatEditor(
      {
        doc: this.input.doc,
        language: this.language,
        idPrefix: this.input.idPrefix,
        answer,
        write: (changes, options) => this.sections.writeHeatChanges(changes, options),
      },
      key,
      { onDone: () => dialog.hide(), onCancel: () => dialog.hide() },
    );
    if (built === null) {
      return;
    }
    const form = built.body as HTMLFormElement;
    dialog.show({ title: built.title, body: form, actions: takeActions(form, `${this.input.idPrefix}-popover-value`) });
  }

  /** A form of the heat card's own on its own (the spa's times, its idle temperature): Save or Cancel closes the dialog. */
  openFormAlone(title: string, build: (handlers: EditorHandlers) => HTMLFormElement): void {
    const dialog = this.ensureDialog();
    const form = build({ onDone: () => dialog.hide(), onCancel: () => dialog.hide() });
    dialog.show({ title, body: form, actions: takeActions(form, `${this.input.idPrefix}-popover-value`) });
  }

  /** One value's editor in the same dialog; Save or Cancel returns to the page as it was, read again after a save. */
  openEditor(title: string, build: (handlers: EditorHandlers) => HTMLElement): void {
    const dialog = this.ensureDialog();
    this.scrollTop = this.panel()?.scrollTop ?? null;
    const back = (): void => {
      this.leaveEditor = null;
      dialog.show({ title: this.title(), body: this.body() });
      const panel = this.panel();
      if (panel !== null && this.scrollTop !== null) {
        panel.scrollTop = this.scrollTop;
      }
    };
    const form = build({
      onDone: () => {
        back();
        void this.load().then(() => this.paint());
      },
      onCancel: back,
    });
    dialog.show({ title, body: form, actions: takeActions(form, `${this.input.idPrefix}-popover-value`) });
    this.leaveEditor = back;
  }

  /** How the open editor goes back to the page (its Cancel), `null` while the page itself shows. */
  private leaveEditor: (() => void) | null = null;

  private body(): HTMLElement {
    const doc = this.input.doc;
    const language = this.language;
    const body = element(doc, "div");
    body.dataset["popover"] = "site";
    if (this.state.kind === "loading") {
      body.append(element(doc, "p", C.muted, translate(language, "popover.loading")));
      return body;
    }
    if (this.state.kind === "failed") {
      body.append(element(doc, "p", C.muted, translate(language, "popover.failed")));
      return body;
    }
    const view = this.state.view;
    const setup = this.setupNode(view);
    if (setup !== null) {
      body.append(setup);
    }
    body.append(this.priceNode(view), this.siteNode(view));
    const solar = this.solarNode(view);
    if (solar !== null) {
      body.append(solar);
    }
    const battery = this.sections.batteryNode();
    if (battery !== null) {
      body.append(battery);
    }
    body.append(...this.sections.heatNodes(), this.phones.node(), this.supportNode());
    return body;
  }

  private setupNode(view: SiteView): HTMLElement | null {
    const block: SetupBlock = {
      items: view.setup.items.map((item) => ({
        id: item.id,
        dismissible: item.dismissible,
        write: item.field === undefined || item.value === undefined ? null : { field: item.field, value: item.value },
      })),
    };
    const rows = setupRowsOf(block);
    if (rows.length === 0 || !view.writable) {
      return null;
    }
    const doc = this.input.doc;
    return setupSettingsSection(doc, {
      language: this.language,
      rows,
      openPlan: () => undefined,
      openSetting: (section, edit) => this.openSettingAt(section, edit),
      forecastChoosable: (view.site?.solar_forecast.choices.length ?? 0) > 0,
      navigate: (path) => this.navigate(path),
      openExternal: (url) => doc.defaultView?.open(url, "_blank", "noopener"),
      dismiss: (id: SetupRowId) => void this.dismiss(id),
      writeSiteOption: (field, value) => void this.writeSiteOption(view.site_id, field, value),
    });
  }

  /** Scroll to a section and open the editor of one of its rows. */
  private openSettingAt(section: string, edit: string): void {
    if (section === "heat") {
      this.sections.heatExpanded = true;
      this.paint();
    }
    const panel = this.panel();
    panel?.querySelector<HTMLElement>(`[data-section="${section}"]`)?.scrollIntoView?.({ block: "start" });
    const row = Array.from(panel?.querySelectorAll<HTMLElement>("[data-edit]") ?? []).find((node) => node.dataset["edit"] === edit);
    row?.click();
  }

  /** A grid sign row's button: that one site option, then the page read again. */
  private async writeSiteOption(siteId: string, field: string, value: boolean): Promise<void> {
    const hass = this.input.hass();
    if (hass === null) {
      return;
    }
    try {
      await updateSiteEntityConfig(hass, siteId, { scope: "site", expected: { [field]: !value }, changes: { [field]: value } });
    } catch {
      // Not written: the row stays until the next read says otherwise.
    }
    await this.load();
    this.paint();
  }

  private async dismiss(id: SetupRowId): Promise<void> {
    const hass = this.input.hass();
    if (hass === null) {
      return;
    }
    try {
      await dismissSetupItem(hass, id);
    } catch {
      // The row stays; nothing else to say.
    }
    await this.load();
    this.paint();
  }

  /** The host side of the shared price and solar sections: this popover's rows and editors. */
  private sectionHost(): SectionHost {
    const doc = this.input.doc;
    const language = this.language;
    const idPrefix = this.input.idPrefix;
    const admin = this.isAdmin();
    return {
      doc,
      language,
      idPrefix,
      isAdmin: admin,
      row: (key, label, value, onTap) => {
        const [row] = settingRow(doc, {
          key,
          label,
          value,
          ...(admin && onTap !== undefined
            ? { onTap, changeableText: translate(language, "settings.row.changeable", { label, value }) }
            : {}),
        });
        return row!;
      },
      rowHelp: (key, text) => {
        const help = element(doc, "p", C.settingRowHelp, text);
        help.dataset["help"] = key;
        return help;
      },
      openValueEditor: (title, build) => this.openEditor(title, build),
      editNumber: (title, spec, save) =>
        this.openEditor(title, (handlers) =>
          numberEditor(
            doc,
            language,
            {
              ...spec,
              idPrefix,
              rangeMessage: translate(language, "settings.error.range", {
                min: formatNumber(language, spec.min, spec.decimals),
                max: formatNumber(language, spec.max, spec.decimals),
              }),
            },
            save,
            handlers,
          ),
        ),
      editSingle: (title, options, selected, save, intro) =>
        this.openEditor(title, (handlers) =>
          singleEditor(doc, language, { ...(intro === undefined ? {} : { intro }), options, selected, idPrefix }, save, handlers),
        ),
      editMulti: (title, options, checked, save) =>
        this.openEditor(title, (handlers) => multiEditor(doc, language, { options, checked }, save, handlers)),
    };
  }

  /**
   * The price: the charger card's own section (`price-section.ts`) over the site's view. Without an electricity
   * price, one row that adds one.
   */
  private priceNode(view: SiteView): HTMLElement {
    const settings = view.price_settings;
    if (settings === null) {
      return this.noPriceNode(view);
    }
    const doc = this.input.doc;
    const market = settings.market;
    const format: FormatContext = {
      language: this.language,
      timeZone: market.timezone ?? "",
      unit: market.minor_unit ?? "",
      currency: market.currency,
      majorUnit: market.major_unit,
      countries: market.countries ?? [],
    };
    const section = priceSectionBody({
      ...this.sectionHost(),
      facts: {
        format,
        priceEntry: priceEntryFacts(settings.price_source, view.charger_ids.length),
        gridTariff: settings.grid_tariff,
        fiscal: settings.fiscal,
        provider: settings.provider,
        areaId: market.area_id,
        areaName: market.area_name,
        onSite: true,
      },
      write: (write) => this.writePrice(view.site_id, settings, write),
      openArea: () => void this.openArea(view.site_id, settings),
      editFiscal: (component) => void this.editFiscal(view.site_id, settings, component),
      hasComponent: (domain) => {
        const components = (this.input.hass() as { config?: { components?: unknown } } | null)?.config?.components;
        return Array.isArray(components) && components.includes(domain);
      },
      navigate: (path) => this.navigate(path),
      openExternal: (url) => {
        doc.defaultView?.open(url, "_blank", "noopener,noreferrer");
      },
    });
    section.dataset["section"] = "price";
    return section;
  }

  private noPriceNode(view: SiteView): HTMLElement {
    const doc = this.input.doc;
    const language = this.language;
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "price";
    section.append(sectionHeading(doc, "price", translate(language, "settings.heading.price")));
    const label = translate(language, "popover.price.area");
    const value = translate(language, "popover.price.none");
    section.append(
      ...settingRow(doc, {
        key: "price_area",
        label,
        value,
        help: translate(language, "popover.price.noneHelp"),
        ...(view.writable
          ? {
              onTap: () => this.navigate(addIntegrationPath("spotnav")),
              changeableText: translate(language, "settings.row.changeable", { label, value }),
            }
          : {}),
      }),
    );
    return section;
  }

  /** One write of the price section, by the site; `null` when it took, else the sentence. */
  private async writePrice(siteId: string, settings: SitePriceSettings, write: PriceWrite): Promise<string | null> {
    const hass = this.input.hass();
    const language = this.language;
    if (hass === null) {
      return translate(language, "settings.error.generic");
    }
    try {
      switch (write.kind) {
        case "priceSource":
          await chooseSitePriceSource(hass, siteId, write.priceEntryId);
          break;
        case "entsoeKey": {
          const answer = await setEntsoeKey(hass, write.key, write.areaId);
          // A key the platform could not be asked about is kept, and proved by the first day it is asked for.
          if (!answer.ok && answer.error !== "entsoe_unreachable") {
            return translate(language, "price.key.rejected");
          }
          break;
        }
        case "priceDuplicate":
          await answerPriceDuplicate(hass, write.areaId, write.keepEntryId);
          // Answered on the page itself (no editor to close): read the page again.
          void this.load().then(() => this.paint());
          break;
        case "gridTariff": {
          const block = settings.grid_tariff;
          if (!block.writable || gridTariffIncluded(block)) {
            return translate(language, "settings.error.generic");
          }
          const body = write.change(block);
          if (!sameBody(body, storedBody(block))) {
            await updateSiteGridTariff(hass, siteId, block.write_revision, body);
          }
          break;
        }
      }
      this.input.onChanged?.("site");
      return null;
    } catch (error) {
      return this.priceFailure(error);
    }
  }

  /** A refused price write in words; a conflict reads the page again so the next Save builds on what is stored. */
  private priceFailure(error: unknown): string {
    const code = error instanceof SpotnavApiError ? error.code : null;
    if (code === GRID_TARIFF_CONFLICT) {
      void this.load();
      return translate(this.language, "gridFee.error.conflict");
    }
    return translate(this.language, settingsErrorKey(code));
  }

  /** The price entry's own row as the area editor reads a record. */
  private static priceRecord(settings: SitePriceSettings): PriceRecord {
    return { area_id: settings.fees.area_id, overrides: [settings.fees] };
  }

  /** Write one area's row (`spotnav/update_price`), compare-and-set on the price's write counter. */
  private async writeRow(
    siteId: string,
    settings: SitePriceSettings,
    values: MarketFormValues,
    options: MarketOptionsV1,
  ): Promise<string | null> {
    const hass = this.input.hass();
    if (hass === null) {
      return translate(this.language, "settings.error.generic");
    }
    const stated = marketRow(SettingsPopover.priceRecord(settings), values, options);
    if (!stated.ok) {
      return translate(this.language, stated.errorKey);
    }
    if (!stated.changed) {
      return null;
    }
    try {
      await updateSitePrice(hass, siteId, settings.grid_tariff.write_revision, stated.row);
      this.input.onChanged?.("site");
      return null;
    } catch (error) {
      return this.priceFailure(error);
    }
  }

  private async marketOptions(siteId: string): Promise<MarketOptionsV1 | null> {
    const hass = this.input.hass();
    if (hass === null) {
      return null;
    }
    try {
      const decoded = decodeMarketOptions(await getSiteMarketOptions(hass, siteId));
      return decoded.ok ? decoded.value : null;
    } catch {
      return null;
    }
  }

  /** A fee's own editor, as the charger card's: it needs the area's suggestion, read with the area list. */
  private async editFiscal(siteId: string, settings: SitePriceSettings, component: FiscalComponentName): Promise<void> {
    if (!this.isAdmin()) {
      return;
    }
    const options = await this.marketOptions(siteId);
    if (options === null) {
      this.openReadFailure(translate(this.language, "market.title"));
      return;
    }
    const record = SettingsPopover.priceRecord(settings);
    const stored = settings.fees[component];
    const suggestion = marketSuggestion(options, record.area_id, component);
    const language = this.language;
    const label = translate(
      language,
      component === "vat" ? "settings.fiscal.vat" : component === "tax" ? "settings.fiscal.tax" : "settings.fiscal.transfer",
    );
    this.sectionHost().editNumber(
      label,
      {
        unit: settings.fiscal?.[component].unit ?? "",
        current: stored.enabled ? (stored.value ?? suggestion) : null,
        min: 0,
        max: component === "vat" ? 100 : 100000,
        decimals: 2,
        noneLabel: translate(language, "settings.value.off"),
        suggestion,
      },
      (value) => this.writeRow(siteId, settings, fiscalValues(record, component, value), options),
    );
  }

  /** The area and its fees in the charger card's market editor, over the price entry's own row. */
  private async openArea(siteId: string, settings: SitePriceSettings): Promise<void> {
    const title = translate(this.language, "market.title");
    if (!this.isAdmin()) {
      return;
    }
    const options = await this.marketOptions(siteId);
    if (options === null) {
      this.openReadFailure(title);
      return;
    }
    const record = SettingsPopover.priceRecord(settings);
    const hass = this.input.hass();
    const country = (hass as { config?: { country?: unknown } } | null)?.config?.country;
    let drafts = {};
    const show = (values: MarketFormValues): void =>
      this.openEditor(title, (handlers) => {
        const built = marketEditorBody(
          this.input.doc,
          this.language,
          { readOnly: !marketEditable(options.state, this.isAdmin()), options, values, conflict: null },
          {
            onSave: (chosen) => {
              const save = built.body.querySelector<HTMLButtonElement>(`.${C.settingsSave}`);
              if (save !== null) {
                save.disabled = true;
              }
              void this.writeRow(siteId, settings, chosen, options).then((sentence) => {
                if (sentence === null) {
                  handlers.onDone();
                  return;
                }
                if (save !== null) {
                  save.disabled = false;
                }
                built.body.querySelector("[data-market-notice]")?.remove();
                const notice = element(this.input.doc, "p", C.settingsNotice, sentence);
                notice.dataset["marketNotice"] = "";
                notice.setAttribute("role", "alert");
                built.body.prepend(notice);
              });
            },
            onReload: () => handlers.onCancel(),
            onReapply: () => handlers.onCancel(),
            onCancel: () => handlers.onCancel(),
            onAreaChange: (areaId, live) => {
              const switched = switchArea(record, drafts, live, areaId);
              drafts = switched.drafts;
              show(switched.values);
            },
            ...(hass === null ? {} : { onFindRegion: (postcode: string) => findRegion(hass, postcode) }),
          },
          `${this.input.idPrefix}-market`,
          typeof country === "string" && country.trim() !== "" ? country : null,
        );
        return built.body;
      });
    show(marketFormFor(record, record.area_id));
  }

  /** The area list could not be read: say so, with the way back. */
  private openReadFailure(title: string): void {
    this.openEditor(title, (handlers) => {
      const editor = shell(this.input.doc, this.language, "market", handlers);
      editor.body.append(element(this.input.doc, "p", C.settingsNotice, translate(this.language, "market.error.read")));
      editor.form.querySelector<HTMLButtonElement>(`.${C.settingsSave}`)?.remove();
      return editor.form;
    });
  }

  /** Solar: the charger card's section (`price-section.ts`) over the site's own block; absent while it is not read. */
  private solarNode(view: SiteView): HTMLElement | null {
    const site = view.site;
    if (site === null) {
      return null;
    }
    const facts = siteFactsFor(site, this.language)!;
    return solarSectionBody({
      ...this.sectionHost(),
      facts: {
        chargerCount: facts.chargerCount,
        appliesToText: facts.appliesToText,
        writable: view.writable && facts.writable,
        choices: facts.solarForecastChoices,
      },
      writeForecast: (forecast) => this.writeForecast(view, forecast),
    });
  }

  private async writeForecast(view: SiteView, forecast: string[]): Promise<string | null> {
    const hass = this.input.hass();
    const site = view.site;
    if (hass === null || site === null) {
      return translate(this.language, "settings.error.generic");
    }
    const request = solarSettingsChange(
      { priority: site.solar_priority, forecast: site.solar_forecast.selected },
      { priority: site.solar_priority, forecast },
    );
    if (request === null) {
      return null;
    }
    try {
      const decoded = decodeSiteSettingsAnswer(await updateSiteSettingsBySite(hass, view.site_id, request));
      if (!decoded.ok) {
        return translate(this.language, decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic");
      }
      if (!decoded.value.ok) {
        return translate(this.language, siteSettingsErrorKey(decoded.value.code));
      }
      this.input.onChanged?.("site");
      return null;
    } catch {
      return translate(this.language, "settings.error.generic");
    }
  }

  private field(name: string): EntityField | undefined {
    return this.entity?.fields.find((entry) => entry.field === name);
  }

  private async writeEntity(draft: EntityDraft): Promise<string | null> {
    const hass = this.input.hass();
    const config = this.entity;
    const language = this.language;
    const view = this.state.kind === "ready" ? this.state.view : null;
    if (hass === null || config === null || view === null) {
      return translate(language, "entity.error.generic");
    }
    const change = entityChange(config, "site", draft);
    if (!change.ok) {
      const first = change.errors[0];
      return translate(language, first === undefined ? entityErrorKey(ENTITY_INVALID_VALUE) : fieldErrorKey(first.code));
    }
    if (!change.changed) {
      return null;
    }
    try {
      const decoded = decodeEntityAnswer(await updateSiteEntityConfig(hass, view.site_id, change.request));
      if (!decoded.ok) {
        return translate(language, "entity.error.generic");
      }
      if (decoded.value.config !== null) {
        this.entity = decoded.value.config;
      }
      if (decoded.value.ok) {
        this.input.onChanged?.("site");
        return null;
      }
      const first = decoded.value.fieldErrors[0];
      return translate(language, first === undefined ? entityErrorKey(decoded.value.code) : fieldErrorKey(first.code));
    } catch {
      return translate(language, "entity.error.generic");
    }
  }

  private numberRow(field: string, label: string, unit: string, offLabel: string | null, help?: string): HTMLElement[] {
    const doc = this.input.doc;
    const language = this.language;
    const found = this.field(field);
    if (found === undefined || found.kind !== "number") {
      return [];
    }
    const current = found.value;
    const shown = current === null ? (offLabel ?? "") : `${formatNumber(language, current, 1)} ${unit}`;
    return settingRow(doc, {
      key: field,
      label,
      value: shown,
      ...(found.writable
        ? {
            onTap: () =>
              this.openEditor(label, (handlers) =>
                numberEditor(
                  doc,
                  language,
                  {
                    ...(help === undefined ? {} : { help }),
                    unit,
                    current,
                    min: found.minimum,
                    max: 1000,
                    decimals: 1,
                    ...(offLabel === null ? {} : { noneLabel: offLabel }),
                    idPrefix: `${this.input.idPrefix}-${field}`,
                    rangeMessage: translate(language, "settings.error.range", {
                      min: formatNumber(language, found.minimum, 1),
                      max: formatNumber(language, 1000, 1),
                    }),
                  },
                  (value) => this.writeEntity({ [field]: value === null ? "" : String(value) } as EntityDraft),
                  handlers,
                ),
              ),
            changeableText: translate(language, "settings.row.changeable", { label, value: shown }),
          }
        : {}),
    });
  }

  private openSiteEntities(): void {
    const config = this.entity;
    if (config === null) {
      return;
    }
    const doc = this.input.doc;
    const language = this.language;
    this.openEditor(translate(language, "entity.editor.site"), (handlers) => {
      const built = entityEditorBody(
        doc,
        language,
        { scope: "site", config, hass: () => this.input.hass(), appliesText: null },
        {
          onSave: (draft) => {
            built.setPending(true);
            void this.writeEntity(draft).then((error) => {
              built.setPending(false);
              if (error === null) {
                handlers.onDone();
              } else {
                built.setNotice(error, null);
              }
            });
          },
          onCancel: handlers.onCancel,
        },
        `${this.input.idPrefix}-site-entities`,
      );
      return built.body as HTMLFormElement;
    });
  }

  private siteNode(view: SiteView): HTMLElement {
    const doc = this.input.doc;
    const language = this.language;
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "site";
    section.append(sectionHeading(doc, "site", translate(language, "settings.heading.site"), view.name));
    const config = this.entity;
    if (config === null || config.site === null) {
      section.append(element(doc, "p", C.muted, translate(language, view.writable ? "entity.loading" : "popover.site.readOnly")));
      section.append(...this.priorityRows(view));
      return section;
    }
    section.append(...this.numberRow("main_fuse_a", translate(language, "entity.field.mainFuse"), "A", null));
    // A site that cannot measure an import target hides the row while none is set (a set one stays, to clear it).
    const target = this.field("import_target_kw");
    if (view.import_target.available || (target?.kind === "number" && target.value !== null)) {
      section.append(
        ...this.numberRow("import_target_kw", translate(language, "site.importTarget"), "kW", translate(language, "settings.value.off"), translate(language, "site.importTarget.help")),
      );
    }
    section.append(...this.priorityRows(view));
    // The same label as the charging card's site row: it is how the site is measured, not a device.
    const label = translate(language, "entity.field.measurementMode");
    const value = modeLabel(language, config.site.measurement.mode);
    section.append(
      ...settingRow(doc, {
        key: "measurement_mode",
        label,
        value,
        onTap: () => this.openSiteEntities(),
        changeableText: translate(language, "settings.row.changeable", { label, value }),
      }),
    );
    return section;
  }

  /**
   * The site's one priority list (`priority-list.ts`): the top row as the value, the whole list in its editor (an
   * administrator's: up and down buttons). Absent from a Home Assistant before it, or with no row at all.
   */
  private priorityRows(view: SiteView): HTMLElement[] {
    const block = view.priority;
    if (block === null || block.order.length === 0) {
      return [];
    }
    const doc = this.input.doc;
    const language = this.language;
    const label = translate(language, "site.priority.title");
    const value = prioritySummary(language, block);
    const writable = block.writable && view.writable && block.order.length > 1;
    return settingRow(doc, {
      key: "priority",
      label,
      value,
      help: translate(language, "site.priority.intro"),
      ...(writable
        ? {
            onTap: () =>
              this.openEditor(label, (handlers) =>
                priorityEditor(doc, language, block, this.input.idPrefix, (order) => this.writePriority(view, order), handlers),
              ),
            changeableText: translate(language, "settings.row.changeable", { label, value }),
          }
        : {}),
    });
  }

  private async writePriority(view: SiteView, order: string[]): Promise<string | null> {
    const hass = this.input.hass();
    const block = view.priority;
    if (hass === null || block === null) {
      return translate(this.language, "settings.error.generic");
    }
    const request = priorityChange(
      block.order.map((row) => row.id),
      order,
    );
    if (request === null) {
      return null;
    }
    try {
      const answer = decodePriorityAnswer(await updateSiteSettingsBySite(hass, view.site_id, request));
      if (!answer.ok) {
        return translate(this.language, answer.code === null ? "settings.error.generic" : siteSettingsErrorKey(answer.code));
      }
      // Shown at once; the card reads the view again.
      const rows = new Map(block.order.map((row) => [row.id, row]));
      const reordered = order.map((id) => rows.get(id) ?? { id, name: null });
      if (this.state.kind === "ready" && this.state.view === view) {
        this.state = { kind: "ready", view: { ...view, priority: { ...block, order: reordered } } };
        this.paint();
      }
      this.input.onChanged?.("site");
      return null;
    } catch {
      return translate(this.language, "settings.error.generic");
    }
  }

  /**
   * Support (`support-section.ts`, the same as on the charger card's Settings page): the card-out-of-date line, the
   * Automations, beta tester and hardware test rows (administrators), the debug download, sending and "Disconnect all
   * phones". "About this card" is the charger card's own (what that charger can do).
   */
  private supportNode(): HTMLElement {
    const doc = this.input.doc;
    const language = this.language;
    const note = cardOutdatedNote(doc, language, this.cardIsOutdated === true);
    this.outdatedNote = note;
    void this.checkCardOutdated();
    const rows: HTMLElement[] = [];
    const actions: SupportAction[] = [];
    const download = supportAction(doc, "download_debug", translate(language, "debug.download"),
      () => void this.downloadDebug(download.button), translate(language, "debug.intro"));
    download.button.dataset["debug"] = "download";
    actions.push(download);
    if (this.isAdmin()) {
      const automations = translate(language, "settings.automations.label");
      const [automationsRow] = settingRow(doc, {
        key: "automations",
        label: automations,
        value: translate(language, "settings.automations.value"),
        onTap: () => this.openAutomations(),
        changeableText: translate(language, "settings.row.changeable", {
          label: automations,
          value: translate(language, "settings.automations.value"),
        }),
      });
      rows.push(automationsRow!);
      const siteId = this.state.kind === "ready" ? this.state.view.site_id : null;
      if (siteId !== null) {
        if (this.betaRows === null || this.betaRowsFor !== siteId) {
          this.betaRows = new BetaRows(this.betaCall(siteId));
          this.betaRowsFor = siteId;
        }
        rows.push(
          ...this.betaRows.rows({
            doc,
            language,
            idPrefix: this.input.idPrefix,
            row: (key, label, value, onTap) =>
              settingRow(doc, {
                key,
                label,
                value,
                onTap,
                changeableText: translate(language, "settings.row.changeable", { label, value }),
              })[0]!,
            open: (title, build) => this.openEditor(title, (handlers) => build(() => handlers.onDone())),
          }),
        );
      }
      const send = supportAction(doc, "send_debug", translate(language, "sendDebug.open"), () => this.openSendDebug());
      send.button.dataset["debug"] = "send";
      const phones = supportAction(doc, "disconnect_phones", translate(language, "phones.disconnect.open"), () => this.openDisconnectPhones());
      phones.button.dataset["disconnectPhones"] = "open";
      actions.push(send, phones);
    }
    return supportSection({ doc, language, note, rows, actions });
  }

  private betaRows: BetaRows | null = null;
  private betaRowsFor: string | null = null;
  private outdatedNote: HTMLElement | null = null;
  /** Whether the card running here is older than the one served: `null` until checked, checked once per page load. */
  private cardIsOutdated: boolean | null = null;
  private cardCheck: Promise<void> | null = null;

  private async checkCardOutdated(): Promise<void> {
    const hass = this.input.hass();
    if (this.cardIsOutdated !== null || this.cardCheck !== null || hass === null) {
      return;
    }
    this.cardCheck = (async () => {
      const outdated = cardOutdated(ownCardBundleHash(), await this.servedCardHash(hass));
      if (outdated !== null) {
        this.cardIsOutdated = outdated;
      }
      if (this.outdatedNote !== null) {
        this.outdatedNote.hidden = this.cardIsOutdated !== true;
      }
    })().finally(() => {
      this.cardCheck = null;
    });
    await this.cardCheck;
  }

  private betaCall(siteId: string): BetaCall {
    return async (request) => {
      const hass = this.input.hass();
      if (hass === null || !this.isAdmin()) {
        throw new Error("not available");
      }
      return await betaCommand(hass, { site_id: siteId }, request);
    };
  }

  /** "Automations": SpotNav's blueprints, each with its import button, and how many automations use each one. */
  private openAutomations(): void {
    const doc = this.input.doc;
    const language = this.language;
    this.openEditor(translate(language, "settings.automations.label"), (handlers) => {
      const list = automationsBody(doc, language, (path) => this.navigate(path));
      const form = doc.createElement("form");
      form.noValidate = true;
      form.dataset["automations"] = "form";
      form.addEventListener("submit", (event) => event.preventDefault());
      const actions = element(doc, "div", C.settingsActions);
      const close = element(doc, "button", `${C.button} ${C.settingsSave}`, translate(language, "dialog.close")) as HTMLButtonElement;
      close.type = "button";
      close.addEventListener("click", () => handlers.onCancel());
      actions.append(close);
      form.append(list.body, actions);
      const hass = this.input.hass();
      if (hass !== null) {
        void getBlueprints(hass)
          .then((raw) => {
            const use = decodeBlueprintUse(raw);
            if (use !== null && form.isConnected) {
              list.showUse(use);
            }
          })
          .catch(() => undefined);
      }
      return form;
    });
  }

  /** "Disconnect all phones" in the popover's dialog: one line, then the confirming button. Administrators only. */
  private openDisconnectPhones(): void {
    this.openEditor(translate(this.language, "phones.disconnect.open"), (handlers) =>
      disconnectPhonesEditor(this.input.doc, this.language, async () => {
        const hass = this.input.hass();
        if (hass === null || !this.isAdmin()) {
          return translate(this.language, "phones.error.notAdmin");
        }
        return await runDisconnectPhones(hass, this.language);
      }, handlers),
    );
  }

  /** "Send debug info" in the popover's dialog; the command is the installation's, so it needs no charger. */
  private openSendDebug(): void {
    this.openEditor(translate(this.language, "sendDebug.title"), (handlers) =>
      sendDebugEditor(this.input.doc, this.language, this.input.idPrefix, async (message, email) => {
        const hass = this.input.hass();
        if (hass === null || !this.isAdmin()) {
          return { error: sendFailureText(this.language, "unauthorized") };
        }
        const outcome = await sendDebugReport(hass, this.language, this.input.doc.defaultView?.navigator?.userAgent ?? null, await this.servedCardHash(hass), message, email);
        if ("ref" in outcome && email !== null) {
          this.betaRows?.rememberEmail(email);
        }
        return outcome;
      }, handlers, this.betaRows?.email ?? null),
    );
  }

  private async downloadDebug(button: HTMLButtonElement): Promise<void> {
    const hass = this.input.hass();
    if (hass === null) {
      return;
    }
    button.disabled = true;
    button.textContent = translate(this.language, "debug.preparing");
    try {
      const answer = decodeDebugAnswer(await getDebugBundle(hass));
      if (answer !== null && answer.ok) {
        // The card running here next to what the backend serves, as the charger card's download adds it.
        const served = servedHashFromBundle(answer.bundle);
        saveDebugBundle(this.input.doc, { ...answer.bundle, client: clientBlock(this.input.doc.defaultView?.navigator?.userAgent ?? null, served) }, new Date());
      }
    } catch {
      // The button comes back; the bundle is asked for again on the next press.
    } finally {
      button.disabled = false;
      button.textContent = "";
    }
  }
}
