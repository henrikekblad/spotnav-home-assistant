// The entity-configuration contract's own rules, apart from any DOM: what the decoder refuses, what a
// Save would send, and that every code the backend can answer with has a sentence in every language.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  decodeEntityAnswer,
  draftFrom,
  entityChange,
  entityErrorKey,
  fieldErrorKey,
  phaseField,
  phaseFieldNames,
  type EntityConfig,
} from "../src/entity-config";
import { LANGUAGES, translate } from "../src/i18n";

const DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");

function raw(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

function config(name: string): EntityConfig {
  const result = decodeEntityAnswer(raw(name));
  if (!result.ok || result.value.config === null) {
    throw new Error(`${name} did not decode`);
  }
  return result.value.config;
}

describe("the control block", () => {
  const control = () => (raw("get_direct")["config"] as { control: Record<string, unknown> }).control;

  it("is decoded into the path, the policy and what the adapter supports", () => {
    const decoded = config("get_direct").control;
    expect(decoded).not.toBeNull();
    expect(decoded?.startStop).toEqual({
      kind: "switch",
      entityIds: ["switch.get_direct_control"],
      inverted: false,
      startOption: null,
      stopOption: null,
    });
    expect(decoded?.current.kind).toBe("none");
    expect(decoded?.policy.regulatorWrites).toBe(true);
    expect(decoded?.capabilities.startStop).toBe(true);
    expect(decoded?.conflicts).toEqual([]);
  });

  it("refuses a block with an unknown key, an unknown kind or a policy that is not a number", () => {
    const answer = raw("get_direct");
    const withControl = (changed: Record<string, unknown>) => ({
      ...answer,
      config: { ...(answer["config"] as Record<string, unknown>), control: changed },
    });
    expect(decodeEntityAnswer(withControl({ ...control(), extra: 1 }))).toEqual({ ok: false, failure: "malformed" });
    expect(
      decodeEntityAnswer(withControl({ ...control(), current: { ...(control()["current"] as object), kind: "magic" } })),
    ).toEqual({ ok: false, failure: "malformed" });
    expect(
      decodeEntityAnswer(withControl({ ...control(), policy: { ...(control()["policy"] as object), min_interval_s: "90" } })),
    ).toEqual({ ok: false, failure: "malformed" });
  });
});

describe("decoding", () => {
  it("refuses another api_version as unsupported, and everything else that does not fit as malformed", () => {
    expect(decodeEntityAnswer({ ...raw("get_direct"), api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeEntityAnswer(null)).toEqual({ ok: false, failure: "malformed" });
    expect(decodeEntityAnswer({ ...raw("get_direct"), extra: 1 })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeEntityAnswer({ ...raw("get_direct"), ok: false })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeEntityAnswer({ ...raw("not_admin"), error: null })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeEntityAnswer({ ...raw("get_direct"), config: null })).toEqual({ ok: false, failure: "malformed" });
  });

  it("refuses a field of a kind or scope it does not know, and an entity field with a stray key", () => {
    const answer = raw("get_direct");
    const source = answer["config"] as { fields: Array<Record<string, unknown>> };
    const withField = (patch: Record<string, unknown>) => ({
      ...answer,
      config: { ...source, fields: [{ ...source.fields[0], ...patch }, ...source.fields.slice(1)] },
    });
    expect(decodeEntityAnswer(withField({ kind: "colour" }))).toEqual({ ok: false, failure: "malformed" });
    expect(decodeEntityAnswer(withField({ scope: "galaxy" }))).toEqual({ ok: false, failure: "malformed" });
    expect(decodeEntityAnswer(withField({ stray: true }))).toEqual({ ok: false, failure: "malformed" });
    expect(decodeEntityAnswer(withField({ current: { entity_id: "x" } }))).toEqual({ ok: false, failure: "malformed" });
  });
});

describe("the effective entity", () => {
  it("is decoded with its source, and a source it does not know is refused", () => {
    const answer = raw("get_direct");
    const source = answer["config"] as { fields: Array<Record<string, unknown>> };
    const withEffective = (effective: unknown) => ({
      ...answer,
      config: { ...source, fields: [{ ...source.fields[0], effective }, ...source.fields.slice(1)] },
    });
    const ok = decodeEntityAnswer(
      withEffective({ entity_id: "switch.x", friendly_name: "X", source: "automatic" }),
    );
    expect(ok.ok && ok.value.ok && ok.value.config.fields[0]).toMatchObject({
      effective: { entityId: "switch.x", friendlyName: "X", source: "automatic" },
    });
    expect(decodeEntityAnswer(withEffective({ entity_id: "switch.x", friendly_name: "X", source: "guessed" }))).toEqual({
      ok: false,
      failure: "malformed",
    });
    expect(decodeEntityAnswer(withEffective({ entity_id: "switch.x", friendly_name: "X" }))).toEqual({
      ok: false,
      failure: "malformed",
    });
  });
});

describe("what a Save sends", () => {
  it("is nothing when the draft is what was read", () => {
    const read = config("get_direct");
    expect(entityChange(read, "charger", draftFrom(read, "charger"))).toEqual({ ok: true, changed: false });
    expect(entityChange(read, "site", draftFrom(read, "site"))).toEqual({ ok: true, changed: false });
  });

  it("never offers the vehicle level for change", () => {
    const read = config("get_direct");
    expect(Object.keys(draftFrom(read, "charger"))).toEqual(["charge_control", "current_limit", "energy_register_entity"]);
  });

  it("clears an optional entity with an empty value, expecting the one it read", () => {
    const read = config("success_charger");
    const change = entityChange(read, "charger", { ...draftFrom(read, "charger"), current_limit: "  " });
    expect(change).toEqual({
      ok: true,
      changed: true,
      request: {
        scope: "charger",
        expected: { current_limit: "number.ok_limit" },
        changes: { current_limit: "" },
      },
    });
  });

  it("takes a decimal comma, and compares numbers as numbers", () => {
    const read = config("get_direct");
    const draft = { ...draftFrom(read, "site") };
    expect(entityChange(read, "site", { ...draft, main_fuse_a: "25.0" })).toEqual({ ok: true, changed: false });
    const change = entityChange(read, "site", { ...draft, max_age_s: "90,5" });
    expect(change.ok && change.changed ? change.request.changes : null).toEqual({ max_age_s: 90.5 });
  });

  it("checks a number for being a number and for the minimum, naming the field", () => {
    const read = config("get_direct");
    const draft = { ...draftFrom(read, "site") };
    expect(entityChange(read, "site", { ...draft, main_fuse_a: "abc" })).toEqual({
      ok: false,
      errors: [{ field: "main_fuse_a", code: "invalid_value" }],
    });
    expect(entityChange(read, "site", { ...draft, main_fuse_a: "0.05", max_age_s: "" })).toEqual({
      ok: false,
      errors: [
        { field: "main_fuse_a", code: "invalid_value" },
        { field: "max_age_s", code: "required" },
      ],
    });
  });

  it("considers only the resulting mode's meters, and gives an unread meter no expectation", () => {
    const read = config("get_direct");
    const draft = {
      ...draftFrom(read, "site"),
      measurement_mode: "derived_phase_current",
      derived_L1_power: "sensor.p1",
      // A direct meter typed earlier is not part of a derived save.
      direct_L1: "sensor.changed",
    };
    const change = entityChange(read, "site", draft);
    expect(change).toEqual({
      ok: true,
      changed: true,
      request: {
        scope: "site",
        expected: { measurement_mode: "direct_phase_current" },
        changes: { measurement_mode: "derived_phase_current", derived_L1_power: "sensor.p1" },
      },
    });
  });

  it("describes the other mode's meters with the backend's own rule", () => {
    expect(phaseFieldNames("derived_phase_current")).toHaveLength(18);
    expect(phaseFieldNames("direct_phase_current")).toEqual(["direct_L1", "direct_L2", "direct_L3"]);
    const read = config("get_direct");
    expect(phaseField(read, "derived_L2_reactive_power")).toMatchObject({
      domains: ["sensor"],
      deviceClasses: ["reactive_power"],
      current: null,
    });
    // The listed descriptor wins over the built one.
    expect(phaseField(read, "direct_L1").current?.entityId).toBe("sensor.site_get_direct_l1");
    // And the derived config's own descriptors agree with what the card would have built.
    const derived = config("get_derived");
    for (const name of phaseFieldNames("derived_phase_current")) {
      const listed = derived.fields.find((field) => field.field === name);
      const built = phaseField(read, name);
      expect(listed?.kind === "entity" ? [listed.domains, listed.deviceClasses] : null, name).toEqual([
        built.domains,
        built.deviceClasses,
      ]);
    }
  });
});

describe("the words", () => {
  const FIELD_CODES = [
    "required",
    "entity_not_found",
    "wrong_domain",
    "invalid_value",
    "not_writable",
    "unknown_field",
    "charge_control_in_use",
    "current_limit_in_use",
  ];
  const TOP_CODES = [
    "spotnav_not_admin",
    "spotnav_conflict",
    "spotnav_invalid_value",
    "spotnav_no_site",
    "spotnav_unknown_charger",
    "spotnav_site_not_charger",
    "spotnav_charger_unloaded",
    null,
  ];

  it("gives every field code its own sentence, in every language", () => {
    const sentences = new Set(FIELD_CODES.map((code) => fieldErrorKey(code)));
    expect(sentences.size).toBe(FIELD_CODES.length);
    for (const language of LANGUAGES) {
      for (const code of FIELD_CODES) {
        expect(translate(language, fieldErrorKey(code)).length, `${language} ${code}`).toBeGreaterThan(3);
      }
    }
  });

  it("gives every envelope code a sentence, and an unknown code the general one", () => {
    for (const language of LANGUAGES) {
      for (const code of TOP_CODES) {
        expect(translate(language, entityErrorKey(code)).length, `${language} ${code}`).toBeGreaterThan(3);
      }
    }
    expect(entityErrorKey("something_new")).toBe("entity.error.generic");
    expect(entityErrorKey("spotnav_conflict")).toBe("entity.error.conflict");
    expect(fieldErrorKey("something_new")).toBe("entity.error.field.unknown");
  });

  it("translates the entity sentences rather than copying the English", () => {
    for (const key of ["entity.error.conflict", "entity.edit.charger", "entity.vehicle.automatic", "market.edit"] as const) {
      expect(new Set(LANGUAGES.map((language) => translate(language, key))).size, key).toBeGreaterThanOrEqual(4);
    }
    expect(translate("sv", "market.edit")).toBe("Ändra elområde och skatter");
    expect(translate("sv", "settings.vehicle.change")).toBe("Ändra fordon");
  });
});

describe("the site's measurement, warnings and detection", () => {
  it("decodes the detected fixture into its estimate, warnings, meters and batteries", () => {
    const read = config("get_detected");
    const site = read.site;
    expect(site?.measurement).toMatchObject({
      mode: "derived_phase_current",
      currentEstimated: true,
      assumedPowerFactor: 0.9,
      basis: { L1: "estimated", L2: "estimated", L3: "estimated" },
    });
    expect(site?.warnings.map((warning) => warning.code)).toEqual([
      "update_interval_exceeds_max_age",
      "own_load_balancing",
    ]);
    expect(site?.warnings[0]).toMatchObject({ integration: "solaredge_modbus_multi", intervalS: 300 });
    expect(site?.warnings[1]?.deviceName).toBe("Easee Equalizer");
    const huawei = site?.meters.find((meter) => meter.integration === "huawei_solar");
    expect(huawei).toMatchObject({ mode: "derived_phase_current", currentSigned: true, powerInverted: true, applied: false });
    expect(huawei?.disabledEntities).toHaveLength(6);
    expect(site?.batteries.map((battery) => battery.integration)).toContain("sma");
  });

  it("refuses a site block with a stray key or an unknown basis", () => {
    const answer = raw("get_detected");
    const source = answer["config"] as { site: Record<string, unknown> };
    expect(decodeEntityAnswer({ ...answer, config: { ...source, site: { ...source.site, stray: 1 } } })).toEqual({
      ok: false,
      failure: "malformed",
    });
    const measurement = source.site["measurement"] as Record<string, unknown>;
    const basis = { L1: "guessed", L2: null, L3: null };
    expect(
      decodeEntityAnswer({ ...answer, config: { ...source, site: { ...source.site, measurement: { ...measurement, basis } } } }),
    ).toEqual({ ok: false, failure: "malformed" });
  });

  it("sends a flag as a boolean with the boolean it read", () => {
    const read = config("get_direct");
    const draft = { ...draftFrom(read, "site"), site_current_signed: "true" };
    const change = entityChange(read, "site", draft);
    expect(change.ok && change.changed ? change.request : null).toEqual({
      scope: "site",
      expected: { site_current_signed: false },
      changes: { site_current_signed: true },
    });
  });

  it("applies a detected setup on its own, by id", () => {
    const read = config("get_detected");
    const change = entityChange(read, "site", { apply_detection: "huawei_solar:huawei_solar_huawei" });
    expect(change).toEqual({
      ok: true,
      changed: true,
      request: { scope: "site", expected: {}, changes: { apply_detection: "huawei_solar:huawei_solar_huawei" } },
    });
  });

  it("gives an optional derived source no requirement and the backend's device class", () => {
    const read = config("get_direct");
    expect(phaseField(read, "derived_L1_apparent_power")).toMatchObject({ required: false, deviceClasses: ["apparent_power"] });
    expect(phaseField(read, "derived_L1_power")).toMatchObject({ required: true });
  });
});
