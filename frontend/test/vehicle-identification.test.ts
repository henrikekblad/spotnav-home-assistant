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

describe("the settings fields", () => {
  it("are decoded, kept by every other editor's replacement, and absent on an older backend", () => {
    const record = decodeSettingsRecord({
      ...read("dashboard", "target_soc_two_vehicles.json")["settings"],
      vehicle_ids: [EV6, NIRO],
      identify_mode: "ask",
    });
    expect(record.vehicle_ids).toEqual([EV6, NIRO]);
    expect(record.identify_mode).toBe("ask");
    const body = encodeBody(record);
    expect(body.vehicle_ids).toEqual([EV6, NIRO]);
    expect(body.identify_mode).toBe("ask");
    const older = { ...read("dashboard", "target_soc_two_vehicles.json")["settings"] };
    delete older["vehicle_ids"];
    delete older["identify_mode"];
    const plain = encodeBody(decodeSettingsRecord(older));
    expect("vehicle_ids" in plain || "identify_mode" in plain).toBe(false);
  });

  it("refuses a mode, a list or a target this card cannot copy", () => {
    const base = read("dashboard", "target_soc_two_vehicles.json")["settings"];
    expect(() => decodeSettingsRecord({ ...base, identify_mode: "sometimes" })).toThrow();
    expect(() => decodeSettingsRecord({ ...base, vehicle_ids: [] })).toThrow();
    expect(() => decodeSettingsRecord({ ...base, vehicle_targets: { [EV6]: 80 } })).toThrow();
  });

  it("knows the question as a notification event", () => {
    expect(NOTIFICATION_EVENTS).toContain("vehicle_identify");
  });

  it("are replaced by the identification Save: every car ticked is every car, and none is refused", () => {
    const record = settingsOf(dashboard());
    const all = [EV6, NIRO];
    const ask = identificationReplacement(record, { mode: "ask", vehicleIds: [NIRO, EV6], allVehicleIds: all });
    expect(ask).toMatchObject({ ok: true, changed: true });
    if (ask.ok) {
      expect(ask.body.identify_mode).toBe("ask");
      expect(ask.body.vehicle_ids).toBeNull();
    }
    const one = identificationReplacement(record, { mode: "automatic", vehicleIds: [NIRO], allVehicleIds: all });
    expect(one.ok && one.body.vehicle_ids).toEqual([NIRO]);
    expect(identificationReplacement(record, { mode: "automatic", vehicleIds: [], allVehicleIds: all })).toMatchObject({
      ok: false,
    });
    expect(identificationReplacement(record, { mode: "automatic", vehicleIds: all, allVehicleIds: all })).toMatchObject({
      ok: true,
      changed: false,
    });
  });
});

describe("the dashboard", () => {
  it("reads each car's own target", () => {
    const payload = dashboard();
    payload["vehicles"][0]["target_percent"] = 80;
    payload["vehicles"][1]["target_percent"] = null;
    const decoded = decodeDashboard(payload);
    expect(decoded.ok && decoded.value.vehicles.map((row) => row.target_percent)).toEqual([80, null]);
  });

  it("reads the open question and each car's sources, and an older backend without them", () => {
    const decoded = decodeDashboard(dashboard(true));
    expect(decoded.ok).toBe(true);
    if (!decoded.ok) return;
    expect(decoded.value.identification?.state).toBe("asking");
    expect(decoded.value.identification?.candidates.map((item) => item.vehicle_id)).toEqual([NIRO, EV6]);
    expect(decoded.value.vehicles[0]!.identification?.plug.name).toBe("EV6 plugged in");
    const older = read("dashboard", "target_soc_two_vehicles.json");
    delete older["identification"];
    for (const row of older["vehicles"]) delete row["identification"];
    const plain = decodeDashboard(older);
    expect(plain.ok && plain.value.identification).toBeNull();
  });

  it("accepts the evidence a field report carries beside the question", () => {
    const payload = dashboard(true);
    payload["identification"]["evidence"] = [
      { vehicle_id: NIRO, plug: null, location: null, verdict: null },
    ];
    const decoded = decodeDashboard(payload);
    expect(decoded.ok && decoded.value.identification?.state).toBe("asking");
  });

  it("hides only the question when its block cannot be read", () => {
    const payload = dashboard(true);
    payload["identification"] = { state: "asking" };
    const decoded = decodeDashboard(payload);
    expect(decoded.ok && decoded.value.identification).toBeNull();
  });
});

describe("the banner", () => {
  it("asks which car is plugged in, likeliest first, and answers with one tap", async () => {
    const { hass, element } = await mounted(dashboard(true));
    const box = banner(element)!;
    expect(box.textContent).toContain("Which car is plugged in?");
    const buttons = Array.from(box.querySelectorAll<HTMLButtonElement>("button"));
    expect(buttons.map((button) => button.textContent)).toEqual(["Niro", "EV6"]);
    expect(buttons[0]!.dataset["likely"]).toBe("true");
    buttons[0]!.click();
    await settle();
    const sent = hass.messages.find((message) => message.type === "spotnav/identify_vehicle");
    expect(sent).toEqual({ type: "spotnav/identify_vehicle", api_version: 1, charger_id: "soc_charger", vehicle_id: NIRO });
    expect(hass.outstanding).toBe(1);
    hass.resolveNext({ api_version: 1, ok: true, error: null, identification: null });
    await settle();
    expect(hass.messages.filter((message) => message.type === "spotnav/get_dashboard").length).toBeGreaterThan(1);
  });

  it("is worded in every language, and any signed-in user may answer it", async () => {
    for (const language of LANGUAGES) {
      document.body.innerHTML = "";
      const { element } = await mounted(dashboard(true), language);
      expect(banner(element)?.textContent, language).toContain(translate(language, "identify.question"));
    }
    document.body.innerHTML = "";
    const { hass, element } = await mounted(dashboard(true), "en", false);
    const buttons = banner(element)!.querySelectorAll<HTMLButtonElement>("button");
    expect(buttons).toHaveLength(2);
    buttons[1]!.click();
    await settle();
    expect(hass.messages.find((message) => message.type === "spotnav/identify_vehicle")).toMatchObject({ vehicle_id: EV6 });
  });

  it("is not shown while nothing is asked", async () => {
    expect(banner((await mounted(dashboard(false))).element)).toBeNull();
  });
});

describe("the Settings page", () => {
  it("names each car's plug sensor and location", async () => {
    const { element } = await openSettings(dashboard());
    const dialog = openDialog(element)!;
    const ev6 = dialog.querySelector<HTMLElement>(`[data-section='vehicle'][data-vehicle='${EV6}']`)!;
    expect(ev6.querySelector("[data-row='plug']")?.textContent).toContain("EV6 plugged in");
    expect(ev6.querySelector("[data-row='location']")?.textContent).toContain(translate("en", "identify.source.choose"));
  });

  it("states the mode and the cars at this charger, and saves a new choice as a settings replacement", async () => {
    const payload = dashboard();
    const { hass, element } = await openSettings(payload);
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='identification']")!;
    expect(section.textContent).toContain(translate("en", "identify.mode.automatic"));
    expect(section.textContent).toContain(translate("en", "identify.vehicles.all"));
    section.querySelector<HTMLButtonElement>("[data-edit-identification]")!.click();
    await settle();
    const form = openDialog(element)!.querySelector<HTMLFormElement>("[data-identification-editor]")!;
    form.querySelector<HTMLInputElement>("[data-identify-mode='ask']")!.click();
    form.querySelector<HTMLInputElement>(`[data-identify-vehicle='${EV6}']`)!.click();
    form.requestSubmit();
    await settle();
    expect(hass.messages.some((message) => message.type === "spotnav/get_settings")).toBe(true);
    expect(hass.outstanding).toBe(1);
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: payload["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings") as Record<string, any>;
    expect(update["settings"]["identify_mode"]).toBe("ask");
    expect(update["settings"]["vehicle_ids"]).toEqual([NIRO]);
  });

  it("is not shown with one car", async () => {
    const payload = dashboard();
    payload["vehicles"] = payload["vehicles"].slice(0, 1);
    payload["vehicle_choices"] = payload["vehicle_choices"].slice(0, 1);
    const { element } = await openSettings(payload);
    expect(openDialog(element)!.querySelector("[data-section='identification']")).toBeNull();
  });

  it("chooses a car's location source in its dialog", async () => {
    const { hass, element } = await openSettings(dashboard());
    openDialog(element)!
      .querySelector<HTMLElement>(`[data-section='vehicle'][data-vehicle='${EV6}']`)!
      .querySelector<HTMLButtonElement>("[data-edit-vehicle]")!
      .click();
    await settle();
    const select = openDialog(element)!.querySelector<HTMLSelectElement>("[data-part='location'] select")!;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "", "device_tracker.ev6_location", "device_tracker.ev6_phone", "none",
    ]);
    select.value = "device_tracker.ev6_phone";
    select.dispatchEvent(new Event("change"));
    openDialog(element)!.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await settle();
    const sent = hass.messages.find((message) => message.type === "spotnav/choose_vehicle_identification");
    expect(sent).toEqual({
      type: "spotnav/choose_vehicle_identification",
      api_version: 1,
      charger_id: "soc_charger",
      vehicle_id: EV6,
      source: "location",
      entity_id: "device_tracker.ev6_phone",
    });
  });
});

function decided(method: string, evidence: Array<Record<string, unknown>> = []): Record<string, any> {
  const payload = dashboard(false);
  payload["identification"] = {
    state: "decided",
    method,
    vehicle_id: EV6,
    since: "2026-10-06T17:00:00+00:00",
    candidates: [
      { vehicle_id: EV6, name: "EV6", likely: method === "plug_sensor" },
      { vehicle_id: NIRO, name: "Niro", likely: false },
    ],
    evidence,
  };
  return payload;
}

const carLine = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>("[data-vehicle-line]");
const changeCar = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>("button[data-vehicle-line]");

describe("the owner's wording", () => {
  it("says inkopplad, Bilar vid laddaren and Laddmål in Swedish, and Charge target in English", () => {
    expect(translate("sv", "identify.question")).toBe("Vilken bil är inkopplad?");
    expect(translate("sv", "identify.vehicles.label")).toBe("Bilar vid laddaren");
    expect(translate("sv", "settings.soc.target")).toBe("Laddmål");
    expect(translate("en", "settings.soc.target")).toBe("Charge target");
  });
});

describe("the car line", () => {
  it("says chosen manually for an answer and a choice, in every language", () => {
    const words = { en: "selected manually", sv: "vald manuellt", da: "valgt manuelt", nb: "valgt manuelt", fi: "valittu käsin" };
    for (const [language, text] of Object.entries(words)) {
      expect(translate(language as "en", "vehicleLine.method.answered")).toBe(text);
      expect(translate(language as "en", "vehicleLine.method.manual")).toBe(text);
    }
  });

  it.each([
    ["plug_sensor", "identifierad via bilens laddkabel"],
    ["location", "identifierad via position"],
    ["answered", "vald manuellt"],
    ["manual", "vald manuellt"],
    ["assumed", "antagen"],
  ])("says how the car was decided (%s)", async (method, words) => {
    const { element } = await mounted(decided(method), "sv");
    expect(carLine(element)?.textContent).toContain(words);
  });

  it("says it is identifying while the question is open, and nothing without identification", async () => {
    expect(carLine((await mounted(dashboard(true), "sv")).element)?.textContent).toContain("identifierar…");
    document.body.innerHTML = "";
    expect(carLine((await mounted(dashboard(false), "sv")).element)?.textContent).not.toContain("identifier");
  });
});

describe("Byt bil", () => {
  it("is labelled in every language", () => {
    const words = { en: "Change car – EV6", sv: "Byt bil – EV6", da: "Skift bil – EV6", nb: "Bytt bil – EV6", fi: "Vaihda auto – EV6" };
    for (const [language, text] of Object.entries(words)) {
      expect(translate(language as "en", "identify.changeCarAria", { name: "EV6" })).toBe(text);
    }
  });

  it("is offered to every signed-in user, and corrects the car through the question's own answer", async () => {
    const evidence = [
      { vehicle_id: EV6, plug: null, location: null, verdict: "plugged_in" },
      { vehicle_id: NIRO, plug: null, location: null, verdict: null },
    ];
    const { hass, element } = await mounted(decided("plug_sensor", evidence), "sv", false);
    expect(changeCar(element)?.getAttribute("aria-label")).toBe("Byt bil – EV6");
    expect(changeCar(element)?.querySelector("[data-icon='swap']")).not.toBeNull();
    expect(changeCar(element)?.lastElementChild?.getAttribute("data-icon")).toBe("swap");
    expect(shadow(element).querySelector("[data-change-car]")).toBeNull();
    changeCar(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    expect(dialog.textContent).toContain("Laddkabeln säger inkopplad");
    const radios = Array.from(dialog.querySelectorAll<HTMLInputElement>("input[data-identify-choice]"));
    expect(radios.map((radio) => [radio.value, radio.checked])).toEqual([[EV6, true], [NIRO, false]]);
    radios[1]!.click();
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await settle();
    expect(hass.messages.find((message) => message.type === "spotnav/identify_vehicle")).toMatchObject({
      vehicle_id: NIRO,
    });
  });

  it("is not offered with one car at the charger", async () => {
    const payload = dashboard(false);
    payload["settings"]["vehicle_ids"] = [EV6];
    expect(changeCar((await mounted(payload)).element)).toBeNull();
  });

  it("is offered with two cars at the charger and no identification, and answers through it when a car is in", async () => {
    const payload = dashboard(false);
    payload["connection"] = { state: "connected", source: null };
    const { hass, element } = await mounted(payload, "sv", false);
    expect(changeCar(element)?.querySelector("[data-icon='swap']")).not.toBeNull();
    changeCar(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    const radios = Array.from(dialog.querySelectorAll<HTMLInputElement>("input[data-identify-choice]"));
    expect(radios.map((radio) => radio.value)).toEqual([EV6, NIRO]);
    radios[1]!.click();
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await settle();
    expect(hass.messages.find((message) => message.type === "spotnav/identify_vehicle")).toMatchObject({ vehicle_id: NIRO });
  });

  it("falls back to the plan's car in the settings when no car is plugged in", async () => {
    const payload = dashboard(false);
    payload["connection"] = { state: "connected", source: null };
    const { hass, element } = await mounted(payload, "sv");
    changeCar(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    dialog.querySelectorAll<HTMLInputElement>("input[data-identify-choice]")[1]!.click();
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await settle();
    hass.resolveNext({ api_version: 1, ok: false, error: "spotnav_not_identifying" });
    await settle();
    expect(hass.messages.some((message) => message.type === "spotnav/get_settings")).toBe(true);
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: payload["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings") as Record<string, any>;
    expect(update["settings"]["target"]["vehicle_id"]).toBe(NIRO);
  });

  it("writes the plan's car straight away when the charger says no car is connected", async () => {
    const payload = dashboard(false);
    payload["connection"] = { state: "disconnected", source: null };
    const { hass, element } = await mounted(payload, "sv");
    changeCar(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    dialog.querySelectorAll<HTMLInputElement>("input[data-identify-choice]")[1]!.click();
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await settle();
    expect(hass.messages.some((message) => message.type === "spotnav/identify_vehicle")).toBe(false);
    expect(hass.messages.some((message) => message.type === "spotnav/get_settings")).toBe(true);
  });
});

describe("the banner's icon", () => {
  it("carries the question mark", async () => {
    const { element } = await mounted(dashboard(true));
    expect(banner(element)?.querySelector("[data-icon='question']")).not.toBeNull();
  });
});

describe("the cars at this charger", () => {
  it("lists every detected car, so an unticked one can be ticked again", async () => {
    const payload = dashboard();
    payload["settings"]["vehicle_ids"] = [NIRO];
    payload["vehicles"] = payload["vehicles"].filter((row: Record<string, unknown>) => row["id"] === NIRO);
    payload["vehicle_choices"] = [
      { id: EV6, name: "EV6" },
      { id: NIRO, name: "Niro" },
    ];
    const { hass, element } = await openSettings(payload);
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='identification']")!;
    expect(section.textContent).toContain("Niro");
    section.querySelector<HTMLButtonElement>("[data-edit-identification]")!.click();
    await settle();
    const form = openDialog(element)!.querySelector<HTMLFormElement>("[data-identification-editor]")!;
    const boxes = Array.from(form.querySelectorAll<HTMLInputElement>("[data-identify-vehicle]"));
    expect(boxes.map((box) => [box.dataset["identifyVehicle"], box.checked])).toEqual([[EV6, false], [NIRO, true]]);
    boxes[0]!.click();
    form.requestSubmit();
    await settle();
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: payload["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings") as Record<string, any>;
    expect(update["settings"]["vehicle_ids"]).toBeNull();
  });
});
