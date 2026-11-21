// The battery status's `grid_charge_cap` block: decoded strictly, shown as one line under what SpotNav may do.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { LANGUAGES } from "../src/i18n";
import { gridChargeCapLine, haDownView, importLimitHint, importLimitPhaseStep, perPhaseLine } from "../src/settings-popover";
import { decodeBatteryControl, type BatteryStatus } from "../src/site-model";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "battery_control");
const fixture = (name: string): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as Record<string, any>;

function status(cap: Record<string, unknown>, allowed = ["grid_charge"], more: Record<string, unknown> = {}): BatteryStatus {
  const raw = fixture("checking.json");
  raw["status"].allowed_actions = allowed;
  raw["status"].grid_charge_cap = { ...raw["status"].grid_charge_cap, ...cap };
  raw["status"] = { ...raw["status"], ...more };
  const decoded = decodeBatteryControl(raw);
  if (!decoded.ok || decoded.value.status === null) {
    throw new Error("unreadable");
  }
  return decoded.value.status;
}

describe("the grid charge cap block", () => {
  it("reads both fixtures, the null forms included", () => {
    expect(decodeBatteryControl(fixture("checking.json")).ok).toBe(true);
    expect(decodeBatteryControl(fixture("needs_ack.json")).ok).toBe(true);
  });

  it("rejects a missing block, an unknown key and a mistyped nested value", () => {
    const missing = fixture("checking.json");
    delete missing["status"].grid_charge_cap;
    expect(decodeBatteryControl(missing).ok).toBe(false);
    const extra = fixture("checking.json");
    extra["status"].grid_charge_cap.extra = 1;
    expect(decodeBatteryControl(extra).ok).toBe(false);
    const nested = fixture("checking.json");
    nested["status"].grid_charge_cap.learned.raw_a.L2 = "4";
    expect(decodeBatteryControl(nested).ok).toBe(false);
  });

  it("says the cap and the rule in Swedish", () => {
    expect(gridChargeCapLine("sv", status({}))).toBe(
      "Högst 3,2 kW från nätet, så säkringen klarar sig även om Home Assistant stannar (inlärt per fas).",
    );
    const few = { ...fixture("checking.json")["status"].grid_charge_cap.learned, ready: false, usable: false, days_counted: 3 };
    expect(gridChargeCapLine("sv", status({ rule: "conservative", learned: null }))).toContain(
      "(försiktigt tills de första dygnen är inlärda)",
    );
    expect(few.days_min).toBe(7);
  });

  it("says the days left to the first usable days while the cautious rule counts", () => {
    const learned = {
      ...fixture("checking.json")["status"].grid_charge_cap.learned,
      ready: false, usable: false, extra_margin_a: null, days_counted: 9, days_spread: 5, days_left: 9,
    };
    expect(gridChargeCapLine("sv", status({ rule: "conservative", cap_kw: 0, learned }))).toBe(
      "Ingen laddning från nätet: huset kan nå säkringen själv (försiktigt tills de första dygnen är inlärda, 2 dygn kvar).",
    );
    expect(gridChargeCapLine("en", status({ rule: "conservative", learned: { ...learned, days_spread: 6 } }))).toContain(
      "(cautious until the first days are learned, 1 day left)",
    );
  });

  it("says the days left to the whole learning while the learned peaks carry a further margin", () => {
    const learned = {
      ...fixture("checking.json")["status"].grid_charge_cap.learned,
      ready: false, usable: true, extra_margin_a: 3.27, days_counted: 8, days_left: 9, days_from_history: 5,
    };
    expect(gridChargeCapLine("sv", status({ rule: "learning", cap_kw: 1.44, learned }))).toBe(
      "Högst 1,4 kW från nätet, så säkringen klarar sig även om Home Assistant stannar (medan husets toppar lärs in, 9 dygn kvar).",
    );
    // No charge at all: one reason. The learning's margin where the learned peaks alone would leave a charge
    // (the fixture's 3.2 kW), the house itself where they would not.
    expect(gridChargeCapLine("en", status({ rule: "learning", cap_kw: 0, learned }))).toBe(
      "No charging from the grid while the house's peaks are being learned (9 days left).",
    );
    expect(gridChargeCapLine("sv", status({ rule: "learning", cap_kw: 0, cap_learned_kw: 0, learned }))).toBe(
      "Ingen laddning från nätet: huset kan nå säkringen själv (inlärt per fas).",
    );
    for (const language of LANGUAGES) {
      const line = gridChargeCapLine(language, status({ rule: "learning", cap_kw: 0, learned }));
      expect(line, language).toMatch(/9/);
      expect(line, language).not.toContain("{");
    }
    for (const language of LANGUAGES) {
      const line = gridChargeCapLine(language, status({ rule: "learning", cap_kw: 1.44, learned }));
      expect(line, language).toMatch(/1[.,]4.*9/);
      expect(line, language).not.toContain("{");
    }
  });

  it("says when the house can reach the fuse by itself", () => {
    expect(gridChargeCapLine("sv", status({ cap_kw: 0 }))).toBe(
      "Ingen laddning från nätet: huset kan nå säkringen själv (inlärt per fas).",
    );
  });

  it("names the import limit when there is no cap, and shows nothing when it cannot", () => {
    const guarding = { value_kw: 4.0, max_kw: 4.47, suggested_kw: 4.4, holds: true, why: null };
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "import_limit", learned: null, import_limit: guarding }))).toBe(
      "Anläggningens egen importgräns (4 kW) skyddar säkringen vid laddning från nätet. Den begränsar summan över de tre"
        + " faserna och är därför satt lägre än säkringen: en fas kan bära husets topp ensam.",
    );
    // A Home Assistant that does not say the limit's value yet.
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "import_limit", learned: null, import_limit: null }))).toBe(
      "Anläggningens egen importgräns skyddar säkringen vid laddning från nätet.",
    );
    expect(gridChargeCapLine("sv", status({ cap_kw: null, rule: "error", learned: null }))).toBeNull();
    expect(gridChargeCapLine("sv", status({}, []))).toBeNull();
  });

  it("says why an import limit at the fuse does not guard it, and the most it may read", () => {
    const acts = { options: { ...fixture("checking.json")["status"].options, battery_control_import_limit_acts: true } };
    const tooHigh = { value_kw: 13.8, max_kw: 4.47, suggested_kw: 4.4, holds: false, why: null };
    expect(importLimitHint("sv", status({ import_limit: tooHigh }, ["grid_charge"], acts))).toBe(
      "Anläggningens importgräns står på 13,8 kW. Den begränsar summan över de tre faserna, inte varje fas, och skyddar"
        + " säkringen först vid högst 4,4 kW: en fas kan bära husets topp ensam.",
    );
    expect(importLimitHint("en", status({ import_limit: { ...tooHigh, value_kw: null } }, ["grid_charge"], acts))).toBe(
      "The plant's import limit limits the total over the three phases, not each phase. It protects the fuse when set"
        + " to 4.4 kW or less: one phase can carry the house's peak alone.",
    );
    // Nothing to say: it guards, it was never said to act, no value can guard, or no grid charging at all.
    expect(importLimitHint("sv", status({ import_limit: { ...tooHigh, holds: true } }, ["grid_charge"], acts))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: tooHigh }))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: { ...tooHigh, suggested_kw: null } }, ["grid_charge"], acts))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: null }, ["grid_charge"], acts))).toBeNull();
    expect(importLimitHint("sv", status({ import_limit: tooHigh }, ["hold"], acts))).toBeNull();
    for (const language of LANGUAGES) {
      for (const limit of [tooHigh, { ...tooHigh, value_kw: null }]) {
        const line = importLimitHint(language, status({ import_limit: limit }, ["grid_charge"], acts));
        expect(line, language).toMatch(/4[.,]4/);
        expect(line, language).not.toContain("{");
      }
      const total = gridChargeCapLine(language, status({ cap_kw: null, rule: "import_limit", import_limit: { ...tooHigh, value_kw: 4.2, holds: true } }));
      expect(total, language).toMatch(/4[.,]2/);
      expect(total, language).not.toContain("{");
    }
  });

  it("says a grid charge per phase: what each phase may take, and what it takes while one runs", () => {
    const block = { state: "verified", why: null, used: true, caps_kw: { L1: 1.02, L2: 0, L3: 0.14 }, charging_kw: null };
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: block }))).toBe(
      "Nätladdning per fas, högst: L1 1,02 kW · L2 0 kW · L3 0,14 kW",
    );
    const running = { ...block, charging_kw: { L1: 1.01, L2: 0, L3: 0.13 } };
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: running }))).toBe(
      "Nätladdning per fas: L1 1,01 kW · L2 0 kW · L3 0,13 kW",
    );
    // Not verified, not used now, no grid charging, or a plant without targets per phase: nothing.
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: { ...block, state: "pending" } }))).toBeNull();
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: { ...block, used: false } }))).toBeNull();
    expect(perPhaseLine("sv", status({}, ["hold"], { per_phase: running }))).toBeNull();
    expect(perPhaseLine("sv", status({}, ["grid_charge"], { per_phase: null }))).toBeNull();
    for (const language of LANGUAGES) {
      for (const each of [block, running]) {
        const line = perPhaseLine(language, status({}, ["grid_charge"], { per_phase: each }));
        expect(line, language).toMatch(/L1 1[.,]0[12] kW · L2 0 kW · L3 0[.,]1[34] kW/);
        expect(line, language).not.toContain("{");
      }
    }
  });

  it("reads an answer from before the two blocks, and rejects a mistyped one", () => {
    const older = fixture("checking.json");
    delete older["status"].per_phase;
    delete older["status"].grid_charge_cap.import_limit;
    const decoded = decodeBatteryControl(older);
    expect(decoded.ok).toBe(true);
    if (decoded.ok) {
      expect(decoded.value.status?.per_phase).toBeNull();
      expect(decoded.value.status?.grid_charge_cap.import_limit).toBeNull();
    }
    const mistyped = fixture("checking.json");
    mistyped["status"].per_phase = { state: "verified", why: null, used: true, caps_kw: { L1: 1, L2: 0 }, charging_kw: null };
    expect(decodeBatteryControl(mistyped).ok).toBe(false);
    const extra = fixture("checking.json");
    extra["status"].grid_charge_cap.import_limit = { value_kw: 1, max_kw: 1, suggested_kw: 1, holds: false, why: null, more: 1 };
    expect(decodeBatteryControl(extra).ok).toBe(false);
  });

  describe("the plant's import limit held per phase", () => {
    const on = (more: Record<string, unknown> = {}) => ({
      options: { ...fixture("checking.json")["status"].options, battery_control_enabled: true, ...more },
    });
    const limit = (phase: Record<string, unknown>, more: Record<string, unknown> = {}) => ({
      import_limit: {
        value_kw: 13.8, max_kw: 4.47, suggested_kw: 4.4, holds: false, why: null,
        per_phase: { max_kw: 13.16, holds: false, why: null, entity_id: null, ...phase }, ...more,
      },
    });
    const step = (language: "sv" | "en", cap: Record<string, unknown>, more = on(), allowed = ["grid_charge"]) =>
      importLimitPhaseStep(language, status(cap, allowed, more));

    it("says that it holds, with a third of the limit on each phase", () => {
      const held = { cap_kw: null, rule: "import_limit", learned: null, ...limit({ holds: true }, { value_kw: 12.4, holds: true }) };
      expect(gridChargeCapLine("sv", status(held))).toBe(
        "Sigenergy håller varje fas under 4,2 kW (anläggningens importgräns, 12,4 kW, delad på tre faser): laddning från"
          + " nätet tar det som huset lämnar.",
      );
      expect(gridChargeCapLine("en", status(held))).toBe(
        "Sigenergy holds each phase under 4.2 kW (the plant's import limit, 12.4 kW, shared over three phases): grid"
          + " charging uses what the house leaves.",
      );
      expect(step("sv", held)).toBeNull();
    });

    it("names the one step missing, in the order the rule checks them", () => {
      expect(step("sv", limit({ why: "not_said_to_act" }))).toEqual({
        code: "not_said_to_act",
        entityId: null,
        text: "Sigenergy kan hålla varje fas under anläggningens egen importgräns, så laddning från nätet behöver inte"
          + " längre hållas tillbaka för att skydda säkringen om Home Assistant stannar. Slå på ”Importgränsen verkar”"
          + " för att lita på den.",
      });
      const sensor = step("en", limit({ why: "grid_sensor_offline", entity_id: "sensor.sigen_plant_grid_sensor_status" }));
      expect(sensor?.entityId).toBe("sensor.sigen_plant_grid_sensor_status");
      expect(sensor?.text).toContain("Enable its status entity in the Sigenergy integration if it is disabled");
      expect(step("en", limit({ why: "output_type_unread" }))?.text).toContain("cannot read the plant's Output Type");
      expect(step("en", limit({ why: "phase_control_off" }))?.text).toContain("Independent phase power control is off");
      expect(step("sv", limit({}))?.text).toBe(
        "Anläggningens importgräns står på 13,8 kW. Sänk den till högst 13,1 kW i Sigenergy-appen: då håller"
          + " anläggningen varje fas under huvudsäkringen och laddning från nätet hålls inte längre tillbaka.",
      );
      expect(step("en", limit({}, { value_kw: null }))?.text).toBe(
        "Set the plant's import limit to at most 13.1 kW in the Sigenergy app: then the plant holds each phase under the"
          + " main fuse and grid charging is no longer held back.",
      );
    });

    it("shows nothing that cannot be acted on here, nor while control is off, nor beside a limit that guards", () => {
      for (const why of ["not_supported", "not_field_tested", "output_type_not_supported", "foreign_pv", "dc_charger",
                         "plant_not_three_phase", "sources_unknown", "a_code_from_later"]) {
        expect(step("sv", limit({ why })), why).toBeNull();
      }
      expect(step("sv", limit({ max_kw: null }))).toBeNull();
      expect(step("sv", limit({ why: "not_said_to_act" }), on({ battery_control_enabled: false }))).toBeNull();
      expect(step("sv", limit({ why: "not_said_to_act" }), on(), ["hold"])).toBeNull();
      expect(step("sv", limit({ why: "phase_control_off" }, { holds: true }))).toBeNull();
      expect(step("sv", { import_limit: null })).toBeNull();
      // A brand without the per-phase hold keeps the total rule's advice; one with it does not get it.
      const acts = on({ battery_control_import_limit_acts: true });
      expect(importLimitHint("sv", status(limit({ why: "phase_control_off" }), ["grid_charge"], acts))).toBeNull();
      expect(importLimitHint("sv", status(limit({ why: "not_field_tested" }), ["grid_charge"], acts))).toMatch(/4,4/);
    });

    it("reads a Home Assistant without the block or its entity, and ignores keys it does not know", () => {
      const older = fixture("checking.json");
      delete older["status"].grid_charge_cap.import_limit.per_phase;
      const decoded = decodeBatteryControl(older);
      expect(decoded.ok && decoded.value.status?.grid_charge_cap.import_limit?.per_phase).toBeNull();
      const noEntity = fixture("checking.json");
      delete noEntity["status"].grid_charge_cap.import_limit.per_phase.entity_id;
      const read = decodeBatteryControl(noEntity);
      expect(read.ok && read.value.status?.grid_charge_cap.import_limit?.per_phase?.entity_id).toBeNull();
    });

    it("has every line in every language", () => {
      for (const language of LANGUAGES) {
        const lines = [
          gridChargeCapLine(language, status({ cap_kw: null, rule: "import_limit", ...limit({ holds: true }, { holds: true }) })),
          ...["not_said_to_act", "output_type_unread", "phase_control_off", "grid_sensor_offline"].map(
            (why) => importLimitPhaseStep(language, status(limit({ why }), ["grid_charge"], on()))?.text ?? null,
          ),
          importLimitPhaseStep(language, status(limit({}), ["grid_charge"], on()))?.text ?? null,
          importLimitPhaseStep(language, status(limit({}, { value_kw: null }), ["grid_charge"], on()))?.text ?? null,
        ];
        for (const line of lines) {
          expect(line, language).not.toBeNull();
          expect(line, language).not.toContain("{");
        }
        expect(lines[0], language).toMatch(/4[.,]6.*13[.,]8/);
        expect(lines[5], language).toMatch(/13[.,]8.*13[.,]1/);
      }
    });
  });

  it("has the line in every language", () => {
    for (const language of LANGUAGES) {
      const line = gridChargeCapLine(language, status({}));
      expect(line, language).toMatch(/3[.,]2/);
      expect(line, language).not.toContain("{");
    }
  });
});

describe("the Home Assistant down acceptance", () => {
  const offered = { offered: true, held_kw: 0.4, empty_house_kw: 13.16 };
  const options = (more: Record<string, unknown> = {}) => ({
    options: { ...fixture("checking.json")["status"].options, battery_control_enabled: true, ...more },
  });

  it("is offered to an administrator only, only where the HA-down cap binds", () => {
    expect(haDownView("sv", status({ ha_down: offered }, ["grid_charge"], options()), true)).toEqual({
      kind: "row",
      help: "Nätladdningen av husbatteriet hålls till högst 0,4 kW, så att huvudsäkringen klarar sig även om Home Assistant stannar mitt i. Godkänner du risken får den gå upp till vad säkringen klarar med ett tomt hus.",
    });
    expect(haDownView("sv", status({ ha_down: offered }, ["grid_charge"], options()), false)).toBeNull();
    expect(haDownView("sv", status({ ha_down: { ...offered, offered: false } }, ["grid_charge"], options()), true)).toBeNull();
    expect(haDownView("sv", status({ ha_down: null }, ["grid_charge"], options()), true)).toBeNull();
  });

  it("once accepted, says so to everyone, and the cap line says the empty house's fuse", () => {
    const accepted = options({ battery_control_accept_ha_down: true });
    const view = haDownView("sv", status({ ha_down: { ...offered, offered: false } }, ["grid_charge"], accepted), false);
    expect(view).toEqual({
      kind: "line",
      text: "Godkänt att en säkring kan lösa ut om Home Assistant stannar eller tappar kontakten med batteriet eller elmätaren under en nätladdning av husbatteriet. Bara en administratör kan ändra det.",
    });
    expect(haDownView("sv", status({}, ["grid_charge"], accepted), true)?.kind).toBe("row");
    expect(gridChargeCapLine("sv", status({ rule: "accepted", cap_kw: 13.16 }, ["grid_charge"], accepted))).toBe(
      "Högst 13,2 kW från nätet: vad huvudsäkringen klarar med ett tomt hus (risken om Home Assistant stannar är godkänd).",
    );
  });

  it("reads a Home Assistant without the option or the block, and rejects a mistyped one", () => {
    const old = fixture("checking.json");
    delete old["status"].options.battery_control_accept_ha_down;
    delete old["status"].grid_charge_cap.ha_down;
    const decoded = decodeBatteryControl(old);
    expect(decoded.ok && decoded.value.status?.options.battery_control_accept_ha_down).toBe(false);
    expect(decoded.ok && decoded.value.status?.grid_charge_cap.ha_down).toBeNull();
    const mistyped = fixture("checking.json");
    mistyped["status"].grid_charge_cap.ha_down = { offered: "yes", held_kw: null, empty_house_kw: null };
    expect(decodeBatteryControl(mistyped).ok).toBe(false);
    const extra = fixture("checking.json");
    extra["status"].grid_charge_cap.ha_down = { offered: true, held_kw: 1, empty_house_kw: 2, more: 1 };
    expect(decodeBatteryControl(extra).ok).toBe(false);
  });

  it("has every line in every language, and Swedish never says a bare nätladda", () => {
    for (const language of LANGUAGES) {
      const view = haDownView(language, status({ ha_down: offered }, ["grid_charge"], options()), true);
      expect(view?.kind === "row" && view.help.includes("0")).toBe(true);
      expect(gridChargeCapLine(language, status({ rule: "accepted", cap_kw: 13.16 }))).not.toBeNull();
    }
    const sv = JSON.parse(readFileSync(join(__dirname, "..", "src", "i18n", "sv.json"), "utf8")) as Record<string, string>;
    for (const [key, text] of Object.entries(sv).filter(([key]) => key.startsWith("battery.haDown."))) {
      for (const match of text.matchAll(/nätladd\w*/gi)) {
        const after = text.slice((match.index ?? 0) + match[0].length);
        // "nätladdningen av husbatteriet", "nätladdar SpotNav husbatteriet": the home battery is named with it.
        expect(/^ (av |SpotNav )?husbatteriet/.test(after), `${key}: ${match[0]}`).toBe(true);
      }
    }
  });
});
