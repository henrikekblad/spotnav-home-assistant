// Pins every export of visual-styles.ts byte for byte, so splitting the stylesheet by area cannot change
// a single character or the order of the rules (the cascade depends on it).

import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import * as styles from "../src/visual-styles";

const sha = (text: string): string => createHash("sha256").update(text).digest("hex");

describe("visual-styles exports are unchanged", () => {
  it("the stylesheet string", () => {
    expect(styles.VISUAL_STYLES.length).toBe(LENGTH);
    expect(sha(styles.VISUAL_STYLES)).toBe(STYLES_HASH);
  });

  it("the data exports", () => {
    const data = {
      HEADER_NARROW_PX: styles.HEADER_NARROW_PX,
      LONG_VALUE_LENGTH: styles.LONG_VALUE_LENGTH,
      VISUAL_CLASSES: styles.VISUAL_CLASSES,
      FOCUS_LINE_TOKENS: styles.FOCUS_LINE_TOKENS,
      PLAN_BAND_STYLE: styles.PLAN_BAND_STYLE,
      CHART_DENSITY: styles.CHART_DENSITY,
    };
    expect(sha(JSON.stringify(data))).toBe(DATA_HASH);
  });

  it("the functions", () => {
    const out = [
      styles.focusLineColour("now"),
      styles.focusLineColour("selection"),
      styles.summaryValueClass(""),
      styles.summaryValueClass("x".repeat(styles.LONG_VALUE_LENGTH)),
      styles.summaryValueClass("x".repeat(styles.LONG_VALUE_LENGTH + 1)),
    ];
    expect(sha(JSON.stringify(out))).toBe(FUNCTIONS_HASH);
  });

  it("the export names", () => {
    expect(Object.keys(styles).sort()).toEqual(EXPORTS);
  });
});

const LENGTH = 84347;
const STYLES_HASH = "d5bee185ea03c6cafdbca457edfbbb57134d1e946ba40ac9d0d6fd2f345ba3e6";
const DATA_HASH = "8c52cf62fe550c677dc54f42b49fef133550f0c07ad7ffb67ef11ff4b44447a2";
const FUNCTIONS_HASH = "5f72b60d9ace94f7d6785e0f611db9702d7801abb701f3d74622a0b15a31524d";
const EXPORTS = [
  "CHART_DENSITY",
  "FOCUS_LINE_TOKENS",
  "HEADER_NARROW_PX",
  "LONG_VALUE_LENGTH",
  "PLAN_BAND_STYLE",
  "VISUAL_CLASSES",
  "VISUAL_STYLES",
  "focusLineColour",
  "summaryValueClass",
];
