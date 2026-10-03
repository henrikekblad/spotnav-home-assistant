// Raw API v1 payloads for the pure tests, and the decoded form of each.
//
// The tests deliberately build *raw* payloads and put them through `decodeDashboard`, so every fixture
// also exercises the decoder's boundary instead of assuming the decoded shape exists.

import { STATUS_CODE_TABLE, decodeDashboard, type Dashboard } from "../src/validate";

/** A plain one-day SE4 answer at a fixed instant, overridable field by field. */
export function rawDashboard(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    api_version: 1,
    generated_at: "2026-09-22T06:00:00+00:00",
    charger: {
      charger_id: "entry_a",
      charger_name: "Garage",
      available: true,
      capabilities: {
        auto_price: true,
        current_limit: true,
        set_current: false,
        regulated_current: false,
        target_soc: false,
        load_balancing: false,
        refresh_vehicle: false,
        set_charge_limit: false,
        target_stop: false,
      },
    },
    settings: {
      revision: 3,
      strategy: "cheapest",
      area_id: "SE4",
      overrides: [],
      phases: 1,
      amps: 10,
      requested_kwh: 20,
      max_periods: 1,
      departure_enabled: false,
      departure_time: "08:00",
      departure_date: null,
      departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
      driver: "manual_kwh",
      target: { vehicle_id: null, target_percent: null },
    },
    fiscal: null,
    planning: {
      state: "proposal_ready",
      reason: "ready",
      settings_revision: 3,
      execution_state: "active",
      execution_reason: null,
      execution_paused: false,
      applied: true,
      missing: [],
      generation: 7,
      calculated_at: "2026-09-22T05:59:00+00:00",
      historical: false,
      price_identity: "price-1",
      price_state: "ready",
      today: "2026-09-22",
      tomorrow: "2026-09-23",
      applied_identity: "installed-1",
      pending_identity: null,
      pending_attempt: null,
      price_wait: null,
      publication_at: null,
      must_buy_now_kwh: null,
    },
    market: {
      catalogue_state: "ready",
      area_id: "SE4",
      area_name: "Malmö",
      countries: ["SE"],
      timezone: "Europe/Stockholm",
      currency: "SEK",
      major_unit: "kr",
      minor_unit: "öre",
      suggested_vat_percent: 25,
      catalogue_fetched_at: "2026-09-22T05:00:00+00:00",
      catalogue_attempt_error: null,
      suggested_tax: 36,
      suggested_grid_fee: 0,
    },
    prices: {
      area_id: "SE4",
      state: "ready",
      reason: "ready",
      today: "2026-09-22",
      tomorrow: "2026-09-23",
      waiting_for_tomorrow: false,
      unpriced: false,
      interval_count: 4,
      priced_slots: 4,
      unpriced_slots: 0,
      today_state: "ready",
      tomorrow_state: "ready",
      today_source: "relay",
      tomorrow_source: "relay",
      today_fetched_at: "2026-09-22T05:00:00+00:00",
      tomorrow_fetched_at: "2026-09-22T05:00:00+00:00",
      today_attempt_at: "2026-09-22T05:00:00+00:00",
      tomorrow_attempt_at: "2026-09-22T05:00:00+00:00",
      today_attempt_error: null,
      tomorrow_attempt_error: null,
      index_state: "ready",
      index_revision: "rev-1",
      resolution_minutes: 15,
      resolutions_minutes: [15],
      intervals: quarterHourRows("2026-09-22T04:00:00+00:00", [100, 200, 300, 400]),
    },
    plan: {
      proposal: {
        identity: "proposal-1",
        settings_revision: 3,
        periods: [{ start: "2026-09-22T04:00:00+00:00", end: "2026-09-22T05:00:00+00:00" }],
        unpriced: false,
        amps: 10,
        phases: 1,
        unpriced_slots: 0,
        priced_slots: 4,
        planned_kwh: 20,
        requested_kwh: 20,
        cost: { value: 22.5, currency: "SEK" },
        distance_mil: 8.5,
        power_kw: 2.3,
      },
      installed: {
        identity: "installed-1",
        periods: [
          { start: "2026-09-22T04:00:00+00:00", end: "2026-09-22T05:00:00+00:00" },
          { start: "2026-09-22T05:00:00+00:00", end: "2026-09-22T05:15:00+00:00" },
        ],
        amps: 10,
        phases: 1,
        power_kw: 2.3,
        active_period_index: 0,
      },
      relation: { applied: true, applied_identity: "installed-1", pending_identity: null },
      delivered_kwh: null,
      remaining_kwh: null,
    },
    live: {
      charging: false,
      schedule_active: true,
      requested_current_a: 10,
      setpoint_current_a: null,
      measured_current_a: null,
    },
    strategy: {
      selected: "cheapest",
      available: [
        { strategy: "cheapest", available: true, reason: null },
        { strategy: "solar", available: false, reason: "needs_solar_surplus_measurement" },
        { strategy: "hybrid", available: false, reason: "needs_solar_and_price_control" },
      ],
    },
    strategy_options: ["cheapest"],
    strategy_state: null,
    // An idle Auto charger: `live.charging` is false, so the immediate axis is exactly a Start --
    // while the automatic axis offers its own Pause, with the choices this charger can honour.
    control: {
      immediate_action: "start",
      immediate_action_reason: null,
      automatic_action: "pause",
      automatic_action_reason: null,
      pause_choices: ["until_tomorrow", "until_resumed"],
      pause: { choice: null, admitted_at: null, expires_at: null },
      pause_blocks_execution: false,
      execution_error: null,
      can_act: true,
    },
    charge_progress: null,
    site: null,
    current_range: null,
    soc: null,
    vehicles: [],
    target_vehicle_id: null,
    detected_phases: null,
    phase_detection: { source: "unknown", confidence: "none" },
    chargers: [{ id: "entry_a", name: "Garage" }],
    status: plannedStatus(),
    summary: {
      charger: {
        start_stop_name: "Charge switch",
        current_path: "none",
        current_entity_name: null,
        energy_name: null,
        energy_automatic: true,
      },
      site: null,
      vehicles: {},
    },
    ...overrides,
  };
}

/** One status line, as Home Assistant's composer emits it: every param of the code, `null` unless given. */
export function statusLine(code: string, params: Record<string, unknown> = {}): { code: string; params: Record<string, unknown> } {
  const table = STATUS_CODE_TABLE as Record<string, readonly [string, Record<string, string>]>;
  const kinds = table[code]?.[1] ?? {};
  return { code, params: Object.fromEntries(Object.keys(kinds).map((key) => [key, params[key] ?? null])) };
}

/** The status block of the default fixture: planned from 06:00 with the proposal's own figures. */
export function plannedStatus(kwh = 20): { tone: string; lines: Array<{ code: string; params: Record<string, unknown> }> } {
  return {
    tone: "normal",
    lines: [
      statusLine("auto_planned", { start: "2026-09-22T04:00:00+00:00" }),
      statusLine("plan_energy", { kwh }),
      statusLine("plan_cost", { amount_minor: 2250, currency: "SEK" }),
      statusLine("plan_distance", { mil: 8.5 }),
    ],
  };
}

/** Quarter-hour rows from an ISO start, one per price, `day` defaulting to the start's local day. */
export function quarterHourRows(
  startIso: string,
  prices: readonly (number | null)[],
  options: { duration?: number; day?: string; flags?: (index: number) => Partial<Record<string, unknown>> } = {},
): Array<Record<string, unknown>> {
  const duration = options.duration ?? 15;
  const startMs = Date.parse(startIso);
  const rows: Array<Record<string, unknown>> = [];
  for (const [index, price] of prices.entries()) {
    const from = startMs + index * duration * 60_000;
    const to = from + duration * 60_000;
    rows.push({
      start: new Date(from).toISOString(),
      end: new Date(to).toISOString(),
      day: options.day ?? new Date(from).toISOString().slice(0, 10),
      duration_minutes: duration,
      raw_price: price,
      effective_price: price,
      proposal_planned: false,
      installed_planned: false,
      ...(options.flags?.(index) ?? {}),
    });
  }
  return rows;
}

/** The decoded form of a raw payload; throws if the fixture itself is malformed. */
export function decoded(overrides: Record<string, unknown> = {}): Dashboard {
  const result = decodeDashboard(rawDashboard(overrides));
  if (!result.ok) {
    throw new Error(`fixture did not decode: ${result.failure}`);
  }
  return result.value;
}

/**
 * A charger with nothing yet: no settings record, no calculation snapshot, no market -- the
 * serializer's own no-snapshot branch (`planning` and `market` null, every price leaf null, the
 * relation's `applied` null, both control axes `no_settings`).
 */
export function rawBare(): Record<string, unknown> {
  return rawDashboard({
    settings: null,
    fiscal: null,
    planning: null,
    market: null,
    prices: {
      area_id: null,
      state: null,
      reason: null,
      today: null,
      tomorrow: null,
      today_state: null,
      tomorrow_state: null,
      today_source: null,
      tomorrow_source: null,
      today_fetched_at: null,
      tomorrow_fetched_at: null,
      today_attempt_at: null,
      tomorrow_attempt_at: null,
      today_attempt_error: null,
      tomorrow_attempt_error: null,
      index_state: null,
      index_revision: null,
      waiting_for_tomorrow: null,
      resolution_minutes: null,
      resolutions_minutes: [],
      priced_slots: null,
      unpriced_slots: null,
      unpriced: null,
      interval_count: 0,
      intervals: [],
    },
    plan: {
      proposal: null,
      installed: null,
      relation: { applied: null, applied_identity: null, pending_identity: null },
      delivered_kwh: null,
      remaining_kwh: null,
    },
    live: {
      charging: false,
      schedule_active: false,
      requested_current_a: null,
      setpoint_current_a: null,
      measured_current_a: null,
    },
    strategy: {
      selected: null,
      available: [
        { strategy: "solar", available: false, reason: "needs_solar_surplus_measurement" },
        { strategy: "hybrid", available: false, reason: "needs_solar_and_price_control" },
      ],
    },
    control: {
      immediate_action: "none",
      immediate_action_reason: "no_settings",
      automatic_action: "none",
      automatic_action_reason: "no_settings",
      pause_choices: [],
      pause: null,
      pause_blocks_execution: false,
      execution_error: null,
      can_act: true,
    },
    status: { tone: "normal", lines: [statusLine("no_plan")] },
  });
}

/** The decoded bare answer; throws if the fixture itself is not valid. */
export function bare(): Dashboard {
  const result = decodeDashboard(rawBare());
  if (!result.ok) {
    throw new Error(`bare fixture did not decode: ${result.failure}`);
  }
  return result.value;
}
