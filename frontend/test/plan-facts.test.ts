// The two facts the status line gained: what a plan saves against charging now, and when the sun is expected
// (hybrid's credit and the solar strategy's forecast), worded in the market's clock and the reader's language.

import { describe, expect, it } from "vitest";

import { LANGUAGES } from "../src/i18n";
import { registerAllLanguages } from "../src/i18n/all";
import { statusText } from "../src/status";
import { decodeDashboard, type Status } from "../src/validate";
import { rawDashboard, statusLine } from "./dashboard-fixtures";

registerAllLanguages();

const NOW = Date.parse("2026-09-22T08:00:00Z");
const format = (language: (typeof LANGUAGES)[number], timeZone = "Europe/Stockholm", currency = "SEK") =>
  ({ language, timeZone, unit: "öre", currency, majorUnit: currency === "SEK" ? "kr" : null }) as const;
const block = (...lines: Array<ReturnType<typeof statusLine>>): Status =>
  ({ tone: "normal", lines }) as unknown as Status;

const PLANNED = [
  statusLine("auto_planned", { start: "2026-09-22T23:00:00+00:00" }),
  statusLine("plan_energy", { kwh: 20 }),
  statusLine("plan_cost", { amount_minor: 3294, currency: "SEK" }),
  statusLine("plan_saving", { amount_minor: 1810, currency: "SEK" }),
];

describe("the saving against charging now", () => {
  it("follows the cost, about and to one decimal", () => {
    expect(statusText(block(...PLANNED), format("en"), NOW)).toBe(
      "Planned from 01:00 · 20 kWh · 32.94 kr · saves ~18.1 kr against charging now",
    );
    expect(statusText(block(...PLANNED), format("sv"), NOW)).toBe(
      "Planerat från 01:00 · 20 kWh · 32,94 kr · sparar ~18,1 kr mot att ladda nu",
    );
  });

  it("is said in every language with the amount in it", () => {
    for (const language of LANGUAGES) {
      const text = statusText(block(PLANNED[3]!), format(language), NOW) ?? "";
      expect(text, language).toMatch(/18[.,]1/);
      expect(text, language).not.toContain("{");
    }
  });

  it("is in pounds where the plan is", () => {
    const line = statusLine("plan_saving", { amount_minor: 125, currency: "GBP" });
    expect(statusText(block(line), format("en", "Europe/London", "GBP"), NOW)).toBe("saves ~£1.25 against charging now");
  });
});

describe("when the sun is expected", () => {
  const hybrid = (sun: { sun_start: string | null; sun_end: string | null }) =>
    block(
      statusLine("hybrid_grid", {
        grid_kwh: 20,
        credit_kwh: 4,
        window_start: "2026-09-22T22:15:00+00:00",
        window_end: "2026-09-23T01:15:00+00:00",
        ...sun,
      }),
    );

  it("gives hybrid's credit its hours", () => {
    const sunny = hybrid({ sun_start: "2026-09-23T08:00:00+00:00", sun_end: "2026-09-23T13:00:00+00:00" });
    expect(statusText(sunny, format("en"), NOW)).toBe(
      "Hybrid · 20 kWh from grid 00:15–03:15, 4 kWh expected from sun 10:00–15:00",
    );
    expect(statusText(sunny, format("sv"), NOW)).toBe(
      "Hybrid · 20 kWh från elnätet 00:15–03:15, 4 kWh väntas från solen 10:00–15:00",
    );
  });

  it("keeps hybrid's line as it was without the hours", () => {
    expect(statusText(hybrid({ sun_start: null, sun_end: null }), format("en"), NOW)).toBe(
      "Hybrid · 20 kWh from grid 00:15–03:15, 4 kWh expected from sun",
    );
  });

  it("tells the solar strategy's sun for today, or tomorrow", () => {
    const today = statusLine("solar_forecast", { kwh: 6.25, start: "2026-09-22T09:00:00+00:00", end: "2026-09-22T14:00:00+00:00" });
    const tomorrow = statusLine("solar_forecast", { kwh: 3, start: "2026-09-23T07:00:00+00:00", end: "2026-09-23T10:00:00+00:00" });
    const headline = statusLine("solar_waiting_for_sun");
    expect(statusText(block(headline, today), format("en"), NOW)).toBe(
      "Solar · waiting for sun · 6.3 kWh expected from the sun 11:00–16:00",
    );
    expect(statusText(block(headline, today), format("sv"), NOW)).toBe(
      "Sol · väntar på sol · 6,3 kWh väntas från solen 11:00–16:00",
    );
    expect(statusText(block(tomorrow), format("en"), NOW)).toBe("3 kWh expected from the sun tomorrow 09:00–12:00");
    expect(statusText(block(tomorrow), format("sv"), NOW)).toBe("3 kWh väntas från solen i morgon 09:00–12:00");
    expect(statusText(block(tomorrow), format("en", ""), NOW)).toBe("3 kWh expected from the sun");
  });

  it("decides today in the market's day, across the autumn clock change", () => {
    // 2026-10-25 is 25 hours in Stockholm; 22:30Z is 23:30 local, still the 25th.
    const now = Date.parse("2026-10-25T10:00:00Z");
    const late = statusLine("solar_forecast", { kwh: 1, start: "2026-10-25T22:00:00+00:00", end: "2026-10-25T22:30:00+00:00" });
    expect(statusText(block(late), format("en"), now)).toBe("1 kWh expected from the sun 23:00–23:30");
  });

  it("is in every language", () => {
    for (const language of LANGUAGES) {
      const line = statusLine("solar_forecast", { kwh: 3, start: "2026-09-23T07:00:00+00:00", end: "2026-09-23T10:00:00+00:00" });
      const text = statusText(block(line), format(language), NOW) ?? "";
      expect(text, language).toMatch(/09[:.]00/);
      expect(text, language).not.toContain("{");
    }
  });
});

describe("the decoder", () => {
  it("reads the new lines and refuses one that lacks a fact", () => {
    const status = {
      tone: "normal",
      lines: [
        { code: "plan_saving", params: { amount_minor: 100, currency: "SEK" } },
        { code: "solar_forecast", params: { kwh: 2, start: "2026-09-23T07:00:00+00:00", end: "2026-09-23T10:00:00+00:00" } },
      ],
    };
    expect(decodeDashboard(rawDashboard({ status })).ok).toBe(true);
    const missing = { tone: "normal", lines: [{ code: "solar_forecast", params: { kwh: 2, start: null, end: null } }] };
    expect(decodeDashboard(rawDashboard({ status: missing })).ok).toBe(false);
  });
});
