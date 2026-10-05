// A need the departure leaves too little time for: a plan with a warning, never the blocking
// "no plan with the data available"; and a named planning_unavailable reason worded as itself.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { issueText, issuesOf, lineText } from "../src/status";
import { buildModel } from "../src/model";
import { decodeDashboard, type StatusLine } from "../src/validate";
import { translate } from "../src/i18n";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const NOW = Date.parse("2026-09-22T07:58:00Z");
const FORMAT = { language: "en" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };

function shortfall(params: Record<string, unknown>): StatusLine {
  return { code: "departure_shortfall", params } as unknown as StatusLine;
}

describe("the best-effort plan's warning", () => {
  it("says the state of charge a target reaches by the departure, in English and Swedish", () => {
    const line = shortfall({ kwh: 7.36, requested_kwh: 11.79, soc_percent: 44.5, departure: "2026-09-22T10:00:00+00:00" });
    expect(lineText(line, FORMAT, NOW)).toBe("Will not be ready by the departure: about 44 % (7.4 kWh) by 12:00.");
    expect(lineText(line, { ...FORMAT, language: "sv" }, NOW)).toBe(
      "Hinner inte bli klar till avresan: cirka 44 % (7,4 kWh) till 12:00.",
    );
  });

  it("says the energy of a manual need, of the need it was asked for", () => {
    const line = shortfall({ kwh: 7.36, requested_kwh: 12, soc_percent: null, departure: "2026-09-22T10:00:00+00:00" });
    expect(lineText(line, FORMAT, NOW)).toBe("Will not be ready by the departure: about 7.4 of 12 kWh by 12:00.");
    expect(lineText(line, { ...FORMAT, language: "sv" }, NOW)).toBe(
      "Hinner inte bli klar till avresan: cirka 7,4 av 12 kWh till 12:00.",
    );
  });

  it("leaves the time out where it cannot be told, and is a notice in the dialog", () => {
    const line = shortfall({ kwh: 7.36, requested_kwh: 12, soc_percent: null, departure: null });
    expect(lineText(line, FORMAT, NOW)).toBe("Will not be ready by the departure: about 7.4 of 12 kWh.");
    const issues = issuesOf({ tone: "notice", lines: [line] }, "en");
    expect(issues.map((issue) => issue.severity)).toEqual(["notice"]);
    expect(issueText("en", issues[0]!)).toBe("Will not be ready by the departure: about 7.4 of 12 kWh.");
  });

  it("is what the field case's dashboard shows: a notice, with the plan, and no blocking sentence", () => {
    const decoded = decodeDashboard(
      JSON.parse(readFileSync(join(DASHBOARD_DIR, "target_soc_departure_shortfall.json"), "utf8")),
    );
    expect(decoded.ok).toBe(true);
    if (!decoded.ok) {
      return;
    }
    const model = buildModel({ dashboard: decoded.value, language: "en", nowMs: NOW });
    expect(model.severity).toBe("notice");
    expect(model.status).toContain("Will not be ready by the departure: about 44 % (7.4 kWh) by 12:00.");
    expect(model.status).not.toContain(translate("en", "issue.planningUnavailable"));
    expect(model.issues.map((issue) => issue.code)).toEqual(["departure_shortfall"]);
  });
});

describe("a planning_unavailable reason the backend names", () => {
  const worded = (reason: string | null) =>
    lineText({ code: "planning_unavailable", params: { reason } } as unknown as StatusLine, FORMAT, NOW);

  it("is worded as itself, never as the generic sentence", () => {
    const generic = translate("en", "issue.planningUnavailable");
    for (const reason of [
      "deadline_too_short",
      "no_published_prices",
      "missing_fx_rate",
      "unexpected_failure",
      "price_data_invalid",
      "insufficient_price_horizon",
      "solar_execution_unavailable",
    ]) {
      expect(worded(reason), reason).not.toBe(generic);
    }
    expect(worded("deadline_too_short")).toBe(
      "Not one whole quarter-hour is left before the departure, so nothing can be charged for it.",
    );
    const issues = issuesOf(
      { tone: "blocking", lines: [{ code: "planning_unavailable", params: { reason: "deadline_too_short" } } as unknown as StatusLine] },
      "sv",
    );
    expect(issueText("sv", issues[0]!)).toBe("Det finns inte en hel kvart kvar före avresan, så inget kan laddas till den.");
    expect(issues[0]!.technical).toBe("deadline_too_short");
  });

  it("keeps the generic sentence for a reason it has no words for", () => {
    expect(worded("something_new")).toBe(translate("en", "issue.planningUnavailable"));
    expect(worded(null)).toBe(translate("en", "issue.planningUnavailable"));
  });
});
