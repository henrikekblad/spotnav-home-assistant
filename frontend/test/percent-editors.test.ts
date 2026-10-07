// The car's charge target and minimum charge level: value rows as before, each opening an editor whose value
// is set with a slider (the value large above it). Nothing is written on opening, only on Save of a moved
// slider; the minimum has Off as its first stop and stops at the target, the track past it hatched.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { floorIndex } from "../src/percent-slider";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const FIX = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, any> => JSON.parse(readFileSync(join(FIX, ...parts), "utf8"));
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};

function payload(target: number | null, minimum: number | null, limit: number | null = 90): Record<string, any> {
  const d = read("dashboard", "cheapest_direct_site_admin.json");
  const two = read("dashboard", "target_soc_two_vehicles.json");
  d.vehicles = two.vehicles;
  d.vehicles[0].target_percent = target;
  d.vehicles[0].min_percent = minimum;
  d.vehicles[0].max_percent = limit;
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

async function openSettings(body: Record<string, any>, language = "en", admin = true) {
  const hass = new FakeHass();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config"
      ? read("entity_config", "v1", "get_direct.json")
      : message["type"] === "spotnav/update_vehicle"
        ? read("vehicle", "v1", "update_vehicle_success.json")
        : new Promise<unknown>(() => undefined);
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: admin };
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

async function openEditor(element: Element, key: "target" | "min_percent"): Promise<HTMLFormElement> {
  dialog(element).querySelector<HTMLButtonElement>(`[data-vehicle='vehicle_ev6'] [data-edit='${key}']`)!.click();
  await settle();
  return dialog(element).querySelector<HTMLFormElement>("form[data-value-editor]")!;
}

const writes = (hass: FakeHass) => hass.entityMessages.filter((message) => message["type"] === "spotnav/update_vehicle");
const slide = (slider: HTMLInputElement, value: number): void => {
  slider.value = String(value);
  slider.dispatchEvent(new Event("input", { bubbles: true }));
};

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the rows", () => {
  it("stay value rows: the target and Off or the level", async () => {
    const { element } = await openSettings(payload(80, null), "sv");
    const car = dialog(element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6']")!;
    expect(car.querySelector("[data-row='target'] button")!.textContent).toBe("80 %");
    expect(car.querySelector("[data-row='min_percent'] button")!.textContent).toBe("Av");
    expect(car.querySelector("input[type='range']")).toBeNull();
  });

  it("cannot be changed by a reader who is not an administrator", async () => {
    const { element } = await openSettings(payload(80, 30), "en", false);
    const car = dialog(element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6']")!;
    expect(car.querySelector("[data-edit='target']")).toBeNull();
    expect(car.querySelector("[data-edit='min_percent']")).toBeNull();
  });
});

describe("the target's editor", () => {
  it("is a 0..100 slider with the value large above it, and writes nothing on opening", async () => {
    const { hass, element } = await openSettings(payload(80, 30));
    const form = await openEditor(element, "target");
    expect(form.dataset["valueEditor"]).toBe("target");
    const slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    expect([slider.min, slider.max, slider.step, slider.value]).toEqual(["0", "100", "1", "80"]);
    const amount = form.querySelector<HTMLElement>(`.${C.valueAmount}`)!;
    expect(amount.textContent).toBe("80 %");
    expect(amount.compareDocumentPosition(slider) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(form.textContent).toContain(translate("en", "settings.vehicle.targetHelp"));
    form.requestSubmit();
    await settle();
    expect(writes(hass)).toHaveLength(0);
  });

  it("writes a moved target on Save, as the car's own", async () => {
    const { hass, element } = await openSettings(payload(80, 30));
    const form = await openEditor(element, "target");
    const slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    slide(slider, 85);
    expect(form.querySelector(`.${C.valueAmount}`)!.textContent).toBe("85 %");
    expect(writes(hass)).toHaveLength(0);
    form.requestSubmit();
    await settle();
    expect(writes(hass)).toHaveLength(1);
    expect(writes(hass)[0]).toMatchObject({
      vehicle_id: "vehicle_ev6",
      changes: { target_percent: 85 },
      expected: { target_percent: 80 },
    });
  });

  it("opens a car with none stored at the default, marked as such, and writes nothing until moved", async () => {
    const { hass, element } = await openSettings(payload(null, null, 70), "sv");
    const row = dialog(element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6'] [data-row='target'] button")!;
    expect(row.textContent).toBe(translate("sv", "entity.notSet"));
    let form = await openEditor(element, "target");
    let slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    expect(slider.value).toBe("70");
    const amount = form.querySelector<HTMLElement>(`.${C.valueAmount}`)!;
    expect(amount.textContent).toBe(`70 % ${translate("sv", "settings.vehicle.targetDefault")}`);
    expect(amount.querySelector(`.${C.valueDefault}`)?.textContent).toBe("(standard)");
    form.requestSubmit();
    await settle();
    expect(writes(hass)).toHaveLength(0);

    form = await openEditor(element, "target");
    slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    slide(slider, 75);
    expect(form.querySelector(`.${C.valueAmount}`)!.textContent).toBe("75 %");
    expect(form.querySelector(`.${C.valueDefault}`)).toBeNull();
    form.requestSubmit();
    await settle();
    expect(writes(hass)[0]).toMatchObject({ changes: { target_percent: 75 }, expected: { target_percent: null } });
  });

  it("writes nothing when moved back to where it opened", async () => {
    const { hass, element } = await openSettings(payload(80, 30));
    const form = await openEditor(element, "target");
    const slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    slide(slider, 60);
    slide(slider, 80);
    form.requestSubmit();
    await settle();
    expect(writes(hass)).toHaveLength(0);
  });
});

describe("the minimum's editor", () => {
  it("is a slider from Off to 80 % with the level large above it", async () => {
    const { hass, element } = await openSettings(payload(80, 30));
    const form = await openEditor(element, "min_percent");
    expect(form.dataset["valueEditor"]).toBe("floor");
    const slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    expect([slider.min, slider.max, slider.step]).toEqual(["0", "15", "1"]);
    expect(slider.value).toBe(String(floorIndex(30)));
    expect(form.querySelector(`.${C.valueAmount}`)!.textContent).toBe("30 %");
    expect(Array.from(form.querySelectorAll(`.${C.sliderEnds} span`)).map((end) => end.textContent)).toEqual(["Off", "80 %"]);
    expect(form.textContent).toContain(translate("en", "settings.vehicle.minimumHelp"));
    // Not capped below 80 %: nothing hatched.
    expect(form.querySelector(`.${C.sliderBlocked}`)).toBeNull();
    form.requestSubmit();
    await settle();
    expect(writes(hass)).toHaveLength(0);
  });

  it("writes Off as none on Save", async () => {
    const { hass, element } = await openSettings(payload(80, 30));
    const form = await openEditor(element, "min_percent");
    const slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    slide(slider, 0);
    expect(form.querySelector(`.${C.valueAmount}`)!.textContent).toBe("Off");
    form.requestSubmit();
    await settle();
    expect(writes(hass)[0]).toMatchObject({ changes: { min_percent: null }, expected: { min_percent: 30 } });
  });

  it("stops at the target, the track past it hatched and the target marked", async () => {
    const { hass, element } = await openSettings(payload(50, 30), "sv");
    const form = await openEditor(element, "min_percent");
    const blocked = form.querySelector<HTMLElement>(`.${C.sliderBlocked}`)!;
    expect(blocked).not.toBeNull();
    expect(blocked.style.getPropertyValue("--spotnav-from")).toBe(String(floorIndex(50) / 15));
    const mark = form.querySelector<HTMLElement>("[data-part='target-mark']")!;
    expect(mark.textContent).toBe("laddmål 50 %");
    const slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    slide(slider, 15);
    expect(slider.value).toBe(String(floorIndex(50)));
    expect(form.querySelector(`.${C.valueAmount}`)!.textContent).toBe("50 %");
    form.requestSubmit();
    await settle();
    expect(writes(hass)[0]).toMatchObject({ changes: { min_percent: 50 }, expected: { min_percent: 30 } });
  });

  it("is capped by the default target when the car has none stored", async () => {
    const { element } = await openSettings(payload(null, null, 60), "sv");
    const form = await openEditor(element, "min_percent");
    expect(form.querySelector("[data-part='target-mark']")!.textContent).toBe("laddmål 60 %");
  });

  it("opens a stored level above the target at the target, and writes nothing unless moved", async () => {
    const { hass, element } = await openSettings(payload(40, 60));
    const form = await openEditor(element, "min_percent");
    const slider = form.querySelector<HTMLInputElement>("input[type='range']")!;
    expect(slider.value).toBe(String(floorIndex(40)));
    form.requestSubmit();
    await settle();
    expect(writes(hass)).toHaveLength(0);
  });
});
