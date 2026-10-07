// The car's minimum charge level in the card: a row on the car's settings ("Lägsta laddnivå", Off or 30 %) through
// the generic one-of-a-few editor, written as the car's own property, a note when the car has no charge level, and
// the status line while it charges.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { lineText } from "../src/status";
import { decodeVehicle } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const FIX = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, any> => JSON.parse(readFileSync(join(FIX, ...parts), "utf8"));
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};

function payload(minimum: number | null, sensor = true): Record<string, any> {
  const d = read("dashboard", "cheapest_direct_site_admin.json");
  const two = read("dashboard", "target_soc_two_vehicles.json");
  d.vehicles = two.vehicles;
  d.vehicles[0].target_percent = 80;
  d.vehicles[0].min_percent = minimum;
  if (!sensor) {
    d.vehicles[0].soc_entity_id = null;
  }
  d.vehicle_choices = [
    { id: "vehicle_ev6", name: "EV6" },
    { id: "vehicle_niro", name: "Niro" },
  ];
  d.target_vehicle_id = "vehicle_ev6";
  d.soc = two.soc;
  d.settings.identify_mode = "automatic";
  d.settings.vehicle_ids = null;
  return d;
}

async function openSettings(body: Record<string, any>, language = "en") {
  const hass = new FakeHass();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config"
      ? read("entity_config", "v1", "get_direct.json")
      : message["type"] === "spotnav/update_vehicle"
        ? read("vehicle", "v1", "update_vehicle_success.json")
        : new Promise<unknown>(() => undefined);
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(body);
  await settle();
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label='${translate(language as "en", "header.settings")}']`)!
    .click();
  await settle();
  return { hass, element };
}

function dialog(element: Element): HTMLElement {
  return Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find(
    (node) => node.closest("[hidden]") === null,
  )!;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the car's minimum charge level", () => {
  it("is a row on the car's settings, Off or the level", async () => {
    const off = await openSettings(payload(null), "sv");
    const row = dialog(off.element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6'] [data-row='min_percent']")!;
    expect(row.textContent).toContain("Lägsta laddnivå");
    expect(row.querySelector("button")!.textContent).toBe("Av");
    const on = await openSettings(payload(30));
    const value = dialog(on.element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6'] [data-row='min_percent'] button")!;
    expect(value.textContent).toBe("30 %");
  });

  it("is not offered by a backend that does not state it", async () => {
    const body = payload(null);
    delete body.vehicles[0].min_percent;
    const { element } = await openSettings(body);
    expect(dialog(element).querySelector("[data-vehicle='vehicle_ev6'] [data-row='min_percent']")).toBeNull();
  });

  it("says it needs the car's charge level when the car has none", async () => {
    const { element } = await openSettings(payload(30, false));
    const value = dialog(element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6'] [data-row='min_percent'] button")!;
    expect(value.textContent).toBe(`30 % · ${translate("en", "settings.vehicle.minimumNeedsSoc")}`);
  });

  it("is chosen in the one-of-a-few editor and written as the car's own", async () => {
    const { hass, element } = await openSettings(payload(null));
    dialog(element).querySelector<HTMLButtonElement>("[data-vehicle='vehicle_ev6'] [data-edit='min_percent']")!.click();
    await settle();
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    const options = Array.from(form.querySelectorAll<HTMLInputElement>("[data-value-option]")).map(
      (option) => option.dataset["valueOption"],
    );
    expect(options).toEqual(["", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55", "60", "65", "70", "75", "80"]);
    expect(form.textContent).toContain(translate("en", "settings.vehicle.minimumHelp"));
    form.querySelector<HTMLInputElement>("[data-value-option='30']")!.click();
    form.requestSubmit();
    await settle();
    expect(hass.entityMessages.find((message) => message["type"] === "spotnav/update_vehicle")).toMatchObject({
      vehicle_id: "vehicle_ev6",
      changes: { min_percent: 30 },
      expected: { min_percent: null },
    });
  });

  it("decodes the row strictly", () => {
    const row = read("dashboard", "target_soc_two_vehicles.json").vehicles[0];
    expect(decodeVehicle({ ...row, min_percent: 30 }).min_percent).toBe(30);
    expect(decodeVehicle({ ...row, min_percent: null }).min_percent).toBeNull();
    expect(() => decodeVehicle({ ...row, min_percent: 90 })).toThrow();
  });

  it("words the status line in every language", () => {
    const format = (language: (typeof LANGUAGES)[number]) =>
      ({ language, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" }) as const;
    const line = { code: "min_soc_charging", params: { percent: 30 } } as never;
    expect(lineText(line, format("sv"), 0)).toBe("Laddar till lägsta nivå (30 %)");
    for (const language of LANGUAGES) {
      expect(lineText(line, format(language), 0)).toContain("30");
    }
  });
});
