// The entity configuration on the Settings page and in its two editors, through
// the real card element with a fake transport. The answers are the backend's own committed fixtures
// (`tests/fixtures/entity_config/v1`), read from there -- never a copy.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import { translate } from "../src/i18n";
import { FakeHass, mountCard } from "./helpers";

const ENTITY_DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");
const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function entityFixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(ENTITY_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

function dashboardFixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}


async function mounted(
  options: {
    admin?: boolean;
    dashboard?: string;
    get?: string;
    update?: string | null;
    patch?: (answer: Record<string, unknown>) => Record<string, unknown>;
    patchDashboard?: (payload: Record<string, unknown>) => void;
  } = {},
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard>; payload: Record<string, unknown> }> {
  const hass = new FakeHass();
  const get = options.get ?? "get_direct";
  const update = options.update ?? null;
  hass.entityHandler = async (message) => {
    if (message["type"] === "spotnav/get_entity_config") {
      return options.patch === undefined ? entityFixture(get) : options.patch(entityFixture(get));
    }
    if (update === null) {
      throw new Error("no update expected");
    }
    return entityFixture(update);
  };
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  if (options.admin ?? true) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  await settle();
  const payload = dashboardFixture(options.dashboard ?? "cheapest_direct_site_admin");
  options.patchDashboard?.(payload);
  hass.resolveNext(payload);
  await settle();
  return { hass, element, payload };
}

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

function openSettings(element: Element): void {
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)
    ?.click();
}

const rows = (element: Element, group: string): HTMLElement[] =>
  Array.from(openDialog(element)?.querySelectorAll<HTMLElement>(`[data-entity-group="${group}"] [data-row]`) ?? []);

const rowText = (element: Element, field: string): string =>
  openDialog(element)?.querySelector(`[data-row="${field}"]`)?.textContent ?? "";

function edit(element: Element, scope: "charger" | "site"): void {
  openDialog(element)?.querySelector<HTMLButtonElement>(`[data-edit-entities="${scope}"]`)?.click();
}

function field(element: Element, name: string): HTMLInputElement {
  const found = openDialog(element)?.querySelector<HTMLInputElement>(`[data-field="${name}"]`) ?? null;
  if (found === null) {
    throw new Error(`no field ${name}`);
  }
  return found;
}

/** Pick one radio of a choice group (grid, current-source, battery, energy), as a person would. */
function choose(element: Element, part: string, value: string): void {
  const radio = openDialog(element)?.querySelector<HTMLInputElement>(`[data-part='${part}'] input[data-choice='${value}']`) ?? null;
  if (radio === null) {
    throw new Error(`no choice ${part}/${value}`);
  }
  radio.checked = true;
  radio.dispatchEvent(new Event("change", { bubbles: true }));
}

function type(element: Element, name: string, value: string): void {
  const input = field(element, name);
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function save(element: Element): void {
  openDialog(element)?.querySelector<HTMLButtonElement>(".spotnav-settings-save")?.click();
}

function fieldError(element: Element, name: string): HTMLElement | null {
  const node = openDialog(element)?.querySelector<HTMLElement>(`[data-field-error="${name}"]`) ?? null;
  return node === null || node.hidden ? null : node;
}

const updates = (hass: FakeHass) => hass.entityMessages.filter((m) => m["type"] === "spotnav/update_entity_config");
const gets = (hass: FakeHass) => hass.entityMessages.filter((m) => m["type"] === "spotnav/get_entity_config");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("the Settings page's charger and site cards", () => {
  const sectionRows = (element: Element, section: string): string[] =>
    Array.from(openDialog(element)?.querySelectorAll<HTMLElement>(`[data-section='${section}'] [data-row]`) ?? []).map(
      (row) => row.dataset["row"] ?? "",
    );

  it("reads the configuration once on opening, as an administrator, and summarises the charger in three rows", async () => {
    const { hass, element } = await mounted();
    expect(gets(hass)).toHaveLength(0);
    openSettings(element);
    await settle();
    expect(gets(hass)).toEqual([{ type: "spotnav/get_entity_config", api_version: 1, charger_id: "entry_a" }]);

    expect(sectionRows(element, "entities")).toEqual([
      "start_stop",
      "current",
      "energy_register",
      "charger_priority",
    ]);
    expect(rowText(element, "start_stop")).toContain(translate("en", "control.startStop"));
    // Short status words, never the entity's name or id.
    expect(rowText(element, "start_stop")).toContain(translate("en", "settings.status.active"));
    expect(rowText(element, "start_stop")).not.toContain("Control get_direct");
    expect(rowText(element, "current")).toContain(translate("en", "settings.status.notControlled"));
    // Nothing chosen and nothing found: the energy meter is said to be found automatically.
    expect(rowText(element, "energy_register")).toContain(translate("en", "settings.status.foundAutomatically"));
    // Each setup row opens its own part of the charger's entity dialog; no sub-heading, no vehicle level.
    expect(
      Array.from(openDialog(element)?.querySelectorAll<HTMLElement>("[data-section='entities'] [data-edit-entities]") ?? []).map(
        (node) => node.dataset["edit"],
      ),
    ).toEqual(["start_stop", "current", "energy_register"]);
    expect(openDialog(element)?.querySelector("[data-section='entities'] [data-row='vehicle_soc']")).toBeNull();
    expect(openDialog(element)?.querySelector("[data-section='entities'] h4")?.textContent).toMatch(
      new RegExp(`^${translate("en", "settings.heading.charger")} · `),
    );
  });

  it("summarises the site in words and lists only what is configured, never a 'not set' row", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    const site = openDialog(element)?.querySelector<HTMLElement>("[data-section='site']");
    expect(site?.textContent).toContain(translate("en", "site.applies.one"));
    expect(sectionRows(element, "site")).toEqual(["main_fuse_a", "measurement_mode", "battery", "active-control"]);
    expect(rowText(element, "main_fuse_a")).toContain("25 A");
    expect(rowText(element, "measurement_mode")).toContain(translate("en", "entity.mode.direct"));
    expect(rowText(element, "battery")).toContain(translate("en", "settings.value.none"));
    // The raw entity fields of the editor are not listed, and nothing says "not set".
    expect(site?.querySelector("[data-row^='direct_L']")).toBeNull();
    expect(site?.querySelector("[data-row='battery_discharge_power_entity']")).toBeNull();
    expect(site?.textContent).not.toContain(translate("en", "entity.notSet"));
    expect(site?.querySelector("[data-edit-entities='site']")).not.toBeNull();
    // The charger's rows are not repeated inside the site card.
    expect(site?.querySelector("[data-row='start_stop']")).toBeNull();
  });

  it("names a configured battery and shows no help toggle anywhere on the overview", async () => {
    const { element } = await mounted({
      patch: (answer) => {
        const config = answer["config"] as Record<string, any>;
        const fields = (config["fields"] as Array<Record<string, any>>).map((entry) =>
          entry["field"] === "battery_aggregate_power_entity"
            ? { ...entry, current: { entity_id: "sensor.home_battery", exists: true, friendly_name: "Home battery" } }
            : entry,
        );
        return { ...answer, config: { ...config, fields } };
      },
    });
    openSettings(element);
    await settle();
    expect(rowText(element, "battery")).toContain(translate("en", "settings.status.present"));
    expect(rowText(element, "battery")).not.toContain("Home battery");
    expect(openDialog(element)?.querySelector("[data-help-for]")).toBeNull();
    expect(openDialog(element)?.querySelector(".spotnav-entity-help-toggle")).toBeNull();
  });

  it("asks a non-administrator nothing, says only administrators can change entities, and offers no editor", async () => {
    const { hass, element } = await mounted({ admin: false });
    openSettings(element);
    await settle();
    expect(gets(hass)).toHaveLength(0);
    const section = openDialog(element)?.querySelector<HTMLElement>("[data-section='entities']");
    expect(section?.textContent).toContain(translate("en", "entity.adminOnly"));
    // Nothing to change: no entity dialog, and no value in the accent colour (the price area opens read-only).
    expect(openDialog(element)?.querySelectorAll("[data-edit-entities], .spotnav-setting-row-editable")).toHaveLength(0);
    expect(openDialog(element)?.textContent).toContain(translate("en", "settings.readOnly"));
  });

  it("states a failed read in words, with the code only as an attribute, and offers no editor", async () => {
    const { hass, element } = await mounted();
    hass.entityHandler = async () => entityFixture("no_site");
    openSettings(element);
    await settle();
    const section = openDialog(element)?.querySelector<HTMLElement>("[data-section='entities']");
    expect(section?.textContent).toContain(translate("en", "entity.error.read"));
    expect(section?.textContent).not.toContain("spotnav_no_site");
    expect(section?.querySelector("[data-code]")?.getAttribute("data-code")).toBe("spotnav_no_site");
    expect(openDialog(element)?.querySelector("[data-section='entities'] [data-edit-entities]")).toBeNull();
  });

  it("shows the charger's priority in words from the dashboard block, and hides it when the block is null or unreadable", async () => {
    const shown = await mounted({
      patchDashboard: (payload) => {
        (payload["charger_priority"] as Record<string, unknown>)["value"] = "last";
      },
    });
    openSettings(shown.element);
    await settle();
    expect(rowText(shown.element, "charger_priority")).toContain(translate("en", "entity.field.chargerPriority"));
    expect(rowText(shown.element, "charger_priority")).toContain(translate("en", "entity.priority.last"));

    for (const block of [null, "junk", { value: 3 }]) {
      document.body.innerHTML = "";
      const hidden = await mounted({
        patchDashboard: (payload) => {
          payload["charger_priority"] = block;
        },
      });
      openSettings(hidden.element);
      await settle();
      expect(sectionRows(hidden.element, "entities")).toEqual(["start_stop", "current", "energy_register"]);
    }
    document.body.innerHTML = "";
    const absent = await mounted({
      patchDashboard: (payload) => {
        delete payload["charger_priority"];
      },
    });
    openSettings(absent.element);
    await settle();
    expect(sectionRows(absent.element, "entities")).toEqual(["start_stop", "current", "energy_register"]);
  });

  it("shows only the charger's card and no site rows when the charger has no site", async () => {
    const { element } = await mounted({ dashboard: "cheapest_no_site", get: "get_no_site" });
    openSettings(element);
    await settle();
    // With no site, the charger holds its own wiring: two rows more than the three setup rows.
    expect(sectionRows(element, "entities")).toEqual([
      "start_stop",
      "current",
      "energy_register",
      "charger_phases",
      "voltage_between_phases_v",
    ]);
    const site = openDialog(element)?.querySelector("[data-section='site']");
    expect(site?.textContent).toContain(translate("en", "site.none"));
    expect(site?.querySelector("[data-row]")).toBeNull();
    expect(openDialog(element)?.querySelector("[data-section='solar']")).toBeNull();
  });

  it("heads the site as Site · its name, and Site alone when it has none", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    const heading = openDialog(element)?.querySelector('[data-section="site"] h4')?.textContent ?? "";
    expect(heading).toBe(
      `${translate("en", "settings.heading.site")} · ${(dashboardFixture("cheapest_direct_site_admin")["site"] as { name: string }).name}`,
    );

    document.body.innerHTML = "";
    const again = await mounted({
      patchDashboard: (payload) => {
        (payload["site"] as Record<string, unknown>)["name"] = "";
      },
    });
    openSettings(again.element);
    await settle();
    expect(openDialog(again.element)?.querySelector('[data-section="site"] h4')?.textContent).toBe(
      translate("en", "settings.heading.site"),
    );
  });
});

describe("the charger's editor", () => {
  it("is a plain text field for the entity when Home Assistant's picker is not loaded", async () => {
    expect(customElements.get("ha-selector")).toBeUndefined();
    const { element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "charger");
    // Opened from Start and stop: titled by that row, the charge control shown alone.
    expect(openDialog(element)?.querySelector("h3")?.textContent).toBe(translate("en", "control.startStop"));
    expect(field(element, "charge_control").value).toBe("switch.get_direct_control");
    expect(field(element, "current_limit").value).toBe("");
    expect(openDialog(element)?.querySelector("[data-field='max_age_s']")).toBeNull();
  });

  it("sends only the changed fields, expecting what it read, and adopts the success", async () => {
    const { hass, element } = await mounted({ update: "success_charger" });
    openSettings(element);
    await settle();
    edit(element, "charger");
    type(element, "current_limit", "number.charger_limit");
    save(element);
    await settle();
    expect(updates(hass)).toEqual([
      {
        type: "spotnav/update_entity_config",
        api_version: 1,
        charger_id: "entry_a",
        scope: "charger",
        expected: { current_limit: "" },
        changes: { current_limit: "number.charger_limit" },
      },
    ]);
  });

  it("sends nothing when nothing changed, and returns to the popover", async () => {
    const { hass, element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "charger");
    save(element);
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(openDialog(element)?.querySelector("[data-section='entities']")).not.toBeNull();
  });

  it("marks each refused field with the sentence for its code, and keeps what was typed", async () => {
    const { hass, element } = await mounted({ update: "field_errors_charger" });
    openSettings(element);
    await settle();
    edit(element, "charger");
    type(element, "charge_control", "sensor.not_a_switch");
    type(element, "current_limit", "number.missing");
    choose(element, "energy", "meter");
    save(element);
    await settle();
    expect(updates(hass)).toHaveLength(1);
    expect(fieldError(element, "charge_control")?.textContent).toBe(translate("en", "entity.error.field.wrongDomain"));
    expect(fieldError(element, "charge_control")?.getAttribute("data-code")).toBe("wrong_domain");
    expect(fieldError(element, "current_limit")?.textContent).toBe(translate("en", "entity.error.field.notFound"));
    expect(field(element, "charge_control").getAttribute("aria-invalid")).toBe("true");
    expect(field(element, "charge_control").value).toBe("sensor.not_a_switch");
    expect(openDialog(element)?.textContent).toContain(translate("en", "entity.error.invalid"));
    // Not optimistic: the popover rows behind it are still what was read.
    expect(field(element, "energy_register_entity").getAttribute("aria-invalid")).toBeNull();
  });

  it("says a switch is in use by another charger, on that field", async () => {
    const { element } = await mounted({ update: "field_errors_in_use" });
    openSettings(element);
    await settle();
    edit(element, "charger");
    type(element, "charge_control", "switch.taken");
    save(element);
    await settle();
    expect(fieldError(element, "charge_control")?.textContent).toBe(
      translate("en", "entity.error.field.chargeControlInUse"),
    );
  });

  it("redraws from the returned config on a conflict, and says values changed elsewhere", async () => {
    const { hass, element } = await mounted({ update: "conflict" });
    openSettings(element);
    await settle();
    edit(element, "charger");
    type(element, "current_limit", "number.mine");
    save(element);
    await settle();
    const dialog = openDialog(element);
    expect(dialog?.textContent).toContain(translate("en", "entity.error.conflict"));
    expect(dialog?.querySelector("[data-code='spotnav_conflict']")).not.toBeNull();
    // Drawn from the answer: the reader's typed value is gone, the server's is shown.
    expect(field(element, "current_limit").value).toBe("");
    expect(field(element, "charge_control").value).toBe("switch.conflict_control");
    // A second Save compares against the answer, not against what was first read.
    type(element, "current_limit", "number.again");
    hass.entityHandler = async () => entityFixture("success_charger");
    save(element);
    await settle();
    expect(updates(hass)).toHaveLength(2);
    expect(updates(hass)[1]?.["expected"]).toEqual({ current_limit: "" });
    expect(updates(hass)[1]?.["charger_id"]).toBe("entry_a");
  });

  it("says one general thing for any other refusal and never marks a field", async () => {
    const { hass, element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "charger");
    hass.entityHandler = async () => entityFixture("not_admin");
    type(element, "current_limit", "number.x");
    save(element);
    await settle();
    expect(openDialog(element)?.textContent).toContain(translate("en", "entity.error.notAdmin"));
    expect(fieldError(element, "current_limit")).toBeNull();
  });

  it("says a failed request in words, without any transport text", async () => {
    const { hass, element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "charger");
    hass.entityHandler = async () => {
      throw Object.assign(new Error("boom: secret detail"), { code: "unknown_error" });
    };
    type(element, "current_limit", "number.x");
    save(element);
    await settle();
    const text = openDialog(element)?.textContent ?? "";
    expect(text).toContain(translate("en", "entity.error.generic"));
    expect(text).not.toContain("secret");
  });

  it("stays open, with a typed value intact, across two refresh periods", async () => {
    const { hass, element, payload } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "charger");
    type(element, "current_limit", "number.half_typed");
    const before = openDialog(element);
    for (let tick = 0; tick < 2; tick += 1) {
      await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
      hass.resolveNext(payload);
      await settle();
    }
    expect(openDialog(element)).toBe(before);
    expect(field(element, "current_limit").value).toBe("number.half_typed");
  });
});

describe("the site's editor", () => {
  it("shows the marker, the fuse, the mode and the three direct meters", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "site");
    const dialog = openDialog(element);
    expect(dialog?.textContent).toContain(translate("en", "site.applies.one"));
    expect(field(element, "main_fuse_a").value).toBe("25");
    expect(field(element, "max_age_s").value).toBe("120");
    expect(field(element, "direct_L1").value).toBe("sensor.site_get_direct_l1");
    expect(dialog?.querySelector("[data-field='derived_L1_power']")).toBeNull();
    const direct = dialog?.querySelector<HTMLInputElement>("input[type='radio'][data-mode='direct_phase_current']");
    expect(direct?.checked).toBe(true);
  });

  it("shows the safety margin right after the main fuse, with its help line and unit", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "site");
    const dialog = openDialog(element);
    expect(field(element, "safety_margin_a").value).toBe("1");
    const order = Array.from(dialog?.querySelectorAll<HTMLElement>("[data-field-block]") ?? []).map(
      (block) => block.dataset["fieldBlock"],
    );
    expect(order.indexOf("safety_margin_a")).toBe(order.indexOf("main_fuse_a") + 1);
    const block = dialog?.querySelector<HTMLElement>("[data-field-block='safety_margin_a']");
    expect(block?.textContent).toContain(translate("en", "entity.field.safetyMargin"));
    expect(block?.textContent).toContain(translate("en", "entity.help.safetyMargin"));
    expect(block?.textContent).toContain("A");
  });

  it("sends a changed safety margin as a number, expecting the number it read", async () => {
    const { hass, element } = await mounted({ update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    type(element, "safety_margin_a", "2.5");
    save(element);
    await settle();
    expect(updates(hass)).toEqual([
      {
        type: "spotnav/update_entity_config",
        api_version: 1,
        charger_id: "entry_a",
        scope: "site",
        expected: { safety_margin_a: 1 },
        changes: { safety_margin_a: 2.5 },
      },
    ]);
  });

  it("refuses a negative safety margin before any request", async () => {
    const { hass, element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "site");
    type(element, "safety_margin_a", "-1");
    save(element);
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(fieldError(element, "safety_margin_a")?.textContent).toBe(translate("en", "entity.error.field.invalid"));
  });

  it("sends a changed fuse as a number, expecting the number it read", async () => {
    const { hass, element } = await mounted({ update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    type(element, "main_fuse_a", "32");
    save(element);
    await settle();
    expect(updates(hass)).toEqual([
      {
        type: "spotnav/update_entity_config",
        api_version: 1,
        charger_id: "entry_a",
        scope: "site",
        expected: { main_fuse_a: 25 },
        changes: { main_fuse_a: 32 },
      },
    ]);
  });

  it("refuses a fuse below the minimum, and an empty one, before any request", async () => {
    const { hass, element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "site");
    type(element, "main_fuse_a", "0");
    save(element);
    await settle();
    expect(updates(hass)).toHaveLength(0);
    expect(fieldError(element, "main_fuse_a")?.textContent).toBe(translate("en", "entity.error.field.invalid"));
    type(element, "main_fuse_a", "");
    save(element);
    await settle();
    expect(fieldError(element, "main_fuse_a")?.textContent).toBe(translate("en", "entity.error.field.required"));
    expect(updates(hass)).toHaveLength(0);
  });

  it("shows the other mode's meters at once when the mode is switched, and hides the direct three", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "site");
    const derived = openDialog(element)?.querySelector<HTMLInputElement>(
      "input[type='radio'][data-mode='derived_phase_current']",
    );
    derived!.checked = true;
    derived!.dispatchEvent(new Event("change", { bubbles: true }));
    const dialog = openDialog(element);
    const names = Array.from(dialog?.querySelectorAll<HTMLElement>("[data-field]") ?? []).map((node) => node.dataset["field"]);
    // Per phase power and voltage only: nothing is stored, so one sensor with direction and an estimated
    // current, and the other kinds' fields are not in the dialog.
    expect(names.filter((name) => name?.startsWith("derived_"))).toEqual(
      ["L1", "L2", "L3"].flatMap((phase) => ["power", "voltage"].map((kind) => `derived_${phase}_${kind}`)),
    );
    expect(names.some((name) => name?.startsWith("direct_"))).toBe(false);
    // And back: the direct three return with what was there.
    const direct = dialog?.querySelector<HTMLInputElement>("input[type='radio'][data-mode='direct_phase_current']");
    direct!.checked = true;
    direct!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(field(element, "direct_L2").value).toBe("sensor.site_get_direct_l2");
  });

  it("sends the mode with every meter the new mode needs, and marks the ones the backend says are missing", async () => {
    const { hass, element } = await mounted({ update: "derived_requirements" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const derived = openDialog(element)?.querySelector<HTMLInputElement>(
      "input[type='radio'][data-mode='derived_phase_current']",
    );
    derived!.checked = true;
    derived!.dispatchEvent(new Event("change", { bubbles: true }));
    type(element, "derived_L1_power", "sensor.l1_power");
    save(element);
    await settle();
    expect(updates(hass)).toEqual([
      {
        type: "spotnav/update_entity_config",
        api_version: 1,
        charger_id: "entry_a",
        scope: "site",
        expected: { measurement_mode: "direct_phase_current" },
        changes: { measurement_mode: "derived_phase_current", derived_L1_power: "sensor.l1_power" },
      },
    ]);
    // Six fields are named `required`; every one shows its sentence on its own field.
    for (const name of ["derived_L1_power", "derived_L1_voltage", "derived_L3_voltage"]) {
      expect(fieldError(element, name)?.textContent, name).toBe(translate("en", "entity.error.field.required"));
    }
  });

  it("marks a site field the backend refused", async () => {
    const { element } = await mounted({ update: "field_errors_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    type(element, "direct_L1", "sensor.wrong");
    save(element);
    await settle();
    expect(openDialog(element)?.querySelector("[data-field-error][data-code]")).not.toBeNull();
  });

  it("adopts a success: the popover is back, with a fresh read of the dashboard", async () => {
    const { hass, element, payload } = await mounted({ update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    type(element, "main_fuse_a", "32");
    save(element);
    await settle();
    // The reload changes what the card shows: one confirmation read of the dashboard.
    hass.resolveNext(payload);
    await settle();
    expect(openDialog(element)?.querySelector("[data-section='entities']")).not.toBeNull();
    expect(openDialog(element)?.querySelector("[data-entity-editor]")).toBeNull();
    // The reopened popover read the configuration again.
    expect(gets(hass).length).toBeGreaterThanOrEqual(2);
  });
});

describe("Home Assistant's own picker", () => {
  class FakeSelector extends HTMLElement {
    hass: unknown;
    selector: unknown;
    value: unknown;
    label = "";
    required = false;
    disabled = false;
  }

  beforeEach(() => {
    if (customElements.get("ha-selector") === undefined) {
      customElements.define("ha-selector", FakeSelector);
    }
  });

  it("is used when defined, filtered by what the backend allows, and reports through value-changed", async () => {
    // `customElements` cannot forget a definition, so this test is the only one that defines it; the
    // tests above ran first and proved the fallback.
    const { hass, element } = await mounted({ update: "success_charger" });
    openSettings(element);
    await settle();
    edit(element, "charger");
    const picker = openDialog(element)?.querySelector<FakeSelector>("ha-selector[data-field='charge_control']");
    expect(picker).not.toBeNull();
    expect(picker!.selector).toEqual({ entity: { domain: ["switch"] } });
    expect(picker!.value).toBe("switch.get_direct_control");
    expect(picker!.hass).toBeTruthy();
    choose(element, "energy", "meter");
    const energy = openDialog(element)?.querySelector<FakeSelector>("ha-selector[data-field='energy_register_entity']");
    expect(energy!.selector).toEqual({ entity: { domain: ["sensor"], device_class: ["energy"] } });
    expect(energy!.value).toBeUndefined();

    energy!.dispatchEvent(new CustomEvent("value-changed", { detail: { value: "sensor.energy_total" }, bubbles: true }));
    save(element);
    await settle();
    expect(updates(hass)[0]?.["changes"]).toEqual({ energy_register_entity: "sensor.energy_total" });
  });
});


/** A realistic installation: a current limit that no longer exists, an auto-found register. */
function ownersInstallation(answer: Record<string, unknown>): Record<string, unknown> {
  const config = answer["config"] as { fields: Array<Record<string, unknown>> };
  const patched = config.fields.map((entry) => {
    if (entry["field"] === "charge_control") {
      const ref = { entity_id: "switch.halo_charger_connector_1_charge_control", friendly_name: "halo_charger Connector 1 Charge Control" };
      return { ...entry, current: { ...ref, exists: true }, effective: { ...ref, source: "configured" } };
    }
    if (entry["field"] === "current_limit") {
      const missing = "number.halo_charger_connector_1_maximum_current";
      return {
        ...entry,
        current: { entity_id: missing, friendly_name: missing, exists: false },
        effective: {
          entity_id: "number.halo_charger_connector_1_session_current_limit",
          friendly_name: "halo_charger Connector 1 Session Current Limit",
          source: "automatic",
        },
      };
    }
    if (entry["field"] === "energy_register_entity") {
      return {
        ...entry,
        current: null,
        effective: {
          entity_id: "sensor.halo_charger_connector_1_energy_active_import_register",
          friendly_name: "halo_charger Connector 1 Energy Active Import Register",
          source: "automatic",
        },
      };
    }
    return entry;
  });
  return { ...answer, config: { ...config, fields: patched } };
}

describe("a charger behind a smart plug", () => {
  const withPlug = (answer: Record<string, unknown>) => {
    const config = answer["config"] as { fields: Array<Record<string, unknown>> };
    const fields = config.fields.map((entry) =>
      entry["field"] === "power_entity"
        ? { ...entry, current: { entity_id: "sensor.plug_power", friendly_name: "Garage plug power", exists: true } }
        : entry,
    );
    return { ...answer, config: { ...config, fields } };
  };

  it("shows the plug's power sensor on the overview only once one is chosen, and offers it in the dialog", async () => {
    const none = await mounted();
    openSettings(none.element);
    await settle();
    expect(openDialog(none.element)?.querySelector("[data-row='power_entity']")).toBeNull();

    const { element } = await mounted({ patch: withPlug });
    openSettings(element);
    await settle();
    expect(rowText(element, "power_entity")).toContain(translate("en", "settings.status.present"));
    expect(rowText(element, "power_entity")).toContain(translate("en", "entity.field.powerEntity"));
    edit(element, "charger");
    const block = openDialog(element)?.querySelector("[data-field-block='power_entity'], [data-field='power_entity']");
    expect(block).not.toBeNull();
    expect(openDialog(element)?.textContent).toContain(translate("en", "entity.help.powerEntity"));
    expect(translate("sv", "entity.help.powerEntity")).toContain("smart plugg");
  });
});

describe("what is actually in use, and what no longer exists", () => {
  it("says an automatically found energy meter as such on the overview, not as 'Not set'", async () => {
    const { element } = await mounted({ patch: ownersInstallation });
    openSettings(element);
    await settle();
    const register = rowText(element, "energy_register");
    expect(register).toContain(translate("en", "settings.status.foundAutomatically"));
    expect(register).not.toContain(translate("en", "entity.notSet"));
  });

  it("shows the warning, the help and the automatic entity in the editor", async () => {
    const { element } = await mounted({ patch: ownersInstallation });
    openSettings(element);
    await settle();
    edit(element, "charger");
    const dialog = openDialog(element);
    const block = (name: string) => dialog?.querySelector<HTMLElement>(`[data-field-block="${name}"]`);
    expect(block("current_limit")?.querySelector("[data-missing]")?.textContent).toBe(
      translate("en", "entity.missing.optional"),
    );
    // The automatic value is a radio of the group, with the help once above it; a stored entity that is gone
    // keeps "Choose an entity" selected, with the warning under its picker.
    const part = (name: string) => dialog?.querySelector<HTMLElement>(`[data-part="${name}"]`);
    expect(part("current-limit")?.textContent).toContain(translate("en", "entity.help.currentLimit"));
    expect(part("current-limit")?.textContent).toContain("Automatic: halo_charger Connector 1 Session Current Limit");
    expect(part("current-limit")?.querySelector<HTMLInputElement>("input:checked")?.dataset["choice"]).toBe("choose");
    expect(part("energy-source")?.textContent).toContain(
      "Automatic: halo_charger Connector 1 Energy Active Import Register",
    );
    expect(part("energy-source")?.textContent).toContain(translate("en", "entity.help.energyRegister"));
    expect(part("energy-source")?.querySelector<HTMLInputElement>("input:checked")?.dataset["choice"]).toBe("automatic");
    expect(block("energy_register_entity")).toBeNull();
    expect(block("charge_control")?.querySelector("[data-missing]")).toBeNull();
    expect(block("charge_control")?.textContent).toContain(translate("en", "entity.help.chargeControl"));
  });
});

describe("an energy meter and the person's None", () => {
  const METER = { entity_id: "sensor.halo_energy", friendly_name: "HALO energy" };
  const register = (none: { chosen: boolean; automatic: typeof METER | null }, automaticInUse: boolean) =>
    (answer: Record<string, unknown>): Record<string, unknown> => {
      const config = answer["config"] as { fields: Array<Record<string, unknown>> };
      const fields = config.fields.map((entry) =>
        entry["field"] === "energy_register_entity"
          ? {
              ...entry,
              current: null,
              effective: automaticInUse ? { ...METER, source: "automatic" } : null,
              none: { allowed: true, ...none },
            }
          : entry,
      );
      return { ...answer, config: { ...config, fields } };
    };

  it("says None on the overview and lists the meter it found under To check in the charger's editor", async () => {
    const { element } = await mounted({ patch: register({ chosen: true, automatic: METER }, false) });
    openSettings(element);
    await settle();
    expect(rowText(element, "energy_register")).toContain(translate("en", "entity.energy.none"));
    edit(element, "charger");
    const dialog = openDialog(element);
    expect(dialog?.querySelector<HTMLInputElement>("[data-part='energy'] input:checked")?.dataset["choice"]).toBe("none");
    const checks = dialog?.querySelector("[data-notices='charger']");
    expect(checks?.tagName).toBe("FIELDSET");
    expect(checks?.querySelector("legend")?.textContent).toBe(translate("en", "entity.checks.title"));
    expect(checks?.querySelector("[data-notice='energy_register_available']")?.textContent).toBe(
      translate("en", "entity.checks.energyRegister", { name: "HALO energy" }),
    );
  });

  it("has nothing to check while the meter is in use", async () => {
    const { element } = await mounted({ patch: register({ chosen: false, automatic: METER }, true) });
    openSettings(element);
    await settle();
    edit(element, "charger");
    expect(openDialog(element)?.querySelector("[data-notices='charger']")).toBeNull();
  });

  it("sends None when it replaces the meter SpotNav found", async () => {
    const { hass, element } = await mounted({
      update: "success_charger",
      patch: register({ chosen: false, automatic: METER }, true),
    });
    openSettings(element);
    await settle();
    edit(element, "charger");
    choose(element, "energy", "none");
    save(element);
    await settle();
    expect(updates(hass)[0]?.["changes"]).toEqual({ energy_register_entity: "none" });
    expect(updates(hass)[0]?.["expected"]).toEqual({ energy_register_entity: "" });
  });

  it("sends the automatic meter back, and nothing when None stays chosen", async () => {
    const kept = await mounted({ update: "success_charger", patch: register({ chosen: true, automatic: METER }, false) });
    openSettings(kept.element);
    await settle();
    edit(kept.element, "charger");
    save(kept.element);
    await settle();
    expect(updates(kept.hass)).toHaveLength(0);

    const { hass, element } = await mounted({ update: "success_charger", patch: register({ chosen: true, automatic: METER }, false) });
    openSettings(element);
    await settle();
    edit(element, "charger");
    choose(element, "energy", "meter");
    choose(element, "energy-source", "automatic");
    save(element);
    await settle();
    expect(updates(hass)[0]?.["changes"]).toEqual({ energy_register_entity: "" });
    expect(updates(hass)[0]?.["expected"]).toEqual({ energy_register_entity: "none" });
  });

  it("does not send None when there is no meter to opt out of", async () => {
    const { hass, element } = await mounted({ update: "success_charger", patch: register({ chosen: false, automatic: null }, false) });
    openSettings(element);
    await settle();
    edit(element, "charger");
    choose(element, "energy", "none");
    save(element);
    await settle();
    expect(updates(hass)).toHaveLength(0);
  });
});

describe("the site's estimate, warnings, sign options and detected meters", () => {
  it("says in the site editor that the current is estimated, and names the slow and self-balancing sources", async () => {
    const { element } = await mounted({ get: "get_detected" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const notices = openDialog(element)?.querySelector("[data-notices='site']");
    expect(notices?.querySelector("[data-notice='estimated']")?.textContent).toContain("0.9");
    expect(notices?.querySelector("[data-warning='update_interval_exceeds_max_age']")?.textContent).toContain("300");
    expect(notices?.querySelector("[data-warning='own_load_balancing']")?.textContent).toContain("Easee Equalizer");
  });

  it("names the meter's unavailable sensors as an inverter in standby in the site's notices", async () => {
    const warning = {
      code: "measurement_unhealthy",
      integration: null,
      entity_id: null,
      interval_s: 120,
      option: null,
      device_name: null,
      phases: [{ phase: "L1", cause: "no_value", entity_id: "sensor.solax_grid_current_l1", age_s: null }],
      limits_a: null,
      unavailable_entities: ["sensor.solax_grid_current_l1"],
      inverter: true,
      negative_phases: [],
    };
    const { element } = await mounted({
      get: "get_detected",
      patch: (answer: Record<string, unknown>) => {
        const config = answer["config"] as { site: Record<string, unknown> };
        return { ...answer, config: { ...config, site: { ...config.site, warnings: [warning] } } };
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    const blocking = openDialog(element)?.querySelector("[data-notices='blocking']");
    expect(blocking?.querySelector("[data-warning='measurement_unhealthy']")?.textContent).toBe(
      "The meter's sensors are unavailable (the inverter may be in standby): sensor.solax_grid_current_l1.",
    );
  });

  const withWarnings = (warnings: Array<Record<string, unknown>>) => (answer: Record<string, unknown>) => {
    const config = answer["config"] as { site: Record<string, unknown> };
    return { ...answer, config: { ...config, site: { ...config.site, warnings } } };
  };

  it("says on top that a meter reporting negative current needs Grid current is signed", async () => {
    const warning = {
      code: "measurement_unhealthy",
      integration: null,
      entity_id: null,
      interval_s: 120,
      option: null,
      device_name: null,
      phases: [
        { phase: "L2", cause: "no_value", entity_id: "sensor.pulse_l2", age_s: null },
        { phase: "L3", cause: "no_value", entity_id: "sensor.pulse_l3", age_s: null },
      ],
      limits_a: null,
      unavailable_entities: [],
      inverter: false,
      negative_phases: ["L2", "L3"],
    };
    const { element } = await mounted({ get: "get_detected", patch: withWarnings([warning]) });
    openSettings(element);
    await settle();
    edit(element, "site");
    const blocking = openDialog(element)?.querySelector("[data-notices='blocking']");
    expect(blocking?.querySelector("[data-warning='measurement_unhealthy']")?.textContent).toBe(
      "The meter reports a negative current (export) on L2 and L3 — turn on “Grid current is signed”.",
    );
    expect(translate("sv", "status.siteCurrentNegative", { phases: "L2 och L3" })).toBe(
      "Mätaren rapporterar negativ ström (export) på L2 och L3 — slå på ”Nätströmmen är teckenmärkt”.",
    );
  });

  it("lists a meter that updates too seldom for load balancing under To check, in whole minutes", async () => {
    const warning = {
      code: "meter_updates_slowly",
      integration: "easee",
      entity_id: "sensor.easee_equalizer_import_power",
      interval_s: 420,
      option: null,
      device_name: "Easee Equalizer",
      phases: [],
      limits_a: null,
      unavailable_entities: [],
      inverter: false,
      negative_phases: [],
    };
    const { element } = await mounted({ get: "get_detected", patch: withWarnings([warning]) });
    openSettings(element);
    await settle();
    edit(element, "site");
    const dialog = openDialog(element);
    expect(dialog?.querySelector("[data-notices='blocking']")).toBeNull();
    expect(dialog?.querySelector("[data-notices='site'] [data-warning='meter_updates_slowly']")?.textContent).toBe(
      "Easee Equalizer updates about every 7 min — too seldom for load balancing; solar and planning still work.",
    );
  });

  it("lists the site's notes once each, as one list, the measurement problem first", async () => {
    const note = (code: string, extra: Record<string, unknown> = {}) => ({
      code,
      integration: "tibber",
      entity_id: null,
      interval_s: 300,
      option: null,
      device_name: "Easee Equalizer",
      phases: [],
      limits_a: null,
      unavailable_entities: [],
      inverter: false,
      negative_phases: [],
      ...extra,
    });
    const warnings = [
      note("update_interval_exceeds_max_age"),
      note("update_interval_exceeds_max_age"),
      note("own_load_balancing"),
      note("measurement_unhealthy", {
        interval_s: 120,
        phases: [
          { phase: "L2", cause: "no_value", entity_id: "sensor.pulse_l2", age_s: null },
          { phase: "L3", cause: "no_value", entity_id: "sensor.pulse_l3", age_s: null },
          { phase: "L1", cause: "stale", entity_id: "sensor.pulse_l1", age_s: 400 },
        ],
      }),
    ];
    const { element } = await mounted({
      get: "get_detected",
      patch: (answer: Record<string, unknown>) => {
        const config = answer["config"] as { site: Record<string, unknown> };
        return { ...answer, config: { ...config, site: { ...config.site, warnings } } };
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    const dialog = openDialog(element);
    const blocking = dialog?.querySelector("[data-notices='blocking']");
    expect(blocking?.tagName).toBe("UL");
    expect(blocking?.querySelectorAll("li").length).toBe(1);
    // The rest is one group under "To check", after the fields and before the buttons, in a normal tone.
    const list = dialog?.querySelector("[data-notices='site']");
    expect(list?.tagName).toBe("FIELDSET");
    expect(list?.querySelector("legend")?.textContent).toBe("To check");
    expect(list?.querySelectorAll("li").length).toBe(3);
    expect(list?.querySelector("li")?.className).toBe("");
    const order = [...(list?.querySelectorAll("li") ?? [])].map((item) => item.dataset["warning"] ?? item.dataset["notice"]);
    expect(order).toEqual(["own_load_balancing", "update_interval_exceeds_max_age", "estimated"]);
    const body = dialog?.querySelector("[data-notices='site']")?.parentElement;
    expect(body?.querySelector("[data-field-block='measurement_mode']")?.compareDocumentPosition(list as Node)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(blocking?.compareDocumentPosition(list as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(blocking?.querySelector("[data-warning='measurement_unhealthy']")?.textContent).toBe(
      "L2 and L3 have no value (sensor.pulse_l2, sensor.pulse_l3). L1 is older than 120 s.",
    );
  });

  it("shows no notices for a site with nothing to say", async () => {
    const { element } = await mounted({
      patch: (answer: Record<string, unknown>) => {
        const config = answer["config"] as { site: Record<string, unknown> };
        return { ...answer, config: { ...config, site: { ...config.site, warnings: [] } } };
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    expect(openDialog(element)?.querySelector("[data-notices]")).toBeNull();
  });

  it("lists the detected meters in the site editor and applies one by id", async () => {
    const { hass, element } = await mounted({ get: "get_detected", update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const huawei = openDialog(element)?.querySelector<HTMLElement>("[data-detected-meter^='huawei_solar']");
    expect(huawei?.textContent).toContain(translate("en", "entity.detect.signed"));
    expect(huawei?.textContent).toContain(translate("en", "entity.detect.inverted"));
    expect(huawei?.textContent).toContain("6");
    const easee = openDialog(element)?.querySelector<HTMLElement>("[data-detected-meter^='easee']");
    expect(easee?.textContent).toContain(translate("en", "entity.detect.warning.own_load_balancing"));
    huawei?.querySelector<HTMLButtonElement>("[data-apply]")?.click();
    await settle();
    expect(updates(hass)).toEqual([
      {
        type: "spotnav/update_entity_config",
        api_version: 1,
        charger_id: "entry_a",
        scope: "site",
        expected: {},
        changes: { apply_detection: huawei?.dataset["detectedMeter"] },
      },
    ]);
  });

  type DetectionRows = { meters: Array<Record<string, unknown>>; batteries: Array<Record<string, unknown>> };
  const detectionOf = (answer: Record<string, unknown>): DetectionRows =>
    ((answer["config"] as { site: { detection: DetectionRows } }).site.detection);

  it("shows a meter the site already uses as In use, with no button, and a differing one with Use that applies", async () => {
    const { hass, element } = await mounted({
      get: "get_detected",
      update: "success_site",
      patch: (answer) => {
        const meters = detectionOf(answer).meters;
        meters[0]!["applied"] = true;
        return answer;
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    const dialog = openDialog(element);
    const cards = [...(dialog?.querySelectorAll<HTMLElement>("[data-detected-meter]") ?? [])];
    const inUse = cards[0];
    expect(inUse?.querySelector("button")).toBeNull();
    expect(inUse?.textContent).toContain(translate("en", "entity.detect.inUse"));
    const other = cards[1];
    const use = other?.querySelector<HTMLButtonElement>("[data-apply]");
    expect(use?.textContent).toBe(translate("en", "entity.detect.use"));
    use?.click();
    await settle();
    expect(updates(hass)[0]).toMatchObject({ changes: { apply_detection: other?.dataset["detectedMeter"] } });
  });

  it("leaves the Found block out when every candidate is what the site uses", async () => {
    const { element } = await mounted({
      get: "get_detected",
      patch: (answer) => {
        const detection = detectionOf(answer);
        for (const meter of detection.meters) {
          meter["applied"] = true;
        }
        detection.batteries = [];
        return answer;
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    expect(openDialog(element)?.querySelector("[data-detection]")).toBeNull();
    expect(openDialog(element)?.textContent).toContain("Measurement for the site.");
  });

  it("shows a battery the site already uses as In use", async () => {
    const { element } = await mounted({
      get: "get_detected",
      patch: (answer) => {
        const config = answer["config"] as { fields: Array<Record<string, unknown>> };
        const battery = detectionOf(answer).batteries[0]!;
        for (const entry of config.fields) {
          if (entry["field"] === "battery_aggregate_power_entity") {
            const ref = { entity_id: battery["entity_id"], friendly_name: "b", exists: true };
            entry["current"] = ref;
          }
          if (entry["field"] === "battery_power_inverted") {
            entry["value"] = battery["inverted"];
          }
        }
        return answer;
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    const card = openDialog(element)?.querySelector<HTMLElement>("[data-detected-battery]");
    expect(card?.querySelector("button")).toBeNull();
    expect(card?.textContent).toContain(translate("en", "entity.detect.inUse"));
    const next = openDialog(element)?.querySelectorAll<HTMLElement>("[data-detected-battery]")[1];
    expect(next?.querySelector("[data-apply]")).not.toBeNull();
  });

  it("keeps an informational detection note neutral and a note that needs a check in the warning colour", async () => {
    const { element } = await mounted({
      get: "get_detected",
      patch: (answer) => {
        detectionOf(answer).meters[0]!["warnings"] = ["voltage_from_other_device", "sign_unverified"];
        return answer;
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    const card = openDialog(element)?.querySelector<HTMLElement>("[data-detected-meter]");
    const info = card?.querySelector<HTMLElement>("[data-detect='voltage_from_other_device']");
    expect(info?.className).toBe("spotnav-entity-help");
    expect(info?.textContent).toContain("This is normal.");
    expect(card?.querySelector<HTMLElement>("[data-detect='sign_unverified']")?.className).toBe("spotnav-entity-warning");
  });

  it("puts each phase heading before the fields and says their help once after the phases", async () => {
    const { element } = await mounted({ get: "get_detected" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const phases = openDialog(element)?.querySelector<HTMLElement>("[data-part='phases']");
    const children = [...(phases?.children ?? [])] as HTMLElement[];
    expect(children.map((child) => child.dataset["phase"] ?? child.dataset["help"])).toEqual(["L1", "L2", "L3", "phases"]);
    expect(children[0]?.querySelector("legend")?.textContent).toBe("L1");
  });

  it("names each direct phase once, in its field's label, with no heading of its own", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "site");
    const phases = openDialog(element)?.querySelector<HTMLElement>("[data-part='phases']");
    const children = [...(phases?.children ?? [])] as HTMLElement[];
    expect(children.map((child) => child.dataset["phase"] ?? child.dataset["help"])).toEqual(["L1", "L2", "L3", "phases"]);
    expect(phases?.querySelector("legend")).toBeNull();
    // The picker carries the label itself; the fallback input has a label line.
    const l2 = phases?.querySelector<HTMLElement>("[data-phase='L2']");
    const picker = l2?.querySelector<HTMLElement & { label?: string }>("[data-field='direct_L2']");
    expect(`${picker?.label ?? ""} ${l2?.textContent ?? ""}`).toContain(translate("en", "entity.phase.direct", { phase: "L2" }));
  });

  it("shows a site read from one entity's attributes as what is read, and keeps it on a save that leaves it", async () => {
    const { hass, element } = await mounted({ get: "get_attributes", update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const group = openDialog(element)?.querySelector<HTMLElement>("[data-part='phases'] [data-part='phase-source']");
    expect(group?.textContent).toContain(
      "Read from Home Equalizer Current (sensor.home_equalizer_current), attributes state_currentL1, state_currentL2, state_currentL3",
    );
    expect(group?.querySelector<HTMLInputElement>("input[data-choice='source']")?.checked).toBe(true);
    // No empty pickers that would say nothing is set.
    expect(openDialog(element)?.querySelector("[data-field='direct_L1']")).toBeNull();
    type(element, "main_fuse_a", "32");
    save(element);
    await settle();
    expect(updates(hass)[0]).toMatchObject({ expected: { main_fuse_a: 25 }, changes: { main_fuse_a: 32 } });
    expect(Object.keys(updates(hass)[0]?.["changes"] as object)).toEqual(["main_fuse_a"]);
  });

  it("replaces the attribute source with three chosen entities, and needs all three", async () => {
    const { hass, element } = await mounted({ get: "get_attributes", update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    choose(element, "phase-source", "choose");
    const help = openDialog(element)?.querySelector("[data-part='phase-source'] [data-help='phases']");
    expect(help?.textContent).toContain(translate("en", "entity.help.phaseSource"));
    const pick = (name: string, value: string): void => {
      const control = field(element, name);
      if (control.tagName.toLowerCase() === "ha-selector") {
        control.dispatchEvent(new CustomEvent("value-changed", { detail: { value }, bubbles: true }));
      } else {
        type(element, name, value);
      }
    };
    pick("direct_L1", "sensor.own_l1");
    save(element);
    await settle();
    expect(updates(hass)).toHaveLength(0);
    for (const name of ["direct_L2", "direct_L3"]) {
      expect(fieldError(element, name)?.textContent, name).toBe(translate("en", "entity.error.field.required"));
    }
    pick("direct_L2", "sensor.own_l2");
    pick("direct_L3", "sensor.own_l3");
    save(element);
    await settle();
    expect(updates(hass)[0]).toMatchObject({
      expected: { direct_L1: "", direct_L2: "", direct_L3: "" },
      changes: { direct_L1: "sensor.own_l1", direct_L2: "sensor.own_l2", direct_L3: "sensor.own_l3" },
    });
  });

  it("says a stored source's three entities by name in five languages", () => {
    for (const language of ["en", "sv", "nb", "da", "fi"] as const) {
      expect(translate(language, "entity.phaseSource.entities", { entities: "A, B, C" }), language).toContain("A, B, C");
      expect(translate(language, "entity.phaseSource.attributes", { source: "Q", attributes: "x, y" }), language).toContain("Q");
    }
  });

  it("offers a checkbox per sign option and sends a changed one as a boolean", async () => {
    const { hass, element } = await mounted({ update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const box = field(element, "site_current_signed");
    expect(box.type).toBe("checkbox");
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    save(element);
    await settle();
    expect(updates(hass)[0]).toMatchObject({
      expected: { site_current_signed: false },
      changes: { site_current_signed: true },
    });
  });

  it("labels the total grid power for solar, says what it is for, and saves it as a changed field", async () => {
    const { hass, element } = await mounted({ update: "success_site" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const power = openDialog(element)?.querySelector<HTMLElement>("[data-field-block='grid_power_source_power']");
    expect(power?.textContent).toContain("Needed for solar and hybrid charging when the phases only report current.");
    expect(translate("en", "entity.field.gridPowerSource")).toBe("Total grid power (for solar)");
    // A picker when Home Assistant's selector is defined (an earlier test may have defined it), else text.
    const control = field(element, "grid_power_source_power");
    if (control.tagName.toLowerCase() === "ha-selector") {
      control.dispatchEvent(new CustomEvent("value-changed", { detail: { value: "sensor.grid_total" }, bubbles: true }));
    } else {
      type(element, "grid_power_source_power", "sensor.grid_total");
    }
    save(element);
    await settle();
    expect(updates(hass)[0]).toMatchObject({
      expected: { grid_power_source_power: "" },
      changes: { grid_power_source_power: "sensor.grid_total" },
    });
  });

  it("shows the detected total in the meter's card and the stored one in the field", async () => {
    const { element } = await mounted({
      get: "get_direct_total",
      // The detected meter is offered (not yet in use), so its card is drawn.
      patch: (answer) => {
        const meters = (answer["config"] as any).site.detection.meters;
        meters[0].applied = false;
        return answer;
      },
    });
    openSettings(element);
    await settle();
    edit(element, "site");
    expect(field(element, "grid_power_source_power").value).toBe("sensor.tibber_power");
    expect(field(element, "grid_power_source_power_export").value).toBe("sensor.tibber_power_production");
    const meter = openDialog(element)?.querySelector<HTMLElement>("[data-detected-meter]");
    expect(meter?.querySelector("[data-detect='gridPower']")?.textContent).toBe(
      translate("en", "entity.detect.gridPower"),
    );
  });
});

// The control path and its write policy, read only, drawn from the `control` block of the answer.
const POLICY_FREE = {
  min_interval_s: 0,
  max_writes_per_minute: null,
  flash_stored: false,
  regulator_writes: true,
  zero_pauses: false,
  ignored_while_paused: false,
  installation_wide: false,
  resend_after_plug_in: false,
};
const CAPABILITIES = {
  start_stop: true,
  set_current: true,
  regulated_current: true,
  reads_charging_state: true,
  reads_measured_current: false,
  reads_energy_register: false,
};

function withControl(control: Record<string, unknown>) {
  return (answer: Record<string, unknown>): Record<string, unknown> => ({
    ...answer,
    config: { ...(answer["config"] as Record<string, unknown>), control },
  });
}

const controlBlock = (element: Element): HTMLElement | null =>
  openDialog(element)?.querySelector<HTMLElement>("[data-control='path']") ?? null;

const controlRow = (element: Element, key: string): string =>
  controlBlock(element)?.querySelector(`[data-control-row="${key}"]`)?.textContent ?? "";

const restriction = (element: Element): string | null =>
  openDialog(element)?.querySelector("[data-limit-restrictions]")?.textContent ?? null;

describe("the charger editor's control path and write policy", () => {
  const wallbox = {
    platform: "wallbox",
    start_stop: { kind: "switch", entity_ids: ["switch.wb"], inverted: false, start_option: null, stop_option: null },
    current: { kind: "number", entity_id: "number.wb_limit", service: "number.set_value", enabled: true },
    charging_state: { source: "status", entity_id: "sensor.wb_status" },
    policy: { ...POLICY_FREE },
    capabilities: CAPABILITIES,
    conflicts: [],
  };
  const open = async (control: Record<string, unknown>) => {
    const { element } = await mounted({ patch: withControl(control) });
    openSettings(element);
    await settle();
    edit(element, "charger");
    return element;
  };

  it("draws no summary block of how the charger is controlled", async () => {
    const element = await open({ ...wallbox, policy: { ...POLICY_FREE, min_interval_s: 90 } });
    const dialog = openDialog(element);
    expect(dialog?.textContent).not.toContain("How SpotNav controls this charger");
    for (const key of ["start_stop", "current", "charging_state", "policy"]) {
      expect(dialog?.querySelector(`[data-control-row="${key}"]`), key).toBeNull();
    }
    expect(dialog?.textContent).not.toContain("Write limits");
    expect(dialog?.textContent).not.toContain("Charging state");
    expect(controlBlock(element)).toBeNull();
  });

  it("says nothing under the current limit for a charger whose current is written freely", async () => {
    const element = await open(wallbox);
    expect(openDialog(element)?.querySelector("[data-part='current-limit']")).not.toBeNull();
    expect(restriction(element)).toBeNull();
  });

  it("says a minimum interval of a minute or more, in minutes when whole and seconds otherwise", async () => {
    expect(restriction(await open({ ...wallbox, policy: { ...POLICY_FREE, min_interval_s: 300 } }))).toBe(
      translate("en", "control.limit.minutes", { count: "5" }),
    );
    expect(restriction(await open({ ...wallbox, policy: { ...POLICY_FREE, min_interval_s: 90 } }))).toBe(
      translate("en", "control.limit.seconds", { count: "90" }),
    );
    expect(translate("en", "control.limit.minutes", { count: "5" })).toBe("The current can change at most every 5 minutes.");
    expect(restriction(await open({ ...wallbox, policy: { ...POLICY_FREE, min_interval_s: 30 } }))).toBeNull();
  });

  it("says a stored current is only changed at a charge start", async () => {
    const element = await open({ ...wallbox, policy: { ...POLICY_FREE, flash_stored: true } });
    expect(restriction(element)).toBe("The current is stored in the charger and is only changed at a charge start.");
    const noRegulator = await open({ ...wallbox, policy: { ...POLICY_FREE, regulator_writes: false } });
    expect(restriction(noRegulator)).toBe(translate("en", "control.limit.flash"));
  });

  it("says load balancing can only stop the charge when it cannot change the current", async () => {
    const element = await open({ ...wallbox, capabilities: { ...CAPABILITIES, regulated_current: false } });
    expect(restriction(element)).toBe("Load balancing can only stop the charge, not lower the current.");
  });

  it("says the limit applies to the whole installation", async () => {
    const element = await open({ ...wallbox, policy: { ...POLICY_FREE, installation_wide: true } });
    expect(restriction(element)).toBe("The limit applies to the whole installation.");
  });

  it("combines several restrictions in one line under the current limit", async () => {
    const element = await open({
      ...wallbox,
      policy: { ...POLICY_FREE, min_interval_s: 900, flash_stored: true, regulator_writes: false, installation_wide: true },
      capabilities: { ...CAPABILITIES, regulated_current: false },
    });
    const lines = openDialog(element)?.querySelectorAll("[data-limit-restrictions]") ?? [];
    expect(lines.length).toBe(1);
    expect(lines[0]?.textContent).toBe(
      [
        translate("en", "control.limit.minutes", { count: "15" }),
        translate("en", "control.limit.flash"),
        translate("en", "control.limit.stopOnly"),
        translate("en", "control.limit.installation"),
      ].join(" "),
    );
    expect(lines[0]?.closest("[data-part='current-limit']")).not.toBeNull();
  });

  it("says nothing when SpotNav does not set the current", async () => {
    const element = await open({
      ...wallbox,
      current: { kind: "none", entity_id: null, service: null, enabled: false },
      policy: { ...POLICY_FREE, min_interval_s: 900 },
      capabilities: { ...CAPABILITIES, set_current: false, regulated_current: false },
    });
    expect(restriction(element)).toBeNull();
  });

  it("words the restrictions in every language", () => {
    for (const language of ["sv", "nb", "da", "fi"] as const) {
      for (const key of ["minutes", "seconds", "flash", "stopOnly", "installation"] as const) {
        const text = translate(language, `control.limit.${key}`, { count: "5" });
        expect(text, `${language} ${key}`).not.toBe(translate("en", `control.limit.${key}`, { count: "5" }));
        expect(text).not.toContain("{count}");
      }
    }
  });

  it("uses one heading style for Charge control, Current limit and Energy", async () => {
    const element = await open(wallbox);
    const legend = (part: string) => openDialog(element)?.querySelector(`[data-part='${part}'] > legend`);
    const control = legend("charge-control");
    expect(control?.textContent).toBe(translate("en", "entity.field.chargeControl"));
    expect(legend("current-limit")?.textContent).toBe(translate("en", "entity.field.currentLimit"));
    expect(legend("energy")?.textContent).toBe(translate("en", "entity.energy.title"));
    expect(control?.className).toBe(legend("current-limit")?.className);
    expect(control?.className).toBe(legend("energy")?.className);
    // The heading is not said a second time by a caption over the entity.
    expect(openDialog(element)?.querySelector("[data-part='charge-control'] label")).toBeNull();
  });

  it("keeps the fixed-path sentence for Easee", async () => {
    const easee = {
      ...wallbox,
      platform: "easee",
      start_stop: { kind: "easee", entity_ids: [], inverted: false, start_option: null, stop_option: null },
      current: { kind: "service", entity_id: null, service: "easee.set_charger_dynamic_limit", enabled: true },
      policy: { ...POLICY_FREE, max_writes_per_minute: 20, resend_after_plug_in: true },
    };
    const { element } = await mounted({ patch: withControl(easee) });
    openSettings(element);
    await settle();
    edit(element, "charger");

    expect(controlRow(element, "start_stop_fixed")).toBe(translate("en", "control.startStop.easeeFixed"));
  });

  it("says a current the person has not allowed is off, and warns about the charger's own mode", async () => {
    const off = {
      ...wallbox,
      current: { kind: "number", entity_id: "number.wb_limit", service: "number.set_value", enabled: false },
      conflicts: [
        { kind: "own_mode", entity_id: "switch.wb_solar", label: "solar divert", state: "on" },
        { kind: "disabled", entity_id: "switch.wb_enabled", label: "enabled", state: "off" },
      ],
    };
    const { element } = await mounted({ patch: withControl(off) });
    openSettings(element);
    await settle();
    edit(element, "charger");

    const warning = controlBlock(element)?.querySelector("[data-conflict='switch.wb_solar']");
    expect(warning?.textContent).toBe(
      translate("en", "control.conflict", { label: "solar divert", name: "switch.wb_solar" }),
    );
    // The charger's own enable switch being off has its own sentence, not the "it can fight SpotNav" one.
    const disabled = controlBlock(element)?.querySelector("[data-conflict='switch.wb_enabled']");
    expect(disabled?.textContent).toBe(translate("en", "control.disabled", { name: "switch.wb_enabled" }));
    expect(disabled?.textContent).toBe("The charger's own enable switch is off (switch.wb_enabled). SpotNav cannot start it: turn it on.");
  });

  it("words a second SpotNav entry on the same charger, naming it", async () => {
    const twice = {
      ...wallbox,
      conflicts: [{ kind: "duplicate_charger", entity_id: "ocpp:garage:1", label: "ocpp", state: "Garage Easee" }],
    };
    const { element } = await mounted({ patch: withControl(twice) });
    openSettings(element);
    await settle();
    edit(element, "charger");

    const warning = controlBlock(element)?.querySelector("[data-conflict='ocpp:garage:1']");
    expect(warning?.textContent).toBe(translate("en", "issue.duplicateCharger", { other: "Garage Easee" }));
    expect(warning?.textContent).toContain("Garage Easee and this charger are the same physical charger.");
    expect(translate("sv", "issue.duplicateCharger", { other: "Garage Easee" })).toContain(
      "Garage Easee och den här laddaren är samma fysiska laddare.",
    );
  });

  it("words another controller that also controls chargers, in all five languages", async () => {
    const other = {
      ...wallbox,
      conflicts: [{ kind: "other_controller", entity_id: "switch.wb_control", label: "EV Smart Charging", state: "charger" }],
    };
    const { element } = await mounted({ patch: withControl(other) });
    openSettings(element);
    await settle();
    edit(element, "charger");

    const warning = controlBlock(element)?.querySelector("[data-conflict='switch.wb_control']");
    expect(warning?.textContent).toBe(
      "EV Smart Charging also controls chargers; turn it off for this charger or SpotNav and EV Smart Charging will fight.",
    );
    for (const language of ["en", "sv", "nb", "da", "fi"] as const) {
      const text = translate(language, "control.otherController", { name: "EV Smart Charging" });
      expect(text).not.toContain("{name}");
      expect(text.split("EV Smart Charging")).toHaveLength(3);
    }
  });

  it("draws nothing when the charger is not loaded", async () => {
    const { element } = await mounted({
      patch: (answer) => ({ ...answer, config: { ...(answer["config"] as Record<string, unknown>), control: null } }),
    });
    openSettings(element);
    await settle();
    edit(element, "charger");

    expect(controlBlock(element)).toBeNull();
  });
});

describe("the external balancer warning", () => {
  const balancerWarning = {
    code: "external_current_balancer",
    integration: "perific",
    entity_id: null,
    interval_s: null,
    option: null,
    device_name: "Zaptec",
    phases: [],
    limits_a: null,
      unavailable_entities: [],
      inverter: false,
      negative_phases: [],
  };
  const withSiteWarnings = (warnings: Array<Record<string, unknown>>) => (answer: Record<string, unknown>) => {
    const config = answer["config"] as { site: Record<string, unknown> };
    return { ...answer, config: { ...config, site: { ...config.site, warnings } } };
  };

  it("shows the external balancer warning in the Site card and the Site dialog, worded", async () => {
    const { element } = await mounted({ patch: withSiteWarnings([balancerWarning]) });
    openSettings(element);
    await settle();
    const expected = translate("en", "entity.warning.externalBalancer", { name: "Zaptec", integration: "perific" });
    expect(shadow(element).textContent).toContain(expected);
    expect(translate("en", "entity.warning.externalBalancer", { name: "Zaptec", integration: "perific" })).toBe(
      "perific balances the current of Zaptec itself, so SpotNav starts and stops the charger but does not write its current.",
    );
    edit(element, "site");
    const row = openDialog(element)?.querySelector("[data-notices='site'] [data-warning='external_current_balancer']");
    expect(row?.textContent).toBe(expected);
    expect(row?.tagName).toBe("LI");
  });

  it("lets no raw code reach the text, and words an unknown warning code", async () => {
    const unknown = { ...balancerWarning, code: "brand_new_code" };
    const { element } = await mounted({ patch: withSiteWarnings([balancerWarning, unknown]) });
    openSettings(element);
    await settle();
    edit(element, "site");
    const dialog = openDialog(element);
    expect(dialog?.querySelector("[data-warning='brand_new_code']")?.textContent).toBe(translate("en", "entity.warning.unknown"));
    for (const raw of ["external_current_balancer", "brand_new_code", "number_pause", "external_balancer"]) {
      expect(dialog?.textContent).not.toContain(raw);
    }
  });
});

describe("the battery's grid import limit warning: two limits on one fuse", () => {
  const limitWarning = {
    code: "battery_import_limit_differs",
    integration: "sigen",
    entity_id: "number.sigen_plant_grid_import_limitation",
    interval_s: null,
    option: null,
    device_name: null,
    phases: [],
    limits_a: { battery: 15.9, spotnav: 24 },
    unavailable_entities: [],
    inverter: false,
    negative_phases: [],
  };
  const withSiteWarnings = (warnings: Array<Record<string, unknown>>) => (answer: Record<string, unknown>) => {
    const config = answer["config"] as { site: Record<string, unknown> };
    return { ...answer, config: { ...config, site: { ...config.site, warnings } } };
  };

  it("is worded with both limits in the Site dialog", async () => {
    const { element } = await mounted({ patch: withSiteWarnings([limitWarning]) });
    openSettings(element);
    await settle();
    edit(element, "site");
    const row = openDialog(element)?.querySelector("[data-notices='site'] [data-warning='battery_import_limit_differs']");
    expect(row?.textContent).toBe(
      "Two limits on one fuse: the battery's grid import limit (sigen) is 15.9 A per phase, SpotNav's is 24 A. Set them to the same value.",
    );
  });

  it("is worded in every language, with the two figures", () => {
    for (const language of ["sv", "nb", "da", "fi"] as const) {
      const text = translate(language, "entity.warning.batteryImportLimit", { integration: "sigen", battery: "15.9", spotnav: "24.0" });
      expect(text, language).toContain("15.9");
      expect(text, language).toContain("24.0");
      expect(text, language).not.toBe(translate("en", "entity.warning.batteryImportLimit", { integration: "sigen", battery: "15.9", spotnav: "24.0" }));
    }
  });
});
