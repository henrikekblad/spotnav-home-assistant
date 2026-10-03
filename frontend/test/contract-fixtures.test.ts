// The cross-language contract fixtures: the backend's own payloads, decoded by the card.
//
// These files are written by the Python serializer and pinned there
// (`tests/test_dashboard_fixtures.py`), and
// they are read from that same directory here. There is deliberately **no** copy inside the frontend
// tree: two hand-maintained copies of a contract is exactly how two contracts happen, and a test that
// reads a private copy proves nothing about what the backend sends.

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, sep } from "node:path";

import { describe, expect, it } from "vitest";

import { buildModel } from "../src/model";
import { decodeDashboard } from "../src/validate";
import { decodeSettingsAnswer } from "../src/settings";
import { decodeEntityAnswer, decodeVehicleAnswer, storedMode } from "../src/entity-config";
import { decodeSiteSettingsAnswer } from "../src/site-settings";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const SETTINGS_DIR = join(__dirname, "..", "..", "tests", "fixtures", "settings", "v1");
const SITE_SETTINGS_DIR = join(__dirname, "..", "..", "tests", "fixtures", "site_settings", "v1");
const ENTITY_CONFIG_V1_DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");

/**
 * One entry per fixture: the control state the backend serialized, and the facts the card must read.
 *
 * Written as a table on purpose: a fixture that changes its state fails here by name, rather than
 * being decoded "successfully" into something subtly different. Both axes are named per row, because
 * the whole reason v3 exists is that one folded action could not express these pairs.
 */
const EXPECTED: Record<
  string,
  {
    immediate: [string, string | null];
    automatic: [string, string | null];
    choices: string[];
    pause: { choice: string | null; admitted: boolean; expires: boolean } | null;
    blocks: boolean;
    error: string | null;
  }
> = {
  "start_idle.json": {
    immediate: ["start", null],
    automatic: ["pause", null],
    choices: ["next_period", "until_tomorrow", "until_resumed"],
    pause: { choice: null, admitted: false, expires: false },
    blocks: false,
    error: null,
  },
  "stop_charging.json": {
    immediate: ["stop", null],
    automatic: ["pause", null],
    choices: ["next_period", "until_tomorrow", "until_resumed"],
    pause: { choice: null, admitted: false, expires: false },
    blocks: false,
    error: null,
  },
  "resume_active.json": {
    immediate: ["start", null],
    automatic: ["resume", null],
    choices: [],
    pause: { choice: "until_resumed", admitted: true, expires: false },
    blocks: true,
    error: null,
  },
  "pause_clear_failed.json": {
    immediate: ["start", null],
    automatic: ["none", "pause_clear_failed"],
    choices: [],
    pause: { choice: "until_tomorrow", admitted: true, expires: true },
    blocks: true,
    error: "pause_clear_failed",
  },
  "action_pending.json": {
    immediate: ["none", "action_pending"],
    automatic: ["none", "action_pending"],
    choices: [],
    pause: { choice: null, admitted: false, expires: false },
    blocks: false,
    error: null,
  },
  "no_settings.json": {
    immediate: ["none", "no_settings"],
    automatic: ["none", "no_settings"],
    choices: [],
    pause: null,
    blocks: false,
    error: null,
  },
};

/** The two dashboard fixtures whose only facts are the plain proposal ones the other tables already cover. */
const PLAIN = ["pending_beside_installed.json", "proposal_ready.json"];

function names(): string[] {
  return readdirSync(DASHBOARD_DIR).sort();
}

function decodeFixture(name: string): ReturnType<typeof decodeDashboard> {
  return decodeDashboard(JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")));
}

describe("the backend's dashboard control fixtures", () => {
  it("are read from the backend's own directory, with no private copy to drift", () => {
    expect(names()).toEqual([...Object.keys(EXPECTED), ...Object.keys(V7_EXPECTED), ...PLAIN].sort());
    // The path is part of the assertion: a fixture kept beside the frontend would be a second
    // contract with nothing keeping it true.
    expect(DASHBOARD_DIR.split(sep).slice(-3).join(sep)).toBe(join("tests", "fixtures", "dashboard"));
    expect(existsSync(join(__dirname, "fixtures"))).toBe(false);
  });

  it.each(Object.keys(EXPECTED))("decodes %s into exactly the state the backend wrote", (name) => {
    const result = decodeFixture(name);
    expect(result.ok, `${name} did not decode`).toBe(true);
    if (!result.ok) {
      return;
    }
    const expected = EXPECTED[name] as NonNullable<(typeof EXPECTED)[string]>;
    const control = result.value.control;
    // Both axes, independently: the whole point of v3 is that these two are read separately rather
    // than folded into one action a client then has to interpret.
    expect([control.immediate_action, control.immediate_action_reason]).toEqual(expected.immediate);
    expect([control.automatic_action, control.automatic_action_reason]).toEqual(expected.automatic);
    expect(control.pause_choices).toEqual(expected.choices);
    expect(control.pause_blocks_execution).toBe(expected.blocks);
    expect(control.execution_error).toBe(expected.error);
    expect(control.can_act).toBe(true);
    if (expected.pause === null) {
      expect(control.pause).toBeNull();
    } else {
      expect(control.pause?.choice).toBe(expected.pause.choice);
      expect(control.pause?.admitted_at === null).toBe(!expected.pause.admitted);
      expect(control.pause?.expires_at === null).toBe(!expected.pause.expires);
    }
    // The strategy block travels with every one of these answers, and Cheapest is the one selected.
    expect(result.value.strategy.selected).toBe(result.value.settings === null ? null : "cheapest");
    for (const row of result.value.strategy.rows) {
      if (row.strategy === "cheapest") {
        expect(row.available).toBe(result.value.settings !== null);
      } else {
        expect(row.available).toBe(false);
        expect(row.reason).not.toBeNull();
      }
    }
  });

  it("names both axes in every one of its control blocks", () => {
    for (const name of names()) {
      const raw = JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")) as Record<string, unknown>;
      const control = raw.control as Record<string, unknown>;
      expect(Object.keys(control).sort(), name).toEqual(
        [
          "automatic_action",
          "automatic_action_reason",
          "can_act",
          "execution_error",
          "immediate_action",
          "immediate_action_reason",
          "pause",
          "pause_blocks_execution",
          "pause_choices",
        ].sort(),
      );
      expect("primary_action" in control, name).toBe(false);
    }
  });
});

// ---------------------------------------------------------------- v7: strategy_state and site

/** One v7 fixture's expected facts: the selected strategy, its `strategy_state`, and its `site`. */
interface ExpectedV7 {
  selected: string;
  strategyState:
    | null
    | {
        kind: "solar";
        state: string;
        reason: string | null;
        available_w: number | null;
        requested_a: number | null;
        priority_effective: string | null;
      }
    | {
        kind: "hybrid";
        state: string;
        reason: string | null;
        grid_kwh: number | null;
        credit_kwh: number | null;
        slack_kwh: number | null;
        forecast_configured: boolean;
        plan_window_active: boolean;
      };
  site:
    | null
    | {
        name: string;
        charger_count: number;
        solar_priority: string;
        forecastSelected: string[];
        forecastChoices: Array<{ id: string; title: string }>;
        activeControlReason: string | null;
        writable: boolean;
      };
}

const V7_EXPECTED: Record<string, ExpectedV7> = {
  "charger_states_its_maximum.json": { selected: "cheapest", strategyState: null, site: null },
  "cheapest_direct_site_admin.json": {
    selected: "cheapest",
    strategyState: null,
    site: {
      name: "Site",
      charger_count: 1,
      solar_priority: "car_first",
      forecastSelected: [],
      forecastChoices: [],
      activeControlReason: "site_measurement_configured_missing",
      writable: true,
    },
  },
  "cheapest_direct_site_read_only.json": {
    selected: "cheapest",
    strategyState: null,
    site: {
      name: "Site",
      charger_count: 1,
      solar_priority: "car_first",
      forecastSelected: [],
      forecastChoices: [],
      activeControlReason: "site_measurement_configured_missing",
      writable: false,
    },
  },
  "cheapest_no_site.json": { selected: "cheapest", strategyState: null, site: null },
  "waiting_for_publication.json": { selected: "cheapest", strategyState: null, site: null },
  "waiting_for_history.json": { selected: "cheapest", strategyState: null, site: null },
  "buying_before_publication.json": { selected: "cheapest", strategyState: null, site: null },
  "charging_without_prices.json": { selected: "cheapest", strategyState: null, site: null },
  "target_soc_estimated.json": { selected: "cheapest", strategyState: null, site: null },
  "target_soc_two_vehicles.json": { selected: "cheapest", strategyState: null, site: null },
  "target_soc_stopped_on_estimate.json": { selected: "cheapest", strategyState: null, site: null },
  "hybrid_derived_site_no_forecast.json": {
    selected: "hybrid",
    strategyState: {
      kind: "hybrid",
      state: "no_forecast",
      reason: "no_forecast_cheapest",
      grid_kwh: 20.0,
      credit_kwh: 0.0,
      slack_kwh: 113.02150202128979,
      forecast_configured: false,
      plan_window_active: false,
    },
    site: {
      name: "Site",
      charger_count: 1,
      solar_priority: "car_first",
      forecastSelected: [],
      forecastChoices: [],
      activeControlReason: "no_commandable_charger",
      writable: true,
    },
  },
  "hybrid_derived_site_with_forecast.json": {
    selected: "hybrid",
    strategyState: {
      kind: "hybrid",
      state: "waiting_for_sun",
      reason: "waiting_for_sun_within_slack",
      grid_kwh: 20.0,
      credit_kwh: 0.0,
      slack_kwh: 113.02150202128979,
      forecast_configured: true,
      plan_window_active: false,
    },
    site: {
      name: "Site",
      charger_count: 1,
      solar_priority: "car_first",
      forecastSelected: ["roof_hybrid"],
      forecastChoices: [{ id: "roof_hybrid", title: "Roof (hybrid)" }],
      activeControlReason: "no_commandable_charger",
      writable: true,
    },
  },
  "solar_direct_site_with_total.json": {
    selected: "solar",
    strategyState: {
      kind: "solar",
      state: "off",
      reason: "off_no_surplus",
      available_w: 0.0,
      requested_a: null,
      priority_effective: "battery_first",
    },
    site: {
      name: "Site",
      charger_count: 1,
      solar_priority: "car_first",
      forecastSelected: [],
      forecastChoices: [],
      activeControlReason: "no_commandable_charger",
      writable: true,
    },
  },
  "solar_derived_site.json": {
    selected: "solar",
    strategyState: {
      kind: "solar",
      state: "off",
      reason: "off_no_surplus",
      available_w: 0.0,
      requested_a: null,
      priority_effective: "battery_first",
    },
    site: {
      name: "Site",
      charger_count: 1,
      solar_priority: "car_first",
      forecastSelected: [],
      forecastChoices: [],
      activeControlReason: "no_commandable_charger",
      writable: true,
    },
  },
};

/** The price-wait facts each new v7 fixture carries: state, reason, wait kind, publication, must-buy. */
const PRICE_WAIT_EXPECTED: Record<
  string,
  { state: string; reason: string; wait: string; publicationAt: string; mustBuy: number; unpriced: boolean }
> = {
  "waiting_for_publication.json": {
    state: "waiting_for_publication",
    reason: "publication_pending",
    wait: "waiting",
    publicationAt: "2026-09-22T11:45:00+00:00",
    mustBuy: 0,
    unpriced: false,
  },
  "waiting_for_history.json": {
    state: "waiting_for_publication",
    reason: "waiting_for_history",
    wait: "waiting",
    publicationAt: "2026-10-02T11:45:00+00:00",
    mustBuy: 0,
    unpriced: false,
  },
  "buying_before_publication.json": {
    state: "proposal_ready",
    reason: "buying_before_publication",
    wait: "buy_now",
    publicationAt: "2026-09-22T11:45:00+00:00",
    mustBuy: 6.420000000000002,
    unpriced: false,
  },
  "charging_without_prices.json": {
    state: "proposal_unpriced",
    reason: "charging_without_prices",
    wait: "guarantee",
    publicationAt: "2026-09-22T11:45:00+00:00",
    mustBuy: 20,
    unpriced: true,
  },
};

/** What the backend's composer said for each v7 fixture: its tone, its codes, and the card's English line. */
const PLANNED = ["auto_planned", "plan_energy", "plan_cost", "plan_distance"];
const V7_STATUS_EXPECTED: Record<string, { tone: string; codes: string[]; english: string }> = {
  "buying_before_publication.json": {
    tone: "normal",
    codes: ["buying_before_publication"],
    english: "Buying 6.4 kWh now, the rest when the prices are published.",
  },
  "charger_states_its_maximum.json": {
    tone: "normal",
    codes: PLANNED,
    english: "Planned from 08:45 · 20.1 kWh · 34.6 kr · 101 km",
  },
  "charging_without_prices.json": {
    tone: "notice",
    codes: ["charging_without_prices", "plan_energy"],
    english: "Charging without published prices to keep the deadline · 20.1 kWh",
  },
  // The site's phase sensors read nothing here, so the status says which phases and where from.
  "cheapest_direct_site_admin.json": {
    tone: "notice",
    codes: [...PLANNED, "site_measurement_problem"],
    english:
      "Planned from 08:45 · 20.1 kWh · 34.6 kr · 101 km · L1, L2, and L3 have no value (sensor.cheapest_direct_admin_site_l1, sensor.cheapest_direct_admin_site_l2, sensor.cheapest_direct_admin_site_l3).",
  },
  "cheapest_direct_site_read_only.json": {
    tone: "notice",
    codes: [...PLANNED, "site_measurement_problem"],
    english:
      "Planned from 08:45 · 20.1 kWh · 34.6 kr · 101 km · L1, L2, and L3 have no value (sensor.cheapest_direct_reader_site_l1, sensor.cheapest_direct_reader_site_l2, sensor.cheapest_direct_reader_site_l3).",
  },
  "cheapest_no_site.json": { tone: "normal", codes: PLANNED, english: "Planned from 08:45 · 20.1 kWh · 34.6 kr · 101 km" },
  "hybrid_derived_site_no_forecast.json": {
    tone: "normal",
    codes: ["hybrid_no_forecast"],
    english: "Hybrid · no forecast source — planning like Cheapest",
  },
  "hybrid_derived_site_with_forecast.json": {
    tone: "normal",
    codes: ["hybrid_grid"],
    english: "Hybrid · 20 kWh from grid 12:15–15:15",
  },
  "solar_direct_site_with_total.json": {
    tone: "notice",
    codes: ["settings_incomplete"],
    english: "Finish setting up in Settings: price area, phases, charging current.",
  },
  "solar_derived_site.json": {
    tone: "notice",
    codes: ["settings_incomplete"],
    english: "Finish setting up in Settings: price area, phases, charging current.",
  },
  "target_soc_estimated.json": { tone: "normal", codes: PLANNED, english: "Planned from 10:15 · 34.5 kWh · 82.92 kr · 173 km" },
  "target_soc_stopped_on_estimate.json": {
    tone: "normal",
    codes: [...PLANNED, "target_reached"],
    english: "Planned from 10:15 · 34.5 kWh · 82.92 kr · 173 km · Stopped at 81 % (estimated, reading 30 min old)",
  },
  "target_soc_two_vehicles.json": { tone: "normal", codes: PLANNED, english: "Planned from 10:15 · 34.5 kWh · 82.92 kr · 173 km" },
  "waiting_for_publication.json": {
    tone: "normal",
    codes: ["waiting_for_publication"],
    english: "Waiting for tomorrow's prices (~13:45), will plan then.",
  },
  "waiting_for_history.json": {
    tone: "normal",
    codes: ["waiting_for_history"],
    english: "Waiting: Sundays were 70 % cheaper the last 4 weeks.",
  },
};

describe("the backend's dashboard strategy_state, site and status fixtures", () => {
  it("are read from the backend's own directory, with no private copy to drift", () => {
    expect(Object.keys(V7_EXPECTED).every((name) => names().includes(name))).toBe(true);
  });

  it.each(Object.keys(V7_EXPECTED))(
    "decodes %s into exactly the strategy_state and site the backend wrote",
    (name) => {
      const result = decodeFixture(name);
      expect(result.ok, `${name} did not decode`).toBe(true);
      if (!result.ok) {
        return;
      }
      const value = result.value;
      expect(value.api_version).toBe(1);
      const expected = V7_EXPECTED[name] as ExpectedV7;
      expect(value.strategy.selected, name).toBe(expected.selected);
      expect(value.strategy_state, name).toEqual(expected.strategyState);
      if (expected.site === null) {
        expect(value.site, name).toBeNull();
      } else {
        expect(value.site, name).toEqual({
          name: expected.site.name,
          charger_count: expected.site.charger_count,
          solar_priority: expected.site.solar_priority,
          solar_forecast: { selected: expected.site.forecastSelected, choices: expected.site.forecastChoices },
          active_control: {
            available: expected.site.activeControlReason === null,
            enabled: false,
            reason: expected.site.activeControlReason,
            // A WebSocket reader may switch it exactly when it may write the site.
            writable: expected.site.writable,
          },
          writable: expected.site.writable,
        });
      }
    },
  );

  it.each(Object.keys(PRICE_WAIT_EXPECTED))("reads the price-wait facts of %s", (name) => {
    const result = decodeFixture(name);
    expect(result.ok, `${name} did not decode`).toBe(true);
    if (!result.ok) {
      return;
    }
    const expected = PRICE_WAIT_EXPECTED[name] as (typeof PRICE_WAIT_EXPECTED)[string];
    const planning = result.value.planning;
    expect(planning?.state, name).toBe(expected.state);
    expect(planning?.reason, name).toBe(expected.reason);
    expect(planning?.price_wait, name).toBe(expected.wait);
    expect(planning?.publication_at, name).toBe(expected.publicationAt);
    expect(planning?.must_buy_now_kwh, name).toBe(expected.mustBuy);
    expect(result.value.prices.unpriced, name).toBe(expected.unpriced);
  });

  it.each(Object.keys(V7_STATUS_EXPECTED))("reads the status block of %s, and words it", (name) => {
    const result = decodeFixture(name);
    expect(result.ok, `${name} did not decode`).toBe(true);
    if (!result.ok) {
      throw new Error(`${name}: no v7 status`);
    }
    const expected = V7_STATUS_EXPECTED[name] as (typeof V7_STATUS_EXPECTED)[string];
    const status = result.value.status;
    expect(status.tone, name).toBe(expected.tone);
    expect(status.lines.map((line) => line.code), name).toEqual(expected.codes);
    // Exactly what the backend wrote: the decoder keeps every param and invents none.
    const raw = JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")) as { status: unknown };
    expect(status, name).toEqual(raw.status);
    const built = buildModel({ dashboard: result.value, language: "en", nowMs: Date.parse("2026-09-22T04:30:00Z") });
    expect(built.status, name).toBe(expected.english);
    expect(built.severity, name).toBe(expected.tone === "normal" ? null : expected.tone);
  });

  it("never carries a version the card does not speak, nor a stray key", () => {
    for (const name of names()) {
      const raw = JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")) as Record<string, unknown>;
      expect(raw.api_version, name).toBe(1);
      expect(Object.keys(raw).sort(), name).toEqual(
        [
          "api_version",
          "charge_progress",
          "charger",
          "chargers",
          "control",
          "current_range",
          "detected_phases",
          "fiscal",
          "generated_at",
          "live",
          "market",
          "phase_detection",
          "plan",
          "planning",
          "prices",
          "sessions_summary",
          "settings",
          "site",
          "soc",
          "status",
          "strategy",
          "strategy_options",
          "strategy_state",
          "summary",
          "target_vehicle_id",
          "vehicles",
        ].sort(),
      );
    }
  });
});

describe("the backend's v7 soc", () => {
  it("is null in every fixture that has no state-of-charge source", () => {
    for (const name of names().filter((entry) => !entry.startsWith("target_soc_"))) {
      const result = decodeFixture(name);
      expect(result.ok, name).toBe(true);
      if (result.ok && true) {
        expect(result.value.soc, name).toBeNull();
      }
    }
  });

  it("is decoded exactly as the backend wrote it in target_soc_estimated.json", () => {
    const result = decodeFixture("target_soc_estimated.json");
    expect(result.ok).toBe(true);
    if (!result.ok || false) {
      throw new Error("no v7 soc");
    }
    expect(result.value.soc).toEqual({
      value: 75.1,
      age_s: 7200,
      source: "vehicle",
      estimated: true,
      target_percent: 80,
      need_kwh: 4.22,
      capacity_kwh: 77,
      vehicle_name: "EV6",
      vehicle_id: "<id>",
      vehicles: [],
      vehicle_max_percent: null,
      efficiency: 0.9,
      missing: [],
    });
    expect(result.value.charger.capabilities.target_soc).toBe(true);
  });

  it("is decoded exactly as the backend wrote it in target_soc_two_vehicles.json", () => {
    const result = decodeFixture("target_soc_two_vehicles.json");
    if (!result.ok || false) {
      throw new Error("no v7 soc");
    }
    expect(result.value.soc).toEqual({
      value: 40,
      age_s: 0,
      source: "vehicle",
      estimated: false,
      target_percent: 80,
      need_kwh: 34.22,
      capacity_kwh: 77,
      vehicle_name: "EV6",
      vehicle_id: "vehicle_ev6",
      vehicles: [
        { id: "vehicle_ev6", name: "EV6" },
        { id: "vehicle_niro", name: "Niro" },
      ],
      vehicle_max_percent: 80,
      efficiency: 0.9,
      missing: [],
    });
  });
});

describe("the backend's v7 vehicles", () => {
  it("lists none in a fixture without a vehicle and names no target vehicle", () => {
    const result = decodeFixture("cheapest_no_site.json");
    if (!result.ok || false) {
      throw new Error("no v7");
    }
    expect(result.value.vehicles).toEqual([]);
    expect(result.value.target_vehicle_id).toBeNull();
  });

  it("lists both vehicles with their own properties, and the one this charger plans for", () => {
    const result = decodeFixture("target_soc_two_vehicles.json");
    if (!result.ok || false) {
      throw new Error("no v7");
    }
    expect(result.value.target_vehicle_id).toBe("vehicle_ev6");
    expect(result.value.vehicles).toEqual([
      {
        id: "vehicle_ev6",
        name: "EV6",
        soc_entity_id: "sensor.ev6_battery",
        capacity_kwh: 77,
        capacity_source: "stored",
        consumption_kwh_per_10km: 2,
        max_percent: 80,
        soc_percent: 40,
      },
      {
        id: "vehicle_niro",
        name: "Niro",
        soc_entity_id: "sensor.niro_battery",
        capacity_kwh: 64.8,
        capacity_source: "stored",
        consumption_kwh_per_10km: 1.7,
        max_percent: null,
        soc_percent: 55,
      },
    ]);
  });

  it.each<[string, (raw: Record<string, any>) => void]>([
    ["a vehicle with an unknown key", (raw) => (raw["vehicles"][0]["extra"] = 1)],
    ["a vehicle without its capacity source", (raw) => delete raw["vehicles"][0]["capacity_source"]],
    ["a capacity source the card does not know", (raw) => (raw["vehicles"][0]["capacity_source"] = "guessed")],
    ["a capacity with no source", (raw) => (raw["vehicles"][0]["capacity_source"] = null)],
    ["a source with no capacity", (raw) => (raw["vehicles"][0]["capacity_kwh"] = null)],
    ["a consumption of zero", (raw) => (raw["vehicles"][0]["consumption_kwh_per_10km"] = 0)],
    ["a charge limit above 100", (raw) => (raw["vehicles"][0]["max_percent"] = 101)],
    ["a vehicle charge level above 100", (raw) => (raw["vehicles"][0]["soc_percent"] = 101)],
    ["a vehicle without its charge level", (raw) => delete raw["vehicles"][0]["soc_percent"]],
    ["vehicles that is not a list", (raw) => (raw["vehicles"] = {})],
    ["no efficiency in soc", (raw) => delete raw["soc"]["efficiency"]],
    ["an efficiency above 1", (raw) => (raw["soc"]["efficiency"] = 1.5)],
    ["no vehicle limit in soc", (raw) => delete raw["soc"]["vehicle_max_percent"]],
    ["no target vehicle", (raw) => delete raw["target_vehicle_id"]],
  ])("refuses the whole answer for %s", (_name, damage) => {
    const raw = JSON.parse(readFileSync(join(DASHBOARD_DIR, "target_soc_two_vehicles.json"), "utf8")) as Record<string, any>;
    damage(raw);
    expect(decodeDashboard(raw)).toEqual({ ok: false, failure: "malformed" });
  });
});

const VEHICLE_V1_DIR = join(__dirname, "..", "..", "tests", "fixtures", "vehicle", "v1");

/** `spotnav/update_vehicle`'s answers: outcome, the field errors and the vehicle row each one carries. */
const VEHICLE_V1_EXPECTED: Record<
  string,
  { ok: boolean; code: string | null; fieldErrors: string[]; vehicle: string | null }
> = {
  "update_vehicle_success.json": { ok: true, code: null, fieldErrors: [], vehicle: "vehicle_niro" },
  "update_vehicle_cleared.json": { ok: true, code: null, fieldErrors: [], vehicle: "vehicle_niro" },
  "update_vehicle_conflict.json": { ok: false, code: "spotnav_conflict", fieldErrors: [], vehicle: "vehicle_niro" },
  "update_vehicle_refused.json": {
    ok: false,
    code: "spotnav_invalid_value",
    fieldErrors: ["capacity_kwh:invalid_capacity", "consumption_kwh_per_10km:invalid_consumption"],
    vehicle: "vehicle_niro",
  },
  "update_vehicle_unknown_vehicle.json": {
    ok: false,
    code: "spotnav_invalid_value",
    fieldErrors: ["vehicle_id:unknown_vehicle"],
    vehicle: null,
  },
  "update_vehicle_not_admin.json": { ok: false, code: "spotnav_not_admin", fieldErrors: [], vehicle: null },
};

describe("the backend's vehicle v1 contract fixtures", () => {
  it("are read from the backend's own directory, every one of them accounted for", () => {
    expect(readdirSync(VEHICLE_V1_DIR).sort()).toEqual(Object.keys(VEHICLE_V1_EXPECTED).sort());
  });

  it.each(Object.keys(VEHICLE_V1_EXPECTED))("decodes %s into exactly what the backend wrote", (name) => {
    const raw = JSON.parse(readFileSync(join(VEHICLE_V1_DIR, name), "utf8"));
    const result = decodeVehicleAnswer(raw);
    expect(result.ok, `${name} did not decode`).toBe(true);
    if (!result.ok) {
      return;
    }
    const expected = VEHICLE_V1_EXPECTED[name] as (typeof VEHICLE_V1_EXPECTED)[string];
    const answer = result.value;
    expect(answer.ok, name).toBe(expected.ok);
    if (!answer.ok) {
      expect(answer.code, name).toBe(expected.code);
    }
    expect(answer.fieldErrors.map((e) => `${e.field}:${e.code}`), name).toEqual(expected.fieldErrors);
    expect(answer.vehicle?.id ?? null, name).toBe(expected.vehicle);
  });

  it("reads the vehicle row a success and a refusal state, not what was asked", () => {
    const ok = decodeVehicleAnswer(JSON.parse(readFileSync(join(VEHICLE_V1_DIR, "update_vehicle_success.json"), "utf8")));
    const refused = decodeVehicleAnswer(JSON.parse(readFileSync(join(VEHICLE_V1_DIR, "update_vehicle_refused.json"), "utf8")));
    expect(ok.ok && ok.value.vehicle?.capacity_kwh).toBe(64.8);
    expect(ok.ok && ok.value.vehicle?.consumption_kwh_per_10km).toBe(1.7);
    expect(refused.ok && refused.value.vehicle?.capacity_kwh).toBeNull();
    expect(refused.ok && refused.value.vehicle?.capacity_source).toBeNull();
  });

  it("refuses an answer with an unknown key, or a version it does not speak", () => {
    const raw = JSON.parse(readFileSync(join(VEHICLE_V1_DIR, "update_vehicle_success.json"), "utf8"));
    expect(decodeVehicleAnswer({ ...raw, extra: 1 })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeVehicleAnswer({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
  });
});

describe("the backend's v7 current_range", () => {
  it.each(names())("is decoded exactly as the backend wrote it in %s", (name) => {
    const result = decodeFixture(name);
    expect(result.ok).toBe(true);
    if (!result.ok) {
      throw new Error(`${name}: no current_range`);
    }
    expect(result.value.current_range).toEqual(
      name === "charger_states_its_maximum.json"
        ? { min_a: 6, max_a: 16, source: "current_limit" }
        : { min_a: 6, max_a: 32, source: "default" },
    );
  });
});

// ---------------------------------------------------------------- settings: the strategy write

const SETTINGS_EXPECTED: Record<
  string,
  { ok: boolean; code: string | null; strategy: string; revision: number; date?: string | null }
> = {
  "success.json": { ok: true, code: null, strategy: "solar", revision: 2, date: null },
  "revision_conflict.json": { ok: false, code: "revision_conflict", strategy: "cheapest", revision: 3 },
  "refusal_invalid_strategy.json": { ok: false, code: "invalid_strategy", strategy: "cheapest", revision: 4 },
  "refusal_unknown_field.json": { ok: false, code: "unknown_field", strategy: "cheapest", revision: 5 },
  "success_dated.json": { ok: true, code: null, strategy: "cheapest", revision: 7, date: "2026-09-27" },
  "refusal_invalid_departure_date.json": {
    ok: false,
    code: "invalid_departure",
    strategy: "cheapest",
    revision: 8,
    date: null,
  },
};

describe("the backend's settings contract fixtures", () => {
  it("are read from the backend's own directory, with no private copy to drift", () => {
    expect(readdirSync(SETTINGS_DIR).sort()).toEqual(Object.keys(SETTINGS_EXPECTED).sort());
    expect(SETTINGS_DIR.split(sep).slice(-4).join(sep)).toBe(join("tests", "fixtures", "settings", "v1"));
  });

  it.each(Object.keys(SETTINGS_EXPECTED))("decodes %s into exactly the strategy the backend wrote", (name) => {
    const raw = JSON.parse(readFileSync(join(SETTINGS_DIR, name), "utf8"));
    const result = decodeSettingsAnswer(raw);
    expect(result.ok, `${name} did not decode`).toBe(true);
    if (!result.ok) {
      return;
    }
    const expected = SETTINGS_EXPECTED[name] as (typeof SETTINGS_EXPECTED)[string];
    const answer = result.value;
    expect(answer.ok, name).toBe(expected.ok);
    expect(answer.settings?.strategy, name).toBe(expected.strategy);
    expect(answer.settings?.revision, name).toBe(expected.revision);
    if (expected.date !== undefined) {
      expect(answer.settings?.departure_date, name).toBe(expected.date);
    }
    if (!answer.ok) {
      expect(answer.code, name).toBe(expected.code);
    }
  });
});

// ---------------------------------------------------------------- site_settings

const SITE_SETTINGS_EXPECTED: Record<
  string,
  {
    ok: boolean;
    code: string | null;
    site: { enabled: boolean; available: boolean } | null;
    restore: { outcome: string; chargers: [string, string | null, number | null, number | null][] } | null;
  }
> = {
  "enable.json": { ok: true, code: null, site: { enabled: true, available: true }, restore: null },
  "enable_unavailable.json": {
    ok: false,
    code: "spotnav_active_control_unavailable",
    site: { enabled: false, available: false },
    restore: null,
  },
  "disable_not_needed.json": {
    ok: true,
    code: null,
    site: { enabled: false, available: true },
    restore: { outcome: "not_needed", chargers: [["not_needed", null, 16, 16]] },
  },
  "disable_restored.json": {
    ok: true,
    code: null,
    site: { enabled: false, available: true },
    restore: { outcome: "restored", chargers: [["restored", null, 10, 16]] },
  },
  "disable_restore_failed.json": {
    ok: true,
    code: null,
    site: { enabled: false, available: true },
    restore: { outcome: "failed", chargers: [["failed", "write_failed", 10, 16]] },
  },
  "conflict.json": { ok: false, code: "spotnav_conflict", site: { enabled: true, available: true }, restore: null },
  "invalid_value.json": { ok: false, code: "spotnav_invalid_value", site: { enabled: true, available: true }, restore: null },
  "no_site.json": { ok: false, code: "spotnav_no_site", site: null, restore: null },
  "not_admin.json": { ok: false, code: "spotnav_not_admin", site: null, restore: null },
  "success.json": { ok: true, code: null, site: { enabled: false, available: false }, restore: null },
};

describe("the backend's site_settings contract fixtures", () => {
  it("are read from the backend's own directory, every one of them accounted for", () => {
    expect(readdirSync(SITE_SETTINGS_DIR).sort()).toEqual(Object.keys(SITE_SETTINGS_EXPECTED).sort());
  });

  it.each(Object.keys(SITE_SETTINGS_EXPECTED))("decodes %s into exactly what the backend wrote", (name) => {
    const raw = JSON.parse(readFileSync(join(SITE_SETTINGS_DIR, name), "utf8"));
    const result = decodeSiteSettingsAnswer(raw);
    expect(result.ok, `${name} did not decode`).toBe(true);
    if (!result.ok) {
      return;
    }
    const expected = SITE_SETTINGS_EXPECTED[name] as (typeof SITE_SETTINGS_EXPECTED)[string];
    const answer = result.value;
    expect(answer.ok, name).toBe(expected.ok);
    if (!answer.ok) {
      expect(answer.code, name).toBe(expected.code);
    }
    if (expected.site === null) {
      expect(answer.site, name).toBeNull();
    } else {
      expect(answer.site?.active_control.enabled, name).toBe(expected.site.enabled);
      expect(answer.site?.active_control.available, name).toBe(expected.site.available);
    }
    if (expected.restore === null) {
      expect(answer.restore, name).toBeNull();
    } else {
      expect(answer.restore?.outcome, name).toBe(expected.restore.outcome);
      expect(
        answer.restore?.chargers.map((c) => [c.outcome, c.code, c.fromA, c.toA]),
        name,
      ).toEqual(expected.restore.chargers);
    }
  });
});

// ---------------------------------------------------------------- entity_config v1

const DIRECT = ["direct_L1", "direct_L2", "direct_L3"];
const DERIVED = ["L1", "L2", "L3"].flatMap((phase) =>
  ["power", "voltage", "power_export", "reactive_power", "apparent_power", "current"].map(
    (kind) => `derived_${phase}_${kind}`,
  ),
);
const DERIVED_REQUIRED = ["L1", "L2", "L3"].flatMap((phase) =>
  ["power", "voltage"].map((kind) => `derived_${phase}_${kind}`),
);
/** What follows the phase meters: the sign options, the meter's total grid power, the battery sensors and the maximum age. */
const SITE_TAIL = [
  "site_current_signed",
  "grid_power_source_power",
  "grid_power_source_power_export",
  "grid_power_inverted",
  "battery_aggregate_power_entity",
  "battery_discharge_power_entity",
  "battery_power_inverted",
  "max_age_s",
];
const CHARGER = ["charge_control", "current_limit", "energy_register_entity", "power_entity", "charger_priority", "vehicle_soc"];
const SITE_FIXED = ["main_fuse_a", "safety_margin_a", "measurement_mode", "voltage_between_phases_v"];
/** A charger in no site holds the voltage between phases itself, listed before the vehicle sensor. */
const CHARGER_NO_SITE = ["charge_control", "current_limit", "energy_register_entity", "power_entity", "voltage_between_phases_v", "vehicle_soc"];

/**
 * One row per fixture: the outcome, the field errors in the order the backend wrote them, and -- when
 * a config travels with the answer -- the charger it names, the fields it lists (in order), the
 * measurement mode, and a few values that prove the fields were read rather than merely counted.
 */
const ENTITY_CONFIG_V1_EXPECTED: Record<
  string,
  {
    ok: boolean;
    code: string | null;
    fieldErrors: Array<[string, string]>;
    config: {
      charger: string;
      fields: string[];
      mode: string | null;
      chargeControl: string;
      currentLimit: string | null;
      fuse: number | null;
      maxAge: number | null;
      siteChargers: number | null;
    } | null;
  }
> = {
  "vehicle_soc_get.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "veh_get",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.veh_get_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "vehicle_soc_confirmed.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "veh_ok",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.veh_ok_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "vehicle_soc_cleared.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "veh_clear",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.veh_clear_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "vehicle_soc_refused.json": {
    ok: false,
    code: "spotnav_invalid_value",
    fieldErrors: [["vehicle_soc", "entity_not_found"]],
    config: {
      charger: "veh_bad",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.veh_bad_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "get_direct.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "get_direct",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.get_direct_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "get_direct_total.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "get_direct_total",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.get_direct_total_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "get_derived.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "get_derived",
      fields: [...CHARGER, ...SITE_FIXED, ...DERIVED, ...SITE_TAIL],
      mode: "derived_phase_current",
      chargeControl: "switch.get_derived_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "get_detected.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "get_detected",
      fields: [...CHARGER, ...SITE_FIXED, ...DERIVED, ...SITE_TAIL],
      mode: "derived_phase_current",
      chargeControl: "switch.get_detected_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "get_no_site.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "get_lone",
      fields: CHARGER_NO_SITE,
      mode: null,
      chargeControl: "switch.get_lone_control",
      currentLimit: null,
      fuse: null,
      maxAge: null,
      siteChargers: null,
    },
  },
  "success_current_limit_none.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "ok_none",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.ok_none_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "success_charger.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "ok_charger",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.ok_charger_control",
      currentLimit: "number.ok_limit",
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "success_site.json": {
    ok: true,
    code: null,
    fieldErrors: [],
    config: {
      charger: "ok_site",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.ok_site_control",
      currentLimit: null,
      fuse: 32,
      maxAge: 60,
      siteChargers: 1,
    },
  },
  "conflict.json": {
    ok: false,
    code: "spotnav_conflict",
    fieldErrors: [],
    config: {
      charger: "conflict",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.conflict_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "field_errors_charger.json": {
    ok: false,
    code: "spotnav_invalid_value",
    fieldErrors: [
      ["charge_control", "wrong_domain"],
      ["current_limit", "entity_not_found"],
      ["vehicle_soc", "not_writable"],
    ],
    config: {
      charger: "err_charger",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.err_charger_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "field_errors_in_use.json": {
    ok: false,
    code: "spotnav_invalid_value",
    fieldErrors: [["charge_control", "charge_control_in_use"]],
    config: {
      charger: "use_charger",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.use_charger_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "field_errors_site.json": {
    ok: false,
    code: "spotnav_invalid_value",
    fieldErrors: [
      ["main_fuse_a", "invalid_value"],
      ["measurement_mode", "invalid_value"],
      ["battery_aggregate_power_entity", "wrong_domain"],
      ["direct_L1", "entity_not_found"],
      ["direct_L2", "required"],
    ],
    config: {
      charger: "err_site",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.err_site_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "derived_requirements.json": {
    ok: false,
    code: "spotnav_invalid_value",
    fieldErrors: DERIVED_REQUIRED.map((field) => [field, "required"] as [string, string]),
    config: {
      charger: "req_derived",
      fields: [...CHARGER, ...SITE_FIXED, ...DIRECT, ...SITE_TAIL],
      mode: "direct_phase_current",
      chargeControl: "switch.req_derived_control",
      currentLimit: null,
      fuse: 25,
      maxAge: 120,
      siteChargers: 1,
    },
  },
  "no_site.json": { ok: false, code: "spotnav_no_site", fieldErrors: [], config: null },
  "not_admin.json": { ok: false, code: "spotnav_not_admin", fieldErrors: [], config: null },
};

/** Per fixture: `[vehicle id, source, selected entity, candidate count]` for each listed vehicle. */
const VEHICLES_EXPECTED: Record<string, Array<[string, string | null, string | null, number]>> = {
  "vehicle_soc_get.json": [["vehicle_a", null, null, 2]],
  "vehicle_soc_confirmed.json": [["vehicle_a", "confirmed", "sensor.pack_a", 2]],
  "vehicle_soc_cleared.json": [["vehicle_a", null, null, 2]],
  "vehicle_soc_refused.json": [["vehicle_a", null, null, 2]],
};

describe("the backend's entity_config v1 contract fixtures", () => {
  it("are read from the backend's own directory, with no private copy to drift", () => {
    expect(readdirSync(ENTITY_CONFIG_V1_DIR).sort()).toEqual(Object.keys(ENTITY_CONFIG_V1_EXPECTED).sort());
    expect(ENTITY_CONFIG_V1_DIR.split(sep).slice(-4).join(sep)).toBe(join("tests", "fixtures", "entity_config", "v1"));
  });

  it.each(Object.keys(ENTITY_CONFIG_V1_EXPECTED))("decodes %s into exactly the facts the backend wrote", (name) => {
    const raw = JSON.parse(readFileSync(join(ENTITY_CONFIG_V1_DIR, name), "utf8"));
    const result = decodeEntityAnswer(raw);
    expect(result.ok, `${name} did not decode`).toBe(true);
    if (!result.ok) {
      return;
    }
    const expected = ENTITY_CONFIG_V1_EXPECTED[name] as (typeof ENTITY_CONFIG_V1_EXPECTED)[string];
    const answer = result.value;
    expect(answer.ok, name).toBe(expected.ok);
    if (!answer.ok) {
      expect(answer.code, name).toBe(expected.code);
      expect(
        answer.fieldErrors.map((error) => [error.field, error.code]),
        name,
      ).toEqual(expected.fieldErrors);
    }
    const config = answer.ok ? answer.config : answer.config;
    if (expected.config === null) {
      expect(config, name).toBeNull();
      return;
    }
    expect(config, name).not.toBeNull();
    if (config === null) {
      return;
    }
    expect(config.chargerId, name).toBe(expected.config.charger);
    expect(config.fields.map((field) => field.field), name).toEqual(expected.config.fields);
    expect(storedMode(config), name).toBe(expected.config.mode);
    const byName = new Map(config.fields.map((field) => [field.field, field]));
    const control = byName.get("charge_control");
    expect(control?.kind === "entity" ? control.current?.entityId : undefined, name).toBe(expected.config.chargeControl);
    const limit = byName.get("current_limit");
    expect(limit?.kind === "entity" ? (limit.current?.entityId ?? null) : undefined, name).toBe(
      expected.config.currentLimit,
    );
    // The current limit says whether "None" may be chosen, and the one saved as None says it is chosen.
    expect(limit?.kind === "entity" ? limit.none : undefined, name).toEqual({
      allowed: true,
      chosen: name === "success_current_limit_none.json",
      automatic: null,
    });
    const fuse = byName.get("main_fuse_a");
    expect(fuse === undefined ? null : fuse.kind === "number" ? fuse.value : "wrong kind", name).toBe(expected.config.fuse);
    // The safety margin sits next to the fuse and is read as a number (the fixtures' site keeps 1 A).
    const margin = byName.get("safety_margin_a");
    expect(margin === undefined ? null : margin.kind === "number" ? margin.value : "wrong kind", name).toBe(
      expected.config.fuse === null ? null : 1,
    );
    const age = byName.get("max_age_s");
    expect(age === undefined ? null : age.kind === "number" ? age.value : "wrong kind", name).toBe(expected.config.maxAge);
    expect(config.site?.chargerCount ?? null, name).toBe(expected.config.siteChargers);
    // The vehicles a charge-level sensor can be chosen for, as the backend listed them.
    const vehicles = VEHICLES_EXPECTED[name] ?? [];
    expect(
      config.vehicles.map((vehicle) => [vehicle.id, vehicle.source, vehicle.selected?.entityId ?? null, vehicle.candidates.length]),
      name,
    ).toEqual(vehicles);
    // The read-only vehicle level is listed, and never writable.
    const soc = byName.get("vehicle_soc");
    expect(soc?.writable, name).toBe(false);
    // Every entity field carries the classes the picker filters by.
    for (const field of config.fields) {
      if (field.kind === "entity") {
        expect(field.domains.length, `${name} ${field.field}`).toBeGreaterThan(0);
      }
    }
  });
});
