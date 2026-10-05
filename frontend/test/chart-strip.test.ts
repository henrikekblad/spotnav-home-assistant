// The collapsed chart: the slim 24-hour strip of charging periods, the price row that toggles it, and
// the per-charger, per-browser memory of the choice.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createCardView, type CardView, type CardViewInput } from "../src/card-view";
import type { ChartNow, PlanBand, PlanBands } from "../src/chart";
import {
  chartPreferenceKey,
  initialChartCollapsed,
  readChartCollapsed,
  writeChartCollapsed,
  type PreferenceStore,
} from "../src/chart-preference";
import {
  STRIP_MIN_BAR_PX,
  stripBarPlacement,
  stripBars,
  stripNowPosition,
  stripTicks,
} from "../src/chart-strip";
import { translate } from "../src/i18n";
import type { CardModel } from "../src/model";
import { VISUAL_CLASSES as C, VISUAL_STYLES } from "../src/visual-styles";
import { dashboard, FakeHass, mountCard } from "./helpers";
import { mountPoint, NOW, quarterModel, twoDayModel } from "./visual-fixtures";

const DAY_MS = 86_400_000;
const HOUR_MS = 3_600_000;
const MIDNIGHT = Date.parse("2026-09-21T22:00:00Z"); // 00:00 local, 2026-09-22

function band(startHour: number, endHour: number, dayIndex = 0): PlanBand {
  const dayStart = MIDNIGHT + dayIndex * DAY_MS;
  return {
    dayKey: dayIndex === 0 ? "2026-09-22" : "2026-09-23",
    dayIndex,
    startMs: dayStart + startHour * HOUR_MS,
    endMs: dayStart + endHour * HOUR_MS,
    startPosition: startHour / 24,
    endPosition: endHour / 24,
  };
}

function bands(installed: PlanBand[], proposal: PlanBand[] = []): PlanBands {
  return { installed, proposal };
}

describe("the strip's bars", () => {
  const now = MIDNIGHT + 12 * HOUR_MS;

  it("dims a period that is over and keeps a coming one at full strength", () => {
    const bars = stripBars(bands([band(2, 4), band(20, 22)]), now);
    expect(bars).toEqual([
      { kind: "installed", start: 2 / 24, end: 4 / 24, past: true },
      { kind: "installed", start: 20 / 24, end: 22 / 24, past: false },
    ]);
  });

  it("splits a period now under way at the now instant", () => {
    const bars = stripBars(bands([band(11, 13)]), now);
    expect(bars).toEqual([
      { kind: "installed", start: 11 / 24, end: 12 / 24, past: true },
      { kind: "installed", start: 12 / 24, end: 13 / 24, past: false },
    ]);
  });

  it("draws the proposal's periods too, in time order, and tomorrow's on the same wall-clock axis", () => {
    const bars = stripBars(bands([band(20, 21)], [band(3, 4, 1), band(1, 2)]), now);
    expect(bars.map((bar) => [bar.kind, bar.start * 24, bar.past])).toEqual([
      ["proposal", 1, true],
      ["proposal", 3, false],
      ["installed", 20, false],
    ]);
  });

  it("gives a quarter-hour at least the minimum width, and never lets it run past either end", () => {
    expect(STRIP_MIN_BAR_PX).toBe(3);
    const quarter = { kind: "installed" as const, start: 3 / 96, end: 4 / 96, past: false };
    const first = stripBarPlacement(quarter);
    expect(Number.parseFloat(first.left ?? "")).toBeCloseTo((3 / 96) * 100, 3);
    expect(first.right).toBeNull();
    expect(Number.parseFloat(first.width)).toBeCloseTo((1 / 96) * 100, 3);
    expect(first.minWidth).toBe("3px");
    // In the right half the bar is anchored on its end, so the minimum width grows inwards.
    const last = stripBarPlacement({ kind: "installed" as const, start: 95 / 96, end: 1, past: false });
    expect(last.left).toBeNull();
    expect(last.right).toBe("0%");
    expect(Number.parseFloat(last.width)).toBeCloseTo((1 / 96) * 100, 3);
    expect(last.minWidth).toBe("3px");
  });
});

describe("the strip's now line and hour labels", () => {
  it("sits at the now instant inside the current interval, and is absent without one", () => {
    const model = quarterModel(() => ({}));
    const mark = model.chart.marks[2];
    expect(mark).toBeDefined();
    const nowFact: ChartNow = { mark: mark ?? null, identity: "x" };
    const at = (mark?.startMs ?? 0) + 5 * 60_000;
    expect(stripNowPosition(nowFact, at)).toBeCloseTo((mark?.position ?? 0) + 5 / 1440, 10);
    expect(stripNowPosition({ mark: null, identity: null }, at)).toBeNull();
  });

  it("labels 00, 06, 12, 18 and 24 at the chart's own tick positions, for one day and for two", () => {
    for (const model of [quarterModel(() => ({})), twoDayModel()]) {
      expect(stripTicks(model.chart)).toEqual([
        { position: 0, label: "00" },
        { position: 0.25, label: "06" },
        { position: 0.5, label: "12" },
        { position: 0.75, label: "18" },
        { position: 1, label: "24" },
      ]);
    }
  });
});

function view(
  model: CardModel,
  overrides: Partial<CardViewInput> = {},
): { card: CardView; root: ShadowRoot } {
  const mount = mountPoint("strip");
  const card = createCardView({
    model,
    mount: mount.root,
    idPrefix: "strip",
    now: () => NOW,
    measure: () => ({ width: 320, height: 150 }),
    observe: () => null,
    onAction: () => {},
    onOpenSettings: () => {},
    onSaveSettings: () => {},
    onReloadSettings: () => {},
    onReapplySettings: () => {},
    onOpenMarket: () => {},
    onSaveMarket: () => {},
    onReloadMarket: () => {},
    onReapplyMarket: () => {},
    onMarketAreaChange: () => {},
    isAdmin: true,
    onSelectStrategy: () => {},
    ...overrides,
  });
  return { card, root: mount.root };
}

// 04:00Z-05:00Z in quarter-hours, the clock at 04:30Z: the first quarter is over, the last is to come.
const pastAndComing = (): CardModel =>
  quarterModel((index) => (index === 0 || index === 3 ? { installed_planned: true } : {}));

const toggleOf = (root: ShadowRoot): HTMLElement => root.querySelector(`.${C.summary}`) as HTMLElement;
const stripOf = (root: ShadowRoot): HTMLElement => root.querySelector(`.${C.strip}`) as HTMLElement;
const viewportOf = (root: ShadowRoot): HTMLElement => root.querySelector(`.${C.viewport}`) as HTMLElement;
const chevronOf = (root: ShadowRoot): string =>
  root.querySelector(`.${C.chartToggleGlyph}`)?.textContent ?? "";

describe("the price row as the chart's toggle", () => {
  it("is a button naming what it does, controlling the chart and the strip", () => {
    const { card, root } = view(pastAndComing());
    const toggle = toggleOf(root);
    expect(toggle.getAttribute("role")).toBe("button");
    expect(toggle.tabIndex).toBe(0);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.getAttribute("aria-controls")).toBe("strip-chart strip-strip");
    expect(viewportOf(root).id).toBe("strip-chart");
    expect(stripOf(root).id).toBe("strip-strip");
    expect(toggle.textContent).toContain(translate("en", "graph.toggle.hide"));
    expect(chevronOf(root)).toBe("˄");
    expect(root.querySelector(`.${C.chartToggleGlyph}`)?.getAttribute("aria-hidden")).toBe("true");
    expect(viewportOf(root).hidden).toBe(false);
    expect(stripOf(root).hidden).toBe(true);
    card.destroy();
  });

  it("collapses and expands on a click, says so, and reports the choice", () => {
    const changes: boolean[] = [];
    const { card, root } = view(pastAndComing(), { onChartCollapsedChange: (collapsed) => changes.push(collapsed) });
    const toggle = toggleOf(root);
    const before = Array.from(toggle.children).map((child) => child.className);
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(viewportOf(root).hidden).toBe(true);
    expect(root.querySelector(`.${C.readout}`)?.hasAttribute("hidden")).toBe(true);
    expect(stripOf(root).hidden).toBe(false);
    expect(chevronOf(root)).toBe("˅");
    expect(toggle.textContent).toContain(translate("en", "graph.toggle.show"));
    // The row keeps the same parts in the same order: only the chevron's glyph and the hidden words change.
    expect(Array.from(toggle.children).map((child) => child.className)).toEqual(before);
    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(viewportOf(root).hidden).toBe(false);
    expect(stripOf(root).hidden).toBe(true);
    expect(changes).toEqual([true, false]);
    card.destroy();
  });

  it("answers Enter and Space, and leaves every other key alone", () => {
    const changes: boolean[] = [];
    const { card, root } = view(pastAndComing(), { onChartCollapsedChange: (collapsed) => changes.push(collapsed) });
    const toggle = toggleOf(root);
    const press = (key: string): KeyboardEvent => {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      toggle.dispatchEvent(event);
      return event;
    };
    expect(press("Enter").defaultPrevented).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(press(" ").defaultPrevented).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(press("a").defaultPrevented).toBe(false);
    expect(press("ArrowRight").defaultPrevented).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(changes).toEqual([true, false]);
    card.destroy();
  });

  it("starts collapsed when asked, and clears a chart selection when it collapses", () => {
    const collapsed = view(pastAndComing(), { chartCollapsed: true });
    expect(toggleOf(collapsed.root).getAttribute("aria-expanded")).toBe("false");
    expect(viewportOf(collapsed.root).hidden).toBe(true);
    expect(stripOf(collapsed.root).hidden).toBe(false);
    expect(chevronOf(collapsed.root)).toBe("˅");
    collapsed.card.destroy();

    const open = view(pastAndComing());
    const mark = pastAndComing().chart.marks[1] ?? null;
    open.card.chart().select(mark);
    expect(open.card.selection()).not.toBeNull();
    toggleOf(open.root).click();
    expect(open.card.selection()).toBeNull();
    open.card.destroy();
  });
});

describe("the strip in the card", () => {
  it("draws the plan's periods, the past one dimmed, each with a minimum width", () => {
    const { card, root } = view(pastAndComing(), { chartCollapsed: true });
    const bars = Array.from(root.querySelectorAll<HTMLElement>(`.${C.stripBar}`));
    // 04:00Z is 06:00 local: the first quarter is over, the last has not begun.
    expect(bars.map((bar) => bar.dataset["past"])).toEqual(["true", "false"]);
    expect(Number.parseFloat(bars[0]?.style.left ?? "")).toBeCloseTo((24 / 96) * 100, 3);
    for (const bar of bars) {
      expect(bar.style.minWidth).toBe("3px");
      expect(Number.parseFloat(bar.style.width)).toBeCloseTo((1 / 96) * 100, 3);
    }
    expect(VISUAL_STYLES).toMatch(/\.spotnav-strip-bar\[data-past="true"\] \{[^}]*opacity/s);
    card.destroy();
  });

  it("draws the now line where the clock is, and the hour labels under the track", () => {
    const { card, root } = view(pastAndComing(), { chartCollapsed: true });
    const line = root.querySelector<HTMLElement>(`.${C.stripNow}`);
    // 04:30Z is 06:30 local.
    expect(line?.hidden).toBe(false);
    expect(Number.parseFloat(line?.style.left ?? "")).toBeCloseTo((6.5 / 24) * 100, 6);
    const labels = Array.from(root.querySelectorAll(`.${C.stripLabel}`)).map((node) => node.textContent);
    expect(labels).toEqual(["00", "06", "12", "18", "24"]);
    expect(stripOf(root).getAttribute("aria-hidden")).toBe("true");
    card.destroy();
  });

  it("hides the now line when no published interval holds the clock", () => {
    const { card, root } = view(pastAndComing(), { chartCollapsed: true, now: () => NOW + 6 * HOUR_MS });
    expect(root.querySelector<HTMLElement>(`.${C.stripNow}`)?.hidden).toBe(true);
    card.destroy();
  });

  it("follows the chart's two days on one wall-clock axis", () => {
    const { card, root } = view(twoDayModel(), { chartCollapsed: true });
    const labels = Array.from(root.querySelectorAll(`.${C.stripLabel}`)).map((node) => node.textContent);
    expect(labels).toEqual(["00", "06", "12", "18", "24"]);
    card.destroy();
  });

  it("has a slim rounded track in the card's muted colour", () => {
    const track = VISUAL_STYLES.match(/\.spotnav-strip-track \{([^}]*)\}/s)?.[1] ?? "";
    // 0.7rem: about 11 px, sized in rem like the rest of the card.
    expect(track).toContain("height: 0.7rem");
    expect(track).toContain("border-radius");
    expect(track).toContain("var(--secondary-background-color");
    expect(VISUAL_STYLES).toMatch(/\.spotnav-chart-viewport\[hidden\][^{]*\{[^}]*display: none/s);
  });
});

describe("remembering the choice", () => {
  function memoryStore(): PreferenceStore & { items: Map<string, string> } {
    const items = new Map<string, string>();
    return {
      items,
      getItem: (key) => items.get(key) ?? null,
      setItem: (key, value) => {
        items.set(key, value);
      },
    };
  }

  it("keys the choice by the charger's config-entry id", () => {
    expect(chartPreferenceKey("entry_a")).toBe("spotnav-card.chart.entry_a");
    const store = memoryStore();
    writeChartCollapsed(() => store, "entry_a", true);
    expect(store.items.get("spotnav-card.chart.entry_a")).toBe("compact");
    expect(readChartCollapsed(() => store, "entry_a")).toBe(true);
    expect(readChartCollapsed(() => store, "entry_b")).toBeNull();
    writeChartCollapsed(() => store, "entry_a", false);
    expect(readChartCollapsed(() => store, "entry_a")).toBe(false);
    store.items.set("spotnav-card.chart.entry_a", "nonsense");
    expect(readChartCollapsed(() => store, "entry_a")).toBeNull();
  });

  it("reads nothing and writes nothing without storage, or when it throws", () => {
    expect(readChartCollapsed(() => null, "entry_a")).toBeNull();
    writeChartCollapsed(() => null, "entry_a", true);
    const throwing: PreferenceStore = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    };
    expect(readChartCollapsed(() => throwing, "entry_a")).toBeNull();
    expect(() => writeChartCollapsed(() => throwing, "entry_a", true)).not.toThrow();
    const unreachable = (): PreferenceStore => {
      throw new Error("SecurityError");
    };
    expect(readChartCollapsed(unreachable, "entry_a")).toBeNull();
    expect(() => writeChartCollapsed(unreachable, "entry_a", true)).not.toThrow();
  });

  it("lets the stored choice win over the YAML default", () => {
    expect(initialChartCollapsed(null, undefined)).toBe(false);
    expect(initialChartCollapsed(null, "compact")).toBe(true);
    expect(initialChartCollapsed(null, "full")).toBe(false);
    expect(initialChartCollapsed(false, "compact")).toBe(false);
    expect(initialChartCollapsed(true, "full")).toBe(true);
  });
});

describe("the card's chart memory, per charger and per browser", () => {
  const settle = async (): Promise<void> => {
    await vi.advanceTimersByTimeAsync(0);
  };
  const expanded = (element: Element): string | null =>
    (element.shadowRoot as ShadowRoot).querySelector(`.${C.summary}`)?.getAttribute("aria-expanded") ?? null;

  async function ready(config: Record<string, unknown>): Promise<{ element: HTMLElement; hass: FakeHass }> {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a", ...config });
    (element as unknown as { hass: FakeHass }).hass = hass;
    await settle();
    hass.resolveNext(dashboard());
    await settle();
    return { element, hass };
  }

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
    vi.useRealTimers();
  });

  it("opens with the full chart by default, and with the strip when the YAML says compact", async () => {
    expect(expanded((await ready({})).element)).toBe("true");
    expect(expanded((await ready({ chart: "compact" })).element)).toBe("false");
  });

  it("remembers a toggle for that charger and lets it win over the YAML", async () => {
    const first = await ready({ chart: "compact" });
    (first.element.shadowRoot?.querySelector(`.${C.summary}`) as HTMLElement).click();
    expect(window.localStorage.getItem("spotnav-card.chart.entry_a")).toBe("full");
    expect(expanded((await ready({ chart: "compact" })).element)).toBe("true");
    window.localStorage.setItem("spotnav-card.chart.entry_a", "compact");
    expect(expanded((await ready({})).element)).toBe("false");
    // Another charger's card has its own memory.
    expect(window.localStorage.getItem("spotnav-card.chart.entry_b")).toBeNull();
  });

  it("keeps the choice across a refresh even when storage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    const { element, hass } = await ready({ chart: "compact" });
    expect(expanded(element)).toBe("false");
    (element.shadowRoot?.querySelector(`.${C.summary}`) as HTMLElement).click();
    expect(expanded(element)).toBe("true");
    // The next poll rebuilds the view; the choice made in this card stands.
    await vi.advanceTimersByTimeAsync(30_000);
    hass.resolveNext(dashboard());
    await settle();
    expect(expanded(element)).toBe("true");
  });

  it("renders without any storage at all", async () => {
    vi.stubGlobal("localStorage", undefined);
    const { element } = await ready({ chart: "compact" });
    expect(expanded(element)).toBe("false");
    (element.shadowRoot?.querySelector(`.${C.summary}`) as HTMLElement).click();
    expect(expanded(element)).toBe("true");
  });
});
