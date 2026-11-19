// The shared settings popover (SpotNav 3.0): one site settings surface reachable from every card. The house and heat
// cards open it with `SettingsPopover` (`{siteId | chargerId, focus}`); the charger card keeps its own Settings page
// and mounts the same battery and heat sections in it (`SiteSections`). Sections are built from what exists, never
// from who opened it: Get started (the site's own rows), price, site, solar, home battery (when one is found), heat
// (always a row; one line "Heat · nothing found" until something is, which opens the section) and support. The price
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
  getDebugBundle,
  getHeat,
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
  type SiteTarget,
} from "./api";
import { clientBlock, decodeCardInfo, servedHashFromBundle } from "./card-identity";
import { decodeDebugAnswer, saveDebugBundle } from "./debug-download";
import { sendDebugEditor, sendDebugReport, sendFailureText } from "./send-debug";
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
import { formatNumber, type FormatContext } from "./format";
import { GRID_TARIFF_CONFLICT, gridTariffIncluded, sameBody, storedBody } from "./grid-tariff";
import { heatEditor, writeHeat } from "./heat-editors";
import { decodeHeat, type HeatAnswer, type HeatConfig } from "./heat-model";
import { heatSettingsSection, type HeatSettingKey } from "./heat-view";
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
import { setupSection, type SetupRowId, setupRowsOf } from "./setup-checklist";
import {
  decodeBatteryControl,
  decodeSiteList,
  decodeSiteView,
  reportOutcomes,
  type BatteryStatus,
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
  sectionHeading,
  settingRow,
  shell,
  singleEditor,
  type EditorHandlers,
} from "./value-editors";
import type { FiscalComponentName } from "./value-writes";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** Where the popover opens: a section, or `house` for the top of the page (the house card's gear). */
export type PopoverFocus = "house" | "setup" | "price" | "site" | "solar" | "battery" | "heat" | "support";

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

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
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
    // The plant's limit is a total over the phases: said with its value, and why that is less than the fuse.
    const value = cap.import_limit?.value_kw ?? null;
    return value === null
      ? translate(language, "battery.cap.importLimit")
      : translate(language, "battery.cap.importLimitTotal", { kw: formatNumber(language, value, 1) });
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

/** Under the cap's line, once the person said the plant's own import limit acts: why it does not guard the fuse as
 * it reads now, and the most it may read to do so (it limits the total over the three phases, so that is less than
 * the fuse). `null` when the limit guards, was not said to act, or no value can guard. SpotNav never writes it. */
export function importLimitHint(language: Language, status: BatteryStatus): string | null {
  const limit = status.grid_charge_cap.import_limit;
  if (
    limit === null || limit.holds || limit.suggested_kw === null || limit.why !== null
    || !status.options.battery_control_import_limit_acts || !status.allowed_actions.includes("grid_charge")
  ) {
    return null;
  }
  const max = formatNumber(language, limit.suggested_kw, 1);
  return limit.value_kw === null
    ? translate(language, "battery.cap.importLimitUnset", { max })
    : translate(language, "battery.cap.importLimitTooHigh", { value: formatNumber(language, limit.value_kw, 1), max });
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
    case "self_managed":
      return translate(language, "battery.need.selfManaged");
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

  /** Whether anything heat-related was found: a heat pump, a meter, climate zones, or settings already made. */
  heatFound(): boolean {
    const answer = this.heat;
    if (answer === null) {
      return false;
    }
    return (
      answer.pump !== null ||
      answer.meter?.entity_id != null ||
      answer.zones.length > 0 ||
      (answer.choices?.zones.length ?? 0) > 0 ||
      answer.config.control_path !== "none" ||
      answer.config.indoor_sensors.length > 0 ||
      answer.config.heat_meter_entity !== null
    );
  }

  // ---- battery

  async writeBattery(changes: Record<string, boolean | string>, enableControlEntities = false): Promise<string | null> {
    const hass = this.input.hass();
    const target = this.input.target();
    const language = this.language;
    if (hass === null || target === null) {
      return translate(language, "battery.error.generic");
    }
    try {
      const decoded = decodeBatteryControl(
        await batteryControl(hass, target, Object.keys(changes).length === 0 ? null : changes, enableControlEntities),
      );
      if (!decoded.ok) {
        return translate(language, "battery.error.generic");
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

  /** The battery mode: one of three, each with its help line; Choose writes `battery_mode`. */
  private openBatteryMode(title: string, current: string): void {
    const { doc, idPrefix } = this.input;
    const language = this.language;
    this.input.openEditor(title, (handlers) =>
      singleEditor(
        doc,
        language,
        {
          options: MODES.map((mode) => ({
            value: mode,
            label: translate(language, `house.mode.${mode}` as TranslationKey),
            help: translate(language, `battery.modeHelp.${mode}` as TranslationKey),
          })),
          selected: current,
          idPrefix: `${idPrefix}-battery-mode`,
        },
        async (value) => (value === current ? null : await this.writeBattery({ battery_mode: value })),
        handlers,
      ),
    );
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
      }
      steps.push(item);
    }
    for (const need of plain) {
      const line = element(doc, "p", C.settingRowHelp, batteryNeed(language, need));
      line.dataset["need"] = need.split(":")[0]!;
      wrap.append(line);
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
        help: translate(language, `battery.modeHelp.${mode}` as TranslationKey),
        ...(admin
          ? {
              onTap: () => this.openBatteryMode(modeLabel, mode),
              changeableText: translate(language, "settings.row.changeable", {
                label: modeLabel,
                value: translate(language, `house.mode.${mode}` as TranslationKey),
              }),
            }
          : {}),
      }),
    );
    if (status.state !== "on") {
      // The mode counts even when nothing is controlled: it moves the comparison's marked row.
      const shadow = element(doc, "p", C.settingRowHelp, translate(language, "battery.modeShadow"));
      shadow.dataset["batteryModeShadow"] = "true";
      section.append(shadow);
    }
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
    const phaseLine = perPhaseLine(language, status);
    if (phaseLine !== null) {
      const phases = element(doc, "p", C.settingRowHelp, phaseLine);
      phases.dataset["perPhase"] = "true";
      section.append(phases);
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

  async writeHeatChanges(changes: Partial<Record<keyof HeatConfig, unknown>>): Promise<string | null> {
    const hass = this.input.hass();
    const target = this.input.target();
    const answer = this.heat;
    const language = this.language;
    if (hass === null || target === null || answer === null) {
      return translate(language, "heat.error.generic");
    }
    const result = await writeHeat(hass, target, answer, changes, language);
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
    const probe = heatEditor({ doc, language, idPrefix, answer, write: (changes) => this.writeHeatChanges(changes) }, key, {
      onDone: () => undefined,
      onCancel: () => undefined,
    });
    if (probe === null) {
      return;
    }
    this.input.openEditor(probe.title, (handlers) => {
      built = heatEditor({ doc, language, idPrefix, answer, write: (changes) => this.writeHeatChanges(changes) }, key, handlers);
      return (built?.body ?? element(doc, "form")) as HTMLFormElement;
    });
  }

  heatNode(): HTMLElement {
    const doc = this.input.doc;
    const language = this.language;
    const answer = this.heat;
    if (answer !== null && (this.heatFound() || this.heatExpanded)) {
      const section = heatSettingsSection(doc, language, answer, (key) => this.openHeatEditor(key));
      section.dataset["section"] = "heat";
      if (!this.heatFound()) {
        section.insertBefore(element(doc, "p", C.settingRowHelp, translate(language, "heat.none.help")), section.children[1] ?? null);
      }
      if (!answer.writable || !this.input.isAdmin()) {
        section.querySelectorAll<HTMLButtonElement>("button[data-edit]").forEach((button) => {
          button.disabled = true;
        });
      }
      return section;
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

  constructor(input: SettingsPopoverInput) {
    this.input = input;
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
        },
      });
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
    if (focus === "heat") {
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
        write: (changes) => this.sections.writeHeatChanges(changes),
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

  /** One value's editor in the same dialog; Save or Cancel returns to the page as it was, read again after a save. */
  openEditor(title: string, build: (handlers: EditorHandlers) => HTMLElement): void {
    const dialog = this.ensureDialog();
    this.scrollTop = this.panel()?.scrollTop ?? null;
    const back = (): void => {
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
  }

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
    body.append(this.sections.heatNode(), this.supportNode());
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
    const wrap = element(doc, "section", C.settingsSection);
    wrap.dataset["section"] = "setup";
    wrap.append(sectionHeading(doc, "setup", translate(this.language, "setup.heading")));
    wrap.append(
      setupSection(doc, {
        language: this.language,
        rows,
        openPlan: () => undefined,
        openSetting: (section, edit) => this.openSettingAt(section, edit),
        forecastChoosable: (view.site?.solar_forecast.choices.length ?? 0) > 0,
        navigate: (path) => this.navigate(path),
        openExternal: (url) => doc.defaultView?.open(url, "_blank", "noopener"),
        heading: false,
        dismiss: (id: SetupRowId) => void this.dismiss(id),
        writeSiteOption: (field, value) => void this.writeSiteOption(view.site_id, field, value),
      }),
    );
    return wrap;
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
      return section;
    }
    section.append(
      ...this.numberRow("main_fuse_a", translate(language, "entity.field.mainFuse"), "A", null),
      ...this.numberRow("import_target_kw", translate(language, "site.importTarget"), "kW", translate(language, "settings.value.off"), translate(language, "site.importTarget.help")),
    );
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

  private supportNode(): HTMLElement {
    const doc = this.input.doc;
    const language = this.language;
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "support";
    section.append(sectionHeading(doc, "support", translate(language, "settings.section.support")));
    section.append(element(doc, "p", C.muted, translate(language, "debug.intro")));
    const button = element(doc, "button", `${C.button} ${C.settingsSectionConfigure}`, translate(language, "debug.download")) as HTMLButtonElement;
    button.type = "button";
    button.dataset["debug"] = "download";
    button.addEventListener("click", () => void this.downloadDebug(button));
    const actions = element(doc, "div", C.settingsSupportActions);
    actions.append(button);
    if (this.isAdmin()) {
      const send = element(doc, "button", `${C.button} ${C.settingsSectionConfigure}`, translate(language, "sendDebug.open")) as HTMLButtonElement;
      send.type = "button";
      send.dataset["debug"] = "send";
      send.addEventListener("click", () => this.openSendDebug());
      actions.append(send);
    }
    section.append(actions);
    return section;
  }

  /** "Send debug info" in the popover's dialog; the command is the installation's, so it needs no charger. */
  private openSendDebug(): void {
    this.openEditor(translate(this.language, "sendDebug.title"), (handlers) =>
      sendDebugEditor(this.input.doc, this.language, this.input.idPrefix, async (message, email) => {
        const hass = this.input.hass();
        if (hass === null || !this.isAdmin()) {
          return { error: sendFailureText(this.language, "unauthorized") };
        }
        return await sendDebugReport(hass, this.language, this.input.doc.defaultView?.navigator?.userAgent ?? null, await this.servedCardHash(hass), message, email);
      }, handlers),
    );
  }

  private async downloadDebug(button: HTMLButtonElement): Promise<void> {
    const hass = this.input.hass();
    if (hass === null) {
      return;
    }
    button.disabled = true;
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
    }
  }
}
