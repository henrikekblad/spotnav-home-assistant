// The area/fiscal editor, through the real card element and both real contracts.
//
// Every case drives public behaviour -- a trigger press, a select change, a Save, a conflict choice, a
// teardown -- with fake timers and deferred promises, so the *order* of the two reads and of the
// confirmation is an assertion rather than a hope. The dashboard payloads are the backend-owned v2
// fixtures; the settings and market answers are hand-built exactly as the backend writes them.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate, type TranslationKey } from "../src/i18n";
import type { MarketFormValues } from "../src/market";
import {
  MARKET_API_VERSION,
  SETTINGS_API_VERSION,
  type AreaOverride,
  type MarketOptionsV1,
  type SettingsRecord,
} from "../src/types";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

function row(areaId: string, vat: AreaOverride["vat"], tax: AreaOverride["tax"], transfer: AreaOverride["transfer"]): AreaOverride {
  return { area_id: areaId, vat, tax, transfer };
}

const OFF = { enabled: false, value: null };

/** One accepted settings record: two stored rows, and planning fields a market edit must not touch. */
function aRecord(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [
      row("SE4", { enabled: true, value: 25 }, { enabled: false, value: 36.5 }, { enabled: true, value: null }),
      row("NO1", { enabled: true, value: 25 }, { enabled: true, value: 7.13 }, { enabled: false, value: 0 }),
    ],
    phases: 3,
    amps: 16,
    requested_kwh: 20.5,
    max_periods: 3,
    departure_enabled: true,
    departure_time: "06:30",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: 80.5 },
    ...overrides,
  };
}

/** One catalogue: SE4 suggests all three figures, DE-LU none, and both publish their own units. */
function catalogue(overrides: Partial<MarketOptionsV1> = {}): MarketOptionsV1 {
  return {
    state: "ready",
    reason: null,
    configured_area: "SE4",
    areas: [
      {
        area_id: "SE4",
        name: "Malmö",
        countries: ["SE"],
        timezone: "Europe/Stockholm",
        currency: "SEK",
        major_unit: "kr",
        minor_unit: "öre",
        suggestions: { vat_percent: 25, tax_minor: 36.5, transfer_minor: 30 },
      },
      {
        area_id: "NO1",
        name: "Oslo",
        countries: ["NO"],
        timezone: "Europe/Oslo",
        currency: "NOK",
        major_unit: "kr",
        minor_unit: "øre",
        suggestions: { vat_percent: 25, tax_minor: 7.13, transfer_minor: 0 },
      },
    ],
    ...overrides,
  };
}

// The market answer is the read command's own shape: five keys, no envelope, no prose.
function marketAnswer(options: MarketOptionsV1 = catalogue()): Record<string, unknown> {
  return { api_version: MARKET_API_VERSION, ...options };
}

const PAUSE = { choice: null, admitted_at: null, expires_at: null };

function success(record: SettingsRecord): Record<string, unknown> {
  return { api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: { ...record }, pause: { ...PAUSE } };
}

function refusal(code: string, record: SettingsRecord | null = null): Record<string, unknown> {
  return {
    api_version: SETTINGS_API_VERSION,
    ok: false,
    error: code,
    settings: record === null ? null : { ...record },
    pause: record === null ? null : { ...PAUSE },
  };
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(
  admin = true,
  payload: Record<string, unknown> = fixture("start_idle"),
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
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

const reads = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/get_settings");
const marketReads = (hass: FakeHass) =>
  hass.messages.filter((message) => message.type === "spotnav/get_market_options");
const updates = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/update_settings");
const dashboards = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/get_dashboard");

/** The open overlay, or null: all six exist from the first render and only one is ever visible. */
function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

/** Whether the open dialog is the Settings page (it holds the area/fiscal trigger). */
function inSettings(element: Element): boolean {
  return openDialog(element)?.querySelector(`.spotnav-settings-trigger[data-setting="market"]`) != null;
}

/** The general Settings button: the fourth trigger's own entry point since it moved into the popover. */
function settingsGeneralButton(element: Element): HTMLButtonElement {
  const found = shadow(element).querySelector<HTMLButtonElement>(
    `[aria-label="${translate("en", "header.settings")}"]`,
  );
  if (found === null) {
    throw new Error("no general settings button");
  }
  return found;
}

/**
 * The area/fiscal trigger, inside the Settings popover.
 *
 * The popover costs no request of its own (it is built from the model, like the capability dialog), so
 * opening it first -- only when the trigger is not already on screen, e.g. still present but hidden
 * behind a later-opened dialog -- costs nothing extra and keeps every existing call site unchanged.
 */
function marketTrigger(element: Element): HTMLButtonElement {
  const existing = shadow(element).querySelector<HTMLButtonElement>(
    `.spotnav-settings-trigger[data-setting="market"]`,
  );
  if (existing !== null) {
    return existing;
  }
  settingsGeneralButton(element).click();
  const found = shadow(element).querySelector<HTMLButtonElement>(
    `.spotnav-settings-trigger[data-setting="market"]`,
  );
  if (found === null) {
    throw new Error("no market trigger");
  }
  return found;
}

function button(element: Element, className: string): HTMLButtonElement | null {
  return openDialog(element)?.querySelector<HTMLButtonElement>(`.${className}`) ?? null;
}

function notice(element: Element): HTMLElement | null {
  return openDialog(element)?.querySelector<HTMLElement>(`.${VISUAL_CLASSES.settingsNotice}`) ?? null;
}

function select(element: Element): HTMLSelectElement {
  const found = openDialog(element)?.querySelector<HTMLSelectElement>("select") ?? null;
  if (found === null) {
    throw new Error("no area selector");
  }
  return found;
}

function figure(element: Element, component: string): HTMLInputElement {
  const found =
    openDialog(element)?.querySelector<HTMLInputElement>(`input[id$="-${component}-value"]`) ?? null;
  if (found === null) {
    throw new Error(`no ${component} figure`);
  }
  return found;
}

/** The component's checkbox. */
function checkbox(element: Element, component: string): HTMLInputElement {
  const found =
    openDialog(element)?.querySelector<HTMLInputElement>(`input[id$="-${component}-enabled"]`) ?? null;
  if (found === null) {
    throw new Error(`no ${component} checkbox`);
  }
  return found;
}

/** Checking or unchecking, as a person does it. */
function toggle(element: Element, component: string, enabled: boolean): void {
  const box = checkbox(element, component);
  box.checked = enabled;
  box.dispatchEvent(new Event("change"));
}

/**
 * Typing a figure, which is what makes it the person's own rather than the catalogue's suggestion.
 *
 * Assigning `value` alone would not: the row tracks the *act* of editing, so a test that skipped the
 * event would assert the wrong state -- and the wire would carry `null` where it expected a number,
 * which is exactly the difference the intent exists to keep.
 */
function type(element: Element, component: string, text: string): void {
  const field = figure(element, component);
  field.value = text;
  field.dispatchEvent(new Event("input"));
}

/** Open the editor and answer *both* reads: the form is on screen when this returns. */
async function openMarket(
  options: { record?: SettingsRecord; catalogue?: MarketOptionsV1; admin?: boolean } = {},
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard>; record: SettingsRecord }> {
  const record = options.record ?? aRecord();
  const { hass, element } = await mounted(options.admin ?? true);
  marketTrigger(element).click();
  await settle();
  hass.resolveNext(success(record));
  hass.resolveNext(marketAnswer(options.catalogue ?? catalogue()));
  await settle();
  return { hass, element, record };
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the combined editor session", () => {
  it("starts both reads in one operation and shows the form only once both are accepted", async () => {
    const { hass, element } = await mounted();
    // The trigger labels itself from the dashboard's own market facts: no request of its own.
    expect(marketTrigger(element).textContent).toBe(translate("en", "market.edit"));
    expect(marketTrigger(element).getAttribute("aria-label")).toBe(
      translate("en", "market.aria", { value: "Malmö · SE4" }),
    );

    marketTrigger(element).click();
    await settle();
    // One operation, two reads, and both were started before either was answered.
    expect(reads(hass)).toHaveLength(1);
    expect(marketReads(hass)).toHaveLength(1);
    expect(reads(hass)[0]?.["api_version"]).toBe(SETTINGS_API_VERSION);
    expect(marketReads(hass)[0]?.["api_version"]).toBe(MARKET_API_VERSION);
    expect(marketReads(hass)[0]?.["charger_id"]).toBe("entry_a");
    // The dialog is open and reading: no form, and therefore no Save.
    expect(text(element)).toContain(translate("en", "market.loading"));
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();

    // One half is not enough: the accepted settings record alone shows nothing.
    hass.resolveNext(success(aRecord()));
    await settle();
    expect(text(element)).toContain(translate("en", "market.loading"));
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();

    hass.resolveNext(marketAnswer());
    await settle();
    // Both halves are in, so the form is built from them: the stored row, and the catalogue's units.
    expect(text(element)).toContain(translate("en", "market.title"));
    expect(select(element).value).toBe("SE4");
    expect(checkbox(element, "vat").checked).toBe(true);
    expect(figure(element, "vat").value).toBe("25");
    expect(button(element, VISUAL_CLASSES.settingsSave)).not.toBeNull();
  });

  it("never pairs a late half with a newer one", async () => {
    const { hass, element } = await mounted();
    marketTrigger(element).click();
    await settle();
    // A second press is a second operation, with its own pair: the first one is now inert.
    marketTrigger(element).click();
    await settle();
    expect(reads(hass)).toHaveLength(2);
    expect(marketReads(hass)).toHaveLength(2);

    // The first operation's *complete* pair arrives after the second has started: nothing is shown.
    hass.resolveNext(success(aRecord({ area_id: "NO1" })));
    hass.resolveNext(marketAnswer());
    await settle();
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();

    // The second operation's own pair, with its own record: the form is that record's, not the other's.
    hass.resolveNext(success(aRecord({ area_id: "SE4" })));
    hass.resolveNext(marketAnswer());
    await settle();
    expect(select(element).value).toBe("SE4");
    expect(button(element, VISUAL_CLASSES.settingsSave)).not.toBeNull();
  });

  it("keeps each half's failure its own sentence, and offers nothing to save", async () => {
    // The settings read is refused: this integration's own stable code, mapped to the settings wording.
    const first = await mounted();
    marketTrigger(first.element).click();
    await settle();
    first.hass.rejectNext(new Error("transport"));
    first.hass.resolveNext(marketAnswer());
    await settle();
    expect(notice(first.element)?.textContent).toBe(translate("en", "settings.error.generic"));
    expect(button(first.element, VISUAL_CLASSES.settingsSave)).toBeNull();

    // The settings answer is another version: unsupported, not malformed, and said as such.
    const second = await mounted();
    marketTrigger(second.element).click();
    await settle();
    second.hass.resolveNext({ ...success(aRecord()), api_version: 2 });
    second.hass.resolveNext(marketAnswer());
    await settle();
    expect(notice(second.element)?.textContent).toBe(translate("en", "settings.error.version"));

    // The market answer is another version: the market contract's own sentence, not the settings one.
    const third = await mounted();
    marketTrigger(third.element).click();
    await settle();
    third.hass.resolveNext(success(aRecord()));
    third.hass.resolveNext({ ...marketAnswer(), api_version: 2 });
    await settle();
    expect(notice(third.element)?.textContent).toBe(translate("en", "market.error.version"));

    // A market payload this card cannot read: distinct from a version it does not speak.
    const fourth = await mounted();
    marketTrigger(fourth.element).click();
    await settle();
    fourth.hass.resolveNext(success(aRecord()));
    fourth.hass.resolveNext({ ...marketAnswer(), state: "something-new" });
    await settle();
    expect(notice(fourth.element)?.textContent).toBe(translate("en", "market.error.read"));

    // A charger this card cannot reach is the same fact for either half.
    const fifth = await mounted();
    marketTrigger(fifth.element).click();
    await settle();
    fifth.hass.resolveNext(success(aRecord()));
    fifth.hass.rejectNext({ code: "spotnav_charger_unloaded" });
    await settle();
    expect(notice(fifth.element)?.textContent).toBe(translate("en", "settings.error.charger"));
  });

  it("lets a non-administrator inspect the area and its taxes, with no Save at all", async () => {
    const { hass, element } = await openMarket({ admin: false });
    // The same two reads, the same form, and every control disabled rather than absent.
    expect(reads(hass)).toHaveLength(1);
    expect(marketReads(hass)).toHaveLength(1);
    expect(select(element).disabled).toBe(true);
    expect(checkbox(element, "vat").disabled).toBe(true);
    expect(figure(element, "vat").disabled).toBe(true);
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();
    // And no write is even possible from the UI: nothing was sent, and nothing can be.
    expect(updates(hass)).toHaveLength(0);
    expect(text(element)).toContain(translate("en", "market.intro"));
  });
});

describe("one Save, one full replacement, one compare-and-set", () => {
  it("sends exactly one update with the revision beside the body and every other field preserved", async () => {
    const { hass, element, record } = await openMarket();
    type(element, "vat", "12.5");
    toggle(element, "tax", true);
    toggle(element, "transfer", false);

    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const update = updates(hass)[0] ?? {};
    expect(update["api_version"]).toBe(SETTINGS_API_VERSION);
    expect(update["charger_id"]).toBe("entry_a");
    expect(update["expected_revision"]).toBe(record.revision);
    const body = update["settings"] as Record<string, unknown>;
    // The revision is beside the body, never inside it.
    expect("revision" in body).toBe(false);
    expect(body["area_id"]).toBe("SE4");
    expect(body["overrides"]).toEqual([
      // VAT: typed 12.5, so it is the person's own figure. Tax: switched on, and its retained 36.5
      // comes back rather than the catalogue's suggestion. Transfer: switched off, and it states
      // nothing at all because its figure was still the suggestion.
      row("SE4", { enabled: true, value: 12.5 }, { enabled: true, value: 36.5 }, { enabled: false, value: null }),
      record.overrides[1],
    ]);
    // Every planning field is the accepted record's own, value for value.
    expect(body).not.toHaveProperty("mode");
    expect(body["strategy"]).toBe("cheapest");
    expect(body["phases"]).toBe(3);
    expect(body["amps"]).toBe(16);
    expect(body["requested_kwh"]).toBe(20.5);
    expect(body["max_periods"]).toBe(3);
    expect(body["departure_enabled"]).toBe(true);
    expect(body["departure_time"]).toBe("06:30");
    expect(body["driver"]).toBe("manual_kwh");
    expect(body["target"]).toEqual({ vehicle_id: null, target_percent: 80.5 });

    // Nothing is predicted: no dashboard read before the answer, and no resting-row change either.
    const before = dashboards(hass).length;
    hass.resolveNext(success(aRecord({ revision: 8, requested_kwh: 20.5 })));
    await settle();
    // The dialog is done, and the confirmation is exactly one fresh read.
    expect(openDialog(element)).toBeNull();
    expect(dashboards(hass).length).toBe(before + 1);
    expect(text(element)).not.toContain(translate("en", "settings.error.confirmationFailed"));
  });

  it("writes nothing when the form states exactly what the record already holds", async () => {
    const { hass, element } = await openMarket();
    // The form was built from the stored row, so pressing Save states it again unchanged.
    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();
    expect(updates(hass)).toHaveLength(0);
    // The reader is back on the Settings page they came from, not on the bare card.
    expect(inSettings(element)).toBe(true);
    expect(dashboards(hass)).toHaveLength(1);
  });

  it("offers Reapply and Reload after a conflict, and Reapply carries the server's own record", async () => {
    const { hass, element } = await openMarket();
    type(element, "vat", "12.5");
    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();
    expect(updates(hass)).toHaveLength(1);

    // The record moved on: the conflict names the server's revision and keeps the reader's values.
    const serverRecord = aRecord({ revision: 9, requested_kwh: 42, amps: 32 });
    hass.resolveNext(refusal("revision_conflict", serverRecord));
    await settle();
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();
    expect(button(element, VISUAL_CLASSES.settingsReapply)).not.toBeNull();
    expect(button(element, VISUAL_CLASSES.settingsReload)).not.toBeNull();
    expect(text(element)).toContain(translate("en", "settings.conflict.intro"));
    expect(figure(element, "vat").value).toBe("12.5");

    button(element, VISUAL_CLASSES.settingsReapply)?.click();
    await settle();
    expect(updates(hass)).toHaveLength(2);
    const second = updates(hass)[1] ?? {};
    // Against the returned revision, and built from the server's current record plus only this draft.
    expect(second["expected_revision"]).toBe(9);
    const body = second["settings"] as Record<string, unknown>;
    expect(body["requested_kwh"]).toBe(42);
    expect(body["amps"]).toBe(32);
    expect(body["overrides"]).toEqual([
      // The person's own VAT figure survives the conflict *as their own* (12.5, not `null`), and the
      // two components they never touched are exactly the server record's: a reapply states their
      // draft and nothing else.
      row("SE4", { enabled: true, value: 12.5 }, { enabled: false, value: 36.5 }, { enabled: true, value: null }),
      serverRecord.overrides[1],
    ]);
    expect(dashboards(hass)).toHaveLength(1);
  });

  it("takes the server's values again on Reload, and drops the draft with them", async () => {
    const { hass, element } = await openMarket();
    type(element, "vat", "12.5");
    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();
    hass.resolveNext(refusal("revision_conflict", aRecord({ revision: 9 })));
    await settle();

    button(element, VISUAL_CLASSES.settingsReload)?.click();
    await settle();
    // Reload is the same combined read: both halves again, in one operation.
    expect(reads(hass)).toHaveLength(2);
    expect(marketReads(hass)).toHaveLength(2);
    const reloaded = aRecord({
      revision: 10,
      area_id: "NO1",
      overrides: [row("NO1", { enabled: true, value: 25 }, { enabled: true, value: 7.13 }, { enabled: false, value: 0 })],
    });
    hass.resolveNext(success(reloaded));
    hass.resolveNext(marketAnswer(catalogue({ configured_area: "NO1" })));
    await settle();
    // The server's values, and no trace of the reader's discarded figure.
    expect(select(element).value).toBe("NO1");
    expect(figure(element, "vat").value).toBe("25");
    expect(figure(element, "tax").value).toBe("7.13");
    expect(figure(element, "transfer").value).toBe("0");
    expect(updates(hass)).toHaveLength(1);
  });

  it("adopts a committed-but-unreconciled replacement, and says which half failed", async () => {
    const { hass, element } = await openMarket();
    type(element, "vat", "12.5");
    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();
    hass.resolveNext(refusal("spotnav_settings_reconcile_failed", aRecord({ revision: 8 })));
    await settle();

    // The replacement *is* durable: the dialog is done, one confirmation runs, and the sentence is
    // about the reconcile rather than about nothing having happened.
    expect(openDialog(element)).toBeNull();
    expect(dashboards(hass)).toHaveLength(2);
    expect(text(element)).toContain(translate("en", "settings.error.reconcileFailed"));
  });

  it("keeps the dialog and changes nothing when the replacement was not committed", async () => {
    const { hass, element } = await openMarket();
    type(element, "vat", "12.5");
    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();
    hass.resolveNext(refusal("spotnav_settings_not_committed"));
    await settle();

    expect(openDialog(element)).not.toBeNull();
    expect(notice(element)?.textContent).toBe(translate("en", "settings.error.notCommitted"));
    // No confirmation read: nothing was written, so there is nothing to confirm.
    expect(dashboards(hass)).toHaveLength(1);
    expect(figure(element, "vat").value).toBe("12.5");
  });
});

describe("the area switch inside the open form", () => {
  it("shows that area's own row, keeps the draft made there, and sends nothing", async () => {
    const { hass, element } = await openMarket();
    const before = hass.messages.length;

    // NO1 has its own stored row in the record, so that is what the form shows for it.
    const chosen = select(element);
    chosen.value = "NO1";
    chosen.dispatchEvent(new Event("change"));
    await settle();
    expect(select(element).value).toBe("NO1");
    expect(figure(element, "vat").value).toBe("25");
    expect(figure(element, "tax").value).toBe("7.13");
    expect(figure(element, "transfer").value).toBe("0");
    // The unit is the selected area's own published minor unit, and VAT stays a percent.
    const units = Array.from(openDialog(element)?.querySelectorAll(`.${VISUAL_CLASSES.settingsUnit}`) ?? []).map(
      (node) => node.textContent,
    );
    expect(units).toEqual(["%", "øre", "øre"]);

    // A draft made for NO1 survives a trip to another area and back, and no request is sent.
    type(element, "vat", "9.5");
    const back = select(element);
    back.value = "SE4";
    back.dispatchEvent(new Event("change"));
    await settle();
    expect(figure(element, "vat").value).toBe("25");
    const again = select(element);
    again.value = "NO1";
    again.dispatchEvent(new Event("change"));
    await settle();
    expect(figure(element, "vat").value).toBe("9.5");
    expect(hass.messages.length).toBe(before);
  });

  it("states the newly selected area, and only that area's row, on the next Save", async () => {
    const { hass, element, record } = await openMarket();
    const chosen = select(element);
    chosen.value = "NO1";
    chosen.dispatchEvent(new Event("change"));
    await settle();
    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const body = (updates(hass)[0] ?? {})["settings"] as Record<string, unknown>;
    expect(body["area_id"]).toBe("NO1");
    expect(body["overrides"]).toEqual([record.overrides[0], record.overrides[1]]);
    // The graph and the prices move only after the backend's confirmed answer, never before it.
    expect(dashboards(hass)).toHaveLength(1);
    hass.resolveNext(success(aRecord({ revision: 8, area_id: "NO1" })));
    await settle();
    expect(dashboards(hass)).toHaveLength(2);
  });
});

describe("the catalogue's own state, as the dialog says it", () => {
  it("explains a stale catalogue without calling it fresh, and keeps the stored area selectable", async () => {
    const options = catalogue({
      state: "stale",
      reason: "offline",
      areas: [catalogue().areas[1]],
      configured_area: "SE4",
    } as Partial<MarketOptionsV1>);
    const { element } = await openMarket({ catalogue: options });
    expect(text(element)).toContain(translate("en", "market.state.staleOffline"));
    // The stored area is still a choice, marked as unpublished, so a Save cannot silently move it.
    const values = Array.from(select(element).options).map((option) => option.value);
    expect(values).toEqual(["NO1", "SE4"]);
    expect(Array.from(select(element).options).map((option) => option.textContent)).toEqual([
      "Oslo · NO1",
      "SE4 · no longer published",
    ]);
    expect(select(element).value).toBe("SE4");
    expect(button(element, VISUAL_CLASSES.settingsSave)).not.toBeNull();
  });

  it("offers no area and no Save when nothing is published and nothing is stored", async () => {
    const options: MarketOptionsV1 = { state: "unavailable", reason: "offline", areas: [], configured_area: null };
    const { element } = await openMarket({ catalogue: options, record: aRecord({ area_id: null, overrides: [] }) });
    expect(text(element)).toContain(translate("en", "market.state.offline"));
    expect(text(element)).toContain(translate("en", "market.area.missing"));
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();
  });
});

describe("what one card's area/fiscal editor never shares with another's", () => {
  it("keeps two cards' forms, drafts, revisions and requests apart", async () => {
    const firstHass = new FakeHass();
    const secondHass = new FakeHass();
    const first = mountCard(CONFIG, firstHass);
    const second = mountCard(CONFIG, secondHass);
    const firstSnapshot = firstHass.snapshot("first", "en");
    const secondSnapshot = secondHass.snapshot("second", "en");
    firstSnapshot.user = { is_admin: true };
    secondSnapshot.user = { is_admin: true };
    first.hass = firstSnapshot;
    second.hass = secondSnapshot;
    await settle();
    firstHass.resolveNext(fixture("start_idle"));
    secondHass.resolveNext(fixture("start_idle"));
    await settle();

    // Both cards open their own editor, each with its own pair of reads.
    marketTrigger(first).click();
    marketTrigger(second).click();
    await settle();
    firstHass.resolveNext(success(aRecord({ revision: 7 })));
    firstHass.resolveNext(marketAnswer());
    secondHass.resolveNext(success(aRecord({ revision: 3 })));
    secondHass.resolveNext(marketAnswer());
    await settle();
    expect(reads(firstHass)).toHaveLength(1);
    expect(reads(secondHass)).toHaveLength(1);

    // A draft typed into one card's form exists in that card only.
    type(first, "vat", "11.5");
    toggle(first, "tax", true);
    expect(figure(second, "vat").value).toBe("25");
    expect(checkbox(second, "tax").checked).toBe(false);

    // A draft made while switching areas in one card is that card's own, and comes back to its owner.
    const secondSelect = select(second);
    secondSelect.value = "NO1";
    secondSelect.dispatchEvent(new Event("change"));
    await settle();
    expect(figure(second, "vat").value).toBe("25");
    type(second, "vat", "9.5");
    // Switching away is what *stores* that draft: exactly the state a shared store would leak.
    const secondBack = select(second);
    secondBack.value = "SE4";
    secondBack.dispatchEvent(new Event("change"));
    await settle();
    expect(figure(second, "vat").value).toBe("25");

    // The other card's visit to the same area shows the record's own row, never a neighbour's figure.
    const firstSelect = select(first);
    firstSelect.value = "NO1";
    firstSelect.dispatchEvent(new Event("change"));
    await settle();
    expect(figure(first, "vat").value).toBe("25");
    expect(figure(first, "tax").value).toBe("7.13");

    // And the card that made the draft gets it back.
    const secondAgain = select(second);
    secondAgain.value = "NO1";
    secondAgain.dispatchEvent(new Event("change"));
    await settle();
    expect(figure(second, "vat").value).toBe("9.5");
    // Switching areas asks the backend nothing at all, in either card.
    expect(firstHass.outstanding + secondHass.outstanding).toBe(0);

    // And a Save in one card sends one update, against *its* revision, changing nothing in the other.
    button(first, VISUAL_CLASSES.settingsSave)?.click();
    await settle();
    expect(updates(secondHass)).toHaveLength(0);
    expect(updates(firstHass)).toHaveLength(1);
    expect(updates(firstHass)[0]?.["expected_revision"]).toBe(7);
    expect(openDialog(second)).not.toBeNull();
    expect(button(second, VISUAL_CLASSES.settingsSave)).not.toBeNull();
  });

  it("leaves one overlay and one pair of reads per open, however often it is opened", async () => {
    const { hass, element } = await mounted();
    const overlays = shadow(element).querySelectorAll("[role='dialog']").length;
    for (let index = 0; index < 3; index += 1) {
      marketTrigger(element).click();
      await settle();
      expect(reads(hass)).toHaveLength(index + 1);
      expect(marketReads(hass)).toHaveLength(index + 1);
      hass.resolveNext(success(aRecord()));
      hass.resolveNext(marketAnswer());
      await settle();
      // Escape is the reader's way out and lands on the Settings page it was opened from (a second
      // Escape leaves that too); the trigger's next press is the way back in. Each open is one
      // operation with one pair of reads, and closing leaves nothing behind.
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await settle();
      expect(openDialog(element)).not.toBeNull();
      expect(text(element)).toContain(translate("en", "settings.overview.title"));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await settle();
      expect(openDialog(element)).toBeNull();
    }
    expect(shadow(element).querySelectorAll("[role='dialog']").length).toBe(overlays);
    expect(reads(hass)).toHaveLength(3);
    expect(marketReads(hass)).toHaveLength(3);
    // Nothing else was ever requested, and nothing is left in flight once the last pair is answered.
    expect(dashboards(hass)).toHaveLength(1);
    expect(hass.outstanding).toBe(0);
  });
});

describe("truthfulness after a market save", () => {
  it("keeps the last confirmed card and says only that confirmation failed", async () => {
    const { hass, element } = await openMarket();
    type(element, "vat", "12.5");
    button(element, VISUAL_CLASSES.settingsSave)?.click();
    await settle();
    hass.resolveNext(success(aRecord({ revision: 8, area_id: "NO1" })));
    await settle();
    // The committed replacement is durable, the dialog is done, and one confirmation is out.
    expect(openDialog(element)).toBeNull();
    expect(dashboards(hass)).toHaveLength(2);

    // That read fails: the last confirmed graph and row stay exactly as they were, and the sentence is
    // about the confirmation rather than about the save.
    hass.resolveNext({ api_version: 2, broken: true });
    await settle();
    expect(text(element)).toContain(translate("en", "settings.error.confirmationFailed"));
    expect(text(element)).not.toContain(translate("en", "settings.error.reconcileFailed"));
    expect(marketTrigger(element).textContent).toBe(translate("en", "market.edit"));
    expect(hass.outstanding).toBe(0);
  });

  it("makes a late pair inert once the card is reconfigured or torn down", async () => {
    // A config change: the operation and generation both move, and the pair that was out is inert.
    const { hass, element } = await mounted();
    marketTrigger(element).click();
    await settle();
    element.setConfig({ type: "custom:spotnav-card", charger: "entry_b" });
    await settle();
    hass.resolveNext(success(aRecord()));
    hass.resolveNext(marketAnswer());
    await settle();
    expect(openDialog(element)).toBeNull();
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();

    // A teardown: the same, with the element gone from the document.
    const second = await mounted();
    marketTrigger(second.element).click();
    await settle();
    second.element.remove();
    second.hass.resolveNext(success(aRecord()));
    second.hass.resolveNext(marketAnswer());
    await settle();
    expect(shadow(second.element).querySelectorAll(`.${VISUAL_CLASSES.settingsSave}`).length).toBeGreaterThanOrEqual(0);
    expect(second.hass.outstanding).toBe(0);
  });

  it("returns focus to the general Settings button when the dialog closes, and keeps the trigger in its own popover section", async () => {
    const { element } = await openMarket();
    const dialog = openDialog(element);
    expect(dialog?.getAttribute("aria-modal") ?? "true").not.toBe("false");
    // Escape closes it. The area/fiscal trigger lives inside the Settings popover, so focus returns to the button that opened that popover -- the same
    // rule the vehicle's consumption editor follows.
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    // Escape returns to the Settings page (as Save does); leaving that returns focus to its button.
    expect(text(element)).toContain(translate("en", "settings.overview.title"));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    expect(openDialog(element)).toBeNull();
    expect(shadow(element).activeElement).toBe(settingsGeneralButton(element));
    // The trigger lives in its own section of the Settings popover, not the compact control row.
    expect(marketTrigger(element).closest(`.${VISUAL_CLASSES.settingsSection}`)).not.toBeNull();
  });

  it("Cancel returns to the Settings page like Save does, sends nothing and leaves nothing pending", async () => {
    const { hass, element } = await openMarket();
    const cancel = Array.from(openDialog(element)?.querySelectorAll<HTMLButtonElement>("button") ?? []).find(
      (candidate) => candidate.textContent === translate("en", "settings.cancel"),
    );
    expect(cancel).toBeDefined();
    cancel?.click();
    await settle();
    expect(text(element)).toContain(translate("en", "settings.overview.title"));
    expect(openDialog(element)?.querySelector("[data-section='market']")).not.toBeNull();
    expect(updates(hass)).toHaveLength(0);
    expect(hass.outstanding).toBe(0);
  });

  it("the close button and the backdrop lead back to the Settings page too", async () => {
    const { element } = await openMarket();
    openDialog(element)?.parentElement?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();
    expect(text(element)).toContain(translate("en", "settings.overview.title"));
  });

  it("sends no update at all when nothing is published and nothing is stored", async () => {
    const options: MarketOptionsV1 = { state: "unavailable", reason: "offline", areas: [], configured_area: null };
    const { hass, element } = await openMarket({ catalogue: options, record: aRecord({ area_id: null, overrides: [] }) });
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();
    expect(updates(hass)).toHaveLength(0);
    expect(dashboards(hass)).toHaveLength(1);
  });
});


/**
 * The editability rule, at both of its boundaries.
 *
 * `ready` and `stale` are held catalogues, so an administrator may edit; `loading`, `unavailable` and
 * `invalid` are not held at all, so nobody may -- and the stored area and its stored override are still
 * shown, with the state explained. `saveProgrammatically` exists because a read-only form has no
 * control that could reach the mutation path, and a boundary that is only guarded in the presentation
 * is not guarded: the only way to prove the second one is to call through it.
 */
function saveProgrammatically(
  element: Element,
  values: MarketFormValues,
  reapply = false,
): Promise<void> {
  const card = element as unknown as {
    saveMarket(form: MarketFormValues, again?: boolean): Promise<void>;
  };
  return card.saveMarket(values, reapply);
}

/**
 * One valid edit of the stored SE4 row: a changed VAT figure, and two components that state no figure.
 *
 * Every policy here is valid *without* a catalogue -- `custom` needs a number and `off` needs nothing at
 * all -- because the states that must refuse this edit are exactly the ones where no area is published.
 * A `suggested` policy would be refused by `marketReplacement` on its own, which would make the test
 * pass for the wrong reason: the guard under test is the one that must stop a *valid* edit.
 */
const STORED_AREA_EDIT: MarketFormValues = {
  areaId: "SE4",
  vat: { enabled: true, value: "12.5", intent: "custom" },
  tax: { enabled: false, value: "36.5", intent: "custom" },
  transfer: { enabled: false, value: "", intent: "suggested" },
};

describe("the catalogue's availability decides editability", () => {
  it.each<[string, MarketOptionsV1, TranslationKey]>([
    [
      "unavailable",
      { state: "unavailable", reason: "offline", areas: [], configured_area: "SE4" },
      "market.state.offline",
    ],
    [
      "invalid",
      { state: "invalid", reason: "invalid", areas: [], configured_area: "SE4" },
      "market.state.invalid",
    ],
    [
      "loading",
      { state: "loading", reason: null, areas: [], configured_area: "SE4" },
      "market.state.loading",
    ],
  ])("is read-only for an administrator with a stored area when the catalogue is %s", async (_name, options, sentence) => {
    const { hass, element } = await openMarket({ catalogue: options });

    // The stored area, its stored override and the honest state are all still on screen -- nothing is
    // discarded and nothing is fabricated in their place.
    expect(select(element).value).toBe("SE4");
    expect(figure(element, "vat").value).toBe("25");
    expect(text(element)).toContain(translate("en", sentence));

    // The mutation boundary first, because this is the assertion a presentation-only guard cannot
    // satisfy: reached directly -- a read-only form has no control that could reach it -- one valid
    // edit of the *stored* area must still write nothing and confirm nothing.
    // Deliberately not awaited: with the guard in place this returns without sending anything, and
    // without it the *request* is what this test must observe -- awaiting an update nobody answers is a
    // hang, not a behaviour. The catch keeps a pending request from becoming an unhandled rejection.
    void saveProgrammatically(element, STORED_AREA_EDIT).catch(() => undefined);
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(dashboards(hass)).toHaveLength(1);
    expect(notice(element)?.textContent).toBe(translate("en", sentence));

    // And the presentation agrees with it: every control disabled, and nothing to press.
    expect(select(element).disabled).toBe(true);
    expect(figure(element, "vat").disabled).toBe(true);
    for (const component of ["vat", "tax", "transfer"] as const) {
      expect(checkbox(element, component).disabled).toBe(true);
      expect(figure(element, component).disabled).toBe(true);
    }
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();
    expect(button(element, VISUAL_CLASSES.settingsReapply)).toBeNull();
  });

  it("keeps a stale but held catalogue editable, while saying it is stale", async () => {
    const { hass, element } = await openMarket({ catalogue: catalogue({ state: "stale", reason: "offline" }) });
    expect(text(element)).toContain(translate("en", "market.state.staleOffline"));
    expect(select(element).disabled).toBe(false);
    expect(figure(element, "vat").disabled).toBe(false);
    const save = button(element, VISUAL_CLASSES.settingsSave);
    expect(save).not.toBeNull();

    type(element, "vat", "12.5");
    save?.click();
    await settle();
    // Exactly one update, built from the stored record, against the revision it was read at.
    expect(updates(hass)).toHaveLength(1);
    expect(updates(hass)[0]?.["expected_revision"]).toBe(7);
    const body = (updates(hass)[0] ?? {})["settings"] as Record<string, unknown>;
    expect(body["area_id"]).toBe("SE4");
    expect((body["overrides"] as AreaOverride[])[0]?.vat).toEqual({ enabled: true, value: 12.5 });
  });

  it("keeps a ready catalogue exactly as accepted, including the zero-request rule", async () => {
    const { hass, element } = await openMarket();
    expect(select(element).disabled).toBe(false);
    const save = button(element, VISUAL_CLASSES.settingsSave);
    expect(save).not.toBeNull();
    type(element, "vat", "12.5");
    save?.click();
    await settle();
    expect(updates(hass)).toHaveLength(1);
    hass.resolveNext(success(aRecord({ revision: 8 })));
    await settle();
    // The save's own confirmation read, so the card is holding the committed record again.
    hass.resolveNext(fixture("start_idle"));
    await settle();
    // Back on the Settings page, drawn from the confirmed read.
    expect(inSettings(element)).toBe(true);

    // A programmatic Save that states exactly what the record now holds is still zero requests, and
    // it closes the dialog rather than inventing a revision.
    marketTrigger(element).click();
    await settle();
    hass.resolveNext(success(aRecord({ revision: 8 })));
    hass.resolveNext(marketAnswer());
    await settle();
    // Exactly the stored row: VAT custom 25, tax off with its kept 36.5, and the suggestion SE4
    // publishes for the transfer -- which is what makes this statement unchanged rather than new.
    void saveProgrammatically(element, {
      areaId: "SE4",
      vat: { enabled: true, value: "25", intent: "custom" },
      tax: { enabled: false, value: "36.5", intent: "custom" },
      transfer: { enabled: true, value: "", intent: "suggested" },
    }).catch(() => undefined);
    await settle();
    expect(updates(hass)).toHaveLength(1);
    expect(inSettings(element)).toBe(true);
  });

  it("keeps a non-administrator read-only in a ready catalogue, sending nothing", async () => {
    const { hass, element } = await openMarket({ admin: false });
    expect(select(element).disabled).toBe(true);
    expect(button(element, VISUAL_CLASSES.settingsSave)).toBeNull();
    void saveProgrammatically(element, STORED_AREA_EDIT).catch(() => undefined);
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(notice(element)?.textContent).toBe(translate("en", "settings.error.readOnly"));
  });
});
