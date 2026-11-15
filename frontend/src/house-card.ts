// The house card (`custom:spotnav-house-card`, SpotNav 3.0 preview): a second element in the same bundle, so nothing
// extra is installed. It reads one read-only answer (`spotnav/get_house`) for a site, named by the site's id or a
// charger's in it, every `HOUSE_REFRESH_MS` (the forecasts change once an hour), and shows it (`house-view.ts`).
// It never writes. The charging card stays about the car.
//
// YAML: `type: custom:spotnav-house-card` with `site: <site entry id>` or `charger: <charger entry id>`.

import { getHouse, listChargers, SpotnavApiError, UNSUPPORTED_API_VERSION } from "./api";
import { entityWordsOf } from "./entity-words";
import { decodeHouse } from "./house-model";
import { HOUSE_STYLES } from "./house-styles";
import { readHiddenSeries, writeHiddenSeries } from "./house-series";
import { houseBody, type HouseState, type HouseUi } from "./house-view";
import { isLanguagePending, loadLanguage, resolveLanguage, type Language } from "./i18n";
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

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    instances += 1;
    this.idPrefix = `spotnav-house-${instances}`;
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
  }

  getCardSize(): number {
    return 5;
  }

  getGridOptions(): { rows: "auto"; columns: "full" } {
    return { rows: "auto", columns: "full" };
  }

  /** The card picker's preview: the one charger's site, else an unconfigured card. */
  static async getStubConfig(hass?: HomeAssistantLike): Promise<HouseCardConfig> {
    const stub: HouseCardConfig = { type: `custom:${HOUSE_CARD_TYPE}` };
    if (hass === undefined) {
      return stub;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const list = await Promise.race([
        listChargers(hass),
        new Promise<null>((resolve) => {
          timer = setTimeout(() => resolve(null), STUB_TIMEOUT_MS);
        }),
      ]);
      const first = list?.chargers[0];
      if (first !== undefined) {
        return { ...stub, charger: first.charger_id };
      }
    } catch {
      // An unconfigured card, never an error in the picker.
    } finally {
      clearTimeout(timer);
    }
    return stub;
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
      return;
    }
    void loadLanguage(next).then(() => {
      if (this.wantedLanguage === next) {
        this.language = next;
        this.render();
      }
    });
  }

  private measure(): void {
    const measured = this.getBoundingClientRect().width - CARD_PADDING_PX;
    const width = measured > 0 ? measured : HOUSE_FALLBACK_WIDTH;
    if (Math.abs(width - this.width) >= 4) {
      this.width = width;
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
      const day = this.ui.day ?? null;
      const decoded = decodeHouse(await getHouse(hass, target, day));
      if ((this.ui.day ?? null) !== day) {
        return;
      }
      if (decoded.ok && decoded.value.day?.today === true && day !== null) {
        // Stepped forward onto today: from now on asked for as today, so it follows midnight.
        this.ui = { ...this.ui, day: null };
      }
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
    const style = doc.createElement("style");
    style.textContent = VISUAL_STYLES + HOUSE_STYLES;
    const shell = doc.createElement("div");
    shell.className = VISUAL_CLASSES.shell;
    const language = this.language ?? resolveLanguage(null);
    shell.append(
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
              (this.root.querySelector("[aria-expanded]") as HTMLElement | null)?.focus();
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
          onDay: (day) => {
            const focused = this.root.activeElement as HTMLElement | null;
            const which = focused?.dataset["step"] ?? null;
            this.ui = { ...this.ui, day, selected: null };
            void this.refresh().then(() => {
              if (which !== null) {
                const again = this.root.querySelector<HTMLButtonElement>(`button[data-step="${which}"]`);
                (again !== null && !again.disabled ? again : this.root.querySelector<HTMLElement>("button[data-step]"))?.focus();
              }
            });
          },
        },
        width: this.width,
        nowMs: Date.now(),
        idPrefix: this.idPrefix,
        entityWords: entityWordsOf(this.hassObject),
      }),
    );
    this.root.replaceChildren(style, shell);
  }
}
