// The plan popover's target slider shows the car's minimum charge level: the track from 0 to it in a darker
// tone, and "min 30 %" under the middle of that part. Off draws nothing; another car picked shows its own.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { SETTINGS_API_VERSION } from "../src/types";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const FIX = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, any> => JSON.parse(readFileSync(join(FIX, ...parts), "utf8"));
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};

function payload(minimum: number | null, other: number | null = null): Record<string, any> {
  const d = read("dashboard", "cheapest_direct_site_admin.json");
  const two = read("dashboard", "target_soc_two_vehicles.json");
  d.vehicles = two.vehicles;
  d.vehicles[0].name = "EV6";
  d.vehicles[1].name = "Niro";
  d.vehicles[0].target_percent = 80;
  d.vehicles[0].min_percent = minimum;
  d.vehicles[1].min_percent = other;
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

async function openPlan(body: Record<string, any>, language = "sv") {
  const hass = new FakeHass();
  hass.entityHandler = async () => new Promise(() => undefined);
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(body);
  await settle();
  shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
  await settle();
  const settings = {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 3,
    amps: 16,
    requested_kwh: 20,
    fill_to_limit: false,
    max_periods: 1,
    departure_enabled: true,
    departure_time: "08:00",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "target_soc",
    target: { vehicle_id: "vehicle_ev6", target_percent: 80 },
  };
  hass.resolveNext({
    api_version: SETTINGS_API_VERSION,
    ok: true,
    error: null,
    settings,
    pause: { choice: null, admitted_at: null, expires_at: null },
  });
  await settle();
  await settle();
  const dialog = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find(
    (node) => node.closest("[hidden]") === null,
  )!;
  return dialog.querySelector<HTMLElement>("[data-part='soc']")!;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the minimum on the plan's target slider", () => {
  it("shades 0 to the minimum and labels it under the middle", async () => {
    const block = await openPlan(payload(30));
    const segment = block.querySelector<HTMLElement>(`.${C.settingsFloorSegment}`)!;
    const label = block.querySelector<HTMLElement>(`.${C.settingsFloorMark}`)!;
    expect(segment.hidden).toBe(false);
    expect(segment.style.getPropertyValue("--spotnav-mark")).toBe("0.3");
    expect(label.hidden).toBe(false);
    expect(label.textContent).toBe("min 30 %");
    expect(label.style.getPropertyValue("--spotnav-mark")).toBe("0.15");
    // In the slider's own track, beside the input.
    expect(segment.parentElement).toBe(block.querySelector("input[type='range']")!.parentElement);
  });

  it("draws nothing when the minimum is off", async () => {
    const block = await openPlan(payload(null));
    expect(block.querySelector<HTMLElement>(`.${C.settingsFloorSegment}`)?.hidden ?? true).toBe(true);
    expect(block.querySelector<HTMLElement>(`.${C.settingsFloorMark}`)?.hidden ?? true).toBe(true);
  });

  it("stays no further than the target as it moves", async () => {
    const block = await openPlan(payload(30));
    const slider = block.querySelector<HTMLInputElement>("input[type='range']")!;
    slider.value = "20";
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    expect(block.querySelector<HTMLElement>(`.${C.settingsFloorSegment}`)!.style.getPropertyValue("--spotnav-mark")).toBe("0.2");
    expect(block.querySelector<HTMLElement>(`.${C.settingsFloorMark}`)!.textContent).toBe("min 20 %");
  });

  it("follows the car picked", async () => {
    const block = await openPlan(payload(30, 50));
    const select = block.querySelector<HTMLSelectElement>("[data-soc='vehicle-choice']")!;
    select.value = "vehicle_niro";
    select.dispatchEvent(new Event("change"));
    expect(block.querySelector<HTMLElement>(`.${C.settingsFloorMark}`)!.textContent).toBe("min 50 %");
    expect(translate("en", "settings.soc.floorMark", { percent: "50" })).toBe("min 50 %");
  });
});
