// Relay contract v2 in the card: a Great Britain region (Octopus Agile) whose price already includes
// VAT, tax and grid fee, its attribution, its money and miles, and finding it from a postcode.

import { describe, expect, it, vi } from "vitest";

import { distanceText, money, type FormatContext } from "../src/format";
import { translate, LANGUAGES } from "../src/i18n";
import {
  countryLabel,
  decodeMarketOptions,
  groupAreasForPicker,
  marketAreaLabel,
  marketFormFor,
  marketReplacement,
} from "../src/market";
import { marketEditorBody, type MarketEditorHandlers } from "../src/market-editor";
import { decodeSettingsRecord, fiscalRows } from "../src/settings";
import type { MarketOptionsV1, SettingsRecord } from "../src/types";

const GB_C = {
  area_id: "GB-C",
  name: "GB C – London",
  countries: ["GB"],
  timezone: "Europe/London",
  currency: "GBP",
  major_unit: "£",
  minor_unit: "p",
  suggestions: { vat_percent: null, tax_minor: null, transfer_minor: null },
  market_timezone: "Europe/Paris",
  included: ["vat", "tax", "transfer"],
  source: { name: "Octopus Energy (Agile)", url: "https://octopus.energy/smart/agile/" },
};

const SE4 = {
  area_id: "SE4",
  name: "Malmö",
  countries: ["SE"],
  timezone: "Europe/Stockholm",
  currency: "SEK",
  major_unit: "kr",
  minor_unit: "öre",
  suggestions: { vat_percent: 25, tax_minor: 36, transfer_minor: 30 },
  market_timezone: "Europe/Stockholm",
  included: [],
  source: { name: "ENTSO-E Transparency Platform", url: "https://transparency.entsoe.eu/" },
};

function options(): MarketOptionsV1 {
  const decoded = decodeMarketOptions({
    api_version: 1,
    state: "ready",
    reason: null,
    areas: [GB_C, SE4],
    configured_area: "GB-C",
  });
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function record(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return decodeSettingsRecord({
    revision: 3,
    area_id: "GB-C",
    overrides: [
      {
        area_id: "GB-C",
        // A row stored before the area included anything: switched on, with no suggestion behind it.
        vat: { enabled: true, value: null },
        tax: { enabled: true, value: 12 },
        transfer: { enabled: false, value: null },
      },
    ],
    phases: 3,
    amps: 16,
    requested_kwh: 20,
    max_periods: 1,
    departure_enabled: false,
    departure_time: "07:00",
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: null },
    fiscal_included: ["vat", "tax", "transfer"],
    ...overrides,
  });
}

describe("the market options of relay contract v2", () => {
  it("reads what is included and where the prices come from", () => {
    const gb = options().areas[0];
    expect(gb?.included).toEqual(["vat", "tax", "transfer"]);
    expect(gb?.market_timezone).toBe("Europe/Paris");
    expect(gb?.source?.name).toBe("Octopus Energy (Agile)");
  });

  it("refuses an included name it does not know and a link that could run something", () => {
    for (const area of [
      { ...GB_C, included: ["vat", "standing_charge"] },
      { ...GB_C, included: ["vat", "vat"] },
      { ...GB_C, source: { name: "x", url: "javascript:alert(1)" } },
    ]) {
      const decoded = decodeMarketOptions({ api_version: 1, state: "ready", reason: null, areas: [area], configured_area: null });
      expect(decoded.ok).toBe(false);
    }
  });

  it("names a region by its own name and groups it under Great Britain", () => {
    expect(marketAreaLabel("en", "GB C – London", "GB-C")).toBe("GB C – London");
    // Unchanged for every other area: a name that does not open with its id gets the id beside it.
    expect(marketAreaLabel("en", "Malmö", "SE4")).toBe("Malmö · SE4");
    expect(marketAreaLabel("en", "Belgium", "BE")).toBe("Belgium · BE");
    const { groups } = groupAreasForPicker(options().areas, "GB");
    expect(groups[0]?.heading).toBe("GB");
    expect(countryLabel("en", "GB")).toBe("Great Britain");
    expect(countryLabel("sv", "GB")).toBe("Storbritannien");
    expect(countryLabel("fi", "GB")).toBe("Iso-Britannia");
    for (const language of LANGUAGES) {
      expect(countryLabel(language, "GB")).not.toMatch(/United Kingdom|Förenade/);
    }
  });
});

describe("included parts are locked", () => {
  const hooks = (overrides: Partial<MarketEditorHandlers> = {}): MarketEditorHandlers => ({
    onSave: vi.fn(),
    onReload: vi.fn(),
    onCancel: vi.fn(),
    onReapply: vi.fn(),
    onAreaChange: vi.fn(),
    ...overrides,
  });

  it("shows them checked, disabled and included, asks for no figure, and links the source", () => {
    const base = record();
    const built = marketEditorBody(
      document,
      "en",
      { readOnly: false, options: options(), values: marketFormFor(base, "GB-C"), conflict: null },
      hooks(),
      "card-a",
      null,
    );
    document.body.replaceChildren(built.body);
    for (const component of ["vat", "tax", "transfer"]) {
      const box = document.querySelector<HTMLInputElement>(`#card-a-${component}-enabled`);
      expect(box?.checked).toBe(true);
      expect(box?.disabled).toBe(true);
      expect(document.querySelector(`#card-a-${component}-value`)).toBeNull();
    }
    expect(document.body.textContent).toContain("Included in the price");
    expect(document.body.textContent).toContain(translate("en", "market.includedNote"));
    // The source sits right under the area choice, before the area's hint.
    const select = document.querySelector("select");
    expect(select?.nextElementSibling?.querySelector("a")?.textContent).toBe("Octopus Energy (Agile)");
    expect(select?.nextElementSibling?.nextElementSibling?.textContent).toBe(translate("en", "market.area.description"));
    const link = document.querySelector<HTMLAnchorElement>("a");
    expect(link?.textContent).toBe("Octopus Energy (Agile)");
    expect(link?.href).toBe("https://octopus.energy/smart/agile/");
    expect(link?.rel).toContain("noopener");
  });

  it("saves the area without stating or judging an included part", () => {
    const base = record({ area_id: "SE4" });
    const check = marketReplacement(base, marketFormFor(base, "GB-C"), options());
    expect(check.ok).toBe(true);
    if (check.ok) {
      expect(check.body.area_id).toBe("GB-C");
      // The stored row stands as it was: the backend adds nothing for an included part either way.
      expect(check.body.overrides).toEqual(base.overrides);
    }
  });

  it("says so in the settings overview", () => {
    const rows = fiscalRows("sv", {
      vat: { policy: "included", effective: null, unit: "%" },
      tax: { policy: "included", effective: null, unit: "p/kWh" },
      transfer: { policy: "included", effective: null, unit: "p/kWh" },
    });
    expect(rows.map((row) => row.value)).toEqual(["Ingår i priset", "Ingår i priset", "Ingår i priset"]);
  });

  it("reads the record's read-only fiscal_included and refuses one it cannot read", () => {
    expect(record().area_id).toBe("GB-C");
    expect(() => record({ fiscal_included: ["standing_charge"] } as unknown as Partial<SettingsRecord>)).toThrow();
  });
});

describe("finding the region from a postcode", () => {
  it("is offered for Great Britain, asks Home Assistant, and selects the region it answers", async () => {
    const onAreaChange = vi.fn();
    const onFindRegion = vi.fn(async () => ({ region: "GB-C", reason: null }));
    const base = record({ area_id: null, overrides: [] });
    const built = marketEditorBody(
      document,
      "en",
      { readOnly: false, options: options(), values: marketFormFor(base, null), conflict: null },
      { onSave: vi.fn(), onReload: vi.fn(), onCancel: vi.fn(), onReapply: vi.fn(), onAreaChange, onFindRegion },
      "card-a",
      "GB",
    );
    document.body.replaceChildren(built.body);
    const input = document.querySelector<HTMLInputElement>("#card-a-postcode");
    expect(input).not.toBeNull();
    input!.value = "SW1A 1AA";
    const button = input!.parentElement!.querySelector<HTMLButtonElement>("button");
    button!.click();
    await vi.waitFor(() => expect(onAreaChange).toHaveBeenCalled());
    expect(onFindRegion).toHaveBeenCalledWith("SW1A 1AA");
    expect(onAreaChange.mock.calls[0]?.[0]).toBe("GB-C");
  });

  it("explains a miss and is not offered outside Great Britain", async () => {
    const onFindRegion = vi.fn(async () => ({ region: null, reason: "not_found" }));
    const base = record({ area_id: "GB-C" });
    const built = marketEditorBody(
      document,
      "sv",
      { readOnly: false, options: options(), values: marketFormFor(base, "GB-C"), conflict: null },
      { onSave: vi.fn(), onReload: vi.fn(), onCancel: vi.fn(), onReapply: vi.fn(), onAreaChange: vi.fn(), onFindRegion },
      "card-a",
      null,
    );
    document.body.replaceChildren(built.body);
    const input = document.querySelector<HTMLInputElement>("#card-a-postcode");
    input!.value = "ZZ9 9ZZ";
    input!.parentElement!.querySelector<HTMLButtonElement>("button")!.click();
    await vi.waitFor(() => expect(document.body.textContent).toContain("Ingen region hittades"));

    const sweden = record({ area_id: "SE4" });
    const elsewhere = marketEditorBody(
      document,
      "sv",
      { readOnly: false, options: options(), values: marketFormFor(sweden, "SE4"), conflict: null },
      { onSave: vi.fn(), onReload: vi.fn(), onCancel: vi.fn(), onReapply: vi.fn(), onAreaChange: vi.fn(), onFindRegion },
      "card-b",
      "SE",
    );
    expect(elsewhere.body.querySelector("#card-b-postcode")).toBeNull();
  });
});

describe("pounds and miles", () => {
  const context = (language: "en" | "sv", currency: string, majorUnit: string): FormatContext => ({
    language,
    timeZone: "Europe/London",
    unit: "p",
    currency,
    majorUnit,
    countries: ["GB"],
  });

  it("writes pounds as the language does and every other currency as before", () => {
    expect(money(context("en", "GBP", "£"), 1.33)).toBe("£1.33");
    expect(money(context("sv", "GBP", "£"), 1.33)).toBe("1,33 £");
    expect(money({ ...context("sv", "SEK", "kr"), countries: ["SE"] }, 12.5)).toBe("12,5 kr");
  });

  it("reads a Great Britain range in miles whatever the language", () => {
    // 16.09344 km is ten miles; one mil is ten kilometres.
    expect(distanceText("sv", 1.609344, ["GB"])).toBe("10 mi");
    expect(distanceText("en", 1.609344, ["GB"])).toBe("10 mi");
    expect(distanceText("sv", 1.5, ["SE"])).toBe("1,5 mil");
    expect(distanceText("en", 1.5)).toBe("15 km");
  });
});
