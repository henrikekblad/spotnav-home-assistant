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

const line = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>("button[data-vehicle-line]");
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
    const { element } = await mounted(withSoc({ value: 92, vehicle_name: "e-Outback" }, { driver: "manual_kwh" }));
    const button = line(element)!;
    expect(button.textContent).toBe("e-Outback· 92 %");
    expect(button.querySelector("svg")).not.toBeNull();
    expect(button.title).toBe("");
    expect(button.getAttribute("aria-label")).toBe("e-Outback, 92 %. Choose which vehicle to charge");
    const name = shadow(element).querySelector(".spotnav-header .spotnav-name")!;
    expect(name.nextElementSibling).toBe(button);
  });

  it("shows the target beside the charge while the target drives the plan", async () => {
    const { element } = await mounted(withSoc({ value: 62 }));
    expect(line(element)!.textContent).toContain("62 % → 80 %");
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
    const { element } = await mounted(withSoc({ value: 62, age_s: 7200 }, { driver: "manual_kwh" }), "sv");
    expect(line(element)!.getAttribute("aria-label")).toContain("Välj vilket fordon som ska laddas");
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

describe("the vehicle dialog", () => {
  it("lists every vehicle with its charge, the planned one selected", async () => {
    const { element } = await mounted(twoVehicles());
    line(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    expect(dialog.querySelector("h3")?.textContent).toBe("Which vehicle should be charged?");
    expect(radios(element).map((radio) => [radio.value, radio.checked, radio.disabled])).toEqual([
      ["vehicle_ev6", true, false],
      ["vehicle_niro", false, false],
    ]);
    const rows = Array.from(dialog.querySelectorAll(".spotnav-vehicle-choice")).map((row) => row.textContent);
    expect(rows).toEqual(["EV640 %", "Niro55 %"]);
  });

  it("writes only target.vehicle_id of a freshly read record, under its revision", async () => {
    const { hass, element } = await mounted(twoVehicles());
    line(element)!.click();
    await settle();
    const niro = radios(element)[1]!;
    niro.checked = true;
    niro.dispatchEvent(new Event("change"));
    await settle();
    expect(openDialog(element)).toBeNull();
    hass.resolveNext(answer(record({ revision: 9, amps: 16 })));
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings")!;
    expect(update["expected_revision"]).toBe(9);
    expect(update["settings"]).toMatchObject({
      amps: 16,
      driver: "target_soc",
      target: { vehicle_id: "vehicle_niro", target_percent: 80 },
    });
  });

  it("writes nothing when the planned vehicle is chosen again", async () => {
    const { hass, element } = await mounted(twoVehicles());
    line(element)!.click();
    await settle();
    const ev6 = radios(element)[0]!;
    ev6.dispatchEvent(new Event("change"));
    await settle();
    expect(hass.messages.some((message) => message.type === "spotnav/update_settings")).toBe(false);
  });

  it("is read-only for a reader who is not an administrator", async () => {
    const { hass, element } = await mounted(twoVehicles(), "en", false);
    expect(line(element)).not.toBeNull();
    line(element)!.click();
    await settle();
    expect(radios(element).every((radio) => radio.disabled)).toBe(true);
    expect(radios(element).map((radio) => radio.checked)).toEqual([true, false]);
    expect(hass.messages.some((message) => message.type === "spotnav/update_settings")).toBe(false);
  });

  it("still opens with a single vehicle and shows just that one", async () => {
    const payload = twoVehicles();
    payload["vehicles"] = payload["vehicles"].slice(0, 1);
    const { element } = await mounted(payload);
    line(element)!.click();
    await settle();
    expect(radios(element).map((radio) => radio.value)).toEqual(["vehicle_ev6"]);
  });
});
