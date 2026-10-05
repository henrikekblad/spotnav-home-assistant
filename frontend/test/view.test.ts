// The card's configuration contract and the editor's choices: no rendering lives here.

import { describe, expect, it } from "vitest";

import { chargerOptions, editorConfig, parseCardConfig, stubChargerId } from "../src/view";
import { charger, chargerList } from "./helpers";


describe("configuration", () => {
  it("accepts the one documented shape", () => {
    expect(parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a" })).toEqual({
      type: "custom:spotnav-card",
      charger: "entry_a",
    });
  });

  it("treats a missing or empty charger as a selectable state, not an error", () => {
    expect(parseCardConfig({ type: "custom:spotnav-card" }).charger).toBe("");
    expect(parseCardConfig({ type: "custom:spotnav-card", charger: "" }).charger).toBe("");
  });

  it("refuses the bare type, other cards, no config, arrays and wrong charger types", () => {
    expect(() => parseCardConfig(null)).toThrow(/object/);
    expect(() => parseCardConfig([])).toThrow(/object/);
    expect(() => parseCardConfig({ charger: "entry_a" })).toThrow(/type/);
    expect(() => parseCardConfig({ type: "spotnav-card", charger: "entry_a" })).toThrow(/type/);
    expect(() => parseCardConfig({ type: "custom:other-card", charger: "a" })).toThrow(/type/);
    expect(() => parseCardConfig({ type: "custom:spotnav-card", charger: 42 })).toThrow(/charger/);
  });

  it("refuses an unknown option instead of ignoring it", () => {
    expect(() =>
      parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a", theme: "dark" }),
    ).toThrow(/only the type, charger and chart options/);
    expect(() =>
      parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a", amps: 16 }),
    ).toThrow(/only the type, charger and chart options/);
  });

  it("requires a real id: no whitespace-only, and no silent trimming", () => {
    expect(() => parseCardConfig({ type: "custom:spotnav-card", charger: "   " })).toThrow(
      /charger/,
    );
    expect(() => parseCardConfig({ type: "custom:spotnav-card", charger: " entry_a" })).toThrow(
      /charger/,
    );
    expect(() => parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a " })).toThrow(
      /charger/,
    );
    // An id with a space inside it is not trimmed into a different id: it is refused or kept whole.
    expect(parseCardConfig({ type: "custom:spotnav-card", charger: "entry a" }).charger).toBe(
      "entry a",
    );
  });

  it("takes the chart option as the collapsed or full default, and refuses any other value", () => {
    expect(parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a", chart: "compact" })).toEqual({
      type: "custom:spotnav-card",
      charger: "entry_a",
      chart: "compact",
    });
    expect(parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a", chart: "full" }).chart).toBe("full");
    // Without the option the key is absent, so a stored config round-trips unchanged.
    expect("chart" in parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a" })).toBe(false);
    for (const value of ["collapsed", "Compact", true, 1, null, ""]) {
      expect(() =>
        parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a", chart: value }),
      ).toThrow(/chart must be "full" or "compact"/);
    }
    expect(() =>
      parseCardConfig({ type: "custom:spotnav-card", charger: "entry_a", chart_collapsed: true }),
    ).toThrow(/only the type, charger and chart options/);
  });

  it("keeps the chart option when the editor changes the charger", () => {
    expect(editorConfig("entry_b", "compact")).toEqual({
      type: "custom:spotnav-card",
      charger: "entry_b",
      chart: "compact",
    });
  });

  it("emits exactly the type and charger keys", () => {
    expect(editorConfig("entry_a")).toEqual({ type: "custom:spotnav-card", charger: "entry_a" });
    expect(Object.keys(editorConfig("entry_a")).sort()).toEqual(["charger", "type"]);
  });
});

describe("editor choices", () => {
  it("offers every charger with its display name and availability", () => {
    const list = chargerList([charger("a", "Garage"), charger("b", "Carport", false)]);
    expect(chargerOptions(list, null)).toEqual([
      { charger_id: "a", label: "Garage", available: true },
      { charger_id: "b", label: "Carport", available: false },
    ]);
  });

  it("keeps a configured charger that is no longer offered, as unresolved", () => {
    const list = chargerList([charger("a", "Garage")]);
    expect(chargerOptions(list, "gone")[0]).toEqual({
      charger_id: "gone",
      label: "gone",
      available: false,
    });
  });

  it("selects automatically only when exactly one charger is available", () => {
    expect(stubChargerId(chargerList([charger("a", "Garage")]))).toBe("a");
    expect(stubChargerId(chargerList([charger("a", "Garage", false)]))).toBeNull();
    expect(stubChargerId(chargerList([charger("a", "One"), charger("b", "Two")]))).toBeNull();
    expect(stubChargerId(chargerList([charger("a", "One"), charger("b", "Two", false)]))).toBe("a");
    expect(stubChargerId(null)).toBeNull();
  });
});
