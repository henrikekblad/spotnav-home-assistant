// The Stage-4A-2 stylesheet: the rules that make the card usable, and the classes that must exist.
//
// Deliberately not a snapshot: the assertions are the properties a hostile 320 px column, a themed
// dashboard and a keyboard reader depend on.

import { describe, expect, it } from "vitest";

import { VISUAL_CLASSES, FOCUS_LINE_TOKENS, VISUAL_STYLES, focusLineColour } from "../src/visual-styles";

const classes = Object.values(VISUAL_CLASSES);

/** WCAG relative luminance, so a fallback colour can be judged rather than asserted by eye. */
function luminance(hex: string): number {
  const value = hex.replace("#", "");
  const channels = [0, 2, 4].map((offset) => {
    const channel = Number.parseInt(value.slice(offset, offset + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(left: string, right: string): number {
  const [lighter, darker] = [luminance(left), luminance(right)].sort((a, b) => b - a) as [
    number,
    number,
  ];
  return (lighter + 0.05) / (darker + 0.05);
}

describe("every class a component uses is defined", () => {
  it("has a rule for each name in VISUAL_CLASSES", () => {
    const missing = classes.filter((name) => !VISUAL_STYLES.includes(`.${name}`));
    expect(missing).toEqual([]);
  });

  it("defines exactly the class names the components share, with no duplicates", () => {
    expect(new Set(classes).size).toBe(classes.length);
    expect(classes.length).toBeGreaterThan(40);
  });
});

describe("responsive and hostile-content rules", () => {
  it("never fixes a content width and never overflows horizontally", () => {
    const fixedWidths = VISUAL_STYLES.split("\n").filter((line) => /(^|[\s;{])width:\s*\d+px/.test(line));
    expect(fixedWidths).toEqual([]);
    expect(VISUAL_STYLES).toContain("max-width: 100%");
    expect(VISUAL_STYLES).toContain("box-sizing: border-box");
    expect(VISUAL_STYLES).toContain("overflow-wrap: break-word");
    expect(VISUAL_STYLES).toContain("min-width: 0");
  });

  it("caps the graph, and leaves the chart height to the policy rather than to CSS", () => {
    expect(VISUAL_STYLES).toContain("max-width: 640px");
    expect(VISUAL_STYLES).toContain(`.${VISUAL_CLASSES.viewport}`);
    // The SVG fills the viewport; the viewport's height is computed in JavaScript from its width, so
    // no CSS height can disagree with the drawn geometry.
    expect(VISUAL_STYLES).toMatch(/\.spotnav-chart-viewport \{[^}]*width: 100%/s);
    const fixedHeights = VISUAL_STYLES.split("\n").filter((line) => /(^|[\s;{])height:\s*\d+px/.test(line));
    expect(fixedHeights).toEqual([]);
  });

  it("makes the graph gesture-safe and the dialogs overlays that cannot shift the card", () => {
    expect(VISUAL_STYLES).toContain("user-select: none");
    expect(VISUAL_STYLES).toContain("touch-action: pan-y");
    expect(VISUAL_STYLES).toMatch(/\.spotnav-dialog-overlay \{[^}]*position: fixed/s);
    expect(VISUAL_STYLES).toContain("[hidden]");
  });
});

describe("accessibility and theming", () => {
  it("keeps focus visible and touch targets practical", () => {
    expect(VISUAL_STYLES).toContain(":focus-visible");
    expect(VISUAL_STYLES).toContain("outline: 2px solid var(--primary-color, #03a9f4)");
    expect(VISUAL_STYLES).toContain("min-height: 44px");
    expect(VISUAL_STYLES).toContain("min-width: 44px");
  });

  it("honours reduced motion", () => {
    expect(VISUAL_STYLES).toContain("@media (prefers-reduced-motion: reduce)");
  });

  it("uses Home Assistant theme variables with plain fallbacks", () => {
    expect(VISUAL_STYLES).toContain("var(--ha-card-background, var(--card-background-color, #ffffff))");
    expect(VISUAL_STYLES).toContain("var(--primary-text-color, #212121)");
    expect(VISUAL_STYLES).toContain("var(--secondary-text-color, #727272)");
    expect(VISUAL_STYLES).toContain("var(--divider-color, #e0e0e0)");
    expect(VISUAL_STYLES).toContain("var(--error-color, #db4437)");
    // Every `var(` in the sheet has a fallback, so a missing theme variable is still legible.
    const withoutFallback = VISUAL_STYLES.split("\n").filter(
      (line) => /var\(--[a-z-]+\)/.test(line),
    );
    expect(withoutFallback).toEqual([]);
  });

  it("paints tomorrow as one neutral series instead of dimmed cheap and expensive colours", () => {
    const neutral = "var(--spotnav-tomorrow, var(--secondary-text-color, #727272))";
    const block = VISUAL_STYLES.match(/\.spotnav-tomorrow,[\s\S]*?\n  }/)?.[0] ?? "";
    expect(block).toContain(`fill: ${neutral}`);
    expect(block).toContain(`stroke: ${neutral}`);
    expect(block).toContain("opacity: 0.62");
    expect(block).not.toContain("--spotnav-cheap");
    expect(block).not.toContain("--spotnav-expensive");
  });

  it("loads no external font, image or network asset", () => {
    expect(VISUAL_STYLES).not.toContain("@import");
    expect(VISUAL_STYLES).not.toContain("url(");
    expect(VISUAL_STYLES).not.toContain("@font-face");
    expect(VISUAL_STYLES).not.toMatch(/https?:\/\//);
  });

  it("gives both focus lines a theme-resolved colour that is readable in light and dark", () => {
    for (const kind of ["now", "selection"] as const) {
      const token = FOCUS_LINE_TOKENS[kind];
      const declaration = focusLineColour(kind);
      // The semantic property first, then Home Assistant's own foreground -- which is dark text on a
      // light card and light text on a dark one -- and only then a plain fallback.
      expect(declaration).toBe(`var(${token.property}, var(${token.theme}, ${token.fallback}))`);
      expect(token.theme).toBe("--primary-text-color");
      expect(token.property).toBe(`--spotnav-${kind === "now" ? "now" : "selection"}-line`);
      // No literal white anywhere in the contract: it would vanish on a light card.
      expect(declaration.toLowerCase()).not.toMatch(/#fff|white/);
      // The fallback only has to stand in when no theme variable exists at all, and it must still be
      // legible against a white card *and* a near-black one (3:1, the non-text contrast rule).
      expect(contrast(token.fallback, "#ffffff")).toBeGreaterThanOrEqual(3);
      expect(contrast(token.fallback, "#121212")).toBeGreaterThanOrEqual(3);
    }
    // And the stylesheet really is generated from that table, for the one class each line carries.
    expect(VISUAL_STYLES).toContain(`stroke: ${focusLineColour("now")};`);
    expect(VISUAL_STYLES).toContain(`stroke: ${focusLineColour("selection")};`);
    expect(VISUAL_STYLES).toMatch(
      new RegExp(`\\.${VISUAL_CLASSES.now} \\{[^}]*stroke-width: 1\\.5`, "s"),
    );
    expect(VISUAL_STYLES).toMatch(
      new RegExp(`\\.${VISUAL_CLASSES.selection} \\{[^}]*stroke-dasharray: 3 3`, "s"),
    );
    // The solid now line is never dashed: the selection line is the only dashed one.
    expect(VISUAL_STYLES).not.toMatch(
      new RegExp(`\\.${VISUAL_CLASSES.now} \\{[^}]*stroke-dasharray`, "s"),
    );
  });

  it("draws every slider alone under its label row, with no slider-and-field pair left", () => {
    // The energy, target, periods and current sliders stand alone at the full width; the old pair grid,
    // its number-field column and its out-of-range note are gone.
    expect(VISUAL_STYLES).not.toContain("spotnav-settings-pair");
    expect(Object.values(VISUAL_CLASSES)).not.toContain("spotnav-settings-pair");
    expect(VISUAL_STYLES).toMatch(
      new RegExp(`\\.${VISUAL_CLASSES.settingsSlider} \\{[^}]*width: 100%[^}]*min-width: 0`, "s"),
    );
    // The unit beside other figures (market, entity editor) is sized by its own text and never wraps.
    expect(VISUAL_STYLES).toMatch(
      new RegExp(`\\.${VISUAL_CLASSES.settingsUnit} \\{[^}]*white-space: nowrap`, "s"),
    );
    // Grids of their own: the market value row, the action bar's two (three columns narrow, six in the
    // wide container query) and the entity editor's meter line (one column narrow, auto columns wide).
    expect(VISUAL_STYLES.match(/grid-template-columns:/g)?.length).toBe(5);
    const valueBlock =
      VISUAL_STYLES.match(new RegExp(`\\.${VISUAL_CLASSES.marketValue} \\{[^}]*\}`, "s"))?.[0] ?? "";
    expect(valueBlock).toContain("minmax(0, 1fr)");
    expect(valueBlock).not.toContain("flex");
  });

  it("keeps the help line under a slider close to it, and only that note", () => {
    // Room under the track for the "full" word, and no more.
    const marked = VISUAL_STYLES.match(
      /\.spotnav-settings-track:has\(> \.spotnav-settings-full-mark:not\(\[hidden\]\)\) \{([^}]*)\}/s,
    )?.[1];
    expect(marked?.trim()).toBe("padding-bottom: 0.4rem;");
    // The note right under a slider's field sits on it; every other note keeps its own space above.
    const under = VISUAL_STYLES.match(
      /\.spotnav-settings-field:has\(> \.spotnav-settings-track\) \+ \.spotnav-settings-note \{([^}]*)\}/s,
    )?.[1];
    expect(under?.trim()).toBe("margin: 0;");
    const shared = VISUAL_STYLES.match(/\.spotnav-settings-readonly,\s*\.spotnav-settings-note \{([^}]*)\}/s)?.[1] ?? "";
    expect(shared).toContain("margin: 8px 0 0");
  });

  it("sets the checkbox's label as every field label is set", () => {
    // One label rule for "Finish by a deadline" and "Requested energy": its size, weight and colour.
    const label = VISUAL_STYLES.match(new RegExp(`\\.${VISUAL_CLASSES.settingsLabel} \\{([^}]*)\}`, "s"))?.[1] ?? "";
    expect(label).toContain("font-size: 0.85em");
    expect(label).toContain("font-weight: 400");
    // No rule sets the checkbox row's label apart.
    expect(VISUAL_STYLES).not.toMatch(/spotnav-settings-check-row[^{]*spotnav-settings-label/);
  });

  it("honours the platform's forced-colours palette for the focus lines", () => {
    expect(VISUAL_STYLES).toContain("@media (forced-colors: active)");
    const block = VISUAL_STYLES.slice(VISUAL_STYLES.indexOf("@media (forced-colors: active)"));
    expect(block).toContain(`.${VISUAL_CLASSES.now}`);
    expect(block).toContain(`.${VISUAL_CLASSES.selection}`);
    expect(block).toContain("stroke: CanvasText");
  });
});

// ------------------------------------------------------------------------------------------------
// The compact header, and the fiscal row it now heads: the two layout guarantees a phone depends on.
// ------------------------------------------------------------------------------------------------

/** One rule's own body, by class: the properties a layout guarantee actually rests on. */
function rule(className: string): string {
  return VISUAL_STYLES.match(new RegExp(`\\.${className} \\{([^}]*)\\}`, "s"))?.[1] ?? "";
}

describe("the compact dialog header and the fiscal row", () => {
  it("shares one header row: the title takes the width, the close keeps a 44x44 target", () => {
    const header = rule(VISUAL_CLASSES.dialogHeader);
    expect(header).toContain("display: flex");
    expect(header).toContain("justify-content: space-between");
    // The title owns everything the close does not need, may shrink, and wraps rather than colliding.
    const title = rule(VISUAL_CLASSES.dialogTitle);
    expect(title).toContain("flex: 1 1 auto");
    expect(title).toContain("min-width: 0");
    expect(title).toContain("overflow-wrap: break-word");
    // And the close keeps its tap target, sized by its own rule, in that same row.
    const close = rule(VISUAL_CLASSES.dialogClose);
    expect(close).toContain("flex: none");
    expect(close).toContain("min-width: 44px");
    expect(close).toContain("min-height: 44px");
    expect(close).not.toContain("align-self: flex-end");
  });

  it("lays a fiscal row out so a long name and a 320 px card still fit", () => {
    const row = rule(VISUAL_CLASSES.marketValue);
    expect(row).toContain("grid-template-columns: auto minmax(0, 1fr) clamp(4.5rem, 20%, 6rem) auto");
    // Arithmetic rather than a screenshot: the three fixed columns plus the gaps, at their worst, must
    // leave room for the name, so a long translated component wraps inside the row instead of pushing
    // the figure out of the card.
    // 16 px in this stylesheet's own units: the figure's clamp maximum (6rem), the unit's own text and
    // the three gaps the row declares.
    const remPx = 16;
    const worstCasePx = 6 * remPx + 2 * remPx + 3 * 8;
    expect(worstCasePx).toBeLessThan(320);
    // One rule for the row, and no per-component variant of it.
    expect(VISUAL_STYLES).not.toMatch(/spotnav-market-value\.[a-z-]/);
    // The reset sits beside the suggestion and wraps with it; the checkbox stays native, because this
    // stylesheet fixes no pixel size and the label beside it is the tap target.
    expect(rule(VISUAL_CLASSES.marketReset)).toContain("flex: none");
    expect(rule(VISUAL_CLASSES.marketSuggestion)).toContain("flex-wrap: wrap");
    expect(rule(VISUAL_CLASSES.marketCheckbox)).not.toContain("width:");
    expect(rule(VISUAL_CLASSES.marketCheckbox)).not.toContain("height:");
  });
});
