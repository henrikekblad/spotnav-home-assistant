// The charge bar: Home Assistant's `progress` block drawn under the status line, its words in five
// languages, the connection line it replaces, and the reads after a Start or Stop.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chargeBarFor, outcomeSettled, outcomeWatchFor, OUTCOME_POLL_MS, OUTCOME_WINDOW_MS } from "../src/charge-bar";
import { createCardView, type CardViewInput } from "../src/card-view";
import { REFRESH_INTERVAL_MS } from "../src/card";
import type { FormatContext } from "../src/format";
import type { Language } from "../src/i18n";
import { buildModel, type CardModel } from "../src/model";
import { decodeDashboard, type ChargeBarBlock } from "../src/validate";
import { VISUAL_CLASSES, VISUAL_STYLES } from "../src/visual-styles";
import { decoded, rawDashboard } from "./dashboard-fixtures";
import { FakeHass, mountCard } from "./helpers";
import { ACTION_API_VERSION } from "../src/types";
import { mountPoint } from "./visual-fixtures";

const NOW = Date.parse("2026-10-06T10:00:00Z");

function block(overrides: Partial<Record<keyof ChargeBarBlock | "ends_at" | "started_at", unknown>> = {}): Record<string, unknown> {
  return {
    basis: "vehicle_limit",
    percent: 62,
    ends_at: "2026-10-06T12:35:00+00:00",
    power_kw: 11.04,
    power_source: "measured",
    moving: true,
    start_soc_percent: 41.0,
    started_at: "2026-10-06T09:00:00+00:00",
    delivered_kwh: 8.4,
    ...overrides,
  };
}

function charging(progress: unknown, extra: Record<string, unknown> = {}): Record<string, unknown> {
  const raw = rawDashboard(extra);
  (raw["live"] as Record<string, unknown>)["charging"] = true;
  raw["connection"] = { state: "charging", source: null };
  raw["progress"] = progress;
  return raw;
}

function format(language: Language, timeZone = "Europe/Stockholm"): FormatContext {
  return { language, timeZone, unit: "öre", currency: "SEK", majorUnit: "kr" };
}

function decodedBlock(raw: Record<string, unknown>): ChargeBarBlock | null {
  const result = decodeDashboard(charging(raw));
  if (!result.ok) {
    throw new Error(result.failure);
  }
  return result.value.progress;
}

function line(raw: Record<string, unknown>, language: Language, nowMs = NOW, timeZone?: string): string | undefined {
  return chargeBarFor(decodedBlock(raw), format(language, timeZone), nowMs)?.text;
}

describe("the progress block is read leniently", () => {
  it("decodes every field", () => {
    const decodedProgress = decodedBlock(block());
    expect(decodedProgress).toMatchObject({ basis: "vehicle_limit", percent: 62, moving: true, power_kw: 11.04 });
    expect(decodedProgress?.ends_at_ms).toBe(Date.parse("2026-10-06T12:35:00Z"));
  });

  it("is null when missing (an older backend), null, or unreadable, and never refuses the dashboard", () => {
    const raw = rawDashboard();
    delete raw["progress"];
    const older = decodeDashboard(raw);
    expect(older.ok && older.value.progress).toBe(null);
    expect(decodeDashboard(charging(null)).ok).toBe(true);
    for (const broken of [block({ basis: "sideways" }), block({ percent: 140 }), block({ moving: "yes" }), "bar"]) {
      const result = decodeDashboard(charging(broken));
      expect(result.ok).toBe(true);
      expect(result.ok && result.value.progress).toBe(null);
    }
  });

  it("keeps a key it does not know", () => {
    expect(decodedBlock(block({ eta_confidence: "high" } as never))?.percent).toBe(62);
  });
});

describe("the words under the bar", () => {
  it("says the share and the end in five languages", () => {
    expect(line(block(), "sv")).toBe("62 % klart · klart ca 14:35");
    expect(line(block(), "en")).toBe("62 % done · done about 14:35");
    expect(line(block(), "nb")).toBe("62 % ferdig · ferdig ca. 14:35");
    expect(line(block(), "da")).toBe("62 % færdig · færdig ca. 14.35");
    expect(line(block(), "fi")).toBe("62 % valmis · valmis noin klo 14.35");
  });

  it("counts a target as a share of the target", () => {
    expect(line(block({ basis: "target", percent: 93 }), "sv")).toBe("93 % av målet · klart ca 14:35");
    expect(line(block({ basis: "target", percent: 93, ends_at: null }), "en")).toBe("93 % of target");
  });

  it("says the open bar's power, a whole number without a decimal", () => {
    const open = block({ basis: "open", percent: null, ends_at: null, power_kw: 11.04 });
    expect(line(open, "sv")).toBe("Laddar · 11 kW");
    expect(line(block({ basis: "open", percent: null, ends_at: null, power_kw: 3.68 }), "sv")).toBe("Laddar · 3,7 kW");
    expect(line(block({ basis: "open", percent: null, ends_at: null, power_kw: 3.68 }), "en")).toBe("Charging · 3.7 kW");
    expect(line(block({ basis: "open", percent: null, ends_at: null, power_kw: null }), "fi")).toBe("Ladataan");
  });

  it("names the weekday when the end falls on another day", () => {
    const late = block({ ends_at: "2026-10-06T23:30:00+00:00" });
    expect(line(late, "en", Date.parse("2026-10-06T21:00:00Z"))).toMatch(/^62 % done · done about Wed.* 01:30$/);
  });

  it("follows the clock changes in the market's zone", () => {
    // 01:30Z on 25 October is 02:30 CET, after the clocks went back at 03:00 CEST.
    expect(line(block({ ends_at: "2026-10-25T01:30:00+00:00" }), "sv", Date.parse("2026-10-25T00:00:00Z"))).toBe(
      "62 % klart · klart ca 02:30",
    );
    // 01:15Z on 29 March is 03:15 CEST, past the skipped hour.
    expect(line(block({ ends_at: "2026-03-29T01:15:00+00:00" }), "sv", Date.parse("2026-03-29T00:30:00Z"))).toBe(
      "62 % klart · klart ca 03:15",
    );
    expect(line(block(), "en", NOW, "UTC")).toBe("62 % done · done about 12:35");
  });

  it("states no end without a market zone", () => {
    expect(line(block(), "en", NOW, "")).toBe("62 % done");
  });

  it("is no bar without a block", () => {
    expect(chargeBarFor(null, format("en"), NOW)).toBeNull();
  });
});

function view(model: CardModel): ShadowRoot {
  const mount = mountPoint("bar");
  const input: CardViewInput = {
    model,
    mount: mount.root,
    idPrefix: "bar",
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
  };
  createCardView(input);
  return mount.root;
}

function modelOf(raw: Record<string, unknown>, language: Language = "sv"): CardModel {
  const result = decodeDashboard(raw);
  if (!result.ok) {
    throw new Error(result.failure);
  }
  return buildModel({ dashboard: result.value, language, nowMs: NOW });
}

describe("the bar in the card", () => {
  it("sits under the status line as a progress bar with its line", () => {
    const root = view(modelOf(charging(block())));
    const bar = root.querySelector(`.${VISUAL_CLASSES.chargeBar}`) as HTMLElement;
    expect(bar).not.toBeNull();
    const status = root.querySelector(`.${VISUAL_CLASSES.status}`);
    expect(status?.nextElementSibling).toBe(bar);
    const track = bar.querySelector('[role="progressbar"]') as HTMLElement;
    expect(track.getAttribute("aria-valuenow")).toBe("62");
    expect(track.getAttribute("aria-valuemin")).toBe("0");
    expect(track.getAttribute("aria-valuemax")).toBe("100");
    expect(track.getAttribute("aria-label")).toBe("Laddningens förlopp");
    expect(track.getAttribute("aria-valuetext")).toBe("62 % klart · klart ca 14:35");
    expect((bar.querySelector(`.${VISUAL_CLASSES.chargeBarFill}`) as HTMLElement).style.width).toBe("62%");
    expect(bar.dataset["moving"]).toBe("true");
    expect(bar.querySelector(`.${VISUAL_CLASSES.chargeBarLine}`)?.textContent).toBe("62 % klart · klart ca 14:35");
  });

  it("stands still while paused, and the open bar has no value", () => {
    const still = view(modelOf(charging(block({ moving: false, ends_at: null }))));
    expect((still.querySelector(`.${VISUAL_CLASSES.chargeBar}`) as HTMLElement).dataset["moving"]).toBe("false");
    const open = view(modelOf(charging(block({ basis: "open", percent: null, ends_at: null }))));
    const bar = open.querySelector(`.${VISUAL_CLASSES.chargeBar}`) as HTMLElement;
    expect(bar.dataset["basis"]).toBe("open");
    expect(bar.querySelector('[role="progressbar"]')?.hasAttribute("aria-valuenow")).toBe(false);
  });

  it("hides the connection line while the bar says it charges, and shows it otherwise", () => {
    const withBar = view(modelOf(charging(block())));
    expect(withBar.querySelector(`.${VISUAL_CLASSES.connectionLine}`)).toBeNull();
    const withoutBar = view(modelOf(charging(null)));
    expect(withoutBar.querySelector(`.${VISUAL_CLASSES.connectionLine}`)?.textContent).toContain("Laddar");
    expect(withoutBar.querySelector(`.${VISUAL_CLASSES.chargeBar}`)).toBeNull();
  });

  it("hides it beside the vehicle line too", () => {
    const soc = {
      value: 50, age_s: 60, source: "vehicle", estimated: false, target_percent: null, need_kwh: null,
      capacity_kwh: 77, vehicle_name: "EV", vehicle_id: "ev", vehicle_max_percent: 80, room_kwh: 25.7,
      efficiency: 0.9, vehicles: [], missing: [],
    };
    const root = view(modelOf(charging(block(), { soc })));
    expect(root.querySelector(`.${VISUAL_CLASSES.vehicleLine}`)).not.toBeNull();
    expect(root.querySelector(`.${VISUAL_CLASSES.connectionLine}`)).toBeNull();
  });

  it("styles a slim track, an accent fill, stripes that drift only while moving, and a gliding open bar", () => {
    expect(VISUAL_STYLES).toMatch(/\.spotnav-charge-bar-track \{[^}]*height: 0.375rem/s);
    expect(VISUAL_STYLES).toContain('.spotnav-charge-bar[data-moving="true"]');
    expect(VISUAL_STYLES).toContain("@keyframes spotnav-charge-bar-drift");
    expect(VISUAL_STYLES).toContain("@keyframes spotnav-charge-bar-glide");
    expect(VISUAL_STYLES).toMatch(/prefers-reduced-motion: reduce\)[^@]*animation: none/s);
  });
});

describe("after a Start or Stop the card reads every five seconds until the charge follows", () => {
  it("watches a Start for charging and a Stop for no charging, for two minutes", () => {
    expect(outcomeWatchFor("start", 0)).toEqual({ charging: true, untilMs: OUTCOME_WINDOW_MS });
    expect(outcomeWatchFor("stop", 0)).toEqual({ charging: false, untilMs: OUTCOME_WINDOW_MS });
    expect(outcomeWatchFor("resume", 0)).toBeNull();
    const watch = { charging: true, untilMs: OUTCOME_WINDOW_MS };
    expect(outcomeSettled(watch, decoded(), 1000)).toBe(false);
    expect(outcomeSettled(watch, decodedCharging(), 1000)).toBe(true);
    expect(outcomeSettled(watch, decoded(), OUTCOME_WINDOW_MS)).toBe(true);
    expect(OUTCOME_POLL_MS).toBe(5_000);
  });
});

function decodedCharging() {
  const result = decodeDashboard(charging(null));
  if (!result.ok) {
    throw new Error(result.failure);
  }
  return result.value;
}

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

const started = (action: string) => ({ api_version: ACTION_API_VERSION, ok: true, error: null, action, choice: null });

describe("the card's reads after a Start or Stop", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const readsOf = (hass: FakeHass): number =>
    hass.messages.filter((message) => message["type"] === "spotnav/get_dashboard").length;

  async function clicked(payload: Record<string, unknown>, action: string): Promise<FakeHass> {
    const hass = new FakeHass();
    const element = mountCard(CONFIG, hass);
    element.hass = hass.snapshot("snapshot", "en");
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(payload);
    await vi.advanceTimersByTimeAsync(0);
    element.shadowRoot?.querySelector<HTMLButtonElement>(".spotnav-action-button")?.click();
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(started(action));
    await vi.advanceTimersByTimeAsync(0);
    return hass;
  }

  it("reads every 5 s until the Start shows, then every 30 s again", async () => {
    const idle = fixture("start_idle");
    const hass = await clicked(idle, "start");
    // The confirmation read: the charger has not started yet.
    hass.resolveNext(idle);
    await vi.advanceTimersByTimeAsync(0);
    const confirmed = readsOf(hass);
    await vi.advanceTimersByTimeAsync(OUTCOME_POLL_MS);
    expect(readsOf(hass)).toBe(confirmed + 1);
    hass.resolveNext(idle);
    await vi.advanceTimersByTimeAsync(OUTCOME_POLL_MS);
    expect(readsOf(hass)).toBe(confirmed + 2);
    // Now the charge shows: back to the ordinary cycle, nothing more until the 30 s timer.
    hass.resolveNext(fixture("stop_charging"));
    await vi.advanceTimersByTimeAsync(OUTCOME_POLL_MS * 3);
    expect(readsOf(hass)).toBe(confirmed + 2);
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    expect(readsOf(hass)).toBeGreaterThan(confirmed + 2);
  });

  it("reads no faster once the confirmation already shows the outcome", async () => {
    const hass = await clicked(fixture("start_idle"), "start");
    hass.resolveNext(fixture("stop_charging"));
    await vi.advanceTimersByTimeAsync(0);
    const confirmed = readsOf(hass);
    await vi.advanceTimersByTimeAsync(OUTCOME_POLL_MS * 3);
    expect(readsOf(hass)).toBe(confirmed);
  });

  it("gives up on a Stop the charger never follows after two minutes", async () => {
    const charging = fixture("stop_charging");
    const hass = await clicked(charging, "stop");
    hass.resolveNext(charging);
    await vi.advanceTimersByTimeAsync(0);
    const answer = (): void => {
      while (hass.outstanding > 0) {
        hass.resolveNext(charging);
      }
    };
    const start = readsOf(hass);
    for (let elapsed = 0; elapsed < OUTCOME_WINDOW_MS; elapsed += OUTCOME_POLL_MS) {
      await vi.advanceTimersByTimeAsync(OUTCOME_POLL_MS);
      answer();
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(readsOf(hass) - start).toBeGreaterThanOrEqual(OUTCOME_WINDOW_MS / OUTCOME_POLL_MS - 2);
    const settled = readsOf(hass);
    await vi.advanceTimersByTimeAsync(OUTCOME_POLL_MS * 4);
    answer();
    expect(readsOf(hass) - settled).toBeLessThanOrEqual(1);
  });
});
