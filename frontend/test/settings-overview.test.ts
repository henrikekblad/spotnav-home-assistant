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
import { automationsUsingPath, BLUEPRINTS, BLUEPRINTS_PATH, decodeBlueprintUse } from "../src/automations";
import { decodeSendAnswer, waitText } from "../src/send-debug";
import { FakeHass, mountCard, settingsOffered, shownSections } from "./helpers";

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
    expect(sections).toEqual(["market", "vehicle", "entities", "site", "notifications", "display", "support"]);
    // Every fact shown came from the dashboard already read; the one thing asked of the backend is the
    // entity configuration (administrators only, on its own line in the fake transport).
    expect(hass.messages.length).toBe(before);
    expect(hass.entityMessages.map((message) => message["type"])).toEqual(["spotnav/get_entity_config"]);
  });

  it("names the site slot clearly and states nothing about a site it has not been told", async () => {
    const { element } = await mounted();
    settingsGeneralButton(element).click();
    const site = section(element, "site");
    expect(site?.textContent).toContain(translate("en", "settings.heading.site"));
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
    // Every row is a value row: the label on the left, the value on the right.
    expect(dialog?.querySelectorAll(`.${VISUAL_CLASSES.settingRow}`).length).toBe(
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
    // The area row's value is the button: no separate "Edit" action under it.
    expect(marketTrigger?.textContent).toBe("Malmö · SE4");
    expect(marketTrigger?.closest("[data-row='area']")).not.toBeNull();
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
      expect(dialog?.textContent, language).toContain(translate(language, "settings.heading.site"));
      expect(dialog?.textContent, language).toContain(translate(language, "site.none"));
      expect(dialog?.textContent, language).toContain(translate(language, "settings.section.vehicle"));
      expect(dialog?.textContent, language).toContain(translate(language, "settings.heading.price"));
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
    const actions = support?.querySelectorAll(`.${VISUAL_CLASSES.settingsSupportActions} button`) ?? [];
    expect(Array.from(actions).map((button) => button.textContent)).toEqual([
      translate("en", "settings.about.open"),
      translate("en", "debug.download"),
      translate("en", "sendDebug.open"),
    ]);

    // A reader who is not an administrator is shown only what they may change: Display, and no Support.
    const reader = await mounted(fixture("start_idle"), false);
    expect(settingsOffered(reader.element)).toBe(true);
    await openSettings(reader.element);
    expect(shownSections(reader.element)).toEqual(["display"]);
    expect(debugButton(reader.element)).toBeNull();
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

describe("Send debug info", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const root = (element: Element): ShadowRoot => shadow(element);
  const form = (element: Element): HTMLFormElement | null =>
    root(element).querySelector<HTMLFormElement>("[data-send-debug='form']");
  const field = <T extends HTMLElement>(element: Element, name: string): T =>
    form(element)?.querySelector<T>(`[data-send-debug-field='${name}']`) as T;
  const sends = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/send_debug_info");

  async function openSend(element: Element): Promise<void> {
    settingsGeneralButton(element).click();
    await settle();
    root(element).querySelector<HTMLButtonElement>("[data-send-debug='open']")?.click();
    await settle();
  }

  function fill(element: Element, message: string, email: string, consent: boolean): void {
    field<HTMLTextAreaElement>(element, "message").value = message;
    field<HTMLInputElement>(element, "email").value = email;
    field<HTMLInputElement>(element, "consent").checked = consent;
  }

  async function submit(element: Element): Promise<void> {
    form(element)?.querySelector<HTMLButtonElement>("button[type='submit']")?.click();
    await settle();
  }

  const errorText = (element: Element): string =>
    form(element)?.querySelector<HTMLElement>("[role='alert']")?.textContent ?? "";

  it("reads the answer strictly and says a wait in minutes, or hours beyond two", () => {
    expect(decodeSendAnswer({ api_version: 1, ok: true, error: null, ref: "SN-AB2C" })).toEqual({ ok: true, ref: "SN-AB2C" });
    expect(decodeSendAnswer({ api_version: 1, ok: true, error: null, ref: null })).toBeNull();
    expect(decodeSendAnswer({ api_version: 2, ok: true, ref: "SN-AB2C" })).toBeNull();
    expect(decodeSendAnswer({ api_version: 1, ok: false, error: "spotnav_report_rate_limited", retry_after_s: 60 })).toEqual({
      ok: false,
      code: "spotnav_report_rate_limited",
      retryAfterS: 60,
    });
    expect(waitText("en", 30)).toBe("1 min");
    expect(waitText("en", 3600)).toBe("60 min");
    expect(waitText("en", 7201)).toBe("3 h");
  });

  it("is offered to administrators only", async () => {
    const reader = await mounted(fixture("start_idle"), false);
    settingsGeneralButton(reader.element).click();
    await settle();
    expect(root(reader.element).querySelector("[data-send-debug='open']")).toBeNull();
  });

  it("says what is sent, links the privacy policy and asks for a message, an e-mail address and consent", async () => {
    const { element } = await mounted();
    await openSend(element);

    const dialog = openDialog(element);
    expect(dialog?.textContent).toContain(translate("en", "sendDebug.title"));
    expect(dialog?.textContent).toContain(translate("en", "sendDebug.intro"));
    const link = form(element)?.querySelector<HTMLAnchorElement>("[data-send-debug-intro] a");
    expect(link?.href).toBe("https://spotnav.sensnology.se/privacy");
    expect(link?.target).toBe("_blank");
    expect(field<HTMLTextAreaElement>(element, "message").maxLength).toBe(4000);
    expect(field<HTMLInputElement>(element, "email").type).toBe("email");
    expect(field<HTMLInputElement>(element, "consent").type).toBe("checkbox");
    expect(field<HTMLInputElement>(element, "consent").checked).toBe(false);
    expect(dialog?.textContent).toContain(translate("en", "sendDebug.consent"));
  });

  it("sends nothing without a message or without consent, or with a bad address", async () => {
    const { hass, element } = await mounted();
    await openSend(element);

    fill(element, "   ", "", true);
    await submit(element);
    expect(errorText(element)).toBe(translate("en", "sendDebug.error.message"));

    fill(element, "It stopped", "", false);
    await submit(element);
    expect(errorText(element)).toBe(translate("en", "sendDebug.error.consent"));

    fill(element, "It stopped", "not an address", true);
    await submit(element);
    expect(errorText(element)).toBe(translate("en", "sendDebug.error.email"));

    expect(sends(hass)).toEqual([]);
  });

  it("sends the message and shows the reference", async () => {
    const { hass, element } = await mounted();
    await openSend(element);
    fill(element, "  It stopped at 03:00 ", " me@example.com ", true);
    await submit(element);

    expect(sends(hass)).toHaveLength(1);
    expect(sends(hass)[0]).toMatchObject({
      type: "spotnav/send_debug_info",
      api_version: 1,
      message: "It stopped at 03:00",
      email: "me@example.com",
      client: { card_version: CARD_VERSION },
    });
    hass.resolveNext({ api_version: 1, ok: true, error: null, ref: "SN-7KQ4", retry_after_s: null });
    await settle();

    const sent = form(element)?.querySelector<HTMLElement>("[data-send-debug-sent]");
    expect(sent?.textContent).toBe(translate("en", "sendDebug.sent", { ref: "SN-7KQ4" }));
    expect(sent?.textContent).toContain("SN-7KQ4");
    form(element)?.querySelector<HTMLButtonElement>("[data-send-debug-close]")?.click();
    await settle();
    expect(openDialog(element)?.querySelector("[data-send-debug='form']")).toBeNull();
    // Back on the Settings page it was opened from.
    expect(section(element, "support")).not.toBeNull();
  });

  it("leaves out an empty e-mail address", async () => {
    const { hass, element } = await mounted();
    await openSend(element);
    fill(element, "It stopped", "", true);
    await submit(element);
    expect(sends(hass)[0]).not.toHaveProperty("email");
  });

  it("says why a send failed and suggests the download instead", async () => {
    const { hass, element } = await mounted();
    await openSend(element);
    fill(element, "It stopped", "", true);

    await submit(element);
    hass.resolveNext({ api_version: 1, ok: false, error: "spotnav_report_rate_limited", ref: null, retry_after_s: 7200 });
    await settle();
    expect(errorText(element)).toBe(
      `${translate("en", "sendDebug.error.rateLimited", { wait: "2 h" })} ${translate("en", "sendDebug.error.download")}`,
    );

    await submit(element);
    hass.resolveNext({ api_version: 1, ok: false, error: "spotnav_report_too_large", ref: null, retry_after_s: null });
    await settle();
    expect(errorText(element)).toContain(translate("en", "sendDebug.error.tooLarge"));
    expect(errorText(element)).toContain(translate("en", "sendDebug.error.download"));

    await submit(element);
    hass.resolveNext({ api_version: 1, ok: false, error: "spotnav_report_network", ref: null, retry_after_s: null });
    await settle();
    expect(errorText(element)).toContain(translate("en", "sendDebug.error.network"));

    await submit(element);
    hass.rejectNext({ code: "unauthorized" });
    await settle();
    expect(errorText(element)).toContain(translate("en", "sendDebug.error.notAdmin"));

    // The form stays, filled in, for another try.
    expect(field<HTMLTextAreaElement>(element, "message").value).toBe("It stopped");
    expect(sends(hass)).toHaveLength(4);
  });
});

describe("Support says when the card here is older than the one served", () => {
  afterEach(() => {
    setOwnCardBundleHashForTest(null);
  });

  async function opened(served: string, language: Language = "en"): Promise<{ element: Element; hass: FakeHass }> {
    setOwnCardBundleHashForTest("11111111");
    const result = await mounted(fixture("start_idle"), true, language);
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

  it("shows the note when the hashes differ, in the reader's language", async () => {
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

describe("Automations", () => {
  function automationsRow(element: Element): HTMLButtonElement | null {
    return shadow(element).querySelector<HTMLButtonElement>("[data-section='support'] [data-edit='automations']");
  }

  async function openAutomations(element: ReturnType<typeof mountCard>, language: Language = "en"): Promise<HTMLElement | null> {
    settingsGeneralButton(element, language).click();
    await settle();
    automationsRow(element)?.click();
    await settle();
    return openDialog(element);
  }

  function navigations(element: Element): string[] {
    const win = element.ownerDocument.defaultView as Window & typeof globalThis;
    const paths: string[] = [];
    win.addEventListener("location-changed", () => paths.push(win.location.pathname + win.location.search));
    return paths;
  }

  const ANSWER = (price: number, solar: number) => ({
    api_version: 1,
    ok: true,
    error: null,
    blueprints: [
      { file: "price_setpoint.yaml", path: "spotnav/price_setpoint.yaml", automations: price },
      { file: "solar_surplus_switch.yaml", path: "spotnav/solar_surplus_switch.yaml", automations: solar },
    ],
  });

  it("is a row in Support for administrators only", async () => {
    const admin = await mounted();
    settingsGeneralButton(admin.element).click();
    await settle();
    const row = automationsRow(admin.element);
    expect(row?.closest("[data-row]")?.textContent).toBe(
      translate("en", "settings.automations.label") + translate("en", "settings.automations.value"),
    );

    const reader = await mounted(fixture("start_idle"), false);
    settingsGeneralButton(reader.element).click();
    await settle();
    expect(automationsRow(reader.element)).toBeNull();
  });

  it.each(["en", "sv"] as const)("lists both blueprints, each with a button to Home Assistant's blueprints (%s)", async (language) => {
    const { element } = await mounted(fixture("start_idle"), true, language);
    const paths = navigations(element);
    const dialog = await openAutomations(element, language);

    expect(dialog?.textContent).toContain(translate(language, "settings.automations.label"));
    expect(dialog?.textContent).toContain(translate(language, "automations.intro"));
    const boxes = Array.from(dialog?.querySelectorAll<HTMLElement>("[data-blueprint]") ?? []);
    expect(boxes.map((box) => box.dataset["blueprint"])).toEqual(BLUEPRINTS.map((blueprint) => blueprint.file));
    expect(boxes[0]?.textContent).toContain(translate(language, "automations.priceSetpoint.name"));
    expect(boxes[0]?.textContent).toContain(translate(language, "automations.priceSetpoint.text"));
    expect(boxes[1]?.textContent).toContain(translate(language, "automations.solarSurplus.name"));
    expect(boxes[1]?.textContent).toContain(translate(language, "automations.solarSurplus.text"));

    const button = boxes[1]?.querySelector<HTMLButtonElement>("[data-blueprint-create]");
    expect(button?.textContent).toBe(translate(language, "automations.add"));
    button?.click();
    await settle();
    expect(paths).toEqual([BLUEPRINTS_PATH]);
    expect(openDialog(element)).toBeNull();
  });

  it("says how many automations use each, and nothing for none", async () => {
    const { hass, element } = await mounted(fixture("start_idle"), true, "sv");
    hass.blueprintHandler = async () => ANSWER(2, 0);
    const paths = navigations(element);
    const dialog = await openAutomations(element, "sv");
    expect(hass.blueprintMessages).toEqual([{ type: "spotnav/get_blueprints", api_version: 1 }]);

    const used = dialog?.querySelector<HTMLAnchorElement>("[data-blueprint-use='price_setpoint.yaml']");
    expect(used?.textContent).toBe("Används av 2 automationer");
    expect(dialog?.querySelector("[data-blueprint-use='solar_surplus_switch.yaml']")).toBeNull();
    used?.click();
    await settle();
    expect(paths).toEqual(["/config/automation/dashboard?blueprint=spotnav%2Fprice_setpoint.yaml"]);
    expect(translate("en", "automations.usedBy.one", { count: "1" })).toBe("Used by 1 automation");
  });

  it("shows no count when the answer cannot be read", async () => {
    const { hass, element } = await mounted();
    hass.blueprintHandler = async () => {
      throw new Error("unknown command");
    };
    const dialog = await openAutomations(element);
    expect(dialog?.querySelectorAll("[data-blueprint]").length).toBe(2);
    expect(dialog?.querySelector("[data-blueprint-use]")).toBeNull();
    expect(decodeBlueprintUse({ ...ANSWER(1, 1), api_version: 2 })).toBeNull();
    expect(decodeBlueprintUse({ ...ANSWER(1, 1), ok: false })).toBeNull();
    expect(decodeBlueprintUse(ANSWER(1, 3))).toEqual({ "price_setpoint.yaml": 1, "solar_surplus_switch.yaml": 3 });
  });

  it("links to Home Assistant's own pages only", () => {
    expect(BLUEPRINTS_PATH).toBe("/config/blueprint/dashboard");
    expect(automationsUsingPath("solar_surplus_switch.yaml")).toBe(
      "/config/automation/dashboard?blueprint=spotnav%2Fsolar_surplus_switch.yaml",
    );
    for (const blueprint of BLUEPRINTS) {
      expect(() =>
        readFileSync(join(__dirname, "..", "..", "custom_components", "spotnav", "blueprints", "automation", blueprint.file)),
      ).not.toThrow();
    }
  });
});
