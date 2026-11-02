// The import target's status lines as the card words them: held, paused to the next hour, the house alone over the
// target, and the target set aside for a departure.

import { describe, expect, it } from "vitest";

import { translate } from "../src/i18n";
import { issuesOf, lineText } from "../src/status";
import type { StatusLine } from "../src/validate";

const NOW = Date.parse("2026-11-02T13:20:00Z");
const EN = { language: "en" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };
const SV = { ...EN, language: "sv" as const };

function line(code: string, params: Record<string, unknown>): StatusLine {
  return { code, params } as unknown as StatusLine;
}

describe("the import target's status lines", () => {
  it("says what the car is held to, the target and the hour so far", () => {
    const held = line("import_target_held", { limit_a: 7, target_kw: 6, hour_kw: 5.1 });
    expect(lineText(held, EN, NOW)).toBe("Held to 7 A to keep this hour under 6.0 kW (now 5.1 kW).");
    expect(lineText(held, SV, NOW)).toBe("Hålls på 7 A så att timmen stannar under 6,0 kW (nu 5,1 kW).");
    const unknown = line("import_target_held", { limit_a: null, target_kw: 6, hour_kw: 5.1 });
    expect(lineText(unknown, EN, NOW)).toBe("Held back to keep this hour under 6.0 kW (now 5.1 kW).");
  });

  it("says until when a pause lasts", () => {
    const paused = line("import_target_paused", { until: "2026-11-02T14:00:00+00:00", target_kw: 6, house_over: false });
    expect(lineText(paused, EN, NOW)).toBe("Paused until 15:00 to keep this hour under 6.0 kW.");
    expect(lineText(paused, SV, NOW)).toBe("Pausad till kl. 15:00 så att timmen stannar under 6,0 kW.");
  });

  it("says the house alone is over the target in the pause's own line, never that the hour stays under it", () => {
    const paused = line("import_target_paused", { until: "2026-11-02T20:00:00+00:00", target_kw: 6, house_over: true });
    expect(lineText(paused, SV, NOW)).toBe(
      "Pausad till kl. 21:00 – huset ensamt ligger över effektmålet på 6,0 kW den här timmen.",
    );
    expect(lineText(paused, EN, NOW)).toBe("Paused until 21:00 – the house alone is above the 6.0 kW target this hour.");
    expect(lineText(paused, EN, NOW)).not.toContain("stays under");
  });

  it("says when the house alone is over the target, and when the departure sets it aside", () => {
    expect(lineText(line("import_target_house_over", { target_kw: 6 }), EN, NOW)).toBe(
      "The house alone is above the 6.0 kW target this hour.",
    );
    const released = line("import_target_released", { target_kw: 6, departure: "2026-11-02T16:00:00+00:00" });
    expect(lineText(released, EN, NOW)).toBe("The import target is set aside by you until 17:00.");
    expect(lineText(released, SV, NOW)).toBe("Effektmålet är släppt av dig till kl. 17:00.");
    const automatic = line("import_target_released", { target_kw: 6, departure: "2026-11-02T16:00:00+00:00", automatic: true });
    expect(lineText(automatic, SV, NOW)).toBe(
      "Effektmålet släpps: bilen behöver effekten för att bli klar till avresan kl. 17:00.",
    );
  });

  it("says what the target makes the car miss, with the press that sets it aside where the plan asks", () => {
    const risk = line("import_target_at_risk", {
      departure: "2026-11-03T06:00:00+00:00",
      missing_kwh: 4,
      target_kw: 6,
      releasable: true,
    });
    expect(lineText(risk, SV, NOW)).toBe("Bilen blir inte klar till 07:00 under effektmålet 6,0 kW – 4 kWh saknas.");
    const [issue] = issuesOf({ tone: "notice", lines: [risk] }, "sv", undefined, SV, NOW);
    expect(issue?.fix?.action).toBe("release_import_target");
    expect(translate("sv", issue!.fix!.labelKey)).toBe("Släpp målet i natt");
    const held = line("import_target_at_risk", {
      departure: "2026-11-03T06:00:00+00:00",
      missing_kwh: 4,
      target_kw: 6,
      releasable: false,
    });
    expect(issuesOf({ tone: "notice", lines: [held] }, "sv", undefined, SV, NOW)[0]?.fix).toBeUndefined();
  });

  it("words an older line for the import target as the plain limit", () => {
    const older = line("load_balancing_limited", { limit_a: 7, phase: null, cause: "import_target" });
    expect(lineText(older, EN, NOW)).toBe(lineText(line("load_balancing_limited", { limit_a: 7, phase: null, cause: null }), EN, NOW));
  });
});

describe("the plan's choice when the car cannot be ready under the import target", () => {
  it("is asked in the Plan dialog only where the site has a target, and sent only once moved", async () => {
    const { settingsEditorBody } = await import("../src/settings-editor");
    const { decodeSettingsRecord, formFromRecord, replacementFor } = await import("../src/settings");
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const answer = JSON.parse(
      readFileSync(join(__dirname, "..", "..", "tests", "fixtures", "settings", "v1", "success.json"), "utf8"),
    );
    const record = decodeSettingsRecord(answer.settings);
    expect(record.if_not_ready).toBe("ask");
    const base = {
      kind: "plan" as const,
      readOnly: false,
      values: formFromRecord(record),
      energyReadOnly: false,
      phases: 3,
      currentRange: { min: 6, max: 16 },
      conflict: null,
      soc: null,
      vehicles: [],
    };
    const handlers = { onSave: () => undefined, onReload: () => undefined, onReapply: () => undefined };
    const without = settingsEditorBody(document, "sv", { ...base, importTarget: false } as never, handlers as never, "t1");
    expect(without.body.querySelector("[data-part='if-not-ready']")).toBeNull();
    const shown = settingsEditorBody(document, "sv", { ...base, importTarget: true } as never, handlers as never, "t2");
    const group = shown.body.querySelector("[data-part='if-not-ready']");
    expect(group?.textContent).toContain("Om bilen inte hinner bli klar");
    expect(group?.textContent).toContain("Släpp målet");
    // The same rows as the departure's "Every day / On a date" choice: a plain label, not a bold checkbox field.
    expect(group?.querySelectorAll(":scope > label.spotnav-site-choice > input[type='radio']")).toHaveLength(3);
    const values = { ...formFromRecord(record), ifNotReady: "hold" };
    const moved = replacementFor("plan", record, values);
    expect(moved.ok && moved.body.if_not_ready).toBe("hold");
    const kept = replacementFor("plan", record, formFromRecord(record));
    expect(kept.ok && kept.body.if_not_ready).toBe("ask");
  });
});


describe("the press beside the import target's shortfall", () => {
  it("keeps the issue banner, so any user reaches Release tonight", async () => {
    const { bannerRepeatsStatus } = await import("../src/card-view");
    const risk = line("import_target_at_risk", {
      departure: "2026-11-03T06:00:00+00:00",
      missing_kwh: 4,
      target_kw: 6,
      releasable: true,
    });
    const issues = issuesOf({ tone: "notice", lines: [risk] }, "sv", undefined, SV, NOW);
    const model = { status: lineText(risk, SV, NOW), issues, language: "sv" } as never;
    expect(bannerRepeatsStatus(model, "notice")).toBe(false);
  });
});
