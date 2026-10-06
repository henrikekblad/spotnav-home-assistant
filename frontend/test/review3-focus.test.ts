// Review round 3: a focused (one-value) charger entity dialog must send nothing of its hidden parts.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { entityChange, decodeEntityAnswer, type EntityConfig } from "../src/entity-config";
import { entityEditorBody } from "../src/entity-editor";

const DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");

function config(patch: Record<string, string>, autoRegister = false): EntityConfig {
  const raw = JSON.parse(readFileSync(join(DIR, "get_direct.json"), "utf8")) as { config: { fields: Array<Record<string, unknown>> } };
  for (const [field, value] of Object.entries(patch)) {
    const entry = raw.config.fields.find((item) => item["field"] === field)!;
    entry["current"] = { entity_id: value, friendly_name: value, exists: true };
  }
  const decoded = decodeEntityAnswer(raw);
  if (!decoded.ok || !decoded.value.ok) throw new Error("fixture");
  const cfg = decoded.value.config;
  if (autoRegister) {
    const register = cfg.fields.find((entry) => entry.field === "energy_register_entity");
    if (register?.kind === "entity") {
      register.effective = { entityId: "sensor.auto_kwh", friendlyName: "Auto kWh", source: "automatic" };
    }
  }
  return cfg;
}

function saveFocused(cfg: EntityConfig, focus: string): unknown {
  const saved: Array<Record<string, string>> = [];
  const built = entityEditorBody(
    document, "en",
    { scope: "charger", config: cfg, hass: () => undefined, appliesText: null, focus },
    { onSave: (draft) => saved.push(draft), onCancel: () => undefined }, "t",
  );
  document.body.replaceChildren(built.body);
  // The person opened only the current limit and pressed Save without touching it.
  built.body.dispatchEvent(new Event("submit", { cancelable: true }));
  const result = entityChange(cfg, "charger", saved[0]!);
  return result.ok && result.changed ? result.request.changes : {};
}

describe("a focused charger dialog sends nothing of its hidden parts", () => {
  it("saving the current limit alone leaves a stored smart-plug power entity (meter also stored)", () => {
    const cfg = config({ energy_register_entity: "sensor.kwh", power_entity: "sensor.plug_w" });
    expect(saveFocused(cfg, "current-limit")).toEqual({});
  });
  it("saving the charge control alone leaves a stored smart-plug power entity (a register found automatically)", () => {
    const cfg = config({ power_entity: "sensor.plug_w" }, true);
    expect(saveFocused(cfg, "charge-control")).toEqual({});
  });
});

describe("a smart-plug charger as the backend describes it (review3_smart_plug.json, from the backend test)", () => {
  it("keeps the plug's power entity when only the charge control is saved", () => {
    const raw = JSON.parse(readFileSync(join(DIR, "review3_smart_plug.json"), "utf8"));
    const decoded = decodeEntityAnswer(raw);
    if (!decoded.ok || !decoded.value.ok) throw new Error("answer");
    expect(saveFocused(decoded.value.config, "charge-control")).toEqual({});
    expect(saveFocused(decoded.value.config, "current-limit")).toEqual({});
  });
});
