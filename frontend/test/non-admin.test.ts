// A wall screen signed in as a Home Assistant user who is not an administrator: Start, Stop, Pause and Resume
// work (the backend says `control.can_act`), while the strategy, the plan and every setting are read only, and
// the card offers no Settings when nothing there is the reader's to change.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { ACTION_API_VERSION, SETTINGS_API_VERSION } from "../src/types";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import { FakeHass, mountCard, settingsOffered, shownSections } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

/** The dashboard as `spotnav/get_dashboard` answers a reader who is not an administrator. */
function readerDashboard(): Record<string, unknown> {
  return JSON.parse(
    readFileSync(join(DASHBOARD_DIR, "cheapest_direct_site_read_only.json"), "utf8"),
  ) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mountedReader(): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("reader", "en");
  snapshot.user = { is_admin: false };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(readerDashboard());
  await settle();
  return { hass, element };
}

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

const sent = (hass: FakeHass, type: string) => hass.messages.filter((message) => message.type === type);
const writes = (hass: FakeHass) =>
  hass.messages.filter((message) => typeof message.type === "string" && /update|choose|write/.test(message.type));

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("a reader who is not an administrator", () => {
  it("starts a charge with one request, and the card shows no error", async () => {
    const { hass, element } = await mountedReader();
    const start = shadow(element).querySelector<HTMLButtonElement>(`.${C.actionButton}`);
    expect(start?.dataset["action"]).toBe("start");
    expect(start?.disabled).toBe(false);

    start?.click();
    expect(sent(hass, "spotnav/manual_action")).toEqual([
      { type: "spotnav/manual_action", api_version: ACTION_API_VERSION, charger_id: "entry_a", action: "start" },
    ]);
    hass.resolveNext({ api_version: ACTION_API_VERSION, ok: true, error: null, action: "start", choice: null });
    await settle();
    hass.resolveNext(readerDashboard());
    await settle();
    expect(shadow(element).textContent).not.toContain(translate("en", "action.error.generic"));
  });

  it("may pause the schedule: the pause choices are enabled", async () => {
    const { element } = await mountedReader();
    const pause = shadow(element).querySelector<HTMLButtonElement>(`.${C.plannerButton}`);
    expect(pause?.disabled).toBe(false);
    pause?.click();
    const choices = Array.from(openDialog(element)?.querySelectorAll<HTMLButtonElement>("[data-choice]") ?? []);
    expect(choices.length).toBeGreaterThan(0);
    expect(choices.every((choice) => !choice.disabled)).toBe(true);
  });

  it("reads the strategy without changing it: every row disabled, the read-only note, nothing sent", async () => {
    const { hass, element } = await mountedReader();
    const before = hass.messages.length;
    shadow(element).querySelector<HTMLButtonElement>(`.${C.strategyButton}`)?.click();
    const dialog = openDialog(element);
    expect(dialog?.querySelector(`.${C.settingsReadOnly}`)?.textContent).toBe(translate("en", "settings.readOnly"));
    const rows = Array.from(dialog?.querySelectorAll<HTMLButtonElement>("[data-strategy]") ?? []);
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((row) => row.disabled)).toBe(true);
    // No way from here into Settings either: the solar and site links are an administrator's.
    expect(dialog?.querySelector("[data-action='setup-solar'], [data-action='setup-site']")).toBeNull();
    expect(hass.messages).toHaveLength(before);
  });

  it("reads the plan without changing it: a read-only form, no Save, no error", async () => {
    const { hass, element } = await mountedReader();
    shadow(element).querySelector<HTMLButtonElement>("[data-setting='plan']")?.click();
    await settle();
    expect(sent(hass, "spotnav/get_settings")).toHaveLength(1);
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      // The settings section of the same dashboard is the record `spotnav/get_settings` answers.
      settings: readerDashboard()["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const dialog = openDialog(element);
    expect(dialog?.querySelector(`.${C.settingsReadOnly}`)?.textContent).toBe(translate("en", "settings.readOnly"));
    expect(dialog?.querySelector(`.${C.settingsSave}`)).toBeNull();
    expect(dialog?.querySelector(`.${C.settingsNotice}:not([hidden])`)?.textContent ?? "").toBe("");
    expect(writes(hass)).toHaveLength(0);
  });

  it("is shown only Display in Settings, and so no price area to change", async () => {
    const { hass, element } = await mountedReader();
    expect(settingsOffered(element)).toBe(true);
    shadow(element).querySelector<HTMLButtonElement>("[data-settings='open']")?.click();
    expect(shownSections(element)).toEqual(["display"]);
    expect(shadow(element).querySelector("[data-setting='market'], [data-edit='area']")).toBeNull();
    expect(sent(hass, "spotnav/get_market_options")).toHaveLength(0);
  });
});
