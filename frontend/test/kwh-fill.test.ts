// The kWh slider past "full": a mark at the battery's room, a range up to twice the room (at least 30 kWh,
// never past what the battery holds to the car's limit), and a last step that is "Fill", stored as the
// settings record's `fill_to_limit`.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import {
  decodeSettingsRecord,
  encodeBody,
  energyFillTop,
  energySliderMaximum,
  ENERGY_SLIDER_MAX_KWH,
  formFromRecord,
  replacementFor,
} from "../src/settings";
import { issuesOf, statusText } from "../src/status";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import type { Status } from "../src/validate";
import { statusLine } from "./dashboard-fixtures";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(soc: Record<string, unknown> = {}): Record<string, any> {
  const payload = JSON.parse(readFileSync(join(DASHBOARD_DIR, "target_soc_estimated.json"), "utf8")) as Record<
    string,
    any
  >;
  // The field case: 89 % of 77 kWh, the car's own limit at 100 %: 9.5 kWh fills it.
  Object.assign(payload["soc"], { value: 89, capacity_kwh: 77, room_kwh: 9.5, vehicle_max_percent: 100 }, soc);
  return payload;
}

function manual(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 1,
    amps: 10,
    requested_kwh: 6,
    fill_to_limit: false,
    max_periods: 1,
    departure_enabled: false,
    departure_time: "08:00",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: "car-1", target_percent: null },
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

async function openPlan(payload: Record<string, unknown>, record: SettingsRecord, language = "en") {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: true };
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

function openDialog(element: Element): HTMLElement {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null)!;
}

const q = <T extends Element = HTMLElement>(element: Element, selector: string): T | null =>
  openDialog(element).querySelector<T>(selector);
const slider = (element: Element) => q<HTMLInputElement>(element, "[data-part='energy'] input[type='range']")!;
const field = (element: Element) => q<HTMLInputElement>(element, "[data-part='energy'] input[type='number']")!;
const fillValue = (element: Element) => q(element, "[data-part='energy'] [data-part='energy-fill']");
const mark = (element: Element) => q(element, "[data-part='energy'] [data-part='full-mark']");
const help = (element: Element) => q(element, "[data-note='energy-room']");
const updates = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/update_settings");

function drag(element: Element, value: string): void {
  slider(element).value = value;
  slider(element).dispatchEvent(new Event("input"));
}

async function save(element: Element, hass: FakeHass): Promise<Record<string, any>> {
  q<HTMLButtonElement>(element, ".spotnav-settings-save")!.click();
  await settle();
  const sent = updates(hass);
  expect(sent).toHaveLength(1);
  return sent[0]!["settings"] as Record<string, any>;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the slider's top past full", () => {
  const facts = (room: number | null, capacity: number | null = 77, limit: number | null = 100) => ({
    room_kwh: room,
    capacity_kwh: capacity,
    vehicle_max_percent: limit,
    efficiency: 0.9,
  });

  it("is twice the room, at least 30 kWh, rounded up to the half-kWh step", () => {
    expect(energyFillTop(facts(9.5))).toBe(30);
    expect(energyFillTop(facts(20.1))).toBe(40.5);
  });

  it("never passes what the battery holds to the car's own limit", () => {
    // 77 kWh x 100 % / 0.9 = 85.6 kWh; to a 50 % limit, 42.8 kWh.
    expect(energyFillTop(facts(40))).toBe(80);
    expect(energyFillTop(facts(45))).toBe(86);
    expect(energyFillTop(facts(20, 77, 50))).toBe(43);
  });

  it("has no room clamp beyond the ordinary 100 kWh without a battery size, and no top without a room", () => {
    expect(energyFillTop(facts(60, null))).toBe(ENERGY_SLIDER_MAX_KWH);
    expect(energyFillTop(facts(null))).toBeNull();
  });

  it("still draws a stored amount above it where it is", () => {
    expect(energySliderMaximum(50, 30)).toBe(50);
    expect(energySliderMaximum(6, 30)).toBe(30);
    expect(energySliderMaximum(6, null)).toBe(ENERGY_SLIDER_MAX_KWH);
  });
});

describe("the settings record's fill_to_limit", () => {
  it("is read when present, sent back as read, and left out for a backend without it", () => {
    const record = decodeSettingsRecord({ ...manual(), fill_to_limit: true });
    expect(record.fill_to_limit).toBe(true);
    expect(encodeBody(record).fill_to_limit).toBe(true);
    const older = { ...manual() } as Record<string, unknown>;
    delete older["fill_to_limit"];
    const decoded = decodeSettingsRecord(older);
    expect(decoded.fill_to_limit).toBeUndefined();
    expect("fill_to_limit" in encodeBody(decoded)).toBe(false);
    expect(() => decodeSettingsRecord({ ...manual(), fill_to_limit: "yes" })).toThrow();
  });

  it("is written only when the form moved it, and reapplied only when the reader moved it", () => {
    const record = manual();
    expect(replacementFor("energy", record, formFromRecord(record))).toMatchObject({ changed: false });
    const filled = replacementFor("energy", record, { ...formFromRecord(record), energy: "30", fill: true });
    expect(filled).toMatchObject({ ok: true, changed: true });
    expect(filled.ok && filled.body.fill_to_limit).toBe(true);
    // Another client set Fill meanwhile; this reader only moved the current.
    const newer = manual({ revision: 9, fill_to_limit: true, requested_kwh: 30 });
    const reapplied = replacementFor("plan", newer, { ...formFromRecord(record), current: "16" }, null, record);
    expect(reapplied.ok && reapplied.body.fill_to_limit).toBe(true);
  });
});

describe("the kWh slider with a known room", () => {
  it("marks full at the room, below it says what fills the battery", async () => {
    const { element } = await openPlan(fixture(), manual());
    expect(slider(element).max).toBe("30");
    expect(mark(element)!.hidden).toBe(false);
    expect(mark(element)!.textContent).toBe("full");
    expect(mark(element)!.style.getPropertyValue("--spotnav-mark")).toBe(String((9.5 - 0.5) / (30 - 0.5)));
    expect(field(element).hidden).toBe(false);
    expect(field(element).value).toBe("6");
    expect(fillValue(element)!.hidden).toBe(true);
    expect(help(element)!.textContent).toBe("9.5 kWh fills the battery.");
  });

  it("above full still shows the amount and the same line", async () => {
    const { element } = await openPlan(fixture(), manual({ requested_kwh: 15 }));
    expect(field(element).value).toBe("15");
    expect(fillValue(element)!.hidden).toBe(true);
    expect(help(element)!.textContent).toBe("9.5 kWh fills the battery.");
  });

  it("at the last step reads Fill, says the battery is charged until full and stores the choice", async () => {
    const { element, hass } = await openPlan(fixture(), manual());
    drag(element, "30");
    expect(field(element).hidden).toBe(true);
    expect(fillValue(element)!.hidden).toBe(false);
    expect(fillValue(element)!.textContent).toBe("Fill");
    expect(slider(element).getAttribute("aria-valuetext")).toBe("Fill");
    expect(help(element)!.textContent).toBe("Charges until the battery is full, 9.5 kWh now.");
    const body = await save(element, hass);
    expect(body["fill_to_limit"]).toBe(true);
    expect(body["requested_kwh"]).toBe(30);
  });

  it("opens a stored Fill at the last step, whatever amount is stored, and moving off it clears the choice", async () => {
    const { element, hass } = await openPlan(fixture(), manual({ fill_to_limit: true, requested_kwh: 19 }));
    expect(slider(element).value).toBe("30");
    expect(fillValue(element)!.hidden).toBe(false);
    drag(element, "12");
    expect(fillValue(element)!.hidden).toBe(true);
    expect(field(element).hidden).toBe(false);
    expect(field(element).value).toBe("12");
    const body = await save(element, hass);
    expect(body["fill_to_limit"]).toBe(false);
    expect(body["requested_kwh"]).toBe(12);
  });

  it("names the car's own limit when it is below 100 %", async () => {
    const { element } = await openPlan(fixture({ value: 80, room_kwh: 7.5, vehicle_max_percent: 90 }), manual({ requested_kwh: 4 }));
    expect(help(element)!.textContent).toBe("7.5 kWh fills the battery (to the car's charge limit of 90 %).");
    drag(element, slider(element).max);
    expect(help(element)!.textContent).toBe("Charges until the battery is full (to the car's charge limit of 90 %), 7.5 kWh now.");
  });

  it("speaks Swedish", async () => {
    const { element } = await openPlan(fixture({ value: 80, room_kwh: 7.5, vehicle_max_percent: 90 }), manual({ requested_kwh: 4 }), "sv");
    expect(mark(element)!.textContent).toBe("fullt");
    expect(help(element)!.textContent).toBe("Det behövs 7,5 kWh för att fylla batteriet (till bilens laddgräns 90 %).");
    drag(element, slider(element).max);
    expect(fillValue(element)!.textContent).toBe("Fyll");
    expect(help(element)!.textContent).toBe("Laddar tills batteriet är fullt (till bilens laddgräns 90 %), nu 7,5 kWh.");
  });

  it("keeps a stored amount above the top where it is, as an amount", async () => {
    const { element } = await openPlan(fixture(), manual({ requested_kwh: 50 }));
    expect(slider(element).max).toBe("50");
    expect(slider(element).value).toBe("50");
    expect(field(element).value).toBe("50");
    expect(fillValue(element)!.hidden).toBe(true);
  });

  it("typing an amount is never Fill", async () => {
    const { element, hass } = await openPlan(fixture(), manual());
    field(element).value = "30";
    field(element).dispatchEvent(new Event("input"));
    expect(fillValue(element)!.hidden).toBe(true);
    const body = await save(element, hass);
    expect(body["fill_to_limit"]).toBe(false);
    expect(body["requested_kwh"]).toBe(30);
  });
});

describe("the kWh slider without a room, or against an older backend", () => {
  it("is as before while the room is unknown: no mark, no line, no Fill", async () => {
    const { element } = await openPlan(fixture({ room_kwh: null }), manual());
    expect(slider(element).max).toBe("100");
    expect(mark(element)!.hidden).toBe(true);
    expect(help(element)!.hidden).toBe(true);
    drag(element, "100");
    expect(fillValue(element)!.hidden).toBe(true);
    expect(field(element).value).toBe("100");
  });

  it("offers no Fill to a backend whose record has no fill_to_limit", async () => {
    const older = manual() as Partial<SettingsRecord>;
    delete older.fill_to_limit;
    const { element, hass } = await openPlan(fixture(), older as SettingsRecord);
    expect(mark(element)!.hidden).toBe(false);
    drag(element, "30");
    expect(fillValue(element)!.hidden).toBe(true);
    const body = await save(element, hass);
    expect("fill_to_limit" in body).toBe(false);
    expect(body["requested_kwh"]).toBe(30);
  });
});

describe("the Fill status lines", () => {
  const NOW = Date.parse("2026-09-22T08:00:00Z");
  const format = (language: "sv" | "en" | "nb" | "da" | "fi") =>
    ({ language, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" }) as const;
  const block = (...lines: Array<ReturnType<typeof statusLine>>): Status =>
    ({ tone: "notice", lines }) as unknown as Status;

  it("say the car is charged until full, as a fact", () => {
    const filling = block(
      statusLine("auto_installed", { start: "2026-09-22T22:00:00+00:00" }),
      statusLine("filling_to_limit", { kwh: 9.47 }),
    );
    expect(statusText(filling, format("en"), NOW)).toContain("Charging until the car is full, 9.5 kWh now.");
    expect(statusText(filling, format("sv"), NOW)).toContain("Laddar tills bilen är full, nu 9,5 kWh.");
    expect(issuesOf({ ...filling, tone: "normal" }, "en")).toEqual([]);
  });

  it("say the stored amount is planned while no room is known, as a notice", () => {
    const unknown = block(
      statusLine("auto_installed", { start: "2026-09-22T22:00:00+00:00" }),
      statusLine("fill_room_unknown", { kwh: 12 }),
    );
    expect(statusText(unknown, format("en"), NOW)).toContain(
      "The car's level or battery size is unknown, so 12.0 kWh is charged instead of filling.",
    );
    expect(statusText(unknown, format("sv"), NOW)).toContain(
      "Bilens nivå eller batteristorlek är okänd, så 12,0 kWh laddas i stället för att fylla.",
    );
    expect(issuesOf(unknown, "en").map((issue) => issue.code)).toEqual(["fill_room_unknown"]);
  });

  it("are worded in every language", () => {
    const both = block(statusLine("filling_to_limit", { kwh: 9.5 }), statusLine("fill_room_unknown", { kwh: 12 }));
    for (const language of ["en", "sv", "nb", "da", "fi"] as const) {
      expect(statusText(both, format(language), NOW)).not.toContain("{kwh}");
      expect(translate(language, "settings.energy.fill")).not.toBe("");
    }
  });
});
