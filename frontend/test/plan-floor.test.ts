// The plan popover's target slider shows the car's minimum charge level: the track from 0 to it in a darker
// tone, and "min 30 %" under the middle of that part. Off draws nothing; another car picked shows its own.
// Ticks mark the level now ("nu") and the car's limit ("gräns"); a word that would touch another drops a line.

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

describe("the level now and the car's limit on the plan's target slider", () => {
  const mark = (block: HTMLElement, part: string): HTMLElement =>
    block.querySelector<HTMLElement>(`[data-part='${part}']`)!;

  it("are ticks at the level now and the car's limit, worded under the track", async () => {
    const block = await openPlan(payload(30));
    const now = mark(block, "now-mark");
    const limit = mark(block, "limit-mark");
    expect(now.hidden).toBe(false);
    expect(now.textContent).toBe("nu");
    expect(now.style.getPropertyValue("--spotnav-mark")).toBe("0.4");
    expect(limit.hidden).toBe(false);
    expect(limit.textContent).toBe("gräns");
    expect(limit.style.getPropertyValue("--spotnav-mark")).toBe("0.8");
    // The same tick as the kWh slider's "fullt", in the slider's own track.
    expect(now.classList.contains(C.settingsFullMark)).toBe(true);
    expect(now.parentElement).toBe(block.querySelector("input[type='range']")!.parentElement);
  });

  it("say the level now is an estimate when it is one", async () => {
    const body = payload(null);
    body.soc.estimated = true;
    const block = await openPlan(body);
    expect(mark(block, "now-mark").textContent).toBe("≈ nu");
  });

  it("leave out the limit when the car states none, and the level now for another car picked", async () => {
    const block = await openPlan(payload(null));
    const select = block.querySelector<HTMLSelectElement>("[data-soc='vehicle-choice']")!;
    select.value = "vehicle_niro";
    select.dispatchEvent(new Event("change"));
    expect(mark(block, "now-mark").hidden).toBe(true);
    expect(mark(block, "limit-mark").hidden).toBe(true);
  });

  it("have no limit tick when the car charges to 100 %", async () => {
    const body = payload(null);
    body.soc.vehicle_max_percent = 100;
    const block = await openPlan(body);
    expect(mark(block, "limit-mark").hidden).toBe(true);
    expect(mark(block, "now-mark").hidden).toBe(false);
  });

  it("drop a word that would touch another to a second line", async () => {
    // A 116 px track (100 px of travel) and 30 px words: "min 30 %" at 23, "nu" at 48 and "gräns" at 88.
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(116);
    const words = vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(30);
    try {
      const block = await openPlan(payload(30));
      const level = (part: string): string => mark(block, part).style.getPropertyValue("--spotnav-mark-level");
      expect(level("floor-mark")).toBe("0");
      expect(level("now-mark")).toBe("1");
      expect(level("limit-mark")).toBe("0");
      const track = block.querySelector<HTMLElement>("input[type='range']")!.parentElement!;
      expect(track.style.getPropertyValue("--spotnav-mark-lines")).toBe("2");
      // Back on the first line once the minimum's word is gone (a target of 0 leaves nothing of it).
      const slider = block.querySelector<HTMLInputElement>("input[type='range']")!;
      slider.value = "0";
      slider.dispatchEvent(new Event("input", { bubbles: true }));
      expect(level("now-mark")).toBe("0");
      expect(track.style.getPropertyValue("--spotnav-mark-lines")).toBe("1");
    } finally {
      width.mockRestore();
      words.mockRestore();
    }
  });

  it("keep a word at the end of the track inside it", async () => {
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(116);
    const words = vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(30);
    try {
      const body = payload(null);
      body.soc.value = 100;
      const block = await openPlan(body);
      // "nu" at 108 would end at 123: held at 86, 7 px left of its tick, the tick itself staying put.
      expect(mark(block, "now-mark").style.getPropertyValue("--spotnav-mark-shift")).toBe("-7px");
      expect(mark(block, "now-mark").style.getPropertyValue("--spotnav-mark")).toBe("1");
    } finally {
      width.mockRestore();
      words.mockRestore();
    }
  });
});
