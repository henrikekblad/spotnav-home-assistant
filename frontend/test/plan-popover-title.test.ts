// The Plan popover as the app's planning card: the title names the car the plan is for ("Charging plan for
// EV6"), following the Vehicle select before anything is saved, and the energy-or-target choice is one
// "Charge by" value row whose tap toggles the mode.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(name = "target_soc_estimated"): Record<string, any> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, any>;
}

function aRecord(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 1,
    amps: 10,
    requested_kwh: 20,
    max_periods: 1,
    departure_enabled: false,
    departure_time: "08:00",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "target_soc",
    target: { vehicle_id: "<id>", target_percent: 80 },
    ...overrides,
  };
}

const success = (record: SettingsRecord): Record<string, unknown> => ({
  api_version: SETTINGS_API_VERSION,
  ok: true,
  error: null,
  settings: { ...record },
  pause: { choice: null, admitted_at: null, expires_at: null },
});

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function openPlan(payload: Record<string, unknown>, record: SettingsRecord, language = "en", admin = true) {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: admin };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
  await settle();
  hass.resolveNext(success(record));
  await settle();
  return { hass, element };
}

function dlg(element: Element): HTMLElement {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null)!;
}

const title = (element: Element): string => dlg(element).querySelector(".spotnav-dialog-title")?.textContent ?? "";
const chargeByRow = (element: Element) => dlg(element).querySelector<HTMLElement>("[data-row='charge_by']");
const chargeByButton = (element: Element) => chargeByRow(element)?.querySelector<HTMLButtonElement>("button") ?? null;
const updates = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/update_settings");

/** Two cars to choose between and no car settled yet: the select starts on "Not chosen". */
function twoCars(): Record<string, any> {
  const payload = fixture();
  Object.assign(payload["soc"], {
    vehicle_id: null,
    vehicle_name: null,
    vehicles: [
      { id: "car-1", name: "EV6" },
      { id: "car-2", name: "Niro" },
    ],
    missing: ["vehicle"],
  });
  payload["vehicles"] = [];
  payload["vehicle_choices"] = [];
  payload["target_vehicle_id"] = null;
  return payload;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the Plan popover's title", () => {
  it("names the planned car in every language", async () => {
    const { element } = await openPlan(fixture(), aRecord());
    expect(title(element)).toBe("Charging plan for EV6");
    const sv = await openPlan(fixture(), aRecord(), "sv");
    expect(title(sv.element)).toBe("Laddplan för EV6");
    expect(translate("de", "settings.plan.titleFor", { name: "EV6" })).toBe("Ladeplan für EV6");
  });

  it("follows the Vehicle select as it changes, before anything is saved", async () => {
    const { hass, element } = await openPlan(twoCars(), aRecord({ target: { vehicle_id: null, target_percent: 80 } }));
    // "Not chosen" and no car known otherwise: the plain title.
    expect(title(element)).toBe("Charging plan");
    const select = dlg(element).querySelector<HTMLSelectElement>("select[data-soc='vehicle-choice']")!;
    select.value = "car-2";
    select.dispatchEvent(new Event("change"));
    expect(title(element)).toBe("Charging plan for Niro");
    select.value = "car-1";
    select.dispatchEvent(new Event("change"));
    expect(title(element)).toBe("Charging plan for EV6");
    expect(updates(hass)).toHaveLength(0);
  });

  it("names the identified car in energy mode, where the select is hidden", async () => {
    const payload = fixture();
    payload["identification"] = {
      state: "decided",
      method: "plug",
      vehicle_id: "car-2",
      since: null,
      candidates: [{ vehicle_id: "car-2", name: "Niro", likely: true }],
    };
    const { element } = await openPlan(payload, aRecord({ driver: "manual_kwh" }));
    expect(title(element)).toBe("Charging plan for Niro");
  });

  it("names the target car in energy mode when no car is identified", async () => {
    const { element } = await openPlan(fixture(), aRecord({ driver: "manual_kwh" }));
    expect(title(element)).toBe("Charging plan for EV6");
  });

  it("keeps the plain title when no car is known", async () => {
    const { element } = await openPlan(
      fixture("cheapest_no_site"),
      aRecord({ driver: "manual_kwh", target: { vehicle_id: null, target_percent: null } }),
    );
    expect(title(element)).toBe("Charging plan");
  });

  it("goes back to the planned car when the mode is switched to energy", async () => {
    const payload = twoCars();
    payload["target_vehicle_id"] = null;
    const { element } = await openPlan(payload, aRecord({ target: { vehicle_id: null, target_percent: 80 } }));
    const select = dlg(element).querySelector<HTMLSelectElement>("select[data-soc='vehicle-choice']")!;
    select.value = "car-2";
    select.dispatchEvent(new Event("change"));
    expect(title(element)).toBe("Charging plan for Niro");
    chargeByButton(element)!.click();
    expect(title(element)).toBe("Charging plan");
  });
});

describe("the Charge by row", () => {
  it("shows the mode as a value row, the value in the accent colour and tappable", async () => {
    const { element } = await openPlan(fixture(), aRecord({ driver: "manual_kwh" }), "sv");
    const row = chargeByRow(element)!;
    expect(row.classList.contains("spotnav-setting-row")).toBe(true);
    expect(row.querySelector(".spotnav-setting-row-label")?.textContent).toBe("Ladda efter");
    const button = chargeByButton(element)!;
    expect(button.textContent).toBe("Energi · kWh");
    expect(button.classList.contains("spotnav-setting-row-editable")).toBe(true);
    expect(dlg(element).querySelector("input[type='radio'][name$='-mode']")).toBeNull();
  });

  it("toggles to the target and saves the driver and target as the radios did", async () => {
    const { hass, element } = await openPlan(
      fixture(),
      aRecord({ driver: "manual_kwh", target: { vehicle_id: "car-1", target_percent: null } }),
    );
    chargeByButton(element)!.click();
    expect(chargeByButton(element)!.textContent).toBe("Target SoC · %");
    expect(chargeByRow(element)!.dataset["mode"]).toBe("target_soc");
    expect(dlg(element).querySelector<HTMLElement>("[data-part='energy']")?.hidden).toBe(true);
    expect(dlg(element).querySelector<HTMLElement>("[data-part='soc']")?.hidden).toBe(false);
    // Nothing is written until Save.
    expect(updates(hass)).toHaveLength(0);
    dlg(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
    await settle();
    expect(updates(hass)).toHaveLength(1);
    const body = updates(hass)[0]!["settings"] as Record<string, any>;
    expect(body["driver"]).toBe("target_soc");
    expect(body["target"]).toEqual({ vehicle_id: "car-1", target_percent: 80 });
    expect(body["requested_kwh"]).toBe(20);
  });

  it("toggles back to energy and saves energy", async () => {
    const { hass, element } = await openPlan(fixture(), aRecord());
    expect(chargeByButton(element)!.textContent).toBe("Target SoC · %");
    chargeByButton(element)!.click();
    expect(chargeByButton(element)!.textContent).toBe("Energy · kWh");
    expect(dlg(element).querySelector<HTMLElement>("[data-part='energy']")?.hidden).toBe(false);
    expect(dlg(element).querySelector<HTMLElement>("[data-part='soc']")?.hidden).toBe(true);
    dlg(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
    await settle();
    const body = updates(hass)[0]!["settings"] as Record<string, any>;
    expect(body["driver"]).toBe("manual_kwh");
  });

  it("is hidden when only energy is possible, as in the app", async () => {
    const { element } = await openPlan(
      fixture("cheapest_no_site"),
      aRecord({ driver: "manual_kwh", target: { vehicle_id: null, target_percent: null } }),
    );
    expect(chargeByRow(element)).toBeNull();
    expect(dlg(element).querySelector("[data-soc='need-sensor']")).not.toBeNull();
    expect(dlg(element).querySelector<HTMLElement>("[data-part='energy']")?.hidden).toBe(false);
  });

  it("is plain text for a reader who may not change the plan", async () => {
    const { element } = await openPlan(fixture(), aRecord(), "en", false);
    expect(chargeByRow(element)).not.toBeNull();
    expect(chargeByButton(element)).toBeNull();
    expect(chargeByRow(element)!.textContent).toContain("Target SoC · %");
  });
});
