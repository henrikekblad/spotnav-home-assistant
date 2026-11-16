// The heat card (`custom:spotnav-heat-card`, SpotNav 3.0 preview): a third element in the same bundle, so nothing extra
// is installed. It reads `spotnav/get_heat` for a site, named by the site's id or a charger's in it, every
// `HEAT_REFRESH_MS`, and shows it (`heat-view.ts`). The dial and home/away write the site's heat settings through
// `spotnav/update_heat` (a compare-and-set); an administrator's settings button opens the shared settings popover
// (`settings-popover.ts`) at its heat section. Shadow
// only: what the card writes is SpotNav's own settings, never a heating device.
//
// YAML: `type: custom:spotnav-heat-card` with `site: <site entry id>` or `charger: <charger entry id>`.

import { getHeat, SpotnavApiError, UNSUPPORTED_API_VERSION } from "./api";
import { writeError, writeHeat } from "./heat-editors";
import { decodeHeat, type HeatAnswer, type HeatConfig } from "./heat-model";
import { HEAT_CLASSES as X, HEAT_STYLES } from "./heat-styles";
import { TARGET_MAX, TARGET_MIN, instantToLocal, localToInstant, heatBody, type HeatState, type HeatUi } from "./heat-view";
import { HistoryDialog } from "./history-dialog";
import { decodeHeatHistory, heatHistoryBody, type HeatHistory } from "./heat-history";
import { HOUSE_STYLES } from "./house-styles";
import { PERIOD_STYLES } from "./period-history";
import { isLanguagePending, loadLanguage, resolveLanguage, translate, type Language } from "./i18n";
import { SettingsPopover, stubTarget } from "./settings-popover";
import type { HomeAssistantLike } from "./types";
import { VISUAL_CLASSES, VISUAL_STYLES } from "./visual-styles";

export { instantToLocal, localToInstant, writeError };

export const HEAT_CARD_TYPE = "spotnav-heat-card";
export const HEAT_CARD_ELEMENT = "spotnav-heat-card";
export const HEAT_REFRESH_MS = 60_000;
/** A dial step is written this long after the last tap, so a run of taps is one write. */
export const DIAL_SETTLE_MS = 700;
export const HEAT_FALLBACK_WIDTH = 330;
const CARD_PADDING_PX = 28;
const STUB_TIMEOUT_MS = 3000;

export interface HeatCardConfig {
  type: string;
  site?: string;
  charger?: string;
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
  return config;
}

let instances = 0;

export class SpotnavHeatCard extends HTMLElement {
  private readonly root: ShadowRoot;
  private readonly mount: HTMLDivElement;
  private readonly layer: HTMLDivElement;
  private hassObject: HomeAssistantLike | null = null;
  private config: HeatCardConfig | null = null;
  private state: HeatState = { kind: "unconfigured" };
  private ui: HeatUi = { pendingTarget: null, error: null };
  private language: Language | null = null;
  private wantedLanguage: Language | null = null;
  private timer: number | null = null;
  private settle: ReturnType<typeof setTimeout> | null = null;
  private connected = false;
  private generation = 0;
  private width = HEAT_FALLBACK_WIDTH;
  private observer: ResizeObserver | null = null;
  /** The shared settings popover (and the away editor), made on first use. */
  private settingsPopover: SettingsPopover | null = null;
  private historyDialog: HistoryDialog<HeatHistory> | null = null;
  private readonly idPrefix: string;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    instances += 1;
    this.idPrefix = `spotnav-heat-${instances}`;
    const doc = this.ownerDocument;
    const style = doc.createElement("style");
    style.textContent = VISUAL_STYLES + HOUSE_STYLES + HEAT_STYLES + PERIOD_STYLES;
    this.mount = doc.createElement("div");
    this.mount.className = VISUAL_CLASSES.shell;
    this.layer = doc.createElement("div");
    this.layer.className = X.card;
    this.root.append(style, this.mount, this.layer);
  }

  setConfig(config: unknown): void {
    this.config = parseHeatConfig(config);
    this.generation += 1;
    this.ui = { pendingTarget: null, error: null };
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
    if (this.settle !== null) {
      clearTimeout(this.settle);
      this.settle = null;
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
      this.timer = window.setInterval(() => void this.refresh(), HEAT_REFRESH_MS);
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
    if (generation !== this.generation) {
      return;
    }
    if (next.kind === "failed" && this.state.kind === "ready") {
      return;
    }
    this.state = next;
    this.render();
  }

  /** Write `changes` with what was last seen for the same keys; `null` when it took, else the sentence. */
  async write(changes: Partial<Record<keyof HeatConfig, unknown>>): Promise<string | null> {
    const hass = this.hassObject;
    const target = this.target();
    const answer = this.answer();
    const language = this.cardLanguage();
    if (hass === null || target === null || answer === null) {
      return translate(language, "heat.error.generic");
    }
    const result = await writeHeat(hass, target, answer, changes, language);
    if (result.config !== null && this.state.kind === "ready") {
      // The stored settings at once; the rest of the view follows with the read.
      this.state = { kind: "ready", answer: { ...this.state.answer, config: result.config } };
    }
    await this.refresh();
    return result.error;
  }

  private step(delta: number): void {
    const answer = this.answer();
    if (answer === null) {
      return;
    }
    const base = this.ui.pendingTarget ?? answer.config.target_c;
    const next = Math.min(TARGET_MAX, Math.max(TARGET_MIN, Math.round((base + delta) * 2) / 2));
    this.ui = { pendingTarget: next, error: null };
    this.render();
    if (this.settle !== null) {
      clearTimeout(this.settle);
    }
    this.settle = setTimeout(() => {
      this.settle = null;
      const wanted = this.ui.pendingTarget;
      if (wanted === null || wanted === answer.config.target_c) {
        this.ui = { pendingTarget: null, error: null };
        this.render();
        return;
      }
      void this.write({ target_c: wanted }).then((error) => {
        this.ui = { pendingTarget: null, error };
        this.render();
      });
    }, DIAL_SETTLE_MS);
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

  /** The shared settings popover, opened at the heat section. */
  showSettings(): void {
    const answer = this.answer();
    if (answer === null || !answer.writable) {
      return;
    }
    void this.ensurePopover().open("heat");
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
    const doc = this.ownerDocument;
    const language = this.cardLanguage();
    this.mount.replaceChildren(
      heatBody({
        doc,
        language,
        state: this.state,
        ui: this.ui,
        width: this.width,
        nowMs: Date.now(),
        handlers: {
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
        },
      }),
    );
  }
}
