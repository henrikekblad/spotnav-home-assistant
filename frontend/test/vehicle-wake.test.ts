// Waking the car for a reading in the card: a row on the car's settings ("Väck bilen för en avläsning", On or Off)
// only for a car whose integration can be woken, whose editor writes it as the car's own property.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { decodeVehicle } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const FIX = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, any> => JSON.parse(readFileSync(join(FIX, ...parts), "utf8"));
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};

function payload(wake: boolean | null | undefined): Record<string, any> {
  const d = read("dashboard", "cheapest_direct_site_admin.json");
  const two = read("dashboard", "target_soc_two_vehicles.json");
  d.vehicles = two.vehicles;
  d.vehicles[0].target_percent = 80;
  if (wake === undefined) {
    delete d.vehicles[0].wake_for_reading;
  } else {
    d.vehicles[0].wake_for_reading = wake;
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

describe("waking the car for a reading", () => {
  it("is a row on the car's settings, On or Off", async () => {
    const off = await openSettings(payload(false), "sv");
    const row = dialog(off.element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6'] [data-row='wake_for_reading']")!;
    expect(row.textContent).toContain("Väck bilen för en avläsning");
    expect(row.querySelector("button")!.textContent).toBe("Av");
    const on = await openSettings(payload(true));
    const value = dialog(on.element).querySelector<HTMLElement>(
      "[data-vehicle='vehicle_ev6'] [data-row='wake_for_reading'] button",
    )!;
    expect(value.textContent).toBe("On");
  });

  it("is not offered for a car that cannot be woken, or by a backend that does not state it", async () => {
    for (const wake of [null, undefined]) {
      const { element } = await openSettings(payload(wake));
      expect(dialog(element).querySelector("[data-vehicle='vehicle_ev6'] [data-row='wake_for_reading']")).toBeNull();
    }
  });

  it("is turned on in its editor, with its help, and written as the car's own", async () => {
    const { hass, element } = await openSettings(payload(false));
    dialog(element).querySelector<HTMLButtonElement>("[data-vehicle='vehicle_ev6'] [data-edit='wake_for_reading']")!.click();
    await settle();
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    expect(form.textContent).toContain(translate("en", "settings.vehicle.wakeHelp"));
    const on = form.querySelector<HTMLInputElement>("input[data-value-option='on']")!;
    on.checked = true;
    on.dispatchEvent(new Event("change", { bubbles: true }));
    form.requestSubmit();
    await settle();
    expect(hass.entityMessages.find((message) => message["type"] === "spotnav/update_vehicle")).toMatchObject({
      vehicle_id: "vehicle_ev6",
      changes: { wake_for_reading: true },
      expected: { wake_for_reading: false },
    });
  });

  it("decodes the row strictly", () => {
    const row = read("dashboard", "target_soc_two_vehicles.json").vehicles[0];
    expect(decodeVehicle({ ...row, wake_for_reading: true }).wake_for_reading).toBe(true);
    expect(decodeVehicle({ ...row, wake_for_reading: null }).wake_for_reading).toBeNull();
    expect(() => decodeVehicle({ ...row, wake_for_reading: "on" })).toThrow();
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      expect(translate(language, "settings.vehicle.wake")).not.toBe("settings.vehicle.wake");
      expect(translate(language, "settings.vehicle.wakeHelp")).toContain("12");
    }
  });
});
