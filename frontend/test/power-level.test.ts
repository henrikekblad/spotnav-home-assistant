// The power level (the dashboard's `grid_tariff.demand.power_level` and `.level_count`): decoded strictly, its row
// with the exceedances beneath, its editor and the write it sends, the running hour against it, and the release
// dialog that says which exceedance a release would be and asks twice at the last one allowed.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { demandRows, hourLine } from "../src/demand-charge";
import { storedBody, withPowerLevel } from "../src/grid-tariff";
import { releaseConfirm } from "../src/status";
import { decodeDashboard, decodeDemand, decodeGridTariff, type StatusLine } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const PILOT = read("grid_tariff", "pilot_demand.json");
const GBG = { days: "weekdays", from_month: 11, to_month: 3, from_hour: 7, to_hour: 20 };
const LEVEL = { level_kw: 10, window: GBG, outside: true };
const COUNT = {
  used: 1,
  allowed: 3,
  exceeded: ["2026-10-03T10:00:00+00:00"],
  counts_now: true,
  next_counted_start: "2026-11-02T13:00:00+00:00",
};
const DEMAND = { ...PILOT["demand"], power_level: LEVEL, level_count: COUNT };
const FORMAT = { language: "sv" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };
const NOW = Date.parse("2026-11-02T13:20:00Z");

function held(peak: number | null, until: string | null = null): StatusLine {
  return {
    code: "import_target_held",
    params: { limit_a: 7, target_kw: 6, hour_kw: 5.1, set_kw: 6, releasable: true, release_until: until, release_peak_kw: peak },
  } as unknown as StatusLine;
}

describe("the power level in the demand block", () => {
  it("is read exactly, and a level that is not one drops the tariff block, not the dashboard", () => {
    expect(decodeDemand(DEMAND).power_level).toEqual(LEVEL);
    expect(decodeDemand(DEMAND).level_count).toEqual(COUNT);
    expect(decodeDemand({ ...DEMAND, level_count: null }).level_count).toBeNull();
    // A backend before it: neither key.
    expect("power_level" in decodeDemand(PILOT["demand"])).toBe(true);
    const { power_level: _l, level_count: _c, ...older } = DEMAND;
    expect("power_level" in decodeDemand(older)).toBe(false);
    const dashboard = read("dashboard", "cheapest_direct_site_admin.json");
    dashboard["price_source"] = { entry_id: "price_a", title: "x", charger_count: 1, choices: [], writable: true, duplicate: null };
    for (const demand of [
      { ...DEMAND, power_level: { ...LEVEL, level_kw: 0 } },
      { ...DEMAND, power_level: { ...LEVEL, window: null } },
      { ...DEMAND, power_level: { ...LEVEL, extra: 1 } },
      { ...DEMAND, level_count: { ...COUNT, used: -1 } },
      { ...older, level_count: COUNT },
    ]) {
      dashboard["grid_tariff"] = { ...PILOT, demand };
      const decoded = decodeDashboard(dashboard);
      expect(decoded.ok && decoded.value.grid_tariff, JSON.stringify(demand)).toBeNull();
    }
  });

  it("shows the level with its exceedances beneath, and offers one only where the backend states it", () => {
    const rows = demandRows(FORMAT, decodeDemand(DEMAND));
    const level = rows.find((row) => row.key === "level")!;
    expect([level.label, level.value]).toEqual(["Effektnivå", "10,0 kW · Utanför vardagar 07–20, nov–mar"]);
    expect(level.lines).toEqual(["Överskridanden: 1 av 3 de senaste 12 månaderna"]);
    expect(rows.some((row) => row.key === "add_level")).toBe(false);
    const english = demandRows({ ...FORMAT, language: "en" }, decodeDemand(DEMAND)).find((row) => row.key === "level")!;
    expect(english.lines).toEqual(["Exceedances: 1 of 3 in the last 12 months"]);
    const { power_level: _l, level_count: _c, ...older } = DEMAND;
    expect(demandRows(FORMAT, decodeDemand(older)).some((row) => String(row.key).includes("level"))).toBe(false);
  });

  it("writes the level with the tariff, and never to a backend that does not state it", () => {
    const block = decodeGridTariff({ ...PILOT, demand: DEMAND });
    expect(storedBody(block).power_level).toEqual(LEVEL);
    expect(withPowerLevel(block, null).power_level).toBeNull();
    const { power_level: _l, level_count: _c, ...older } = DEMAND;
    expect("power_level" in storedBody(decodeGridTariff({ ...PILOT, demand: older }))).toBe(false);
  });

  it("words the running hour against the level where it holds the hour below the target", () => {
    expect(hourLine("sv", decodeDemand({ ...DEMAND, effective_target_kw: 5 }))).toBe(
      "Den här timmen: 3,1 av 5,0 kW – effektnivån (effektmål 6,0 kW)",
    );
  });
});

describe("a release that would exceed the power level", () => {
  const status = (line: StatusLine) => ({ tone: "normal", lines: [line] }) as never;

  it("says which exceedance it would be, and asks twice at the last one allowed", () => {
    const demand = decodeDemand(DEMAND);
    const second = releaseConfirm(status(held(11)), FORMAT, NOW, demand)!;
    expect(second.text).toContain("Det blir överskridande 2 av 3 av effektnivån (10,0 kW) på 12 månader.");
    expect(second.askTwice).toBe(false);
    const last = releaseConfirm(status(held(11)), FORMAT, NOW, decodeDemand({ ...DEMAND, level_count: { ...COUNT, used: 2 } }))!;
    expect(last.text).toContain("överskridande 3 av 3");
    expect(last.text).toContain("Det är det sista som tillåts");
    expect(last.askTwice).toBe(true);
  });

  it("says nothing of the level when the release stays under it or ends before its hours", () => {
    const demand = decodeDemand(DEMAND);
    expect(releaseConfirm(status(held(9)), FORMAT, NOW, demand)!.text).not.toContain("effektnivå");
    const later = decodeDemand({ ...DEMAND, level_count: { ...COUNT, counts_now: false, next_counted_start: "2026-11-02T19:00:00+00:00" } });
    expect(releaseConfirm(status(held(11, "2026-11-02T18:00:00+00:00")), FORMAT, NOW, later)!.text).not.toContain("effektnivå");
    expect(releaseConfirm(status(held(11, "2026-11-02T20:00:00+00:00")), FORMAT, NOW, later)!.text).toContain("effektnivå");
    expect(releaseConfirm(status(held(11)), FORMAT, NOW, null)!.askTwice).toBe(false);
  });
});

// ------------------------------------------------------------------------------------------------ in the card

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  document.body.replaceChildren();
});

function dashboardWith(demand: Record<string, unknown>, lines: StatusLine[] = []): Record<string, unknown> {
  const payload = read("dashboard", "cheapest_direct_site_admin.json");
  payload["price_source"] = { entry_id: "price_a", title: "Electricity price SE4", charger_count: 1, choices: [], writable: true, duplicate: null };
  payload["grid_tariff"] = { ...PILOT, demand };
  if (lines.length > 0) {
    payload["status"] = { tone: "normal", lines };
  }
  return payload;
}

describe("in the card", () => {
  it("asks twice before a release that is the power level's last allowed exceedance", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    element.hass = hass.snapshot("snapshot", "en");
    await settle();
    hass.resolveNext(dashboardWith({ ...DEMAND, level_count: { ...COUNT, used: 2 } }, [held(11)]));
    await settle();
    const root = shadow(element);
    root.querySelector<HTMLButtonElement>("[data-part='release-import-target'] button")!.click();
    await settle();
    const release = root.querySelector<HTMLButtonElement>("[data-action='release']")!;
    const sent = (): number => hass.messages.filter((message) => message["type"] === "spotnav/release_import_target").length;
    release.click();
    await settle();
    expect(sent()).toBe(0);
    expect(release.textContent).toBe("Yes, exceed it");
    release.click();
    await settle();
    expect(sent()).toBe(1);
  });

  it("adds a power level outside a part's window in one write", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    const snapshot = hass.snapshot("snapshot", "en");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await settle();
    hass.resolveNext(dashboardWith({ ...DEMAND, power_level: null, level_count: null }));
    await settle();
    const root = shadow(element);
    root.querySelector<HTMLButtonElement>(`[aria-label="Settings"]`)?.click();
    await settle();
    const dialog = (): HTMLElement =>
      [...root.querySelectorAll<HTMLElement>("[role='dialog']")].find((node) => node.closest("[hidden]") === null)!;
    dialog().querySelector<HTMLButtonElement>("[data-row='demand_add_level'] button")!.click();
    await settle();
    const editor = dialog();
    expect(editor.querySelector("[data-value-editor='level']")).not.toBeNull();
    // The pilot's day part's window, the level outside it.
    expect(editor.querySelector<HTMLSelectElement>("[data-level='counted']")!.value).toBe("outside");
    editor.querySelector<HTMLInputElement>("[data-level='kw']")!.value = "0";
    editor.querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
    await settle();
    expect(hass.messages.filter((m) => m["type"] === "spotnav/update_grid_tariff")).toEqual([]);
    editor.querySelector<HTMLInputElement>("[data-level='kw']")!.value = "10";
    editor.querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
    await settle();
    const writes = hass.messages.filter((m) => m["type"] === "spotnav/update_grid_tariff");
    expect(writes.map((m) => (m["grid_tariff"] as Record<string, unknown>)["power_level"])).toEqual([
      { level_kw: 10, window: PILOT["demand"]["components"][0]["window"], outside: true },
    ]);
  });
});
