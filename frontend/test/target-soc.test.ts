// Target SoC: the v7 `soc` block, the Plan popover's mode switch, the Plan cell in target
// mode, the conflict rule for the new fields, and the Settings page's phase choice.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { issuesOf } from "../src/status";
import { translate } from "../src/i18n";
import {
  checkTargetPercent,
  formFromRecord,
  planSummaryParts,
  replacementFor,
} from "../src/settings";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { decodeDashboard, type Dashboard } from "../src/validate";
import { statusLine } from "./dashboard-fixtures";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(name = "target_soc_estimated"): Record<string, any> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, any>;
}

function aRecord(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 1,
    amps: 10,
    requested_kwh: 20,
    max_periods: 1,
    departure_enabled: false,
    departure_time: "08:00",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "target_soc",
    target: { vehicle_id: "car-1", target_percent: 80 },
    ...overrides,
  };
}

const PAUSE = { choice: null, admitted_at: null, expires_at: null };
const success = (record: SettingsRecord): Record<string, unknown> => ({
  api_version: SETTINGS_API_VERSION,
  ok: true,
  error: null,
  settings: { ...record },
  pause: { ...PAUSE },
});
const refusal = (code: string, record: SettingsRecord): Record<string, unknown> => ({
  api_version: SETTINGS_API_VERSION,
  ok: false,
  error: code,
  settings: { ...record },
  pause: { ...PAUSE },
});

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

async function openPlan(payload: Record<string, unknown>, record: SettingsRecord, language = "en") {
  const { hass, element } = await mounted(payload, language);
  shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
  await settle();
  hass.resolveNext(success(record));
  await settle();
  return { hass, element };
}

const updates = (hass: FakeHass) => hass.messages.filter((message) => message.type === "spotnav/update_settings");
const dlg = (element: Element): HTMLElement => openDialog(element)!;
const q = <T extends Element = HTMLElement>(element: Element, selector: string): T | null =>
  dlg(element).querySelector<T>(selector);
const saveButton = (element: Element) => q<HTMLButtonElement>(element, ".spotnav-settings-save");
/** The target slider's value, on its label row. */
const targetText = (element: Element) => q(element, "[data-part='soc'] [data-part='target-value']")?.textContent ?? "";
const rowText = (element: Element, key: string) => q(element, `[data-soc-row='${key}']`)?.textContent ?? null;

function decodeV7(payload: unknown) {
  return decodeDashboard(payload);
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the v7 soc block", () => {
  it("is required, and null means no source resolves", () => {
    const payload = fixture("cheapest_no_site");
    const nothing = decodeV7({ ...payload, soc: null });
    expect(nothing.ok && nothing.value.soc).toBeNull();
    delete payload["soc"];
    expect(decodeV7(payload).ok).toBe(false);
  });

  it.each<[string, (soc: Record<string, any>) => void]>([
    ["an unknown key", (soc) => (soc["extra"] = 1)],
    ["a missing key", (soc) => delete soc["age_s"]],
    ["a percentage above 100", (soc) => (soc["value"] = 101)],
    ["a negative age", (soc) => (soc["age_s"] = -1)],
    ["a zero capacity", (soc) => (soc["capacity_kwh"] = 0)],
    ["an unknown missing entry", (soc) => (soc["missing"] = ["everything"])],
    ["a duplicated missing entry", (soc) => (soc["missing"] = ["soc", "soc"])],
    ["an unknown source", (soc) => (soc["source"] = "cloud")],
    ["a non-boolean estimate flag", (soc) => (soc["estimated"] = "yes")],
  ])("refuses the whole answer for %s", (_name, damage) => {
    const payload = fixture();
    damage(payload["soc"]);
    const result = decodeV7(payload);
    expect(result.ok).toBe(false);
  });

  it("reads the stored driver and target from the dashboard's settings", () => {
    const result = decodeV7(fixture());
    expect(result.ok && result.value.settings?.driver).toBe("target_soc");
    expect(result.ok && result.value.settings?.target.target_percent).toBe(80);
    const incomplete = fixture("cheapest_no_site");
    delete incomplete["settings"]["driver"];
    expect(decodeV7(incomplete).ok).toBe(false);
  });
});

describe("target_soc_unknown", () => {
  it("is said in words, in every language, and is a blocking issue", () => {
    const payload = fixture();
    payload["planning"] = { ...payload["planning"], state: "planning_unavailable", reason: "target_soc_unknown" };
    payload["status"] = { tone: "blocking", lines: [statusLine("target_soc_unknown", { missing: ["soc"] })] };
    const result = decodeV7(payload);
    expect(result.ok).toBe(true);
    const found = issuesOf((result as { value: Dashboard }).value.status, "en");
    const issue = found.find((entry) => entry.code === "target_soc_unknown");
    expect(issue?.textKey).toBe("issue.targetSocUnknown");
    expect(issue?.severity).toBe("blocking");
    for (const language of ["en", "sv", "nb", "da", "fi"] as const) {
      expect(translate(language, "issue.targetSocUnknown")).not.toBe("");
    }
  });
});

describe("the Plan cell", () => {
  it("reads the target first when the driver is a target, and the energy otherwise", () => {
    const result = decodeV7(fixture());
    const settings = result.ok ? result.value.settings : null;
    expect(planSummaryParts("en", settings)).toEqual(["80 %", "No deadline", "10 A"]);
    expect(planSummaryParts("sv", settings)[0]).toBe("80 %");
    expect(planSummaryParts("en", settings === null ? null : { ...settings, driver: "manual_kwh" })[0]).toBe("20 kWh");
    expect(planSummaryParts("en", settings === null ? null : { ...settings, target: { ...settings.target, target_percent: null } })[0]).toBe("Not set");
  });

  it("shows 80 % · 08:00 · 16 A on the card, and names the target in its label", async () => {
    const payload = fixture();
    Object.assign(payload["settings"], { departure_enabled: true, departure_time: "08:00", amps: 16 });
    const { element } = await mounted(payload);
    const cell = shadow(element).querySelector(".spotnav-settings-trigger[data-setting='plan']");
    expect(cell?.querySelector(".spotnav-settings-value")?.textContent).toBe("80 % · 08:00 · 16 A");
  });
});

describe("the Plan popover in target mode", () => {
  it("stands on the target with the estimate, its age, the need and the vehicle", async () => {
    const { element } = await openPlan(fixture(), aRecord({ target: { vehicle_id: "<id>", target_percent: 80 } }));
    expect(q(element, "[data-row='charge_by']")?.dataset["mode"]).toBe("target_soc");
    expect(q(element, "[data-part='energy']")?.hidden).toBe(true);
    expect(q<HTMLInputElement>(element, "[data-part='soc'] input[type='range']")?.value).toBe("80");
    // One line: an estimate marked "≈", as on the slider's tick, and the age of the reading it was built from.
    expect(q(element, "[data-soc='facts']")?.textContent).toBe("Now ≈ 75.1 % · 2 h ago");
    expect(q(element, "[data-soc='facts']")?.dataset["estimated"]).toBe("true");
    expect(q(element, "[data-soc='reading']")).toBeNull();
    expect(rowText(element, "need")).toContain("4.2 kWh");
    expect(rowText(element, "vehicle")).toContain("EV6");
    // The Plan popover no longer links to Settings.
    expect(q(element, "[data-soc='settings-link']")).toBeNull();
    // Nothing is missing, so no capacity field and no sensor sentence.
    expect(q(element, "input[id$='-capacity']")).toBeNull();
    expect(q(element, "[data-soc='need-sensor']")).toBeNull();
  });

  it("marks a fresh reading as not estimated, and says so in the local words", async () => {
    const payload = fixture();
    Object.assign(payload["soc"], { estimated: false, age_s: 30 });
    const { element } = await openPlan(payload, aRecord({ target: { vehicle_id: "<id>", target_percent: 80 } }), "sv");
    // A fresh reading has no age to state, and is not called an estimate.
    expect(q(element, "[data-soc='facts']")?.textContent).toBe("Nu 75,1 %");
    expect(q(element, "[data-soc='facts']")?.dataset["estimated"]).toBe("false");
  });

  it("states an older reading's age and the car's limit on the same line", async () => {
    const payload = fixture();
    Object.assign(payload["soc"], { estimated: false, age_s: 480, vehicle_max_percent: 80 });
    const record = aRecord({ target: { vehicle_id: "<id>", target_percent: 80 } });
    const { element } = await openPlan(payload, record);
    expect(q(element, "[data-soc='facts']")?.textContent).toBe("Now 75.1 % · Charge limit 80 % · 8 min ago");
    const sv = await openPlan(payload, record, "sv");
    expect(q(sv.element, "[data-soc='facts']")?.textContent).toBe("Nu 75,1 % · Laddgräns 80 % · för 8 min sedan");
  });

  it("does not ask for the battery capacity in the plan: one sentence, no link", async () => {
    const payload = fixture();
    Object.assign(payload["soc"], { capacity_kwh: null, need_kwh: null, missing: ["capacity"] });
    payload["charger"]["capabilities"]["target_soc"] = false; // the capability is not the gate
    const { hass, element } = await openPlan(payload, aRecord(), "sv");
    expect(q(element, "input[id$='-capacity']")).toBeNull();
    expect(q(element, "[data-soc='capacity-missing']")?.textContent).toBe(
      "Batterikapacitet saknas \u2014 ange den i Inst\u00e4llningar",
    );
    expect(rowText(element, "need")).toContain("Ok\u00e4nd");
    expect(updates(hass)).toHaveLength(0);
    expect(q(element, "[data-soc='settings-link']")).toBeNull();
  });

  it("explains that a sensor must be chosen when the block lists soc", async () => {
    const payload = fixture();
    Object.assign(payload["soc"], { value: null, age_s: null, source: null, need_kwh: null, missing: ["soc"] });
    const { element } = await openPlan(payload, aRecord());
    expect(q(element, "[data-soc='need-sensor']")?.textContent).toBe(translate("en", "settings.soc.needSensor"));
    expect(q(element, "[data-soc='facts']")?.hidden).toBe(true);
    expect(rowText(element, "need")).toContain("Unknown");
    expect(q(element, "[data-soc='settings-link']")).toBeNull();
  });

  it("offers the target only when a source exists: a null block leaves out the Charge by row and explains why", async () => {
    const payload = fixture("cheapest_no_site");
    const { element } = await openPlan(payload, aRecord({ driver: "manual_kwh", target: { vehicle_id: null, target_percent: null } }));
    // Only energy is possible: no Charge by row, as in the app.
    expect(q(element, "[data-row='charge_by']")).toBeNull();
    expect(q(element, "[data-part='energy']")?.hidden).toBe(false);
    expect(q(element, "[data-soc='need-sensor']")).not.toBeNull();
    expect(q(element, "[data-part='soc']")).toBeNull();
  });

  it("switches from energy to target and saves driver and target in one replacement", async () => {
    const payload = fixture();
    const { hass, element } = await openPlan(
      payload,
      aRecord({ driver: "manual_kwh", target: { vehicle_id: "car-1", target_percent: null } }),
    );
    q<HTMLButtonElement>(element, "[data-row='charge_by'] button")!.click();
    expect(q(element, "[data-part='energy']")?.hidden).toBe(true);
    expect(q(element, "[data-part='soc'] input[type='number']")).toBeNull();
    const slider = q<HTMLInputElement>(element, "[data-part='soc'] input[type='range']")!;
    expect(slider.value).toBe("80");
    expect(targetText(element)).toBe("80 %");
    slider.value = "90";
    slider.dispatchEvent(new Event("input"));
    expect(targetText(element)).toBe("90 %");
    saveButton(element)!.click();
    await settle();
    expect(updates(hass)).toHaveLength(1);
    const body = updates(hass)[0]!["settings"] as Record<string, any>;
    expect(body["driver"]).toBe("target_soc");
    expect(body["target"]).toEqual({ vehicle_id: "car-1", target_percent: 90 });
    expect(body["requested_kwh"]).toBe(20);
  });

  it("offers the target with a soc block while settings hold no vehicle, and saves the resolved vehicle", async () => {
    const payload = fixture();
    Object.assign(payload["soc"], { vehicle_id: "car-1", vehicles: [], capacity_kwh: null, need_kwh: null, missing: ["capacity"] });
    const record = aRecord({
      driver: "manual_kwh",
      target: { vehicle_id: null, target_percent: null },
    });
    const { hass, element } = await openPlan(payload, record);
    q<HTMLButtonElement>(element, "[data-row='charge_by'] button")!.click();
    saveButton(element)!.click();
    await settle();
    const body = updates(hass)[0]!["settings"] as Record<string, any>;
    expect(body["driver"]).toBe("target_soc");
    expect(body["target"]).toEqual({ vehicle_id: "car-1", target_percent: 80 });
    // A conflict reapply carries the vehicle too, since the reader's choice differs from what they opened.
    hass.resolveNext(refusal("revision_conflict", { ...record, revision: 9 }));
    await settle();
    q<HTMLButtonElement>(element, ".spotnav-settings-reapply")!.click();
    await settle();
    const retry = updates(hass)[1]!["settings"] as Record<string, any>;
    expect(retry["target"]["vehicle_id"]).toBe("car-1");
  });

  it("asks which vehicle when several exist and writes the picked one", async () => {
    const payload = fixture();
    Object.assign(payload["soc"], {
      vehicle_id: null,
      vehicle_name: null,
      vehicles: [{ id: "car-1", name: "EV6" }, { id: "car-2", name: "Niro" }],
      missing: ["vehicle"],
    });
    const record = aRecord({
      driver: "manual_kwh",
      target: { vehicle_id: null, target_percent: null },
    });
    const { hass, element } = await openPlan(payload, record);
    q<HTMLButtonElement>(element, "[data-row='charge_by'] button")!.click();
    const select = q<HTMLSelectElement>(element, "select[data-soc='vehicle-choice']")!;
    select.value = "car-2";
    saveButton(element)!.click();
    await settle();
    const body = updates(hass)[0]!["settings"] as Record<string, any>;
    expect(body["target"]["vehicle_id"]).toBe("car-2");
  });

  it("offers no empty vehicle once one is chosen, and only a placeholder that cannot be picked before", async () => {
    const chosen = fixture();
    Object.assign(chosen["soc"], { vehicles: [{ id: "car-1", name: "EV6" }, { id: "car-2", name: "Niro" }] });
    const first = await openPlan(chosen, aRecord());
    const picked = q<HTMLSelectElement>(first.element, "select[data-soc='vehicle-choice']")!;
    expect([...picked.options].map((option) => option.value)).toEqual(["car-1", "car-2"]);

    const unknown = fixture();
    Object.assign(unknown["soc"], {
      vehicle_id: null,
      vehicle_name: null,
      vehicles: [{ id: "car-1", name: "EV6" }, { id: "car-2", name: "Niro" }],
      missing: ["vehicle"],
    });
    const second = await openPlan(unknown, aRecord({ driver: "manual_kwh", target: { vehicle_id: null, target_percent: null } }));
    q<HTMLButtonElement>(second.element, "[data-row='charge_by'] button")!.click();
    const open = q<HTMLSelectElement>(second.element, "select[data-soc='vehicle-choice']")!;
    const placeholder = [...open.options].find((option) => option.value === "")!;
    expect(placeholder.disabled).toBe(true);
    expect(open.value).toBe("");
  });

  it("reapplies only the fields the reader moved onto a newer record after a conflict", async () => {
    const payload = fixture();
    const { hass, element } = await openPlan(payload, aRecord());
    const slider = q<HTMLInputElement>(element, "[data-part='soc'] input[type='range']")!;
    slider.value = "90";
    slider.dispatchEvent(new Event("input"));
    saveButton(element)!.click();
    await settle();
    // Someone else remembered a capacity and switched to energy meanwhile.
    const server = aRecord({
      revision: 9,
      driver: "manual_kwh",
      target: { vehicle_id: "car-1", target_percent: 70 },
    });
    hass.resolveNext(refusal("revision_conflict", server));
    await settle();
    q<HTMLButtonElement>(element, ".spotnav-settings-reapply")!.click();
    await settle();
    expect(updates(hass)).toHaveLength(2);
    const retry = updates(hass)[1]!;
    expect(retry["expected_revision"]).toBe(9);
    const body = retry["settings"] as Record<string, any>;
    expect(body["driver"], "the reader did not move the driver").toBe("manual_kwh");
    expect(body["target"]).toEqual({ vehicle_id: "car-1", target_percent: 90 });
  });
});

describe("the replacement builder for the new fields", () => {
  const values = (overrides: Record<string, string>) => ({ ...formFromRecord(aRecord()), ...overrides });

  it("sends nothing when the target mode is saved as it was read", () => {
    const check = replacementFor("plan", aRecord(), formFromRecord(aRecord()));
    expect(check).toMatchObject({ ok: true, changed: false });
  });

  it("judges the target only while the reader stands on the target", () => {
    expect(checkTargetPercent("0")).toEqual({ ok: true, value: 0 });
    expect(checkTargetPercent("-1")).toMatchObject({ ok: false });
    expect(checkTargetPercent("101")).toMatchObject({ ok: false });
    expect(checkTargetPercent("100")).toEqual({ ok: true, value: 100 });
    const manual = aRecord({ driver: "manual_kwh", target: { vehicle_id: null, target_percent: 0.5 } });
    expect(replacementFor("plan", manual, formFromRecord(manual))).toMatchObject({ ok: true, changed: false });
    expect(replacementFor("plan", aRecord(), values({ targetPercent: "" }))).toMatchObject({ ok: false });
    expect(replacementFor("plan", aRecord(), values({ driver: "sometimes" }))).toMatchObject({ ok: false });
  });

  it("keeps a stored decimal target through a Save that does not move it", () => {
    const record = aRecord({ target: { vehicle_id: "car-1", target_percent: 80.5 } });
    const check = replacementFor("plan", record, { ...formFromRecord(record), current: "12" });
    expect(check).toMatchObject({ ok: true, changed: true });
    if (check.ok) {
      expect(check.body.target).toEqual(record.target);
    }
  });
});

describe("the phase count", () => {
  it("is no longer a section of the Settings page, and has no save of its own there", async () => {
    const { element } = await mounted(fixture());
    shadow(element).querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)!.click();
    await settle();
    expect(dlg(element).querySelector("[data-section='phases']")).toBeNull();
    expect(dlg(element).querySelector("[data-action='save-phases']")).toBeNull();
    expect(dlg(element).querySelector("input[type='radio']")).toBeNull();
  });

  it("is never written by a plan replacement: the record's phases are carried through as the server sent them", () => {
    const record = aRecord();
    const values = formFromRecord(record);
    // Nothing moved, so nothing is written, whatever count the record carries.
    expect(replacementFor("plan", record, values)).toMatchObject({ ok: true, changed: false });
    expect(replacementFor("plan", aRecord({ phases: null }), formFromRecord(aRecord({ phases: null })))).toMatchObject({
      ok: true,
      changed: false,
    });
    // A moved current carries the newer record's phases along, untouched.
    const newer = aRecord({ revision: 9, phases: 1 });
    const moved = replacementFor("plan", newer, { ...formFromRecord(newer), current: "16" });
    expect(moved).toMatchObject({ ok: true, changed: true });
    if (moved.ok) {
      expect(moved.body.phases).toBe(1);
    }
  });
});

// The kWh slider's room, "full" mark and "Fill" step are kwh-fill.test.ts's.
describe("the target slider at its top", () => {
  const verdict = (element: Element) => q(element, "[data-soc='verdict']")!;
  const drag = (element: Element, value: number): void => {
    const slider = q<HTMLInputElement>(element, "[data-part='soc'] input[type='range']")!;
    slider.value = String(value);
    slider.dispatchEvent(new Event("input"));
  };

  it("says the limit once, as the verdict, from a target at the car's own limit", async () => {
    const payload = fixture();
    Object.assign(payload["soc"], { vehicle_max_percent: 80 });
    const { element } = await openPlan(payload, aRecord({ target: { vehicle_id: "<id>", target_percent: 79 } }), "sv");
    expect(verdict(element).hidden).toBe(true);
    drag(element, 80);
    expect(targetText(element)).toBe("80 %");
    expect(verdict(element).textContent).toBe("Laddar till bilens gräns, 80 %");
    expect(q(element, "[data-soc='car-ends']")).toBeNull();
  });

  it("adds no note at 100 % when the car states no limit", async () => {
    const { element } = await openPlan(fixture(), aRecord({ target: { vehicle_id: "<id>", target_percent: 99 } }));
    drag(element, 100);
    expect(verdict(element).hidden).toBe(true);
    expect(q(element, "[data-soc='car-ends']")).toBeNull();
    expect(rowText(element, "need")).toContain("kWh");
  });
});

describe("the target slider's value on its label row", () => {
  const slider = (element: Element) => q<HTMLInputElement>(element, "[data-part='soc'] input[type='range']")!;

  it("has no number field and no unit: the slider alone, full width, its value at the end of the label row", async () => {
    for (const language of ["en", "sv"] as const) {
      document.body.innerHTML = "";
      const { element } = await openPlan(fixture(), aRecord(), language);
      const group = q(element, "[data-part='soc'] [role='group']")!;
      expect(group.querySelectorAll("input")).toHaveLength(1);
      expect(group.querySelector(".spotnav-settings-unit")).toBeNull();
      expect(group.querySelector(".spotnav-settings-pair")).toBeNull();
      // No "slider out of range" note: the value text says what is stored.
      expect(group.querySelector(".spotnav-settings-note")).toBeNull();
      const label = group.querySelector("label")!;
      const value = q(element, "[data-part='soc'] [data-part='target-value']")!;
      expect(value.parentElement).toBe(label.parentElement);
      expect(label.getAttribute("for")).toBe(slider(element).id);
      expect(value.textContent, language).toBe("80 %");
      expect(slider(element).getAttribute("aria-valuetext"), language).toBe("80 %");
      expect(slider(element).disabled).toBe(false);
      expect([slider(element).min, slider(element).max, slider(element).step]).toEqual(["0", "100", "1"]);
    }
  });

  it("follows the slider and saves what it stands at", async () => {
    const { hass, element } = await openPlan(fixture(), aRecord());
    slider(element).value = "65";
    slider(element).dispatchEvent(new Event("input"));
    expect(targetText(element)).toBe("65 %");
    expect(slider(element).getAttribute("aria-valuetext")).toBe("65 %");
    saveButton(element)!.click();
    await settle();
    const body = updates(hass)[0]!["settings"] as Record<string, any>;
    expect(body["target"]["target_percent"]).toBe(65);
  });

  it.each<[number, string, string, string]>([
    [80.5, "81", "80.5 %", "80,5 %"],
    [72.25, "72", "72.25 %", "72,25 %"],
  ])("shows a stored %s percent off the slider's step exactly, the slider at its nearest step, and keeps it on Save", async (stored, at, en, sv) => {
    for (const [language, text] of [["en", en], ["sv", sv]] as const) {
      document.body.innerHTML = "";
      const { hass, element } = await openPlan(
        fixture(),
        aRecord({ target: { vehicle_id: "car-1", target_percent: stored } }),
        language,
      );
      expect(targetText(element), language).toBe(text);
      expect(slider(element).getAttribute("aria-valuetext"), language).toBe(text);
      expect(slider(element).value, language).toBe(at);
      expect(slider(element).disabled, language).toBe(false);
      // A Save that moved the current, not the slider, keeps the stored target exactly.
      const current = q<HTMLInputElement>(element, "[aria-labelledby$='-current-label'] input[type='range']")!;
      current.value = "16";
      current.dispatchEvent(new Event("input"));
      saveButton(element)!.click();
      await settle();
      const body = updates(hass)[0]!["settings"] as Record<string, any>;
      expect(body["target"]["target_percent"], language).toBe(stored);
      expect(body["amps"], language).toBe(16);
    }
  });

  it("shows a non-administrator the value and a disabled slider", async () => {
    const { hass, element } = await mounted(fixture(), "en", false);
    shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
    await settle();
    hass.resolveNext(success(aRecord({ target: { vehicle_id: "car-1", target_percent: 80.5 } })));
    await settle();
    expect(targetText(element)).toBe("80.5 %");
    expect(slider(element).disabled).toBe(true);
    expect(saveButton(element)).toBeNull();
  });
});
