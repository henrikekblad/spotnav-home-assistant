// The house card (`custom:spotnav-house-card`, SpotNav 3.0 preview): a second element in the same bundle, so nothing
// extra is installed. It reads one read-only answer (`spotnav/get_house`) for a site, named by the site's id or a
// charger's in it, every `HOUSE_REFRESH_MS` (the forecasts change once an hour), and shows it (`house-view.ts`).
// An administrator's gear opens the shared settings popover (`settings-popover.ts`): the house card is where a home
// without a charger starts. The charging card stays about the car. The front shows today; the clock beside the gear
// opens the history (`history-dialog.ts`, `house-history.ts`): a day, month or year. House in the energy flow (the
// front's and the history's) opens what the house used (`house-loads.ts`).
//
// YAML: `type: custom:spotnav-house-card` with `site: <site entry id>` or `charger: <charger entry id>`.

import { getHouse, SpotnavApiError, UNSUPPORTED_API_VERSION } from "./api";
import { entityWordsOf } from "./entity-words";
import { LOADS_STYLES, LoadsPopover, type LoadsKind, type LoadsRequest } from "./house-loads";
import { decodeHouse, type EnergyFlow } from "./house-model";
import { HEAT_STYLES } from "./heat-styles";
import { HistoryDialog } from "./history-dialog";
import { decodeHouseHistory, houseHistoryBody, type HouseHistory } from "./house-history";
import { HOUSE_STYLES } from "./house-styles";
import { PERIOD_STYLES } from "./period-history";
import { readHiddenSeries, writeHiddenSeries } from "./house-series";
import { houseBody, type HouseState, type HouseUi } from "./house-view";
import { isLanguagePending, loadLanguage, resolveLanguage, type Language } from "./i18n";
import { SettingsPopover, stubTarget } from "./settings-popover";
import type { HomeAssistantLike } from "./types";
import { VISUAL_CLASSES, VISUAL_STYLES } from "./visual-styles";

export const HOUSE_CARD_TYPE = "spotnav-house-card";
export const HOUSE_CARD_ELEMENT = "spotnav-house-card";
export const HOUSE_REFRESH_MS = 300_000;
/** The chart's width before the card has been measured (and in a DOM that lays nothing out). */
export const HOUSE_FALLBACK_WIDTH = 340;
/** The card's side padding (`.spotnav-card`), taken off the measured width for the chart. */
const CARD_PADDING_PX = 28;
const STUB_TIMEOUT_MS = 3000;

export interface HouseCardConfig {
  type: string;
  site?: string;
  charger?: string;
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
  return config;
}

/** Whether the flow shows the car or the heat on its own (the pie says they are not in it). */
function apart(flow: EnergyFlow | null): { car: boolean; heat: boolean } {
  const nodes = flow?.nodes ?? [];
  return { car: nodes.some((node) => node.role === "car"), heat: nodes.some((node) => node.role === "heat") };
}

let instances = 0;

export class SpotnavHouseCard extends HTMLElement {
  private readonly root: ShadowRoot;
  private hassObject: HomeAssistantLike | null = null;
  private config: HouseCardConfig | null = null;
  private state: HouseState = { kind: "unconfigured" };
  private ui: HouseUi = { detailsOpen: false, selected: null, view: "energy", hidden: readHiddenSeries() };
  private language: Language | null = null;
  private wantedLanguage: Language | null = null;
  private timer: number | null = null;
  private connected = false;
  private generation = 0;
  private width = HOUSE_FALLBACK_WIDTH;
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
    this.styleNode.textContent = VISUAL_STYLES + HOUSE_STYLES + HEAT_STYLES + PERIOD_STYLES + LOADS_STYLES;
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
        body: houseHistoryBody,
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

  /** What the house (or the heat) used on the day the front shows (any user who sees the card). */
  showLoads(opener: HTMLElement | null = null, kind: LoadsKind = "house"): void {
    if (this.target() === null || this.state.kind !== "ready") {
      return;
    }
    const answer = this.state.answer;
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
    this.ui = { detailsOpen: false, selected: null, view: "energy", hidden: this.ui.hidden ?? readHiddenSeries() };
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
    if (Math.abs(width - this.width) >= 4) {
      this.width = width;
      this.render();
      this.historyDialog?.repaint();
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
    this.render();
  }

  private render(): void {
    if (this.config === null) {
      return;
    }
    const doc = this.ownerDocument;
    const language = this.language ?? resolveLanguage(null);
    const admin = this.hassObject?.user?.is_admin === true;
    this.mount.replaceChildren(
      houseBody({
        doc,
        language,
        state: this.state,
        ui: this.ui,
        handlers: {
          onDetails: (open) => {
            const focused = this.root.activeElement !== null;
            this.ui = { ...this.ui, detailsOpen: open };
            this.render();
            if (focused) {
              (this.root.querySelector(".spotnav-house-details-toggle") as HTMLElement | null)?.focus();
            }
          },
          onView: (view) => {
            const focused = this.root.activeElement !== null;
            this.ui = { ...this.ui, view };
            this.render();
            if (focused) {
              (this.root.querySelector(`button[data-view="${view}"]`) as HTMLElement | null)?.focus();
            }
          },
          onLegend: (view, hidden) => {
            const focused = (this.root.activeElement as HTMLElement | null)?.dataset["legend"] ?? null;
            this.ui = { ...this.ui, hidden: { ...this.ui.hidden, [view]: hidden } };
            writeHiddenSeries(view, hidden);
            this.render();
            if (focused !== null) {
              (this.root.querySelector(`button[data-legend="${focused}"]`) as HTMLElement | null)?.focus();
            }
          },
          onSelect: (index) => {
            this.ui = { ...this.ui, selected: index };
          },
          onTest: (open) => {
            this.ui = { ...this.ui, testOpen: open };
            this.render();
            (this.root.querySelector("button[data-action='test']") as HTMLElement | null)?.focus();
          },
          ...(admin ? { onSettings: () => this.showSettings() } : {}),
          onHistory: (opener) => this.showHistory(opener),
          onHouseLoads: (opener, kind) => this.showLoads(opener, kind),
        },
        width: this.width,
        nowMs: Date.now(),
        idPrefix: this.idPrefix,
        entityWords: entityWordsOf(this.hassObject),
      }),
    );
  }
}
