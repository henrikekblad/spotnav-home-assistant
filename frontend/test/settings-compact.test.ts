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

async function openSettings(
  language = "en",
  admin = true,
  body: Record<string, any> = payload(),
  entities: Record<string, any> = read("entity_config", "v1", "get_direct.json"),
) {
  const hass = new FakeHass();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config" ? entities : new Promise<unknown>(() => undefined);
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
        ["car", "Bil"],
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

/** The entity config with the charger's current path set as the backend states it. */
function withCurrent(kind: string, enabled: boolean): Record<string, any> {
  const config = read("entity_config", "v1", "get_direct.json");
  config["config"]["control"]["current"] = {
    kind,
    enabled,
    entity_id: kind === "number" ? "number.charger_limit" : null,
    service: kind === "service" ? "easee.set_charger_dynamic_limit" : null,
  };
  return config;
}

/** The parts of the open entity dialog a reader can see. */
const visibleParts = (element: Element): string[] =>
  Array.from(dialog(element).querySelectorAll<HTMLElement>("form[data-entity-editor] > [data-part]"))
    .filter((node) => !node.hidden)
    .map((node) => node.dataset["part"]!);

describe("the app's status words", () => {
  it("says how the current is set as the app does: OCPP, Easee, Controlled, Not controlled", async () => {
    const cases: Array<[string, boolean, string]> = [
      ["ocpp", true, "OCPP"],
      ["service", true, "Easee"],
      ["number", true, "Styrs"],
      ["none", false, "Styrs inte"],
      // Not enabled is not controlled, whatever the path (the dashboard summary's own rule).
      ["ocpp", false, "Styrs inte"],
    ];
    for (const [kind, enabled, word] of cases) {
      document.body.innerHTML = "";
      const { element } = await openSettings("sv", true, payload(), withCurrent(kind, enabled));
      expect(dialog(element).querySelector("[data-row='current'] button")?.textContent, `${kind} ${enabled}`).toBe(word);
    }
    expect(["en", "da", "nb", "fi"].map((l) => translate(l as "en", "settings.status.notControlled"))).toEqual([
      "Not controlled",
      "Styres ikke",
      "Styres ikke",
      "Ei ohjata",
    ]);
    expect(["en", "da", "nb", "fi"].map((l) => translate(l as "en", "settings.status.controlled"))).toEqual([
      "Controlled",
      "Styres",
      "Styres",
      "Ohjataan",
    ]);
  });

  it("says a found energy register as the app does", async () => {
    const { element } = await openSettings("sv");
    expect(dialog(element).querySelector("[data-row='energy_register'] button")?.textContent).toBe("Hittad automatiskt");
  });

  it("says a car's plug sensor and position in status words, never the entity's name, and opens the source choice", async () => {
    const body = payload();
    body.vehicles[0].identification = {
      plug: { entity_id: "binary_sensor.ev6_plug", name: "EV6 Plugged in", chosen: false, candidates: [{ entity_id: "binary_sensor.ev6_plug", name: "EV6 Plugged in" }] },
      location: {
        entity_id: null,
        name: null,
        chosen: false,
        candidates: [
          { entity_id: "device_tracker.a", name: "A" },
          { entity_id: "device_tracker.b", name: "B" },
        ],
      },
    };
    body.vehicles[1].identification = {
      plug: { entity_id: null, name: null, chosen: true, candidates: [] },
      location: { entity_id: null, name: null, chosen: false, candidates: [] },
    };
    const { element } = await openSettings("sv", true, body);
    const value = (car: string, row: string) => {
      dialog(element).querySelector<HTMLButtonElement>(`[data-vehicle-tab='${car}'][aria-selected='false']`)?.click();
      return dialog(element).querySelector(`[data-vehicle='${car}'] [data-row='${row}'] button`)?.textContent;
    };
    expect(value("vehicle_ev6", "plug")).toBe("Finns");
    expect(value("vehicle_ev6", "location")).toBe("Välj en");
    expect(value("vehicle_niro", "plug")).toBe("Ingen");
    expect(value("vehicle_niro", "location")).toBe("Saknas");
    dialog(element).querySelector<HTMLButtonElement>("[data-vehicle-tab='vehicle_ev6']")!.click();
    expect(dialog(element).textContent).not.toContain("EV6 Plugged in");
    await tap(element, "[data-vehicle='vehicle_ev6'] [data-edit='plug']");
    expect(dialog(element).querySelector("form[data-value-editor='single'] [data-value-option='binary_sensor.ev6_plug']")).not.toBeNull();
  });
});

describe("the car's charge limit", () => {
  it("shows the limit the car reports, and only where it reports one", async () => {
    const body = payload();
    body.vehicles[0].max_percent = 90;
    const { element } = await openSettings("sv", true, body);
    const row = dialog(element).querySelector<HTMLElement>("[data-vehicle='vehicle_ev6'] [data-row='charge_limit']")!;
    expect(row.textContent).toContain("Laddgräns");
    expect(row.textContent).toContain("90 %");
    dialog(element).querySelector<HTMLButtonElement>("[data-vehicle-tab='vehicle_niro']")!.click();
    expect(dialog(element).querySelector("[data-vehicle='vehicle_niro'] [data-row='charge_limit']")).toBeNull();
    expect(["en", "da", "nb", "fi"].map((l) => translate(l as "en", "settings.vehicle.limit"))).toEqual([
      "Charge limit",
      "Ladegrænse",
      "Ladegrense",
      "Latausraja",
    ]);
  });

  it("is read-only without the capability, and for someone who is not an administrator", async () => {
    const body = payload();
    body.vehicles[0].max_percent = 90;
    body.charger.capabilities.set_charge_limit = false;
    const { element } = await openSettings("en", true, body);
    expect(dialog(element).querySelector("[data-vehicle='vehicle_ev6'] [data-row='charge_limit'] button")).toBeNull();
    document.body.innerHTML = "";
    const allowed = payload();
    allowed.vehicles[0].max_percent = 90;
    allowed.charger.capabilities.set_charge_limit = true;
    const reader = await openSettings("en", false, allowed);
    expect(dialog(reader.element).querySelector("[data-vehicle='vehicle_ev6'] [data-row='charge_limit'] button")).toBeNull();
  });

  it("opens the number editor, 1 to 100 in whole percent as in the app, and writes the car's limit", async () => {
    const body = payload();
    body.vehicles[0].max_percent = 90;
    body.charger.capabilities.set_charge_limit = true;
    const { hass, element } = await openSettings("en", true, body);
    await tap(element, "[data-vehicle='vehicle_ev6'] [data-edit='charge_limit']");
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='number']")!;
    const field = form.querySelector<HTMLInputElement>("[data-value-field='number']")!;
    expect(field.value).toBe("90");
    expect(form.querySelector(`.${VISUAL_CLASSES.entityHelp}`)?.textContent).toBe(
      translate("en", "settings.vehicle.limitHelp"),
    );
    expect(form.querySelector("[data-value-none]")).toBeNull();
    field.value = "0";
    await submit(element);
    expect(form.querySelector("[role='alert']")?.textContent).toBe("Between 1 and 100.");
    field.value = "80";
    hass.entityHandler = async (message) =>
      message["type"] === "spotnav/write_charge_limit"
        ? { api_version: 1, ok: false, error: "spotnav_too_soon", retry_after_s: 42 }
        : read("entity_config", "v1", "get_direct.json");
    await submit(element);
    expect(hass.entityMessages.find((message) => message["type"] === "spotnav/write_charge_limit")).toMatchObject({
      api_version: 1,
      charger_id: "entry_a",
      vehicle_id: "vehicle_ev6",
      percent: 80,
    });
    expect(form.querySelector("[role='alert']")?.textContent).toBe(translate("en", "settings.vehicle.limitTooSoon"));
    hass.entityHandler = async (message) =>
      message["type"] === "spotnav/write_charge_limit"
        ? { api_version: 1, ok: false, error: "spotnav_invalid_value", retry_after_s: null }
        : read("entity_config", "v1", "get_direct.json");
    await submit(element);
    expect(form.querySelector("[role='alert']")?.textContent).toBe(translate("en", "settings.vehicle.limitFailed"));
    hass.entityHandler = async (message) =>
      message["type"] === "spotnav/write_charge_limit"
        ? { api_version: 1, ok: true, error: null, retry_after_s: null }
        : read("entity_config", "v1", "get_direct.json");
    const reads = hass.messages.filter((message) => message["type"] === "spotnav/get_dashboard").length;
    await submit(element);
    // It took: the editor closes and the dashboard is read once to show the car's new limit.
    expect(form.isConnected && form.closest("[hidden]") === null).toBe(false);
    expect(hass.messages.filter((message) => message["type"] === "spotnav/get_dashboard").length).toBe(reads + 1);
  });
});

describe("one setup value, one dialog", () => {
  it("opens only the charge control for Start and stop, titled by its row", async () => {
    const { element } = await openSettings();
    await tap(element, "[data-edit='start_stop']");
    expect(visibleParts(element)).toEqual(["charge-control"]);
    expect(dialog(element).querySelector("h3")?.textContent).toBe(translate("en", "control.startStop"));
  });

  it("opens only the current limit, with None, for the charging current", async () => {
    const { element } = await openSettings();
    await tap(element, "[data-edit='current']");
    expect(visibleParts(element)).toEqual(["current-limit"]);
    expect(dialog(element).textContent).toContain(translate("en", "entity.limit.none"));
  });

  it("opens only the energy choice for the energy register", async () => {
    const { element } = await openSettings();
    await tap(element, "[data-edit='energy_register']");
    expect(visibleParts(element)).toEqual(["energy"]);
  });

  it("still opens the whole site dialog for the measurement", async () => {
    const { element } = await openSettings();
    await tap(element, "[data-edit='measurement_mode']");
    expect(dialog(element).querySelector("form[data-entity-editor='site']")).not.toBeNull();
    expect(dialog(element).querySelectorAll("form[data-entity-editor] > [data-part][hidden]")).toHaveLength(0);
  });

  it("gives a charger in no site its wiring and voltage as their own choices", async () => {
    const body = payload();
    body.site = null;
    const { hass, element } = await openSettings("en", true, body, read("entity_config", "v1", "get_no_site.json"));
    const charger = dialog(element).querySelector<HTMLElement>("[data-section='entities']")!;
    expect(charger.querySelector("[data-row='charger_phases'] button")).not.toBeNull();
    expect(charger.querySelector("[data-row='voltage_between_phases_v'] button")).not.toBeNull();
    await tap(element, "[data-edit='charger_phases']");
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    const other = Array.from(form.querySelectorAll<HTMLInputElement>("[data-value-option]")).find((radio) => !radio.checked)!;
    other.click();
    hass.entityHandler = async () => read("entity_config", "v1", "get_no_site.json");
    form.requestSubmit();
    await settle();
    const update = hass.entityMessages.find((message) => message["type"] === "spotnav/update_entity_config") as Record<string, any>;
    expect(Object.keys(update["changes"])).toEqual(["charger_phases"]);
    expect(update["changes"]["charger_phases"]).toBe(other.dataset["valueOption"]);
  });
});

describe("a value saved while the plan could not be updated", () => {
  it("returns to Settings and says so there, as the warning it is", async () => {
    const { hass, element } = await openSettings();
    await tap(element, "[data-edit='identify_mode']");
    dialog(element).querySelector<HTMLInputElement>("[data-value-option='ask']")!.click();
    await submit(element);
    const record = payload()["settings"];
    hass.resolveNext(settingsAnswer(record));
    await settle();
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: false,
      error: "spotnav_settings_reconcile_failed",
      settings: { ...record, identify_mode: "ask", revision: (record.revision ?? 0) + 1 },
      pause: PAUSE,
    });
    await settle();
    hass.resolveNext(payload());
    await settle();
    expect(dialog(element).querySelector("form[data-value-editor]")).toBeNull();
    expect(dialog(element).querySelector("[data-section='entities']")).not.toBeNull();
    expect(dialog(element).textContent).toContain(translate("en", "settings.error.reconcileFailed"));
  });
});
