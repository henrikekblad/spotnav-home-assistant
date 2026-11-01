// The dashboard's `grid_tariff` block (the electricity price entry's grid fee as a percentage of spot, its fee in
// high-load hours and the window): accepted beside `price_source`, never the reason a dashboard is refused.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { decodeDashboard } from "../src/validate";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

/** The block as `api/grid_tariff.py:serialize_grid_tariff` writes it for the owner's tariff. */
const BLOCK = {
  entry_id: "price_a",
  write_revision: 3,
  transfer_percent: { policy: "manual", explicit_value: 5, effective_value: 5, unit: "%" },
  transfer_high: { policy: "off", explicit_value: null, effective_value: null, unit: "öre/kWh" },
  high_load_window: { weekdays_only: true, from_hour: 7, to_hour: 19, winter_only: true },
  writable: true,
};

describe("the grid_tariff block", () => {
  it("is accepted where it is present, and the rest of the dashboard reads as without it", () => {
    for (const fixture of ["cheapest_direct_site_admin.json", "cheapest_no_site.json"]) {
      const plain = decodeDashboard(read("dashboard", fixture));
      const withBlock = decodeDashboard({ ...read("dashboard", fixture), grid_tariff: BLOCK });
      expect(plain.ok, fixture).toBe(true);
      expect(withBlock.ok, fixture).toBe(true);
      expect(withBlock, fixture).toEqual(plain);
    }
  });

  it("is accepted in any shape a later release may give it", () => {
    const payload = { ...read("dashboard", "cheapest_no_site.json"), grid_tariff: { ...BLOCK, demand: null } };
    expect(decodeDashboard(payload).ok).toBe(true);
  });
});
