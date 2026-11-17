// The house card (`custom:spotnav-house-card`, SpotNav 3.0 preview): a second element in the same bundle, so nothing
// extra is installed. It reads one read-only answer (`spotnav/get_house`) for a site, named by the site's id or a
// charger's in it, every `HOUSE_REFRESH_MS` (the forecasts change once an hour), and shows it (`house-view.ts`).
// An administrator's gear opens the shared settings popover (`settings-popover.ts`): the house card is where a home
// without a charger starts. The charging card stays about the car. Under the day's money the card has four tabs
// (`house-tabs.ts`): Now (the live flows, `spotnav/get_house_now`, read every `LIVE_REFRESH_MS` while it is shown),
// Energy flow, Energy chart and Price and battery (one period picker for the three: a day, month or year, read with
// `spotnav/get_house_history` and, for a past day's hours, `spotnav/get_house` with its date). The tab is remembered
// per card in this browser. The clock beside the gear opens the history (`history-dialog.ts`, `house-history.ts`)
// with the deep analysis. House in an energy flow opens what the house used (`house-loads.ts`). In a box of fixed
// height (a wall screen) the shown tab's picture takes the box's spare height.
//
// YAML: `type: custom:spotnav-house-card` with `site: <site entry id>` or `charger: <charger entry id>`, and
// optionally `tab: now | flow | graph | price` to show only that tab (no tab row).

import { getHouse, getHouseNow, getPeriodHistory, SpotnavApiError, UNSUPPORTED_API_VERSION } from "./api";
import { entityWordsOf } from "./entity-words";
import { LOADS_STYLES, LoadsPopover, type LoadsKind, type LoadsRequest } from "./house-loads";
import { decodeHouse, type EnergyFlow, type HouseAnswer } from "./house-model";
import { decodeHouseNow, NOW_STYLES, type HouseNow } from "./house-now";
import { PERIOD_VIEW_STYLES } from "./house-period-view";
import { HOUSE_TABS, isHouseTab, readTab, TAB_STYLES, writeTab, type HouseTab, type PeriodChoice } from "./house-tabs";
import { HEAT_STYLES } from "./heat-styles";
import { HistoryDialog } from "./history-dialog";
import { decodeHouseHistory, houseHistoryBody, type HouseHistory } from "./house-history";
import type { HistoryPeriod } from "./period-history";
import { HOUSE_STYLES } from "./house-styles";
import { PERIOD_STYLES } from "./period-history";
import { readHiddenSeries, writeHiddenSeries } from "./house-series";
import { houseBody, houseDetails, localDate, type HouseState, type HouseTabsData, type HouseUi, type Loadable } from "./house-view";
import { element as periodElement } from "./period-history";
import { isLanguagePending, loadLanguage, resolveLanguage, translate, type Language } from "./i18n";
import { SettingsPopover, stubTarget } from "./settings-popover";
import type { HomeAssistantLike } from "./types";
import { VISUAL_CLASSES, VISUAL_STYLES } from "./visual-styles";

export const HOUSE_CARD_TYPE = "spotnav-house-card";
export const HOUSE_CARD_ELEMENT = "spotnav-house-card";
export const HOUSE_REFRESH_MS = 300_000;
/** The Now tab's live flows are read this often while it is shown (the charging card's own refresh). */
export const LIVE_REFRESH_MS = 30_000;
/** The least the shown tab's picture is drawn in a box of fixed height. */
export const FILL_MIN_PX = 150;
/** Periods kept read while the card lives (the oldest go first). */
const PERIODS_KEPT = 12;
/** The chart's width before the card has been measured (and in a DOM that lays nothing out). */
export const HOUSE_FALLBACK_WIDTH = 340;
/** The card's side padding (`.spotnav-card`), taken off the measured width for the chart. */
const CARD_PADDING_PX = 28;
const STUB_TIMEOUT_MS = 3000;

export interface HouseCardConfig {
  type: string;
  site?: string;
  charger?: string;
  /** Show only this tab, without the tab row (a wall screen). */
  tab?: HouseTab;
}

/** The YAML, checked: `site` or `charger` as a non-empty string (the site wins when both are given). */
export function parseHouseConfig(raw: unknown): HouseCardConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error("Invalid configuration");
  }
  const source = raw as Record<string, unknown>;
  const config: HouseCardConfig = { type: typeof source.type === "string" ? source.type : `custom:${HOUSE_CARD_TYPE}` };
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
    if (!isHouseTab(tab)) {
      throw new Error(`tab must be one of ${HOUSE_TABS.join(", ")}`);
    }
    config.tab = tab;
  }
  return config;
}

/** The card editor's form (Home Assistant's own, from a schema): the site, and the one tab to show. */
export function houseConfigForm(language: Language): { schema: unknown[]; computeLabel: (field: { name: string }) => string } {
  const options = [{ value: "all", label: translate(language, "house.editor.tabAll") }, ...HOUSE_TABS.map((tab) => ({ value: tab, label: translate(language, `house.tab.${tab}`) }))];
  return {
    schema: [
      { name: "site", selector: { config_entry: { integration: "spotnav" } } },
      { name: "tab", selector: { select: { mode: "dropdown", options } } },
    ],
    computeLabel: (field) => translate(language, field.name === "tab" ? "house.editor.tab" : "house.editor.site"),
  };
}

/** Whether the flow shows the car or the heat on its own (the pie says they are not in it). */
function apart(flow: EnergyFlow | null): { car: boolean; heat: boolean } {
  const nodes = flow?.nodes ?? [];
  return { car: nodes.some((node) => node.role === "car"), heat: nodes.some((node) => node.role === "heat") };
}

let instances = 0;

/** The selector that finds an element of the card again after a redraw (focus kept over it), or `null`. */
function focusSelector(element: Element | null): string | null {
  if (!(element instanceof HTMLElement)) {
    return null;
  }
  for (const key of ["tab", "step", "pick", "pickKind", "bar", "legend", "view", "action"] as const) {
    const value = element.dataset[key];
    if (value !== undefined) {
      const attribute = key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
      return `[data-${attribute}="${value}"]`;
    }
  }
  return null;
}

/** The pictures a box of fixed height may grow, the first in the panel being the one that does. */
const FILL_TARGETS = ".spotnav-now-schematic, .spotnav-house-flow-chart, .spotnav-house-surface, .spotnav-house-pbars-plot";

export class SpotnavHouseCard extends HTMLElement {
  private readonly root: ShadowRoot;
  private hassObject: HomeAssistantLike | null = null;
  private config: HouseCardConfig | null = null;
  private state: HouseState = { kind: "unconfigured" };
  private ui: HouseUi = { selected: null, view: "energy", hidden: readHiddenSeries(), tab: "now", locked: null, choice: { period: "day", date: null }, bar: null, fill: null };
  private language: Language | null = null;
  private wantedLanguage: Language | null = null;
  private timer: number | null = null;
  private liveTimer: number | null = null;
  private live: HouseNow | null = null;
  /** The backend has no live flows (older than the tabs): Now shows the day's figures only, and nothing is polled. */
  private liveUnsupported = false;
  private readonly periods = new Map<string, Loadable<HouseHistory>>();
  private readonly days = new Map<string, Loadable<HouseAnswer>>();
  private connected = false;
  private generation = 0;
  private width = HOUSE_FALLBACK_WIDTH;
  private boxHeight = 0;
  private observer: ResizeObserver | null = null;
  private readonly idPrefix: string;
  private readonly styleNode: HTMLStyleElement;
  private readonly mount: HTMLDivElement;
  /** Where the shared settings popover's dialog lives, apart from what each render replaces. */
  private readonly layer: HTMLDivElement;
  private settingsPopover: SettingsPopover | null = null;
  private historyDialog: HistoryDialog<HouseHistory> | null = null;
  private loadsPopover: LoadsPopover | null = null;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    instances += 1;
    this.idPrefix = `spotnav-house-${instances}`;
    const doc = this.ownerDocument;
    this.styleNode = doc.createElement("style");
    this.styleNode.textContent = VISUAL_STYLES + HOUSE_STYLES + HEAT_STYLES + PERIOD_STYLES + LOADS_STYLES + TAB_STYLES + NOW_STYLES + PERIOD_VIEW_STYLES;
    this.mount = doc.createElement("div");
    this.mount.className = VISUAL_CLASSES.shell;
    this.layer = doc.createElement("div");
    this.layer.className = VISUAL_CLASSES.shell;
    this.root.append(this.styleNode, this.mount, this.layer);
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
        language: () => this.language ?? resolveLanguage(null),
        onChanged: () => void this.refresh(),
      });
    }
    return this.settingsPopover;
  }

  private ensureHistory(): HistoryDialog<HouseHistory> {
    if (this.historyDialog === null) {
      this.historyDialog = new HistoryDialog<HouseHistory>({
        kind: "house",
        doc: this.ownerDocument,
        owner: this.layer,
        background: () => this.mount,
        idPrefix: this.idPrefix,
        hass: () => this.hassObject,
        target: () => this.target(),
        language: () => this.language ?? resolveLanguage(null),
        width: () => this.width,
        decode: decodeHouseHistory,
        // With Detailed on, the analysis of the last days the front once kept behind "Show details".
        body: (input) => {
          const body = houseHistoryBody(input);
          if (input.ui.detailed && this.state.kind === "ready") {
            const block = periodElement(input.doc, "section", VISUAL_CLASSES.historyTile);
            block.dataset["part"] = "recent";
            block.append(
              periodElement(input.doc, "h4", VISUAL_CLASSES.historyTileHeading, translate(input.language, "hist.house.recent")),
              houseDetails(input.doc, input.language, this.state.answer, entityWordsOf(this.hassObject), `${this.idPrefix}-history`),
            );
            body.append(block);
          }
          return body;
        },
        flowHouse: (answer, back, kind) => {
          const running = answer.today >= answer.date && answer.today < answer.end;
          this.ensureLoads().open({ kind, period: answer.period, date: answer.date, running, ...apart(answer.energy_flow) }, null, back);
        },
      });
    }
    return this.historyDialog;
  }

  private ensureLoads(): LoadsPopover {
    if (this.loadsPopover === null) {
      this.loadsPopover = new LoadsPopover({
        doc: this.ownerDocument,
        owner: this.layer,
        background: () => this.mount,
        idPrefix: this.idPrefix,
        hass: () => this.hassObject,
        target: () => this.target(),
        language: () => this.language ?? resolveLanguage(null),
      });
    }
    return this.loadsPopover;
  }

  /** What the house (or the heat) used over a period the card shows (today's day without one; any user who sees the
   * card). */
  showLoads(opener: HTMLElement | null = null, kind: LoadsKind = "house", period: { period: HistoryPeriod; date: string; running: boolean } | null = null): void {
    if (this.target() === null || this.state.kind !== "ready") {
      return;
    }
    const answer = this.state.answer;
    if (period !== null) {
      const shown = this.periods.get(this.periodKey(this.ui.choice ?? { period: "day", date: null }));
      const flow = shown?.kind === "ready" ? shown.answer.energy_flow : answer.energy_flow;
      this.ensureLoads().open({ kind, period: period.period, date: period.date, running: period.running, ...apart(flow) }, opener);
      return;
    }
    const past = answer.day !== null && !answer.day.today;
    const request: LoadsRequest = { kind, period: "day", date: past ? answer.day!.date : null, running: !past, ...apart(answer.energy_flow) };
    this.ensureLoads().open(request, opener);
  }

  /** The history dialog: a day, month or year of the house (any user who sees the card). */
  showHistory(opener: HTMLElement | null = null): void {
    if (this.target() === null) {
      return;
    }
    this.ensureHistory().open(opener);
  }

  /** The shared settings popover, opened at its top (the house's own section is the site). */
  showSettings(): void {
    if (this.hassObject?.user?.is_admin !== true || this.target() === null) {
      return;
    }
    void this.ensurePopover().open("house");
  }

  setConfig(config: unknown): void {
    this.config = parseHouseConfig(config);
    this.generation += 1;
    const locked = this.config.tab ?? null;
    const tab = locked ?? readTab(this.targetKey()) ?? "now";
    this.ui = { selected: null, view: "energy", hidden: this.ui.hidden ?? readHiddenSeries(), tab, locked, choice: { period: "day", date: null }, bar: null, fill: null };
    this.periods.clear();
    this.days.clear();
    this.live = null;
    this.liveUnsupported = false;
    this.stopLive();
    this.state = this.target() === null ? { kind: "unconfigured" } : { kind: "loading" };
    this.render();
    this.start();
  }

  set hass(hass: HomeAssistantLike) {
    const first = this.hassObject === null;
    this.hassObject = hass;
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
    this.stopLive();
    this.settingsPopover?.destroy();
    this.settingsPopover = null;
    this.historyDialog?.destroy();
    this.historyDialog = null;
    this.loadsPopover?.destroy();
    this.loadsPopover = null;
  }

  getCardSize(): number {
    return 5;
  }

  getGridOptions(): { rows: "auto"; columns: "full" } {
    return { rows: "auto", columns: "full" };
  }

  /** The card editor: Home Assistant's own form from a schema, the site and the one tab to show. */
  static getConfigForm(): ReturnType<typeof houseConfigForm> {
    const language = resolveLanguage(typeof document === "undefined" ? null : document.documentElement.lang || null);
    return houseConfigForm(language);
  }

  /** The card picker's preview: the first charger's site, else the first site (a home without a charger), else an
   *  unconfigured card. */
  static async getStubConfig(hass?: HomeAssistantLike): Promise<HouseCardConfig> {
    const stub: HouseCardConfig = { type: `custom:${HOUSE_CARD_TYPE}` };
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

  private followLanguage(next: Language): void {
    if (this.wantedLanguage === next) {
      return;
    }
    this.wantedLanguage = next;
    if (!isLanguagePending(next)) {
      this.language = next;
      this.render();
      this.historyDialog?.repaint();
      this.loadsPopover?.repaint();
      return;
    }
    void loadLanguage(next).then(() => {
      if (this.wantedLanguage === next) {
        this.language = next;
        this.render();
        this.historyDialog?.repaint();
        this.loadsPopover?.repaint();
      }
    });
  }

  private measure(): void {
    const measured = this.getBoundingClientRect().width - CARD_PADDING_PX;
    const width = measured > 0 ? measured : HOUSE_FALLBACK_WIDTH;
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
      this.timer = window.setInterval(() => void this.refresh(), HOUSE_REFRESH_MS);
    }
    void this.refresh();
  }

  /** Read the answer once; an older read that comes back after a newer config is dropped. */
  async refresh(): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    if (hass === null || target === null) {
      return;
    }
    const generation = this.generation;
    let next: HouseState;
    try {
      const decoded = decodeHouse(await getHouse(hass, target));
      next = decoded.ok
        ? { kind: "ready", answer: decoded.value }
        : { kind: "failed", sentenceKey: decoded.failure === "unsupported" ? "house.unsupported" : "house.failed", code: null };
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      const unsupported = code === UNSUPPORTED_API_VERSION || code === "unknown_command";
      next = { kind: "failed", sentenceKey: unsupported ? "house.unsupported" : "house.failed", code };
    }
    if (generation !== this.generation) {
      return;
    }
    // A failed read keeps what was shown, unless nothing was.
    if (next.kind === "failed" && this.state.kind === "ready") {
      return;
    }
    this.state = next;
    // The running periods are read again with the answer; past ones stay.
    for (const key of [...this.periods.keys()].filter((key) => key.endsWith(":now"))) {
      this.periods.delete(key);
    }
    this.render();
    this.ensureData();
  }

  // -------------------------------------------------------------------------------------------- the tabs' data

  private shownTab(): HouseTab {
    return this.ui.locked ?? this.ui.tab ?? "now";
  }

  private periodKey(choice: PeriodChoice): string {
    return `${choice.period}:${choice.date ?? "now"}`;
  }

  /** What the shown tab and period need besides the card's answer. */
  needs(): { live: boolean; history: PeriodChoice | null; day: string | null } {
    const tab = this.shownTab();
    const choice = this.ui.choice ?? { period: "day", date: null };
    if (tab === "now") {
      return { live: true, history: null, day: null };
    }
    const first = this.state.kind === "ready" ? (this.state.answer.day?.first ?? null) : null;
    const hours = choice.period === "day" && choice.date !== null && (first === null || choice.date >= first) ? choice.date : null;
    if (tab === "flow") {
      return { live: false, history: choice.period === "day" && choice.date === null ? null : choice, day: null };
    }
    if (tab === "graph") {
      if (choice.period === "day" && choice.date === null) {
        return { live: false, history: null, day: null };
      }
      return { live: false, history: hours === null ? choice : null, day: hours };
    }
    return { live: false, history: choice, day: hours };
  }

  private tabsData(): HouseTabsData | undefined {
    if (this.state.kind !== "ready") {
      return undefined;
    }
    const answer = this.state.answer;
    const needs = this.needs();
    return {
      live: this.live,
      history: needs.history === null ? null : (this.periods.get(this.periodKey(needs.history)) ?? { kind: "loading" }),
      day: needs.day === null ? null : (this.days.get(needs.day) ?? { kind: "loading" }),
      today: answer.day?.today === true ? answer.day.date : localDate(answer.time_zone, Date.now()),
    };
  }

  /** Read what the shown tab needs and has not got; poll the live flows while Now is shown. */
  private ensureData(): void {
    if (!this.connected || this.state.kind !== "ready") {
      return;
    }
    const needs = this.needs();
    if (needs.live) {
      this.startLive();
    } else {
      this.stopLive();
    }
    if (needs.history !== null) {
      void this.loadPeriod(needs.history);
    }
    if (needs.day !== null) {
      void this.loadDay(needs.day);
    }
  }

  private async loadPeriod(choice: PeriodChoice): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    const key = this.periodKey(choice);
    if (hass === null || target === null || this.periods.has(key)) {
      return;
    }
    this.periods.set(key, { kind: "loading" });
    const generation = this.generation;
    let next: Loadable<HouseHistory>;
    try {
      const decoded = decodeHouseHistory(await getPeriodHistory(hass, "house", target, choice.period, choice.date));
      next = decoded.ok ? { kind: "ready", answer: decoded.value } : { kind: "failed", code: decoded.failure === "unsupported" ? UNSUPPORTED_API_VERSION : null };
    } catch (error) {
      next = { kind: "failed", code: error instanceof SpotnavApiError ? error.code : null };
    }
    if (generation !== this.generation) {
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
    this.render();
  }

  private async loadDay(date: string): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    if (hass === null || target === null || this.days.has(date)) {
      return;
    }
    this.days.set(date, { kind: "loading" });
    const generation = this.generation;
    let next: Loadable<HouseAnswer>;
    try {
      const decoded = decodeHouse(await getHouse(hass, target, date));
      next = decoded.ok ? { kind: "ready", answer: decoded.value } : { kind: "failed", code: null };
    } catch (error) {
      next = { kind: "failed", code: error instanceof SpotnavApiError ? error.code : null };
    }
    if (generation !== this.generation) {
      return;
    }
    this.days.set(date, next);
    while (this.days.size > PERIODS_KEPT) {
      this.days.delete(this.days.keys().next().value as string);
    }
    this.render();
  }

  private startLive(): void {
    if (this.liveUnsupported || this.liveTimer !== null || !this.connected) {
      return;
    }
    this.liveTimer = window.setInterval(() => void this.readLive(), LIVE_REFRESH_MS);
    void this.readLive();
  }

  private stopLive(): void {
    if (this.liveTimer !== null) {
      window.clearInterval(this.liveTimer);
      this.liveTimer = null;
    }
  }

  /** The live flows once; a backend without them stops the polling, a failed read keeps the last. */
  async readLive(): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    if (hass === null || target === null) {
      return;
    }
    const generation = this.generation;
    try {
      const decoded = decodeHouseNow(await getHouseNow(hass, target));
      if (generation !== this.generation) {
        return;
      }
      if (decoded.ok) {
        this.live = decoded.value;
      } else if (decoded.failure === "unsupported") {
        this.liveUnsupported = true;
        this.stopLive();
      }
    } catch (error) {
      if (generation !== this.generation) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      if (code === UNSUPPORTED_API_VERSION || code === "unknown_command") {
        this.liveUnsupported = true;
        this.stopLive();
      }
    }
    if (this.shownTab() === "now") {
      this.render();
    }
  }

  // -------------------------------------------------------------------------------------------- drawing

  private render(): void {
    if (this.config === null) {
      return;
    }
    const focused = focusSelector(this.root.activeElement);
    this.paint(null);
    this.fit();
    if (focused !== null) {
      const again = this.root.querySelector<HTMLElement>(focused);
      if (again !== null && !(again as HTMLButtonElement).disabled) {
        again.focus();
      }
    }
  }

  /**
   * In a box of fixed height (a wall screen: the host is as tall as the box, not as its content), the shown tab's
   * picture takes the room the rest leaves, never below `FILL_MIN_PX`. One reading of the natural layout, one plan
   * from it: measuring again gives the same plan, so a resize never loops. In a plain column nothing changes.
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
    const fill = Math.max(FILL_MIN_PX, Math.floor(compactBasis + box - (compact?.offsetHeight ?? natural)));
    this.paint(fill);
    const filled = this.mount.firstElementChild as HTMLElement | null;
    if (filled !== null) {
      filled.dataset["box"] = "true";
    }
  }

  private paint(fill: number | null): void {
    const doc = this.ownerDocument;
    const language = this.language ?? resolveLanguage(null);
    const admin = this.hassObject?.user?.is_admin === true;
    const tabs = this.tabsData();
    this.mount.replaceChildren(
      houseBody({
        doc,
        language,
        state: this.state,
        ui: { ...this.ui, fill },
        handlers: {
          onView: (view) => {
            this.ui = { ...this.ui, view };
            this.render();
          },
          onLegend: (view, hidden) => {
            this.ui = { ...this.ui, hidden: { ...this.ui.hidden, [view]: hidden } };
            writeHiddenSeries(view, hidden);
            this.render();
          },
          onSelect: (index) => {
            this.ui = { ...this.ui, selected: index };
          },
          onTest: (open) => {
            this.ui = { ...this.ui, testOpen: open };
            this.render();
            (this.root.querySelector("button[data-action='test']") as HTMLElement | null)?.focus();
          },
          onTab: (tab, byKey) => {
            this.ui = { ...this.ui, tab, selected: null, bar: null };
            writeTab(this.targetKey(), tab);
            this.render();
            if (byKey) {
              (this.root.querySelector(`[role='tab'][data-tab='${tab}']`) as HTMLElement | null)?.focus();
            }
            this.ensureData();
          },
          onPeriod: (choice) => {
            this.ui = { ...this.ui, choice, selected: null, bar: null };
            this.render();
            this.ensureData();
          },
          onBar: (key) => {
            this.ui = { ...this.ui, bar: key };
          },
          ...(admin ? { onSettings: () => this.showSettings() } : {}),
          onHistory: (opener) => this.showHistory(opener),
          onHouseLoads: (opener, kind, period) => this.showLoads(opener, kind, period ?? null),
        },
        width: this.width,
        nowMs: Date.now(),
        idPrefix: this.idPrefix,
        entityWords: entityWordsOf(this.hassObject),
        ...(tabs === undefined ? {} : { tabs }),
      }),
    );
  }
}
