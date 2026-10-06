// Vehicles on the card: the Plan popover's live target editor, and the Settings page's
// vehicles, each with its own battery size and consumption (`spotnav/update_vehicle`).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { summaryValueClass } from "../src/visual-styles";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "soc_charger" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, any>;
const twoVehicles = (): Record<string, any> => read("dashboard", "target_soc_two_vehicles.json");
const vehicleAnswer = (name: string): Record<string, any> => read("vehicle", "v1", `update_vehicle_${name}.json`);

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
    target: { vehicle_id: "vehicle_ev6", target_percent: 80 },
    ...overrides,
  };
}

const settingsAnswer = (record: SettingsRecord) => ({
  api_version: SETTINGS_API_VERSION,
  ok: true,
  error: null,
  settings: { ...record },
  pause: { choice: null, admitted_at: null, expires_at: null },
});

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

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

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}
const dlg = (element: Element): HTMLElement => openDialog(element)!;
const q = <T extends Element = HTMLElement>(element: Element, selector: string): T | null =>
  dlg(element).querySelector<T>(selector);

async function openPlan(payload: Record<string, unknown>, record = aRecord(), language = "en") {
  const { hass, element } = await mounted(payload, language);
  shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
  await settle();
  hass.resolveNext(settingsAnswer(record));
  await settle();
  return { hass, element };
}

/** Drag the target slider the way a finger does: the slider moves, the input event follows. */
function drag(element: Element, value: number): void {
  const slider = q<HTMLInputElement>(element, "[data-part='soc'] input[type='range']")!;
  slider.value = String(value);
  slider.dispatchEvent(new Event("input"));
}

const needText = (element: Element) => q(element, "[data-soc-row='need']")?.textContent ?? "";
const verdict = (element: Element) => q(element, "[data-soc='verdict']") as HTMLElement;

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the Plan popover's target editor", () => {
  it("spans 0 to 100 and states what is known above the slider, each part only when known", async () => {
    const { element } = await openPlan(twoVehicles());
    const slider = q<HTMLInputElement>(element, "[data-part='soc'] input[type='range']")!;
    expect([slider.min, slider.max, slider.value]).toEqual(["0", "100", "80"]);
    expect(q(element, "[data-soc='facts']")?.textContent).toBe("Now 40 % · Vehicle charge limit 80 %");
    const facts = q(element, "[data-soc='facts']")!;
    const field = q(element, "[data-part='soc'] [role='group']")!;
    expect(facts.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const noLimit = twoVehicles();
    noLimit["soc"]["vehicle_max_percent"] = null;
    const second = await openPlan(noLimit);
    expect(q(second.element, "[data-soc='facts']")?.textContent).toBe("Now 40 %");

    const nothing = twoVehicles();
    Object.assign(nothing["soc"], { vehicle_max_percent: null, value: null, missing: ["soc"] });
    const third = await openPlan(nothing);
    expect(q(third.element, "[data-soc='facts']")?.hidden).toBe(true);
  });

  it("says it in Swedish", async () => {
    const { element } = await openPlan(twoVehicles(), aRecord(), "sv");
    expect(q(element, "[data-soc='facts']")?.textContent).toBe("Nu 40 % · Bilens laddgräns 80 %");
  });

  it("recomputes the energy while the slider moves, with the backend's formula", async () => {
    const { element } = await openPlan(twoVehicles());
    // At the saved target the backend's own figure is shown.
    expect(needText(element)).toContain("34.2 kWh");
    drag(element, 60);
    expect(needText(element)).toContain("17.1 kWh"); // (60 - 40) % of 77 kWh over 0.9
    drag(element, 50);
    expect(needText(element)).toContain("8.6 kWh");
    drag(element, 40);
    expect(needText(element)).toContain("0.0 kWh");
    drag(element, 5);
    expect(needText(element)).toContain("0.0 kWh");
    drag(element, 70);
    expect(needText(element)).toContain("25.7 kWh");
  });

  it("says no charging is needed when the target is at or below the charge now", async () => {
    const { element } = await openPlan(twoVehicles());
    expect(verdict(element).hidden).toBe(true);
    drag(element, 40);
    expect(verdict(element).textContent).toBe("No charging needed now");
    drag(element, 20);
    expect(verdict(element).textContent).toBe("No charging needed now");
    drag(element, 41);
    expect(verdict(element).hidden).toBe(true);
  });

  it("says the charge stops at the vehicle's limit when the target is above it, and counts only up to it", async () => {
    const { element } = await openPlan(twoVehicles());
    drag(element, 81);
    expect(verdict(element).textContent).toBe("Charging to the vehicle's limit, 80 %");
    expect(needText(element)).toContain("34.2 kWh");
    drag(element, 100);
    expect(verdict(element).textContent).toBe("Charging to the vehicle's limit, 80 %");
    expect(needText(element)).toContain("34.2 kWh");
    drag(element, 80);
    expect(verdict(element).hidden).toBe(true);
  });

  it("says it in Swedish, with a decimal comma", async () => {
    const { element } = await openPlan(twoVehicles(), aRecord(), "sv");
    drag(element, 100);
    expect(verdict(element).textContent).toBe("Laddar till bilens gräns, 80 %");
    drag(element, 30);
    expect(verdict(element).textContent).toBe("Ingen laddning behövs nu");
    drag(element, 60);
    expect(needText(element)).toContain("17,1 kWh");
  });

  it("says 'unknown' without a battery size, at every position of the slider", async () => {
    const payload = twoVehicles();
    Object.assign(payload["soc"], { capacity_kwh: null, need_kwh: null, missing: ["capacity"] });
    const { element } = await openPlan(payload);
    drag(element, 60);
    expect(needText(element)).toContain("Unknown");
    expect(q(element, "[data-soc='capacity-missing']")).not.toBeNull();
    expect(q(element, "[data-soc='settings-link']")).toBeNull();
  });

  it("offers a picker with more than one vehicle, and only the resolved vehicle's charge is stated", async () => {
    const { hass, element } = await openPlan(twoVehicles());
    const select = q<HTMLSelectElement>(element, "select[data-soc='vehicle-choice']")!;
    expect(Array.from(select.options).map((option) => option.textContent)).toEqual(["Not chosen", "EV6", "Niro"]);
    expect(select.value).toBe("vehicle_ev6");
    select.value = "vehicle_niro";
    select.dispatchEvent(new Event("change"));
    // The other vehicle's charge is not known until the choice is saved: no figure of the first vehicle's is used.
    expect(q(element, "[data-soc='facts']")?.hidden).toBe(true);
    expect(needText(element)).toContain("Unknown");
    expect(q(element, "[data-part='soc']")?.textContent).toContain(translate("en", "settings.soc.needAfterVehicle"));
    q<HTMLButtonElement>(element, ".spotnav-settings-save")!.click();
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings")!;
    expect(((update["settings"] as Record<string, any>)["target"] as Record<string, unknown>)["vehicle_id"]).toBe("vehicle_niro");
  });

  it("shows the target the chosen car keeps here, and sends it with the car", async () => {
    const record = { ...aRecord(), vehicle_ids: null, identify_mode: "automatic" as const, vehicle_targets: { vehicle_ev6: 80, vehicle_niro: 65 } };
    const { hass, element } = await openPlan(twoVehicles(), record);
    const select = q<HTMLSelectElement>(element, "select[data-soc='vehicle-choice']")!;
    select.value = "vehicle_niro";
    select.dispatchEvent(new Event("change"));
    expect(q(element, "[data-part='target-value']")?.textContent).toContain("65");
    q<HTMLButtonElement>(element, ".spotnav-settings-save")!.click();
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings")!;
    expect((update["settings"] as Record<string, any>)["target"]).toEqual({ vehicle_id: "vehicle_niro", target_percent: 65 });
  });

  it("has no picker with one vehicle, just its name, and no link to Settings anywhere", async () => {
    const payload = twoVehicles();
    payload["soc"]["vehicles"] = [];
    payload["vehicles"] = payload["vehicles"].slice(0, 1);
    const { element } = await openPlan(payload);
    expect(q(element, "select[data-soc='vehicle-choice']")).toBeNull();
    expect(q(element, "[data-soc-row='vehicle']")?.textContent).toContain("EV6");
    expect(q(element, "[data-soc='settings-link']")).toBeNull();
  });
});

// ---------------------------------------------------------------- Settings

async function openSettings(
  payload: Record<string, unknown>,
  options: { language?: string; admin?: boolean; entities?: Record<string, unknown> | null } = {},
) {
  const { hass, element } = await mounted(payload, options.language ?? "en", options.admin ?? true);
  const entities = options.entities === undefined ? vehicleAnswer("success") : options.entities;
  if (entities !== null) {
    hass.entityHandler = async (message) => {
      if (message["type"] === "spotnav/get_entity_config") {
        return entities;
      }
      return new Promise<unknown>(() => undefined);
    };
  }
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate((options.language ?? "en") as "en", "header.settings")}"]`)!
    .click();
  await settle();
  return { hass, element };
}

const entityConfig = (): Record<string, any> => {
  const { api_version, ok, error, field_errors, config } = vehicleAnswer("success");
  return { api_version, ok, error, field_errors, config };
};
/** A vehicle's summary card on the Settings page. */
const block = (element: Element, id: string) => dlg(element).querySelector<HTMLElement>(`[data-section='vehicle'][data-vehicle='${id}']`)!;
/** Press a vehicle card's Change button: its dialog replaces the Settings page. */
async function openVehicle(element: Element, id: string): Promise<void> {
  block(element, id).querySelector<HTMLButtonElement>("[data-edit-vehicle]")!.click();
  await settle();
}
const part = (element: Element, which: "capacity" | "consumption") =>
  dlg(element).querySelector<HTMLElement>(`[data-part='${which}']`)!;
const input = (element: Element, which: "capacity" | "consumption") => part(element, which).querySelector<HTMLInputElement>("input")!;
const fieldError = (element: Element, name: string): HTMLElement | null => {
  const node = dlg(element).querySelector<HTMLElement>(`[data-field-error='${name}']`);
  return node === null || node.hidden ? null : node;
};
const dialogNotice = (element: Element): HTMLElement | null => {
  const node = dlg(element).querySelector<HTMLElement>("[role='status']");
  return node === null || node.hidden ? null : node;
};
const saveButton = (element: Element) => dlg(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!;
/** Type into a field the way a reader does: the value changes and the input event follows. */
function typeInto(control: HTMLInputElement, value: string): void {
  control.value = value;
  control.dispatchEvent(new Event("input"));
}
const vehicleUpdates = (hass: FakeHass) => hass.entityMessages.filter((message) => message["type"] === "spotnav/update_vehicle");

/** The success answer, with the vehicle row and the answer's other facts set as a test needs. */
function answerFrom(name: string, vehicle: Record<string, unknown> | null): Record<string, any> {
  const answer = vehicleAnswer(name);
  if (vehicle !== null) {
    answer["vehicle"] = { ...answer["vehicle"], ...vehicle };
  }
  return answer;
}

describe("the Settings page's vehicles", () => {
  it("names the charger in the heading", async () => {
    const { element } = await openSettings(twoVehicles(), { entities: entityConfig() });
    expect(dlg(element).textContent).toContain("Card settings · Wallbox");
    const swedish = await openSettings(twoVehicles(), { language: "sv", entities: entityConfig() });
    expect(dlg(swedish.element).textContent).toContain("Kortinställningar · Wallbox");
  });

  it("summarises every vehicle in its own card, marks the one this charger plans for and edits nothing inline", async () => {
    const { element } = await openSettings(twoVehicles());
    const ids = Array.from(dlg(element).querySelectorAll<HTMLElement>("[data-section='vehicle']")).map((node) => node.dataset["vehicle"]);
    expect(ids).toEqual(["vehicle_ev6", "vehicle_niro"]);
    expect(block(element, "vehicle_ev6").querySelector("h4")?.textContent).toBe("EV6");
    expect(block(element, "vehicle_niro").querySelector("h4")?.textContent).toBe("Niro");
    expect(block(element, "vehicle_ev6").querySelector("[data-row='capacity']")?.textContent).toContain("77.0 kWh");
    expect(block(element, "vehicle_ev6").querySelector("[data-row='consumption']")?.textContent).toContain("2.0 kWh/10 km");
    expect(block(element, "vehicle_niro").querySelector("[data-row='capacity']")?.textContent).toContain("64.8 kWh");
    expect(block(element, "vehicle_niro").querySelector("[data-row='consumption']")?.textContent).toContain("1.7 kWh/10 km");
    expect(block(element, "vehicle_ev6").dataset["planned"]).toBe("true");
    expect(block(element, "vehicle_niro").dataset["planned"]).toBe("false");
    expect(block(element, "vehicle_ev6").querySelector("[data-vehicle-mark='planned']")).not.toBeNull();
    expect(block(element, "vehicle_niro").querySelector("[data-vehicle-mark='planned']")).toBeNull();
    // No input and no Save anywhere on the page: one Change button per vehicle.
    expect(dlg(element).querySelector("[data-section='vehicle'] input")).toBeNull();
    expect(dlg(element).querySelector("[data-action^='save-vehicle']")).toBeNull();
    expect(dlg(element).querySelectorAll("[data-edit-vehicle]")).toHaveLength(2);
  });

  it("shows each vehicle's charge level, not its sensor, in the Settings overview", async () => {
    const { element } = await openSettings(twoVehicles());
    const ev6 = block(element, "vehicle_ev6").querySelector("[data-row='charge_level']");
    expect(ev6?.textContent).toContain("Charge level");
    expect(ev6?.textContent).toContain("40 %");
    const niro = block(element, "vehicle_niro").querySelector("[data-row='charge_level']");
    expect(niro?.textContent).toContain("55 %");
    expect(block(element, "vehicle_niro").textContent).not.toContain("Automatic");
    expect(block(element, "vehicle_niro").textContent).not.toContain("sensor.");
  });

  it("marks an estimate with ~ and says No reading when there is none", async () => {
    const payload = twoVehicles();
    payload["soc"]["estimated"] = true;
    payload["vehicles"][1]["soc_percent"] = null;
    const { element } = await openSettings(payload);
    expect(block(element, "vehicle_ev6").querySelector("[data-row='charge_level']")?.textContent).toContain("~40 %");
    expect(block(element, "vehicle_niro").querySelector("[data-row='charge_level']")?.textContent).toContain(
      "No reading",
    );
  });

  it("names the charge level in every language", () => {
    const names = ["en", "sv", "da", "nb", "fi"].map((l) => translate(l as "en", "settings.vehicle.charge"));
    expect(names).toEqual(["Charge level", "Laddnivå", "Ladeniveau", "Ladenivå", "Varaustaso"]);
  });

  it("marks a value past 18 characters to stack under its label and leaves a short one beside it", () => {
    expect(summaryValueClass("x".repeat(18))).not.toContain("long");
    expect(summaryValueClass("x".repeat(19))).toContain("long");
  });

  it("shows a reported capacity as read-only in the vehicle's dialog, with where it comes from", async () => {
    const payload = twoVehicles();
    payload["vehicles"][0]["capacity_source"] = "reported";
    const { element } = await openSettings(payload, { language: "sv" });
    await openVehicle(element, "vehicle_ev6");
    const row = dlg(element).querySelector("[data-row='capacity']");
    expect(row?.textContent).toContain("77,0 kWh");
    expect(dlg(element).textContent).toContain("rapporterad av bilen");
    expect(dlg(element).querySelector("[data-part='capacity']")).toBeNull();
    expect(input(element, "consumption").disabled).toBe(false);
  });

  it("disables every Change button for a non-administrator, and says why", async () => {
    const { element } = await openSettings(twoVehicles(), { admin: false, entities: null });
    const buttons = Array.from(dlg(element).querySelectorAll<HTMLButtonElement>("[data-edit-vehicle]"));
    expect(buttons).toHaveLength(2);
    expect(buttons.every((button) => button.disabled)).toBe(true);
    expect(dlg(element).textContent).toContain(translate("en", "settings.readOnly"));
  });

  it("opens the dialog with Save and Cancel, the current figures and no request", async () => {
    const { hass, element } = await openSettings(twoVehicles(), { entities: entityConfig() });
    await openVehicle(element, "vehicle_niro");
    expect(dlg(element).parentElement?.textContent).toContain("Vehicle · Niro");
    expect(input(element, "capacity").value).toBe("64.8");
    expect(input(element, "consumption").value).toBe("1.7");
    expect(input(element, "consumption").step).toBe("0.1");
    expect(input(element, "consumption").min).toBe("0.1");
    expect(input(element, "consumption").max).toBe("50");
    const buttons = Array.from(dlg(element).querySelectorAll<HTMLButtonElement>("form button")).map((node) => node.textContent);
    expect(buttons).toEqual([translate("en", "settings.save"), translate("en", "settings.cancel")]);
    expect(vehicleUpdates(hass)).toHaveLength(0);
  });

  it("writes both fields in one request under compare-and-set, never adopting before the answer, then returns to Settings", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_niro");
    typeInto(input(element, "consumption"), "1.94");
    typeInto(input(element, "capacity"), "70");
    let release: (value: unknown) => void = () => undefined;
    hass.entityHandler = () => new Promise((resolve) => (release = resolve));
    saveButton(element).click();
    await settle();
    expect(vehicleUpdates(hass)).toEqual([
      {
        type: "spotnav/update_vehicle",
        api_version: 1,
        charger_id: "soc_charger",
        vehicle_id: "vehicle_niro",
        changes: { capacity_kwh: 70, consumption_kwh_per_10km: 1.9 },
        expected: { capacity_kwh: 64.8, consumption_kwh_per_10km: 1.7 },
      },
    ]);
    // While the answer is on its way the dialog is still the dialog, with Save disabled.
    expect(dlg(element).querySelector("[data-entity-editor='vehicle']")).not.toBeNull();
    expect(saveButton(element).disabled).toBe(true);
    release(answerFrom("success", { capacity_kwh: 70, consumption_kwh_per_10km: 1.9 }));
    await settle();
    // One read of the dashboard confirms the write.
    expect(hass.messages.filter((message) => message.type === "spotnav/get_dashboard").length).toBeGreaterThan(1);
    hass.resolveNext(twoVehicles());
    await settle();
    expect(dlg(element).querySelector("[data-section='vehicle']")).not.toBeNull();
    expect(dlg(element).querySelector("[data-entity-editor]")).toBeNull();
  });

  it("shows the onboard charger as a 1-phase or 3-phase choice, on what is stored, and writes only a change", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_niro");
    const radios = Array.from(dlg(element).querySelectorAll<HTMLInputElement>("[data-part='onboard'] input[data-onboard]"));
    expect(radios.map((radio) => radio.dataset["onboard"])).toEqual(["1", "3"]);
    expect(radios.map((radio) => radio.checked)).toEqual([false, true]);
    expect(dlg(element).querySelector("[data-part='onboard']")?.textContent).toContain(translate("en", "settings.vehicle.onboardLegend"));
    expect(dlg(element).querySelector("[data-part='onboard']")?.textContent).toContain("1-phase");
    expect(dlg(element).querySelector("[data-part='onboard']")?.textContent).toContain("3-phase");
    // Nothing moved: nothing is sent.
    saveButton(element).click();
    await settle();
    expect(vehicleUpdates(hass)).toHaveLength(0);

    await openVehicle(element, "vehicle_niro");
    hass.entityHandler = async () => answerFrom("success", { onboard_phases: 1 });
    const one = dlg(element).querySelector<HTMLInputElement>("input[data-onboard='1']")!;
    one.checked = true;
    one.dispatchEvent(new Event("change", { bubbles: true }));
    saveButton(element).click();
    await settle();
    expect(vehicleUpdates(hass)[0]).toMatchObject({
      vehicle_id: "vehicle_niro",
      changes: { onboard_phases: 1 },
      expected: { onboard_phases: 3 },
    });
    expect(Object.keys((vehicleUpdates(hass)[0] as Record<string, any>)["changes"])).toEqual(["onboard_phases"]);
  });

  it("names the onboard charger on the vehicle's card and in every language", async () => {
    const { element } = await openSettings(twoVehicles());
    expect(block(element, "vehicle_niro").querySelector("[data-row='onboard']")?.textContent).toContain("3-phase");
    for (const language of ["sv", "nb", "da", "fi"] as const) {
      for (const key of ["settings.vehicle.onboardLegend", "settings.vehicle.onboardOne", "settings.vehicle.onboardHelp", "settings.vehicle.error.onboardPhases", "settings.phases.limitedByVehicle", "settings.phases.line"] as const) {
        expect(translate(language, key), `${language} ${key}`).not.toBe(translate("en", key));
      }
    }
    expect(translate("sv", "settings.phases.limitedByVehicle")).toBe("Bilen laddar på en fas.");
  });

  it("writes only the field that changed", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_ev6");
    hass.entityHandler = async () => answerFrom("success", { capacity_kwh: 81.5 });
    typeInto(input(element, "capacity"), "81.46");
    saveButton(element).click();
    await settle();
    expect(vehicleUpdates(hass)[0]).toMatchObject({
      vehicle_id: "vehicle_ev6",
      changes: { capacity_kwh: 81.5 },
      expected: { capacity_kwh: 77 },
    });
    expect(Object.keys((vehicleUpdates(hass)[0] as Record<string, any>)["changes"])).toEqual(["capacity_kwh"]);
  });

  it("sends nothing when the figures are what is shown, and returns to Settings", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_ev6");
    saveButton(element).click();
    await settle();
    expect(vehicleUpdates(hass)).toHaveLength(0);
    expect(dlg(element).querySelector("[data-section='vehicle']")).not.toBeNull();
  });

  it("returns to Settings on Cancel without a request, dropping what was typed", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_ev6");
    typeInto(input(element, "capacity"), "99");
    const cancel = Array.from(dlg(element).querySelectorAll<HTMLButtonElement>("form button")).find(
      (node) => node.textContent === translate("en", "settings.cancel"),
    );
    cancel!.click();
    await settle();
    expect(vehicleUpdates(hass)).toHaveLength(0);
    expect(block(element, "vehicle_ev6").querySelector("[data-row='capacity']")?.textContent).toContain("77.0 kWh");
  });

  it("judges the range before any request and marks the field", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_niro");
    typeInto(input(element, "consumption"), "0.05");
    saveButton(element).click();
    await settle();
    expect(fieldError(element, "consumption_kwh_per_10km")?.textContent).toBe(
      translate("en", "settings.vehicle.error.consumption"),
    );
    typeInto(input(element, "capacity"), "900");
    saveButton(element).click();
    await settle();
    expect(vehicleUpdates(hass)).toHaveLength(0);
    expect(fieldError(element, "capacity_kwh")?.textContent).toBe(translate("en", "settings.vehicle.error.capacity"));
    expect(input(element, "capacity").getAttribute("aria-invalid")).toBe("true");
    // What was typed stays where it was typed.
    expect(input(element, "capacity").value).toBe("900");
  });

  it("marks a refused field with the backend's own refusal and keeps what was typed", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_niro");
    hass.entityHandler = async () => vehicleAnswer("refused");
    typeInto(input(element, "capacity"), "70");
    saveButton(element).click();
    await settle();
    expect(fieldError(element, "capacity_kwh")?.textContent).toBe(translate("en", "settings.vehicle.error.capacity"));
    expect(input(element, "capacity").value).toBe("70");
    expect(input(element, "capacity").getAttribute("aria-invalid")).toBe("true");
    expect(dlg(element).querySelector("[data-entity-editor='vehicle']")).not.toBeNull();
    expect(saveButton(element).disabled).toBe(false);
  });

  it("redraws from the row the answer states on a conflict, dropping what was typed, and says it changed elsewhere", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_niro");
    hass.entityHandler = async () => answerFrom("conflict", { capacity_kwh: 70, consumption_kwh_per_10km: 2.4 });
    typeInto(input(element, "capacity"), "75");
    saveButton(element).click();
    await settle();
    expect(input(element, "capacity").value).toBe("70.0");
    expect(input(element, "consumption").value).toBe("2.4");
    expect(dialogNotice(element)?.textContent).toBe(translate("en", "entity.error.conflict"));
  });

  it("says a vehicle that no longer exists, and a non-administrator's refusal, as a sentence", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_niro");
    hass.entityHandler = async () => vehicleAnswer("unknown_vehicle");
    typeInto(input(element, "capacity"), "70");
    saveButton(element).click();
    await settle();
    expect(dialogNotice(element)?.textContent).toBe(translate("en", "entity.error.field.unknownVehicle"));
    hass.entityHandler = async () => vehicleAnswer("not_admin");
    saveButton(element).click();
    await settle();
    expect(dialogNotice(element)?.textContent).toBe(translate("en", "entity.error.notAdmin"));
  });

  it("says a transport failure in the dialog and keeps what was typed", async () => {
    const { hass, element } = await openSettings(twoVehicles());
    await openVehicle(element, "vehicle_niro");
    hass.entityHandler = async () => {
      throw new Error("offline");
    };
    typeInto(input(element, "capacity"), "70");
    saveButton(element).click();
    await settle();
    expect(dialogNotice(element)?.textContent).toBe(translate("en", "entity.error.generic"));
    expect(dialogNotice(element)?.textContent).not.toContain("offline");
    expect(input(element, "capacity").value).toBe("70");
  });

  it("chooses the charge-level sensor and writes the figures in the same Save, sensor first", async () => {
    const { hass, element } = await openSettings(twoVehicles(), { entities: entityConfig() });
    await openVehicle(element, "vehicle_niro");
    const radios = Array.from(dlg(element).querySelectorAll<HTMLInputElement>("input[data-vehicle-choice]"));
    expect(radios.length).toBeGreaterThan(1);
    const other = radios.find((radio) => !radio.checked && radio.dataset["vehicleChoice"] !== "automatic")!;
    other.click();
    typeInto(input(element, "capacity"), "70");
    hass.entityHandler = async (message) =>
      message["type"] === "spotnav/choose_vehicle_soc" ? entityConfig() : answerFrom("success", { capacity_kwh: 70 });
    saveButton(element).click();
    await settle();
    const types = hass.entityMessages.map((message) => message["type"]);
    expect(types.indexOf("spotnav/choose_vehicle_soc")).toBeGreaterThanOrEqual(0);
    expect(types.indexOf("spotnav/choose_vehicle_soc")).toBeLessThan(types.indexOf("spotnav/update_vehicle"));
    expect(hass.entityMessages.find((message) => message["type"] === "spotnav/choose_vehicle_soc")).toMatchObject({
      vehicle_id: "vehicle_niro",
      entity_id: other.value,
    });
  });

  it("offers no vehicle card, and no capacity or consumption, when the dashboard lists none", async () => {
    const payload = twoVehicles();
    payload["vehicles"] = [];
    payload["target_vehicle_id"] = null;
    const { element } = await openSettings(payload, { entities: null });
    expect(dlg(element).querySelector("[data-vehicle]")).toBeNull();
    expect(dlg(element).querySelector("[data-part]")).toBeNull();
    expect(dlg(element).textContent).toContain(translate("en", "settings.vehicle.none"));
  });
});
