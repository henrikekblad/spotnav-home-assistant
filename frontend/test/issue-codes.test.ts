// The contract test: every code the backend can emit is known to the card, and every status code is
// worded in all five languages.
//
// The list is written by the backend from its real constants
// (`tests/test_issue_codes_fixture.py`) and read here from that same directory, with no copy.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { LANGUAGES, TRANSLATIONS, translate } from "../src/i18n";
import {
  ACTIVE_CONTROL_REASON_KEYS,
  EXECUTION_ERROR_KEYS,
  REASON_KEYS,
  STRATEGY_REASON_KEYS,
  activeControlReasonText,
  buildModel,
} from "../src/model";
import { STATUS_VARIANT_KEYS, STATUS_WORDING, issuesOf, lineText } from "../src/status";
import { STATUS_CODE_TABLE, STATUS_TONES, decodeDashboard, type StatusCode, type StatusLine } from "../src/validate";
import { rawDashboard, statusLine } from "./dashboard-fixtures";

const CODES = JSON.parse(
  readFileSync(join(__dirname, "..", "..", "tests", "fixtures", "codes", "dashboard_codes.json"), "utf8"),
) as Record<string, any>;

const NOW = Date.parse("2026-09-22T04:30:00Z");
const UNKNOWN = translate("en", "issue.unknown");
const FORMAT = { language: "en" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };

const STATUS_FIXTURE = CODES["status_codes"] as Record<string, { tone: string; params: string[] }>;
const STATUS_CODES = Object.keys(STATUS_FIXTURE).sort();

/** A representative value for a param kind, so a line can be worded in full. */
function sample(kind: string): unknown {
  switch (kind) {
    case "instant":
    case "instantOrNull":
      return "2026-09-22T05:15:00+00:00";
    case "number":
    case "numberOrNull":
      return 12.5;
    case "int":
      return 1234;
    case "codes":
      return ["amps", "phases"];
    case "choiceOrNull":
      return "until_tomorrow";
    case "text":
      return "SEK";
    default:
      return "some_reason";
  }
}

/** Entity ids and friendly names are shown as they are; any other text param is a code that must never be shown raw. */
function sampleFor(name: string, kind: string): unknown {
  if (name === "entity" || name === "entity_name") {
    return "sensor.sample_entity";
  }
  if (name === "entities") {
    return ["sensor.sample_entity"];
  }
  return sample(kind);
}

function fullLine(code: StatusCode): StatusLine {
  const kinds = STATUS_CODE_TABLE[code][1] as Record<string, string>;
  return { code, params: Object.fromEntries(Object.entries(kinds).map(([name, kind]) => [name, sampleFor(name, kind)])) } as StatusLine;
}

describe("every code the backend emits is known to the card", () => {
  it("has a fixture with every group", () => {
    expect(Object.keys(CODES).sort()).toEqual([
      "active_control_reasons",
      "control_reasons",
      "execution_errors",
      "hybrid_reasons",
      "planning_reasons",
      "planning_states",
      "price_reasons",
      "price_states",
      "solar_reasons",
      "status_codes",
      "status_tones",
      "strategy_reasons",
    ]);
  });

  it.each((CODES["control_reasons"] as string[]))("control reason %s is translated", (reason) => {
    expect(REASON_KEYS[reason], reason).toBeDefined();
  });

  it.each((CODES["execution_errors"] as string[]))("execution error %s is translated or generic", (code) => {
    // `install_failed`/`plan_unusable` deliberately use the generic execution sentence + technical code.
    expect(EXECUTION_ERROR_KEYS[code] ?? "control.executionError").toBeDefined();
  });

  it.each((CODES["strategy_reasons"] as string[]))("strategy reason %s is translated", (reason) => {
    expect(STRATEGY_REASON_KEYS[reason], reason).toBeDefined();
  });

  it.each((CODES["active_control_reasons"] as string[]))("active-control reason %s is translated", (reason) => {
    expect(activeControlReasonText("en", reason)?.text, reason).not.toBe(UNKNOWN);
    if (!reason.startsWith("site_measurement_")) {
      expect(ACTIVE_CONTROL_REASON_KEYS[reason], reason).toBeDefined();
    }
  });
});

describe("every status code the backend composes is decoded and worded", () => {
  it("knows exactly the tones the backend states", () => {
    expect([...STATUS_TONES].sort()).toEqual([...(CODES["status_tones"] as string[])].sort());
  });

  it("decodes exactly the codes the backend emits, with their tone and param names", () => {
    expect(Object.keys(STATUS_CODE_TABLE).sort()).toEqual(STATUS_CODES);
    for (const code of STATUS_CODES) {
      const [tone, kinds] = STATUS_CODE_TABLE[code as StatusCode] as readonly [string, Record<string, string>];
      expect(tone, code).toBe(STATUS_FIXTURE[code]?.tone);
      expect(Object.keys(kinds).sort(), code).toEqual([...(STATUS_FIXTURE[code]?.params ?? [])].sort());
    }
  });

  it.each(STATUS_CODES)("%s decodes in a whole dashboard, with the backend's tone and param names", (code) => {
    // Built from the backend's own fixture (its tone and its param names), not from the card's table: a
    // code, a param or a tone the card does not read fails here by name rather than as "cannot read".
    const entry = STATUS_FIXTURE[code]!;
    const kinds = (STATUS_CODE_TABLE as Record<string, readonly [string, Record<string, string>]>)[code]?.[1] ?? {};
    const params = Object.fromEntries(entry.params.map((name) => [name, sampleFor(name, kinds[name] ?? "text")]));
    const decoded = decodeDashboard(rawDashboard({ status: { tone: entry.tone, lines: [{ code, params }] } }));
    expect(decoded.ok, code).toBe(true);
  });

  it("says what ends a person's Stop: a plug-in where the charger reports one, else only a Start or a window", () => {
    const say = (ends: string) =>
      lineText({ code: "stopped_by_person", params: { ends } } as StatusLine, FORMAT, NOW);
    expect(say("replug")).toContain("plugged in again");
    expect(say("start")).not.toContain("plugged in");
    expect(say("start")).toContain("Start now");
  });

  it.each(STATUS_CODES)("%s has a wording in every language", (code) => {
    const key = STATUS_WORDING[code as StatusCode];
    expect(key, `no wording for status code ${code}`).toBeDefined();
    for (const language of LANGUAGES) {
      expect(TRANSLATIONS[language][key], `${language} ${code}`).toBeTruthy();
      const worded = lineText(fullLine(code as StatusCode), { ...FORMAT, language }, NOW);
      expect(worded, `${language} ${code}`).not.toBe("");
      // Every placeholder was filled from a typed param, never shown raw.
      expect(worded, `${language} ${code}`).not.toMatch(/\{\w+\}/u);
      expect(worded, `${language} ${code}`).not.toContain("some_reason");
    }
  });

  it("words a fact the composer left empty (null params) without a placeholder or a raw code", () => {
    for (const code of STATUS_CODES) {
      const empty = statusLine(code) as unknown as StatusLine;
      for (const language of LANGUAGES) {
        const worded = lineText(empty, { ...FORMAT, language }, NOW);
        expect(worded, `${language} ${code}`).not.toMatch(/\{\w+\}/u);
        expect(worded, `${language} ${code}`).not.toContain(code);
      }
    }
  });

  it("words a target stop by its basis and its reading's age", () => {
    const worded = (language: "sv" | "en", basis: string, age: number | null, soc = 81.1): string =>
      lineText(
        { code: "target_reached", params: { soc_percent: soc, basis, reading_age_s: age } } as unknown as StatusLine,
        { ...FORMAT, language },
        NOW,
      );
    expect(worded("sv", "reading", 600)).toBe("Stoppad vid 81 % (avläsningen 10 min gammal)");
    expect(worded("sv", "estimate", 600)).toBe("Stoppad vid 81 % (uppskattat, avläsningen 10 min gammal)");
    expect(worded("sv", "reading", 10)).toBe("Stoppad vid 81 % (nyss)");
    expect(worded("sv", "reading", null)).toBe("Stoppad vid 81 %");
    expect(worded("sv", "estimate", 7300)).toBe("Stoppad vid 81 % (uppskattat, avläsningen 2 h gammal)");
    expect(worded("en", "estimate", 30)).toBe("Stopped at 81 % (estimated)");
    expect(worded("en", "reading", 3700)).toBe("Stopped at 81 % (reading 1 h old)");
  });

  it("words missing settings as setup, naming the price area on its own", () => {
    const worded = (language: "sv" | "en", missing: string[] | null) =>
      lineText(
        { code: "settings_incomplete", params: { reason: "settings_missing", missing } } as unknown as StatusLine,
        { ...FORMAT, language },
        NOW,
      );
    expect(worded("en", ["area"])).toBe("Finish setting up: choose a price area in Settings.");
    expect(worded("sv", ["area"])).toBe("Slutför inställningen: välj ett prisområde i Inställningar.");
    expect(worded("en", ["phases", "amps"])).toBe("Finish setting up in Settings: phases, charging current.");
    expect(worded("en", [])).toBe("Finish setting up: some settings are still missing. Open Settings.");
    expect(worded("en", null)).toBe("Finish setting up: some settings are still missing. Open Settings.");
  });

  it("says once, neutrally, that the defaults came from Home Assistant", () => {
    const line = { code: "settings_suggested", params: { fields: ["area", "phases", "amps"] } } as unknown as StatusLine;
    expect(lineText(line, { ...FORMAT, language: "sv" }, NOW)).toBe(
      "Förslag utifrån din plats och laddare – kontrollera Inställningar.",
    );
    expect(lineText(line, { ...FORMAT, language: "en" }, NOW)).toBe(
      "Suggested from your location and charger – check Settings.",
    );
    expect(issuesOf({ tone: "normal", lines: [line] }, "en")).toEqual([]);
  });

  it("words every variant in every language", () => {
    for (const key of STATUS_VARIANT_KEYS) {
      for (const language of LANGUAGES) {
        expect(TRANSLATIONS[language][key], `${language} ${key}`).toBeTruthy();
      }
    }
  });

  it("lists as an issue only what the block itself marks, at the block's own severity", () => {
    for (const code of STATUS_CODES) {
      const tone = STATUS_FIXTURE[code]?.tone;
      const found = issuesOf({ tone: tone as "normal" | "notice" | "blocking", lines: [fullLine(code as StatusCode)] }, "en");
      if (tone === "normal") {
        expect(found, code).toEqual([]);
      } else {
        expect(found.map((entry) => entry.severity), code).toContain(tone);
        for (const entry of found) {
          expect(translate("en", entry.textKey, entry.params), code).not.toMatch(/\{\w+\}/u);
        }
      }
    }
  });
});

describe("a normal state is not shown as an error", () => {
  const modelFor = (status: unknown) => {
    const result = decodeDashboard(rawDashboard({ status }));
    if (!result.ok) {
      throw new Error("fixture did not decode");
    }
    return buildModel({ dashboard: result.value, language: "sv", nowMs: NOW });
  };

  it("waiting for tomorrow says so in words and raises no issue and no banner severity", () => {
    const model = modelFor({ tone: "normal", lines: [statusLine("waiting_for_tomorrow")] });
    expect(model.status).toBe("Väntar på morgondagens priser.");
    expect(model.issues).toEqual([]);
    expect(model.severity).toBeNull();
  });

  it("only a blocking block is blocking; a notice never reads as an error", () => {
    expect(modelFor({ tone: "notice", lines: [statusLine("unpriced")] }).severity).toBe("notice");
    expect(modelFor({ tone: "blocking", lines: [statusLine("planning_unavailable", { reason: "x" })] }).severity).toBe("blocking");
  });

  it("blocking issues have no dismiss control", () => {
    const source = readFileSync(join(__dirname, "..", "src", "card-view.ts"), "utf8");
    expect(source.toLowerCase()).not.toContain("dismiss(");
  });
});
