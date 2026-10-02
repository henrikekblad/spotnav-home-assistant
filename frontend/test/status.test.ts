// The status line is a pure renderer of Home Assistant's status block: order, tone and facts come
// from the block, and this card only words and formats them.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { statusNote, statusText } from "../src/status";
import type { Status } from "../src/validate";
import { statusLine } from "./dashboard-fixtures";

const NOW = Date.parse("2026-09-22T08:00:00Z");
const format = (language: "sv" | "en" | "nb" | "da" | "fi", timeZone = "Europe/Stockholm") =>
  ({ language, timeZone, unit: "öre", currency: "SEK", majorUnit: "kr" }) as const;
const block = (...lines: Array<ReturnType<typeof statusLine>>): Status =>
  ({ tone: "normal", lines }) as unknown as Status;

describe("the status line renders the block and nothing else", () => {
  it("keeps the suggestion note out of the plan line and gives it its own", () => {
    const status = block(
      statusLine("auto_planned", { start: "2026-09-22T04:00:00+00:00" }),
      statusLine("plan_energy", { kwh: 22.2 }),
      statusLine("settings_suggested", { fields: ["area"] }),
    );
    expect(statusText(status, format("en"), NOW)).toBe("Planned from 06:00 · 22.2 kWh");
    expect(statusNote(status, format("en"), NOW)).toBe("Suggested from your location and charger – check Settings.");
    expect(statusNote(status, format("sv"), NOW)).toBe("Förslag utifrån din plats och laddare – kontrollera Inställningar.");
    expect(statusText(block(statusLine("settings_suggested", { fields: ["area"] })), format("en"), NOW)).toBeNull();
    expect(statusNote(block(statusLine("no_plan")), format("en"), NOW)).toBeNull();
  });

  it("joins the headline and its facts, in the block's own order, with the full stop dropped", () => {
    const status = block(
      statusLine("auto_planned", { start: "2026-09-22T04:00:00+00:00" }),
      statusLine("plan_energy", { kwh: 20 }),
      statusLine("plan_cost", { amount_minor: 2250, currency: "SEK" }),
      statusLine("plan_distance", { mil: 8.5 }),
    );
    expect(statusText(status, format("en"), NOW)).toBe("Planned from 06:00 · 20 kWh · 22.5 kr · 85 km");
    expect(statusText(status, format("sv"), NOW)).toBe("Planerat från 06:00 · 20 kWh · 22,5 kr · 8,5 mil");
    // The same lines in another order are read in that order: there is no ranking here.
    const swapped = block(statusLine("plan_energy", { kwh: 20 }), statusLine("no_plan"));
    expect(statusText(swapped, format("en"), NOW)).toBe("20 kWh · No charging plan could be calculated right now.");
  });

  it("words waiting on history with the weekday's plural, the saving and the weeks, in every language", () => {
    const waiting = block(statusLine("waiting_for_history", { weekday: 7, percent: 30, weeks: 4 }));
    expect(statusText(waiting, format("en"), NOW)).toBe("Waiting: Sundays were 30 % cheaper the last 4 weeks.");
    expect(statusText(waiting, format("sv"), NOW)).toBe("Väntar: söndagar har varit 30 % billigare de senaste 4 veckorna.");
    expect(statusText(waiting, format("nb"), NOW)).toBe("Venter: søndager har vært 30 % billigere de siste 4 ukene.");
    expect(statusText(waiting, format("da"), NOW)).toBe("Venter: søndage har været 30 % billigere de seneste 4 uger.");
    expect(statusText(waiting, format("fi"), NOW)).toBe(
      "Odotetaan: sunnuntaisin on ollut 30 % halvempaa viimeisten 4 viikon aikana.",
    );
  });

  it("names the weekday of the market's own week, whatever the card's zone", () => {
    const monday = block(statusLine("waiting_for_history", { weekday: 1, percent: 12, weeks: 3 }));
    expect(statusText(monday, format("en", "Pacific/Auckland"), NOW)).toBe(
      "Waiting: Mondays were 12 % cheaper the last 3 weeks.",
    );
  });

  it("says the plain fact when the composer left a number out, never a placeholder", () => {
    for (const params of [
      { weekday: null, percent: 30, weeks: 4 },
      { weekday: 7, percent: null, weeks: 4 },
      { weekday: 7, percent: 30, weeks: null },
      { weekday: 9, percent: 30, weeks: 4 },
    ]) {
      const text = statusText(block(statusLine("waiting_for_history", params)), format("en"), NOW);
      expect(text).toBe("Waiting for hours that usually cost less, will plan then.");
    }
  });

  it("says nothing for an idle block", () => {
    expect(statusText(block(), format("en"), NOW)).toBeNull();
    expect(statusText(null, format("en"), NOW)).toBeNull();
  });

  it("words charging now with and without an end, and never invents one", () => {
    const open = block(statusLine("charging_now"));
    expect(statusText(open, format("sv"), NOW)).toBe("Laddar nu.");
    expect(statusText(open, format("en"), NOW)).toBe("Charging now.");
    const bounded = block(statusLine("charging_now", { until: "2026-09-22T09:00:00+00:00" }));
    expect(statusText(bounded, format("sv"), NOW)).toBe("Laddar nu; schemalagd till 11:00.");
    expect(statusText(bounded, format("en", ""), NOW)).toBe("Charging now.");
  });

  it("formats money from the minor unit and currency, in the market's unit only for its own currency", () => {
    const own = block(statusLine("plan_cost", { amount_minor: 0, currency: "SEK" }));
    expect(statusText(own, format("en"), NOW)).toBe("0 kr");
    const other = block(statusLine("plan_cost", { amount_minor: 1234, currency: "EUR" }));
    expect(statusText(other, format("sv"), NOW)).toBe("12,34 EUR");
  });

  it("words nothing to charge, a pause and load balancing", () => {
    expect(statusText(block(statusLine("nothing_to_charge")), format("sv"), NOW)).toBe("Inget att ladda just nu.");
    expect(statusText(block(statusLine("paused", { until: null })), format("en"), NOW)).toBe("Paused until you resume.");
    expect(statusText(block(statusLine("paused", { until: "2026-09-22T09:00:00+00:00" })), format("en"), NOW)).toBe(
      "Paused until 11:00.",
    );
    expect(statusText(block(statusLine("load_balancing_limited", { limit_a: 10 })), format("en"), NOW)).toBe(
      "Charging is limited to 10 A by the site's load balancing.",
    );
    expect(statusText(block(statusLine("load_balancing_limited")), format("en"), NOW)).toBe(
      "Charging is limited by the site's load balancing right now.",
    );
  });

  it("words solar and hybrid, with the window in the market's clock", () => {
    expect(statusText(block(statusLine("solar_charging", { requested_a: 8 })), format("en"), NOW)).toBe(
      "Solar · charging 8 A from surplus",
    );
    expect(statusText(block(statusLine("solar_waiting_for_sun")), format("sv"), NOW)).toBe("Sol · väntar på sol");
    const hybrid = block(
      statusLine("hybrid_grid", {
        grid_kwh: 20,
        credit_kwh: 4,
        window_start: "2026-09-22T10:15:00+00:00",
        window_end: "2026-09-22T13:15:00+00:00",
      }),
    );
    expect(statusText(hybrid, format("en"), NOW)).toBe("Hybrid · 20 kWh from grid 12:15–15:15, 4 kWh expected from sun");
  });

  it("holds no precedence of its own: the card's old judgement code is gone", () => {
    const model = readFileSync(join(__dirname, "..", "src", "model.ts"), "utf8");
    for (const gone of ["statusSentence", "strategyStatusSentence", "issuesFor", "isNormalHorizonGap", "PLANNING_REASON_CLASS"]) {
      expect(model, gone).not.toContain(gone);
    }
  });
});
