// The neutral "Good to know" banner is left out when the status headline already says what it lists.

import { describe, expect, it } from "vitest";

import { bannerRepeatsStatus } from "../src/card-view";
import { issuesOf, statusLineCodes, statusText } from "../src/status";
import type { StatusLine } from "../src/validate";

const EN = { language: "sv" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };
const NOW = Date.parse("2026-11-02T13:20:00Z");

function line(code: string, params: Record<string, unknown> = {}): StatusLine {
  return { code, params } as unknown as StatusLine;
}

function modelOf(lines: StatusLine[]) {
  const status = { tone: "notice", lines } as never;
  return {
    language: "sv",
    status: statusText(status, EN, NOW),
    statusCodes: statusLineCodes(status),
    issues: issuesOf(status, "sv", undefined, EN, NOW),
  } as never;
}

describe("a neutral banner and the status headline", () => {
  it("is left out when the headline is the notice, also when its sentence has a middle dot inside", () => {
    // "Sol · ingen mätning av nätets effekt …" is one line of the headline, not two.
    const model = modelOf([line("solar_no_grid_power", { entity: null, entity_name: null })]);
    expect(bannerRepeatsStatus(model, "notice")).toBe(true);
    expect(bannerRepeatsStatus(modelOf([line("unpriced")]), "notice")).toBe(true);
  });

  it("is left out when every one of several notices is a line of the headline", () => {
    expect(bannerRepeatsStatus(modelOf([line("unpriced"), line("price_data_stale", { reason: null })]), "notice")).toBe(true);
  });

  it("stays when it lists something the headline does not (the missing settings)", () => {
    expect(bannerRepeatsStatus(modelOf([line("settings_incomplete", { reason: null, missing: ["amps"] })]), "notice")).toBe(false);
  });

  it("stays when blocking, and when there is no headline", () => {
    expect(bannerRepeatsStatus(modelOf([line("unpriced")]), "blocking")).toBe(false);
    expect(bannerRepeatsStatus({ ...(modelOf([line("unpriced")]) as object), status: null } as never, "notice")).toBe(false);
  });
});
