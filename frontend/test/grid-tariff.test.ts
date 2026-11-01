// The dashboard's `grid_tariff` block (the electricity price entry's grid fee as a percentage of spot, its fee in
// high-load hours and the window): the strict decoder, the rows as the price section shows them and the write's body.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { gridTariffRows, SUGGESTED_WINDOW, windowText, withHigh, withPercent, withWindow } from "../src/grid-tariff";
import { decodeDashboard, decodeGridTariff } from "../src/validate";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

const WINDOW = { weekdays_only: true, from_hour: 7, to_hour: 19, winter_only: true };

/** The block as `api/grid_tariff.py:serialize_grid_tariff` writes it for the owner's tariff. */
const BLOCK = {
  entry_id: "price_a",
  write_revision: 3,
  transfer_percent: { policy: "manual", explicit_value: 5, effective_value: 5, unit: "%" },
  transfer_high: { policy: "off", explicit_value: null, effective_value: null, unit: "öre/kWh" },
  high_load_window: WINDOW,
  writable: true,
};

const OFF = { policy: "off", explicit_value: null, effective_value: null };
/** Nothing stored: what a fresh electricity price carries. */
const NOTHING = {
  ...BLOCK,
  transfer_percent: { ...OFF, unit: "%" },
  transfer_high: { ...OFF, unit: "öre/kWh" },
  high_load_window: null,
};
const INCLUDED = {
  ...BLOCK,
  transfer_percent: { policy: "included", explicit_value: 5, effective_value: null, unit: "%" },
  transfer_high: { policy: "included", explicit_value: null, effective_value: null, unit: "öre/kWh" },
};

describe("the grid_tariff block", () => {
  it("is read when present, and the rest of the dashboard reads as without it", () => {
    for (const fixture of ["cheapest_direct_site_admin.json", "cheapest_no_site.json"]) {
      const plain = decodeDashboard(read("dashboard", fixture));
      const withBlock = decodeDashboard({ ...read("dashboard", fixture), grid_tariff: BLOCK });
      expect(plain.ok, fixture).toBe(true);
      expect(plain.ok && plain.value.grid_tariff, fixture).toBeNull();
      expect(withBlock.ok && withBlock.value.grid_tariff, fixture).toEqual(BLOCK);
      expect(withBlock.ok && { ...withBlock.value, grid_tariff: null }, fixture).toEqual(plain.ok && plain.value);
    }
    expect(decodeGridTariff(NOTHING)).toEqual(NOTHING);
    expect(decodeGridTariff(INCLUDED)).toEqual(INCLUDED);
    expect(decodeGridTariff({ ...BLOCK, transfer_high: { ...BLOCK.transfer_high, unit: null } }).transfer_high.unit).toBeNull();
  });

  it("is dropped, not the whole dashboard, when it is not exactly its shape", () => {
    for (const broken of [
      { ...BLOCK, demand: null },
      { ...BLOCK, writable: "yes" },
      { ...BLOCK, write_revision: -1 },
      { ...BLOCK, write_revision: 1.5 },
      { ...BLOCK, entry_id: "" },
      { ...BLOCK, transfer_percent: { ...BLOCK.transfer_percent, suggested_value: 5 } },
      { ...BLOCK, transfer_percent: { ...BLOCK.transfer_percent, policy: "suggested" } },
      // On, but nothing added; off, but something added.
      { ...BLOCK, transfer_percent: { ...BLOCK.transfer_percent, effective_value: null } },
      { ...BLOCK, transfer_high: { ...BLOCK.transfer_high, effective_value: 12 } },
      { ...BLOCK, high_load_window: { ...WINDOW, to_hour: 7 } },
      { ...BLOCK, high_load_window: { ...WINDOW, from_hour: 24 } },
      { ...BLOCK, high_load_window: { ...WINDOW, to_hour: 25 } },
      { ...BLOCK, high_load_window: { ...WINDOW, months: [11] } },
      { ...BLOCK, high_load_window: { from_hour: 7, to_hour: 19 } },
      "price_a",
    ]) {
      const decoded = decodeDashboard({ ...read("dashboard", "cheapest_no_site.json"), grid_tariff: broken });
      expect(decoded.ok).toBe(true);
      expect(decoded.ok && decoded.value.grid_tariff, JSON.stringify(broken)).toBeNull();
    }
  });
});

describe("the rows and the write's body", () => {
  const block = decodeGridTariff(BLOCK);

  it("shows the hours only while the high-load fee is on or hours are stored", () => {
    expect(gridTariffRows("sv", block).map((row) => [row.key, row.label, row.value])).toEqual([
      ["transfer_percent", "Nätavgift, % av spotpriset", "5 %"],
      ["transfer_high", "Nätavgift under höglasttid", "Av"],
      ["high_load_window", "Höglasttid", "Vardagar 07–19, nov–mar"],
    ]);
    expect(gridTariffRows("sv", decodeGridTariff(NOTHING)).map((row) => row.key)).toEqual(["transfer_percent", "transfer_high"]);
    expect(gridTariffRows("en", null)).toEqual([]);
    expect(windowText("sv", { weekdays_only: false, from_hour: 6, to_hour: 24, winter_only: false })).toBe(
      "Alla dagar 06–24, hela året",
    );
  });

  it("locks the rows where the price already includes the grid fee", () => {
    const rows = gridTariffRows("en", decodeGridTariff(INCLUDED));
    expect(rows.map((row) => [row.key, row.value, row.included])).toEqual([
      ["transfer_percent", "Included in the price", true],
      ["transfer_high", "Included in the price", true],
    ]);
  });

  it("replaces all three parts, keeping what is not changed as stored", () => {
    expect(withPercent(block, 7.5)).toEqual({
      transfer_percent: { enabled: true, value: 7.5 },
      transfer_high: { enabled: false, value: null },
      high_load_window: WINDOW,
    });
    // Off keeps the figure, so on again is one step.
    expect(withPercent(block, null).transfer_percent).toEqual({ enabled: false, value: 5 });
    expect(withHigh(block, 12)).toEqual({
      transfer_percent: { enabled: true, value: 5 },
      transfer_high: { enabled: true, value: 12 },
      high_load_window: WINDOW,
    });
    const evening = { ...WINDOW, from_hour: 16, to_hour: 20 };
    expect(withWindow(block, evening).high_load_window).toEqual(evening);
    // Never a window it was not handed.
    expect(withHigh(decodeGridTariff(NOTHING), 12).high_load_window).toBeNull();
    expect(SUGGESTED_WINDOW).toEqual(WINDOW);
  });
});
