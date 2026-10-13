// A wall screen signed in as a Home Assistant user who is not an administrator: Start, Stop, Pause and Resume
// work (the backend says `control.can_act`), while the strategy, the plan and every setting are read only, and
// the card offers no Settings when nothing there is the reader's to change.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { ACTION_API_VERSION, SETTINGS_API_VERSION } from "../src/types";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import { FakeHass, mountCard, settingsOffered, shownSections } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

/** The dashboard as `spotnav/get_dashboard` answers a reader who is not an administrator (or the named fixture). */
function readerDashboard(name = "cheapest_direct_site_read_only"): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

/** The same dashboard where an administrator let users who are not administrators change the strategy and plan. */
function lettingUsersPlan(name?: string): Record<string, unknown> {
  const payload = readerDashboard(name);
  (payload["settings"] as Record<string, unknown>)["users_change_plan"] = true;
  return payload;
}

async function mountedReader(
  payload: Record<string, unknown> = readerDashboard(),
  admin = false,
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("reader", "en");
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

describe("a reader who is not an administrator, where the charger lets users change strategy and plan", () => {
  it("chooses a strategy: the available rows are buttons, and a choice is one settings write", async () => {
    // A site where every strategy can work, so there is one to choose.
    const { hass, element } = await mountedReader(lettingUsersPlan("solar_derived_site"));
    shadow(element).querySelector<HTMLButtonElement>(`.${C.strategyButton}`)?.click();
    const dialog = openDialog(element);
    expect(dialog?.querySelector(`.${C.settingsReadOnly}`)).toBeNull();
    const free = Array.from(dialog?.querySelectorAll<HTMLButtonElement>("[data-strategy]") ?? []).filter(
      (row) => !row.disabled,
    );
    expect(free.length).toBeGreaterThan(0);
    const chosen = free[0]!.dataset["strategy"];
    free[0]!.click();
    await settle();
    // The write reads the record fresh first, then replaces it.
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: lettingUsersPlan("solar_derived_site")["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const update = sent(hass, "spotnav/update_settings")[0] as Record<string, any> | undefined;
    expect(update?.["settings"]["strategy"]).toBe(chosen);
  });

  it("edits the plan: the form has Save and no read-only note", async () => {
    const { hass, element } = await mountedReader(lettingUsersPlan());
    shadow(element).querySelector<HTMLButtonElement>("[data-setting='plan']")?.click();
    await settle();
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: lettingUsersPlan()["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const dialog = openDialog(element);
    expect(dialog?.querySelector(`.${C.settingsReadOnly}`)).toBeNull();
    expect(dialog?.querySelector(`.${C.settingsSave}`)).not.toBeNull();
  });

  it("still sees only Display in Settings, and not the switch itself", async () => {
    const { element } = await mountedReader(lettingUsersPlan());
    shadow(element).querySelector<HTMLButtonElement>("[data-settings='open']")?.click();
    expect(shownSections(element)).toEqual(["display"]);
    expect(shadow(element).querySelector("[data-row='users_change_plan']")).toBeNull();
  });
});

describe("the administrator's switch", () => {
  it("is a row in the charger's section, Off by default, and turning it on is one settings write", async () => {
    const { hass, element } = await mountedReader(readerDashboard(), true);
    shadow(element).querySelector<HTMLButtonElement>("[data-settings='open']")?.click();
    const row = openDialog(element)?.querySelector<HTMLElement>("[data-section='entities'] [data-row='users_change_plan']");
    expect(row?.textContent).toBe(translate("en", "settings.usersPlan.label") + translate("en", "settings.value.off"));
    row?.querySelector<HTMLButtonElement>("[data-edit='users_change_plan']")?.click();
    await settle();
    const form = openDialog(element)?.querySelector<HTMLFormElement>("form[data-value-editor='single']");
    expect(form?.textContent).toContain(translate("en", "settings.usersPlan.help"));
    form?.querySelector<HTMLInputElement>("[data-value-option='on']")?.click();
    form?.requestSubmit();
    await settle();
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: readerDashboard()["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const update = sent(hass, "spotnav/update_settings")[0] as Record<string, any> | undefined;
    expect(update?.["settings"]["users_change_plan"]).toBe(true);
  });

  it("has its words in every language, in Swedish as asked", () => {
    for (const language of LANGUAGES) {
      for (const key of ["settings.usersPlan.label", "settings.usersPlan.help"] as const) {
        expect(translate(language, key)).not.toBe(key);
      }
    }
    expect(translate("sv", "settings.usersPlan.label")).toBe("Låt användare som inte är administratörer ändra strategi och plan");
  });
});
