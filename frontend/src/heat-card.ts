// The heat card (`custom:spotnav-heat-card`, SpotNav 3.0 preview): a third element in the same bundle, so nothing extra
// is installed. It reads `spotnav/get_heat` for a site, named by the site's id or a charger's in it, every
// `HEAT_REFRESH_MS`, and shows it (`heat-view.ts`). The dial and home/away write the site's heat settings through
// `spotnav/update_heat` (a compare-and-set); an administrator's settings button opens the shared settings popover
// (`settings-popover.ts`) at its heat section. What the card writes is SpotNav's own settings, never a heating device.
// Under the header the card has three tabs (`heat-tabs.ts`): Now (the dial, home or away, the plan's next hours and
// today's figures), Plan (the day's chart, today's or a past day's) and Use (a day, month or year by what is measured,
// in kWh or money), the two last on one period picker, read with `spotnav/get_heat_history`. The tab, the unit and the
// parts hidden are remembered per card in this browser. The "i" beside the gear opens the details
// (`history-dialog.ts`, `heat-history.ts`). In a box of fixed height (a wall screen) the shown tab's picture takes
// the box's spare height.
//
// The Pool, Spa and Sauna tabs (`pool-view.ts`, `spa-view.ts`) follow where the site has one: their dials, the season,
// the extra filtering, the times and "now" go through `spotnav/pool`, `spotnav/spa` and `spotnav/sauna`, any user's.
//
// YAML: `type: custom:spotnav-heat-card` with `site: <site entry id>` or `charger: <charger entry id>`, and
// optionally `tab: now | plan | use | pool | spa | sauna` to show only that tab (no tab row).

import { followTheme } from "./energy-palette";
import {
  deadlineAction,
  getHeat,
  getPeriodHistory,
  getPlan,
  heatBoost,
  laterDeparture,
  poolAction,
  releaseImportTarget,
  SpotnavApiError,
  takeWhatFits,
  UNSUPPORTED_API_VERSION,
} from "./api";
import { decodePlan } from "./house-plan";
import { NOT_READY_STYLES, openedForNotReady, type NotReady, type NotReadyPress } from "./not-ready-sheet";
import { browserStore, type PreferenceStore } from "./chart-preference";
import { sessionsEditor, writeError, writeHeat, type HeatWriteOptions, type SessionsChoice } from "./heat-editors";
import { decodeHeat, decodeHeatBoost, decodeLoadAnswer, type HeatAnswer, type HeatConfig } from "./heat-model";
import { HEAT_CLASSES as X, HEAT_STYLES } from "./heat-styles";
import {
  fitHeatTabs,
  HEAT_TABS,
  isHeatTab,
  readHeatTab,
  readUseHidden,
  readUseUnit,
  writeHeatTab,
  writeUseHidden,
  writeUseUnit,
  type HeatTab,
} from "./heat-tabs";
import { USE_STYLES } from "./heat-use";
import {
  TARGET_MAX,
  TARGET_MIN,
  instantToLocal,
  localToInstant,
  heatBody,
  planChoice,
  type HeatLoadable,
  type HeatState,
  type HeatTabsData,
  type HeatUi,
  type LoadKind,
  type PoolPress,
} from "./heat-view";
import { POOL_TARGET_MAX, POOL_TARGET_MIN, POOL_TARGET_STEP } from "./pool-view";
import { SAUNA_TARGET_MAX, SAUNA_TARGET_MIN, SPA_BATH_MAX, SPA_BATH_MIN } from "./spa-view";
import { PERIOD_VIEW_STYLES } from "./house-period-view";
import { TAB_STYLES, type PeriodChoice } from "./house-tabs";
import { HistoryDialog } from "./history-dialog";
import { decodeHeatHistory, heatHistoryBody, type HeatHistory } from "./heat-history";
import { HOUSE_STYLES } from "./house-styles";
import { PERIOD_STYLES, type HistoryPeriod } from "./period-history";
import { isLanguagePending, loadLanguage, resolveLanguage, translate, type Language, type TranslationKey } from "./i18n";
import { SettingsPopover, stubTarget } from "./settings-popover";
import type { HomeAssistantLike } from "./types";
import { VISUAL_CLASSES, VISUAL_STYLES } from "./visual-styles";

export { instantToLocal, localToInstant, writeError };

export const HEAT_CARD_TYPE = "spotnav-heat-card";
export const HEAT_CARD_ELEMENT = "spotnav-heat-card";
export const HEAT_REFRESH_MS = 60_000;
/** The sentence for a refused press of the Boost button. */
export function boostErrorKey(code: string | null): "heat.boost.rateLimited" | "heat.boost.noHeadroom" | "heat.boost.failed" {
  return code === "spotnav_rate_limited" ? "heat.boost.rateLimited" : code === "spotnav_no_headroom" ? "heat.boost.noHeadroom" : "heat.boost.failed";
}

/** The sentence for a refused press in the pool's, the spa's or the sauna's tab. */
export function loadErrorKey(code: string | null): TranslationKey {
  switch (code) {
    case "spotnav_rate_limited":
      return "heat.load.rateLimited";
    case "spotnav_no_later_bath":
    case "spotnav_no_later_session":
      return "heat.load.noLater";
    case "spotnav_invalid_value":
      return "heat.load.invalid";
    case "spotnav_not_supported":
      return "heat.load.notSupported";
    default:
      return "heat.error.generic";
  }
}

/** Each load dial's range and step (the pool's water, the spa's bath, the sauna's whole degrees). */
const LOAD_DIALS: Record<LoadKind, { min: number; max: number; step: number }> = {
  pool: { min: POOL_TARGET_MIN, max: POOL_TARGET_MAX, step: POOL_TARGET_STEP },
  spa: { min: SPA_BATH_MIN, max: SPA_BATH_MAX, step: 0.5 },
  sauna: { min: SAUNA_TARGET_MIN, max: SAUNA_TARGET_MAX, step: 1 },
};

/** While the Boost button's start waits for its read-back: look again this often, this many times. */
const BOOST_POLL_MS = 5_000;
const BOOST_POLLS = 40;
/** A dial step is written this long after the last tap, so a run of taps is one write. */
export const DIAL_SETTLE_MS = 700;
export const HEAT_FALLBACK_WIDTH = 330;
const CARD_PADDING_PX = 28;
const STUB_TIMEOUT_MS = 3000;
/** The least the shown tab's picture is drawn in a box of fixed height. */
export const HEAT_FILL_MIN_PX = 110;
/** Periods kept read while the card lives (the oldest go first). */
const PERIODS_KEPT = 12;

export interface HeatCardConfig {
  type: string;
  site?: string;
  charger?: string;
  /** Show only this tab, without the tab row (a wall screen). */
  tab?: HeatTab;
}

/** The YAML, checked: `site` or `charger` as a non-empty string (the site wins when both are given). */
export function parseHeatConfig(raw: unknown): HeatCardConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error("Invalid configuration");
  }
  const source = raw as Record<string, unknown>;
  const config: HeatCardConfig = { type: typeof source.type === "string" ? source.type : `custom:${HEAT_CARD_TYPE}` };
  for (const key of ["site", "charger"] as const) {
    const value = source[key];
    if (value === undefined || value === null || value === "") {
      continue;
    }
    if (typeof value !== "string") {
      throw new Error(`${key} must be text`);
    }
    config[key] = value;
  }
  const tab = source.tab;
  if (tab !== undefined && tab !== null && tab !== "" && tab !== "all") {
    if (!isHeatTab(tab)) {
      throw new Error(`tab must be one of ${HEAT_TABS.join(", ")}`);
    }
    config.tab = tab;
  }
  return config;
}

/** The card editor's form (Home Assistant's own, from a schema): the site, and the one tab to show. */
export function heatConfigForm(language: Language): { schema: unknown[]; computeLabel: (field: { name: string }) => string } {
  const options = [{ value: "all", label: translate(language, "house.editor.tabAll") }, ...HEAT_TABS.map((tab) => ({ value: tab, label: translate(language, `heat.tab.${tab}`) }))];
  return {
    schema: [
      { name: "site", selector: { config_entry: { integration: "spotnav" } } },
      { name: "tab", selector: { select: { mode: "dropdown", options } } },
    ],
    computeLabel: (field) => translate(language, field.name === "tab" ? "house.editor.tab" : "house.editor.site"),
  };
}

/** The selector that finds an element of the card again after a redraw (focus kept over it), or `null`. */
function focusSelector(element: Element | null): string | null {
  if (!(element instanceof HTMLElement)) {
    return null;
  }
  for (const key of ["tab", "step", "pick", "pickLabel", "kind", "unit", "bar", "legend", "mode", "action"] as const) {
    const value = element.dataset[key];
    if (value !== undefined) {
      const attribute = key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
      // The card and the panel carry the tab's name too: the tab itself is the button.
      return `${key === "tab" ? "[role='tab']" : ""}[data-${attribute}="${value}"]`;
    }
  }
  return null;
}

/** The pictures a box of fixed height may grow, the first in the panel being the one that does. */
const FILL_TARGETS = ".spotnav-heat-dial, .spotnav-heat-day, .spotnav-house-pbars-plot";

let instances = 0;

export class SpotnavHeatCard extends HTMLElement {
  private readonly root: ShadowRoot;
  private readonly mount: HTMLDivElement;
  private readonly layer: HTMLDivElement;
  private hassObject: HomeAssistantLike | null = null;
  private config: HeatCardConfig | null = null;
  private state: HeatState = { kind: "unconfigured" };
  private ui: HeatUi = { pendingTarget: null, error: null, tab: "now", locked: null, choice: { period: "day", date: null }, fill: null };
  private readonly periods = new Map<string, HeatLoadable<HeatHistory>>();
  /** What will not be ready in time across the site (`get_plan`'s `not_ready`), read while a bath or a session is not
   * ready; `null` otherwise. */
  private notReady: NotReady | null = null;
  private notReadySeq = 0;
  /** The pointer is over the card: a refresh waits for it to leave, so a hover's readout is not swept away. */
  private hovering = false;
  private pendingRender = false;
  private boxHeight = 0;
  private language: Language | null = null;
  private wantedLanguage: Language | null = null;
  private timer: number | null = null;
  private settle: ReturnType<typeof setTimeout> | null = null;
  /** The pool's, the spa's and the sauna's dials' pending writes. */
  private readonly loadSettle = new Map<LoadKind, ReturnType<typeof setTimeout>>();
  /** The boost poll's next look, and how many looks are left (kept over a disconnect, picked up again on connect). */
  private boostTimer: number | null = null;
  private boostLeft = 0;
  /** Counts the reads of the answer; only the newest one's result is used. */
  private refreshSeq = 0;
  private connected = false;
  private generation = 0;
  private width = HEAT_FALLBACK_WIDTH;
  private observer: ResizeObserver | null = null;
  /** The shared settings popover (and the away editor), made on first use. */
  private settingsPopover: SettingsPopover | null = null;
  private historyDialog: HistoryDialog<HeatHistory> | null = null;
  private readonly idPrefix: string;
  /** This browser's storage for the tab, the unit and the hidden parts; a test may hand in its own. */
  store: () => PreferenceStore | null = browserStore;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    instances += 1;
    this.idPrefix = `spotnav-heat-${instances}`;
    const doc = this.ownerDocument;
    const style = doc.createElement("style");
    style.textContent = VISUAL_STYLES + HOUSE_STYLES + HEAT_STYLES + PERIOD_STYLES + TAB_STYLES + PERIOD_VIEW_STYLES + USE_STYLES + NOT_READY_STYLES;
    this.mount = doc.createElement("div");
    this.mount.className = VISUAL_CLASSES.shell;
    this.layer = doc.createElement("div");
    this.layer.className = X.card;
    this.root.append(style, this.mount, this.layer);
    this.mount.addEventListener("pointerenter", (event) => {
      this.hovering = (event as PointerEvent).pointerType === "mouse";
    });
    this.mount.addEventListener("pointerleave", () => {
      this.hovering = false;
      if (this.pendingRender) {
        this.render();
      }
    });
  }

  setConfig(config: unknown): void {
    this.config = parseHeatConfig(config);
    this.generation += 1;
    const locked = this.config.tab ?? null;
    const key = this.targetKey();
    this.ui = {
      pendingTarget: null,
      error: null,
      tab: locked ?? readHeatTab(key, this.store) ?? "now",
      locked,
      choice: { period: "day", date: null },
      planDate: null,
      useUnit: readUseUnit(key, this.store),
      useHidden: readUseHidden(key, this.store),
      bar: null,
      fill: null,
    };
    this.periods.clear();
    this.state = this.target() === null ? { kind: "unconfigured" } : { kind: "loading" };
    this.render();
    this.start();
  }

  set hass(hass: HomeAssistantLike) {
    const first = this.hassObject === null;
    this.hassObject = hass;
    followTheme(this, hass);
    this.followLanguage(resolveLanguage(hass.locale?.language ?? hass.language));
    if (first) {
      this.start();
    }
  }

  get hass(): HomeAssistantLike | null {
    return this.hassObject;
  }

  connectedCallback(): void {
    this.connected = true;
    if (typeof ResizeObserver !== "undefined" && this.observer === null) {
      this.observer = new ResizeObserver(() => this.measure());
      this.observer.observe(this);
    }
    this.measure();
    this.start();
  }

  disconnectedCallback(): void {
    this.connected = false;
    this.generation += 1;
    this.observer?.disconnect();
    this.observer = null;
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
    if (this.settle !== null) {
      clearTimeout(this.settle);
      this.settle = null;
    }
    for (const timer of this.loadSettle.values()) {
      clearTimeout(timer);
    }
    this.loadSettle.clear();
    if (this.boostTimer !== null) {
      window.clearTimeout(this.boostTimer);
      this.boostTimer = null;
    }
    this.settingsPopover?.destroy();
    this.settingsPopover = null;
    this.historyDialog?.destroy();
    this.historyDialog = null;
  }

  getCardSize(): number {
    return 7;
  }

  getGridOptions(): { rows: "auto"; columns: number; min_columns: number } {
    return { rows: "auto", columns: 6, min_columns: 6 };
  }

  /** The card editor: Home Assistant's own form from a schema, the site and the one tab to show. */
  static getConfigForm(): ReturnType<typeof heatConfigForm> {
    return heatConfigForm(resolveLanguage(typeof document === "undefined" ? null : document.documentElement.lang || null));
  }

  /** The card picker's preview: the first charger's site, else the first site, else an unconfigured card. */
  static async getStubConfig(hass?: HomeAssistantLike): Promise<HeatCardConfig> {
    const stub: HeatCardConfig = { type: `custom:${HEAT_CARD_TYPE}` };
    return { ...stub, ...(hass === undefined ? {} : await stubTarget(hass, STUB_TIMEOUT_MS)) };
  }

  private target(): { site: string } | { charger: string } | null {
    const config = this.config;
    if (config?.site !== undefined) {
      return { site: config.site };
    }
    return config?.charger !== undefined ? { charger: config.charger } : null;
  }

  /** The card's own name for remembering its tab: the site or charger it shows. */
  private targetKey(): string {
    return this.config?.site ?? this.config?.charger ?? "";
  }

  private cardLanguage(): Language {
    return this.language ?? resolveLanguage(null);
  }

  private answer(): HeatAnswer | null {
    return this.state.kind === "ready" ? this.state.answer : null;
  }

  private followLanguage(next: Language): void {
    if (this.wantedLanguage === next) {
      return;
    }
    this.wantedLanguage = next;
    if (!isLanguagePending(next)) {
      this.language = next;
      this.render();
      this.historyDialog?.repaint();
      return;
    }
    void loadLanguage(next).then(() => {
      if (this.wantedLanguage === next) {
        this.language = next;
        this.render();
        this.historyDialog?.repaint();
      }
    });
  }

  private measure(): void {
    const measured = this.getBoundingClientRect().width - CARD_PADDING_PX;
    const width = measured > 0 ? measured : HEAT_FALLBACK_WIDTH;
    const box = this.offsetHeight;
    if (Math.abs(width - this.width) >= 4) {
      this.width = width;
      this.render();
      this.historyDialog?.repaint();
    } else if (Math.abs(box - this.boxHeight) >= 2) {
      this.render();
    }
  }

  private start(): void {
    if (!this.connected || this.hassObject === null || this.target() === null) {
      return;
    }
    if (this.timer === null) {
      this.timer = window.setInterval(() => void this.refresh(), HEAT_REFRESH_MS);
    }
    void this.refresh();
    if (this.boostLeft > 0 && this.boostTimer === null) {
      this.pollBoost(this.boostLeft);
    }
  }

  /** Read the answer once; an older read that comes back after a newer one, or after a newer config, is dropped. */
  async refresh(): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    if (hass === null || target === null) {
      return;
    }
    const generation = this.generation;
    const seq = ++this.refreshSeq;
    let next: HeatState;
    try {
      const decoded = decodeHeat(await getHeat(hass, target));
      next = decoded.ok
        ? { kind: "ready", answer: decoded.value }
        : { kind: "failed", sentenceKey: decoded.failure === "unsupported" ? "heat.unsupported" : "heat.failed", code: null };
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      const unsupported = code === UNSUPPORTED_API_VERSION || code === "unknown_command";
      next = { kind: "failed", sentenceKey: unsupported ? "heat.unsupported" : "heat.failed", code };
    }
    if (generation !== this.generation || seq !== this.refreshSeq) {
      return;
    }
    if (next.kind === "failed" && this.state.kind === "ready") {
      return;
    }
    const first = this.state.kind !== "ready";
    this.state = next;
    if (first && next.kind === "ready" && this.ui.locked == null && openedForNotReady()) {
      // A tap on the phones' "not ready in time" notice: the tab of the bath or the session not ready, with the sheet.
      const kind = next.answer.spa?.not_ready ? "spa" : next.answer.sauna?.not_ready ? "sauna" : null;
      if (kind !== null) {
        this.ui = { ...this.ui, tab: kind };
      }
    }
    if (first) {
      this.render();
    } else {
      this.renderSoon();
    }
    this.ensureData();
    void this.loadNotReady();
    // The running periods are read again with the answer, the old ones shown until the new are in; past ones stay.
    if (!first) {
      for (const key of [...this.periods.keys()].filter((key) => key.endsWith(":now"))) {
        const [period] = key.split(":") as [HistoryPeriod];
        void this.loadPeriod({ period, date: null }, true);
      }
    }
  }

  // -------------------------------------------------------------------------------------------- not ready in time

  /** Read what will not be ready in time across the site while the spa or the sauna says one of theirs will not be. */
  private async loadNotReady(): Promise<void> {
    const answer = this.answer();
    const hass = this.hassObject;
    const target = this.target();
    const seq = ++this.notReadySeq;
    if (answer === null || hass === null || target === null || (answer.spa?.not_ready ?? answer.sauna?.not_ready ?? null) === null) {
      if (this.notReady !== null) {
        this.notReady = null;
        this.renderSoon();
      }
      return;
    }
    let found: NotReady | null = null;
    try {
      const decoded = decodePlan(await getPlan(hass, target));
      found = decoded.ok ? decoded.value.not_ready : null;
    } catch {
      found = null;
    }
    if (seq !== this.notReadySeq) {
      return;
    }
    this.notReady = found;
    this.renderSoon();
  }

  /** A choice on the sheet: a car's to its charger, a bath's or a session's to the spa or the sauna. */
  private async notReadyChoice(press: NotReadyPress): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    if (hass === null || target === null) {
      return;
    }
    for (const load of press.loads) {
      try {
        if (load.kind === "car") {
          if (press.code === "accept_peak") {
            await releaseImportTarget(hass, load.load_id);
          } else if (press.code === "takes_what_fits") {
            await takeWhatFits(hass, load.load_id);
          } else {
            await laterDeparture(hass, load.load_id);
          }
        } else {
          const action = press.code === "later" ? (load.kind === "spa" ? "later_bath" : "later_session") : press.code;
          await deadlineAction(hass, target, load.kind, action);
        }
      } catch {
        // Not written: the sheet stays until the next read says otherwise.
      }
    }
    await this.refresh();
  }

  // -------------------------------------------------------------------------------------------- the tabs' data

  private shownTab(): HeatTab {
    return this.ui.locked ?? this.ui.tab ?? "now";
  }

  private periodKey(choice: PeriodChoice): string {
    return `${choice.period}:${choice.date ?? "now"}`;
  }

  /** The period the shown tab reads besides the card's answer: Use's period, Plan's past day; `null` for none. */
  needs(): PeriodChoice | null {
    const tab = this.shownTab();
    if (tab === "use") {
      return this.ui.choice ?? { period: "day", date: null };
    }
    if (tab === "plan") {
      const choice = planChoice(this.ui);
      return choice.date === null ? null : choice;
    }
    return null;
  }

  private tabsData(): HeatTabsData | undefined {
    if (this.state.kind !== "ready") {
      return undefined;
    }
    const answer = this.state.answer;
    const needs = this.needs();
    return {
      history: needs === null ? null : (this.periods.get(this.periodKey(needs)) ?? { kind: "loading" }),
      today: instantToLocal(Date.parse(answer.now), answer.time_zone).date,
    };
  }

  /** Read what the shown tab needs and has not got. */
  private ensureData(): void {
    if (!this.connected || this.state.kind !== "ready") {
      return;
    }
    const needs = this.needs();
    if (needs !== null) {
      void this.loadPeriod(needs);
    }
  }

  /** Read a period once (`quiet`: again, the one shown kept until the new one is in). */
  private async loadPeriod(choice: PeriodChoice, quiet = false): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    const key = this.periodKey(choice);
    if (hass === null || target === null || (!quiet && this.periods.has(key))) {
      return;
    }
    if (!quiet) {
      this.periods.set(key, { kind: "loading" });
    }
    const generation = this.generation;
    let next: HeatLoadable<HeatHistory>;
    try {
      const decoded = decodeHeatHistory(await getPeriodHistory(hass, "heat", target, choice.period, choice.date));
      next = decoded.ok ? { kind: "ready", answer: decoded.value } : { kind: "failed", code: decoded.failure === "unsupported" ? UNSUPPORTED_API_VERSION : null };
    } catch (error) {
      next = { kind: "failed", code: error instanceof SpotnavApiError ? error.code : null };
    }
    if (generation !== this.generation || (quiet && next.kind === "failed")) {
      return;
    }
    this.periods.set(key, next);
    while (this.periods.size > PERIODS_KEPT) {
      this.periods.delete(this.periods.keys().next().value as string);
    }
    // A failed read is tried again with the next refresh or choice.
    if (next.kind === "failed") {
      window.setTimeout(() => {
        if (this.periods.get(key) === next) {
          this.periods.delete(key);
        }
      }, 0);
    }
    if (quiet) {
      this.renderSoon();
    } else {
      this.render();
    }
  }

  /** Draw again, unless the pointer is over the card: then once it leaves (a refresh, not a choice of the reader's). */
  private renderSoon(): void {
    if (this.hovering) {
      this.pendingRender = true;
      return;
    }
    this.render();
  }

  /** Write `changes` with what was last seen for the same keys; `null` when it took, else the sentence. */
  async write(changes: Partial<Record<keyof HeatConfig, unknown>>, options?: HeatWriteOptions): Promise<string | null> {
    const hass = this.hassObject;
    const target = this.target();
    const answer = this.answer();
    const language = this.cardLanguage();
    if (hass === null || target === null || answer === null) {
      return translate(language, "heat.error.generic");
    }
    const result = await writeHeat(hass, target, answer, changes, language, options);
    if (result.config !== null && this.state.kind === "ready") {
      // The stored settings at once; the rest of the view follows with the read.
      this.state = { kind: "ready", answer: { ...this.state.answer, config: result.config } };
    }
    await this.refresh();
    return result.error;
  }

  /** The Boost button pressed: the start or stop sent, the answer's state shown, and while it starts (read back,
   * up to a few minutes through a cloud) the card looks again every `BOOST_POLL_MS`. */
  private async boost(action: "start" | "stop"): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    const language = this.cardLanguage();
    if (hass === null || target === null || this.ui.boostBusy === true) {
      return;
    }
    this.ui = { ...this.ui, boostBusy: true, boostError: null };
    this.render();
    let error: string | null = null;
    try {
      const decoded = decodeHeatBoost(await heatBoost(hass, target, action));
      if (!decoded.ok) {
        error = translate(language, "heat.error.generic");
      } else if (!decoded.value.ok) {
        error = translate(language, boostErrorKey(decoded.value.error));
      }
    } catch {
      error = translate(language, "heat.boost.failed");
    }
    this.ui = { ...this.ui, boostBusy: false, boostError: error };
    await this.refresh();
    this.pollBoost(BOOST_POLLS);
  }

  private pollBoost(left: number): void {
    if (this.boostTimer !== null) {
      window.clearTimeout(this.boostTimer);
      this.boostTimer = null;
    }
    this.boostLeft = left;
    if (left <= 0 || !this.connected || this.answer()?.hot_water?.boost?.state !== "starting") {
      this.boostLeft = 0;
      return;
    }
    this.boostTimer = window.setTimeout(() => {
      this.boostTimer = null;
      void this.refresh().then(() => {
        if (this.connected) {
          this.pollBoost(left - 1);
        }
      });
    }, BOOST_POLL_MS);
  }

  private step(delta: number): void {
    const answer = this.answer();
    if (answer === null) {
      return;
    }
    const base = this.ui.pendingTarget ?? answer.config.target_c;
    const next = Math.min(TARGET_MAX, Math.max(TARGET_MIN, Math.round((base + delta) * 2) / 2));
    this.ui = { ...this.ui, pendingTarget: next, error: null };
    this.render();
    if (this.settle !== null) {
      clearTimeout(this.settle);
    }
    this.settle = setTimeout(() => {
      this.settle = null;
      const wanted = this.ui.pendingTarget;
      if (wanted === null || wanted === answer.config.target_c) {
        this.ui = { ...this.ui, pendingTarget: null, error: null };
        this.render();
        return;
      }
      void this.write({ target_c: wanted }).then((error) => {
        this.ui = { ...this.ui, pendingTarget: null, error };
        this.render();
      });
    }, DIAL_SETTLE_MS);
  }

  /** A load's stored target, the dial's figure without a pending change. */
  private loadTarget(kind: LoadKind): number | null {
    const answer = this.answer();
    if (answer === null) {
      return null;
    }
    return kind === "pool" ? (answer.pool?.target_c ?? null) : kind === "spa" ? (answer.spa?.bath_c ?? null) : (answer.sauna?.target_c ?? null);
  }

  /** A load's dial stepped: shown at once, written `DIAL_SETTLE_MS` after the last tap. */
  private stepLoad(kind: LoadKind, delta: number): void {
    const stored = this.loadTarget(kind);
    if (stored === null) {
      return;
    }
    const range = LOAD_DIALS[kind];
    const base = this.ui.pendingLoad?.[kind] ?? stored;
    const next = Math.min(range.max, Math.max(range.min, Math.round((base + delta) / range.step) * range.step));
    this.ui = { ...this.ui, pendingLoad: { ...this.ui.pendingLoad, [kind]: next }, loadError: { ...this.ui.loadError, [kind]: null } };
    this.render();
    const old = this.loadSettle.get(kind);
    if (old !== undefined) {
      clearTimeout(old);
    }
    this.loadSettle.set(
      kind,
      setTimeout(() => {
        this.loadSettle.delete(kind);
        const wanted = this.ui.pendingLoad?.[kind];
        const clear = (): void => {
          const pending = { ...this.ui.pendingLoad };
          delete pending[kind];
          this.ui = { ...this.ui, pendingLoad: pending };
        };
        if (wanted === undefined || wanted === this.loadTarget(kind)) {
          clear();
          this.render();
          return;
        }
        const press = kind === "pool"
          ? this.loadPress("pool", (hass, target) => poolAction(hass, target, "target", { target_c: wanted }))
          : this.loadPress(kind, (hass, target) => deadlineAction(hass, target, kind, kind === "spa" ? "bath_c" : "target_c", wanted));
        void press.then(() => {
          clear();
          this.render();
        });
      }, DIAL_SETTLE_MS),
    );
  }

  /** Send a pool, spa or sauna press; the refusal's sentence is shown in its tab. Returns that sentence, or `null`. */
  private async loadPress(
    kind: LoadKind,
    send: (hass: HomeAssistantLike, target: { site: string } | { charger: string }) => Promise<unknown>,
  ): Promise<string | null> {
    const hass = this.hassObject;
    const target = this.target();
    const language = this.cardLanguage();
    if (hass === null || target === null) {
      return translate(language, "heat.error.generic");
    }
    this.ui = { ...this.ui, loadBusy: kind, loadError: { ...this.ui.loadError, [kind]: null } };
    this.render();
    let error: string | null = null;
    try {
      const decoded = decodeLoadAnswer(kind, await send(hass, target));
      if (!decoded.ok) {
        error = translate(language, "heat.error.generic");
      } else if (!decoded.value.ok) {
        error = translate(language, loadErrorKey(decoded.value.error));
      }
    } catch {
      error = translate(language, "heat.error.generic");
    }
    this.ui = { ...this.ui, loadBusy: null, loadError: { ...this.ui.loadError, [kind]: error } };
    await this.refresh();
    this.render();
    return error;
  }

  private pressPool(press: PoolPress): void {
    void this.loadPress("pool", (hass, target) =>
      press.action === "season"
        ? poolAction(hass, target, "season", { open: press.open })
        : press.action === "extra_filtering"
          ? poolAction(hass, target, "extra_filtering", { hours: press.hours })
          : poolAction(hass, target, "cancel_extra"),
    );
  }

  /** The spa's or the sauna's times in an editor of their own (any user's; the spa's idle temperature is an
   * administrator's, in the settings). */
  editLoad(kind: "spa" | "sauna", _what: "times"): void {
    const answer = this.answer();
    const load = kind === "spa" ? answer?.spa : answer?.sauna;
    if (answer === null || load === null || load === undefined) {
      return;
    }
    const doc = this.ownerDocument;
    const language = this.cardLanguage();
    const send = (action: string, value: unknown): Promise<string | null> =>
      this.loadPress(kind, (hass, target) => deadlineAction(hass, target, kind, action, value));
    const current: SessionsChoice = answer.spa !== null && kind === "spa"
      ? { weekly: answer.spa.baths, once: answer.spa.bath_once }
      : { weekly: answer.sauna?.sessions ?? [], once: answer.sauna?.session_once ?? null };
    this.ensurePopover().openFormAlone(translate(language, `heat.${kind}.times` as TranslationKey), (handlers) =>
      sessionsEditor(doc, language, answer, kind, current, async (choice) => {
        const weeklyKey = kind === "spa" ? "baths" : "sessions";
        const onceKey = kind === "spa" ? "bath_once" : "session_once";
        if (JSON.stringify(choice.weekly) !== JSON.stringify(current.weekly)) {
          const error = await send(weeklyKey, choice.weekly);
          if (error !== null) {
            return error;
          }
        }
        return choice.once === current.once ? null : await send(onceKey, choice.once);
      }, handlers),
    );
  }

  private ensurePopover(): SettingsPopover {
    if (this.settingsPopover === null) {
      this.settingsPopover = new SettingsPopover({
        doc: this.ownerDocument,
        owner: this.layer,
        background: () => this.mount,
        idPrefix: this.idPrefix,
        hass: () => this.hassObject,
        target: () => this.target(),
        language: () => this.cardLanguage(),
        onChanged: (what) => {
          if (what === "heat") {
            void this.refresh();
          }
        },
      });
    }
    return this.settingsPopover;
  }

  /** The history dialog: a day, month or year of the house's heating (any user who sees the card). */
  showHistory(opener: HTMLElement | null = null): void {
    if (this.target() === null) {
      return;
    }
    if (this.historyDialog === null) {
      this.historyDialog = new HistoryDialog<HeatHistory>({
        kind: "heat",
        doc: this.ownerDocument,
        owner: this.layer,
        background: () => this.mount,
        idPrefix: this.idPrefix,
        hass: () => this.hassObject,
        target: () => this.target(),
        language: () => this.cardLanguage(),
        width: () => this.width,
        decode: decodeHeatHistory,
        body: heatHistoryBody,
      });
    }
    this.historyDialog.open(opener);
  }

  /** The shared settings popover, opened at the shown tab's section: the pool's, the spa's or the sauna's, else heat. */
  showSettings(): void {
    const answer = this.answer();
    if (answer === null || !answer.writable) {
      return;
    }
    const tab = this.shownTab();
    void this.ensurePopover().open(tab === "pool" || tab === "spa" || tab === "sauna" ? tab : "heat");
  }

  /** The away editor on its own (the card's away button), in the popover's dialog. */
  showAway(): void {
    const answer = this.answer();
    if (answer === null) {
      return;
    }
    this.ensurePopover().openHeatAlone(answer, "away");
  }

  private render(): void {
    if (this.config === null) {
      return;
    }
    this.pendingRender = false;
    const focused = focusSelector(this.root.activeElement);
    this.paint(null);
    this.fit();
    if (focused !== null && !this.mount.contains(this.root.activeElement)) {
      const again = this.root.querySelector<HTMLElement>(focused);
      if (again !== null && !(again as HTMLButtonElement).disabled) {
        again.focus();
      }
    }
  }

  /**
   * In a box of fixed height (a wall screen: the host is as tall as the box, not as its content), the shown tab's
   * picture takes the room the rest leaves, never below `HEAT_FILL_MIN_PX`. One reading of the natural layout, one
   * plan from it: measuring again gives the same plan, so a resize never loops. In a plain column nothing changes.
   */
  private fit(): void {
    const box = this.offsetHeight;
    this.boxHeight = box;
    const card = this.mount.firstElementChild as HTMLElement | null;
    if (card === null || box <= 0 || this.state.kind !== "ready") {
      return;
    }
    const natural = card.offsetHeight;
    const target = card.querySelector<HTMLElement | SVGElement>(FILL_TARGETS);
    if (target === null || Math.abs(box - natural) < 2) {
      return;
    }
    // A tab draws itself more compactly in a box (fewer words): read it so, at its picture's natural height, then
    // give the picture what is left.
    const basis = Math.round((target as HTMLElement).getBoundingClientRect().height);
    this.paint(basis);
    const compact = this.mount.firstElementChild as HTMLElement | null;
    const picture = compact?.querySelector<HTMLElement | SVGElement>(FILL_TARGETS) ?? null;
    const compactBasis = picture === null ? basis : (picture as HTMLElement).getBoundingClientRect().height;
    const fill = Math.max(HEAT_FILL_MIN_PX, Math.floor(compactBasis + box - (compact?.offsetHeight ?? natural)));
    this.paint(fill);
    const filled = this.mount.firstElementChild as HTMLElement | null;
    if (filled !== null) {
      filled.dataset["box"] = "true";
    }
  }

  private paint(fill: number | null): void {
    const doc = this.ownerDocument;
    const language = this.cardLanguage();
    const tabs = this.tabsData();
    this.mount.replaceChildren(
      heatBody({
        doc,
        language,
        state: this.state,
        ui: { ...this.ui, fill },
        width: this.width,
        nowMs: Date.now(),
        idPrefix: this.idPrefix,
        ...(tabs === undefined ? {} : { tabs }),
        notReady: this.notReady,
        handlers: {
          onNotReady: (press) => void this.notReadyChoice(press),
          onStep: (delta) => this.step(delta),
          onHome: () => {
            if (this.answer()?.config.mode === "home") {
              return;
            }
            void this.write({ mode: "home" }).then((error) => {
              this.ui = { ...this.ui, error };
              this.render();
            });
          },
          onAway: () => this.showAway(),
          onSettings: () => this.showSettings(),
          onHistory: (opener) => this.showHistory(opener),
          // Choosing a time changes the drawn chart in place; kept here for the next redraw.
          onChartSelect: (ms) => {
            this.ui = { ...this.ui, chartSelectedMs: ms };
          },
          onTest: (open) => {
            this.ui = { ...this.ui, testOpen: open };
            this.render();
            (this.root.querySelector("button[data-action='test']") as HTMLElement | null)?.focus();
          },
          onTab: (tab, byKey) => {
            this.ui = { ...this.ui, tab, bar: null, chartSelectedMs: null };
            writeHeatTab(this.targetKey(), tab, this.store);
            this.render();
            if (byKey) {
              (this.root.querySelector(`[role='tab'][data-tab='${tab}']`) as HTMLElement | null)?.focus();
            }
            this.ensureData();
          },
          onPeriod: (choice, plan) => {
            // Plan shows days alone: while the shared period is a month or a year its day is its own, so going back
            // to Use finds the month or year as it was left.
            const shared = this.ui.choice ?? { period: "day", date: null };
            this.ui = plan && shared.period !== "day"
              ? { ...this.ui, planDate: choice.date, chartSelectedMs: null }
              : { ...this.ui, choice, planDate: null, bar: null, chartSelectedMs: null };
            this.render();
            this.ensureData();
          },
          onUnit: (unit) => {
            this.ui = { ...this.ui, useUnit: unit };
            writeUseUnit(this.targetKey(), unit, this.store);
            this.render();
          },
          onUsePart: (hidden) => {
            this.ui = { ...this.ui, useHidden: hidden };
            writeUseHidden(this.targetKey(), hidden, this.store);
            this.render();
          },
          onBar: (key) => {
            this.ui = { ...this.ui, bar: key };
          },
          onBoost: (action) => void this.boost(action),
          onLoadStep: (kind, delta) => this.stepLoad(kind, delta),
          onPool: (press) => this.pressPool(press),
          onDeadline: (kind, action, value) => void this.loadPress(kind, (hass, target) => deadlineAction(hass, target, kind, action, value)),
          onLoadEdit: (kind, what) => this.editLoad(kind, what),
        },
      }),
    );
    // The tab row's wording by the room it really has: names or icons.
    const row = this.mount.querySelector<HTMLElement>("[role='tablist']");
    if (row !== null) {
      row.dataset["fit"] = "";
      const fit = fitHeatTabs(row, language);
      if (fit !== this.ui.tabFit) {
        this.ui = { ...this.ui, tabFit: fit };
      }
    }
  }
}
