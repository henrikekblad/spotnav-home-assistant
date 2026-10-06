// Notifications: the record's `notifications` field decoded, the replacement a Save sends, and the
// Settings section and dialog through the real card element.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { noRecipientsHint, notificationsSummary } from "../src/notifications";
import { decodeSettingsRecord, encodeBody, notificationsReplacement, NOTIFICATION_EVENTS } from "../src/settings";
import { SETTINGS_API_VERSION, type NotificationsRecord, type SettingsRecord } from "../src/types";
import { FakeHass, mountCard } from "./helpers";

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

describe("the notifications field", () => {
  it("is decoded with the phones that can be chosen, and left out when the backend has none", () => {
    expect(record({ targets: ["mobile_app_pixel_8"] }).notifications).toEqual(
      notifications({ targets: ["mobile_app_pixel_8"] }),
    );
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

  it("is never part of another editor's replacement, so those keep what is stored", () => {
    expect("notifications" in encodeBody(record({ targets: ["mobile_app_ipad"] }))).toBe(false);
  });

  it("is sent by a notifications Save, in the backend's event order, with the page a tap opens", () => {
    const check = notificationsReplacement(record(), {
      targets: ["mobile_app_pixel_8"],
      events: ["unplugged", "plan_stopped"],
      url: "/lovelace/ev",
    });
    expect(check).toMatchObject({
      ok: true,
      changed: true,
      body: { notifications: { targets: ["mobile_app_pixel_8"], events: ["plan_stopped", "unplugged"], url: "/lovelace/ev" } },
    });
    const same = notificationsReplacement(record(), {
      targets: [],
      events: ["charge_complete", "plan_stopped", "plan_at_risk"],
      url: null,
    });
    expect(same.ok && same.changed).toBe(false);
  });

  it("cannot be saved to a backend that does not have it", () => {
    const older = wire();
    delete older["notifications"];
    expect(notificationsReplacement(decodeSettingsRecord(older), { targets: [], events: [], url: null })).toEqual({
      ok: false,
      errorKey: "settings.error.version",
    });
  });

  it("summarises the phones by name and the events as a count, a gone phone said as such", () => {
    const rows = notificationsSummary("en", notifications({ targets: ["mobile_app_pixel_8", "mobile_app_old"] }));
    expect(rows.map((row) => row.value)).toEqual([
      `Pixel 8, ${translate("en", "notifications.missing", { name: "mobile_app_old" })}`,
      `3 of ${NOTIFICATION_EVENTS.length} on`,
    ]);
    expect(notificationsSummary("sv", notifications())[0]!.value).toBe(translate("sv", "settings.value.none"));
  });
});

// ------------------------------------------------------------------ through the card

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

function withNotifications(record: NotificationsRecord): Record<string, unknown> {
  const payload = fixture("start_idle");
  (payload["settings"] as Record<string, unknown>)["notifications"] = record;
  return payload;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(payload: Record<string, unknown>, admin = true) {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  if (admin) {
    snapshot.user = { is_admin: true };
  }
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
  it("states the chosen phones and events and opens a dialog with a box per phone and per event", async () => {
    const { element } = await mounted(withNotifications(notifications({ targets: ["mobile_app_pixel_8"] })));
    openSettings(element);
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='notifications']")!;
    expect(section.querySelector("[data-row='notification_targets']")?.textContent).toContain("Pixel 8");
    section.querySelector<HTMLButtonElement>("[data-edit-notifications]")!.click();
    const dialog = openDialog(element)!;
    expect(dialog.querySelector("h3")?.textContent).toBe(translate("en", "notifications.dialogTitle"));
    const phones = Array.from(dialog.querySelectorAll<HTMLInputElement>("[data-target]"));
    expect(phones.map((box) => [box.dataset["target"], box.checked])).toEqual([
      ["mobile_app_ipad", false],
      ["mobile_app_pixel_8", true],
    ]);
    const events = Array.from(dialog.querySelectorAll<HTMLInputElement>("[data-event]"));
    expect(events.map((box) => box.checked)).toEqual([true, true, true, false, false, false, false, false]);
  });

  it("saves the choice into a freshly read record under its revision and returns to Settings", async () => {
    const { hass, element } = await mounted(withNotifications(notifications()));
    openSettings(element);
    openDialog(element)!.querySelector<HTMLButtonElement>("[data-edit-notifications]")!.click();
    const dialog = openDialog(element)!;
    const ipad = dialog.querySelector<HTMLInputElement>("[data-target='mobile_app_ipad']")!;
    ipad.checked = true;
    ipad.dispatchEvent(new Event("change"));
    const plugged = dialog.querySelector<HTMLInputElement>("[data-event='plugged_in']")!;
    plugged.checked = true;
    plugged.dispatchEvent(new Event("change"));
    dialog.querySelector<HTMLFormElement>("form[data-notifications-editor]")!.requestSubmit();
    await settle();
    expect(openDialog(element)).toBeNull();
    const current = wire();
    hass.resolveNext({ api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: { ...current, revision: 9 }, pause: { choice: null, admitted_at: null, expires_at: null } });
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings")!;
    expect(update["expected_revision"]).toBe(9);
    expect(update["settings"]).toMatchObject({
      amps: 10,
      notifications: {
        targets: ["mobile_app_ipad"],
        events: ["plan_stopped", "plan_at_risk", "charge_complete", "plugged_in"],
      },
    });
  });

  it("says when no phone has the app", async () => {
    const { element } = await mounted(withNotifications(notifications({ available: [] })));
    openSettings(element);
    openDialog(element)!.querySelector<HTMLButtonElement>("[data-edit-notifications]")!.click();
    expect(openDialog(element)!.textContent).toContain(translate("en", "notifications.noPhones"));
  });

  it("cannot be changed by a reader who is not an administrator", async () => {
    const { element } = await mounted(withNotifications(notifications()), false);
    openSettings(element);
    const button = openDialog(element)!.querySelector<HTMLButtonElement>("[data-edit-notifications]")!;
    expect(button.disabled).toBe(true);
  });

  it("is absent for a backend without notifications", async () => {
    const payload = fixture("start_idle");
    delete (payload["settings"] as Record<string, unknown>)["notifications"];
    const { element } = await mounted(payload);
    openSettings(element);
    expect(openDialog(element)!.querySelector("[data-section='notifications']")).toBeNull();
  });
});

describe("the hint for events on and no recipients", () => {
  it("is given only when events are on and no phone is chosen", () => {
    const text = translate("en", "notifications.noRecipients");
    expect(text).toBe("No recipients chosen \u2013 this charger sends no notifications.");
    expect(noRecipientsHint("en", notifications())).toBe(text);
    expect(noRecipientsHint("en", notifications({ targets: ["mobile_app_ipad"] }))).toBeNull();
    expect(noRecipientsHint("en", notifications({ events: [] }))).toBeNull();
    expect(translate("sv", "notifications.noRecipients")).toBe(
      "Inga mottagare valda \u2013 den h\u00e4r laddaren skickar inga notiser.",
    );
    for (const language of LANGUAGES) {
      expect(translate(language, "notifications.noRecipients").length).toBeGreaterThan(10);
    }
  });

  it("shows in the Notifications section and its dialog, and not once a phone is chosen", async () => {
    const hint = translate("en", "notifications.noRecipients");
    const { element } = await mounted(withNotifications(notifications()));
    openSettings(element);
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='notifications']")!;
    expect(section.querySelector("[data-notice='notifications_no_targets']")?.textContent).toBe(hint);
    section.querySelector<HTMLButtonElement>("[data-edit-notifications]")!.click();
    expect(openDialog(element)!.querySelector("[data-notice='notifications_no_targets']")?.textContent).toBe(hint);

    const chosen = await mounted(withNotifications(notifications({ targets: ["mobile_app_pixel_8"] })));
    openSettings(chosen.element);
    expect(openDialog(chosen.element)!.querySelector("[data-notice='notifications_no_targets']")).toBeNull();
  });

  it("is not given when no event is on", async () => {
    const { element } = await mounted(withNotifications(notifications({ events: [] })));
    openSettings(element);
    expect(openDialog(element)!.querySelector("[data-notice='notifications_no_targets']")).toBeNull();
  });
});
