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

    expect(sectionRows(element, "entities")).toEqual(["start_stop", "current", "energy_register"]);
    expect(rowText(element, "start_stop")).toContain(translate("en", "control.startStop"));
    expect(rowText(element, "start_stop")).toContain("Control get_direct");
    expect(rowText(element, "current")).toContain(translate("en", "control.current.none"));
    // Nothing chosen and nothing found: the energy meter is said to be found automatically.
    expect(rowText(element, "energy_register")).toContain(translate("en", "entity.foundAutomatically"));
    // One Change button, no sub-heading, no vehicle level among the charger's rows.
    expect(openDialog(element)?.querySelectorAll("[data-section='entities'] [data-edit-entities]")).toHaveLength(1);
    expect(openDialog(element)?.querySelector("[data-section='entities'] [data-row='vehicle_soc']")).toBeNull();
    expect(openDialog(element)?.querySelector("[data-section='entities'] h4")?.textContent).toBe(
      translate("en", "settings.section.entities"),
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
    expect(rowText(element, "battery")).toContain("Home battery");
    expect(openDialog(element)?.querySelector("[data-help-for]")).toBeNull();
    expect(openDialog(element)?.querySelector(".spotnav-entity-help-toggle")).toBeNull();
  });

  it("asks a non-administrator nothing, says only administrators can change entities, and disables the buttons", async () => {
    const { hass, element } = await mounted({ admin: false });
    openSettings(element);
    await settle();
    expect(gets(hass)).toHaveLength(0);
    const section = openDialog(element)?.querySelector<HTMLElement>("[data-section='entities']");
    expect(section?.textContent).toContain(translate("en", "entity.adminOnly"));
    const buttons = Array.from(openDialog(element)?.querySelectorAll<HTMLButtonElement>("[data-edit-entities]") ?? []);
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons.every((button) => button.disabled)).toBe(true);
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
    const button = openDialog(element)?.querySelector<HTMLButtonElement>("[data-section='entities'] [data-edit-entities]");
    expect(button?.disabled).toBe(true);
  });

  it("shows only the charger's card and no site rows when the charger has no site", async () => {
    const { element } = await mounted({ dashboard: "cheapest_no_site", get: "get_no_site" });
    openSettings(element);
    await settle();
    expect(sectionRows(element, "entities")).toHaveLength(3);
    const site = openDialog(element)?.querySelector("[data-section='site']");
    expect(site?.textContent).toContain(translate("en", "site.none"));
    expect(site?.querySelector("[data-row]")).toBeNull();
    expect(openDialog(element)?.querySelector("[data-section='solar']")).toBeNull();
  });

  it("names the site by its name alone, and by the word Site only when it has none", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    const heading = openDialog(element)?.querySelector('[data-section="site"] h4')?.textContent ?? "";
    expect(heading).toBe((dashboardFixture("cheapest_direct_site_admin")["site"] as { name: string }).name);

    document.body.innerHTML = "";
    const again = await mounted({
      patchDashboard: (payload) => {
        (payload["site"] as Record<string, unknown>)["name"] = "";
      },
    });
    openSettings(again.element);
    await settle();
    expect(openDialog(again.element)?.querySelector('[data-section="site"] h4')?.textContent).toBe(
      translate("en", "settings.section.site"),
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
    expect(openDialog(element)?.textContent).toContain(translate("en", "entity.editor.charger"));
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
    // Per phase the two required sources first, then the optional ones inside "more sources".
    expect(names.filter((name) => name?.startsWith("derived_"))).toEqual(
      ["L1", "L2", "L3"].flatMap((phase) =>
        ["power", "voltage", "power_export", "reactive_power", "apparent_power", "current"].map(
          (kind) => `derived_${phase}_${kind}`,
        ),
      ),
    );
    expect(dialog?.querySelector("[data-optional-sources='L1']")).not.toBeNull();
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
    expect(rowText(element, "power_entity")).toContain("Garage plug power");
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
    expect(register).toContain("Automatic: halo_charger Connector 1 Energy Active Import Register");
    expect(register).not.toContain(translate("en", "entity.notSet"));
  });

  it("stacks that long value under its label, with the label kept whole", async () => {
    const { element } = await mounted({ patch: ownersInstallation });
    openSettings(element);
    await settle();
    const row = openDialog(element)?.querySelector("[data-row=\"energy_register\"]");
    expect(row?.querySelector(".spotnav-settings-value")?.className).toContain("spotnav-settings-value-long");
    expect(row?.firstElementChild?.className).toContain("spotnav-capability-label");
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
    expect(block("current_limit")?.textContent).toContain(translate("en", "entity.help.currentLimit"));
    expect(block("current_limit")?.textContent).toContain("Automatic: halo_charger Connector 1 Session Current Limit");
    expect(block("energy_register_entity")?.textContent).toContain(
      "Automatic: halo_charger Connector 1 Energy Active Import Register",
    );
    expect(block("energy_register_entity")?.textContent).toContain(translate("en", "entity.help.energyRegister"));
    expect(block("charge_control")?.querySelector("[data-missing]")).toBeNull();
    expect(block("charge_control")?.textContent).toContain(translate("en", "entity.help.chargeControl"));
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

  it("lists the site's notes once each, as one list, the measurement problem first", async () => {
    const note = (code: string, extra: Record<string, unknown> = {}) => ({
      code,
      integration: "tibber",
      entity_id: null,
      interval_s: 300,
      option: null,
      device_name: "Easee Equalizer",
      phases: [],
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
    const list = openDialog(element)?.querySelector("[data-notices='site']");
    expect(list?.tagName).toBe("UL");
    expect(list?.querySelectorAll("li").length).toBe(4);
    const order = [...(list?.querySelectorAll("li") ?? [])].map((item) => item.dataset["warning"] ?? item.dataset["notice"]);
    expect(order).toEqual(["measurement_unhealthy", "own_load_balancing", "update_interval_exceeds_max_age", "estimated"]);
    expect(list?.querySelector("[data-warning='measurement_unhealthy']")?.textContent).toBe(
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
    expect(openDialog(element)?.querySelector("[data-notices='site']")).toBeNull();
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

  it("shows the total grid power in direct mode and hides it in derived mode, where the sign option stays", async () => {
    const { element } = await mounted();
    openSettings(element);
    await settle();
    edit(element, "site");
    const block = (name: string) => openDialog(element)?.querySelector<HTMLElement>(`[data-field-block='${name}']`);
    expect(block("grid_power_source_power")?.hidden).toBe(false);
    expect(block("grid_power_source_power_export")?.hidden).toBe(false);
    // The sign applies to the total in direct mode as to the per-phase power in derived mode.
    expect(block("grid_power_inverted")?.hidden).toBe(false);
    const derived = openDialog(element)?.querySelector<HTMLInputElement>("input[type='radio'][data-mode='derived_phase_current']");
    derived!.checked = true;
    derived!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(block("grid_power_source_power")?.hidden).toBe(true);
    expect(block("grid_power_source_power_export")?.hidden).toBe(true);
    expect(block("grid_power_inverted")?.hidden).toBe(false);
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


  it("keeps the optional sources folded unless one is set", async () => {
    const { element } = await mounted({ get: "get_detected" });
    openSettings(element);
    await settle();
    edit(element, "site");
    const more = openDialog(element)?.querySelector<HTMLDetailsElement>("[data-optional-sources='L1']");
    expect(more?.open).toBe(false);
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

describe("the charger editor's control path and write policy", () => {
  const wallbox = {
    platform: "wallbox",
    start_stop: { kind: "switch", entity_ids: ["switch.wb"], inverted: false, start_option: null, stop_option: null },
    current: { kind: "number", entity_id: "number.wb_limit", service: "number.set_value", enabled: true },
    charging_state: { source: "status", entity_id: "sensor.wb_status" },
    policy: { ...POLICY_FREE, min_interval_s: 90 },
    capabilities: CAPABILITIES,
    conflicts: [],
  };

  it("says how the charger is controlled and the limit every write obeys", async () => {
    const { element } = await mounted({ patch: withControl(wallbox) });
    openSettings(element);
    await settle();
    edit(element, "charger");

    expect(controlRow(element, "start_stop")).toContain(translate("en", "control.startStop.switch"));
    expect(controlRow(element, "current")).toContain(translate("en", "control.current.number", { name: "number.wb_limit" }));
    expect(controlRow(element, "charging_state")).toContain(translate("en", "control.state.status"));
    expect(controlRow(element, "policy")).toContain(translate("en", "control.policy.interval", { seconds: "90" }));
    expect(controlBlock(element)?.textContent).toContain(translate("en", "control.regulated.yes"));
    expect(controlBlock(element)?.querySelector("[data-conflict]")).toBeNull();
  });

  it("names a stored setting as written at a session start only, and says load balancing can only stop", async () => {
    const stored = {
      ...wallbox,
      start_stop: { kind: "select", entity_ids: ["select.garo"], inverted: false, start_option: "ALWAYS_ON", stop_option: "ALWAYS_OFF" },
      policy: { ...POLICY_FREE, min_interval_s: 60, flash_stored: true, regulator_writes: false },
      capabilities: { ...CAPABILITIES, regulated_current: false },
    };
    const { element } = await mounted({ patch: withControl(stored) });
    openSettings(element);
    await settle();
    edit(element, "charger");

    expect(controlRow(element, "start_stop")).toContain(
      translate("en", "control.startStop.select", { start: "ALWAYS_ON", stop: "ALWAYS_OFF" }),
    );
    expect(controlRow(element, "policy")).toContain(translate("en", "control.policy.flash"));
    expect(controlBlock(element)?.textContent).toContain(translate("en", "control.regulated.no"));
  });

  it("shows Easee's service path with its per-minute budget and the plug-in resend", async () => {
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

    expect(controlRow(element, "start_stop")).toContain(translate("en", "control.startStop.easee"));
    expect(controlRow(element, "start_stop_fixed")).toBe(translate("en", "control.startStop.easeeFixed"));
    expect(controlRow(element, "current")).toContain(translate("en", "control.current.service"));
    expect(controlRow(element, "policy")).toContain(translate("en", "control.policy.perMinute", { count: "20" }));
    expect(controlRow(element, "policy")).toContain(translate("en", "control.policy.resend"));
  });

  it("says when SpotNav does not set the current, and draws no write limits for it", async () => {
    const startStopOnly = {
      ...wallbox,
      current: { kind: "none", entity_id: null, service: null, enabled: false },
      capabilities: { ...CAPABILITIES, set_current: false, regulated_current: false },
    };
    const { element } = await mounted({ patch: withControl(startStopOnly) });
    openSettings(element);
    await settle();
    edit(element, "charger");

    expect(controlRow(element, "current")).toContain(translate("en", "control.current.none"));
    expect(controlBlock(element)?.querySelector("[data-control-row='policy']")).toBeNull();
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

    expect(controlRow(element, "current")).toContain(translate("en", "control.current.off"));
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

describe("the charger audit's start and stop kinds and the external balancer warning", () => {
  const abb = {
    platform: "abb",
    start_stop: { kind: "number_pause", entity_ids: ["button.abb_start", "number.abb_current"], inverted: false, start_option: null, stop_option: null },
    current: { kind: "number", entity_id: "number.abb_current", service: "number.set_value", enabled: true },
    charging_state: { source: "status", entity_id: "sensor.abb_status" },
    policy: { ...POLICY_FREE, min_interval_s: 60 },
    capabilities: CAPABILITIES,
    conflicts: [],
  };
  const balancerWarning = {
    code: "external_current_balancer",
    integration: "perific",
    entity_id: null,
    interval_s: null,
    option: null,
    device_name: "Zaptec",
    phases: [],
  };
  const withSiteWarnings = (warnings: Array<Record<string, unknown>>) => (answer: Record<string, unknown>) => {
    const config = answer["config"] as { site: Record<string, unknown> };
    return { ...answer, config: { ...config, site: { ...config.site, warnings } } };
  };

  it("words number_pause, and any kind the card does not know, in every language", async () => {
    const { element } = await mounted({ patch: withControl(abb) });
    openSettings(element);
    await settle();
    edit(element, "charger");
    expect(controlRow(element, "start_stop")).toContain("Through the current limit: 0 A pauses, the planned current resumes");
    expect(controlRow(element, "start_stop")).not.toContain(translate("en", "control.startStop.switch"));

    const unknown = { ...abb, start_stop: { ...abb.start_stop, kind: "from_the_future" } };
    const later = await mounted({ patch: withControl(unknown) });
    openSettings(later.element);
    await settle();
    edit(later.element, "charger");
    expect(controlRow(later.element, "start_stop")).toContain(translate("en", "control.startStop.other"));
    expect(controlRow(later.element, "start_stop")).not.toContain("from_the_future");
    expect(translate("sv", "control.startStop.numberPause")).toBe(
      "Via strömgränsen: 0 A pausar, planerad ström återupptar",
    );
  });

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
    expect(row?.className).toBe("spotnav-entity-warning");
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
