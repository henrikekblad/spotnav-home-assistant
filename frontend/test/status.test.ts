// The status line is a pure renderer of Home Assistant's status block: order, tone and facts come
// from the block, and this card only words and formats them.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { issuesOf, issueText, statusNote, statusText } from "../src/status";
import type { Status } from "../src/validate";
import { statusLine } from "./dashboard-fixtures";

const NOW = Date.parse("2026-09-22T08:00:00Z");
const format = (language: "sv" | "en" | "nb" | "da" | "fi", timeZone = "Europe/Stockholm") =>
  ({ language, timeZone, unit: "öre", currency: "SEK", majorUnit: "kr" }) as const;
const block = (...lines: Array<ReturnType<typeof statusLine>>): Status =>
  ({ tone: "normal", lines }) as unknown as Status;

describe("the status line renders the block and nothing else", () => {
  it("says a charger kept charging after SpotNav's stops, in every language", () => {
    const status = block(statusLine("charger_ignores_stop"));
    expect(statusText(status, format("en"), NOW)).toContain("keeps charging although it was stopped");
    expect(statusText(status, format("sv"), NOW)).toContain("Laddaren fortsätter ladda fast den stoppades");
    for (const language of ["da", "nb", "fi"] as const) {
      expect(statusText(status, format(language), NOW)).toContain("SpotNav");
    }
  });

  it("explains a charger whose own enable switch is off, in every language", () => {
    const status = block(statusLine("waiting_for_tomorrow"), statusLine("charger_disabled"));
    expect(statusText(status, format("en"), NOW)).toBe(
      "Waiting for tomorrow's prices · The charger's own enable switch is off, so it cannot start. Turn it on in the charger's settings.",
    );
    expect(statusText(status, format("sv"), NOW)).toContain(
      "Laddarens egen aktiveringsbrytare är av, så den kan inte starta. Slå på den i laddarens inställningar.",
    );
  });

  it("words a hold until the planned start and a person's override, in every language", () => {
    const held = block(statusLine("held_until_window", { time: "2026-09-22T22:00:00+00:00" }));
    expect(statusText(held, format("en"), NOW)).toContain("Charging waits for the planned start at");
    expect(statusText(held, format("sv"), NOW)).toContain("Laddningen väntar till planerad start kl.");
    for (const language of ["da", "fi", "nb"] as const) {
      expect(statusText(held, format(language), NOW)).not.toContain("{time}");
    }
    const overridden = block(statusLine("charging_now"), statusLine("hold_overridden"));
    expect(statusText(overridden, format("en"), NOW)).toContain(
      "Charging was started outside the plan and is allowed to continue.",
    );
    expect(statusText(overridden, format("sv"), NOW)).toContain(
      "Laddningen startades utanför planen och får fortsätta.",
    );
  });

  it("says how a manual need is counted without the energy meter, in every language", () => {
    const kept = block(statusLine("auto_installed", { start: "2026-09-22T22:00:00+00:00" }), statusLine("remaining_need_estimated", { kwh: 6.4, basis: "kept" }));
    expect(statusText(kept, format("en"), NOW)).toContain("The energy meter cannot be read: 6.4 kWh remains, from its last reading.");
    expect(statusText(kept, format("sv"), NOW)).toContain("Energimätaren kan inte läsas: 6,4 kWh återstår enligt dess senaste värde.");
    const sessions = block(statusLine("remaining_need_estimated", { kwh: 3, basis: "sessions" }));
    expect(statusText(sessions, format("en"), NOW)).toBe(
      "No energy meter: 3 kWh remains, counted from this charger's recorded charges.",
    );
    expect(statusText(sessions, format("sv"), NOW)).toContain("Ingen energimätare: 3 kWh återstår");
    for (const language of ["da", "fi", "nb"] as const) {
      expect(statusText(kept, format(language), NOW)).not.toContain("{kwh}");
      expect(statusText(sessions, format(language), NOW)).not.toContain("{kwh}");
    }
    const issues = issuesOf({ tone: "notice", lines: [statusLine("remaining_need_estimated", { kwh: 3, basis: "sessions" })] } as unknown as Status, "en");
    expect(issues.map((issue) => [issue.severity, issueText("en", issue)])).toEqual([
      ["notice", "No energy meter: 3 kWh remains, counted from this charger's recorded charges."],
    ]);
  });

  it("says why solar has no full basis and names the meter's unavailable sensors, in every language", () => {
    const field = block(
      statusLine("solar_waiting_for_sun"),
      statusLine("solar_charger_current_missing", { entity: null }),
      statusLine("solar_site_incomplete", { phases: ["L1"] }),
      statusLine("site_meter_unavailable", { entities: ["sensor.solax_grid_current_l1"], cause: "inverter_standby" }),
    );
    expect(statusText(field, format("en"), NOW)).toBe(
      "Solar · waiting for sun · The charger's own current is not set — choose it under Site wiring (e.g. Easee Current); until then solar starts only at the minimum current · The site measurement is incomplete (L1) — solar runs on total grid power only · The meter's sensors are unavailable (the inverter may be in standby): sensor.solax_grid_current_l1.",
    );
    expect(statusText(field, format("sv"), NOW)).toContain(
      "Laddarens egen ström är inte vald — välj den under Anläggningens koppling (t.ex. Easee Current)",
    );
    expect(statusText(field, format("sv"), NOW)).toContain(
      "Mätarens sensorer är otillgängliga (växelriktaren kan vara i viloläge): sensor.solax_grid_current_l1.",
    );
    const grid = block(statusLine("solar_no_grid_power", { entity: null }));
    expect(statusText(grid, format("en"), NOW)).toBe("Solar · no grid power reading — set Total grid power under Site entities");
    const unreadable = block(statusLine("solar_no_grid_power", { entity: "sensor.net" }));
    expect(statusText(unreadable, format("sv"), NOW)).toBe("Sol · nätets totala effekt (sensor.net) har ingen färsk mätning");
    // A friendly name is shown in place of the raw id, in the lines that name an entity.
    const named = block(
      statusLine("solar_charger_current_missing", { entity: "sensor.halo_current", entity_name: "HALO current" }),
      statusLine("site_meter_unavailable", {
        entities: ["sensor.a", "sensor.b"],
        cause: "meter_unavailable",
        entity_names: ["Meter A", "Meter B"],
      }),
    );
    const namedText = statusText(named, format("en"), NOW);
    expect(namedText).toContain("HALO current");
    expect(namedText).toContain("Meter A, Meter B");
    expect(namedText).not.toContain("sensor.");
    for (const language of ["da", "fi", "nb"] as const) {
      expect(statusText(field, format(language), NOW)).not.toMatch(/\{\w+\}/u);
    }
    // The missing current and the meter are items to review; the incomplete site is a normal fact.
    expect(issuesOf({ ...field, tone: "notice" }, "en").map((issue) => issue.code)).toEqual([
      "solar_charger_current_missing",
      "site_meter_unavailable",
    ]);
    expect(issueText("en", issuesOf({ ...field, tone: "notice" }, "en")[1]!)).toBe(
      "The meter's sensors are unavailable (the inverter may be in standby): sensor.solax_grid_current_l1.",
    );
  });

  it("says when a manual need is capped at the battery's room, and that the car ends a charge to its own limit", () => {
    const capped = block(statusLine("auto_installed", { start: "2026-09-22T22:00:00+00:00" }), statusLine("need_limited_by_room", { kwh: 3.44 }));
    expect(statusText(capped, format("en"), NOW)).toContain("Limited to 3.4 kWh: the car is almost full.");
    expect(statusText(capped, format("sv"), NOW)).toContain("Begränsat till 3,4 kWh: bilen är nästan full.");
    const toLimit = block(statusLine("charging_now", { until: null }), statusLine("charging_to_vehicle_limit", { percent: 100 }));
    expect(statusText(toLimit, format("en"), NOW)).toContain("Charging until the car stops at its own limit (100 %).");
    expect(statusText(toLimit, format("sv"), NOW)).toContain("Laddar tills bilen stoppar vid sin egen laddgräns (100 %).");
    for (const language of ["da", "fi", "nb"] as const) {
      expect(statusText(capped, format(language), NOW)).not.toContain("{kwh}");
      expect(statusText(toLimit, format(language), NOW)).not.toContain("{percent}");
    }
    // Normal facts, never an item to review.
    expect(issuesOf(capped, "en")).toEqual([]);
    expect(issuesOf(toLimit, "en")).toEqual([]);
  });

  it("says a car finishing past the plan's last window charges until it is full, at most until a time", () => {
    const topOff = block(
      statusLine("topping_off", { until: "2026-09-22T21:40:00+00:00" }),
      statusLine("charging_to_vehicle_limit", { percent: 80 }),
    );
    const en = statusText(topOff, format("en"), NOW);
    expect(en).toContain("Charging until the car is full (at most until 23:40)");
    expect(en).toContain("Charging until the car stops at its own limit (80 %).");
    expect(statusText(topOff, format("sv"), NOW)).toContain("Laddar tills bilen är full (som längst till 23:40)");
    for (const language of ["da", "fi", "nb"] as const) {
      expect(statusText(topOff, format(language), NOW)).not.toContain("{time}");
    }
    expect(issuesOf(topOff, "en")).toEqual([]);
  });

  it("names the phases that make the site measurement unusable, and where they are read from", () => {
    const empty = block(
      statusLine("waiting_for_tomorrow"),
      statusLine("site_measurement_problem", {
        no_value_phases: ["L2", "L3"],
        no_value_entities: ["sensor.tibber_pulse_hus_current_l2", "sensor.tibber_pulse_hus_current_l3"],
        stale_phases: [],
        max_age_s: 120,
      }),
    );
    expect(statusText(empty, format("en"), NOW)).toBe(
      "Waiting for tomorrow's prices · L2 and L3 have no value (sensor.tibber_pulse_hus_current_l2, sensor.tibber_pulse_hus_current_l3).",
    );
    expect(statusText(empty, format("sv"), NOW)).toContain(
      "L2 och L3 saknar värde (sensor.tibber_pulse_hus_current_l2, sensor.tibber_pulse_hus_current_l3).",
    );
    const stale = block(
      statusLine("site_measurement_problem", {
        no_value_phases: [],
        no_value_entities: [],
        stale_phases: ["L1"],
        max_age_s: 120,
      }),
    );
    expect(statusText(stale, format("en"), NOW)).toBe("L1 is older than 120 s.");
    expect(statusText(stale, format("sv"), NOW)).toBe("L1 är äldre än 120 s.");
    for (const language of ["nb", "da", "fi"] as const) {
      const said = statusText(stale, format(language), NOW) ?? "";
      expect(said).toContain("L1");
      expect(said).not.toMatch(/[{}]/u);
    }
    // The issue list says the same sentence, and both causes together read as two sentences.
    const both = statusLine("site_measurement_problem", {
      no_value_phases: ["L2"],
      no_value_entities: ["sensor.l2"],
      stale_phases: ["L1"],
      max_age_s: 120,
    });
    const issues = issuesOf({ tone: "notice", lines: [both] } as unknown as Status, "en");
    expect(issues.map((issue) => issueText("en", issue))).toEqual([
      "L2 has no value (sensor.l2). L1 is older than 120 s.",
    ]);
  });

  it("names the other entry when two chargers are the same charger", () => {
    const status = block(statusLine("waiting_for_tomorrow"), statusLine("duplicate_charger", { other: "Garage Easee" }));
    expect(statusText(status, format("en"), NOW)).toContain("Garage Easee and this charger are the same physical charger.");
    expect(statusText(status, format("sv"), NOW)).toContain("Garage Easee och den här laddaren är samma fysiska laddare.");
    const issues = issuesOf({ tone: "notice", lines: [statusLine("duplicate_charger", { other: "Garage Easee" })] } as unknown as Status, "en");
    expect(issues.map((issue) => issueText("en", issue))[0]).toContain("Garage Easee and this charger");
  });

  it("names the charge control that is gone or disabled, and keeps the general wording otherwise", () => {
    const gone = block(statusLine("charger_unavailable", { problem: "control_missing", entity: "switch.garage" }));
    for (const language of ["en", "sv", "da", "fi", "nb"] as const) {
      expect(statusText(gone, format(language), NOW)).toContain("switch.garage");
      const disabled = block(statusLine("charger_unavailable", { problem: "control_disabled", entity: "switch.garage" }));
      expect(statusText(disabled, format(language), NOW)).toContain("switch.garage");
      expect(statusText(disabled, format(language), NOW)).not.toBe(statusText(gone, format(language), NOW));
    }
    const issues = issuesOf({ tone: "blocking", lines: gone.lines } as unknown as Status, "en");
    expect(issues.map((issue) => issueText("en", issue))[0]).toContain("no longer exists");
    expect(statusText(block(statusLine("charger_unavailable")), format("en"), NOW)).toContain("not usable");
  });

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

  it("names why load balancing holds the car below its plan, where the server knows", () => {
    const battery = block(statusLine("load_balancing_limited", { limit_a: 11, cause: "battery_shares_fuse" }));
    const house = block(statusLine("load_balancing_limited", { limit_a: 11, cause: "house_consumption" }));
    expect(statusText(battery, format("en"), NOW)).toBe(
      "The home battery charges from the grid and shares the main fuse: the car gets 11 A.",
    );
    expect(statusText(battery, format("sv"), NOW)).toBe(
      "Hemmabatteriet laddar från nätet och delar huvudsäkringen: bilen får 11 A.",
    );
    expect(statusText(house, format("en"), NOW)).toBe("House consumption limits the car to 11 A.");
    expect(statusText(house, format("sv"), NOW)).toBe("Hushållets förbrukning begränsar bilen till 11 A.");
    // An unknown cause keeps the plain wording, and the issue list words the cause too.
    const unknown = block(statusLine("load_balancing_limited", { limit_a: 11, cause: "something_new" }));
    expect(statusText(unknown, format("en"), NOW)).toBe("Charging is limited to 11 A by the site's load balancing.");
    expect(issueText("en", issuesOf(battery, "en")[0]!)).toBe(
      "The home battery charges from the grid and shares the main fuse: the car gets 11 A.",
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

  it("shows the price wait after a strategy headline", () => {
    const waiting = block(
      statusLine("hybrid_grid", { grid_kwh: 20, credit_kwh: 4, window_start: null, window_end: null }),
      statusLine("waiting_for_publication", { publication_at: "2026-09-22T11:00:00+00:00" }),
    );
    const text = statusText(waiting, format("en"), NOW) ?? "";
    expect(text.startsWith("Hybrid · 20 kWh from grid")).toBe(true);
    expect(text).toContain(" · ");
    expect(text.toLowerCase()).toContain("13:00");
  });

  it("holds no precedence of its own: the card's old judgement code is gone", () => {
    const model = readFileSync(join(__dirname, "..", "src", "model.ts"), "utf8");
    for (const gone of ["statusSentence", "strategyStatusSentence", "issuesFor", "isNormalHorizonGap", "PLANNING_REASON_CLASS"]) {
      expect(model, gone).not.toContain(gone);
    }
  });
});
