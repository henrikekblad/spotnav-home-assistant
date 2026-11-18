// What a battery figure is made of, in the readout: the energy given back, its worth, what it cost out of the stock
// (to be compared with the battery maker's cash flow), and an hour that only charged; the older answer without it.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { earnBars } from "../src/house-earnings";
import { decodeHouseHistory, type BatteryFlow, type HouseHistory } from "../src/house-history";
import { flowWords } from "../src/house-period-view";
import { LANGUAGES } from "../src/i18n";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "house_history");

function history(name: string, change: (raw: Record<string, unknown>) => void = () => undefined): HouseHistory {
  const raw = JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as Record<string, unknown>;
  change(raw);
  const decoded = decodeHouseHistory(raw);
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

const kr = (minor: number): string => `${(minor / 100).toFixed(2)} kr`;
const BOTH: BatteryFlow = { discharged: 3.5, worth: 810, cost: 560, charged: 4.2, charge_cost: 390 };

describe("the words for a battery figure", () => {
  it("says the discharge as worth less cost and the charge as counted when used, in Swedish and English", () => {
    expect(flowWords("sv", { ...BOTH, charged: 0, charge_cost: 0 }, kr)).toBe("urladdat 3,5 kWh, värt 8.10 kr, minus 5.60 kr som energin kostade att ladda = 2.50 kr");
    expect(flowWords("sv", { ...BOTH, discharged: 0, worth: 0, cost: 0 }, kr)).toBe("laddade 4,2 kWh för 3.90 kr (räknas när energin används)");
    expect(flowWords("en", BOTH, kr)).toBe(
      "discharged 3.5 kWh, worth 8.10 kr, minus 5.60 kr that the energy cost to charge = 2.50 kr · charged 4.2 kWh for 3.90 kr (counted when the energy is used)",
    );
    expect(flowWords("sv", { discharged: 0, worth: 0, cost: 0, charged: 0, charge_cost: 0 }, kr)).toBeNull();
  });

  it("is written in every language, with the numbers in its own format", () => {
    for (const language of LANGUAGES) {
      const text = flowWords(language, BOTH, kr)!;
      expect(text, language).toMatch(/8\.10 kr/);
      expect(text, language).not.toMatch(/[{}]/);
      expect(text, language).toContain(new Intl.NumberFormat(language, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(3.5));
    }
  });
});

describe("the readout of a bar", () => {
  const withFlow = (answer: HouseHistory, index: number, flow: BatteryFlow | null): HouseHistory => ({
    ...answer,
    buckets: answer.buckets.map((bucket, at) => (at === index ? { ...bucket, battery_flow: flow } : bucket)),
  });

  it("explains the hour in the Earnings tab, and falls back without it", () => {
    const day = withFlow(history("villa_day.json"), 17, BOTH);
    const chart = earnBars({ doc: document, language: "sv", answer: day, hidden: new Set(), height: 150, money: kr, selected: null })!;
    const label = chart.querySelectorAll(".spotnav-house-pbar")[17]!.getAttribute("aria-label")!;
    expect(label).toContain("17:00: urladdat 3,5 kWh, värt 8.10 kr");
    expect(label).not.toContain("Arbitrage");
    // An older hour keeps the short readout.
    const old = earnBars({ doc: document, language: "sv", answer: withFlow(day, 17, null), hidden: new Set(), height: 150, money: kr, selected: null })!;
    expect(old.querySelectorAll(".spotnav-house-pbar")[17]!.getAttribute("aria-label")).toMatch(/^17:00: .*kr$/);
    expect(old.querySelectorAll(".spotnav-house-pbar")[17]!.getAttribute("aria-label")).not.toContain("urladdat");
  });

  it("decodes an answer from before the breakdown with none", () => {
    const old = history("villa_month.json", (raw) => {
      for (const bucket of raw["buckets"] as Array<Record<string, unknown>>) {
        delete bucket["battery_flow"];
      }
    });
    expect(old.buckets.every((bucket) => bucket.battery_flow === null)).toBe(true);
  });
});
