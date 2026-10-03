// The market read on the client: one strict decoder, and every refusal it must make.
//
// The payloads here are hand-built *and* deliberately shaped exactly like the backend's encoder
// (`market_api.py`): five keys for the answer, eight for an area, three for a suggestion row. The
// decoder makes no request, so everything here is a value in and a value out -- including the
// refusals, which are *values* (`unsupported`, `malformed`) rather than exceptions.

import { describe, expect, it } from "vitest";

import {
  MARKET_EDITABLE_STATES,
  countryLabel,
  groupAreasForPicker,
  decodeMarketOptions,
  figureEdited,
  fiscalFormFrom,
  fiscalUnit,
  marketAreaIsKnown,
  marketAreaLabel,
  marketEditable,
  marketFormFor,
  marketReplacement,
  marketStateKey,
  marketSuggestion,
  resetToSuggestion,
  setEnabled,
  switchArea,
  type FiscalFormValue,
  type MarketDrafts,
  type MarketFormValues,
} from "../src/market";
import { encodeBody } from "../src/settings";
import {
  MARKET_API_VERSION,
  MARKET_REASONS,
  MARKET_STATES,
  type AreaOverride,
  type FiscalOverride,
  type MarketOptionsV1,
  type SettingsRecord,
} from "../src/types";

type Json = Record<string, unknown>;

/** One area, exactly as `market_api.py` writes it: the parsed `AreaEntry`, minus its EIC. */
function area(overrides: Json = {}): Json {
  return {
    area_id: "SE4",
    name: "Malmö · SE4",
    countries: ["SE"],
    timezone: "Europe/Stockholm",
    currency: "SEK",
    major_unit: "kr",
    minor_unit: "öre",
    suggestions: { vat_percent: 25.0, tax_minor: 36.0, transfer_minor: 30.0 },
    ...overrides,
  };
}

/** One answer, exactly as the backend writes it. */
function payload(overrides: Json = {}): Json {
  return {
    api_version: MARKET_API_VERSION,
    state: "ready",
    reason: null,
    areas: [area()],
    configured_area: "SE4",
    ...overrides,
  };
}

function decoded(raw: unknown): MarketOptionsV1 {
  const result = decodeMarketOptions(raw);
  if (!result.ok) {
    throw new Error(`expected a decoded answer, got ${result.failure}`);
  }
  return result.value;
}

describe("the market options decoder", () => {
  it("accepts the backend's own shape and returns exactly its facts", () => {
    const value = decoded(payload());
    expect(Object.keys(value).sort()).toEqual(
      ["areas", "configured_area", "reason", "state"].sort(),
    );
    expect(value.state).toBe("ready");
    expect(value.reason).toBeNull();
    expect(value.configured_area).toBe("SE4");
    expect(value.areas).toEqual([
      {
        area_id: "SE4",
        name: "Malmö · SE4",
        countries: ["SE"],
        timezone: "Europe/Stockholm",
        currency: "SEK",
        major_unit: "kr",
        minor_unit: "öre",
        suggestions: { vat_percent: 25, tax_minor: 36, transfer_minor: 30 },
      },
    ]);
  });

  it("keeps an absent suggestion apart from a published zero", () => {
    const value = decoded(
      payload({
        areas: [
          area({
            area_id: "DK1",
            suggestions: { vat_percent: 25, tax_minor: 0, transfer_minor: null },
          }),
          area({ area_id: "DE-LU", suggestions: { vat_percent: null, tax_minor: null, transfer_minor: null } }),
        ],
      }),
    );
    expect(value.areas.map((entry) => entry.suggestions)).toEqual([
      { vat_percent: 25, tax_minor: 0, transfer_minor: null },
      { vat_percent: null, tax_minor: null, transfer_minor: null },
    ]);
  });

  it("keeps the relay's own area order and its minor-unit labels", () => {
    const value = decoded(
      payload({
        areas: [
          area({ area_id: "NO1", currency: "NOK", minor_unit: "øre" }),
          area({ area_id: "SE4", currency: "SEK", minor_unit: "öre" }),
          area({ area_id: "DE-LU", currency: "EUR", major_unit: "€", minor_unit: "cent" }),
        ],
      }),
    );
    expect(value.areas.map((entry) => entry.area_id)).toEqual(["NO1", "SE4", "DE-LU"]);
    expect(value.areas.map((entry) => entry.minor_unit)).toEqual(["øre", "öre", "cent"]);
    expect(value.areas.map((entry) => entry.currency)).toEqual(["NOK", "SEK", "EUR"]);
  });

  it("accepts a configured area the catalogue no longer lists", () => {
    const value = decoded(payload({ state: "stale", reason: "offline", areas: [], configured_area: "SE4" }));
    expect(value.state).toBe("stale");
    expect(value.reason).toBe("offline");
    expect(value.areas).toEqual([]);
    expect(value.configured_area).toBe("SE4");
  });

  it("accepts every state and every reason the backend can answer with", () => {
    for (const state of MARKET_STATES) {
      const value = decoded(
        payload({
          state,
          reason: state === "stale" || state === "unavailable" ? "offline" : state === "invalid" ? "invalid" : null,
        }),
      );
      expect(value.state).toBe(state);
    }
    expect(MARKET_REASONS).toEqual(["offline", "invalid"]);
  });

  it("refuses a version it does not speak, distinctly from a malformed payload", () => {
    expect(decodeMarketOptions(payload({ api_version: 2 }))).toEqual({
      ok: false,
      failure: "unsupported",
    });
    expect(decodeMarketOptions(payload({ api_version: 0 }))).toEqual({
      ok: false,
      failure: "unsupported",
    });
    // Any number that is not this version is a version, and therefore "unsupported" -- the same
    // judgement the settings boundary makes, where a non-finite number is a version too.
    for (const version of [1.5, Number.NaN, -1, 99]) {
      expect(decodeMarketOptions(payload({ api_version: version }))).toEqual({
        ok: false,
        failure: "unsupported",
      });
    }
    for (const version of ["1", true, null, undefined]) {
      expect(decodeMarketOptions(payload({ api_version: version }))).toEqual({
        ok: false,
        failure: "malformed",
      });
    }
  });

  it("refuses a payload that is not this contract's shape at all", () => {
    const cases: unknown[] = [
      null,
      undefined,
      42,
      "ready",
      [],
      {}, // no api_version at all
      { ...payload(), extra: true }, // an unknown key beside the five
      { ...payload(), areas: undefined },
      payload({ state: "unknown" }),
      payload({ state: "ready", reason: "offline" }), // a ready catalogue has no failure to report
      payload({ state: "invalid", reason: null }),
      payload({ state: "invalid", reason: "offline" }),
      payload({ reason: "timeout" }), // an internal code is not a public reason
      payload({ reason: "" }),
      payload({ areas: {} }),
      payload({ configured_area: "" }),
      payload({ configured_area: 7 }),
    ];
    for (const raw of cases) {
      expect(decodeMarketOptions(raw), JSON.stringify(raw)).toEqual({
        ok: false,
        failure: "malformed",
      });
    }
  });

  it("refuses an area whose identities or suggestions are not facts", () => {
    const cases: Json[] = [
      area({ area_id: "" }),
      area({ area_id: 7 }),
      area({ name: "" }),
      area({ name: null }),
      area({ timezone: "" }),
      area({ currency: "" }),
      area({ major_unit: "" }),
      area({ minor_unit: "" }),
      area({ countries: "SE" }),
      area({ countries: [""] }),
      area({ countries: [7] }),
      area({ suggestions: { vat_percent: "25", tax_minor: 0, transfer_minor: 0 } }),
      area({ suggestions: { vat_percent: 25, tax_minor: Number.NaN, transfer_minor: 0 } }),
      area({ suggestions: { vat_percent: 25, tax_minor: 0 } }),
      area({ suggestions: { vat_percent: 25, tax_minor: 0, transfer_minor: 0, extra: 1 } }),
      area({ extra: "eic" }),
    ];
    for (const bad of cases) {
      expect(decodeMarketOptions(payload({ areas: [bad] })), JSON.stringify(bad)).toEqual({
        ok: false,
        failure: "malformed",
      });
    }
  });

  it("refuses two rows claiming one area id", () => {
    expect(
      decodeMarketOptions(payload({ areas: [area(), area({ name: "Twice" })] })),
    ).toEqual({ ok: false, failure: "malformed" });
  });

  it("carries no text out of a refusal", () => {
    const refusal = decodeMarketOptions(payload({ areas: [area({ name: "<img src=x onerror=alert(1)>", suggestions: { vat_percent: "nope", tax_minor: 0, transfer_minor: 0 } })] }));
    expect(refusal.ok).toBe(false);
    expect(JSON.stringify(refusal)).toBe('{"ok":false,"failure":"malformed"}');
  });
});

// ------------------------------------------------------------- the area and fiscal model

/** One fiscal coercion, as the contract states it. */
function fiscal(enabled: boolean, value: number | null): FiscalOverride {
  return { enabled, value };
}

/** One stored override row. */
function row(areaId: string, vat: FiscalOverride, tax: FiscalOverride, transfer: FiscalOverride): AreaOverride {
  return { area_id: areaId, vat, tax, transfer };
}

/**
 * One accepted settings record, with the planning fields a market edit must carry through untouched.
 *
 * The figures are deliberately not round numbers: a decimal target percent, a fractional consumption, a
 * half kilowatt-hour and a current the phase count makes awkward are exactly what a full replacement
 * must preserve value-for-value.
 */
function record(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 3,
    amps: 16,
    requested_kwh: 20.5,
    max_periods: 3,
    departure_enabled: true,
    departure_time: "06:30",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: 80.5 },
    ...overrides,
  };
}

/** A catalogue holding SE4 (all three suggestions) and DE-LU (none at all). */
function options(): MarketOptionsV1 {
  return {
    state: "ready",
    reason: null,
    configured_area: "SE4",
    areas: [
      {
        area_id: "SE4",
        name: "Malmö",
        countries: ["SE"],
        timezone: "Europe/Stockholm",
        currency: "SEK",
        major_unit: "kr",
        minor_unit: "öre",
        suggestions: { vat_percent: 25, tax_minor: 36.5, transfer_minor: 0 },
      },
      {
        area_id: "DE-LU",
        name: "Germany-Luxembourg",
        countries: ["DE", "LU"],
        timezone: "Europe/Berlin",
        currency: "EUR",
        major_unit: "€",
        minor_unit: "cent",
        suggestions: { vat_percent: null, tax_minor: null, transfer_minor: null },
      },
    ],
  };
}

describe("the fiscal row model: enabled, figure, and what the figure means", () => {
  it("reads an accepted row's four states into checkbox, figure and intent", () => {
    // No row at all, and a stored row that states nothing: unchecked, and still the suggestion.
    expect(fiscalFormFrom(null, "vat")).toEqual({ enabled: false, value: "", intent: "suggested" });
    expect(fiscalFormFrom(row("SE4", fiscal(false, null), fiscal(false, null), fiscal(false, null)), "tax")).toEqual({
      enabled: false,
      value: "",
      intent: "suggested",
    });
    // `enabled=true, value=null`: checked, the suggestion is what it means.
    expect(fiscalFormFrom(row("SE4", fiscal(true, null), fiscal(true, 0), fiscal(true, 7.13)), "vat")).toEqual({
      enabled: true,
      value: "",
      intent: "suggested",
    });
    // `enabled=true, value=N`: the person's own figure, exactly as stored.
    expect(fiscalFormFrom(row("SE4", fiscal(true, null), fiscal(true, 0), fiscal(true, 7.13)), "tax")).toEqual({
      enabled: true,
      value: "0",
      intent: "custom",
    });
    // `enabled=false, value=N`: unchecked, the figure retained, and it is still the person's own.
    expect(fiscalFormFrom(row("SE4", fiscal(false, 30), fiscal(false, null), fiscal(true, null)), "vat")).toEqual({
      enabled: false,
      value: "30",
      intent: "custom",
    });
    expect(fiscalFormFrom(row("SE4", fiscal(true, null), fiscal(true, 0), fiscal(true, 7.13)), "transfer")).toEqual({
      enabled: true,
      value: "7.13",
      intent: "custom",
    });
  });

  it("keeps the intent apart from the digits: editing is what makes a figure the person's own", () => {
    const suggested: FiscalFormValue = { enabled: true, value: "25", intent: "suggested" };
    // The same digits, and a different meaning: this is the case numeric equality cannot express.
    expect(figureEdited(suggested, "25")).toEqual({ enabled: true, value: "25", intent: "custom" });
    expect(figureEdited(suggested, "12.5")).toEqual({ enabled: true, value: "12.5", intent: "custom" });
    // Unchecking keeps both the figure and the intent; rechecking is not a way to lose either.
    const off = setEnabled(suggested, false);
    expect(off).toEqual({ enabled: false, value: "25", intent: "suggested" });
    expect(setEnabled(off, true)).toEqual(suggested);
    const custom = figureEdited(suggested, "9.5");
    expect(setEnabled(setEnabled(custom, false), true)).toEqual(custom);
    // And only the reset means the suggestion again -- with the figure back to the catalogue's own.
    expect(resetToSuggestion(custom)).toEqual({ enabled: true, value: "", intent: "suggested" });
  });

  it("begins an area from its own stored row, or from off with absent figures", () => {
    const stored = record({
      overrides: [row("SE4", fiscal(true, 25), fiscal(false, 36), fiscal(true, null))],
    });
    expect(marketFormFor(stored, "SE4")).toEqual({
      areaId: "SE4",
      vat: { enabled: true, value: "25", intent: "custom" },
      tax: { enabled: false, value: "36", intent: "custom" },
      transfer: { enabled: true, value: "", intent: "suggested" },
    });
    // An area with no stored row: three times off, three absent figures, three suggestions.
    expect(marketFormFor(stored, "DE-LU")).toEqual({
      areaId: "DE-LU",
      vat: { enabled: false, value: "", intent: "suggested" },
      tax: { enabled: false, value: "", intent: "suggested" },
      transfer: { enabled: false, value: "", intent: "suggested" },
    });
    expect(marketFormFor(stored, null).areaId).toBeNull();
  });

  it("restores a draft made in the same open form -- figure and intent both -- and leaves others alone", () => {
    const stored = record({
      overrides: [row("SE4", fiscal(true, 25), fiscal(true, 36), fiscal(true, 30))],
    });
    const se4: MarketFormValues = {
      areaId: "SE4",
      vat: { enabled: true, value: "25", intent: "custom" },
      tax: { enabled: false, value: "36", intent: "custom" },
      transfer: { enabled: false, value: "", intent: "suggested" },
    };
    const left = switchArea(stored, {}, se4, "DE-LU");
    // Nothing crosses an area change: DE-LU starts all-off, with no figures at all.
    expect(left.values).toEqual({
      areaId: "DE-LU",
      vat: { enabled: false, value: "", intent: "suggested" },
      tax: { enabled: false, value: "", intent: "suggested" },
      transfer: { enabled: false, value: "", intent: "suggested" },
    });
    const back = switchArea(stored, left.drafts, left.values, "SE4");
    expect(back.values).toEqual(se4);
    expect(Object.keys(back.drafts).sort()).toEqual(["DE-LU", "SE4"]);
  });

  it("names suggestions and units from the selected area only", () => {
    const catalogue = options();
    expect(marketSuggestion(catalogue, "SE4", "vat")).toBe(25);
    expect(marketSuggestion(catalogue, "SE4", "tax")).toBe(36.5);
    expect(marketSuggestion(catalogue, "SE4", "transfer")).toBe(0);
    expect(marketSuggestion(catalogue, "DE-LU", "vat")).toBeNull();
    expect(marketSuggestion(catalogue, "XX", "vat")).toBeNull();
    expect(marketSuggestion(catalogue, null, "vat")).toBeNull();
    expect(fiscalUnit(catalogue, "SE4", "vat")).toBe("%");
    expect(fiscalUnit(catalogue, "SE4", "tax")).toBe("öre");
    expect(fiscalUnit(catalogue, "DE-LU", "transfer")).toBe("cent");
    expect(fiscalUnit(catalogue, "XX", "tax")).toBe("");
  });
});

describe("one market save as one full replacement", () => {
  const all = (vat: FiscalFormValue, tax: FiscalFormValue, transfer: FiscalFormValue): MarketFormValues => ({
    areaId: "SE4",
    vat,
    tax,
    transfer,
  });
  const offSuggested: FiscalFormValue = { enabled: false, value: "", intent: "suggested" };

  it("states the area and that area's row, and preserves everything else value-for-value", () => {
    const base = record({
      overrides: [
        row("NO1", fiscal(true, 25), fiscal(true, 7.13), fiscal(false, null)),
        row("SE4", fiscal(false, null), fiscal(true, 36), fiscal(true, null)),
        row("DK1", fiscal(true, 25), fiscal(false, 0), fiscal(true, 30)),
      ],
    });
    const check = marketReplacement(
      base,
      all(
        { enabled: true, value: "12.5", intent: "custom" },
        { enabled: true, value: "36", intent: "suggested" },
        { enabled: false, value: "30", intent: "custom" },
      ),
      options(),
    );
    if (!check.ok) {
      throw new Error(`expected a replacement, got ${check.errorKey}`);
    }
    expect(check.changed).toBe(true);
    const body = check.body;
    expect(body).toEqual({
      ...encodeBody(base),
      area_id: "SE4",
      overrides: [
        row("NO1", fiscal(true, 25), fiscal(true, 7.13), fiscal(false, null)),
        row("SE4", fiscal(true, 12.5), fiscal(true, null), fiscal(false, 30)),
        row("DK1", fiscal(true, 25), fiscal(false, 0), fiscal(true, 30)),
      ],
    });
    expect(body.overrides.map((item) => item.area_id)).toEqual(["NO1", "SE4", "DK1"]);
    expect(body.overrides[0]).toEqual(base.overrides[0]);
    expect(body.overrides[2]).toEqual(base.overrides[2]);
    expect(body.target.target_percent).toBe(80.5);
    expect(body.phases).toBe(3);
  });

  it("never stores a displayed suggestion as if the person had chosen it", () => {
    // The field *shows* the catalogue's figure for a suggested component; what it states is `null`.
    const check = marketReplacement(
      record(),
      all({ enabled: true, value: "25", intent: "suggested" }, offSuggested, offSuggested),
      options(),
    );
    if (!check.ok) {
      throw new Error("expected a replacement");
    }
    expect(check.body.overrides[0]?.vat).toEqual({ enabled: true, value: null });

    // And the same digits with the person's own intent are stored as their number, not as a suggestion:
    // they typed it deliberately, so a later catalogue change must not move it.
    const typed = marketReplacement(
      record(),
      all({ enabled: true, value: "25", intent: "custom" }, offSuggested, offSuggested),
      options(),
    );
    if (!typed.ok) {
      throw new Error("expected a replacement");
    }
    expect(typed.body.overrides[0]?.vat).toEqual({ enabled: true, value: 25 });
  });

  it("appends a row for an area that had none, and states nothing when there is nothing to state", () => {
    const base = record({ area_id: null, overrides: [row("NO1", fiscal(true, 25), fiscal(true, 7), fiscal(true, 30))] });
    const stated = marketReplacement(base, all({ enabled: true, value: "25", intent: "custom" }, offSuggested, offSuggested), options());
    if (!stated.ok) {
      throw new Error("expected a replacement");
    }
    expect(stated.changed).toBe(true);
    expect(stated.body.area_id).toBe("SE4");
    expect(stated.body.overrides.map((item) => item.area_id)).toEqual(["NO1", "SE4"]);
    expect(stated.body.overrides[1]).toEqual(row("SE4", fiscal(true, 25), fiscal(false, null), fiscal(false, null)));

    // Three components off, all following the catalogue, for an area with no stored row: not a row.
    const silent = marketReplacement(
      record({ area_id: "SE4" }),
      { areaId: "DE-LU", vat: offSuggested, tax: offSuggested, transfer: offSuggested },
      options(),
    );
    if (!silent.ok) {
      throw new Error("expected a replacement");
    }
    expect(silent.changed).toBe(true);
    expect(silent.body.overrides).toEqual([]);
  });

  it("reports an unchanged statement as no change at all, with the body equal to the record", () => {
    const base = record({
      overrides: [row("SE4", fiscal(true, 25), fiscal(false, 36), fiscal(true, null))],
    });
    const check = marketReplacement(
      base,
      all(
        { enabled: true, value: "25", intent: "custom" },
        { enabled: false, value: "36", intent: "custom" },
        { enabled: true, value: "", intent: "suggested" },
      ),
      options(),
    );
    if (!check.ok) {
      throw new Error("expected a replacement");
    }
    expect(check.changed).toBe(false);
    expect(check.body).toEqual(encodeBody(base));
  });

  it("treats an explicit zero as a value, and a blank or impossible one as an error only when on", () => {
    const zero = marketReplacement(
      record(),
      all(
        { enabled: true, value: "0", intent: "custom" },
        { enabled: true, value: "0,5", intent: "custom" },
        offSuggested,
      ),
      options(),
    );
    if (!zero.ok) {
      throw new Error("expected a replacement");
    }
    expect(zero.body.overrides[0]).toEqual(row("SE4", fiscal(true, 0), fiscal(true, 0.5), fiscal(false, null)));

    const cases: Array<[FiscalFormValue, string]> = [
      [{ enabled: true, value: "", intent: "custom" }, "settings.error.required"],
      [{ enabled: true, value: "two", intent: "custom" }, "settings.error.invalidNumber"],
      [{ enabled: true, value: "-1", intent: "custom" }, "settings.error.outOfRange"],
    ];
    for (const [vat, errorKey] of cases) {
      expect(marketReplacement(record(), all(vat, offSuggested, offSuggested), options())).toEqual({ ok: false, errorKey });
    }

    // Unchecked, the same unreadable text is absence rather than a refusal: nothing is judged where
    // nothing is required. All three components then state nothing, so no row is added at all -- and
    // the Save is the zero-request one rather than an error about a field that is switched off.
    const off = marketReplacement(
      record(),
      all({ enabled: false, value: "two", intent: "custom" }, offSuggested, offSuggested),
      options(),
    );
    if (!off.ok) {
      throw new Error(`expected a replacement, got ${off.errorKey}`);
    }
    expect(off.changed).toBe(false);
    expect(off.body.overrides).toEqual([]);
  });

  it("requires the person's own number where the area publishes no suggestion", () => {
    const offSuggestedAtDlu: MarketFormValues = {
      areaId: "DE-LU",
      vat: { enabled: true, value: "", intent: "suggested" },
      tax: offSuggested,
      transfer: offSuggested,
    };
    expect(marketReplacement(record(), offSuggestedAtDlu, options())).toEqual({
      ok: false,
      errorKey: "settings.error.required",
    });
    // With a figure beside it, the same checked state is a custom value: "the suggestion" cannot be
    // stored where the catalogue published none.
    const typed = marketReplacement(
      record(),
      { ...offSuggestedAtDlu, vat: { enabled: true, value: "5", intent: "suggested" } },
      options(),
    );
    if (!typed.ok) {
      throw new Error("expected a replacement");
    }
    expect(typed.body.overrides[0]?.vat).toEqual({ enabled: true, value: 5 });
  });

  it("refuses an area that is not a choice, and accepts the charger's own stored one", () => {
    const missingArea: MarketFormValues = { areaId: null, vat: offSuggested, tax: offSuggested, transfer: offSuggested };
    expect(marketReplacement(record(), missingArea, options())).toEqual({
      ok: false,
      errorKey: "market.error.areaRequired",
    });
    expect(marketReplacement(record(), { ...missingArea, areaId: "XX" }, options())).toEqual({
      ok: false,
      errorKey: "market.error.areaUnknown",
    });

    const base = record({
      area_id: "SE4",
      overrides: [row("SE4", fiscal(true, 25), fiscal(true, 36), fiscal(true, 30))],
    });
    const catalogue = options();
    catalogue.areas = catalogue.areas.filter((area) => area.area_id !== "SE4");
    catalogue.configured_area = "SE4";
    const check = marketReplacement(
      base,
      all(
        { enabled: false, value: "25", intent: "custom" },
        { enabled: false, value: "36", intent: "custom" },
        { enabled: false, value: "30", intent: "custom" },
      ),
      catalogue,
    );
    if (!check.ok) {
      throw new Error(`expected a replacement, got ${check.errorKey}`);
    }
    expect(check.changed).toBe(true);
    expect(check.body.overrides[0]).toEqual(row("SE4", fiscal(false, 25), fiscal(false, 36), fiscal(false, 30)));
    expect(marketAreaIsKnown(catalogue, base, "SE4")).toBe(true);
    expect(marketAreaIsKnown(catalogue, base, "NO1")).toBe(false);
  });
});

describe("the market resting summary and catalogue states", () => {
  it("shows the area's name with its id, and one honest fallback", () => {
    expect(marketAreaLabel("en", "Malmö", "SE4")).toBe("Malmö · SE4");
    // The id only when the name does not already carry it.
    expect(marketAreaLabel("en", "Malmö (SE4)", "SE4")).toBe("Malmö (SE4)");
    expect(marketAreaLabel("en", null, "SE4")).toBe("SE4");
    expect(marketAreaLabel("en", "Malmö", null)).toBe("Malmö");
    expect(marketAreaLabel("en", "", "")).toBe("Not set");
    expect(marketAreaLabel("en", null, null)).toBe("Not set");
    // A hostile name is text and nothing else: no markup, and no flag as the identity.
    const hostile = '<img src=x onerror=alert(1)>';
    expect(marketAreaLabel("en", hostile, "SE4")).toBe(`${hostile} · SE4`);
    const drafts: MarketDrafts = {};
    expect(Object.keys(drafts)).toEqual([]);
  });

  it("explains a catalogue that is not ready without calling a held one fresh", () => {
    expect(marketStateKey("ready", null)).toBeNull();
    expect(marketStateKey("stale", "offline")).toBe("market.state.staleOffline");
    expect(marketStateKey("stale", "invalid")).toBe("market.state.staleInvalid");
    expect(marketStateKey("unavailable", "offline")).toBe("market.state.offline");
    expect(marketStateKey("unavailable", null)).toBe("market.state.offline");
    expect(marketStateKey("invalid", "invalid")).toBe("market.state.invalid");
    expect(marketStateKey("loading", null)).toBe("market.state.loading");
  });
});

// ------------------------------------------------------------------ the remaining §6 guarantees

describe("a market replacement preserves the record exactly", () => {
  const values: MarketFormValues = {
    areaId: "SE4",
    vat: { enabled: true, value: "25", intent: "custom" },
    tax: { enabled: false, value: "", intent: "suggested" },
    transfer: { enabled: false, value: "", intent: "suggested" },
  };

  it("carries phases 1, 3 and null and an absent target through untouched", () => {
    for (const phases of [1, 3, null] as const) {
      const base = record({ phases, target: { vehicle_id: null, target_percent: 80.5 } });
      const check = marketReplacement(base, values, options());
      if (!check.ok) {
        throw new Error(`expected a replacement, got ${check.errorKey}`);
      }
      expect(check.body.phases).toBe(phases);
      expect(check.body.target.target_percent).toBe(80.5);
      const bare = record({ phases, target: { vehicle_id: "v1", target_percent: null } });
      const again = marketReplacement(bare, values, options());
      if (!again.ok) {
        throw new Error(`expected a replacement, got ${again.errorKey}`);
      }
      expect(again.body.phases).toBe(phases);
      expect(again.body.target).toEqual({ vehicle_id: "v1", target_percent: null });
    }
  });

  it("audits the decoded answer for facts this card must never carry", () => {
    const decodedValue = decoded(payload());
    const serialized = JSON.stringify(decodedValue).toLowerCase();
    for (const forbidden of ["eic", "http://", "https://", "webhook", "entity_id", "device_id", "owner"]) {
      expect(serialized).not.toContain(forbidden);
    }
    // An EIC smuggled into an area is not "an extra field nobody reads": it is a document this card
    // does not understand, and it is refused whole.
    expect(decodeMarketOptions(payload({ areas: [area({ eic: "10Y1001A1001A82H" })] }))).toEqual({
      ok: false,
      failure: "malformed",
    });
    expect(decodeMarketOptions(payload({ areas: [area({ name: "Relay said: HTTP 500 at https://relay.test/v1/areas.json" })] })).ok).toBe(
      true,
    );
  });
});


describe("the editability rule, stated once", () => {
  it("is editable exactly while an administrator has a usable catalogue", () => {
    // A held catalogue whose last attempt did not fail, and one whose last attempt did: both are
    // usable, and the second stays usable while visibly stale.
    for (const state of ["ready", "stale"] as const) {
      expect(marketEditable(state, true)).toBe(true);
      expect(marketEditable(state, false)).toBe(false);
    }
    // Nothing held: nobody can choose an area they cannot see, whatever their authority.
    for (const state of ["loading", "unavailable", "invalid"] as const) {
      expect(marketEditable(state, true)).toBe(false);
      expect(marketEditable(state, false)).toBe(false);
    }
    expect(MARKET_EDITABLE_STATES).toEqual(["ready", "stale"]);
  });
});

describe("the area picker's grouping, as the Android app builds it", () => {
  const area = (id: string, countries: string[]) => ({ id, countries });
  const catalogue = [
    area("SE3", ["SE"]),
    area("NO1", ["NO"]),
    area("DE-LU", ["DE", "LU"]),
    area("SE4", ["SE"]),
    area("NOWHERE", []),
  ];
  const headings = (region: string | null) =>
    groupAreasForPicker(catalogue, region).groups.map((group) => [group.heading, group.areas.map((entry) => entry.id)]);

  it("lists the areas covering the region first, then the rest, each in catalogue order", () => {
    expect(headings("SE")).toEqual([
      ["SE", ["SE3", "SE4"]],
      ["NO", ["NO1"]],
      ["DE", ["DE-LU"]],
    ]);
    expect(headings("se")[0]).toEqual(["SE", ["SE3", "SE4"]]);
  });

  it("puts a multi-country area under the region it covers, else under its first country", () => {
    expect(headings("LU")).toEqual([
      ["LU", ["DE-LU"]],
      ["SE", ["SE3", "SE4"]],
      ["NO", ["NO1"]],
    ]);
    expect(headings("DE")[0]).toEqual(["DE", ["DE-LU"]]);
  });

  it("moves nothing forward without a region, or with one nothing covers", () => {
    const expected = [
      ["SE", ["SE3", "SE4"]],
      ["NO", ["NO1"]],
      ["DE", ["DE-LU"]],
    ];
    expect(headings(null)).toEqual(expected);
    expect(headings("FI")).toEqual(expected);
    expect(headings("")).toEqual(expected);
  });

  it("returns an area with no countries apart, for the caller to list after the groups", () => {
    expect(groupAreasForPicker(catalogue, "SE").ungrouped.map((entry) => entry.id)).toEqual(["NOWHERE"]);
  });

  it("names a country in the card language and falls back to the upper-case code", () => {
    expect(countryLabel("sv", "SE")).toBe("Sverige");
    expect(countryLabel("en", "se")).toBe("Sweden");
    expect(countryLabel("en", "qq")).toBe("QQ");
    expect(countryLabel("en", "not a code")).toBe("NOT A CODE");
  });
});
