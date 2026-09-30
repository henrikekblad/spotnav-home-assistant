// The decoder boundary: every field the card reads, and every way a payload can be wrong.

import { describe, expect, it } from "vitest";

import { decodeDashboard, isValidTimeZone } from "../src/validate";
import { bare, plannedStatus, rawDashboard, rawBare } from "./dashboard-fixtures";

const decode = (payload: unknown) => decodeDashboard(payload);

function withSection(key: string, value: unknown): Record<string, unknown> {
  return rawDashboard({ [key]: value });
}

function patch(section: string, patchValue: Record<string, unknown>): Record<string, unknown> {
  const base = rawDashboard();
  return rawDashboard({ [section]: { ...(base[section] as Record<string, unknown>), ...patchValue } });
}

describe("the outer envelope", () => {
  it("accepts the documented answer and types it as v1", () => {
    const result = decode(rawDashboard());
    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    // No assertion or cast needed to read a decoded field: it is typed.
    expect(result.value.api_version).toBe(1);
    expect(result.value.charger.charger_id).toBe("entry_a");
    expect(result.value.prices?.intervals.length).toBe(4);
    expect(result.value.plan?.relation.applied).toBe(true);
  });

  it("refuses a non-object, a missing version and a non-numeric version as malformed", () => {
    expect(decode(null)).toEqual({ ok: false, failure: "malformed" });
    expect(decode([])).toEqual({ ok: false, failure: "malformed" });
    expect(decode("nope")).toEqual({ ok: false, failure: "malformed" });
    const { api_version: _omitted, ...rest } = rawDashboard();
    expect(decode(rest)).toEqual({ ok: false, failure: "malformed" });
    expect(decode(rawDashboard({ api_version: "1" }))).toEqual({ ok: false, failure: "malformed" });
  });

  it("reports another version as unsupported rather than as malformed", () => {
    for (const version of [0, 2, 3, 4, 7]) {
      expect(decode(rawDashboard({ api_version: version })), String(version)).toEqual({
        ok: false,
        failure: "unsupported",
      });
    }
  });

  it("refuses an extra top-level key, while a section keeps its additive tolerance", () => {
    // The dashboard-v2 top level is exact-key: a field the card did not expect means it is reading a
    // document it does not fully know, so it is refused rather than rendered half of.
    expect(decode(rawDashboard({ something_new: { nested: true } }))).toEqual({
      ok: false,
      failure: "malformed",
    });
    // A price section is still additive-tolerant, which is what keeps an installed card working when
    // the backend adds a field *inside* it; the settings record is not (it is an editable document).
    const withExtra = rawDashboard({
      prices: { ...(rawDashboard().prices as Record<string, unknown>), something_new: 1 },
    });
    expect(decode(withExtra).ok).toBe(true);
    const settingsExtra = rawDashboard({
      settings: { ...(rawDashboard().settings as Record<string, unknown>), something_new: 1 },
    });
    expect(decode(settingsExtra)).toEqual({ ok: false, failure: "malformed" });
  });

  it("carries no payload text or exception message in a failure", () => {
    const hostile = rawDashboard({ battery: "<img src=x onerror=alert(1)>" });
    (hostile.prices as Record<string, unknown>).intervals = [{ start: "SECRET-TOKEN" }];
    const result = decode(hostile);
    expect(result).toEqual({ ok: false, failure: "malformed" });
    expect(JSON.stringify(result)).not.toContain("SECRET-TOKEN");
    expect(JSON.stringify(result)).not.toContain("<img");
  });
});

describe("nullable sections", () => {
  it.each(["settings", "planning", "market", "fiscal", "site", "soc", "charge_progress", "current_range"])(
    "accepts a null %s section",
    (section) => {
      const result = decode(withSection(section, null));
      expect(result.ok).toBe(true);
      if (result.ok) {
        expect(result.value[section as "settings"]).toBeNull();
      }
    },
  );

  it("still requires the charger section", () => {
    expect(decode(withSection("charger", null))).toEqual({ ok: false, failure: "malformed" });
  });

  it("refuses a null live section: v1 always emits the charger's own facts", () => {
    expect(decode(withSection("live", null))).toEqual({ ok: false, failure: "malformed" });
  });

  it("refuses a null prices or plan section: both are objects in v1, absence lives inside", () => {
    for (const section of ["prices", "plan"]) {
      expect(decode(withSection(section, null)), section).toEqual({
        ok: false,
        failure: "malformed",
      });
    }
  });

  it("refuses a missing section rather than treating it as null", () => {
    const payload = rawDashboard();
    for (const section of ["settings", "fiscal", "planning", "market", "prices", "plan", "live", "soc", "current_range", "charge_progress", "chargers"]) {
      const stripped = { ...payload };
      delete stripped[section];
      expect(decode(stripped), section).toEqual({ ok: false, failure: "malformed" });
    }
  });
});

describe("the charger and its capabilities", () => {
  it("requires every capability boolean to be a boolean", () => {
    const base = rawDashboard();
    const charger = base.charger as Record<string, unknown>;
    const capabilities = charger.capabilities as Record<string, unknown>;
    expect(decode(withSection("charger", { ...charger, capabilities: { ...capabilities, target_soc: "yes" } })))
      .toEqual({ ok: false, failure: "malformed" });
    expect(decode(withSection("charger", { ...charger, available: 1 }))).toEqual({
      ok: false,
      failure: "malformed",
    });
    expect(decode(withSection("charger", { ...charger, charger_id: null }))).toEqual({
      ok: false,
      failure: "malformed",
    });
  });

  it("keeps a false capability, a null name and a zero revision as real values", () => {
    const result = decode(patch("settings", { revision: 0 }));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.charger.capabilities.target_soc).toBe(false);
      expect(result.value.settings?.revision).toBe(0);
    }
  });
});

describe("numbers are finite, and never booleans", () => {
  it.each([Number.NaN, Number.POSITIVE_INFINITY, "10", true])(
    "refuses %p as a settings revision",
    (value) => {
      expect(decode(patch("settings", { revision: value }))).toEqual({ ok: false, failure: "malformed" });
    },
  );

  it("refuses a non-finite price and a wrong planned flag", () => {
    const rows = rawDashboard().prices as Record<string, unknown>;
    const first = (rows.intervals as Array<Record<string, unknown>>)[0] as Record<string, unknown>;
    const bad = rawDashboard();
    (bad.prices as Record<string, unknown>).intervals = [{ ...first, effective_price: Number.NaN }];
    expect(decode(bad)).toEqual({ ok: false, failure: "malformed" });
    const flags = rawDashboard();
    (flags.prices as Record<string, unknown>).intervals = [{ ...first, installed_planned: "yes" }];
    expect(decode(flags)).toEqual({ ok: false, failure: "malformed" });
  });
});

describe("instants, ordering and coverage", () => {
  it("refuses a naive timestamp", () => {
    const bad = rawDashboard();
    const rows = (bad.prices as Record<string, unknown>).intervals as Array<Record<string, unknown>>;
    rows[0] = { ...(rows[0] as Record<string, unknown>), start: "2026-09-22T04:00:00" };
    expect(decode(bad)).toEqual({ ok: false, failure: "malformed" });
  });

  it("refuses an invalid date, end <= start and a duration that disagrees with the span", () => {
    const base = rawDashboard();
    const rows = (base.prices as Record<string, unknown>).intervals as Array<Record<string, unknown>>;
    const first = rows[0] as Record<string, unknown>;
    const cases: Array<Record<string, unknown>> = [
      { start: "not-a-date+00:00" },
      { end: first.start },
      { end: "2026-09-22T03:00:00+00:00" },
      { duration_minutes: 0 },
      { duration_minutes: -15 },
      { duration_minutes: 60 },
    ];
    for (const change of cases) {
      const payload = rawDashboard();
      (payload.prices as Record<string, unknown>).intervals = [{ ...first, ...change }];
      expect(decode(payload), JSON.stringify(change)).toEqual({ ok: false, failure: "malformed" });
    }
  });

  it("refuses out-of-order and overlapping rows", () => {
    const base = rawDashboard();
    const rows = (base.prices as Record<string, unknown>).intervals as Array<Record<string, unknown>>;
    const outOfOrder = rawDashboard();
    (outOfOrder.prices as Record<string, unknown>).intervals = [rows[1], rows[0]];
    expect(decode(outOfOrder)).toEqual({ ok: false, failure: "malformed" });
    const overlap = rawDashboard();
    (overlap.prices as Record<string, unknown>).intervals = [
      rows[0],
      { ...(rows[1] as Record<string, unknown>), start: "2026-09-22T04:10:00+00:00", end: "2026-09-22T04:25:00+00:00", duration_minutes: 15 },
    ];
    expect(decode(overlap)).toEqual({ ok: false, failure: "malformed" });
  });

  it("refuses a row whose day is not its own local day in the market zone", () => {
    const base = rawDashboard();
    const rows = (base.prices as Record<string, unknown>).intervals as Array<Record<string, unknown>>;
    (base.prices as Record<string, unknown>).intervals = [{ ...(rows[0] as Record<string, unknown>), day: "2026-09-23" }];
    expect(decode(base)).toEqual({ ok: false, failure: "malformed" });
  });

  it("validates the market timezone instead of falling back to the browser's", () => {
    expect(isValidTimeZone("Europe/Stockholm")).toBe(true);
    expect(isValidTimeZone("Mars/Olympus")).toBe(false);
    expect(decode(patch("market", { timezone: "Mars/Olympus" }))).toEqual({ ok: false, failure: "malformed" });
    const withoutMarket = decode(withSection("market", null));
    expect(withoutMarket.ok).toBe(true); // no zone named, nothing to validate or pretend
  });
});

describe("the plan's own shapes", () => {
  it("accepts a valid installed schedule beside a malformed proposal by refusing the payload", () => {
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    expect(decode(withSection("plan", { ...plan, proposal: { periods: "none" } }))).toEqual({
      ok: false,
      failure: "malformed",
    });
    expect(decode(withSection("plan", { ...plan, installed: { ...(plan.installed as object), periods: [{ start: "2026-09-22T05:00:00+00:00", end: "2026-09-22T05:00:00+00:00" }] } })))
      .toEqual({ ok: false, failure: "malformed" });
  });

  it("keeps a proposal and an installed schedule as separate objects", () => {
    const result = decode(rawDashboard());
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.plan?.proposal?.identity).toBe("proposal-1");
      expect(result.value.plan?.installed?.identity).toBe("installed-1");
      expect(result.value.plan?.proposal?.periods.length).toBe(1);
      expect(result.value.plan?.installed?.periods.length).toBe(2);
    }
  });
});

describe("immutability and determinism", () => {
  it("does not mutate the caller's payload", () => {
    const payload = rawDashboard();
    const snapshot = JSON.stringify(payload);
    const result = decode(payload);
    expect(result.ok).toBe(true);
    expect(JSON.stringify(payload)).toBe(snapshot);
  });

  it("decodes the same payload identically twice", () => {
    const first = decode(rawDashboard());
    const second = decode(rawDashboard());
    expect(first).toEqual(second);
  });
});

describe("the bare shape (no settings, no calculation snapshot)", () => {
  it("decodes with null settings, planning and market and a null relation.applied", () => {
    const result = decode(rawBare());
    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.value.settings).toBeNull();
    expect(result.value.planning).toBeNull();
    expect(result.value.market).toBeNull();
    expect(result.value.plan?.proposal).toBeNull();
    expect(result.value.plan?.installed).toBeNull();
    expect(result.value.plan?.relation).toEqual({
      applied: null,
      applied_identity: null,
      pending_identity: null,
    });
    expect(result.value.prices?.intervals).toEqual([]);
    expect(result.value.prices?.unpriced).toBeNull();
    expect(result.value.prices?.interval_count).toBe(0);
  });

  it("still refuses a boolean-only relation", () => {
    const payload = rawBare();
    const plan = payload.plan as Record<string, unknown>;
    (plan.relation as Record<string, unknown>).applied = "yes";
    expect(decode(payload)).toEqual({ ok: false, failure: "malformed" });
  });

  it("refuses a relation.applied that is neither a boolean nor null", () => {
    const payload = rawBare();
    ((payload.plan as Record<string, unknown>).relation as Record<string, unknown>).applied = 0;
    expect(decode(payload)).toEqual({ ok: false, failure: "malformed" });
  });
});

describe("required keys versus nullable values", () => {
  const requiredKeys: Array<[string, string, string]> = [
    ["plan", "proposal", "plan.proposal"],
    ["plan", "installed", "plan.installed"],
    ["plan", "relation", "plan.relation"],
    ["plan", "delivered_kwh", "plan.delivered_kwh"],
    ["plan", "remaining_kwh", "plan.remaining_kwh"],
    ["plan", "relation", "applied"],
    ["plan", "relation", "applied_identity"],
    ["plan", "relation", "pending_identity"],
    ["prices", "unpriced", "prices.unpriced"],
    ["prices", "resolutions_minutes", "prices.resolutions_minutes"],
    ["prices", "interval_count", "prices.interval_count"],
    ["prices", "intervals", "prices.intervals"],
    ["prices", "state", "prices.state"],
    ["market", "timezone", "market.timezone"],
    ["market", "currency", "market.currency"],
    ["planning", "state", "planning.state"],
    ["planning", "missing", "planning.missing"],
    ["settings", "revision", "settings.revision"],
    ["live", "charging", "live.charging"],
  ];

  it.each(requiredKeys)("refuses a missing %s.%s", (section, key) => {
    const payload = rawDashboard();
    delete (payload[section] as Record<string, unknown>)[key];
    expect(decode(payload), `${section}.${key}`).toEqual({ ok: false, failure: "malformed" });
  });

  const nullableKeys: Array<[string, string]> = [
    // `plan.proposal` and `plan.installed` are the serializer's explicit `None` leaves: required
    // keys whose value may honestly be null, which is exactly what `required()` distinguishes.
    ["plan", "proposal"],
    ["plan", "installed"],
    ["plan", "delivered_kwh"],
    ["plan", "remaining_kwh"],
    ["plan", "relation"],
    ["prices", "unpriced"],
    ["prices", "priced_slots"],
  ];

  it.each(nullableKeys)("accepts an explicit null for the nullable %s.%s", (section, key) => {
    const payload = rawDashboard();
    const relation = { applied: null, applied_identity: null, pending_identity: null };
    (payload[section] as Record<string, unknown>)[key] = key === "relation" ? relation : null;
    const result = decode(payload);
    expect(result.ok, `${section}.${key}`).toBe(true);
    if (result.ok && section === "plan" && (key === "proposal" || key === "installed")) {
      // The distinction itself: null is preserved as an absence, not turned into an object.
      expect(result.value.plan[key]).toBeNull();
    }
  });

  it("keeps a required non-null field required even when it is explicitly null", () => {
    for (const [section, key] of [
      ["prices", "interval_count"],
      ["prices", "intervals"],
      ["settings", "revision"],
    ] as Array<[string, string]>) {
      const payload = rawDashboard();
      (payload[section] as Record<string, unknown>)[key] = null;
      expect(decode(payload), `${section}.${key}`).toEqual({ ok: false, failure: "malformed" });
    }
  });

  it("refuses a missing top-level section, and accepts a present null one", () => {
    for (const section of ["settings", "fiscal", "planning", "market", "prices", "plan", "live", "strategy_options", "phase_detection"]) {
      const missing = rawDashboard();
      delete missing[section];
      expect(decode(missing), section).toEqual({ ok: false, failure: "malformed" });
    }
    const nulled = rawDashboard({
      settings: null,
      planning: null,
      market: null,
      fiscal: null,
    });
    const result = decode(nulled);
    expect(result.ok).toBe(true);
    if (result.ok) {
      // The two required sections stay objects even when every nullable leaf inside is null.
      expect(typeof result.value.prices).toBe("object");
      expect(typeof result.value.plan).toBe("object");
      // Nulling those four sections does not touch the plan's own nullable leaves.
      expect(result.value.plan.proposal).not.toBeNull();
      expect(result.value.plan.relation.applied).toBe(true);
    }
  });

  it("still accepts extra additive keys", () => {
    const payload = rawDashboard();
    (payload.prices as Record<string, unknown>).tomorrow_extra = { anything: true };
    expect(decode(payload).ok).toBe(true);
  });
});

describe("the strategy block", () => {
  const withStrategy = (value: unknown) => withSection("strategy", value);
  const selected = (reason: string | null = null) => ({
    strategy: "cheapest",
    available: true,
    reason,
  });
  const solar = (reason: string | null = "needs_solar_surplus_measurement") => ({
    strategy: "solar",
    available: false,
    reason,
  });

  it("reads the rows the way the backend states them", () => {
    const result = decode(withStrategy({ selected: "cheapest", available: [selected(), solar()] }));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.strategy.selected).toBe("cheapest");
      expect(result.value.strategy.rows).toEqual([
        { strategy: "cheapest", available: true, reason: null },
        { strategy: "solar", available: false, reason: "needs_solar_surplus_measurement" },
      ]);
    }
  });

  it("refuses a block whose keys are not exactly the contract's", () => {
    expect(decode(withStrategy({ selected: "cheapest", available: [] })).ok).toBe(false);
    expect(
      decode(withStrategy({ selected: "cheapest", available: [], extra: 1 })).ok,
    ).toBe(false);
    expect(decode(withStrategy(null)).ok).toBe(false);
    expect(decode(withStrategy({ selected: "cheapest" })).ok).toBe(false);
  });

  it("refuses a row with unknown keys, an unknown strategy or a non-boolean availability", () => {
    const bad = (row: unknown) => decode(withStrategy({ selected: "cheapest", available: [selected(), row] }));
    expect(bad({ strategy: "wind", available: false, reason: "x" }).ok).toBe(false);
    expect(bad({ strategy: "solar", available: "false", reason: "x" }).ok).toBe(false);
    expect(bad({ strategy: "solar", available: false, reason: "x", extra: true }).ok).toBe(false);
  });

  it("refuses a reason the card cannot translate rather than rendering it as a sentence", () => {
    expect(decode(withStrategy({ selected: "cheapest", available: [selected(), solar("because")] })).ok).toBe(
      false,
    );
  });

  it("refuses a payload that describes a product this release does not have", () => {
    // A named strategy that is not the available row, the selected row itself saying it is not
    // available (even beside a genuinely available one -- v6's own shape, a second test below), an
    // unavailable Cheapest, an available Solar with no reason to explain it, an unexplained
    // unavailable row and a duplicated row are all impossible.
    expect(decode(withStrategy({ selected: "cheapest", available: [solar()] })).ok).toBe(false);
    expect(
      decode(
        withStrategy({
          selected: "solar",
          available: [{ strategy: "cheapest", available: true, reason: null }, solar()],
        }),
      ).ok,
    ).toBe(false);
    expect(
      decode(withStrategy({ selected: "cheapest", available: [{ ...selected(), available: false }] })).ok,
    ).toBe(false);
    expect(
      decode(withStrategy({ selected: "cheapest", available: [selected(), solar(null)] })).ok,
    ).toBe(false);
    expect(decode(withStrategy({ selected: "cheapest", available: [selected(), selected()] })).ok).toBe(
      false,
    );
    expect(decode(withStrategy({ selected: null, available: [selected()] })).ok).toBe(false);
    expect(decode(withStrategy({ selected: null, available: [solar()] })).ok).toBe(true);
  });

  it("accepts v6's own shape: every row available at once, as long as the selected one is among them", () => {
    // `serialize_strategy_v6` turns Cheapest, Solar and Hybrid all available together on a site that
    // can run every strategy -- unlike v2/v3, where the selected row was always the *only* one that
    // could be. `selected` still has to be one of the available rows; it no longer has to be alone.
    const result = decode(
      rawDashboard({
        strategy: {
          selected: "hybrid",
          available: [
            { strategy: "cheapest", available: true, reason: null },
            { strategy: "solar", available: true, reason: null },
            { strategy: "hybrid", available: true, reason: null },
          ],
        },
        strategy_options: ["cheapest", "solar", "hybrid"],
        strategy_state: {
          state: "no_forecast",
          reason: "no_forecast_cheapest",
          grid_kwh: 20,
          credit_kwh: 0,
          slack_kwh: 1,
          plan_window_active: false,
          forecast_configured: false,
        },
      }),
    );
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.strategy.rows.every((row) => row.available)).toBe(true);
    }
  });
});

describe("the control block", () => {
  const pause = (over: Record<string, unknown> = {}) => ({
    choice: null,
    admitted_at: null,
    expires_at: null,
    ...over,
  });
  // An idle Auto charger: the immediate axis is Start now, and the automatic axis offers its own
  // Pause. Both axes are present in every case below, because that is the shape the contract fixes.
  const control = (over: Record<string, unknown> = {}) => ({
    immediate_action: "start",
    immediate_action_reason: null,
    automatic_action: "pause",
    automatic_action_reason: null,
    pause_choices: [],
    pause: pause(),
    pause_blocks_execution: false,
    execution_error: null,
    can_act: true,
    ...over,
  });
  const withControl = (value: unknown) => withSection("control", value);
  /** The two axes of one decoded answer, as a comparable pair. */
  const axes = (result: ReturnType<typeof decode>) =>
    result.ok
      ? [result.value.control.immediate_action, result.value.control.automatic_action]
      : null;

  it("reads both axes, the choices, the pause and the authority flag", () => {
    const result = decode(
      withControl(
        control({
          immediate_action: "stop",
          pause_choices: ["until_tomorrow", "until_resumed"],
          pause_blocks_execution: true,
          execution_error: "pause_stop_failed",
          can_act: false,
          pause: pause({
            choice: "until_tomorrow",
            admitted_at: "2026-09-22T18:00:00+02:00",
            expires_at: "2026-09-23T00:00:00+02:00",
          }),
        }),
      ),
    );
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.control).toEqual({
        immediate_action: "stop",
        immediate_action_reason: null,
        automatic_action: "pause",
        automatic_action_reason: null,
        pause_choices: ["until_tomorrow", "until_resumed"],
        pause: {
          choice: "until_tomorrow",
          admitted_at: "2026-09-22T18:00:00+02:00",
          admitted_at_ms: Date.parse("2026-09-22T18:00:00+02:00"),
          expires_at: "2026-09-23T00:00:00+02:00",
          expires_at_ms: Date.parse("2026-09-23T00:00:00+02:00"),
        },
        pause_blocks_execution: true,
        execution_error: "pause_stop_failed",
        can_act: false,
      });
    }
  });

  it("reads both axes at once, for the rows of the product's matrix", () => {
    // Neither axis is folded into the other, and an idle charger is Start now *and* Pause -- which is
    // the whole point of the split. The immediate axis follows the charger; the automatic one follows
    // the planning record.
    expect(axes(decode(withControl(control())))).toEqual(["start", "pause"]);
    expect(
      axes(decode(withControl(control({ immediate_action: "stop", pause_choices: ["until_resumed"] })))),
    ).toEqual(["stop", "pause"]);
    expect(
      axes(
        decode(
          withControl(
            control({
              automatic_action: "resume",
              pause: pause({ choice: "until_resumed" }),
              pause_blocks_execution: true,
            }),
          ),
        ),
      ),
      "the charger reads itself while Auto's own axis is the Resume",
    ).toEqual(["start", "resume"]);
    expect(
      axes(
        decode(
          withControl(
            control({ immediate_action: "stop", automatic_action: "resume", automatic_action_reason: null }),
          ),
        ),
      ),
    ).toEqual(["stop", "resume"]);
    expect(
      axes(
        decode(
          withControl(
            control({ automatic_action: "none", automatic_action_reason: "pause_unsettled" }),
          ),
        ),
      ),
    ).toEqual(["start", "none"]);
  });
  it("refuses a block whose keys are not exactly the contract's", () => {
    for (const broken of [control({ extra: 1 }), control({ can_act: undefined })]) {
      expect(decode(withControl(broken)).ok).toBe(false);
    }
    const { can_act: _dropped, ...missing } = control();
    expect(decode(withControl(missing)).ok).toBe(false);
    expect(decode(withControl(null)).ok).toBe(false);
    // v2's folded key is not accepted here: a v2 payload read as v3 would have to invent the other
    // axis, and inventing it is exactly what the contract forbids.
    expect(decode(withControl({ ...control(), primary_action: "start" })).ok).toBe(false);
    // And neither axis may be left out.
    const { immediate_action: _immediate, ...withoutImmediate } = control();
    const { automatic_action: _automatic, ...withoutAutomatic } = control();
    expect(decode(withControl(withoutImmediate)).ok).toBe(false);
    expect(decode(withControl(withoutAutomatic)).ok).toBe(false);
  });

  it("refuses an unknown action, a reason from the wrong axis or a wrongly typed flag", () => {
    expect(decode(withControl(control({ immediate_action: "charge" }))).ok).toBe(false);
    expect(decode(withControl(control({ immediate_action: null }))).ok).toBe(false);
    expect(decode(withControl(control({ automatic_action: "hibernate" }))).ok).toBe(false);
    expect(decode(withControl(control({ automatic_action: null }))).ok).toBe(false);
    // The two axes' vocabularies are separate sets: a `pause` as the immediate action, or a `start`
    // or a `stop` as the automatic one, describes a product that does not exist -- and the card must
    // not render one of them as if it did.
    expect(decode(withControl(control({ immediate_action: "pause" }))).ok).toBe(false);
    expect(decode(withControl(control({ automatic_action: "start" }))).ok).toBe(false);
    expect(decode(withControl(control({ automatic_action: "stop" }))).ok).toBe(false);
    // Their reasons are separate sets too: the immediate axis cannot say "an external schedule" or
    // "a pause is not settled", because it is blind to the planning record by construction.
    expect(decode(withControl(control({ immediate_action_reason: "external_authority" }))).ok).toBe(
      false,
    );
    expect(decode(withControl(control({ immediate_action_reason: "pause_unsettled" }))).ok).toBe(
      false,
    );
    expect(decode(withControl(control({ immediate_action_reason: "because" }))).ok).toBe(false);
    expect(decode(withControl(control({ automatic_action_reason: "because" }))).ok).toBe(false);
    // And one shared code -- a Start awaiting acknowledgement -- is legal on both.
    expect(
      decode(
        withControl(
          control({
            immediate_action: "none",
            immediate_action_reason: "action_pending",
            automatic_action: "none",
            automatic_action_reason: "action_pending",
          }),
        ),
      ).ok,
    ).toBe(true);
    expect(decode(withControl(control({ can_act: "yes" }))).ok).toBe(false);
    expect(decode(withControl(control({ pause_blocks_execution: null }))).ok).toBe(false);
    expect(decode(withControl(control({ execution_error: 5 }))).ok).toBe(false);
  });
  it("reads each reason the card can translate, on the axis that reason belongs to", () => {
    const persisted = pause({
      choice: "until_tomorrow",
      admitted_at: "2026-09-22T18:00:00+00:00",
      expires_at: "2026-09-23T00:00:00+00:00",
    });
    const cases: Array<[string, Record<string, unknown>, [string, string | null], [string, string | null]]> = [
      [
        "no_settings",
        control({
          immediate_action: "none",
          immediate_action_reason: "no_settings",
          automatic_action: "none",
          automatic_action_reason: "no_settings",
          pause: null,
        }),
        ["none", "no_settings"],
        ["none", "no_settings"],
      ],
      [
        "action_pending",
        control({
          immediate_action: "none",
          immediate_action_reason: "action_pending",
          automatic_action: "none",
          automatic_action_reason: "action_pending",
        }),
        ["none", "action_pending"],
        ["none", "action_pending"],
      ],
      [
        "pause_unsettled",
        control({ automatic_action: "none", automatic_action_reason: "pause_unsettled" }),
        ["start", null],
        ["none", "pause_unsettled"],
      ],
      [
        "pause_clear_failed",
        control({
          automatic_action: "none",
          automatic_action_reason: "pause_clear_failed",
          pause: persisted,
          pause_blocks_execution: true,
        }),
        ["start", null],
        ["none", "pause_clear_failed"],
      ],
    ];
    for (const [reason, shape, wantImmediate, wantAutomatic] of cases) {
      const result = decode(withControl(shape));
      expect(result.ok, reason).toBe(true);
      if (result.ok) {
        expect(
          [result.value.control.immediate_action, result.value.control.immediate_action_reason],
          reason,
        ).toEqual(wantImmediate);
        expect(
          [result.value.control.automatic_action, result.value.control.automatic_action_reason],
          reason,
        ).toEqual(wantAutomatic);
      }
    }
  });

  it("refuses choices that are unknown, duplicated or offered beside a non-pause action", () => {
    expect(decode(withControl(control({ pause_choices: ["forever"] }))).ok).toBe(false);
    // Choices belong to the automatic pause: not to a resume, not to `none`, and not to the immediate
    // axis either (an immediate stop is a bare stop).
    expect(
      decode(withControl(control({ automatic_action: "resume", pause_choices: ["until_resumed"] }))).ok,
    ).toBe(false);
    expect(
      decode(
        withControl(
          control({
            automatic_action: "none",
            automatic_action_reason: "external_authority",
            pause_choices: ["until_resumed"],
          }),
        ),
      ).ok,
    ).toBe(false);
    expect(
      decode(
        withControl(
          control({ immediate_action: "stop", pause_choices: ["until_resumed"], automatic_action: "none", automatic_action_reason: "external_authority" }),
        ),
      ).ok,
    ).toBe(false);
    expect(
      decode(
        withControl(control({ pause_choices: ["until_resumed", "until_resumed"] })),
      ).ok,
    ).toBe(false);
    // A pause with no choices at all is legal: it means this charger can honour none right now, and
    // the card must then open no sheet rather than invent a fallback.
    expect(decode(withControl(control())).ok).toBe(true);
  });
  it("reads every pause record the store itself can hold, including a migrated one", () => {
    const withPause = (value: Record<string, unknown>) =>
      withControl(control({ automatic_action: "resume", pause: value, pause_blocks_execution: true }));
    // A pause without an admission instant: the absence is a *fact*, not a gap. It must decode for both the indefinite shape and a
    // timed one -- and the card must not invent an instant to fill it in.
    const migratedIndefinite = decode(withPause(pause({ choice: "until_resumed" })));
    expect(migratedIndefinite.ok).toBe(true);
    if (migratedIndefinite.ok) {
      expect(migratedIndefinite.value.control.pause).toMatchObject({
        choice: "until_resumed",
        admitted_at: null,
        admitted_at_ms: null,
        expires_at: null,
      });
      expect(migratedIndefinite.value.control.automatic_action).toBe("resume");
    }
    const migratedTimed = decode(
      withPause(pause({ choice: "until_tomorrow", expires_at: "2026-09-23T00:00:00+00:00" })),
    );
    expect(migratedTimed.ok).toBe(true);
    if (migratedTimed.ok) {
      expect(migratedTimed.value.control.pause?.admitted_at_ms).toBeNull();
    }
    // A *full* timed record, which is what a pause admitted by this release looks like.
    expect(
      decode(
        withPause(
          pause({
            choice: "next_period",
            admitted_at: "2026-09-22T17:00:00+00:00",
            expires_at: "2026-09-22T18:00:00+00:00",
          }),
        ),
      ).ok,
    ).toBe(true);
  });

  it("refuses a pause whose three facts cannot describe a stored intent", () => {
    const withPause = (value: Record<string, unknown>) => withControl(control({ pause: value }));
    // A naive instant describes no instant at all, and a malformed one is not a time.
    expect(
      decode(withPause(pause({ choice: "until_resumed", admitted_at: "2026-09-22T06:00:00" }))).ok,
    ).toBe(false);
    expect(decode(withPause(pause({ choice: "until_resumed", admitted_at: "yesterday" }))).ok).toBe(
      false,
    );
    // An indefinite pause has no expiry; a timed one must have exactly one.
    expect(
      decode(
        withPause(
          pause({
            choice: "until_resumed",
            admitted_at: "2026-09-22T06:00:00+00:00",
            expires_at: "2026-09-22T07:00:00+00:00",
          }),
        ),
      ).ok,
    ).toBe(false);
    expect(
      decode(withPause(pause({ choice: "until_tomorrow", admitted_at: "2026-09-22T06:00:00+00:00" })))
        .ok,
    ).toBe(false);
    // Instants without a choice, and an unknown choice.
    expect(decode(withPause(pause({ admitted_at: "2026-09-22T06:00:00+00:00" }))).ok).toBe(false);
    expect(decode(withPause(pause({ choice: "forever", admitted_at: "2026-09-22T06:00:00+00:00" }))).ok).toBe(
      false,
    );
    expect(decode(withPause(pause({ choice: "until_resumed", extra: 1 }))).ok).toBe(false);
    // A pause must end after it was admitted: the store refuses it, so the card must too.
    expect(
      decode(
        withPause(
          pause({
            choice: "until_tomorrow",
            admitted_at: "2026-09-23T00:00:00+00:00",
            expires_at: "2026-09-22T00:00:00+00:00",
          }),
        ),
      ).ok,
    ).toBe(false);
    expect(
      decode(
        withPause(
          pause({
            choice: "until_tomorrow",
            admitted_at: "2026-09-23T00:00:00+00:00",
            expires_at: "2026-09-23T00:00:00+00:00",
          }),
        ),
      ).ok,
    ).toBe(false);
  });
  it("refuses a pause observation that is missing where it must be present", () => {
    // `null` is the no-settings fact and nothing else -- not a way to omit an answer, and it must be
    // *both* axes saying so.
    expect(decode(withControl(control({ pause: null }))).ok).toBe(false);
    expect(
      decode(withControl(control({ immediate_action: "stop", automatic_action: "resume", pause: null })))
        .ok,
    ).toBe(false);
    expect(
      decode(
        withControl(
          control({
            immediate_action: "none",
            immediate_action_reason: "no_settings",
            automatic_action: "none",
            automatic_action_reason: "no_settings",
            pause: null,
            pause_blocks_execution: true,
          }),
        ),
      ).ok,
      "a gate with no record behind it",
    ).toBe(false);
    const noSettings = decode(
      withControl(
        control({
          immediate_action: "none",
          immediate_action_reason: "no_settings",
          automatic_action: "none",
          automatic_action_reason: "no_settings",
          pause: null,
        }),
      ),
    );
    expect(noSettings.ok).toBe(true);
    if (noSettings.ok) {
      expect(noSettings.value.control.pause).toBeNull();
    }
  });

  it("refuses a control whose facts contradict each other", () => {
    const persisted = {
      choice: "until_tomorrow",
      admitted_at: "2026-09-22T18:00:00+00:00",
      expires_at: "2026-09-23T00:00:00+00:00",
    };
    // An actionable axis has no reason; `none` always has one -- judged per axis, so a reason beside
    // the immediate Start is as wrong as a reason beside the automatic Resume.
    expect(
      decode(withControl(control({ immediate_action_reason: "action_pending" }))).ok,
    ).toBe(false);
    expect(
      decode(
        withControl(
          control({ automatic_action: "resume", automatic_action_reason: "pause_unsettled" }),
        ),
      ).ok,
    ).toBe(false);
    expect(
      decode(
        withControl(
          control({ immediate_action: "none", immediate_action_reason: null, automatic_action: "none", automatic_action_reason: "external_authority" }),
        ),
      ).ok,
    ).toBe(false);
    // The execution gate and the persisted pause are two views of one fact.
    expect(
      decode(withControl(control({ pause: pause(persisted), pause_blocks_execution: false }))).ok,
    ).toBe(false);
    expect(
      decode(
        withControl(
          control({
            automatic_action: "resume",
            pause: pause(persisted),
            pause_blocks_execution: false,
          }),
        ),
      ).ok,
    ).toBe(false);
    // An elapsed-but-uncleared pause needs a persisted pause to be the failure *of*.
    expect(
      decode(
        withControl(
          control({
            automatic_action: "none",
            automatic_action_reason: "pause_clear_failed",
          }),
        ),
      ).ok,
    ).toBe(false);
    // And the valid retry state: a paused charger whose stop failed offers the automatic Pause again,
    // with the choices it would accept -- beside the immediate Stop that the charging charger calls for.
    const retry = decode(
      withControl(
        control({
          immediate_action: "stop",
          pause_choices: ["until_resumed"],
          pause: pause(persisted),
          pause_blocks_execution: true,
          execution_error: "pause_stop_failed",
        }),
      ),
    );
    expect(retry.ok).toBe(true);
    if (retry.ok) {
      expect(axes(retry)).toEqual(["stop", "pause"]);
    }
  });

  it("carries no payload text when the blocks are malformed", () => {
    const hostile = rawDashboard({
      control: control({ immediate_action_reason: "<img src=x onerror=alert(1)>" }),
    });
    const result = decode(hostile);
    expect(result).toEqual({ ok: false, failure: "malformed" });
    expect(JSON.stringify(result)).not.toContain("<img");
    expect(JSON.stringify(result)).not.toContain("onerror");
  });
});

/**
 * V4's `charge_progress`: the one value this decoder refuses *independently*.
 *
 * A malformed diagnostic may hide the advisory -- and only the advisory. Everything else in the
 * document has to decode exactly as it does for v3, because a card that threw away prices, a plan and
 * both control axes over one unreadable optional block would be losing far more than it gained.
 */
describe("the vehicle-side observation", () => {
  const v7 = (progress?: unknown): Record<string, unknown> => rawDashboard({ charge_progress: progress });

  it("accepts the documented block", () => {
    const result = decode(
      v7({
        state: "vehicle_not_requesting_current",
        reason: "suspended_ev_zero_current",
        since: "2026-09-27T12:00:00+00:00",
      }),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    expect(result.value.charge_progress).toEqual({
      state: "vehicle_not_requesting_current",
      reason: "suspended_ev_zero_current",
      since: "2026-09-27T12:00:00+00:00",
    });
  });

  it("accepts every state in the frozen vocabulary, `since` null included", () => {
    for (const state of ["normal", "vehicle_not_requesting_current", "unknown"]) {
      const result = decode(v7({ state, reason: "suspended_ev_zero_current_pending", since: null }));
      expect(result.ok).toBe(true);
      if (result.ok) {
        expect(result.value.charge_progress?.state).toBe(state);
        expect(result.value.charge_progress?.since).toBeNull();
      }
    }
  });

  it("hides the advisory for an unreadable block without losing anything else", () => {
    const broken: unknown[] = [
      undefined, // absent altogether: an integration that sent nothing to say
      null,
      "vehicle_not_requesting_current",
      [],
      { state: "suspended_ev", reason: "suspended_ev_zero_current", since: null }, // unknown state
      { state: "vehicle_not_requesting_current", since: null }, // no reason
      { state: "vehicle_not_requesting_current", reason: "x" }, // no since
      { state: "vehicle_not_requesting_current", reason: "x", since: 5 }, // wrong since type
      { state: "vehicle_not_requesting_current", reason: "x", since: null, extra: true }, // extra key
      { state: 7, reason: "x", since: null }, // wrong state type
    ];
    for (const progress of broken) {
      const result = decode(v7(progress));
      expect(result.ok, JSON.stringify(progress)).toBe(true);
      if (!result.ok) {
        continue;
      }
      expect(result.value.charge_progress).toBeNull();
      // The rest of the document is decoded in full.
      expect(result.value.charger.charger_id).toBe("entry_a");
      expect(result.value.prices.intervals.length).toBe(4);
      expect(result.value.control.immediate_action).toBeDefined();
    }
  });

  it("still refuses an unknown top-level key beside it", () => {
    const result = decode(
      rawDashboard({
        charge_progress: { state: "normal", reason: "charge_not_expected", since: null },
        something_new: true,
      }),
    );
    expect(result).toEqual({ ok: false, failure: "malformed" });
  });

  it("states no observation as null, rather than a defaulted one", () => {
    const result = decode(rawDashboard());
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.charge_progress).toBeNull();
    }
  });
});

describe("the status block", () => {
  const withStatus = (status: unknown): unknown => decode(rawDashboard({ status }));
  const line = (code: string, params: Record<string, unknown>) => ({ code, params });

  it("decodes the block with typed params", () => {
    const result = decode(rawDashboard());
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.status.tone).toBe("normal");
      expect(result.value.status.lines.map((entry) => entry.code)).toEqual([
        "auto_planned",
        "plan_energy",
        "plan_cost",
        "plan_distance",
      ]);
    }
  });

  it("refuses an answer without status, and an unknown key beside it", () => {
    const missing = rawDashboard();
    delete missing["status"];
    expect(decode(missing)).toEqual({ ok: false, failure: "malformed" });
  });

  it.each([
    ["an unknown code", { tone: "normal", lines: [line("brand_new_code", {})] }],
    ["an unknown tone", { tone: "loud", lines: [] }],
    ["a missing param", { tone: "normal", lines: [line("plan_energy", {})] }],
    ["an extra param", { tone: "normal", lines: [line("no_plan", { kwh: 1 })] }],
    ["a wrongly typed param", { tone: "normal", lines: [line("plan_energy", { kwh: "20" })] }],
    ["a fractional minor amount", { tone: "normal", lines: [line("plan_cost", { amount_minor: 22.5, currency: "SEK" })] }],
    ["a naive instant", { tone: "normal", lines: [line("auto_installed", { start: "2026-09-22T06:00:00" })] }],
    ["an unknown pause choice", { tone: "normal", lines: [line("paused", { until: null, choice: "someday" })] }],
    ["a blocking line under a normal tone", { tone: "normal", lines: [line("charger_unavailable", {})] }],
    ["a blocking tone with no blocking line", { tone: "blocking", lines: [line("no_plan", {})] }],
    ["an extra key on the block", { tone: "normal", lines: [], extra: 1 }],
  ])("refuses %s", (_name, status) => {
    expect(withStatus(status)).toEqual({ ok: false, failure: "malformed" });
  });

  it("accepts an empty block: idle", () => {
    expect((withStatus({ tone: "normal", lines: [] }) as { ok: boolean }).ok).toBe(true);
  });
});
