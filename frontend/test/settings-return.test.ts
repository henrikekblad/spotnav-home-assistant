// Every dialog opened from the Settings page returns to the Settings page, by every way out.
//
// The sub-dialogs are the charger's entities, the site's entities and a value's own editor (here the
// vehicle's charge-level sensor; the price area and fiscal editor has its own coverage in `market-card.test.ts`, through the same
// shared mechanism, `leaveSettingsChild` in `card-view.ts`). The ways out are Cancel, the close button,
// Escape, the backdrop, a Save that changes something and a Save that changes nothing. After each of
// them the Settings page is the open dialog -- never the bare card -- and after a Save it is drawn from
// the confirmed read. The charge-level sensor editor's own requests are pinned here too.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { FakeHass, mountCard } from "./helpers";

const ENTITY_DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");
const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const json = (dir: string, name: string): Record<string, unknown> =>
  JSON.parse(readFileSync(join(dir, `${name}.json`), "utf8")) as Record<string, unknown>;

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(get: string, handler: (message: Record<string, unknown>) => Record<string, unknown>) {
  const hass = new FakeHass();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config" ? json(ENTITY_DIR, get) : handler(message);
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(json(DASHBOARD_DIR, "cheapest_direct_site_admin"));
  await settle();
  return { hass, element };
}

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

/** True when the open dialog is the Settings page (it has its sections), not a sub-dialog or nothing. */
const onSettings = (element: Element): boolean =>
  openDialog(element)?.querySelector("[data-section='entities']") != null;

async function openSettings(element: Element): Promise<void> {
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)
    ?.click();
  await settle();
}

type Sub = "charger" | "site" | "vehicle";

async function openSub(element: Element, sub: Sub): Promise<void> {
  const dialog = openDialog(element);
  const button =
    sub === "vehicle"
      ? dialog?.querySelector<HTMLButtonElement>("[data-vehicle] [data-edit='charge_level']")
      : dialog?.querySelector<HTMLButtonElement>(`[data-edit-entities="${sub}"]`);
  button?.click();
  await settle();
  const marker = sub === "vehicle" ? "[data-value-editor='single']" : `[data-entity-editor="${sub}"]`;
  expect(openDialog(element)?.querySelector(marker), `${sub} editor is open`).not.toBeNull();
}

const exits: Record<string, (element: Element) => void> = {
  Cancel: (element) => {
    const buttons = Array.from(openDialog(element)?.querySelectorAll<HTMLButtonElement>("form button[type='button']") ?? []);
    buttons.find((node) => node.textContent === translate("en", "settings.cancel"))?.click();
  },
  "the close button": (element) => {
    openDialog(element)?.querySelector<HTMLButtonElement>(".spotnav-dialog-close")?.click();
  },
  Escape: () => {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  },
  "the backdrop": (element) => {
    (openDialog(element)?.parentElement as HTMLElement).click();
  },
  "a Save that changes nothing": (element) => {
    openDialog(element)?.querySelector<HTMLButtonElement>(".spotnav-settings-save")?.click();
  },
};

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe.each<Sub>(["charger", "site", "vehicle"])("the %s sub-dialog", (sub) => {
  it.each(Object.keys(exits))("returns to the Settings page on %s", async (way) => {
    const { hass, element } = await mounted("vehicle_soc_get", () => json(ENTITY_DIR, "success_charger"));
    await openSettings(element);
    expect(onSettings(element)).toBe(true);
    await openSub(element, sub);

    exits[way]?.(element);
    await settle();

    expect(onSettings(element), `${sub} / ${way}`).toBe(true);
    expect(hass.entityMessages.filter((m) => m["type"] !== "spotnav/get_entity_config")).toEqual([]);
    // And it can be opened again afterwards: nothing was left half-closed.
    await openSub(element, sub);
  });
});

describe("a Save that changes something", () => {
  it("returns a charger group's editor to Settings, drawn from the confirmed read", async () => {
    const { hass, element } = await mounted("get_direct", () => json(ENTITY_DIR, "success_charger"));
    await openSettings(element);
    // The charging current's own row: its part of the charger's dialog, alone.
    openDialog(element)?.querySelector<HTMLButtonElement>("[data-edit='current']")?.click();
    await settle();
    const input = openDialog(element)?.querySelector<HTMLInputElement>("[data-field='current_limit']") as HTMLInputElement;
    input.value = "number.charger_limit";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    openDialog(element)?.querySelector<HTMLButtonElement>(".spotnav-settings-save")?.click();
    await settle();
    hass.resolveNext(json(DASHBOARD_DIR, "cheapest_direct_site_admin"));
    await settle();
    expect(onSettings(element)).toBe(true);
  });

  it("returns the charge-level sensor editor to Settings too, after one choose_vehicle_soc request", async () => {
    const { hass, element } = await mounted("vehicle_soc_get", () => json(ENTITY_DIR, "vehicle_soc_confirmed"));
    await openSettings(element);
    await openSub(element, "vehicle");
    const dialog = openDialog(element);
    expect(dialog?.querySelector("h3, h2")?.textContent).toContain(translate("en", "entity.field.vehicleSoc"));
    dialog
      ?.querySelector<HTMLInputElement>("input[data-value-option='sensor.pack_a']")
      ?.click();
    dialog?.querySelector<HTMLButtonElement>(".spotnav-settings-save")?.click();
    await settle();
    expect(hass.entityMessages.filter((m) => m["type"] === "spotnav/choose_vehicle_soc")).toEqual([
      {
        type: "spotnav/choose_vehicle_soc",
        api_version: 1,
        charger_id: "entry_a",
        vehicle_id: "vehicle_a",
        entity_id: "sensor.pack_a",
      },
    ]);
    hass.resolveNext(json(DASHBOARD_DIR, "cheapest_direct_site_admin"));
    await settle();
    expect(onSettings(element)).toBe(true);
  });

  it("goes back to automatic detection with a null entity", async () => {
    const { hass, element } = await mounted("vehicle_soc_confirmed", () => json(ENTITY_DIR, "vehicle_soc_cleared"));
    await openSettings(element);
    await openSub(element, "vehicle");
    openDialog(element)?.querySelector<HTMLInputElement>("input[data-value-option='']")?.click();
    openDialog(element)?.querySelector<HTMLButtonElement>(".spotnav-settings-save")?.click();
    await settle();
    expect(hass.entityMessages.filter((m) => m["type"] === "spotnav/choose_vehicle_soc")).toEqual([
      {
        type: "spotnav/choose_vehicle_soc",
        api_version: 1,
        charger_id: "entry_a",
        vehicle_id: "vehicle_a",
        entity_id: null,
      },
    ]);
  });

  it("stays in the sensor editor with the sentence for a refusal, and returns nothing", async () => {
    const { element } = await mounted("vehicle_soc_get", () => json(ENTITY_DIR, "vehicle_soc_refused"));
    await openSettings(element);
    await openSub(element, "vehicle");
    openDialog(element)?.querySelector<HTMLInputElement>("input[data-value-option='sensor.pack_a']")?.click();
    openDialog(element)?.querySelector<HTMLButtonElement>(".spotnav-settings-save")?.click();
    await settle();
    expect(openDialog(element)?.querySelector("[data-value-editor='single']")).not.toBeNull();
    expect(openDialog(element)?.querySelector("[role='alert']")?.textContent).toBe(
      translate("en", "entity.error.field.notFound"),
    );
  });
});

describe("the charge-level sensor editor's words", () => {
  it("names the vehicle, lists only its sensors and offers automatic detection", async () => {
    const { element } = await mounted("vehicle_soc_get", () => json(ENTITY_DIR, "success_charger"));
    await openSettings(element);
    await openSub(element, "vehicle");
    const choices = Array.from(openDialog(element)?.querySelectorAll<HTMLInputElement>("input[data-value-option]") ?? []);
    expect(choices.map((node) => node.dataset["valueOption"])).toEqual(["sensor.pack_a", "sensor.pack_b", ""]);
    expect(choices.map((node) => node.checked)).toEqual([false, false, true]);
    expect(openDialog(element)?.textContent).toContain(translate("en", "entity.vehicle.several"));
  });

  it("does not say detection cannot choose when it has chosen among several sensors", async () => {
    const hass = new FakeHass();
    hass.entityHandler = async (message) => {
      if (message["type"] !== "spotnav/get_entity_config") return json(ENTITY_DIR, "vehicle_soc_refused");
      const document = json(ENTITY_DIR, "vehicle_soc_get") as { config: { vehicles: Array<Record<string, unknown>> } };
      document.config.vehicles[0]!["selected"] = { entity_id: "sensor.pack_a", friendly_name: "pack a" };
      document.config.vehicles[0]!["source"] = "automatic";
      return document as unknown as Record<string, unknown>;
    };
    const element = mountCard(CONFIG, hass);
    const snapshot = hass.snapshot("snapshot", "en");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await settle();
    hass.resolveNext(json(DASHBOARD_DIR, "cheapest_direct_site_admin"));
    await settle();
    await openSettings(element);
    await openSub(element, "vehicle");
    const dialog = openDialog(element);
    expect(dialog?.querySelectorAll("input[data-value-option]").length).toBe(3);
    expect(dialog?.textContent).not.toContain(translate("en", "entity.vehicle.several"));
  });

  it("has no charger sub-heading above the charger's rows, and the charge level as the car's tappable value", async () => {
    const { element } = await mounted("vehicle_soc_get", () => json(ENTITY_DIR, "success_charger"));
    await openSettings(element);
    expect(
      openDialog(element)?.querySelector("[data-entity-group='charger'] .spotnav-site-legend"),
    ).toBeNull();
    expect(openDialog(element)?.querySelector("[data-edit-vehicle]")).toBeNull();
    expect(openDialog(element)?.querySelector("[data-vehicle] [data-edit='charge_level']")).not.toBeNull();
  });
});
