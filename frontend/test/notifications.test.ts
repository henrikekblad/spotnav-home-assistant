// Notifications: the settings record's `notifications` field as older clients read it, and the one section for the
// whole site, per phone (`notification-phones.ts`): its decoder, its rows, the phone's editor and what a Save sends,
// through the real card element.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import {
  decodePhonesAnswer,
  eventGroups,
  linkCandidates,
  phoneChanges,
  phoneSummary,
  type PhoneLoads,
  type PhoneRow,
} from "../src/notification-phones";
import { decodeSettingsRecord, encodeBody } from "../src/settings";
import { NOTIFICATION_PHONES_API_VERSION, type NotificationsRecord, type SettingsRecord } from "../src/types";
import { FakeHass, mountCard, shownSections } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const PIXEL = { service: "mobile_app_pixel_8", name: "Pixel 8" };
const IPAD = { service: "mobile_app_ipad", name: "iPad" };

function notifications(overrides: Partial<NotificationsRecord> = {}): NotificationsRecord {
  return {
    targets: [],
    events: ["plan_stopped", "plan_at_risk", "charge_complete"],
    url: null,
    available: [IPAD, PIXEL],
    ...overrides,
  };
}

function wire(overrides: Partial<NotificationsRecord> = {}): Record<string, unknown> {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 3,
    amps: 10,
    requested_kwh: 20,
    max_periods: 1,
    departure_enabled: true,
    departure_time: "07:00",
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: null },
    notifications: notifications(overrides),
  };
}

function record(overrides: Partial<NotificationsRecord> = {}): SettingsRecord {
  return decodeSettingsRecord(wire(overrides));
}

describe("the settings record's notifications field (older clients)", () => {
  it("is decoded, and left out when the backend has none", () => {
    expect(record({ targets: ["mobile_app_pixel_8"] }).notifications).toEqual(notifications({ targets: ["mobile_app_pixel_8"] }));
    const older = wire();
    delete older["notifications"];
    expect("notifications" in decodeSettingsRecord(older)).toBe(false);
  });

  it("refuses an event this card does not know and an unknown key", () => {
    expect(() => record({ events: ["fire"] })).toThrow();
    const extra = wire();
    (extra["notifications"] as Record<string, unknown>)["sound"] = "loud";
    expect(() => decodeSettingsRecord(extra)).toThrow();
  });

  it("is never part of this card's replacement, so a settings Save never moves the phones", () => {
    expect("notifications" in encodeBody(record({ targets: ["mobile_app_ipad"] }))).toBe(false);
  });
});

// ------------------------------------------------------------------ the per-phone section, pure

const LOADS: PhoneLoads = { chargers: 2, pool: true, spa: true, sauna: false };

function phone(overrides: Partial<PhoneRow> = {}): PhoneRow {
  return {
    id: "ph_000000000001",
    name: "Pixel 8",
    on: true,
    events: ["plan_stopped", "plan_at_risk", "charge_complete", "vehicle_identify", "pool_season"],
    chargers: null,
    companion: { service: "mobile_app_pixel_8", name: "Pixel 8", found: true },
    app: null,
    route: "companion",
    ...overrides,
  };
}

function answer(phones: PhoneRow[] = [phone()], loads: PhoneLoads = LOADS): Record<string, unknown> {
  return {
    api_version: NOTIFICATION_PHONES_API_VERSION,
    ok: true,
    error: null,
    phones,
    chargers: [
      { id: "entry_a", name: "Garage" },
      { id: "entry_b", name: "Carport" },
    ].slice(0, loads.chargers),
    loads,
    events: [],
  };
}

describe("the phones' listing", () => {
  it("is decoded strictly, an event a later backend adds read past", () => {
    const decoded = decodePhonesAnswer(answer([phone({ events: ["plan_stopped", "later_event" as never] })]));
    expect(decoded).toMatchObject({ ok: true, value: { phones: [{ id: "ph_000000000001", events: ["plan_stopped"] }] } });
    expect(decodePhonesAnswer({ ...answer(), phones: [{ id: "x" }] })).toBeNull();
    expect(decodePhonesAnswer({ ...answer(), api_version: 2 })).toBeNull();
    expect(decodePhonesAnswer({ api_version: 1, ok: false, error: "spotnav_not_admin" })).toEqual({ ok: false, code: "spotnav_not_admin" });
  });

  it("groups the events by what the site has, Not ready in time once", () => {
    expect(eventGroups(LOADS).map((group) => [group.key, group.events.length])).toEqual([
      ["charging", 8],
      ["pool", 1],
    ]);
    expect(eventGroups({ chargers: 0, pool: false, spa: true, sauna: true })).toEqual([{ key: "spa", events: ["plan_at_risk"] }]);
    expect(eventGroups({ chargers: 0, pool: false, spa: false, sauna: true })).toEqual([{ key: "sauna", events: ["plan_at_risk"] }]);
    expect(eventGroups({ chargers: 0, pool: false, spa: false, sauna: false })).toEqual([]);
  });

  it("summarises a phone as off, or how many of the shown events are on", () => {
    expect(phoneSummary("en", phone({ on: false }), LOADS)).toBe(translate("en", "settings.value.off"));
    expect(phoneSummary("en", phone(), LOADS)).toBe("5 of 9 on");
    expect(phoneSummary("en", phone(), { chargers: 0, pool: true, spa: true, sauna: false })).toBe("2 of 2 on");
  });

  it("offers to join a Companion phone only with an app that has none, and back", () => {
    const companion = phone();
    const app = phone({ id: "ph_000000000002", companion: null, app: { instant: true }, route: "app" });
    const both = phone({ id: "ph_000000000003", app: { instant: false } });
    expect(linkCandidates(companion, [companion, app, both])).toEqual([app]);
    expect(linkCandidates(app, [companion, app, both])).toEqual([companion]);
    expect(linkCandidates(both, [companion, app, both])).toEqual([]);
  });

  it("sends only what changed, keeps events not shown and sends every charger as null", () => {
    const pixel = phone({ events: ["plan_stopped", "pool_season"] });
    const noPool = { ...LOADS, pool: false };
    expect(phoneChanges(pixel, noPool, ["entry_a", "entry_b"], { on: true, events: ["plan_stopped"], chargers: ["entry_a", "entry_b"] })).toBeNull();
    expect(
      phoneChanges(pixel, noPool, ["entry_a", "entry_b"], { on: false, events: ["plan_stopped", "plugged_in"], chargers: ["entry_b"] }),
    ).toEqual({ on: false, events: ["plan_stopped", "plugged_in", "pool_season"], chargers: ["entry_b"] });
    const one = phone({ chargers: ["entry_a"] });
    expect(phoneChanges(one, LOADS, ["entry_a", "entry_b"], { on: true, events: one.events, chargers: ["entry_a", "entry_b"] })).toEqual({
      chargers: null,
    });
  });
});

// ------------------------------------------------------------------ through the card

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(admin = true, listing: Record<string, unknown> = answer()) {
  const hass = new FakeHass();
  hass.phonesHandler = async (message) =>
    message["type"] === "spotnav/notification_phones" ? listing : answer([phone({ on: false })]);
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  if (admin) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  await settle();
  hass.resolveNext(fixture("start_idle"));
  await settle();
  return { hass, element };
}

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

function openSettings(element: Element): void {
  shadow(element).querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)!.click();
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the Notifications section", () => {
  it("lists every phone with its summary and route, read for the whole site", async () => {
    const app = phone({ id: "ph_000000000002", name: "Anna", companion: null, app: { instant: true }, route: "app" });
    const guest = phone({ id: "notify:mobile_app_guest", name: "Guest", on: false });
    const { hass, element } = await mounted(true, answer([phone(), app, guest]));
    openSettings(element);
    await settle();
    expect(hass.phonesMessages[0]).toEqual({ type: "spotnav/notification_phones", api_version: 1 });
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='notifications']")!;
    expect(section.querySelector("h4")?.textContent).toBe(translate("en", "settings.section.notifications"));
    const rows = Array.from(section.querySelectorAll<HTMLElement>("[data-row]"));
    expect(rows.map((row) => row.dataset["row"])).toEqual(["phone:ph_000000000001", "phone:ph_000000000002", "phone:notify:mobile_app_guest"]);
    expect(rows[2]!.textContent).toContain(translate("en", "settings.value.off"));
    expect(section.querySelector("[data-help='phone:ph_000000000002']")?.textContent).toBe(translate("en", "notifications.via.app"));
    // A Companion-only phone says which app reaches it, also while it is off.
    expect(section.querySelector("[data-help='phone:notify:mobile_app_guest']")?.textContent).toBe(
      translate("en", "notifications.via.companion"),
    );
    const silent = phone({ id: "ph_000000000003", companion: null, app: { instant: false }, route: "none", on: false });
    const { element: other } = await mounted(true, answer([silent]));
    openSettings(other);
    await settle();
    const quiet = openDialog(other)!.querySelector<HTMLElement>("[data-section='notifications']")!;
    expect(quiet.querySelector("[data-help='phone:ph_000000000003']")).toBeNull();
  });

  it("edits one phone: its events by group, the chargers it follows, and saves only what changed with the page", async () => {
    const app = phone({ id: "ph_000000000002", name: "Anna", companion: null, app: { instant: true }, route: "app" });
    const { hass, element } = await mounted(true, answer([phone(), app]));
    openSettings(element);
    await settle();
    openDialog(element)!.querySelector<HTMLButtonElement>("[data-edit='phone:ph_000000000001']")!.click();
    await settle();
    const form = openDialog(element)!.querySelector<HTMLFormElement>("form[data-value-editor='notification-phone']")!;
    expect(Array.from(form.querySelectorAll<HTMLElement>("[data-value-group]")).map((node) => node.dataset["valueGroup"])).toEqual([
      "charging",
      "pool",
      "chargers",
    ]);
    expect(form.querySelector("[data-phone-link]")).not.toBeNull();
    expect(form.querySelector("[data-phone-action='forget']")).not.toBeNull();
    form.querySelector<HTMLInputElement>("[data-phone-event='plugged_in']")!.click();
    form.querySelector<HTMLInputElement>("[data-phone-charger='entry_b']")!.click();
    form.requestSubmit();
    await settle();
    const update = hass.phonesMessages.find((message) => message["type"] === "spotnav/update_notification_phone")!;
    expect(update["phone"]).toBe("ph_000000000001");
    expect(update["changes"]).toEqual({
      events: ["plan_stopped", "plan_at_risk", "charge_complete", "plugged_in", "vehicle_identify", "pool_season"],
      chargers: ["entry_a"],
    });
    expect(typeof update["url"]).toBe("string");
  });

  it("joins a Companion phone and its app as one", async () => {
    const app = phone({ id: "ph_000000000002", name: "Anna", companion: null, app: { instant: true }, route: "app" });
    const { hass, element } = await mounted(true, answer([phone(), app], { chargers: 1, pool: false, spa: false, sauna: false }));
    openSettings(element);
    await settle();
    openDialog(element)!.querySelector<HTMLButtonElement>("[data-edit='phone:ph_000000000001']")!.click();
    await settle();
    const form = openDialog(element)!.querySelector<HTMLFormElement>("form[data-value-editor='notification-phone']")!;
    expect(form.querySelector("[data-value-group='chargers']")).toBeNull();
    const select = form.querySelector<HTMLSelectElement>("[data-phone-link]")!;
    select.value = "ph_000000000002";
    form.requestSubmit();
    await settle();
    const update = hass.phonesMessages.find((message) => message["type"] === "spotnav/update_notification_phone")!;
    expect(update["changes"]).toEqual({ link: "ph_000000000002" });
  });

  it("cannot be seen by a reader who is not an administrator", async () => {
    const { hass, element } = await mounted(false);
    openSettings(element);
    expect(shownSections(element)).toEqual(["display"]);
    expect(element.shadowRoot?.querySelector("[data-section='notifications']")).toBeNull();
    expect(hass.phonesMessages).toEqual([]);
  });
});
