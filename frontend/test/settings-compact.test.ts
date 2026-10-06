// The Settings page in the app's compact style: headings with icons that name the thing, one value per row
// (tappable in the accent colour when it can be changed), one value per dialog through a few generic editors
// (number, one of a few, several, on/off) with what it is for above the input, short status words for the
// setup, and the existing entity dialogs behind the setup rows.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { MARKET_API_VERSION, SETTINGS_API_VERSION } from "../src/types";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const FIX = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, any> => JSON.parse(readFileSync(join(FIX, ...parts), "utf8"));
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};
const PAUSE = { choice: null, admitted_at: null, expires_at: null };

function payload(): Record<string, any> {
  const d = read("dashboard", "cheapest_direct_site_admin.json");
  const two = read("dashboard", "target_soc_two_vehicles.json");
  d.charger.charger_name = "HALO Charger";
  d.site.name = "My Home";
  d.site.active_control = { available: true, enabled: false, reason: null, writable: true };
  d.vehicles = two.vehicles;
  d.vehicles[0].target_percent = 80;
  d.vehicles[1].target_percent = null;
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

const settingsAnswer = (settings: Record<string, unknown>) => ({
  api_version: SETTINGS_API_VERSION,
  ok: true,
  error: null,
  settings,
  pause: PAUSE,
});

async function openSettings(language = "en", admin = true, body: Record<string, any> = payload()) {
  const hass = new FakeHass();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config"
      ? read("entity_config", "v1", "get_direct.json")
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

const tap = async (element: Element, selector: string): Promise<void> => {
  dialog(element).querySelector<HTMLButtonElement>(selector)!.click();
  await settle();
};
const submit = async (element: Element): Promise<void> => {
  dialog(element).querySelector<HTMLFormElement>("form[data-value-editor]")!.requestSubmit();
  await settle();
};

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the page", () => {
  it("heads each section with its icon and names what it is", async () => {
    const { element } = await openSettings("sv");
    const headings = Array.from(dialog(element).querySelectorAll("section h4")).map((heading) => [
      heading.querySelector("svg")?.getAttribute("data-icon") ?? null,
      heading.textContent,
    ]);
    expect(headings).toEqual(
      expect.arrayContaining([
        ["price", "Elpris"],
        ["car", "Bil · EV6"],
        ["charger", "Laddare · HALO Charger"],
        ["site", "Anläggning · My Home"],
        ["solar", "Sol"],
        ["notifications", "Aviseringar"],
      ]),
    );
  });

  it("has no Change buttons: a changeable value is the button, in the accent colour", async () => {
    const { element } = await openSettings();
    const page = dialog(element);
    expect(page.querySelectorAll("[data-edit-vehicle], [data-edit-notifications], [data-edit-identification], [data-edit-solar]")).toHaveLength(0);
    expect(page.querySelectorAll(`section:not([data-section='support']) .${VISUAL_CLASSES.settingsSectionConfigure}`)).toHaveLength(0);
    const target = page.querySelector<HTMLElement>("[data-vehicle='vehicle_ev6'] [data-row='target']")!;
    const value = target.querySelector("button")!;
    expect(value.classList.contains(VISUAL_CLASSES.settingRowEditable)).toBe(true);
    expect(value.textContent).toBe("80 %");
    expect(value.getAttribute("aria-label")).toBe("Charge target, 80 %. Tap to change");
    expect(page.querySelector("[data-vehicle='vehicle_ev6'] [data-row='charge_level'] button")).toBeNull();
  });

  it("shows the setup in short words, never entity ids", async () => {
    const { element } = await openSettings("sv");
    const charger = dialog(element).querySelector<HTMLElement>("[data-section='entities']")!;
    expect(charger.querySelector("[data-row='start_stop']")?.textContent).toBe("Start och stoppAktiv");
    expect(charger.textContent).not.toContain("switch.");
  });

  it("is read-only for a reader who is not an administrator", async () => {
    const { element } = await openSettings("en", false);
    expect(dialog(element).querySelectorAll(`.${VISUAL_CLASSES.settingRowEditable}`)).toHaveLength(0);
  });

  it("says Max laddperioder in Swedish", () => {
    expect(translate("sv", "settings.deadline.periods")).toBe("Max laddperioder");
  });
});

describe("the number editor", () => {
  it("says what the value is for above the field, offers 'not specified' and writes the car's target", async () => {
    const { hass, element } = await openSettings();
    await tap(element, "[data-vehicle='vehicle_ev6'] [data-edit='target']");
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='number']")!;
    const help = form.querySelector(`.${VISUAL_CLASSES.entityHelp}`)!;
    const field = form.querySelector<HTMLInputElement>("[data-value-field='number']")!;
    expect(help.textContent).toBe("Follows the car to every charger");
    expect(help.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(field.value).toBe("80");
    expect(form.querySelector<HTMLInputElement>("[data-value-none]")?.checked).toBe(false);
    field.value = "101";
    await submit(element);
    expect(form.querySelector("[role='alert']")?.textContent).toBe("Between 0 and 100.");
    expect(hass.entityMessages.some((message) => message["type"] === "spotnav/update_vehicle")).toBe(false);
    field.value = "85";
    hass.entityHandler = async (message) =>
      message["type"] === "spotnav/update_vehicle"
        ? read("vehicle", "v1", "update_vehicle_success.json")
        : read("entity_config", "v1", "get_direct.json");
    await submit(element);
    expect(hass.entityMessages.find((message) => message["type"] === "spotnav/update_vehicle")).toMatchObject({
      vehicle_id: "vehicle_ev6",
      changes: { target_percent: 85 },
      expected: { target_percent: 80 },
    });
  });

  it("offers the area's suggestion for a fee and fills the field from it", async () => {
    const { hass, element } = await openSettings("sv");
    const body = payload();
    await tap(element, "[data-edit='tax']");
    hass.resolveNext(settingsAnswer(body["settings"]));
    hass.resolveNext({
      api_version: MARKET_API_VERSION,
      state: "ready",
      reason: null,
      configured_area: "SE4",
      areas: [
        {
          area_id: "SE4", name: "Malmö", countries: ["SE"], timezone: "Europe/Stockholm", currency: "SEK",
          major_unit: "kr", minor_unit: "öre", suggestions: { vat_percent: 25, tax_minor: 36.5, transfer_minor: 30 },
        },
      ],
    });
    await settle();
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='number']")!;
    const offer = form.querySelector<HTMLButtonElement>("[data-value-suggestion]")!;
    expect(offer.textContent).toContain("Förslag: 36,5");
    offer.click();
    expect(form.querySelector<HTMLInputElement>("[data-value-field='number']")?.value).toBe("36,5");
  });
});

describe("the choice editors", () => {
  it("chooses one, with each option's help under it, and writes it as a settings replacement", async () => {
    const { hass, element } = await openSettings();
    await tap(element, "[data-edit='identify_mode']");
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    expect(form.textContent).toContain(translate("en", "identify.mode.askHelp"));
    form.querySelector<HTMLInputElement>("[data-value-option='ask']")!.click();
    await submit(element);
    hass.resolveNext(settingsAnswer(payload()["settings"]));
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings") as Record<string, any>;
    expect(update["settings"]["identify_mode"]).toBe("ask");
  });

  it("chooses several, and refuses none where at least one is needed", async () => {
    const { hass, element } = await openSettings();
    await tap(element, "[data-edit='identify_vehicles']");
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='multi']")!;
    for (const box of Array.from(form.querySelectorAll<HTMLInputElement>("[data-value-option]"))) {
      box.click();
    }
    await submit(element);
    expect(form.querySelector("[role='alert']")?.textContent).toBe(translate("en", "identify.error.noVehicle"));
    expect(hass.messages.some((message) => message.type === "spotnav/get_settings")).toBe(false);
  });

  it("turns active load balancing on or off through its own write", async () => {
    const { hass, element } = await openSettings();
    await tap(element, "[data-edit='active-control']");
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    expect(form.textContent).toContain(translate("en", "site.activeControl.note"));
    form.querySelector<HTMLInputElement>("[data-value-option='on']")!.click();
    await submit(element);
    expect(hass.messages.find((message) => message.type === "spotnav/update_site_settings")).toMatchObject({
      expected: { active_control_enabled: false },
      changes: { active_control_enabled: true },
    });
  });
});

describe("the setup rows", () => {
  it("open the charger's entity dialog", async () => {
    const { element } = await openSettings();
    await tap(element, "[data-edit='start_stop']");
    expect(dialog(element).querySelector("[data-entity-editor='charger']")).not.toBeNull();
  });
});
