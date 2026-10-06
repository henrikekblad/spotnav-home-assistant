// The entity editors' choose-the-type-then-its-fields groups: grid power, where the current is taken
// from, battery and the charger's energy. Drawn straight from the backend's committed fixtures, with
// what a Save would send judged by `entityChange` (the same call the card makes).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { entityChange, decodeEntityAnswer, type EntityConfig, type EntityScope } from "../src/entity-config";
import { entityEditorBody } from "../src/entity-editor";
import { LANGUAGES, translate, type Language } from "../src/i18n";

const DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");

type Patch = Record<string, string | boolean>;

/** A fixture's config with the named fields set: entity ids by name, flags as booleans, mode as text. */
function config(name: string, patch: Patch = {}): EntityConfig {
  const raw = JSON.parse(readFileSync(join(DIR, `${name}.json`), "utf8")) as { config: { fields: Array<Record<string, unknown>> } };
  const fields = raw.config.fields;
  for (const [field, value] of Object.entries(patch)) {
    let entry = fields.find((item) => item["field"] === field);
    if (entry === undefined) {
      entry = {
        field,
        kind: "entity",
        scope: "site",
        required: false,
        writable: true,
        current: null,
        effective: null,
        allowed_domains: ["sensor"],
        allowed_device_classes: [],
      };
      fields.push(entry);
    }
    if (typeof value === "boolean" || entry["kind"] === "flag") {
      entry["value"] = value;
    } else if (entry["kind"] === "enum") {
      entry["value"] = value;
    } else if (entry["kind"] === "entity") {
      entry["current"] = value === "" ? null : { entity_id: value, friendly_name: value, exists: true };
    }
  }
  const decoded = decodeEntityAnswer(raw);
  if (!decoded.ok || !decoded.value.ok) {
    throw new Error("fixture does not decode");
  }
  return decoded.value.config;
}

const DERIVED = { measurement_mode: "derived_phase_current" };

function open(cfg: EntityConfig, scope: EntityScope, options: { language?: Language; readOnly?: boolean } = {}) {
  const saved: Array<Record<string, string>> = [];
  const built = entityEditorBody(
    document,
    options.language ?? "en",
    { scope, config: cfg, hass: () => undefined, appliesText: null, readOnly: options.readOnly ?? false },
    { onSave: (draft) => saved.push(draft), onCancel: () => undefined },
    "t",
  );
  document.body.replaceChildren(built.body);
  const sent = () => entityChange(cfg, scope, built.draft());
  return { built, saved, sent };
}

const choice = (part: string, value: string): HTMLInputElement | null =>
  document.querySelector<HTMLInputElement>(`[data-part='${part}'] input[data-choice='${value}']`);

function pick(part: string, value: string): void {
  const radio = choice(part, value);
  if (radio === null) {
    throw new Error(`no ${part}/${value}`);
  }
  radio.checked = true;
  radio.dispatchEvent(new Event("change", { bubbles: true }));
}

const has = (field: string): boolean => document.querySelector(`[data-field='${field}']`) !== null;
const checked = (part: string): string | undefined =>
  Array.from(document.querySelectorAll<HTMLInputElement>(`[data-part='${part}'] input[data-choice]`)).find((r) => r.checked)?.dataset["choice"];
const note = (part: string): HTMLElement | null => document.querySelector<HTMLElement>(`[data-choice-note='${part}']`);
const noteShown = (part: string): boolean => note(part) !== null && !note(part)!.hidden;

function changes(result: ReturnType<ReturnType<typeof open>["sent"]>): Record<string, unknown> {
  if (!result.ok || !result.changed) {
    return {};
  }
  return result.request.changes;
}

describe("grid power: one sensor with direction, or import and export as two", () => {
  it("in direct mode shows the total, adds the export for two sensors and keeps the sign under one", () => {
    open(config("get_direct"), "site");
    expect(checked("grid")).toBe("one");
    expect(has("grid_power_source_power")).toBe(true);
    expect(has("grid_power_source_power_export")).toBe(false);
    expect(has("grid_power_inverted")).toBe(true);
    pick("grid", "two");
    expect(has("grid_power_source_power_export")).toBe(true);
    expect(has("grid_power_inverted")).toBe(false);
    pick("grid", "one");
    expect(has("grid_power_source_power_export")).toBe(false);
    expect(has("grid_power_inverted")).toBe(true);
  });

  it("in derived mode puts the export on each phase for two sensors, and the sign under one", () => {
    open(config("get_derived"), "site");
    expect(checked("grid")).toBe("one");
    expect(has("derived_L1_power_export")).toBe(false);
    expect(has("grid_power_source_power")).toBe(false);
    expect(has("grid_power_inverted")).toBe(true);
    pick("grid", "two");
    for (const phase of ["L1", "L2", "L3"]) {
      expect(has(`derived_${phase}_power_export`)).toBe(true);
    }
    expect(has("grid_power_inverted")).toBe(false);
  });

  it("opens on two sensors when an export is stored, and a save with one sensor clears it", () => {
    const { sent } = open(config("get_direct_total"), "site");
    expect(checked("grid")).toBe("two");
    expect(noteShown("grid")).toBe(false);
    pick("grid", "one");
    expect(changes(sent())).toEqual({ grid_power_source_power_export: "" });
  });

  it("clears the sign when two sensors are saved, and says so when both were stored", () => {
    const { sent } = open(config("get_direct_total", { grid_power_inverted: true }), "site");
    expect(checked("grid")).toBe("two");
    expect(noteShown("grid")).toBe(true);
    expect(changes(sent())).toEqual({ grid_power_inverted: false });
  });

  it("clears each phase's export in derived mode when one sensor is chosen", () => {
    const { sent } = open(config("get_derived", { ...DERIVED, derived_L2_power_export: "sensor.l2_export" }), "site");
    expect(checked("grid")).toBe("two");
    pick("grid", "one");
    expect(changes(sent())).toEqual({ derived_L2_power_export: "" });
  });
});

describe("two sensors are never saved incomplete", () => {
  const submit = (): void => {
    document.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
  };
  const errorOf = (field: string): HTMLElement | null => document.querySelector<HTMLElement>(`[data-field-error='${field}']`);

  it("refuses a derived save with empty exports, shows the error on each and keeps the sign flag", () => {
    const { saved } = open(config("get_derived", { grid_power_inverted: true }), "site");
    pick("grid", "two");
    submit();
    expect(saved).toEqual([]);
    for (const phase of ["L1", "L2", "L3"]) {
      expect(errorOf(`derived_${phase}_power_export`)!.hidden).toBe(false);
      expect(errorOf(`derived_${phase}_power_export`)!.dataset["code"]).toBe("required");
    }
  });

  it("refuses an export on some phases only, and names the phases without one", () => {
    const { saved } = open(config("get_derived", { derived_L2_power_export: "sensor.l2_export" }), "site");
    pick("grid", "two");
    submit();
    expect(saved).toEqual([]);
    expect(errorOf("derived_L1_power_export")!.hidden).toBe(false);
    expect(errorOf("derived_L2_power_export")!.hidden).toBe(true);
    expect(errorOf("derived_L3_power_export")!.hidden).toBe(false);
  });

  it("refuses a direct save with no export total", () => {
    const { saved } = open(config("get_direct", { grid_power_inverted: true }), "site");
    pick("grid", "two");
    submit();
    expect(saved).toEqual([]);
    expect(errorOf("grid_power_source_power_export")!.hidden).toBe(false);
  });

  it("saves a complete pair and clears the sign", () => {
    const exports = { derived_L1_power_export: "sensor.e1", derived_L2_power_export: "sensor.e2", derived_L3_power_export: "sensor.e3" };
    const { saved } = open(config("get_derived", { ...exports, grid_power_inverted: true }), "site");
    expect(checked("grid")).toBe("two");
    submit();
    expect(saved).toHaveLength(1);
    expect(saved[0]!["grid_power_inverted"]).toBe("false");
  });

  it("says in derived mode that a meter without export per phase keeps one sensor", () => {
    open(config("get_derived"), "site");
    pick("grid", "two");
    expect(document.querySelector("[data-help='grid-phases']")!.textContent).toContain(translate("en", "entity.help.derivedTwoSensors"));
  });
});

describe("a detected sign that disagrees with the stored one", () => {
  const solax = (powerInverted: boolean) => ({
    id: "m1",
    integration: "solax_modbus",
    title: "SolaX",
    mode: "derived_phase_current",
    confidence: "high" as const,
    currentSigned: false,
    powerInverted,
    estimated: false,
    disabledEntities: [],
    entities: ["L1", "L2", "L3"].map((phase) => ({
      role: "power",
      phase,
      entityId: `sensor.p_${phase.toLowerCase()}`,
      friendlyName: phase,
      disabled: false,
    })),
    warnings: [],
    applied: true,
  });
  const powers = { derived_L1_power: "sensor.p_l1", derived_L2_power: "sensor.p_l2", derived_L3_power: "sensor.p_l3" };
  const notice = (): HTMLElement | null => document.querySelector<HTMLElement>("[data-sign-notice='grid']");

  it("warns, dismissibly, when the meter reports export as positive and the flag is off", () => {
    const cfg = config("get_derived", { ...powers, grid_power_inverted: false });
    cfg.site!.meters = [solax(true)];
    open(cfg, "site");
    expect(notice()!.hidden).toBe(false);
    expect(notice()!.textContent).toContain("SolaX reports export as positive");
    notice()!.querySelector("button")!.click();
    expect(notice()!.hidden).toBe(true);
  });

  it("goes away when the flag is turned on, and warns the other way round", () => {
    const cfg = config("get_derived", { ...powers, grid_power_inverted: false });
    cfg.site!.meters = [solax(true)];
    open(cfg, "site");
    const box = document.querySelector<HTMLInputElement>("input[data-field='grid_power_inverted']")!;
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    expect(notice()!.hidden).toBe(true);

    const reverse = config("get_derived", { ...powers, grid_power_inverted: true });
    reverse.site!.meters = [solax(false)];
    open(reverse, "site");
    expect(notice()!.hidden).toBe(false);
    expect(notice()!.dataset["want"]).toBe("off");
  });

  it("stays silent when the entities are not the detected meter's or the sign agrees", () => {
    const other = config("get_derived", { grid_power_inverted: false });
    other.site!.meters = [solax(true)];
    open(other, "site");
    expect(notice()!.hidden).toBe(true);
    const agree = config("get_derived", { ...powers, grid_power_inverted: true });
    agree.site!.meters = [solax(true)];
    open(agree, "site");
    expect(notice()!.hidden).toBe(true);
  });
});

describe("current is taken from: one kind for every phase", () => {
  it("is estimated when nothing is stored, says it is marked in the card, and shows no current fields", () => {
    open(config("get_derived"), "site");
    expect(checked("current-source")).toBe("estimated");
    expect(document.querySelector("[data-help='current-estimated']")).not.toBeNull();
    for (const kind of ["current", "apparent_power", "reactive_power"]) {
      expect(has(`derived_L1_${kind}`)).toBe(false);
    }
  });

  it("shows only the chosen kind's field on every phase", () => {
    open(config("get_derived"), "site");
    const all = ["current", "apparent_power", "reactive_power"];
    for (const [value, kind] of [["measured", "current"], ["apparent", "apparent_power"], ["reactive", "reactive_power"]] as const) {
      pick("current-source", value);
      for (const phase of ["L1", "L2", "L3"]) {
        for (const other of all) {
          expect(has(`derived_${phase}_${other}`), `${value} ${phase} ${other}`).toBe(other === kind);
        }
      }
      expect(document.querySelector("[data-help='current-estimated']")).toBeNull();
    }
    pick("current-source", "estimated");
    expect(document.querySelector("[data-help='current-estimated']")).not.toBeNull();
    expect(has("derived_L1_apparent_power")).toBe(false);
  });

  it("opens on the kind in use, whichever phase has it", () => {
    open(config("get_derived", { derived_L3_reactive_power: "sensor.l3_q" }), "site");
    expect(checked("current-source")).toBe("reactive");
    expect(noteShown("current-source")).toBe(false);
    expect(has("derived_L1_reactive_power")).toBe(true);
  });

  it("opens a mixed state on the kind in precedence, notes it, and a save clears the others", () => {
    const { sent } = open(
      config("get_derived", {
        derived_L1_current: "sensor.l1_i",
        derived_L2_apparent_power: "sensor.l2_s",
        derived_L3_reactive_power: "sensor.l3_q",
      }),
      "site",
    );
    expect(checked("current-source")).toBe("measured");
    expect(noteShown("current-source")).toBe(true);
    expect(note("current-source")?.textContent).toBe(translate("en", "entity.choice.mixed"));
    expect(changes(sent())).toEqual({ derived_L2_apparent_power: "", derived_L3_reactive_power: "" });
  });

  it("clears a stored kind when estimated is chosen, and keeps what was typed when switching back", () => {
    const { sent } = open(config("get_derived", { derived_L1_current: "sensor.l1_i" }), "site");
    expect(checked("current-source")).toBe("measured");
    pick("current-source", "estimated");
    expect(changes(sent())).toEqual({ derived_L1_current: "" });
    pick("current-source", "measured");
    expect(changes(sent())).toEqual({});
  });

  it("is not offered in direct mode", () => {
    open(config("get_direct"), "site");
    expect(document.querySelector("[data-part='current-source']")).toBeNull();
    expect(has("site_current_signed")).toBe(true);
  });

  it("appears and disappears with the measurement mode, and the grid choice follows the mode's fields", () => {
    open(config("get_direct_total"), "site");
    expect(checked("grid")).toBe("two");
    const derived = document.querySelector<HTMLInputElement>("input[data-mode='derived_phase_current']")!;
    derived.checked = true;
    derived.dispatchEvent(new Event("change", { bubbles: true }));
    expect(document.querySelector("[data-part='current-source']")).not.toBeNull();
    expect(checked("grid")).toBe("one");
    expect(has("grid_power_source_power")).toBe(false);
  });
});

describe("help under each choice, and where the sign of the current applies", () => {
  const helpIn = (part: string): string[] =>
    Array.from(document.querySelectorAll<HTMLElement>(
        `[data-part='${part}'] [data-help='grid-phases'], [data-part='${part}'] [data-help^='current-']`,
      )).map((node) => node.textContent ?? "");

  it("says the power and voltage once under Grid power and the current's help once under its choice, not per phase", () => {
    open(config("get_derived", { derived_L1_apparent_power: "sensor.l1_s" }), "site");
    expect(helpIn("grid")).toEqual([
      `${translate("en", "entity.help.derivedPower")} ${translate("en", "entity.help.derivedVoltage")}`,
    ]);
    expect(helpIn("current-source")).toEqual([translate("en", "entity.help.derivedApparentPower")]);
    expect(document.querySelector("[data-help='phases']")?.children.length).toBe(0);
    pick("current-source", "estimated");
    expect(helpIn("current-source")).toEqual([translate("en", "entity.current.estimatedNote")]);
    pick("grid", "two");
    expect(helpIn("grid")[0]).toContain(translate("en", "entity.help.derivedPowerExport"));
  });

  it("keeps the phase help in direct mode", () => {
    open(config("get_direct"), "site");
    expect(document.querySelector("[data-help='phases']")?.textContent).toBe(translate("en", "entity.help.phaseDirect"));
  });

  it("shows the signed current in direct mode, and in derived mode only for the meter's own current", () => {
    open(config("get_direct"), "site");
    expect(has("site_current_signed")).toBe(true);
    document.body.replaceChildren();
    const { sent } = open(config("get_derived", { derived_L1_current: "sensor.l1_i", site_current_signed: true }), "site");
    expect(checked("current-source")).toBe("measured");
    expect(has("site_current_signed")).toBe(true);
    expect(changes(sent())).toEqual({});
    pick("current-source", "apparent");
    expect(has("site_current_signed")).toBe(false);
    pick("current-source", "estimated");
    expect(has("site_current_signed")).toBe(false);
    expect(changes(sent())).toEqual({ derived_L1_current: "", site_current_signed: false });
    pick("current-source", "measured");
    expect(has("site_current_signed")).toBe(true);
  });
});

describe("battery: none, one sensor, or charging and discharging as two", () => {
  it("is none by default, and shows the fields of the chosen variant only", () => {
    open(config("get_direct"), "site");
    expect(checked("battery")).toBe("none");
    for (const field of ["battery_aggregate_power_entity", "battery_discharge_power_entity", "battery_power_inverted"]) {
      expect(has(field)).toBe(false);
    }
    pick("battery", "one");
    expect(has("battery_aggregate_power_entity")).toBe(true);
    expect(has("battery_power_inverted")).toBe(true);
    expect(has("battery_discharge_power_entity")).toBe(false);
    pick("battery", "two");
    expect(has("battery_aggregate_power_entity")).toBe(true);
    expect(has("battery_discharge_power_entity")).toBe(true);
    expect(has("battery_power_inverted")).toBe(false);
  });

  it("opens on what is stored", () => {
    open(config("get_direct", { battery_aggregate_power_entity: "sensor.battery" }), "site");
    expect(checked("battery")).toBe("one");
    document.body.replaceChildren();
    open(config("get_direct", { battery_aggregate_power_entity: "sensor.charge", battery_discharge_power_entity: "sensor.discharge" }), "site");
    expect(checked("battery")).toBe("two");
  });

  it("clears the sensors and the sign when none is chosen", () => {
    const { sent } = open(
      config("get_direct", { battery_aggregate_power_entity: "sensor.charge", battery_power_inverted: true }),
      "site",
    );
    expect(checked("battery")).toBe("one");
    pick("battery", "none");
    expect(changes(sent())).toEqual({ battery_aggregate_power_entity: "", battery_power_inverted: false });
  });

  it("clears the discharge sensor for one sensor, and the sign for two", () => {
    const stored = { battery_aggregate_power_entity: "sensor.charge", battery_discharge_power_entity: "sensor.discharge", battery_power_inverted: true };
    const { sent } = open(config("get_direct", stored), "site");
    expect(checked("battery")).toBe("two");
    expect(noteShown("battery")).toBe(true);
    expect(changes(sent())).toEqual({ battery_power_inverted: false });
    pick("battery", "one");
    expect(changes(sent())).toEqual({ battery_discharge_power_entity: "" });
  });

  it("sends nothing for an untouched site", () => {
    const { sent } = open(config("get_direct"), "site");
    expect(sent()).toEqual({ ok: true, changed: false });
  });
});

describe("the charger's energy: meter, power or none", () => {
  it("is none with nothing stored, and shows the chosen field only", () => {
    open(config("get_direct"), "charger");
    expect(checked("energy")).toBe("none");
    expect(has("energy_register_entity")).toBe(false);
    expect(has("power_entity")).toBe(false);
    pick("energy", "meter");
    expect(has("energy_register_entity")).toBe(true);
    expect(has("power_entity")).toBe(false);
    pick("energy", "power");
    expect(has("energy_register_entity")).toBe(false);
    expect(has("power_entity")).toBe(true);
    expect(note("energy")?.hidden).toBe(true);
  });

  it("opens on the stored kind, preferring the meter, and a save clears the other", () => {
    const { sent } = open(
      config("get_direct", { energy_register_entity: "sensor.kwh", power_entity: "sensor.plug_w" }),
      "charger",
    );
    expect(checked("energy")).toBe("meter");
    expect(noteShown("energy")).toBe(true);
    expect(changes(sent())).toEqual({ power_entity: "" });
  });

  it("opens on power for a plug, and none clears whichever is stored", () => {
    const { sent } = open(config("get_direct", { power_entity: "sensor.plug_w" }), "charger");
    expect(checked("energy")).toBe("power");
    pick("energy", "none");
    expect(changes(sent())).toEqual({ power_entity: "" });
  });

  it("opens on the meter when one is found automatically", () => {
    const cfg = config("get_direct");
    const register = cfg.fields.find((entry) => entry.field === "energy_register_entity");
    if (register?.kind !== "entity") {
      throw new Error("no register");
    }
    register.effective = { entityId: "sensor.auto_kwh", friendlyName: "Auto kWh", source: "automatic" };
    open(cfg, "charger");
    expect(checked("energy")).toBe("meter");
    // The automatic one is the first radio under the meter; the picker waits for "Choose an entity".
    expect(checked("energy-source")).toBe("automatic");
    expect(has("energy_register_entity")).toBe(false);
    expect(document.querySelector("[data-part='energy-source']")?.textContent).toContain("Automatic: Auto kWh");
    pick("energy-source", "choose");
    expect(has("energy_register_entity")).toBe(true);
  });
});

/** The charger's current limit as the backend describes it, with what each case needs set on the decoded field. */
function limitConfig(state: {
  stored?: string;
  automatic?: string;
  none?: { allowed: boolean; chosen: boolean } | null;
}): EntityConfig {
  const cfg = config("get_direct");
  const field = cfg.fields.find((entry) => entry.field === "current_limit");
  if (field?.kind !== "entity") {
    throw new Error("no current limit");
  }
  const automatic =
    state.automatic === undefined ? null : { entityId: state.automatic, friendlyName: "Session limit" };
  if (state.stored !== undefined) {
    field.current = { entityId: state.stored, friendlyName: state.stored, exists: true };
    field.effective = { entityId: state.stored, friendlyName: state.stored, source: "configured" };
  } else if (automatic !== null && !(state.none?.chosen ?? false)) {
    field.effective = { ...automatic, source: "automatic" };
  }
  field.none = state.none === null ? null : { allowed: true, chosen: false, ...state.none, automatic };
  return cfg;
}

const radioText = (part: string, value: string): string =>
  choice(part, value)?.closest("label")?.textContent ?? "";
const pickerOf = (field: string): HTMLInputElement | null =>
  document.querySelector<HTMLInputElement>(`[data-field='${field}']`);
const typeInto = (field: string, value: string): void => {
  const input = pickerOf(field);
  if (input === null) {
    throw new Error(`no ${field}`);
  }
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
};

describe("the charger's current limit: automatic, a chosen entity, or none", () => {
  it("opens on Automatic, naming the entity, when nothing is configured and one is found", () => {
    const { sent } = open(limitConfig({ automatic: "number.session" }), "charger");
    expect(checked("current-limit")).toBe("automatic");
    expect(radioText("current-limit", "automatic")).toBe("Automatic: Session limit");
    expect(radioText("current-limit", "choose")).toBe("Choose an entity");
    expect(radioText("current-limit", "none")).toBe("None (SpotNav does not set the current)");
    expect(has("current_limit")).toBe(false);
    // Said once: not again as a line under a picker.
    expect(document.querySelectorAll("[data-help='current_limit']")).toHaveLength(1);
    expect(sent()).toEqual({ ok: true, changed: false });
  });

  it("opens on Choose with the entity in its picker when one is configured, and still offers Automatic", () => {
    const { sent } = open(limitConfig({ stored: "number.mine", automatic: "number.session" }), "charger");
    expect(checked("current-limit")).toBe("choose");
    expect(pickerOf("current_limit")?.value).toBe("number.mine");
    expect(radioText("current-limit", "automatic")).toBe("Automatic: Session limit");
    expect(sent()).toEqual({ ok: true, changed: false });
    pick("current-limit", "automatic");
    expect(has("current_limit")).toBe(false);
    expect(changes(sent())).toEqual({ current_limit: "" });
  });

  it("saves the entity from Choose, and clears on Automatic even after one was typed", () => {
    const { sent } = open(limitConfig({ automatic: "number.session" }), "charger");
    pick("current-limit", "choose");
    expect(has("current_limit")).toBe(true);
    typeInto("current_limit", "number.mine");
    expect(changes(sent())).toEqual({ current_limit: "number.mine" });
    pick("current-limit", "automatic");
    expect(sent()).toEqual({ ok: true, changed: false });
  });

  it("offers no Automatic when nothing is found, and opens on Choose", () => {
    open(limitConfig({}), "charger");
    expect(choice("current-limit", "automatic")).toBeNull();
    expect(checked("current-limit")).toBe("choose");
    expect(has("current_limit")).toBe(true);
  });

  it("sends none as the value and what it read as expected, and clears nothing else", () => {
    const { sent } = open(limitConfig({ stored: "number.mine", automatic: "number.session" }), "charger");
    pick("current-limit", "none");
    expect(has("current_limit")).toBe(false);
    const result = sent();
    expect(changes(result)).toEqual({ current_limit: "none" });
    expect(result.ok && result.changed ? result.request.expected : null).toEqual({ current_limit: "number.mine" });
  });

  it("opens on None when that is stored, offers Automatic again, and leaving None sends the way back", () => {
    const { sent } = open(limitConfig({ automatic: "number.session", none: { allowed: true, chosen: true } }), "charger");
    expect(checked("current-limit")).toBe("none");
    expect(sent()).toEqual({ ok: true, changed: false });
    expect(radioText("current-limit", "automatic")).toBe("Automatic: Session limit");
    pick("current-limit", "automatic");
    const result = sent();
    expect(changes(result)).toEqual({ current_limit: "" });
    expect(result.ok && result.changed ? result.request.expected : null).toEqual({ current_limit: "none" });
  });

  it("does not send none when it is already so: nothing configured and nothing found", () => {
    const { sent } = open(limitConfig({}), "charger");
    pick("current-limit", "none");
    expect(sent()).toEqual({ ok: true, changed: false });
  });

  it("hides None where the current is set through this entity, but keeps it when it is what is stored", () => {
    open(limitConfig({ stored: "number.mine", none: { allowed: false, chosen: false } }), "charger");
    expect(choice("current-limit", "none")).toBeNull();
    expect(choice("current-limit", "choose")).not.toBeNull();
    document.body.replaceChildren();
    open(limitConfig({ none: { allowed: false, chosen: true } }), "charger");
    expect(checked("current-limit")).toBe("none");
  });

  it("hides None for a backend that does not describe it", () => {
    open(limitConfig({ stored: "number.mine", none: null }), "charger");
    expect(choice("current-limit", "none")).toBeNull();
  });

  it("leaves the picker's value out of a save while None is chosen, and brings it back on Choose", () => {
    const { sent } = open(limitConfig({ stored: "number.mine" }), "charger");
    pick("current-limit", "none");
    pick("current-limit", "choose");
    expect(pickerOf("current_limit")?.value).toBe("number.mine");
    expect(sent()).toEqual({ ok: true, changed: false });
  });

  it("keeps its radios disabled for a reader who may not change entities", () => {
    open(limitConfig({ automatic: "number.session" }), "charger", { readOnly: true });
    for (const radio of document.querySelectorAll<HTMLInputElement>("[data-part='current-limit'] input")) {
      expect(radio.disabled).toBe(true);
    }
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      open(limitConfig({ automatic: "number.session" }), "charger", { language });
      const text = document.body.textContent ?? "";
      expect(text, language).not.toMatch(/entity\.(limit|choice|automatic)/);
      for (const key of ["entity.choice.choose", "entity.limit.none"] as const) {
        expect(text, `${language} ${key}`).toContain(translate(language, key));
      }
      expect(text, language).toContain(translate(language, "entity.automatic", { name: "Session limit" }));
    }
    expect(translate("sv", "entity.limit.none")).toBe("Ingen (SpotNav ställer inte in strömmen)");
  });
});

describe("the charger's energy register: automatic or chosen, under the meter", () => {
  function withRegister(stored: string | null) {
    const cfg = config("get_direct", stored === null ? {} : { energy_register_entity: stored });
    const register = cfg.fields.find((entry) => entry.field === "energy_register_entity");
    if (register?.kind !== "entity") {
      throw new Error("no register");
    }
    register.effective = {
      entityId: stored ?? "sensor.auto_kwh",
      friendlyName: stored ?? "Auto kWh",
      source: stored === null ? "automatic" : "configured",
    };
    return cfg;
  }

  it("opens on Automatic with nothing configured, sends nothing, and has no None of its own", () => {
    const { sent } = open(withRegister(null), "charger");
    expect(checked("energy")).toBe("meter");
    expect(checked("energy-source")).toBe("automatic");
    expect(choice("energy-source", "none")).toBeNull();
    expect(sent()).toEqual({ ok: true, changed: false });
  });

  it("saves the chosen register, and clears it again on Automatic", () => {
    const { sent } = open(withRegister(null), "charger");
    pick("energy-source", "choose");
    typeInto("energy_register_entity", "sensor.my_kwh");
    expect(changes(sent())).toEqual({ energy_register_entity: "sensor.my_kwh" });
    pick("energy-source", "automatic");
    expect(sent()).toEqual({ ok: true, changed: false });
  });

  it("is a plain picker, with the help, when nothing is found automatically", () => {
    open(config("get_direct"), "charger");
    pick("energy", "meter");
    expect(document.querySelector("[data-part='energy-source']")).toBeNull();
    expect(has("energy_register_entity")).toBe(true);
    expect(document.querySelector("[data-help='energy_register_entity']")).not.toBeNull();
  });

  it("shows a stored register's picker directly (the backend names no automatic one then), and None above clears it as a choice", () => {
    const { sent } = open(withRegister("sensor.my_kwh"), "charger");
    expect(document.querySelector("[data-part='energy-source']")).toBeNull();
    expect(checked("energy")).toBe("meter");
    pick("energy", "none");
    // Not "": the register is not detected again for a person who cleared it.
    expect(changes(sent())).toEqual({ energy_register_entity: "none" });
  });
});

describe("saving and cancelling", () => {
  it("hands the cleared draft to onSave, and cancel changes nothing", () => {
    const { built, saved } = open(config("get_direct_total"), "site");
    pick("grid", "one");
    let cancelled = 0;
    built.body.querySelector<HTMLButtonElement>("button[type='button']")!.addEventListener("click", () => (cancelled += 1));
    built.body.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(saved).toHaveLength(1);
    expect(saved[0]?.["grid_power_source_power_export"]).toBe("");
    built.body.querySelector<HTMLButtonElement>("button[type='button']")!.click();
    expect(cancelled).toBe(1);
    expect(saved).toHaveLength(1);
  });

  it("keeps a refused field's error on the visible field only", () => {
    const { built } = open(config("get_derived", { derived_L1_current: "sensor.l1_i" }), "site");
    built.markErrors([
      { field: "derived_L1_current", code: "entity_not_found" },
      { field: "derived_L1_apparent_power", code: "entity_not_found" },
    ]);
    const shown = Array.from(document.querySelectorAll<HTMLElement>("[data-field-error]"))
      .filter((node) => !node.hidden)
      .map((node) => node.dataset["fieldError"]);
    expect(shown).toEqual(["derived_L1_current"]);
  });
});

describe("a reader who may not change anything", () => {
  it("sees the same layout with every control disabled", () => {
    open(config("get_derived", { battery_aggregate_power_entity: "sensor.b" }), "site", { readOnly: true });
    expect(checked("battery")).toBe("one");
    const controls = Array.from(document.querySelectorAll<HTMLInputElement | HTMLButtonElement>("input, button"));
    expect(controls.length).toBeGreaterThan(10);
    for (const control of controls) {
      if (control.matches("button[type='button']")) {
        continue;
      }
      expect(control.disabled, control.outerHTML).toBe(true);
    }
    pick("grid", "two");
    expect(has("derived_L1_power_export")).toBe(true);
    for (const control of document.querySelectorAll<HTMLInputElement>("input")) {
      expect(control.disabled).toBe(true);
    }
  });

  it("does not save", () => {
    const { built, saved } = open(config("get_direct"), "site", { readOnly: true });
    built.body.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(saved).toHaveLength(0);
  });

  it("keeps the charger's energy radios disabled too", () => {
    open(config("get_direct"), "charger", { readOnly: true });
    for (const radio of document.querySelectorAll<HTMLInputElement>("[data-part='energy'] input")) {
      expect(radio.disabled).toBe(true);
    }
  });
});

describe("every group in all five languages", () => {
  it.each([...LANGUAGES])("words the groups in %s without a raw key", (language) => {
    open(config("get_derived", { battery_aggregate_power_entity: "sensor.b" }), "site", { language });
    const text = document.body.textContent ?? "";
    for (const key of [
      "entity.grid.title",
      "entity.grid.one",
      "entity.grid.two",
      "entity.current.title",
      "entity.current.measured",
      "entity.current.apparent",
      "entity.current.reactive",
      "entity.current.estimated",
      "entity.current.estimatedNote",
      "entity.battery.title",
      "entity.battery.none",
      "entity.battery.one",
      "entity.battery.two",
    ] as const) {
      const words = translate(language, key);
      expect(words, key).not.toBe(key);
      expect(text, key).toContain(words);
    }
    expect(text).not.toMatch(/entity\.(grid|current|battery|choice|energy)\./);
    open(config("get_derived", { energy_register_entity: "sensor.k", power_entity: "sensor.p" }), "charger", { language });
    const energy = document.body.textContent ?? "";
    for (const key of ["entity.energy.title", "entity.energy.meter", "entity.energy.power", "entity.energy.none", "entity.choice.mixed"] as const) {
      expect(energy, key).toContain(translate(language, key));
    }
  });

  it("has distinct, non-English words in every other language", () => {
    for (const key of ["entity.grid.one", "entity.current.title", "entity.battery.two", "entity.energy.meter", "entity.choice.mixed"] as const) {
      for (const language of LANGUAGES) {
        if (language !== "en") {
          expect(translate(language, key), `${language} ${key}`).not.toBe(translate("en", key));
        }
      }
    }
  });
});

describe("voltage between phases: 400 V or 230 V, chosen like a type", () => {
  it("is offered in the site dialog, opens on what is stored and saves only a change", () => {
    const { sent } = open(config("get_direct"), "site");
    expect(checked("voltage")).toBe("400");
    expect(sent()).toEqual({ ok: true, changed: false });
    pick("voltage", "230");
    const result = sent();
    expect(result.ok && result.changed && result.request.changes).toEqual({ voltage_between_phases_v: "230" });
    expect(result.ok && result.changed && result.request.expected).toEqual({ voltage_between_phases_v: "400" });
  });

  it("is offered in the dialog of a charger that is in no site, and not for a charger in a site", () => {
    open(config("get_no_site"), "charger");
    expect(choice("voltage", "230")).not.toBeNull();
    open(config("get_direct"), "charger");
    expect(choice("voltage", "230")).toBeNull();
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      open(config("get_direct"), "site", { language });
      const text = document.body.textContent ?? "";
      for (const key of ["entity.field.voltageBetweenPhases", "entity.help.voltageBetweenPhases", "entity.voltage.tn", "entity.voltage.it"] as const) {
        expect(text, `${language} ${key}`).toContain(translate(language, key));
      }
      if (language !== "en") {
        expect(translate(language, "entity.voltage.it")).not.toBe(translate("en", "entity.voltage.it"));
      }
    }
  });
});

describe("the phases the charger is wired for: 1 or 3, chosen like a type, for a charger in no site", () => {
  it("is offered in the dialog of a charger that is in no site, opens on what is stored and saves only a change", () => {
    const { sent } = open(config("get_no_site"), "charger");
    expect(checked("charger-phases")).toBe("3");
    expect(sent()).toEqual({ ok: true, changed: false });
    pick("charger-phases", "1");
    const result = sent();
    expect(result.ok && result.changed && result.request.changes).toEqual({ charger_phases: "1" });
    expect(result.ok && result.changed && result.request.expected).toEqual({ charger_phases: "3" });
  });

  it("is not offered to a charger in a site, whose site holds the wiring", () => {
    open(config("get_direct"), "charger");
    expect(choice("charger-phases", "1")).toBeNull();
  });

  it("is stated read-only for a charger in a site, with a way to the site's wiring, and saves nothing", () => {
    let opened = 0;
    const cfg = config("get_direct");
    const built = entityEditorBody(
      document,
      "en",
      { scope: "charger", config: cfg, hass: () => undefined, appliesText: null },
      { onSave: () => undefined, onCancel: () => undefined, onOpenSite: () => (opened += 1) },
      "t",
    );
    document.body.replaceChildren(built.body);
    const line = document.querySelector<HTMLElement>("[data-wired-phases]")!;
    expect(line.dataset["wiredPhases"]).toBe("3");
    expect(line.textContent).toContain("The charger is wired for 3 phases (from the site).");
    expect(line.querySelector("button")?.textContent).toBe("Site wiring");
    expect(entityChange(cfg, "charger", built.draft())).toEqual({ ok: true, changed: false });
    line.querySelector<HTMLButtonElement>("[data-action='open-site']")!.click();
    expect(opened).toBe(1);
  });

  it("states where the site reads the charger's measured current from, and when it reads none", () => {
    const cfg = config("get_direct");
    const field = cfg.fields.find((entry) => entry.field === "measured_current_source")!;
    expect(field.kind).toBe("enum");
    const render = (value: string | null) => {
      const edited = { ...cfg, fields: cfg.fields.map((entry) => (entry.field === "measured_current_source" ? { ...entry, value } : entry)) } as typeof cfg;
      const built = entityEditorBody(
        document,
        "en",
        { scope: "charger", config: edited, hass: () => undefined, appliesText: null },
        { onSave: () => undefined, onCancel: () => undefined },
        "t",
      );
      document.body.replaceChildren(built.body);
      expect(entityChange(edited, "charger", built.draft())).toEqual({ ok: true, changed: false });
      return document.querySelector("[data-measured-source]")?.textContent ?? "";
    };
    expect(render("sensor.easee_driveway_current")).toContain("measured current from sensor.easee_driveway_current");
    expect(render(null)).toContain("reads no measured current");
    for (const language of LANGUAGES) {
      expect(translate(language, "entity.measuredSource.from", { source: "x" }), language).toContain("x");
      expect(translate(language, "entity.measuredSource.none"), language).not.toBe("entity.measuredSource.none");
    }
  });

  it("is not stated for a charger in no site, which chooses it", () => {
    open(config("get_no_site"), "charger");
    expect(document.querySelector("[data-wired-phases]")).toBeNull();
  });

  it("is stated in every language", () => {
    for (const language of LANGUAGES) {
      open(config("get_direct"), "charger", { language });
      const line = document.querySelector("[data-wired-phases]")?.textContent ?? "";
      expect(line, language).toContain(translate(language, "settings.phases.three"));
      expect(line, language).toContain(translate(language, "entity.phases.fromSite", { phases: translate(language, "settings.phases.three") }));
    }
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      open(config("get_no_site"), "charger", { language });
      const text = document.body.textContent ?? "";
      for (const key of ["entity.field.chargerPhases", "entity.help.chargerPhases", "settings.phases.one", "settings.phases.three"] as const) {
        expect(text, `${language} ${key}`).toContain(translate(language, key));
      }
      if (language !== "en") {
        expect(translate(language, "entity.help.chargerPhases")).not.toBe(translate("en", "entity.help.chargerPhases"));
      }
    }
  });
});

describe("charger priority: first, normal or last in the site's order", () => {
  it("is offered to a charger in a site, opens on what is stored and saves only a change", () => {
    const { sent } = open(config("get_direct"), "charger");
    expect(checked("priority")).toBe("normal");
    expect(sent()).toEqual({ ok: true, changed: false });
    pick("priority", "first");
    const result = sent();
    expect(result.ok && result.changed && result.request.changes).toEqual({ charger_priority: "first" });
    expect(result.ok && result.changed && result.request.expected).toEqual({ charger_priority: "normal" });
  });

  it("is not offered to a charger in no site", () => {
    open(config("get_no_site"), "charger");
    expect(choice("priority", "first")).toBeNull();
  });

  it("is worded in every language", () => {
    for (const language of LANGUAGES) {
      open(config("get_direct"), "charger", { language });
      const text = document.body.textContent ?? "";
      for (const key of ["entity.field.chargerPriority", "entity.help.chargerPriority", "entity.priority.first", "entity.priority.last"] as const) {
        expect(text, `${language} ${key}`).toContain(translate(language, key));
      }
      if (language !== "en") {
        expect(translate(language, "entity.help.chargerPriority")).not.toBe(translate("en", "entity.help.chargerPriority"));
      }
    }
  });
});
