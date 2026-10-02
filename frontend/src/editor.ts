// Visual editor: one charger chosen by display name, stored as a config-entry id.
//
// Choices come from `spotnav/list_chargers`. A configured id the backend no longer offers is kept
// as an explicit unresolved choice, and the emitted config-changed event carries exactly
// `{config: {type, charger}}`, the shape Home Assistant reads (`ev.detail.config`). Everything
// dynamic goes into the DOM as text or properties, failures are one static sentence, and a
// rejection's text is never shown.
//
// Load ownership (see test/editor-lifecycle.test.ts):
//   * a load starts only while connected, with a `hass` snapshot and no valid list yet;
//   * each attempt carries a generation: disconnecting or retrying makes a late completion inert;
//   * a new `hass` snapshot is not an invalidation (one arrives per state update);
//   * reconnecting reloads only if there is no valid list; no timers, no shared state.

import { listChargers } from "./api";
import { chargerOption, labelledButton, textParagraph } from "./dom";
import { resolveLanguage, translate } from "./i18n";
import { CARD_TYPE } from "./types";
import type { ChargerList, HomeAssistantLike } from "./types";
import { chargerOptions, editorConfig, parseCardConfig, stubChargerId, type CardConfig } from "./view";

const EDITOR_STYLES = `
  .card { padding: 12px 14px; background: var(--card-background-color, #fff);
          color: var(--primary-text-color, #212121); }
  label { display: block; font-size: 0.85rem; color: var(--secondary-text-color, #727272); }
  select { font: inherit; margin-top: 4px; max-width: 100%; padding: 6px;
           color: inherit; background: var(--card-background-color, #fff);
           border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px; }
  p { margin: 8px 0 0; font-size: 0.85rem; overflow-wrap: anywhere; }
  .error { color: var(--error-color, #db4437); }
  button { margin-top: 8px; font: inherit; color: inherit; background: transparent;
           border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px; padding: 6px 10px; }
  button:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
`;

export const EDITOR_LOAD_FAILED_MESSAGE =
  "Could not load the SpotNav chargers. Check the connection and try again.";

export class SpotnavCardEditor extends HTMLElement {
  private root: ShadowRoot;
  private hassObject: HomeAssistantLike | null = null;
  private config: CardConfig = { type: `custom:${CARD_TYPE}`, charger: "" };
  private list: ChargerList | null = null;
  private loadFailed = false;
  private connected = false;
  private assigned = false;
  private generation = 0;
  private inFlight: number | null = null;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
  }

  setConfig(config: unknown): void {
    this.config = parseCardConfig(config);
    this.render();
    this.startLoad();
  }

  /**
   * Every `hass` assignment only stores the snapshot for the next request; only the first may
   * start the initial load.
   */
  set hass(hass: HomeAssistantLike) {
    this.hassObject = hass;
    if (this.assigned) {
      return;
    }
    this.assigned = true;
    this.startLoad();
  }

  get hass(): HomeAssistantLike | null {
    return this.hassObject;
  }

  connectedCallback(): void {
    this.connected = true;
    this.render();
    this.startLoad();
  }

  disconnectedCallback(): void {
    this.connected = false;
    this.invalidate();
  }

  private invalidate(): void {
    this.generation += 1;
    this.inFlight = null;
  }

  private startLoad(): void {
    if (!this.connected || this.hassObject === null || this.list !== null) {
      return;
    }
    if (this.inFlight === this.generation) {
      return; // One attempt per generation, however often config or `hass` arrives.
    }
    const generation = this.generation;
    const hass = this.hassObject;
    this.inFlight = generation;
    void this.load(hass, generation);
  }

  private async load(hass: HomeAssistantLike, generation: number): Promise<void> {
    let loaded: ChargerList | null = null;
    let failed = false;
    try {
      loaded = await listChargers(hass);
    } catch {
      failed = true;
    }
    if (generation !== this.generation || !this.connected) {
      return; // A late answer for a config, an attempt or a life that is no longer current.
    }
    this.inFlight = null;
    this.loadFailed = failed;
    this.list = failed ? null : loaded;
    this.render();
    this.autoSelect();
  }

  private autoSelect(): void {
    if (this.list === null || this.config.charger !== "") {
      return;
    }
    const only = stubChargerId(this.list);
    if (only !== null) {
      this.config = editorConfig(only);
      this.render();
      this.emit(only);
    }
  }

  private emit(chargerId: string): void {
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        // Home Assistant's card editor reads `ev.detail.config`.
        detail: { config: editorConfig(chargerId) },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private render(): void {
    const style = document.createElement("style");
    style.textContent = EDITOR_STYLES;

    const card = document.createElement("div");
    card.className = "card";

    const label = document.createElement("label");
    label.setAttribute("for", "spotnav-charger");
    label.textContent = "SpotNav charger";

    const select = document.createElement("select");
    select.id = "spotnav-charger";
    select.setAttribute("aria-label", "SpotNav charger");
    const placeholder = chargerOption("Select a charger", "");
    placeholder.selected = this.config.charger === "";
    select.append(placeholder);
    for (const choice of chargerOptions(this.list, this.config.charger)) {
      const item = chargerOption(
        `${choice.label}${choice.available ? "" : " (unavailable)"}`,
        choice.charger_id,
      );
      item.selected = choice.charger_id === this.config.charger;
      select.append(item);
    }
    select.addEventListener("change", () => {
      this.config = editorConfig(select.value);
      this.emit(this.config.charger);
      this.render();
    });

    card.append(label, select, this.statusElement());
    if (this.loadFailed) {
      card.append(this.retryButton());
    }
    this.root.replaceChildren(style, card);
  }

  private statusElement(): HTMLParagraphElement {
    const status = (text: string, className?: string): HTMLParagraphElement => {
      const element = textParagraph(text, className);
      element.setAttribute("role", "status");
      return element;
    };
    if (this.loadFailed) {
      return status(EDITOR_LOAD_FAILED_MESSAGE, "error");
    }
    const options = chargerOptions(this.list, this.config.charger);
    if (this.list !== null && options.length === 0) {
      // The charger list is empty, so say how to add one (a site on its own has no card to configure).
      const language = resolveLanguage(this.hassObject?.language);
      return status(`${translate(language, "state.noChargers")} ${translate(language, "state.addCharger")}`);
    }
    if (this.list === null) {
      return status("Loading chargers\u2026");
    }
    return status(
      this.config.charger === "" ? "No charger selected yet." : `Selected: ${this.config.charger}`,
    );
  }

  private retryButton(): HTMLButtonElement {
    const button = labelledButton("Retry", "Retry loading the charger list");
    button.addEventListener("click", () => {
      this.list = null;
      this.loadFailed = false;
      this.invalidate();
      this.render();
      this.startLoad();
    });
    return button;
  }
}
