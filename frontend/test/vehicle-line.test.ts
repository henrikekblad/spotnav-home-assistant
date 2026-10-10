// The vehicle line under the charger name and the dialog that switches the planned vehicle.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "soc_charger" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const twoVehicles = (): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, "target_soc_two_vehicles.json"), "utf8")) as Record<string, any>;

const record = (overrides: Partial<SettingsRecord> = {}): SettingsRecord => ({
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
  target: { vehicle_id: "vehicle_ev6", target_percent: 80 },
  ...overrides,
});
const answer = (settings: SettingsRecord) => ({
  api_version: SETTINGS_API_VERSION,
  ok: true,
  error: null,
  settings: { ...settings },
  pause: { choice: null, admitted_at: null, expires_at: null },
});

const settle = (): Promise<void> => vi.advanceTimersByTimeAsync(0).then(() => undefined);

async function mounted(payload: Record<string, unknown>, language = "en", admin = true) {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: admin };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

const line = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>("[data-vehicle-line]");
const openDialog = (element: Element): HTMLElement | null =>
  Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find(
    (dialog) => dialog.closest("[hidden]") === null,
  ) ?? null;
const radios = (element: Element): HTMLInputElement[] =>
  Array.from(openDialog(element)?.querySelectorAll<HTMLInputElement>("input[type='radio']") ?? []);

function withSoc(changes: Record<string, unknown>, settings: Record<string, unknown> = {}): Record<string, any> {
  const payload = twoVehicles();
  Object.assign(payload["soc"], changes);
  Object.assign(payload["settings"], settings);
  return payload;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the vehicle line", () => {
  it("shows the planned vehicle and its charge under the charger name", async () => {
    // One car at this charger: the line only states it (with two it is Byt bil, `vehicle-identification.test`).
    const { element } = await mounted(
      withSoc({ value: 92, vehicle_name: "e-Outback" }, { driver: "manual_kwh", vehicle_ids: ["vehicle_ev6"] }),
    );
    const button = line(element)!;
    expect(button.textContent).toBe("e-Outback· 92 %");
    expect(button.querySelector("svg")).not.toBeNull();
    expect(button.title).toBe("");
    expect(button.getAttribute("aria-label")).toBe("e-Outback, 92 %");
    const name = shadow(element).querySelector(".spotnav-header .spotnav-name")!;
    expect(name.nextElementSibling).toBe(button);
  });

  it("joins only the parts that exist, never with a leading or doubled separator", async () => {
    const parts = async (changes: Record<string, unknown>, connection: boolean) => {
      const payload = withSoc(changes, { driver: "manual_kwh" });
      if (!connection) {
        payload["connection"] = { state: "unknown" };
      }
      const { element } = await mounted(payload);
      const spans = Array.from(line(element)!.querySelectorAll("span")).map((span) => span.textContent ?? "");
      return spans.join(" ").replace(/\s+/g, " ").trim();
    };
    for (const name of [null, "", "  "]) {
      const text = await parts({ value: 64, vehicle_name: name }, true);
      expect(text.startsWith("\u00b7")).toBe(false);
      expect(text).not.toContain("\u00b7 \u00b7");
      expect(text.startsWith("64 %")).toBe(true);
    }
    const named = await parts({ value: 64, vehicle_name: "Kia" }, false);
    expect(named).toBe("Kia \u00b7 64 %");
    const old = await parts({ value: 64, vehicle_name: null, age_s: 7200, estimated: false }, false);
    expect(old).toBe("64 % \u00b7 2 h ago");
  });

  it("words the charge as a share of the target while the target drives the plan", async () => {
    const { element } = await mounted(withSoc({ value: 62 }));
    expect(line(element)!.textContent).toContain("62 % of 80 % target");
    expect(line(element)!.textContent).not.toContain("→");
    // Above the target, the line never reads as if the level would drop to it.
    const above = await mounted(withSoc({ value: 100 }), "sv");
    expect(line(above.element)!.textContent).toContain("100 % av 80 % mål");
  });

  it.each([
    ["sv", "62 % av 80 % mål"],
    ["en", "62 % of 80 % target"],
    ["da", "62 % af 80 % mål"],
    ["nb", "62 % av 80 % mål"],
    ["fi", "62 % / 80 % tavoite"],
    ["de", "62 % von 80 % Ziel"],
    ["nl", "62 % van 80 % doel"],
    ["es", "62 % de un objetivo de 80 %"],
    ["fr", "62 % sur un objectif de 80 %"],
  ])("words the charge of the target in %s", async (language, words) => {
    const { element } = await mounted(withSoc({ value: 62 }), language as "en");
    const text = (line(element)!.textContent ?? "").replace(/\s/g, " ");
    expect(text).toContain(words);
  });

  it("keeps the estimate's ~ before the level of the target", async () => {
    const { element } = await mounted(withSoc({ value: 62, estimated: true, age_s: 12 * 60 }));
    expect(line(element)!.textContent).toContain("~62 % of 80 % target");
  });

  it("marks an estimate with ~ and says in a tooltip how old the reading is", async () => {
    const { element } = await mounted(withSoc({ value: 62, estimated: true, age_s: 12 * 60 }, { driver: "manual_kwh" }));
    const button = line(element)!;
    expect(button.textContent).toContain("~62 %");
    expect(button.title).toBe("Estimated between readings, read 12 min ago");
    expect(button.querySelector(".spotnav-vehicle-line-age")).toBeNull();
  });

  it("adds the age of a stale reading, but not of a fresh one or of an estimate", async () => {
    const stale = await mounted(withSoc({ value: 62, age_s: 3 * 3600 + 120 }, { driver: "manual_kwh" }));
    expect(line(stale.element)!.querySelector(".spotnav-vehicle-line-age")?.textContent).toBe("· 3 h ago");
    const fresh = await mounted(withSoc({ value: 62, age_s: 3600 }, { driver: "manual_kwh" }));
    expect(line(fresh.element)!.querySelector(".spotnav-vehicle-line-age")).toBeNull();
  });

  it("is in the card's language", async () => {
    const { element } = await mounted(
      withSoc({ value: 62, age_s: 7200 }, { driver: "manual_kwh", vehicle_ids: ["vehicle_ev6"] }),
      "sv",
    );
    expect(line(element)!.getAttribute("aria-label")).toBe("EV6, 62 %, för 2 h sedan");
    expect(line(element)!.textContent).toContain("för 2 h sedan");
  });

  it.each<[string, Record<string, unknown>]>([
    ["no vehicle is chosen", { vehicle_id: null, vehicle_name: null }],
    ["there is no charge reading", { value: null, missing: ["soc"] }],
  ])("is not rendered at all when %s", async (_name, changes) => {
    const { element } = await mounted(withSoc(changes));
    expect(line(element)).toBeNull();
    expect(shadow(element).querySelector(".spotnav-name-block")).toBeNull();
    expect(shadow(element).querySelector(".spotnav-header .spotnav-name")?.textContent).toBe("Wallbox");
  });

  it("is not rendered without a soc block", async () => {
    const payload = twoVehicles();
    payload["soc"] = null;
    const { element } = await mounted(payload);
    expect(line(element)).toBeNull();
  });
});

describe("the charger status in the header line", () => {
  const withConnection = (state: string, soc: Record<string, unknown> = {}): Record<string, any> => {
    const payload = withSoc({ value: 96, vehicle_name: "EV6", ...soc }, { driver: "manual_kwh" });
    payload["connection"] = { state, source: "sensor.charger_status" };
    return payload;
  };
  const status = (element: Element): Element | null => shadow(element).querySelector("[data-connection]");

  it.each<[string, string, string]>([
    ["disconnected", "en", "Not connected"],
    ["connected", "en", "Connected"],
    ["charging", "en", "Charging"],
    ["paused", "en", "Paused"],
    ["finished", "en", "Finished"],
    ["error", "en", "Error"],
    ["disconnected", "sv", "Ej ansluten"],
    ["connected", "sv", "Ansluten"],
    ["charging", "sv", "Laddar"],
    ["paused", "sv", "Pausad"],
    ["finished", "sv", "Klar"],
    ["error", "sv", "Fel"],
    ["connected", "da", "Tilsluttet"],
    ["connected", "nb", "Tilkoblet"],
    ["connected", "fi", "Kytketty"],
  ])("appends the %s status after the charge in %s", async (state, language, words) => {
    const { element } = await mounted(withConnection(state), language as "en");
    expect(line(element)!.textContent).toBe(`EV6· 96 %· ${words}`);
    expect(status(element)!.textContent).toBe(`· ${words}`);
  });

  it("marks an error in the warning colour only", async () => {
    const error = await mounted(withConnection("error"));
    expect(status(error.element)!.classList.contains("spotnav-connection-error")).toBe(true);
    const fine = await mounted(withConnection("charging"));
    expect(status(fine.element)!.classList.contains("spotnav-connection-error")).toBe(false);
  });

  it("shows nothing for an unknown state, a missing block or an unreadable one", async () => {
    const unknown = await mounted(withConnection("unknown"));
    expect(status(unknown.element)).toBeNull();
    expect(line(unknown.element)!.textContent).toBe("EV6· 96 %");
    const missing = withConnection("charging");
    delete missing["connection"];
    expect(status((await mounted(missing)).element)).toBeNull();
    const bad = withConnection("charging");
    bad["connection"] = { state: "levitating", source: null };
    expect(status((await mounted(bad)).element)).toBeNull();
  });

  it("shows only the status, under the name, when there is no vehicle", async () => {
    const { element } = await mounted(withConnection("disconnected", { vehicle_id: null, vehicle_name: null, value: null }));
    expect(line(element)).toBeNull();
    const block = shadow(element).querySelector(".spotnav-name-block")!;
    expect(block.querySelector(".spotnav-name")?.textContent).toBe("Wallbox");
    expect(status(element)!.textContent).toBe("Not connected");
    expect(block.lastElementChild).toBe(status(element));
  });
});

describe("the vehicle line with one car at the charger", () => {
  it("is no button and has no swap icon: there is nothing to change at this charger", async () => {
    for (const admin of [true, false]) {
      document.body.innerHTML = "";
      const payload = twoVehicles();
      payload["settings"]["vehicle_ids"] = ["vehicle_ev6"];
      const { element } = await mounted(payload, "en", admin);
      expect(line(element)?.tagName).not.toBe("BUTTON");
      expect(line(element)?.querySelector("[data-icon='swap']")).toBeNull();
      line(element)!.click();
      await settle();
      expect(openDialog(element)).toBeNull();
    }
  });
});
