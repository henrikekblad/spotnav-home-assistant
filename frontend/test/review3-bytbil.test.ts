// Which car is plugged in: the settings fields decoded and kept, the identification block and each car's
// sources on the dashboard, the card's banner that answers the question, the Settings section and dialog
// for the vehicles at this charger and the mode, and each car's plug and location sources.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { decodeSettingsRecord, encodeBody, identificationReplacement, NOTIFICATION_EVENTS } from "../src/settings";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { decodeDashboard } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "soc_charger" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, any>;

const EV6 = "vehicle_ev6";
const NIRO = "vehicle_niro";

function source(entityId: string | null, name: string | null, chosen = false, candidates: string[] = []) {
  return {
    entity_id: entityId,
    name,
    chosen,
    candidates: candidates.map((id) => ({ entity_id: id, name: id.split(".")[1]!.replace(/_/g, " ") })),
  };
}

/** The two-vehicle dashboard, with sources on both cars and, when asked, an open question. */
function dashboard(asking = false): Record<string, any> {
  const payload = read("dashboard", "target_soc_two_vehicles.json");
  for (const row of payload["vehicles"]) {
    const key = row["id"] === EV6 ? "ev6" : "niro";
    row["identification"] = {
      plug: source(`binary_sensor.${key}_plugged_in`, `${row["name"]} plugged in`, false, [`binary_sensor.${key}_plugged_in`]),
      location: source(null, null, false, [`device_tracker.${key}_location`, `device_tracker.${key}_phone`]),
    };
  }
  payload["identification"] = asking
    ? {
        state: "asking",
        method: "assumed",
        vehicle_id: EV6,
        since: "2026-10-06T17:00:00+00:00",
        candidates: [
          { vehicle_id: NIRO, name: "Niro", likely: true },
          { vehicle_id: EV6, name: "EV6", likely: false },
        ],
      }
    : null;
  return payload;
}

const settingsOf = (payload: Record<string, any>): SettingsRecord => decodeSettingsRecord(payload["settings"]);

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

async function openSettings(payload: Record<string, unknown>, language = "en") {
  const { hass, element } = await mounted(payload, language);
  hass.entityHandler = async (message) => {
    if (message["type"] === "spotnav/get_entity_config") {
      return read("vehicle", "v1", "update_vehicle_success.json");
    }
    return new Promise<unknown>(() => undefined);
  };
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate(language as "en", "header.settings")}"]`)!
    .click();
  await settle();
  return { hass, element };
}

const banner = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>("[data-banner='identify']");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

const changeCar = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>("button[data-vehicle-line]");

describe("review3: Byt bil for a user who is not an administrator, no car plugged in", () => {
  it("is either not offered or does something visible", async () => {
    const payload = dashboard(false);
    payload["connection"] = { state: "disconnected", source: null };
    const { hass, element } = await mounted(payload, "sv", false);
    const button = changeCar(element);
    if (button === null) {
      return;
    }
    button.click();
    await settle();
    const dialog = openDialog(element)!;
    dialog.querySelectorAll<HTMLInputElement>("input[data-identify-choice]")[1]!.click();
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await settle();
    const sent = hass.messages.some((m) => m.type === "spotnav/identify_vehicle" || m.type === "spotnav/get_settings");
    const said = (shadow(element).textContent ?? "").includes(translate("sv", "entity.error.notAdmin"));
    // The tap was offered, the dialog chosen, and then: nothing sent, nothing said.
    expect(sent || said).toBe(true);
  });
});

describe("Byt bil and who may change the car", () => {
  it("is offered to a reader while a car is plugged in, and to an administrator without one", async () => {
    const plugged = await mounted(dashboard(false), "sv", false);
    expect(changeCar(plugged.element)).not.toBeNull();
    const unplugged = dashboard(false);
    unplugged["connection"] = { state: "disconnected", source: null };
    const admin = await mounted(unplugged, "sv", true);
    expect(changeCar(admin.element)).not.toBeNull();
  });

  it("is the plain car line for a reader with no car plugged in", async () => {
    const payload = dashboard(false);
    payload["connection"] = { state: "disconnected", source: null };
    const { element } = await mounted(payload, "sv", false);
    expect(changeCar(element)).toBeNull();
    expect(shadow(element).querySelector("[data-vehicle-line]")).not.toBeNull();
  });
});

describe("the car line with identification off", () => {
  it("says no method when the block names none", async () => {
    const payload = dashboard(false);
    payload["identification"] = {
      state: "decided",
      method: null,
      vehicle_id: EV6,
      since: null,
      candidates: [
        { vehicle_id: EV6, name: "EV6", likely: false },
        { vehicle_id: NIRO, name: "Niro", likely: false },
      ],
      evidence: [],
    };
    const { element } = await mounted(payload, "sv", true);
    const line = shadow(element).querySelector<HTMLElement>("[data-vehicle-line]")!;
    expect(line.textContent).not.toContain(translate("sv", "vehicleLine.method.manual"));
    expect(line.textContent).not.toContain(translate("sv", "vehicleLine.method.assumed"));
  });
});
