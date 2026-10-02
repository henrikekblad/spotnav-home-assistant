// Dialogs must survive refreshes: `render()` defers while a dialog is open and re-renders once every
// dialog has closed (`CardView.onDialogsClosed`). Two refresh periods pass for every dialog the card can
// open, each with a fresh accepted answer, and the dialog stays open with any typed value intact.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import { translate } from "../src/i18n";
import {
  MARKET_API_VERSION,
  SETTINGS_API_VERSION,
  type MarketOptionsV1,
  type SettingsRecord,
} from "../src/types";
import { statusLine } from "./dashboard-fixtures";
import { charger, chargerOf, dashboard, FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

/** Every dialog is one of these overlays; exactly one is ever visible at a time. */
function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((node) => node.closest("[hidden]") === null) ?? null;
}

function mounted(
  payload: Record<string, unknown> = fixture("start_idle"),
  admin = true,
): { hass: FakeHass; element: ReturnType<typeof mountCard> } {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  if (admin) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  return { hass, element };
}

/** Two refresh periods, each answered with a fresh accepted read -- the ordinary refresh policy. */
async function passTwoRefreshPeriods(hass: FakeHass, payload: Record<string, unknown>): Promise<void> {
  for (let tick = 0; tick < 2; tick += 1) {
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    hass.resolveNext(payload);
    await settle();
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("a refresh never closes an open dialog", () => {
  it("keeps the issues dialog open, across two refresh periods, with its content intact", async () => {
    const payload = fixture("start_idle");
    payload["charger"] = { ...chargerOf(payload), available: false };
    payload["status"] = { tone: "blocking", lines: [statusLine("charger_unavailable")] }; // a blocking block -> banner
    const { hass, element } = mounted(payload);
    await settle();
    hass.resolveNext(payload);
    await settle();

    const banner = shadow(element).querySelector<HTMLButtonElement>(".spotnav-banner");
    expect(banner).not.toBeNull();
    banner!.click();
    const before = openDialog(element);
    expect(before?.textContent).toContain(translate("en", "dialog.issues"));

    await passTwoRefreshPeriods(hass, payload);

    const after = openDialog(element);
    expect(after).not.toBeNull();
    expect(after).toBe(before); // the same node: never destroyed and rebuilt
    expect(after?.textContent).toContain(translate("en", "dialog.issues"));
  });

  it("keeps the capability dialog open across two refresh periods", async () => {
    const payload = fixture("start_idle");
    const { hass, element } = mounted(payload);
    await settle();
    hass.resolveNext(payload);
    await settle();

    const help = shadow(element).querySelector<HTMLButtonElement>(
      `[aria-label="${translate("en", "header.info")}"]`,
    );
    help!.click();
    const before = openDialog(element);
    expect(before?.textContent).toContain(translate("en", "cap.title"));

    await passTwoRefreshPeriods(hass, payload);

    const after = openDialog(element);
    expect(after).toBe(before);
    expect(after?.textContent).toContain(translate("en", "cap.title"));
  });

  it("keeps the pause sheet open across two refresh periods", async () => {
    const payload = fixture("start_idle"); // automatic_action: "pause", with real choices
    const { hass, element } = mounted(payload);
    await settle();
    hass.resolveNext(payload);
    await settle();

    const planner = shadow(element).querySelector<HTMLButtonElement>(
      ".spotnav-planner-button[data-action='pause']",
    );
    expect(planner).not.toBeNull();
    planner!.click();
    const before = openDialog(element);
    expect(before?.textContent).toContain(translate("en", "pause.sheetTitle"));

    await passTwoRefreshPeriods(hass, payload);

    const after = openDialog(element);
    expect(after).toBe(before);
    expect(after?.textContent).toContain(translate("en", "pause.sheetTitle"));
  });

  it("keeps the strategy dialog open across two refresh periods", async () => {
    const payload = fixture("start_idle"); // strategy.selected: "cheapest"
    const { hass, element } = mounted(payload);
    await settle();
    hass.resolveNext(payload);
    await settle();

    const strategyButton = shadow(element).querySelector<HTMLButtonElement>(".spotnav-strategy-button");
    expect(strategyButton).not.toBeNull();
    strategyButton!.click();
    const before = openDialog(element);
    expect(before?.textContent).toContain(translate("en", "strategy.dialogTitle"));

    await passTwoRefreshPeriods(hass, payload);

    const after = openDialog(element);
    expect(after).toBe(before);
    expect(after?.textContent).toContain(translate("en", "strategy.dialogTitle"));
  });

  it("keeps the general Settings popover open across two refresh periods", async () => {
    const payload = fixture("start_idle");
    const { hass, element } = mounted(payload);
    await settle();
    hass.resolveNext(payload);
    await settle();

    const settingsGeneral = shadow(element).querySelector<HTMLButtonElement>(
      `[aria-label="${translate("en", "header.settings")}"]`,
    );
    settingsGeneral!.click();
    const before = openDialog(element);
    expect(before?.textContent).toContain(translate("en", "settings.overview.title"));

    await passTwoRefreshPeriods(hass, payload);

    const after = openDialog(element);
    expect(after).toBe(before);
    expect(after?.textContent).toContain(translate("en", "settings.overview.title"));
  });

  it("keeps the settings editor open, with a typed value intact, across two refresh periods", async () => {
    const payload = fixture("start_idle");
    const record: SettingsRecord = {
      revision: 7,
      area_id: "SE4",
      overrides: [],
      phases: 3,
      amps: 10,
      requested_kwh: 20,
      max_periods: 4,
      departure_enabled: true,
      departure_time: "06:30",
      departure_date: null,
      strategy: "cheapest",
      driver: "manual_kwh",
      target: { vehicle_id: null, target_percent: null },
    };
    const settingsAnswer = {
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: { ...record },
      pause: { choice: null, admitted_at: null, expires_at: null },
    };
    const { hass, element } = mounted(payload);
    await settle();
    hass.resolveNext(payload);
    await settle();

    const energyTrigger = shadow(element).querySelector<HTMLButtonElement>(
      ".spotnav-settings-trigger[data-setting='plan']",
    );
    energyTrigger!.click();
    await settle();
    hass.resolveNext(settingsAnswer);
    await settle();

    const before = openDialog(element);
    expect(before?.textContent).toContain(translate("en", "settings.plan.title"));
    const number = before?.querySelector<HTMLInputElement>(".spotnav-settings-input");
    expect(number).not.toBeNull();
    number!.value = "42.5";
    number!.dispatchEvent(new Event("input", { bubbles: true }));
    expect(number!.value).toBe("42.5");

    await passTwoRefreshPeriods(hass, payload);

    const after = openDialog(element);
    expect(after).toBe(before); // never destroyed and rebuilt: the same dialog node
    const stillThere = after?.querySelector<HTMLInputElement>(".spotnav-settings-input");
    expect(stillThere).toBe(number); // the same input node
    expect(stillThere?.value).toBe("42.5"); // the reader's own typed value, not reset
  });

  it("keeps the market editor open, with a typed figure intact, across two refresh periods", async () => {
    const payload = fixture("start_idle");
    const record: SettingsRecord = {
      revision: 7,
      area_id: "SE4",
      overrides: [
        {
          area_id: "SE4",
          vat: { enabled: true, value: 25 },
          tax: { enabled: true, value: 36.5 },
          transfer: { enabled: true, value: 8.75 },
        },
      ],
      phases: 3,
      amps: 10,
      requested_kwh: 20,
      max_periods: 4,
      departure_enabled: false,
      departure_time: "06:30",
      departure_date: null,
      strategy: "cheapest",
      driver: "manual_kwh",
      target: { vehicle_id: null, target_percent: null },
    };
    const settingsAnswer = {
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: { ...record },
      pause: { choice: null, admitted_at: null, expires_at: null },
    };
    const catalogue: MarketOptionsV1 = {
      state: "ready",
      reason: null,
      configured_area: "SE4",
      areas: [
        {
          area_id: "SE4",
          name: "Malmö",
          countries: ["SE"],
          timezone: "Europe/Stockholm",
          currency: "SEK",
          major_unit: "kr",
          minor_unit: "öre",
          suggestions: { vat_percent: 25, tax_minor: 36.5, transfer_minor: 30 },
        },
      ],
    };
    const marketAnswer = { api_version: MARKET_API_VERSION, ...catalogue };

    const { hass, element } = mounted(payload);
    await settle();
    hass.resolveNext(payload);
    await settle();

    const settingsGeneral = shadow(element).querySelector<HTMLButtonElement>(
      `[aria-label="${translate("en", "header.settings")}"]`,
    );
    settingsGeneral!.click();
    await settle();
    const marketTrigger = shadow(element).querySelector<HTMLButtonElement>(
      ".spotnav-settings-trigger[data-setting='market']",
    );
    expect(marketTrigger).not.toBeNull();
    marketTrigger!.click();
    await settle();
    hass.resolveNext(settingsAnswer);
    hass.resolveNext(marketAnswer);
    await settle();

    const before = openDialog(element);
    expect(before?.textContent).toContain(translate("en", "market.title"));
    const taxValue = before?.querySelector<HTMLInputElement>(`input[id$="-tax-value"]`);
    expect(taxValue).not.toBeNull();
    taxValue!.value = "12.3";
    taxValue!.dispatchEvent(new Event("input", { bubbles: true }));
    expect(taxValue!.value).toBe("12.3");

    await passTwoRefreshPeriods(hass, payload);

    const after = openDialog(element);
    expect(after).toBe(before);
    const stillThere = after?.querySelector<HTMLInputElement>(`input[id$="-tax-value"]`);
    expect(stillThere).toBe(taxValue);
    expect(stillThere?.value).toBe("12.3");
  });

  it("applies the deferred refresh the instant the dialog closes", async () => {
    const idle = fixture("start_idle");
    const charging = dashboard({ charger: charger("entry_a", "Renamed Charger") });
    const { hass, element } = mounted(idle);
    await settle();
    hass.resolveNext(idle);
    await settle();

    const help = shadow(element).querySelector<HTMLButtonElement>(
      `[aria-label="${translate("en", "header.info")}"]`,
    );
    help!.click();
    expect(openDialog(element)).not.toBeNull();
    expect(shadow(element).textContent).not.toContain("Renamed Charger");

    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    hass.resolveNext(charging);
    await settle();
    // The refresh landed, but the open dialog was not touched by it.
    expect(openDialog(element)).not.toBeNull();
    expect(shadow(element).textContent).not.toContain("Renamed Charger");

    // Close it (Escape, the same path the close button and the backdrop share).
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await settle();

    expect(openDialog(element)).toBeNull();
    expect(shadow(element).textContent).toContain("Renamed Charger");
  });
});
