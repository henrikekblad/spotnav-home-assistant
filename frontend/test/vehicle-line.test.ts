// The car button over the action bar, the reading's age in the dialog it opens, and the charger's status after its name.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { VISUAL_STYLES } from "../src/visual-styles";
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

const car = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>("[data-car-button]");
/** The button's visible car words (the line under the caption): "EV6 · 62 %". */
const carWords = (element: Element): string =>
  (car(element)?.querySelector(".spotnav-bar-value")?.textContent ?? "").replace(/\s/g, " ");
const header = (element: Element): HTMLElement => shadow(element).querySelector<HTMLElement>(".spotnav-header")!;
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

describe("the car button", () => {
  /** One car at this charger: the button opens the car's reading (with two it is Byt bil, `vehicle-identification.test`). */
  const oneCar = (soc: Record<string, unknown>, settings: Record<string, unknown> = {}) =>
    withSoc(soc, { driver: "manual_kwh", vehicle_ids: ["vehicle_ev6"], ...settings });

  it("shows the planned vehicle and its charge right over the action bar, not in the header", async () => {
    const { element } = await mounted(oneCar({ value: 92, vehicle_name: "e-Outback" }));
    const button = car(element)!;
    expect(button.querySelector(".spotnav-bar-caption")?.textContent).toBe("Car");
    expect(button.querySelector(".spotnav-car-button-name")?.textContent).toBe("e-Outback");
    expect(carWords(element)).toBe("e-Outback · 92 %");
    expect(button.querySelector("svg")).not.toBeNull();
    // Its own full-width row at the top of the action bar's block, the cells after it.
    expect(button.parentElement?.classList.contains("spotnav-action-bar-wrap")).toBe(true);
    expect(button.nextElementSibling?.classList.contains("spotnav-action-bar")).toBe(true);
    expect(header(element).textContent).not.toContain("e-Outback");
    expect(header(element).textContent).not.toContain("92 %");
  });

  it("is a real button with a label, reached and opened from the keyboard", async () => {
    const { element } = await mounted(oneCar({ value: 92, vehicle_name: "e-Outback" }));
    const button = car(element)!;
    expect(button.tagName).toBe("BUTTON");
    expect(button.type).toBe("button");
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-haspopup")).toBe("dialog");
    expect(button.getAttribute("aria-label")).toBe("Car. e-Outback, 92 %");
    // A native button: Tab reaches it and Enter or Space press it. The dialog's Escape gives the focus back to it.
    button.focus();
    expect(shadow(element).activeElement).toBe(button);
    button.click();
    await settle();
    expect(openDialog(element)).not.toBeNull();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    expect(openDialog(element)).toBeNull();
    expect(shadow(element).activeElement).toBe(button);
  });

  it("joins only the parts that exist, never with a leading or doubled separator", async () => {
    for (const name of [null, "", "  "]) {
      document.body.innerHTML = "";
      const { element } = await mounted(withSoc({ value: 64, vehicle_name: name }, { driver: "manual_kwh" }));
      expect(carWords(element)).toBe("64 %");
    }
    const named = await mounted(withSoc({ value: 64, vehicle_name: "Kia", age_s: 7200 }, { driver: "manual_kwh" }));
    expect(carWords(named.element)).toBe("Kia · 64 %");
  });

  it("words the charge as a share of the target while the target drives the plan", async () => {
    const { element } = await mounted(withSoc({ value: 62 }));
    expect(carWords(element)).toContain("62 % of 80 % target");
    expect(carWords(element)).not.toContain("→");
    // Above the target, the line never reads as if the level would drop to it.
    const above = await mounted(withSoc({ value: 100 }), "sv");
    expect(carWords(above.element)).toContain("100 % av 80 % mål");
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
    expect(carWords(element)).toContain(words);
  });

  it("keeps the estimate's ~ before the level of the target", async () => {
    const { element } = await mounted(withSoc({ value: 62, estimated: true, age_s: 12 * 60 }));
    expect(carWords(element)).toContain("~62 % of 80 % target");
  });

  it.each<[string, Record<string, unknown>]>([
    ["no vehicle is chosen", { vehicle_id: null, vehicle_name: null }],
    ["there is no charge reading", { value: null, missing: ["soc"] }],
  ])("is not rendered at all when %s", async (_name, changes) => {
    const { element } = await mounted(withSoc(changes));
    expect(car(element)).toBeNull();
    expect(shadow(element).querySelector(".spotnav-name-block")).toBeNull();
    expect(shadow(element).querySelector(".spotnav-header .spotnav-name")?.textContent).toBe("Wallbox");
  });

  it("is not rendered without a soc block", async () => {
    const payload = twoVehicles();
    payload["soc"] = null;
    const { element } = await mounted(payload);
    expect(car(element)).toBeNull();
  });
});

describe("the reading's age", () => {
  const oneCar = (soc: Record<string, unknown>) =>
    withSoc({ value: 62, vehicle_name: "EV6", ...soc }, { driver: "manual_kwh", vehicle_ids: ["vehicle_ev6"] });

  it("is in the dialog the button opens, and nowhere on the card", async () => {
    const { element } = await mounted(oneCar({ age_s: 3 * 3600 + 120 }));
    const card = shadow(element).querySelector<HTMLElement>(".spotnav-card")!;
    expect(card.textContent).not.toContain("3 h ago");
    expect(car(element)!.getAttribute("aria-label")).not.toContain("ago");
    expect(car(element)!.title).toBe("");
    car(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    expect(dialog.querySelector("[data-reading='vehicle_ev6']")?.textContent).toBe("62 % · read 3 h ago");
    // The car's name is the dialog's title; nothing to choose at a one-car charger.
    expect(dialog.textContent).toContain("EV6");
    expect(dialog.querySelectorAll("input[type='radio']")).toHaveLength(0);
  });

  it("is said for a fresh reading and for an estimate too, the estimate with its ~", async () => {
    const fresh = await mounted(oneCar({ age_s: 5 * 60 }));
    car(fresh.element)!.click();
    await settle();
    expect(openDialog(fresh.element)!.querySelector("[data-reading]")?.textContent).toBe("62 % · read 5 min ago");
    document.body.innerHTML = "";
    const estimate = await mounted(oneCar({ age_s: 12 * 60, estimated: true }));
    expect(carWords(estimate.element)).toBe("EV6 · ~62 %");
    car(estimate.element)!.click();
    await settle();
    expect(openDialog(estimate.element)!.querySelector("[data-reading]")?.textContent).toBe("~62 % · read 12 min ago");
  });

  it.each<[string, string]>([
    ["en", "62 % · read 2 h ago"],
    ["sv", "62 % · avläst för 2 h sedan"],
    ["nb", "62 % · avlest"],
    ["da", "62 % · aflæst"],
    ["fi", "62 % · luettu"],
    ["de", "62 % · gelesen"],
    ["nl", "62 % · gelezen"],
    ["fr", "62 % · relevé"],
    ["es", "62 % · leído"],
  ])("is in the card's language (%s)", async (language, words) => {
    const { element } = await mounted(oneCar({ age_s: 7200 }), language as "en");
    car(element)!.click();
    await settle();
    expect((openDialog(element)!.querySelector("[data-reading]")?.textContent ?? "").replace(/\s/g, " ")).toContain(words);
  });

  it("is under the planned car's radio in Change car", async () => {
    const { element } = await mounted(withSoc({ value: 62, vehicle_name: "EV6", age_s: 7200 }, { driver: "manual_kwh" }));
    car(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    const reading = dialog.querySelector<HTMLElement>("[data-reading='vehicle_ev6']")!;
    expect(reading.textContent).toBe("62 % · read 2 h ago");
    expect(reading.previousElementSibling?.querySelector("input")?.value).toBe("vehicle_ev6");
    expect(dialog.querySelectorAll("[data-reading]")).toHaveLength(1);
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
  ])("puts the %s status after the charger's name in %s, not on the vehicle line", async (state, language, words) => {
    const { element } = await mounted(withConnection(state), language as "en");
    expect(carWords(element)).toBe("EV6 · 96 %");
    expect(status(element)!.textContent).toBe(`· ${words}`);
    const row = status(element)!.parentElement!;
    expect(row.classList.contains("spotnav-name-row")).toBe(true);
    expect(row.firstElementChild?.textContent).toBe("Wallbox");
    expect(row.textContent).toBe(`Wallbox· ${words}`);
    expect(car(element)!.contains(status(element))).toBe(false);
    expect(car(element)!.getAttribute("aria-label")).not.toContain(words);
  });

  it("reads the status word without its leading dot", async () => {
    const { element } = await mounted(withConnection("connected"), "en");
    const dot = status(element)!.querySelector(".spotnav-connection-word > span")!;
    expect(dot.textContent).toBe("\u00b7");
    expect(dot.getAttribute("aria-hidden")).toBe("true");
  });

  it.each<[string, string]>([
    ["en", "Waiting"],
    ["sv", "Väntar"],
    ["da", "Venter"],
    ["de", "Wartet"],
    ["es", "En espera"],
    ["fi", "Odottaa"],
    ["fr", "En attente"],
    ["nb", "Venter"],
    ["nl", "Wacht"],
  ])("says waiting in %s when the backend shows a finished charger as waiting for the plan", async (language, words) => {
    const payload = withConnection("finished");
    payload["connection"]["shown"] = "waiting";
    const { element } = await mounted(payload, language as "en");
    expect(status(element)!.textContent).toBe(`· ${words}`);
    expect(status(element)!.getAttribute("data-connection")).toBe("waiting");
  });

  it("shows what the backend shows: finished only when the backend says finished", async () => {
    const finished = withConnection("finished");
    finished["connection"]["shown"] = "finished";
    expect(status((await mounted(finished, "sv")).element)!.textContent).toBe("· Klar");
    document.body.innerHTML = "";
    // An older backend without `shown`: the charger's own word.
    expect(status((await mounted(withConnection("finished"), "sv")).element)!.textContent).toBe("· Klar");
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
    expect(carWords(unknown.element)).toBe("EV6 · 96 %");
    const missing = withConnection("charging");
    delete missing["connection"];
    expect(status((await mounted(missing)).element)).toBeNull();
    const bad = withConnection("charging");
    bad["connection"] = { state: "levitating", source: null };
    expect(status((await mounted(bad)).element)).toBeNull();
    // A shown word from a newer backend: the charger's own state is shown instead, not nothing.
    const badShown = withConnection("finished");
    badShown["connection"]["shown"] = "levitating";
    expect(status((await mounted(badShown)).element)?.getAttribute("data-connection")).toBe("finished");
    const extra = withConnection("finished");
    extra["connection"]["other"] = 1;
    expect(status((await mounted(extra)).element)).toBeNull();
  });

  it("shows the status after the name when there is no vehicle", async () => {
    const { element } = await mounted(withConnection("disconnected", { vehicle_id: null, vehicle_name: null, value: null }));
    expect(car(element)).toBeNull();
    const block = shadow(element).querySelector(".spotnav-name-block")!;
    expect(block.querySelector(".spotnav-name")?.textContent).toBe("Wallbox");
    expect(status(element)!.textContent).toBe("· Not connected");
    expect(status(element)!.previousElementSibling).toBe(block.querySelector(".spotnav-name"));
  });
});

describe("the car button with one car at the charger", () => {
  it("opens the car's reading, not Change car: there is nothing to change at this charger", async () => {
    for (const admin of [true, false]) {
      document.body.innerHTML = "";
      const payload = twoVehicles();
      payload["settings"]["vehicle_ids"] = ["vehicle_ev6"];
      const { element } = await mounted(payload, "en", admin);
      expect(car(element)!.getAttribute("aria-label")).toBe("Car. EV6, 40 % of 80 % target");
      car(element)!.click();
      await settle();
      expect(openDialog(element)!.querySelector("[data-change-car-form]")).toBeNull();
      expect(openDialog(element)!.querySelector("[data-reading]")?.textContent).toBe("40 % of 80 % target · read just now");
    }
  });
});

describe("the car button's look", () => {
  it("is the cells' look at the row's full width, marked in the accent while the question is open", () => {
    expect(VISUAL_STYLES).toMatch(/\.spotnav-car-button \{[^}]*width: 100%;/);
    expect(VISUAL_STYLES).toMatch(/\.spotnav-car-button\[data-asking="true"\] \{[^}]*border: 2px solid var\(--primary-color/);
  });
});
