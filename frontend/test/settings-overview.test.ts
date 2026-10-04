// The general Settings popover: one structured overview, through the real card element.
//
// It gathers the price area/fiscal editor and the vehicle's consumption editor, shows the capability
// facts inline, and carries a marked, empty site section. Nothing here invents a site name, a charger
// count or a switch when the payload has no `site` block.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate, type Language } from "../src/i18n";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { CARD_OUTDATED_NOTE, setOwnCardBundleHashForTest } from "../src/card-identity";
import { CARD_VERSION } from "../src/generated/version";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

function aRecord(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 3,
    amps: 10,
    requested_kwh: 20.5,
    max_periods: 4,
    departure_enabled: true,
    departure_time: "06:30",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: null },
    ...overrides,
  };
}

const PAUSE = { choice: null, admitted_at: null, expires_at: null };

function success(record: SettingsRecord): Record<string, unknown> {
  return { api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: { ...record }, pause: { ...PAUSE } };
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(
  payload: Record<string, unknown> = fixture("start_idle"),
  admin = true,
  language = "en",
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  if (admin) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

/** The open overlay, or null: every dialog exists from the first render and only one is ever visible. */
function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

function settingsGeneralButton(element: Element, language: Language = "en"): HTMLButtonElement {
  const found = shadow(element).querySelector<HTMLButtonElement>(
    `[aria-label="${translate(language, "header.settings")}"]`,
  );
  if (found === null) {
    throw new Error("no general settings button");
  }
  return found;
}

function section(element: Element, name: string): HTMLElement | null {
  return openDialog(element)?.querySelector<HTMLElement>(`[data-section="${name}"]`) ?? null;
}


const reads = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/get_settings");
const marketReads = (hass: FakeHass) =>
  hass.messages.filter((message) => message.type === "spotnav/get_market_options");
const updates = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/update_settings");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

/** About this card lives in the Settings popover: open that, then press its About button. */
function openAbout(element: Element): void {
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)
    ?.click();
  shadow(element).querySelector<HTMLButtonElement>("[data-about='open']")?.click();
}

describe("the general Settings popover", () => {
  it("opens with its sections, in order, and asks only for the entity configuration", async () => {
    const { hass, element } = await mounted();
    const before = hass.messages.length;
    settingsGeneralButton(element).click();

    const dialog = openDialog(element);
    expect(dialog?.textContent).toContain(translate("en", "settings.overview.title"));
    const sections = Array.from(dialog?.querySelectorAll("[data-section]") ?? []).map((node) =>
      node.getAttribute("data-section"),
    );
    expect(sections).toEqual(["market", "vehicle", "entities", "site", "notifications", "support"]);
    // Every fact shown came from the dashboard already read; the one thing asked of the backend is the
    // entity configuration (administrators only, on its own line in the fake transport).
    expect(hass.messages.length).toBe(before);
    expect(hass.entityMessages.map((message) => message["type"])).toEqual(["spotnav/get_entity_config"]);
  });

  it("names the site slot clearly and states nothing about a site it has not been told", async () => {
    const { element } = await mounted();
    settingsGeneralButton(element).click();
    const site = section(element, "site");
    expect(site?.textContent).toContain(translate("en", "settings.section.site"));
    expect(site?.textContent).toContain(translate("en", "site.none"));
    // No invented site name, count or switch: this fixture is v3, which carries no `site` block at all.
    expect(site?.querySelector("input")).toBeNull();
    expect(site?.querySelector("button")).toBeNull();
  });

  it("does not repeat the capability list: that belongs to About this card", async () => {
    const { element } = await mounted();
    settingsGeneralButton(element).click();
    const dialog = openDialog(element);
    expect(dialog?.querySelector("[data-section='capabilities']")).toBeNull();
    expect(dialog?.querySelectorAll(`.${VISUAL_CLASSES.capabilityItem}`).length).toBe(
      dialog?.querySelectorAll("[data-row]").length,
    );
    expect(dialog?.querySelector("[data-capability]")).toBeNull();
    expect(dialog?.textContent).not.toContain(translate("en", "cap.available"));
    // About this card still has them.
    openAbout(element);
    expect(openDialog(element)?.querySelectorAll("[data-capability]").length).toBe(4);
  });

  it("shows every value on the main page: area, VAT, energy tax, grid fee", async () => {
    const { hass, element } = await mounted();
    const before = hass.messages.length;
    settingsGeneralButton(element).click();
    const market = section(element, "market");
    const row = (key: string) => market?.querySelector(`[data-row="${key}"]`)?.textContent ?? "";
    expect(row("area")).toContain("Malmö");
    expect(row("currency")).toBe("");
    // The fixture's fiscal components are all off: stated as "off", not omitted.
    for (const key of ["vat", "tax", "transfer"]) {
      expect(row(key), key).toContain(translate("en", "settings.value.off"));
    }
    expect(row("vat")).toContain(translate("en", "settings.fiscal.vat"));
    expect(row("tax")).toContain(translate("en", "settings.fiscal.tax"));
    expect(row("transfer")).toContain(translate("en", "settings.fiscal.transfer"));
    // No request was needed to show any of it.
    expect(hass.messages.length).toBe(before);
  });

  it("states applied fiscal figures with their units", async () => {
    const payload = fixture("start_idle");
    const fiscal = payload["fiscal"] as Record<string, Record<string, unknown>>;
    fiscal["vat"] = { ...fiscal["vat"], policy: "suggested", effective_value: 25, unit: "%" };
    fiscal["transfer"] = { ...fiscal["transfer"], policy: "manual", effective_value: 8.75, unit: "öre/kWh" };
    const { element } = await mounted(payload);
    settingsGeneralButton(element).click();
    const market = section(element, "market");
    expect(market?.querySelector("[data-row='vat']")?.textContent).toContain("25 %");
    expect(market?.querySelector("[data-row='transfer']")?.textContent).toContain("8.75 öre/kWh");
  });

  it("opens the area/fiscal editor from its own section, closing the popover behind it", async () => {
    const { hass, element } = await mounted();
    settingsGeneralButton(element).click();
    const marketTrigger = section(element, "market")?.querySelector<HTMLButtonElement>(
      `.${VISUAL_CLASSES.settingsTrigger}`,
    );
    // The button is an action; the area is the row above it (and not repeated on the button).
    expect(marketTrigger?.textContent).toBe(translate("en", "market.edit"));
    expect(section(element, "market")?.querySelector("[data-row='area']")?.textContent).toContain("Malmö · SE4");
    marketTrigger?.click();
    await settle();
    expect(marketReads(hass)).toHaveLength(1);
    expect(openDialog(element)?.textContent).toContain(translate("en", "market.title"));
  });

  it("keeps one overlay open at a time, in both directions", async () => {
    const { element } = await mounted();
    settingsGeneralButton(element).click();
    expect(openDialog(element)?.textContent).toContain(translate("en", "settings.overview.title"));

    // Opening the capability (About) dialog closes the popover.
    shadow(element).querySelector<HTMLButtonElement>("[data-about='open']")?.click();
    expect(openDialog(element)?.textContent).toContain(translate("en", "cap.title"));

    // And opening the popover again closes About.
    settingsGeneralButton(element).click();
    expect(openDialog(element)?.textContent).toContain(translate("en", "settings.overview.title"));
  });

  it("closes on Escape and returns focus to the button that opened it", async () => {
    const { element } = await mounted();
    settingsGeneralButton(element).click();
    expect(openDialog(element)).not.toBeNull();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(openDialog(element)).toBeNull();
    expect(shadow(element).activeElement).toBe(settingsGeneralButton(element));
  });

  it("labels every section in Swedish and Finnish, not only in English", async () => {
    for (const language of ["sv", "fi"] as const) {
      const { element } = await mounted(fixture("start_idle"), true, language);
      settingsGeneralButton(element, language).click();
      const dialog = openDialog(element);
      expect(dialog?.textContent, language).toContain(translate(language, "settings.section.site"));
      expect(dialog?.textContent, language).toContain(translate(language, "site.none"));
      expect(dialog?.textContent, language).toContain(translate(language, "settings.section.vehicle"));
      expect(dialog?.textContent, language).toContain(translate(language, "settings.section.market"));
    }
  });
});

describe("Download debug info", () => {
  const BUNDLE = { bundle_version: 2, chargers: [] };

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function debugButton(element: Element): HTMLButtonElement | null {
    return shadow(element).querySelector<HTMLButtonElement>("[data-download-debug]");
  }

  async function openSettings(element: ReturnType<typeof mountCard>): Promise<void> {
    settingsGeneralButton(element).click();
    await settle();
  }

  it("is offered to administrators only, beside About this card in Support", async () => {
    const admin = await mounted();
    await openSettings(admin.element);
    expect(debugButton(admin.element)?.textContent).toBe(translate("en", "debug.download"));
    const support = section(admin.element, "support");
    expect(Array.from(support?.querySelectorAll("button") ?? []).map((button) => button.textContent)).toEqual([
      translate("en", "settings.about.open"),
      translate("en", "debug.download"),
    ]);

    const reader = await mounted(fixture("start_idle"), false);
    await openSettings(reader.element);
    expect(debugButton(reader.element)).toBeNull();
    // About this card is for everyone: a reader's Support section holds it alone.
    const readerSupport = section(reader.element, "support");
    expect(Array.from(readerSupport?.querySelectorAll("button") ?? []).map((button) => button.textContent)).toEqual([
      translate("en", "settings.about.open"),
    ]);
    expect(section(reader.element, "about")).toBeNull();
  });

  it("asks for the bundle and saves it as spotnav-debug-<date>.json", async () => {
    vi.setSystemTime(new Date(2026, 9, 3, 12, 0, 0));
    const { hass, element } = await mounted();
    await openSettings(element);
    const created: Blob[] = [];
    const win = element.ownerDocument.defaultView as Window & typeof globalThis;
    win.URL.createObjectURL = vi.fn((blob: Blob) => {
      created.push(blob);
      return "blob:debug";
    });
    win.URL.revokeObjectURL = vi.fn();
    const clicked: string[] = [];
    vi.spyOn(win.HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.download);
    });

    debugButton(element)?.click();
    await settle();
    expect(hass.messages.at(-1)).toMatchObject({ type: "spotnav/get_debug_bundle", api_version: 1 });
    expect(debugButton(element)?.disabled).toBe(true);
    expect(debugButton(element)?.textContent).toBe(translate("en", "debug.preparing"));

    hass.resolveNext({ api_version: 1, ok: true, error: null, bundle: BUNDLE });
    await settle();

    expect(clicked).toEqual(["spotnav-debug-2026-10-03.json"]);
    expect(created).toHaveLength(1);
    const saved = JSON.parse(await (created[0] as Blob).text()) as Record<string, unknown>;
    const { client, ...rest } = saved;
    expect(rest).toEqual(BUNDLE);
    // The card running here is recorded beside the backend's versions; a test run has no bundle URL.
    expect(client).toMatchObject({ card_bundle_hash: null, card_version: CARD_VERSION, card_outdated: null });
    expect(debugButton(element)?.disabled).toBe(false);
  });

  it("records an outdated card in the file and says so in Support", async () => {
    setOwnCardBundleHashForTest("11111111");
    try {
      const { hass, element } = await mounted();
      await openSettings(element);
      const created: Blob[] = [];
      const win = element.ownerDocument.defaultView as Window & typeof globalThis;
      win.URL.createObjectURL = vi.fn((blob: Blob) => {
        created.push(blob);
        return "blob:debug";
      });
      win.URL.revokeObjectURL = vi.fn();
      vi.spyOn(win.HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
      const note = (): HTMLElement | null =>
        section(element, "support")?.querySelector<HTMLElement>("[data-card-outdated]") ?? null;
      expect(note()?.hidden).toBe(true);

      debugButton(element)?.click();
      await settle();
      hass.resolveNext({
        api_version: 1,
        ok: true,
        error: null,
        bundle: { ...BUNDLE, versions: { card_bundle_hash: "22222222", card_bundle_hash_served: "33333333" } },
      });
      await settle();

      const saved = JSON.parse(await (created[0] as Blob).text()) as { client: Record<string, unknown> };
      expect(saved.client).toMatchObject({
        card_bundle_hash: "11111111",
        card_outdated: true,
        note: CARD_OUTDATED_NOTE,
      });
      expect(note()?.hidden).toBe(false);
      expect(note()?.textContent).toBe(translate("en", "debug.cardOutdated"));
    } finally {
      setOwnCardBundleHashForTest(null);
    }
  });

  it("says so when the backend refuses", async () => {
    const { hass, element } = await mounted();
    await openSettings(element);

    debugButton(element)?.click();
    await settle();
    hass.resolveNext({ api_version: 1, ok: false, error: "spotnav_not_admin", bundle: null });
    await settle();

    expect(text(element)).toContain(translate("en", "debug.error.notAdmin"));
    expect(debugButton(element)?.disabled).toBe(false);
  });
});

describe("Support says when the card here is older than the one served", () => {
  afterEach(() => {
    setOwnCardBundleHashForTest(null);
  });

  async function opened(served: string, language: Language = "en"): Promise<{ element: Element; hass: FakeHass }> {
    setOwnCardBundleHashForTest("11111111");
    const result = await mounted(fixture("start_idle"), false, language);
    result.hass.cardInfoHandler = async () => ({
      api_version: 1,
      ok: true,
      error: null,
      spotnav_version: "1.0.0",
      card_bundle_hash: served,
    });
    settingsGeneralButton(result.element, language).click();
    await settle();
    return result;
  }

  it("shows the note to any user when the hashes differ, in their language", async () => {
    const { element, hass } = await opened("22222222", "sv");
    expect(hass.cardInfoMessages.at(-1)).toMatchObject({ type: "spotnav/get_card_info", api_version: 1 });
    const note = section(element, "support")?.querySelector<HTMLElement>("[data-card-outdated]");
    expect(note?.hidden).toBe(false);
    expect(note?.textContent).toBe(translate("sv", "debug.cardOutdated"));
  });

  it("stays quiet when they match", async () => {
    const { element } = await opened("11111111");
    expect(section(element, "support")?.querySelector<HTMLElement>("[data-card-outdated]")?.hidden).toBe(true);
  });
});
