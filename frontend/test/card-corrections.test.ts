// Card corrections, checked at the DOM boundary.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { VISUAL_STYLES } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(language = "en", charger = "entry_a") {
  const hass = new FakeHass();
  const element = mountCard({ type: "custom:spotnav-card", charger }, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(fixture("start_idle"));
  await settle();
  return { hass, element };
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("item 3: the four-cell action bar", () => {
  it("is one bar of four cells, each with an axis caption and an accessible name with axis and action", async () => {
    const { element } = await mounted();
    const cells = Array.from(shadow(element).querySelectorAll<HTMLButtonElement>(".spotnav-action-bar > button"));
    expect(cells.map((cell) => cell.dataset["cell"])).toEqual([
      "charging",
      "schedule",
      "strategy",
      "plan",
    ]);
    // The two old rows are gone.
    expect(shadow(element).querySelector(".spotnav-action-row")).toBeNull();
    expect(shadow(element).querySelector(".spotnav-settings-row")).toBeNull();
    const captions = cells.map((cell) => cell.querySelector(".spotnav-bar-caption")?.textContent);
    expect(captions).toEqual(["Charge now", "Schedule active", "Strategy", "Plan"]);
    for (const cell of cells) {
      expect(cell.getAttribute("aria-label"), cell.dataset["cell"]).toContain(":");
    }
    expect(cells[0]?.getAttribute("aria-label")).toBe("Charging: not charging. Start now");
    expect(cells[1]?.getAttribute("aria-label")).toBe("Schedule: active. Pause automatic charging");
  });

  it("captions every axis in the five locales", () => {
    for (const language of LANGUAGES) {
      for (const key of ["bar.charging", "bar.schedule", "bar.plan", "bar.start", "bar.stop", "bar.change"] as const) {
        expect(translate(language, key), `${language} ${key}`).not.toBe("");
      }
    }
    expect(translate("sv", "bar.charging")).toBe("Laddning");
    expect(translate("sv", "bar.schedule")).toBe("Schema");
    expect(translate("sv", "bar.plan")).toBe("Plan");
    // Norwegian and Danish called the schedule "Plan" too; it is a different cell from the Plan cell.
    for (const language of LANGUAGES) {
      expect(translate(language, "bar.schedule"), language).not.toBe(translate(language, "bar.plan"));
    }
  });

  it("is 2x2 on a narrow card and 4x1 on a wide one, by the card's own width", () => {
    expect(VISUAL_STYLES).toMatch(/\.spotnav-action-bar-wrap \{[^}]*container-type: inline-size/s);
    expect(VISUAL_STYLES).toMatch(/\.spotnav-action-bar \{[^}]*repeat\(2, minmax\(0, 1fr\)\)/s);
    expect(VISUAL_STYLES).toMatch(/@container \(min-width: 520px\) \{[^}]*repeat\(4, minmax\(0, 1fr\)\)/s);
  });

  it("keeps the same popovers: pause sheet, strategy dialog and the plan editor", async () => {
    const { element } = await mounted();
    const click = (cell: string) =>
      shadow(element).querySelector<HTMLButtonElement>(`.spotnav-action-bar [data-cell="${cell}"]`)?.click();
    const open = () =>
      Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"))
        .find((dialog) => dialog.closest("[hidden]") === null)?.textContent ?? "";
    click("schedule");
    expect(open()).toContain(translate("en", "pause.sheetTitle"));
    click("strategy");
    expect(open()).toContain(translate("en", "strategy.dialogTitle"));
  });
});

describe("item 4: the chart line above the plot", () => {
  it("never wraps and is not over the plot", () => {
    const block = VISUAL_STYLES.match(/\.spotnav-summary \{([^}]*)\}/s)?.[1] ?? "";
    expect(block).toContain("flex-wrap: nowrap");
    expect(block).toContain("white-space: nowrap");
    expect(block).not.toContain("position: absolute");
    const extremes = VISUAL_STYLES.match(/\.spotnav-summary-extremes \{([^}]*)\}/s)?.[1] ?? "";
    expect(extremes).toContain("flex-wrap: nowrap");
  });
});

describe("item 5: the SpotNav mark", () => {
  it("draws the owner's artwork, not the bolt, with ids unique per card instance", async () => {
    const first = await mounted();
    const second = await mounted();
    const ids = (element: Element): string[] =>
      Array.from(shadow(element).querySelectorAll(".spotnav-identity [id]")).map((node) => node.id);
    const firstIds = ids(first.element);
    const secondIds = ids(second.element);
    expect(firstIds.length).toBe(3);
    expect(secondIds.length).toBe(3);
    for (const id of firstIds) {
      expect(secondIds).not.toContain(id);
    }
    const mark = shadow(first.element).querySelector(".spotnav-identity");
    expect(mark?.getAttribute("viewBox")).toBe("0 0 306 306");
    expect(mark?.getAttribute("aria-hidden")).toBe("true");
    expect(mark?.innerHTML).not.toContain("M13 2 4 14h6l-1 8");
    // Every reference points at an id inside the same mark.
    for (const node of Array.from(mark?.querySelectorAll("[fill^='url'],[filter^='url']") ?? [])) {
      for (const attribute of ["fill", "filter"]) {
        const value = node.getAttribute(attribute) ?? "";
        if (value.startsWith("url(")) {
          expect(firstIds).toContain(/url\(#([^)]+)\)/.exec(value)?.[1]);
        }
      }
    }
  });
});

describe("item 6: checkbox rows are flush left with their label", () => {
  it("puts the deadline checkbox first in a left-aligned row", async () => {
    const { hass, element } = await mounted();
    shadow(element)
      .querySelector<HTMLButtonElement>(`.spotnav-action-bar [data-setting="plan"]`)
      ?.click();
    await settle();
    hass.resolveNext({
      api_version: 1,
      ok: true,
      error: null,
      settings: {
        revision: 7, area_id: "SE4", overrides: [], phases: 3, amps: 10, requested_kwh: 20,
        max_periods: 4, departure_enabled: true, departure_time: "06:30",
        strategy: "cheapest", driver: "manual_kwh",
        target: { vehicle_id: null, target_percent: null },
      },
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const row = shadow(element).querySelector<HTMLElement>(".spotnav-settings-check-row");
    expect(row).not.toBeNull();
    expect(row?.firstElementChild?.getAttribute("type")).toBe("checkbox");
    expect(row?.lastElementChild?.tagName).toBe("LABEL");
    const rule = VISUAL_STYLES.match(/\.spotnav-settings-check-row \{([^}]*)\}/s)?.[1] ?? "";
    expect(rule).toContain("flex-direction: row");
    expect(rule).toContain("justify-content: flex-start");
  });
});

describe("item 7: explicit schedule execution is not listed", () => {
  it("has no such row in Info", async () => {
    const { element } = await mounted();
    shadow(element)
      .querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.info")}"]`)
      ?.click();
    const keys = Array.from(shadow(element).querySelectorAll("[data-capability]")).map((node) =>
      node.getAttribute("data-capability"),
    );
    expect(keys).toEqual(["auto_price", "current_limit", "load_balancing", "target_soc"]);
    expect(shadow(element).textContent).not.toMatch(/explicit schedule/i);
  });
});

describe("the header buttons centre their glyphs", () => {
  it("draws Info and the cog as the same kind of icon, and centres it by the box", async () => {
    const { element } = await mounted();
    const buttons = Array.from(shadow(element).querySelectorAll<HTMLButtonElement>(".spotnav-header > button"));
    expect(buttons).toHaveLength(2);
    for (const button of buttons) {
      // One symmetrical icon each, and no text glyph whose side bearings would push it off-centre.
      expect(Array.from(button.children).map((node) => node.tagName.toLowerCase())).toEqual(["svg"]);
      expect(button.textContent).toBe("");
      expect(button.querySelector("svg")?.getAttribute("viewBox")).toBe("0 0 24 24");
    }
    const rule = VISUAL_STYLES.match(/\.spotnav-icon-button \{([^}]*)\}/s)?.[1] ?? "";
    expect(rule).toContain("display: inline-flex");
    expect(rule).toContain("align-items: center");
    expect(rule).toContain("justify-content: center");
    expect(rule).toContain("padding: 0");
  });
});
