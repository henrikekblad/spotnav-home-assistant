// The guest car: the dashboard's `guest` block decoded (and dropped when it cannot be read), "Guest car" in the
// question and in Byt bil (also at a one-car charger, while a car is plugged in), the Plan dialog of a guest plan
// (its kWh, no car target, level, fill, minimum level or charge limit), the Settings page's Guest tab that writes
// the guest amount, and the two status lines.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { lineText } from "../src/status";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { decodeDashboard, guestOrNull } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "soc_charger" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const EV6 = "vehicle_ev6";
const NIRO = "vehicle_niro";

/** The two-car dashboard with a car plugged in; `asking` opens the question. */
function dashboard(options: { asking?: boolean; first?: boolean; active?: boolean; oneCar?: boolean } = {}) {
  const payload = JSON.parse(readFileSync(join(FIXTURES, "target_soc_two_vehicles.json"), "utf8")) as Record<string, any>;
  payload["connection"] = { state: "connected", source: null };
  payload["guest"] = { active: options.active === true, amount_kwh: 10, first: options.first === true };
  if (options.oneCar === true) {
    payload["settings"]["vehicle_ids"] = [EV6];
  }
  const candidates =
    options.oneCar === true
      ? [{ vehicle_id: EV6, name: "EV6", likely: false }]
      : [
          { vehicle_id: NIRO, name: "Niro", likely: true },
          { vehicle_id: EV6, name: "EV6", likely: false },
        ];
  payload["identification"] =
    options.asking === true
      ? { state: "asking", method: "only_candidate", vehicle_id: EV6, since: "2026-10-06T17:00:00+00:00", candidates }
      : null;
  if (options.active === true) {
    payload["settings"]["driver"] = "manual_kwh";
    payload["settings"]["requested_kwh"] = 12;
    payload["settings"]["target"] = { vehicle_id: "guest", target_percent: null };
    payload["target_vehicle_id"] = null;
  }
  return payload;
}

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

const banner = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>("[data-banner='identify']");
const carLine = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>("[data-vehicle-line]");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the guest block", () => {
  it("is read from every committed fixture and from a guest plan", () => {
    const decoded = decodeDashboard(dashboard({ active: true, first: true }));
    expect(decoded.ok && decoded.value.guest).toEqual({ active: true, amount_kwh: 10, first: true });
  });

  it("is dropped, never guessed, when it cannot be read, and the dashboard still decodes", () => {
    expect(guestOrNull({ active: false, amount_kwh: 10, first: false, extra: 1 })).toBeNull();
    expect(guestOrNull({ active: false, amount_kwh: 0, first: false })).toBeNull();
    expect(guestOrNull({ active: "no", amount_kwh: 10, first: false })).toBeNull();
    expect(guestOrNull(undefined)).toBeNull();
    const payload = dashboard();
    payload["guest"] = { active: false, amount_kwh: 10 };
    const decoded = decodeDashboard(payload);
    expect(decoded.ok && decoded.value.guest).toBeNull();
  });
});

describe("the question", () => {
  it("offers Guest car last, and answers it with the guest's id", async () => {
    const { hass, element } = await mounted(dashboard({ asking: true }));
    const buttons = Array.from(banner(element)!.querySelectorAll<HTMLButtonElement>("button"));
    expect(buttons.map((button) => button.textContent)).toEqual(["Niro", "EV6", "Guest car"]);
    buttons[2]!.click();
    await settle();
    expect(hass.messages.find((message) => message.type === "spotnav/identify_vehicle")).toMatchObject({
      vehicle_id: "guest",
    });
  });

  it("puts Guest car first when every car is ruled out", async () => {
    const { element } = await mounted(dashboard({ asking: true, first: true }));
    const buttons = Array.from(banner(element)!.querySelectorAll<HTMLButtonElement>("button"));
    expect(buttons.map((button) => button.textContent)).toEqual(["Guest car", "Niro", "EV6"]);
  });

  it("asks at a one-car charger: <car> or Guest car, in every language, saying the car seems not plugged in", async () => {
    for (const language of LANGUAGES) {
      document.body.innerHTML = "";
      const { element } = await mounted(dashboard({ asking: true, oneCar: true }), language);
      const words = Array.from(banner(element)!.querySelectorAll("button")).map((button) => button.textContent);
      expect(words, language).toEqual(["EV6", translate(language, "identify.guest")]);
      expect(banner(element)!.textContent, language).toContain(translate(language, "identify.keepsSingle", { name: "EV6" }));
    }
  });

  it("keeps the two-car wording with two cars", async () => {
    const { element } = await mounted(dashboard({ asking: true }));
    expect(banner(element)!.textContent).toContain(translate("en", "identify.keeps", { name: "EV6" }));
  });

  it("has no Guest car from a backend that does not know it", async () => {
    const payload = dashboard({ asking: true });
    delete payload["guest"];
    const { element } = await mounted(payload);
    expect(Array.from(banner(element)!.querySelectorAll("button")).map((button) => button.textContent)).toEqual([
      "Niro",
      "EV6",
    ]);
  });
});

describe("Byt bil", () => {
  it("is offered at a one-car charger while a car is plugged in, with Guest car at the end", async () => {
    const { hass, element } = await mounted(dashboard({ oneCar: true }), "sv", false);
    expect(carLine(element)?.tagName).toBe("BUTTON");
    carLine(element)!.click();
    await settle();
    const dialog = openDialog(element)!;
    const radios = Array.from(dialog.querySelectorAll<HTMLInputElement>("input[data-identify-choice]"));
    expect(radios.map((radio) => radio.value)).toEqual([EV6, "guest"]);
    expect(radios[1]!.parentElement?.textContent).toBe("Gästbil");
    radios[1]!.click();
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await settle();
    expect(hass.messages.find((message) => message.type === "spotnav/identify_vehicle")).toMatchObject({
      vehicle_id: "guest",
    });
  });

  it("is not offered at a one-car charger with no car plugged in, nor the guest in the choice", async () => {
    const payload = dashboard({ oneCar: true });
    payload["connection"] = { state: "disconnected", source: null };
    const { element } = await mounted(payload);
    expect(carLine(element)?.tagName).not.toBe("BUTTON");
    document.body.innerHTML = "";
    const two = dashboard();
    two["connection"] = { state: "disconnected", source: null };
    const second = await mounted(two);
    carLine(second.element)!.click();
    await settle();
    const values = Array.from(openDialog(second.element)!.querySelectorAll<HTMLInputElement>("input[data-identify-choice]")).map(
      (radio) => radio.value,
    );
    expect(values).toEqual([EV6, NIRO]);
  });

  it("says Guest car on the car line and has the guest chosen during a guest plan", async () => {
    const { element } = await mounted(dashboard({ active: true }));
    expect(carLine(element)!.textContent).toContain("Guest car");
    expect(carLine(element)!.textContent).toContain("12 kWh");
    carLine(element)!.click();
    await settle();
    const checked = openDialog(element)!.querySelector<HTMLInputElement>("input[data-identify-choice]:checked");
    expect(checked?.value).toBe("guest");
  });
});

describe("the Plan dialog of a guest plan", () => {
  const record = (): SettingsRecord =>
    ({ ...dashboard({ active: true })["settings"] }) as unknown as SettingsRecord;

  async function openPlan(language = "en") {
    const { hass, element } = await mounted(dashboard({ active: true }), language);
    shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
    await settle();
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: record(),
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    return { hass, element };
  }

  it("names the guest, shows its kWh and nothing of a car's target, level, fill or limits", async () => {
    const { element } = await openPlan();
    const dialog = openDialog(element)!;
    expect(dialog.querySelector("[data-soc-row='vehicle']")?.textContent).toContain("Guest car");
    expect(dialog.querySelector("[data-row='charge_by']")).toBeNull();
    expect(dialog.querySelector("[data-part='soc']")).toBeNull();
    expect(dialog.querySelector("[data-part='floor-mark']")).toBeNull();
    expect(dialog.querySelector("[data-part='full-mark']:not([hidden])")).toBeNull();
    expect(dialog.querySelector("[data-part='energy-value']")?.textContent).toContain("12");
    // The departure time and the current stay; the household's weekdays and date do not.
    expect(dialog.querySelector("[data-part='departure']")).not.toBeNull();
    expect(dialog.querySelector("[data-part='departure-weekdays']")).toBeNull();
    expect(dialog.querySelector("[data-part='departure-date']")).toBeNull();
  });

  it("writes the guest's kWh and keeps the guest as the target", async () => {
    const { hass, element } = await openPlan();
    const dialog = openDialog(element)!;
    const slider = dialog.querySelector<HTMLInputElement>("[data-part='energy'] input[type='range']")!;
    slider.value = "15";
    slider.dispatchEvent(new Event("input"));
    shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings") as Record<string, any>;
    expect(update["settings"]["requested_kwh"]).toBe(15);
    expect(update["settings"]["driver"]).toBe("manual_kwh");
    expect(update["settings"]["target"]["vehicle_id"]).toBe("guest");
  });
});

describe("the Guest tab", () => {
  async function openSettings(admin = true) {
    const { hass, element } = await mounted(dashboard(), "en", admin);
    hass.entityHandler = () => new Promise<unknown>(() => undefined);
    shadow(element).querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)!.click();
    await settle();
    return { hass, element };
  }

  it("comes after the household's cars and holds the guest amount only", async () => {
    const { element } = await openSettings();
    const tabs = Array.from(openDialog(element)!.querySelectorAll<HTMLButtonElement>("[role='tab']"));
    expect(tabs.map((tab) => tab.textContent)).toEqual(["EV6", "Niro", "Guest car"]);
    tabs[2]!.click();
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='vehicle'][data-vehicle='guest']")!;
    expect(Array.from(section.querySelectorAll<HTMLElement>("[data-row]")).map((row) => row.dataset["row"])).toEqual([
      "guest_amount",
    ]);
    expect(section.querySelector("[data-row='guest_amount']")?.textContent).toContain("10 kWh");
  });

  it("writes the amount with spotnav/update_guest", async () => {
    const { hass, element } = await openSettings();
    openDialog(element)!.querySelector<HTMLButtonElement>("[data-vehicle-tab='guest']")!.click();
    openDialog(element)!.querySelector<HTMLButtonElement>("[data-edit='guest_amount']")!.click();
    await settle();
    const form = openDialog(element)!.querySelector<HTMLFormElement>("form[data-value-editor='number']")!;
    form.querySelector<HTMLInputElement>("[data-value-field='number']")!.value = "14";
    form.requestSubmit();
    await settle();
    expect(hass.messages.find((message) => message.type === "spotnav/update_guest")).toEqual({
      type: "spotnav/update_guest",
      api_version: 1,
      charger_id: "soc_charger",
      amount_kwh: 14,
    });
  });

  it("is an administrator's only", async () => {
    const { element } = await openSettings(false);
    expect(openDialog(element)!.querySelector("[data-vehicle-tab='guest']")).toBeNull();
  });
});

describe("the status lines", () => {
  const FORMAT = { language: "sv" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };
  const NOW = Date.parse("2026-11-03T18:00:00Z");

  it("say the guest's amount by the departure, without one, and the wait for the unplug", () => {
    expect(
      lineText({ code: "guest_planned", params: { kwh: 10, departure: "2026-11-04T06:00:00+00:00" } }, FORMAT, NOW),
    ).toBe("Gästbil · 10 kWh före 07:00");
    expect(lineText({ code: "guest_planned", params: { kwh: 12.5, departure: null } }, FORMAT, NOW)).toBe(
      "Gästbil · 12,5 kWh",
    );
    expect(lineText({ code: "guest_done", params: {} }, FORMAT, NOW)).toBe("Gästbil · klar, väntar på urkoppling");
    expect(lineText({ code: "guest_done", params: {} }, { ...FORMAT, language: "en" }, NOW)).toBe(
      "Guest car · done, waiting to be unplugged",
    );
  });
});
