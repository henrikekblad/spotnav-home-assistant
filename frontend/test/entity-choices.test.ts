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
    expect(has("energy_register_entity")).toBe(true);
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
