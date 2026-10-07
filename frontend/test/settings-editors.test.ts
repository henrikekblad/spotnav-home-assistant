// The three compact planning editors, through the real card element and the real settings contract.
//
// Every case drives public behaviour -- a trigger press, an input, a Save, a conflict choice, a
// teardown -- with fake timers and deferred promises. The dashboard payloads are the backend-owned v2
// fixtures; the settings answers are hand-built exactly like `settings_contract.py` writes them.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate, type Language, type TranslationKey } from "../src/i18n";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

type Kind = "energy" | "deadline" | "current";

function aRecord(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [
      {
        area_id: "SE4",
        vat: { enabled: true, value: null },
        tax: { enabled: true, value: 0.0 },
        transfer: { enabled: true, value: 8.75 },
      },
      {
        area_id: "FI",
        vat: { enabled: false, value: null },
        tax: { enabled: false, value: null },
        transfer: { enabled: false, value: null },
      },
    ],
    phases: 3,
    amps: 10,
    requested_kwh: 20.5,
    max_periods: 4,
    departure_enabled: true,
    departure_time: "06:30",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: null },
    ...overrides,
  };
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

/** One mounted card with its dashboard read answered, in one Home Assistant language. */
async function mounted(
  payload: Record<string, unknown> = fixture("start_idle"),
  admin = true,
  language = "en",
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  if (admin) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

/** The open settings dialog, or null: the settings overlay exists from the first render. */
function editorDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

/** Which of the Plan popover's three sections a test is exercising: `inputs()` returns that one's controls. */
let focusKind: Kind = "energy";

function inputs(element: Element): HTMLInputElement[] {
  const dialog = editorDialog(element);
  // The popover's own order: energy, the deadline toggle, the time, current.
  const all = Array.from(dialog?.querySelectorAll<HTMLInputElement>(".spotnav-settings-input") ?? []);
  // The current, like the energy, is its slider alone: no number field beside it.
  const current = Array.from(dialog?.querySelectorAll<HTMLInputElement>("input[type='range'][id$='-current']") ?? []);
  // The departure date picker has its own helper (`dateField`); it is not one of the positional controls.
  const tail = all.filter((node) => node.type !== "date");
  if (focusKind === "energy") {
    // The energy editor's one control is its slider: no number field beside it.
    const slider = dialog?.querySelector<HTMLInputElement>("[data-part='energy'] input[type='range']");
    return dialog?.querySelector("[data-energy]") || slider == null ? [] : [slider];
  }
  if (focusKind === "current") {
    return current;
  }
  return tail;
}

/** Set a control's value the way a reader does, so its own listeners hear it. */
function enter(node: HTMLInputElement, value: string): void {
  node.value = value;
  node.dispatchEvent(new Event("input", { bubbles: true }));
}

/** The energy editor's value text, on its label row. */
function energyText(element: Element): string {
  return editorDialog(element)?.querySelector("[data-part='energy'] [data-part='energy-value']")?.textContent ?? "";
}

/** The current editor's value text, on its label row. */
function currentText(element: Element): string {
  return editorDialog(element)?.querySelector("[data-part='current-value']")?.textContent ?? "";
}

function button(element: Element, className: string): HTMLButtonElement | null {
  return editorDialog(element)?.querySelector<HTMLButtonElement>(`.${className}`) ?? null;
}

/**
 * The dialog's buttons, by accessible name: the primitive's own Close carries a glyph and a label, so
 * the label is what identifies it -- and the order here is the DOM order a tab walks.
 */
function dialogButtons(element: Element): string[] {
  // The Plan popover's link to Settings (shown while no charge-level source resolves) is not one of
  // its Save or conflict choices, so it is not counted among them.
  return Array.from(
    editorDialog(element)?.querySelectorAll<HTMLButtonElement>("button:not([data-soc='settings-link']):not([data-action^='date-'])") ?? [],
  ).map(
    (node) => node.getAttribute("aria-label") ?? node.textContent ?? "",
  );
}

/** The Plan cell: every section this file exercises lives in its one popover. */
function trigger(element: Element, kind: Kind): HTMLButtonElement {
  focusKind = kind;
  const found = shadow(element).querySelector<HTMLButtonElement>(
    `.spotnav-settings-trigger[data-setting="plan"]`,
  );
  if (found === null) {
    throw new Error(`no plan trigger`);
  }
  return found;
}

const reads = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/get_settings");
const updates = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/update_settings");
const dashboards = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/get_dashboard");

/** Open one editor and answer its read, so the form is on screen. */
const linePower = (phases: string, power: string): string =>
  translate("en", "settings.phases.line", { phases, power });

async function openEditor(
  kind: Kind,
  options: {
    record?: SettingsRecord;
    admin?: boolean;
    payload?: Record<string, unknown>;
    language?: string;
  } = {},
): Promise<{
  hass: FakeHass;
  element: ReturnType<typeof mountCard>;
  record: SettingsRecord;
}> {
  const record = options.record ?? aRecord();
  const { hass, element } = await mounted(
    options.payload ?? fixture("start_idle"),
    options.admin ?? true,
    options.language ?? "en",
  );
  trigger(element, kind).click();
  await settle();
  hass.resolveNext(success(record));
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

describe("the resting settings row", () => {
  it("shows the Plan cell with the dashboard's own values, after the graph", async () => {
    const { element } = await mounted();
    const row = shadow(element).querySelector(".spotnav-action-bar");

    expect(row).not.toBeNull();
    const triggers = Array.from(row?.querySelectorAll(".spotnav-settings-trigger") ?? []);
    // One planning cell: requested energy, finish by and current live together in its popover. The
    // area/fiscal editor and the vehicle's consumption live in the Settings popover instead.
    expect(triggers.map((node) => node.getAttribute("data-setting"))).toEqual(["plan"]);
    expect(triggers[0]?.querySelector(".spotnav-settings-value")?.textContent).toBe(
      "20 kWh \u00b7 No deadline \u00b7 10 A",
    );
    expect(rowValues(element)).toEqual(["20 kWh", "No deadline", "10 A"]);
    // All four captions are plain text: the Plan cell carries no icon.
    expect(triggers[0]?.querySelector("svg")).toBeNull();
    expect(triggers[0]?.getAttribute("aria-label")).toBe(
      `${translate("en", "bar.plan")}: 20 kWh \u00b7 No deadline \u00b7 10 A. ${translate("en", "bar.change")}`,
    );
    // The graph keeps priority: the row is after the chart and the periods, and before the context.
    const chart = shadow(element).querySelector(".spotnav-chart-viewport") as Element;
    const periods = shadow(element).querySelector(".spotnav-periods") as Element;
    expect(chart.compareDocumentPosition(row as Element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(periods.compareDocumentPosition(row as Element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The bar itself is six cells (charging, schedule, strategy + these three); this filter is the three.
    // No product name, phase value, revision or entity id anywhere in the row.
    const rowText = row?.textContent ?? "";
    expect(rowText).not.toContain("SpotNav");
    expect(rowText).not.toMatch(/revision|entity|phase/i);
    expect(shadow(element).querySelectorAll(".spotnav-settings-row select").length).toBe(0);
  });

  it.each([
    [42, "42 kWh"],
    [41.6, "41.6 kWh"],
    [20.25, "20.25 kWh"],
  ])(
    "states a stored %s kWh as that amount and not a rounded one, in the value and its name",
    async (requested, shown) => {
      const payload = fixture("start_idle");
      (payload["settings"] as Record<string, unknown>)["requested_kwh"] = requested;
      const { element } = await mounted(payload);

      const energyTrigger = shadow(element).querySelector(
        ".spotnav-settings-trigger[data-setting='plan']",
      );
      expect(rowValues(element)[0], String(requested)).toBe(shown);
      // The accessible name carries the same spelling: the row states one amount, one way.
      expect(energyTrigger?.getAttribute("aria-label")).toBe(
        `${translate("en", "bar.plan")}: ${shown} \u00b7 No deadline \u00b7 10 A. ${translate("en", "bar.change")}`,
      );
    },
  );
});

describe("the request lifecycle", () => {
  it("reads the full record once when a dialog opens, with the settings contract's own version", async () => {
    const { hass, element } = await openEditor("energy");

    expect(reads(hass)).toEqual([
      { type: "spotnav/get_settings", api_version: SETTINGS_API_VERSION, charger_id: "entry_a" },
    ]);
    expect(updates(hass)).toHaveLength(0);
    expect(text(element)).toContain(translate("en", "settings.energy.label"));
    expect(inputs(element)[0]?.value).toBe("20.5");
    expect(energyText(element)).toBe("20.5 kWh");
  });

  it("writes nothing when a dialog is opened and closed again", async () => {
    const { hass, element } = await openEditor("deadline");
    const readsBefore = reads(hass).length;

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();

    expect(editorDialog(element)).toBeNull();
    expect(reads(hass).length).toBe(readsBefore);
    expect(updates(hass)).toHaveLength(0);
    expect(dashboards(hass).length).toBe(1);
  });

  it("sends one full replacement per Save, with the revision beside the body and never inside it", async () => {
    const { hass, element, record } = await openEditor("current");
    enter(inputs(element)[0]!, "16");

    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const message = updates(hass)[0]!;
    expect(Object.keys(message).sort()).toEqual([
      "api_version",
      "charger_id",
      "expected_revision",
      "settings",
      "type",
    ]);
    expect(message["api_version"]).toBe(SETTINGS_API_VERSION);
    expect(message["charger_id"]).toBe("entry_a");
    expect(message["expected_revision"]).toBe(record.revision);
    const body = message["settings"] as Record<string, unknown>;
    expect(Object.keys(body)).not.toContain("revision");
    expect(body["amps"]).toBe(16);
  });

  it("changes only the fields one editor owns, and preserves every other value", async () => {
    const record = aRecord();
    const cases: Array<[Kind, string]> = [
      ["energy", "12.5"],
      ["current", "16"],
    ];
    for (const [kind, value] of cases) {
      document.body.innerHTML = "";
      const { hass, element } = await openEditor(kind, { record });
      enter(inputs(element)[0]!, value);
      button(element, "spotnav-settings-save")!.click();
      await settle();

      const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
      const expected: Record<string, unknown> = { ...record };
      delete expected["revision"];
      if (kind === "energy") {
        expected["requested_kwh"] = 12.5;
      } else {
        expected["amps"] = 16;
      }
      expect(body, kind).toEqual(expected);
    }
  });

  it("changes the deadline's fields in one replacement, keeping the stored values when off", async () => {
    const { hass, element, record } = await openEditor("deadline");
    const [enabled, time] = inputs(element);
    enabled!.checked = false;
    time!.value = "05:15";

    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
    expect(body["departure_enabled"]).toBe(false);
    expect(body["departure_time"]).toBe("05:15");
    // The charge periods are a charger setting: kept as stored.
    expect(body["max_periods"]).toBe(record["max_periods"]);
    // Everything else, including the fields this dialog does not own, is untouched.
    const expected: Record<string, unknown> = { ...record };
    delete expected["revision"];
    expected["departure_enabled"] = false;
    expected["departure_time"] = "05:15";
    expect(body).toEqual(expected);
  });

  it("echoes the record's phases back untouched when another field moves, and offers no phases choice", async () => {
    for (const phases of [1, 3, null]) {
      document.body.innerHTML = "";
      const { hass, element } = await openEditor("current", { record: aRecord({ phases }) });
      // The count is not chosen here any more: the charger's wiring and the car decide it.
      expect(editorDialog(element)?.querySelectorAll("input[data-phases]")).toHaveLength(0);
      expect(editorDialog(element)?.querySelector("[data-part='phases']")).toBeNull();
      enter(inputs(element)[0]!, "16");
      button(element, "spotnav-settings-save")!.click();
      await settle();

      const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
      expect(body["phases"], String(phases)).toBe(phases);
      expect(body["amps"]).toBe(16);
    }
  });

  it("names the phases a charge uses and the power they give as one read-only line", async () => {
    const { element } = await openEditor("current", { record: aRecord({ phases: 1, amps: 16 }) });
    expect(powerLine(element)).toBe(
      translate("en", "settings.phases.line", { phases: translate("en", "settings.phases.one"), power: "3.7" }),
    );
    expect(powerLine(element)).toBe("Charges on 1 phase · nominal power ≈ 3.7 kW");
    expect(editorDialog(element)?.querySelector("[data-part='phases-reason']")).toBeNull();
  });

  it("says why when the car limits the phases", async () => {
    const payload = fixture("start_idle");
    payload["charging_phases"] = { phases: 1, charger: 3, vehicle: 1, limited_by: "vehicle" };
    const { element } = await openEditor("current", { record: aRecord({ phases: 1, amps: 16 }), payload });
    expect(editorDialog(element)?.querySelector("[data-part='phases-reason']")?.textContent).toBe(
      translate("en", "settings.phases.limitedByVehicle"),
    );
    expect(translate("en", "settings.phases.limitedByVehicle")).toBe("The car charges on one phase.");
  });

  it("closes without a request on Cancel and offers it beside Save", async () => {
    const { hass, element } = await openEditor("current");
    enter(inputs(element)[0]!, "20");
    const cancel = editorDialog(element)?.querySelector<HTMLButtonElement>("[data-action='cancel']");
    expect(cancel?.textContent).toBe(translate("en", "settings.cancel"));
    cancel!.click();
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(editorDialog(element)).toBeNull();
  });

  it("offers its own Save, and no conflict action, before anything conflicts", async () => {
    const { element } = await openEditor("energy");

    expect(button(element, "spotnav-settings-save")).not.toBeNull();
    expect(button(element, "spotnav-settings-reapply")).toBeNull();
    expect(button(element, "spotnav-settings-reload")).toBeNull();
    expect(dialogButtons(element)).toEqual([
      translate("en", "dialog.close"),
      translate("en", "settings.save"),
      translate("en", "settings.cancel"),
    ]);
  });

  it("sends nothing at all when a Save changes none of its own fields", async () => {
    const { hass, element } = await openEditor("energy");

    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(0);
    expect(editorDialog(element)).toBeNull();
    expect(dashboards(hass).length).toBe(1);
  });

  it("asks for no more than the charger's range: the slider stops at its top", async () => {
    const { hass, element } = await openEditor("current");
    const input = inputs(element)[0]!;
    enter(input, "200");
    expect(input.value).toBe("32");
    expect(currentText(element)).toBe("32 A");

    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect((updates(hass)[0]!["settings"] as Record<string, unknown>)["amps"]).toBe(32);
  });

  it("shows a current that was never set as not set, and refuses an empty Save", async () => {
    const { hass, element } = await openEditor("current", { record: aRecord({ amps: null }) });
    expect(currentText(element)).toBe(translate("en", "settings.value.unset"));
    expect(sliderNode(element)!.getAttribute("aria-valuetext")).toBe("Not set");

    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(0);
    expect(text(element)).toContain(translate("en", "settings.error.required"));
  });

  it("opens and saves with a decimal target in the record, leaving it untouched", async () => {
    const record = aRecord({
      target: { vehicle_id: "car-1", target_percent: 80.5 },
    });
    const { hass, element } = await openEditor("energy", { record });

    // The editor opens at all -- which is the point: reading the target as a whole number refused the
    // whole document and made every focused editor unreachable for such a charger.
    expect(inputs(element)[0]!.value).toBe("20.5");
    enter(inputs(element)[0]!, "30");
    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
    expect(body["requested_kwh"]).toBe(30);
    expect(body["target"]).toEqual({
      vehicle_id: "car-1",
      target_percent: 80.5,
    });
    expect((body["target"] as Record<string, unknown>)["target_percent"]).toBe(80.5);
  });

  it("reads the values but sends nothing for a non-administrator", async () => {
    const { hass, element } = await openEditor("energy", { admin: false });

    expect(inputs(element)[0]?.value).toBe("20.5");
    expect(inputs(element)[0]?.disabled).toBe(true);
    expect(energyText(element)).toBe("20.5 kWh");
    expect(button(element, "spotnav-settings-save")).toBeNull();
    expect(text(element)).toContain(translate("en", "settings.readOnly"));
    expect(updates(hass)).toHaveLength(0);
  });

  it("stands on target SoC when the record does, with the manual energy hidden and nothing sent", async () => {
    const { hass, element } = await openEditor("energy", {
      record: aRecord({ driver: "target_soc" }),
    });

    const dialog = editorDialog(element);
    expect(dialog?.querySelector<HTMLElement>("[data-row='charge_by']")?.dataset["mode"]).toBe("target_soc");
    expect(dialog?.querySelector<HTMLElement>("[data-part='energy']")?.hidden).toBe(true);
    expect(updates(hass)).toHaveLength(0);
  });

  it.each([
    [42, "42 kWh"],
    [41.6, "41.6 kWh"],
    [20.25, "20.25 kWh"],
  ])("spells the stored %s kWh on the Plan cell as one amount", async (stored, shown) => {
    const payload = fixture("start_idle");
    (payload["settings"] as Record<string, unknown>)["requested_kwh"] = stored;
    const { element } = await openEditor("energy", {
      record: aRecord({ requested_kwh: stored }),
      payload,
    });

    expect(rowValues(element)[0], String(stored)).toBe(shown);
  });

  it.each<[Language, string]>([
    ["da", "1.000 kWh"],
    ["en", "1,000 kWh"],
    ["fi", "1\u00a0000 kWh"],
    ["nb", "1\u00a0000 kWh"],
    ["sv", "1\u00a0000 kWh"],
  ])(
    "states a stored 1000 kWh in %s's conventions, the dialog and the row saying the same thing",
    async (language, shown) => {
      // The amount is at the field's own maximum, so the grouping mark is exercised as well as the
      // decimal one: `da` groups with a period, `en` with a comma, and `fi`/`nb`/`sv` with a no-break
      // space. None of that is this card's choice to make, so it is asserted, not assumed.
      const payload = fixture("start_idle");
      (payload["settings"] as Record<string, unknown>)["requested_kwh"] = 1000;
      const { element } = await openEditor("energy", {
        record: aRecord({ requested_kwh: 1000 }),
        payload,
        language,
      });

      expect(rowValues(element)[0], language).toBe(shown);
      // The trigger's accessible name carries the same figure in its own translated sentence.
      expect(trigger(element, "energy").getAttribute("aria-label")).toBe(
        `${translate(language, "bar.plan")}: ${shown} \u00b7 ${translate(language, "settings.deadline.none")} \u00b7 10 A. ${translate(language, "bar.change")}`,
      );
    },
  );
});

/** The three *planning* trigger values: the area/fiscal trigger labels itself from the market and has
 * its own coverage in `market-card.test.ts`. */
function rowValues(element: Element): (string | null)[] {
  // The Plan cell's one line, as its three parts (each is its own span).
  return Array.from(
    shadow(element).querySelectorAll(".spotnav-settings-trigger[data-setting='plan'] .spotnav-bar-part"),
  ).map((node) => node.textContent);
}

function rowError(element: Element): HTMLElement | null {
  return shadow(element).querySelector<HTMLElement>(".spotnav-settings-error:not([hidden])");
}

function chartNode(element: Element): Element | null {
  return shadow(element).querySelector(".spotnav-chart-viewport svg");
}

/** Save the current form and answer with this settings envelope. */
async function save(
  kind: Kind,
  answer: Record<string, unknown>,
  options: { value?: string; admin?: boolean; record?: SettingsRecord } = {},
) {
  const { hass, element, record } = await openEditor(kind, options);
  enter(inputs(element)[0]!, options.value ?? "30");
  button(element, "spotnav-settings-save")!.click();
  await settle();
  hass.resolveNext(answer);
  await settle();
  return { hass, element, record };
}

describe("success and its confirmation", () => {
  it("adopts the returned revision, then confirms with one dashboard read and no optimism", async () => {
    const { hass, element } = await openEditor("energy");
    const dashboardsBefore = dashboards(hass).length;
    enter(inputs(element)[0]!, "30");

    button(element, "spotnav-settings-save")!.click();
    await settle();

    // Nothing optimistic: the dialog is still open and the row still shows the confirmed values.
    expect(editorDialog(element)).not.toBeNull();
    expect(rowValues(element)).toEqual(["20 kWh", "No deadline", "10 A"]);
    expect(dashboards(hass).length).toBe(dashboardsBefore);

    hass.resolveNext(success(aRecord({ revision: 8, requested_kwh: 30 })));
    await settle();

    expect(editorDialog(element)).toBeNull();
    expect(dashboards(hass).length,"exactly one confirmation read").toBe(dashboardsBefore + 1);
    // Still the old row: only the refreshed dashboard may change it.
    expect(rowValues(element)).toEqual(["20 kWh", "No deadline", "10 A"]);

    const refreshed = fixture("start_idle");
    (refreshed["settings"] as Record<string, unknown>)["requested_kwh"] = 30;
    hass.resolveNext(refreshed);
    await settle();

    expect(rowValues(element)).toEqual(["30 kWh", "No deadline", "10 A"]);
    expect(rowError(element)).toBeNull();
    expect(updates(hass)).toHaveLength(1);
  });

  it("keeps the confirmed graph and says the saved state could not be confirmed", async () => {
    const { hass, element } = await openEditor("current");
    const graph = chartNode(element);
    enter(inputs(element)[0]!, "16");
    button(element, "spotnav-settings-save")!.click();
    await settle();
    hass.resolveNext(success(aRecord({ revision: 8, amps: 16 })));
    await settle();

    hass.rejectNext({ code: null, message: "prose that must not be shown" });
    await settle();

    expect(rowError(element)?.textContent).toBe(translate("en", "settings.error.confirmationFailed"));
    expect(text(element)).not.toContain("prose that must not be shown");
    expect(chartNode(element)).toBe(graph);
    expect(updates(hass)).toHaveLength(1);
  });
});

describe("a conflict", () => {
  it("keeps the typed values and offers the server's record or a reapply", async () => {
    const server = aRecord({ revision: 9, amps: 6, requested_kwh: 18 });
    const { hass, element } = await openEditor("current");
    enter(inputs(element)[0]!, "16");
    button(element, "spotnav-settings-save")!.click();
    await settle();

    hass.resolveNext(refusal("revision_conflict", server));
    await settle();

    // One request so far, the reader's value still on screen, and the choice stated in words.
    expect(updates(hass)).toHaveLength(1);
    expect(inputs(element)[0]!.value).toBe("16");
    expect(text(element)).toContain(translate("en", "settings.conflict.intro"));
    // Exactly two choices, each exactly once, and no ordinary Save: it would be built on the
    // revision the server has already rejected.
    expect(dialogButtons(element)).toEqual([
      translate("en", "dialog.close"),
      translate("en", "settings.reapply"),
      translate("en", "settings.reload"),
    ]);
    expect(button(element, "spotnav-settings-save")).toBeNull();
    expect(button(element, "spotnav-settings-reapply")).not.toBeNull();
    expect(button(element, "spotnav-settings-reload")).not.toBeNull();

    // Nothing else in the dialog can send anything at all, and certainly not at the old revision. The
    // primitive's own Close is skipped because it would close the dialog, which is not this test's
    // subject; the two choices are left for the next step.
    const closeLabel = translate("en", "dialog.close");
    for (const node of Array.from(editorDialog(element)!.querySelectorAll<HTMLButtonElement>("button"))) {
      const choice =
        node.classList.contains("spotnav-settings-reapply") ||
        node.classList.contains("spotnav-settings-reload") ||
        node.dataset["soc"] === "settings-link"; // navigates away, sends nothing
      if (!choice && node.getAttribute("aria-label") !== closeLabel) {
        node.click();
      }
    }
    await settle();
    expect(updates(hass)).toHaveLength(1);
    expect(updates(hass).every((message) => message["expected_revision"] === 7)).toBe(true);
    expect(button(element, "spotnav-settings-save")).toBeNull();

    button(element, "spotnav-settings-reapply")!.click();
    await settle();

    expect(updates(hass), "one new CAS, never an automatic retry").toHaveLength(2);
    const retry = updates(hass)[1]!;
    expect(retry["expected_revision"]).toBe(9);
    const body = retry["settings"] as Record<string, unknown>;
    expect(body["amps"]).toBe(16);
    // Built from the server's current record with only what the reader moved applied over it: the
    // current was changed, the energy was not, so the server's newer 18 kWh is kept rather than
    // overwritten by the untouched 20.5 the popover still showed.
    expect(body["requested_kwh"]).toBe(18);
    expect(body["phases"]).toBe(3);
    expect(updates(hass)[0]!["expected_revision"]).toBe(7);
  });

  it("reloads the server's values on request, writing nothing", async () => {
    const server = aRecord({ revision: 9, amps: 6 });
    const { hass, element } = await openEditor("current");
    enter(inputs(element)[0]!, "16");
    button(element, "spotnav-settings-save")!.click();
    await settle();
    hass.resolveNext(refusal("revision_conflict", server));
    await settle();
    const readsBefore = reads(hass).length;

    button(element, "spotnav-settings-reload")!.click();
    await settle();

    expect(reads(hass).length).toBe(readsBefore + 1);
    hass.resolveNext(success(server));
    await settle();

    expect(inputs(element)[0]!.value).toBe("6");
    expect(text(element)).not.toContain(translate("en", "settings.conflict.intro"));
    expect(updates(hass)).toHaveLength(1);
  });
});

describe("refusals and failures", () => {
  it.each<[string, SettingsRecord | null, TranslationKey]>([
    ["spotnav_settings_not_committed", null, "settings.error.notCommitted"],
    ["invalid_energy", aRecord(), "settings.error.invalid"],
    ["spotnav_settings_unavailable", null, "settings.error.unavailable"],
  ])("keeps the dialog open and says what happened for %s", async (code, record, key) => {
    document.body.innerHTML = "";
    const { hass, element } = await openEditor("energy");
    const graph = chartNode(element);
    enter(inputs(element)[0]!, "30");
    button(element, "spotnav-settings-save")!.click();
    await settle();

    hass.resolveNext(refusal(code, record));
    await settle();

    expect(editorDialog(element), code).not.toBeNull();
    expect(text(element), code).toContain(translate("en", key));
    expect(inputs(element)[0]!.value, code).toBe("30");
    expect(updates(hass), code).toHaveLength(1);
    expect(chartNode(element), code).toBe(graph);
  });

  it("turns a rejected message into one localized sentence, never prose", async () => {
    const { hass, element } = await openEditor("energy");
    enter(inputs(element)[0]!, "30");
    button(element, "spotnav-settings-save")!.click();
    await settle();

    hass.rejectNext({ code: "spotnav_unknown_charger", message: "prose that must not be shown" });
    await settle();

    expect(text(element)).toContain(translate("en", "settings.error.charger"));
    expect(text(element)).not.toContain("prose that must not be shown");
    expect(editorDialog(element)).not.toBeNull();
  });

  it.each<[string, Record<string, unknown>, TranslationKey]>([
    ["a malformed answer", { api_version: SETTINGS_API_VERSION, ok: true }, "settings.error.generic"],
    ["an unsupported answer", { api_version: SETTINGS_API_VERSION + 1 }, "settings.error.version"],
  ])("claims nothing for %s", async (_name, answer, key) => {
    document.body.innerHTML = "";
    const { hass, element } = await openEditor("energy");
    enter(inputs(element)[0]!, "30");
    button(element, "spotnav-settings-save")!.click();
    await settle();

    hass.resolveNext(answer);
    await settle();

    expect(text(element)).toContain(translate("en", key));
    expect(editorDialog(element)).not.toBeNull();
    expect(dashboards(hass).length).toBe(1);
  });

  it("adopts a committed record whose reconcile failed, distinctly from a non-commit", async () => {
    const committed = aRecord({ revision: 8, requested_kwh: 30 });
    const { hass, element } = await openEditor("energy");
    const dashboardsBefore = dashboards(hass).length;
    enter(inputs(element)[0]!, "30");
    button(element, "spotnav-settings-save")!.click();
    await settle();

    hass.resolveNext(refusal("spotnav_settings_reconcile_failed", committed));
    await settle();

    // The replacement is durable, so the dialog is done and the card confirms with one read.
    expect(editorDialog(element)).toBeNull();
    expect(dashboards(hass).length).toBe(dashboardsBefore + 1);
    const sentence = rowError(element);
    expect(sentence?.textContent).toBe(translate("en", "settings.error.reconcileFailed"));
    expect(sentence?.dataset["code"]).toBe("spotnav_settings_reconcile_failed");
    expect(sentence?.textContent).not.toBe(translate("en", "settings.error.notCommitted"));

    // The confirmation read answers, and the committed fact is still what the reader is told.
    const refreshed = fixture("start_idle");
    (refreshed["settings"] as Record<string, unknown>)["requested_kwh"] = 30;
    hass.resolveNext(refreshed);
    await settle();
    expect(rowError(element)?.textContent).toBe(translate("en", "settings.error.reconcileFailed"));
  });
});

describe("lateness, duplication and teardown", () => {
  it("makes a held save's answer inert after the card is disconnected", async () => {
    const { hass, element } = await openEditor("energy");
    const dashboardsBefore = dashboards(hass).length;
    enter(inputs(element)[0]!, "30");
    button(element, "spotnav-settings-save")!.click();
    await settle();

    element.remove();
    hass.resolveNext(success(aRecord({ revision: 8, requested_kwh: 30 })));
    await settle();

    expect(dashboards(hass).length, "no confirmation read for a card that is gone").toBe(dashboardsBefore);
    expect(shadow(element).querySelectorAll("[role='dialog']").length).toBe(0);
  });

  it("makes a held read inert when the dialog was closed and reopened", async () => {
    const { hass, element } = await mounted();
    trigger(element, "energy").click();
    await settle();
    // Close it before the first read answers, then open it again and answer the second read.
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    trigger(element, "energy").click();
    await settle();
    hass.resolveAt(1, success(aRecord({ revision: 8, requested_kwh: 30 })));
    await settle();
    expect(inputs(element)[0]!.value).toBe("30");

    // The first read's answer now arrives: it belongs to an operation nobody is watching.
    hass.resolveAt(0, success(aRecord({ revision: 4, requested_kwh: 12 })));
    await settle();

    expect(inputs(element)[0]!.value).toBe("30");
    expect(reads(hass)).toHaveLength(2);
  });

  it("makes a held read inert when the charger is reconfigured", async () => {
    const { hass, element } = await mounted();
    trigger(element, "energy").click();
    await settle();

    element.setConfig({ type: "custom:spotnav-card", charger: "entry_b" });
    await settle();

    // The old charger's answer arrives: nothing of it may appear on a card that now serves another
    // charger, and no editor may open itself from an operation nobody is watching.
    hass.resolveAt(0, success(aRecord({ requested_kwh: 30 })));
    await settle();

    expect(editorDialog(element)).toBeNull();
    expect(reads(hass).filter((message) => message.charger_id === "entry_b")).toHaveLength(0);
    // The card does read the new charger's *dashboard*, which is its ordinary lifecycle.
    expect(dashboards(hass).some((message) => message.charger_id === "entry_b")).toBe(true);
  });

  it("sends one update for a double Save click", async () => {
    const { hass, element } = await openEditor("current");
    enter(inputs(element)[0]!, "16");
    const save = button(element, "spotnav-settings-save")!;

    save.click();
    save.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    expect(save.disabled).toBe(true);
  });

  it("keeps two cards' records, revisions and dialogs apart", async () => {
    const first = await openEditor("current", { record: aRecord({ revision: 7, amps: 10 }) });
    const second = await openEditor("current", { record: aRecord({ revision: 3, amps: 6 }) });

    enter(inputs(first.element)[0]!, "16");
    button(first.element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(first.hass)).toHaveLength(1);
    expect(updates(second.hass)).toHaveLength(0);
    expect(updates(first.hass)[0]!["expected_revision"]).toBe(7);
    expect(inputs(second.element)[0]!.value).toBe("6");
  });

  it("closes every dialog and returns focus to its trigger on Escape", async () => {
    const { element } = await openEditor("deadline");
    expect(editorDialog(element)).not.toBeNull();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();

    expect(editorDialog(element)).toBeNull();
    expect(shadow(element).activeElement).toBe(trigger(element, "deadline"));
  });

  it("keeps focus inside a conflict dialog, and closing returns it to the trigger", async () => {
    const { hass, element } = await openEditor("current");
    enter(inputs(element)[0]!, "16");
    button(element, "spotnav-settings-save")!.click();
    await settle();
    hass.resolveNext(refusal("revision_conflict", aRecord({ revision: 9, amps: 6 })));
    await settle();

    const dialog = editorDialog(element) as HTMLElement;
    const active = shadow(element).activeElement;
    expect(active !== null && dialog.contains(active), "focus stays in the dialog").toBe(true);
    // The dialog's own Close, then the two choices, in that DOM order.
    expect(dialogButtons(element)).toEqual([
      translate("en", "dialog.close"),
      translate("en", "settings.reapply"),
      translate("en", "settings.reload"),
    ]);

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();

    expect(editorDialog(element)).toBeNull();
    expect(shadow(element).activeElement).toBe(trigger(element, "current"));
  });

  it("opens only one overlay at a time, whichever trigger is pressed", async () => {
    const { hass, element } = await openEditor("energy");
    trigger(element, "current").click();
    await settle();
    hass.resolveNext(success(aRecord({ amps: 6 })));
    await settle();

    const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).filter(
      (dialog) => dialog.closest("[hidden]") === null,
    );
    expect(dialogs).toHaveLength(1);
    expect(text(element)).toContain(translate("en", "settings.current.label"));
  });
});

function sliderNode(element: Element): HTMLInputElement | null {
  const sliders = Array.from(
    editorDialog(element)?.querySelectorAll<HTMLInputElement>(".spotnav-settings-slider") ?? [],
  );
  const label = (node: HTMLInputElement): string => node.getAttribute("aria-label") ?? "";
  if (focusKind === "energy") {
    return sliders.find((node) => label(node).startsWith("Energy slider")) ?? null;
  }
  if (focusKind === "current") {
    return sliders.find((node) => label(node).startsWith("Current slider")) ?? null;
  }
  return null;
}

function powerLine(element: Element): string {
  return editorDialog(element)?.querySelector<HTMLElement>(".spotnav-settings-power")?.textContent ?? "";
}

describe("the energy slider and its value on the label row", () => {
  it("moves the value text with the slider, and sends that one value on Save", async () => {
    const { hass, element } = await openEditor("energy", { record: aRecord({ requested_kwh: 20 }) });
    const range = sliderNode(element)!;

    expect(inputs(element)).toEqual([range]);
    expect(range.value).toBe("20");
    expect([range.min, range.max, range.step]).toEqual(["0.5", "100", "0.5"]);
    expect(range.disabled).toBe(false);
    expect(energyText(element)).toBe("20.0 kWh");
    expect(range.getAttribute("aria-valuetext")).toBe("20.0 kWh");
    // The accessible name states the slider's own range.
    expect(range.getAttribute("aria-label")).toBe("Energy slider, 0.5 to 100 kWh in half-kWh steps");
    // No number field and no unit in the energy editor.
    const energy = editorDialog(element)!.querySelector<HTMLElement>("[data-part='energy']")!;
    expect(energy.querySelector("input[type='number']")).toBeNull();
    expect(energy.querySelector(".spotnav-settings-unit")).toBeNull();

    enter(range, "42.5");
    await settle();

    expect(energyText(element)).toBe("42.5 kWh");
    expect(range.getAttribute("aria-valuetext")).toBe("42.5 kWh");
    // Form-local: no request yet, and the resting row is untouched.
    expect(updates(hass)).toHaveLength(0);
    expect(rowValues(element)).toEqual(["20 kWh", "No deadline", "10 A"]);

    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
    expect(body["requested_kwh"]).toBe(42.5);
  });

  it.each<[number, string, string, string]>([
    [0.1, "0.5", "100", "0.1 kWh"],
    [7.3, "7.5", "100", "7.3 kWh"],
    [20.25, "20.5", "100", "20.25 kWh"],
    [100, "100", "100", "100.0 kWh"],
    [150, "150", "150", "150.0 kWh"],
    [1000, "1000", "1000", "1,000.0 kWh"],
  ])(
    "keeps a stored %s kWh exactly through opening and a neighbouring save",
    async (value, at, max, shown) => {
      document.body.innerHTML = "";
      const energy = await openEditor("energy", { record: aRecord({ requested_kwh: value }) });
      const range = sliderNode(energy.element)!;

      // The value text says exactly what is stored; the slider stands at its nearest step and stays usable.
      // A stored value above the ordinary top extends it, as before, and its accessible name says so.
      expect(energyText(energy.element), String(value)).toBe(shown);
      expect(range.value, String(value)).toBe(at);
      expect(range.max, String(value)).toBe(max);
      expect(range.disabled, String(value)).toBe(false);
      expect(range.getAttribute("aria-label"), String(value)).toBe(
        `Energy slider, 0.5 to ${Number(max).toLocaleString("en")} kWh in half-kWh steps`,
      );

      // A Save that moved the current, not the slider, carries the stored value through untouched.
      focusKind = "current";
      enter(inputs(energy.element)[0]!, "16");
      button(energy.element, "spotnav-settings-save")!.click();
      await settle();

      expect(updates(energy.hass)).toHaveLength(1);
      const body = updates(energy.hass)[0]!["settings"] as Record<string, unknown>;
      expect(body["requested_kwh"], String(value)).toBe(value);
      expect(body["amps"], String(value)).toBe(16);
    },
  );

  it("states the value in the reader's language", async () => {
    const { element } = await openEditor("energy", { record: aRecord({ requested_kwh: 7.3 }), language: "sv" });
    expect(energyText(element)).toBe("7,3 kWh");
    const range = editorDialog(element)?.querySelector<HTMLInputElement>("[data-part='energy'] input[type='range']");
    expect(range?.getAttribute("aria-valuetext")).toBe("7,3 kWh");
    expect(range?.value).toBe("7.5");
  });
});

describe("the current slider and the nominal power beside it", () => {
  it.each([6, 16, 32])("states %s A on the label row, with the slider alone under it", async (amps) => {
    document.body.innerHTML = "";
    const { element } = await openEditor("current", { record: aRecord({ amps, phases: 3 }) });
    const range = sliderNode(element)!;

    expect(inputs(element)).toEqual([range]);
    expect(range.value).toBe(String(amps));
    // The charger's own range (the everyday 6 to 32 A when the answer states none), not the 80 A bound.
    expect([range.min, range.max, range.step]).toEqual(["6", "32", "1"]);
    expect(range.getAttribute("aria-label")).toBe("Current slider, 6 to 32 A in whole amperes");
    expect(currentText(element)).toBe(`${amps} A`);
    expect(range.getAttribute("aria-valuetext")).toBe(`${amps} A`);
    // No number field and no unit: the value stands at the end of the label's row.
    const group = range.closest<HTMLElement>("[role='group']")!;
    expect(group.querySelectorAll("input")).toHaveLength(1);
    expect(group.querySelector(".spotnav-settings-unit")).toBeNull();
    expect(editorDialog(element)?.querySelector("input[type='number']")).toBeNull();
    const label = group.querySelector("label")!;
    expect(label.textContent).toBe(translate("en", "settings.current.label"));
    expect(label.getAttribute("for")).toBe(range.id);
    expect(group.querySelector("[data-part='current-value']")!.parentElement).toBe(label.parentElement);

    enter(range, "20");
    expect(currentText(element)).toBe("20 A");
    expect(range.getAttribute("aria-valuetext")).toBe("20 A");
  });

  it("states the current in the reader's language", async () => {
    const { element } = await openEditor("current", { record: aRecord({ amps: 10 }), language: "sv" });
    expect(currentText(element)).toBe("10 A");
    const label = editorDialog(element)?.querySelector("[data-part='current-value']")?.parentElement;
    expect(label?.textContent).toContain(translate("sv", "settings.current.label"));
    expect(inputs(element)[0]?.getAttribute("aria-label")).toBe("Strömreglage, 6 till 32 A i hela ampere");
  });

  it.each([
    [1, "3.7"],
    [3, "11.1"],
  ])("names the nominal power at 16 A with %s phase(s)", async (phases, power) => {
    const { element } = await openEditor("current", { record: aRecord({ amps: 16, phases }) });
    expect(powerLine(element)).toBe(
      translate("en", "settings.phases.line", {
        phases: translate("en", phases === 1 ? "settings.phases.one" : "settings.phases.three"),
        power,
      }),
    );
  });

  it("says the power is unknown rather than assuming a phase count", async () => {
    for (const phases of [null, 2, 4]) {
      document.body.innerHTML = "";
      const { element } = await openEditor("current", {
        record: aRecord({ amps: 16, phases: phases as number | null }),
      });
      expect(powerLine(element), String(phases)).toBe(
        translate("en", "settings.current.powerUnknown"),
      );
      // Never a number that could be read as a phase count it does not know.
      expect(powerLine(element), String(phases)).not.toMatch(/\d/);
    }
  });

  it("updates the nominal power as the draft moves, sending nothing", async () => {
    const { hass, element } = await openEditor("current", { record: aRecord({ amps: 6, phases: 1 }) });
    expect(powerLine(element)).toBe(linePower("1 phase", "1.4"));

    const range = sliderNode(element)!;
    range.value = "16";
    range.dispatchEvent(new Event("input", { bubbles: true }));
    await settle();

    expect(powerLine(element)).toBe(linePower("1 phase", "3.7"));
    expect(updates(hass)).toHaveLength(0);
    expect(rowValues(element)).toEqual(["20 kWh", "No deadline", "10 A"]);
  });

  it.each<[number, string]>([
    [1, "6"],
    [80, "32"],
  ])("keeps a stored %s A exactly outside the charger's range, until the slider moves", async (amps, at) => {
    document.body.innerHTML = "";
    const { hass, element } = await openEditor("current", { record: aRecord({ amps, phases: 3 }) });
    const range = sliderNode(element)!;
    // The value text says what is stored; the slider stands at its nearest end and stays usable.
    expect(currentText(element)).toBe(`${amps} A`);
    expect(range.getAttribute("aria-valuetext")).toBe(`${amps} A`);
    expect(range.value).toBe(at);
    expect(range.disabled).toBe(false);

    // A Save that moved the energy, not the current, carries the stored current through untouched.
    focusKind = "energy";
    enter(inputs(element)[0]!, "12.5");
    button(element, "spotnav-settings-save")!.click();
    await settle();
    const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
    expect(body["amps"]).toBe(amps);
    expect(body["requested_kwh"]).toBe(12.5);
  });

  it("has no charging periods slider: they are a charger setting", async () => {
    const { element } = await openEditor("deadline", { record: aRecord({ max_periods: 4 }) });
    expect(editorDialog(element)!.querySelector("input[id$='-deadline-periods']")).toBeNull();
    expect(editorDialog(element)!.querySelector("[data-part='periods-value']")).toBeNull();
  });
});

  it("draws every slider the same way: its label row, then the slider alone in its track", async () => {
    // Energy and current: the label and the value on one row, the slider under them at the full
    // width. No slider has a number field, a unit or an out-of-range note beside it any more.
    const { element } = await openEditor("current");
    const dialog = editorDialog(element)!;
    expect(dialog.querySelector(".spotnav-settings-pair")).toBeNull();
    expect(dialog.querySelector("input[type='number']")).toBeNull();
    const sliders = Array.from(dialog.querySelectorAll<HTMLInputElement>("input[type='range']"));
    expect(sliders.map((node) => node.id.replace(/^.*-(energy|current)$/, "$1"))).toEqual(["energy", "current"]);
    for (const slider of sliders) {
      const group = slider.closest<HTMLElement>("[role='group']")!;
      expect(Array.from(group.children).map((node) => node.className), slider.id).toEqual([
        VISUAL_CLASSES.settingsHead,
        VISUAL_CLASSES.settingsTrack,
      ]);
      const head = group.children[0]!;
      expect(Array.from(head.children).map((node) => node.className), slider.id).toEqual([
        VISUAL_CLASSES.settingsLabel,
        VISUAL_CLASSES.settingsAmount,
      ]);
      expect(group.children[1]!.firstElementChild, slider.id).toBe(slider);
      expect(slider.getAttribute("aria-valuetext"), slider.id).toBe(head.children[1]!.textContent);
    }
  });

  it("labels the deadline checkbox as the other fields are labelled, after the checkbox", async () => {
    const { element } = await openEditor("deadline");
    const dialog = editorDialog(element)!;
    const row = dialog.querySelector<HTMLElement>(`.${VISUAL_CLASSES.settingsCheckRow}`)!;
    const [box, label] = Array.from(row.children) as [HTMLInputElement, HTMLLabelElement];
    expect(box.type).toBe("checkbox");
    expect(label.tagName).toBe("LABEL");
    expect(label.getAttribute("for")).toBe(box.id);
    expect(label.textContent).toBe(translate("en", "settings.deadline.enabled"));
    // The same label as "Requested energy" or "Departure time".
    expect(label.className).toBe(VISUAL_CLASSES.settingsLabel);
  });

describe("the slider draft's lifecycle", () => {
  it("disables both representations for a non-administrator, and sends nothing", async () => {
    const { hass, element } = await openEditor("current", { record: aRecord({ amps: 10 }), admin: false });

    expect(inputs(element)[0]!.disabled).toBe(true);
    expect(sliderNode(element)!.disabled).toBe(true);
    expect(button(element, "spotnav-settings-save")).toBeNull();
    expect(updates(hass)).toHaveLength(0);
  });

  it("hides the energy controls while the popover stands on target SoC", async () => {
    const { element } = await openEditor("energy", { record: aRecord({ driver: "target_soc" }) });

    const energy = editorDialog(element)?.querySelector<HTMLElement>("[data-part='energy']");
    expect(energy?.hidden).toBe(true);
  });

  it("discards a slider draft on close, and reopening reads the canonical record again", async () => {
    const { hass, element } = await openEditor("current", { record: aRecord({ amps: 10 }) });
    const range = sliderNode(element)!;
    range.value = "30";
    range.dispatchEvent(new Event("input", { bubbles: true }));
    expect(currentText(element)).toBe("30 A");

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    expect(editorDialog(element)).toBeNull();
    expect(updates(hass)).toHaveLength(0);

    const readsBefore = reads(hass).length;
    trigger(element, "current").click();
    await settle();
    expect(reads(hass)).toHaveLength(readsBefore + 1);
    hass.resolveNext(success(aRecord({ amps: 10 })));
    await settle();

    // The server's record again, not the discarded draft.
    expect(currentText(element)).toBe("10 A");
    expect(sliderNode(element)!.value).toBe("10");
  });

  it("keeps the synchronized draft through a conflict and reapplies it at the server's revision", async () => {
    const server = aRecord({ revision: 9, amps: 6, phases: 1 });
    const { hass, element } = await openEditor("current", { record: aRecord({ amps: 10, phases: 1 }) });
    const range = sliderNode(element)!;
    range.value = "24";
    range.dispatchEvent(new Event("input", { bubbles: true }));
    expect(powerLine(element)).toBe(linePower("1 phase", "5.5"));

    button(element, "spotnav-settings-save")!.click();
    await settle();
    hass.resolveNext(refusal("revision_conflict", server));
    await settle();

    // The draft survives the conflict rerender, on both controls, with the two choices and no Save.
    expect(currentText(element)).toBe("24 A");
    expect(sliderNode(element)!.value).toBe("24");
    expect(button(element, "spotnav-settings-save")).toBeNull();
    expect(dialogButtons(element)).toEqual([
      translate("en", "dialog.close"),
      translate("en", "settings.reapply"),
      translate("en", "settings.reload"),
    ]);
    // The nominal power now names what the *server's* record would draw.
    expect(powerLine(element)).toBe(linePower("1 phase", "5.5"));

    button(element, "spotnav-settings-reapply")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(2);
    expect(updates(hass)[1]!["expected_revision"]).toBe(9);
    const body = updates(hass)[1]!["settings"] as Record<string, unknown>;
    expect(body["amps"]).toBe(24);
    expect(body["phases"]).toBe(1);
  });

  it("replaces both controls from the server on reload, writing nothing", async () => {
    const server = aRecord({ revision: 9, amps: 6, phases: 3 });
    const { hass, element } = await openEditor("current", { record: aRecord({ amps: 10, phases: 1 }) });
    const range = sliderNode(element)!;
    range.value = "24";
    range.dispatchEvent(new Event("input", { bubbles: true }));

    button(element, "spotnav-settings-save")!.click();
    await settle();
    hass.resolveNext(refusal("revision_conflict", server));
    await settle();
    const readsBefore = reads(hass).length;

    button(element, "spotnav-settings-reload")!.click();
    await settle();
    expect(reads(hass)).toHaveLength(readsBefore + 1);
    hass.resolveNext(success(server));
    await settle();

    expect(currentText(element)).toBe("6 A");
    expect(sliderNode(element)!.value).toBe("6");
    expect(powerLine(element)).toBe(linePower("3 phases", "4.2"));
    expect(updates(hass)).toHaveLength(1);
    expect(text(element)).not.toContain(translate("en", "settings.conflict.intro"));
  });

  it("makes the slider a real keyboard control, named by its own label", async () => {
    const { element } = await openEditor("energy");
    const range = sliderNode(element)!;
    const group = editorDialog(element)?.querySelector<HTMLElement>("[role='group']");
    const label = group?.querySelector<HTMLElement>("label");

    expect(range.type).toBe("range");
    expect(range.getAttribute("aria-label")).toBe("Energy slider, 0.5 to 100 kWh in half-kWh steps");
    // The energy slider has no out-of-range note: its value text says what is stored.
    expect(group?.querySelector(".spotnav-settings-note")).toBeNull();
    expect(group?.getAttribute("aria-labelledby")).toBe(label?.id);
    expect(label?.getAttribute("for")).toBe(range.id);
    expect(range.disabled).toBe(false);
    expect(range.step).toBe("0.5");
    expect(range.getAttribute("aria-valuetext")).toBe("20.5 kWh");

    range.focus();
    expect(shadow(element).activeElement).toBe(range);
  });

  it("keeps two cards' sliders, drafts and revisions apart", async () => {
    const first = await openEditor("current", { record: aRecord({ revision: 7, amps: 10 }) });
    const second = await openEditor("current", { record: aRecord({ revision: 3, amps: 6 }) });

    const range = sliderNode(first.element)!;
    range.value = "24";
    range.dispatchEvent(new Event("input", { bubbles: true }));

    expect(inputs(second.element)[0]!.value).toBe("6");
    expect(sliderNode(second.element)!.value).toBe("6");
    expect(updates(first.hass)).toHaveLength(0);
    expect(updates(second.hass)).toHaveLength(0);

    button(first.element, "spotnav-settings-save")!.click();
    await settle();
    expect(updates(first.hass)).toHaveLength(1);
    expect(updates(second.hass)).toHaveLength(0);
    expect(updates(first.hass)[0]!["expected_revision"]).toBe(7);
  });
});

// ------------------------------------------------------------------------------------------------
// The dialog's body while it is reading is the node a failure is shown in: a read that fails before any
// record arrives must render its sentence there, not leave "Reading…" forever. One read, one sentence,
// and the dialog stays open.
// ------------------------------------------------------------------------------------------------

describe("a read that fails before the form exists", () => {
  it("says so inside the open dialog rather than reading forever", async () => {
    const record = aRecord();
    const { hass, element } = await mounted();
    trigger(element, "energy").click();
    await settle();

    hass.rejectNext({ code: "spotnav_charger_unloaded", message: "prose that must not be shown" });
    await settle();

    expect(editorDialog(element)).not.toBeNull();
    expect(text(element)).toContain(translate("en", "settings.error.charger"));
    expect(text(element)).not.toContain("prose that must not be shown");
    expect(text(element)).not.toContain(translate("en", "settings.loading"));
    expect(reads(hass)).toHaveLength(1);
    expect(record.revision).toBe(7);
  });
});

// ------------------------------------------------------------------------------------------------
// The departure date: a picker beside the time, limited to today..+7 in the market's zone, starting at
// the next occurrence, shown as "Sun 4 Oct", and cleared back to a daily departure.
// ------------------------------------------------------------------------------------------------

describe("the departure date picker", () => {
  // Friday 2026-10-02, 20:00 in Stockholm (the dashboard fixture's market).
  const EVENING = Date.parse("2026-10-02T18:00:00Z");

  beforeEach(() => {
    vi.setSystemTime(EVENING);
  });

  const dateField = (element: Element): HTMLInputElement | null =>
    editorDialog(element)?.querySelector<HTMLInputElement>("input[type='date']") ?? null;
  const radio = (element: Element, which: "daily" | "date"): HTMLInputElement | null =>
    editorDialog(element)?.querySelector<HTMLInputElement>(`input[data-departure-day='${which}']`) ?? null;
  const choose = (element: Element, which: "daily" | "date"): void => {
    const input = radio(element, which)!;
    input.checked = true;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const pickerPart = (element: Element): HTMLElement | null =>
    editorDialog(element)?.querySelector<HTMLElement>("[data-part='departure-date-picker']") ?? null;
  const departurePart = (element: Element): HTMLElement | null =>
    editorDialog(element)?.querySelector<HTMLElement>("[data-part='departure']") ?? null;
  const helpOf = (element: Element): HTMLElement | null =>
    editorDialog(element)?.querySelector<HTMLElement>("[data-departure-date-help]") ?? null;
  const noteOf = (element: Element): HTMLElement | null =>
    editorDialog(element)?.querySelector<HTMLElement>("[data-departure-date-note]") ?? null;

  it("offers Every day and On a date below the time; the picker waits for the date option", async () => {
    const { element } = await openEditor("deadline");
    const time = editorDialog(element)!.querySelector<HTMLInputElement>("input[type='time']")!;
    const group = editorDialog(element)!.querySelector<HTMLElement>("[data-part='departure-date']")!;

    expect(time.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(group.querySelector("legend")!.textContent).toBe(translate("en", "settings.deadline.date"));
    expect(group.textContent).toContain(translate("en", "settings.deadline.dateDaily"));
    expect(group.textContent).toContain(translate("en", "settings.deadline.dateOn"));
    expect(radio(element, "daily")!.checked).toBe(true);
    expect(radio(element, "date")!.checked).toBe(false);
    expect(pickerPart(element)!.hidden).toBe(true);
    expect(dateField(element)!.value).toBe("");
    expect([dateField(element)!.min, dateField(element)!.max]).toEqual(["2026-10-02", "2026-10-09"]);
  });

  it("preselects tomorrow when today's time has passed, and shows the help under the date option only", async () => {
    const { element } = await openEditor("deadline"); // 06:30, and it is 20:00
    expect(helpOf(element)!.closest("[hidden]")).not.toBeNull();
    choose(element, "date");

    expect(pickerPart(element)!.hidden).toBe(false);
    expect(dateField(element)!.value).toBe("2026-10-03");
    expect(helpOf(element)!.textContent).toBe(translate("en", "settings.deadline.dateHelp"));
    expect(helpOf(element)!.closest("[hidden]")).toBeNull();
    expect(pickerPart(element)!.contains(helpOf(element))).toBe(true);
  });

  it("preselects today when the time is still ahead", async () => {
    const { element } = await openEditor("deadline", { record: aRecord({ departure_time: "21:30" }) });
    choose(element, "date");
    expect(dateField(element)!.value).toBe("2026-10-02");
  });

  it("goes back to Every day: the picker is hidden again and its date is dropped", async () => {
    const { element } = await openEditor("deadline");
    choose(element, "date");
    choose(element, "daily");
    expect(pickerPart(element)!.hidden).toBe(true);
    expect(dateField(element)!.value).toBe("");
  });

  it("is worded in the card's language", async () => {
    const { element } = await openEditor("deadline", { language: "sv" });
    expect(editorDialog(element)!.textContent).toContain("Ett visst datum");
    expect(editorDialog(element)!.textContent).toContain("Varje dag");
  });

  it("writes the chosen date with the rest of the record, in one replacement", async () => {
    const { hass, element, record } = await openEditor("deadline");
    choose(element, "date");
    const date = dateField(element)!;
    date.value = "2026-10-04";
    date.dispatchEvent(new Event("change", { bubbles: true }));

    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
    expect(body["departure_date"]).toBe("2026-10-04");
    const expected: Record<string, unknown> = { ...record };
    delete expected["revision"];
    expected["departure_date"] = "2026-10-04";
    expect(body).toEqual(expected);
  });

  it("opens on the stored date, and Every day clears it on Save", async () => {
    const { hass, element } = await openEditor("deadline", { record: aRecord({ departure_date: "2026-10-04" }) });
    expect(radio(element, "date")!.checked).toBe(true);
    expect(pickerPart(element)!.hidden).toBe(false);
    expect(dateField(element)!.value).toBe("2026-10-04");

    choose(element, "daily");
    expect(pickerPart(element)!.hidden).toBe(true);

    button(element, "spotnav-settings-save")!.click();
    await settle();
    expect((updates(hass)[0]!["settings"] as Record<string, unknown>)["departure_date"]).toBeNull();
  });

  it("Cancel keeps the stored date, even after Every day was chosen", async () => {
    const { hass, element } = await openEditor("deadline", { record: aRecord({ departure_date: "2026-10-04" }) });
    choose(element, "daily");
    editorDialog(element)?.querySelector<HTMLButtonElement>("button[data-action='cancel']")?.click();
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(editorDialog(element)).toBeNull();
  });

  it("refuses a typed date outside today..+7 with its own sentence and sends nothing", async () => {
    for (const value of ["2026-10-01", "2026-10-10"]) {
      document.body.innerHTML = "";
      const { hass, element } = await openEditor("deadline");
      choose(element, "date");
      const date = dateField(element)!;
      date.value = value;
      date.dispatchEvent(new Event("change", { bubbles: true }));
      button(element, "spotnav-settings-save")!.click();
      await settle();

      expect(updates(hass), value).toHaveLength(0);
      expect(text(element)).toContain(translate("en", "settings.error.dateRange"));
    }
  });

  it("marks a stored date that has gone by, and lets a save carry on without judging it", async () => {
    const { hass, element } = await openEditor("deadline", { record: aRecord({ departure_date: "2026-09-30" }) });
    expect(radio(element, "date")!.checked).toBe(true);
    expect(dateField(element)!.value).toBe("2026-09-30");
    expect(noteOf(element)!.hidden).toBe(false);
    expect(noteOf(element)!.textContent).toBe(translate("en", "settings.deadline.datePast"));
    expect(translate("en", "settings.deadline.datePast")).toMatch(/every day until you choose a new date.*clears it/);

    const [, time] = inputs(element);
    time!.value = "05:45";
    button(element, "spotnav-settings-save")!.click();
    await settle();
    expect(updates(hass)).toHaveLength(1);
    expect((updates(hass)[0]!["settings"] as Record<string, unknown>)["departure_date"]).toBe("2026-09-30");
  });

  it("is set aside with the deadline: off hides the time and the day and saves no date", async () => {
    const { hass, element } = await openEditor("deadline", { record: aRecord({ departure_date: "2026-10-04" }) });
    expect(departurePart(element)!.hidden).toBe(false);
    const enabled = editorDialog(element)!.querySelector<HTMLInputElement>("input[type='checkbox']")!;
    enabled.checked = false;
    enabled.dispatchEvent(new Event("change", { bubbles: true }));
    expect(departurePart(element)!.hidden).toBe(true);
    expect(departurePart(element)!.querySelector("input[type='time']")).not.toBeNull();

    button(element, "spotnav-settings-save")!.click();
    await settle();
    const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
    expect(body["departure_enabled"]).toBe(false);
    expect(body["departure_date"]).toBeNull();
  });

  it("is hidden from the start when the deadline is off", async () => {
    const { element } = await openEditor("deadline", { record: aRecord({ departure_enabled: false }) });
    expect(departurePart(element)!.hidden).toBe(true);
  });

  it("is read-only for a non-administrator", async () => {
    const { element } = await openEditor("deadline", {
      admin: false,
      record: aRecord({ departure_date: "2026-10-04" }),
    });
    expect(dateField(element)!.disabled).toBe(true);
    expect(radio(element, "daily")!.disabled).toBe(true);
    expect(radio(element, "date")!.disabled).toBe(true);
    expect(button(element, "spotnav-settings-save")).toBeNull();
  });

  it("is part of the Plan popover as well, beside its departure time", async () => {
    const { element } = await openEditor("energy");
    expect(dateField(element)).not.toBeNull();
    const { element: current } = await openEditor("current");
    expect(dateField(current)).not.toBeNull();
  });

  it("is not offered without the market's zone, but a date already there is shown and can be cleared", async () => {
    const payload = fixture("start_idle");
    (payload["market"] as Record<string, unknown>)["timezone"] = null;
    const { element: bare } = await openEditor("deadline", { payload });
    expect(radio(bare, "date")).toBeNull();
    expect(editorDialog(bare)!.querySelector("[data-part='departure-date']")).toBeNull();

    document.body.innerHTML = "";
    const { hass, element } = await openEditor("deadline", {
      payload,
      record: aRecord({ departure_date: "2026-10-04" }),
    });
    expect(radio(element, "date")!.checked).toBe(true);
    expect(dateField(element)!.disabled).toBe(true);
    expect(radio(element, "daily")!.disabled).toBe(false);
    choose(element, "daily");
    button(element, "spotnav-settings-save")!.click();
    await settle();
    expect((updates(hass)[0]!["settings"] as Record<string, unknown>)["departure_date"]).toBeNull();
  });

  it("names the departure day in the Plan cell: today, tomorrow, or the weekday and date", async () => {
    const dated = (date: string | null): Record<string, unknown> => {
      const payload = fixture("start_idle");
      Object.assign(payload["settings"] as Record<string, unknown>, {
        departure_enabled: true,
        departure_time: "08:00",
        departure_date: date,
      });
      return payload;
    };
    for (const [date, expected] of [
      [null, "08:00"],
      ["2026-10-02", "today 08:00"],
      ["2026-10-03", "tomorrow 08:00"],
      ["2026-10-04", "Sun 4 Oct 08:00"],
      ["2026-09-30", "08:00"],
    ] as const) {
      document.body.innerHTML = "";
      const { element } = await mounted(dated(date));
      expect(rowValues(element)[1], String(date)).toBe(expected);
    }
  });
});

describe("the weekdays of a daily departure", () => {
  const weekday = (element: HTMLElement, day: number): HTMLInputElement | null =>
    editorDialog(element)?.querySelector<HTMLInputElement>(`input[data-weekday='${day}']`) ?? null;
  const group = (element: HTMLElement): HTMLElement | null =>
    editorDialog(element)?.querySelector<HTMLElement>("[data-part='departure-weekdays']") ?? null;

  it("shows seven toggles, all on, named in the card's language", async () => {
    const { element } = await openEditor("deadline", { language: "sv" });
    for (let day = 1; day <= 7; day += 1) {
      expect(weekday(element, day)!.checked).toBe(true);
    }
    expect(group(element)!.textContent).toContain(translate("sv", "settings.deadline.weekdays"));
    expect(group(element)!.textContent?.toLowerCase()).toContain("mån");
    expect(group(element)!.hidden).toBe(false);
  });

  it("sends the chosen days with the rest of the record, in one replacement", async () => {
    const { hass, element, record } = await openEditor("deadline");
    for (const day of [6, 7]) {
      const toggle = weekday(element, day)!;
      toggle.checked = false;
      toggle.dispatchEvent(new Event("change", { bubbles: true }));
    }
    button(element, "spotnav-settings-save")!.click();
    await settle();

    expect(updates(hass)).toHaveLength(1);
    const body = updates(hass)[0]!["settings"] as Record<string, unknown>;
    expect(body["departure_weekdays"]).toEqual([1, 2, 3, 4, 5]);
    const expected: Record<string, unknown> = { ...record };
    delete expected["revision"];
    expected["departure_weekdays"] = [1, 2, 3, 4, 5];
    expect(body).toEqual(expected);
  });

  it("refuses an empty choice with its own sentence and sends nothing", async () => {
    const { hass, element } = await openEditor("deadline");
    for (let day = 1; day <= 7; day += 1) {
      weekday(element, day)!.checked = false;
    }
    button(element, "spotnav-settings-save")!.click();
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(editorDialog(element)!.textContent).toContain(translate("en", "settings.error.weekdays"));
  });

  it("hides the toggles while a date is chosen, which overrides them", async () => {
    const { element } = await openEditor("deadline");
    const choose = (which: "daily" | "date"): void => {
      const input = editorDialog(element)!.querySelector<HTMLInputElement>(`input[data-departure-day='${which}']`)!;
      input.checked = true;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    };
    choose("date");
    expect(group(element)!.hidden).toBe(true);
    choose("daily");
    expect(group(element)!.hidden).toBe(false);
  });
});
