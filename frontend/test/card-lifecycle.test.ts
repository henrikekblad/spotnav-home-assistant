// Refresh ownership, races and timers -- all deterministic: deferred promises and fake timers,
// no sleeps. Every case here is one of the rules the card documents.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import { translate } from "../src/i18n";
import { charger, dashboard, planOf, pricesOf, FakeHass, mountCard } from "./helpers";
import { plannedStatus, quarterHourRows } from "./dashboard-fixtures";
import { AFTER_MIDNIGHT_MS } from "./visual-fixtures";

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";
const configFor = (chargerId: string) => ({ type: "custom:spotnav-card", charger: chargerId });

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("when a request happens", () => {
  it("asks once on connect, and once per bounded interval after that", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    // setConfig, the first `hass` assignment and connectedCallback all ask; they coalesce.
    expect(hass.messages).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(1);

    hass.resolveNext(dashboard());
    await settle();
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    expect(hass.messages).toHaveLength(2);

    hass.resolveNext(dashboard());
    await settle();
    // Two more ticks while the answer is still outstanding: the second tick coalesces into the
    // attempt the first one opened, so a slow backend cannot turn ticks into a queue.
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS * 2);
    expect(hass.messages).toHaveLength(3);
    hass.resolveNext(dashboard());
    await settle();
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    expect(hass.messages).toHaveLength(4);
  });

  it("does not storm when Home Assistant keeps assigning state", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    hass.resolveNext(dashboard());
    await settle();

    for (let i = 0; i < 20; i += 1) {
      element.hass = hass;
    }
    await settle();
    expect(hass.messages).toHaveLength(1);
  });

  it("coalesces repeated retry clicks into the one forced attempt", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    hass.rejectNext(new Error("Connection lost"));
    await settle();

    const retry = shadow(element).querySelector("button") as HTMLButtonElement;
    retry.click();
    retry.click();
    retry.click();
    await settle();
    expect(hass.messages).toHaveLength(2);
  });
});

describe("late answers", () => {
  it("never paints charger A's answer onto charger B after a config change", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    expect(hass.messages.map((message) => message.charger_id)).toEqual(["entry_a"]);

    element.setConfig(configFor("entry_b"));
    await settle();
    expect(hass.messages.map((message) => message.charger_id)).toEqual(["entry_a", "entry_b"]);

    hass.resolveAt(0, dashboard()); // A answers late, after the config moved on
    await settle();
    expect(text(element)).not.toContain("Garage");
    expect(text(element)).toContain(translate("en", "state.loading"));

    hass.resolveAt(0, dashboard({ charger: charger("entry_b", "Carport") }));
    await settle();
    expect(text(element)).toContain("Carport");
  });

  it("lets a newer attempt win over an older one for the same charger", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    element.setConfig(configFor("entry_a"));
    await settle();
    expect(hass.messages).toHaveLength(2);

    const base = dashboard();
    const newer = dashboard({
      plan: { ...planOf(base), proposal: { ...(planOf(base).proposal as Record<string, unknown>), planned_kwh: 33 } },
      status: plannedStatus(33),
    });
    const older = dashboard({
      plan: { ...planOf(base), proposal: { ...(planOf(base).proposal as Record<string, unknown>), planned_kwh: 11 } },
      status: plannedStatus(11),
    });
    hass.resolveAt(1, newer);
    await settle();
    expect(text(element)).toContain("33 kWh");

    hass.resolveAt(0, older); // the older request finishes last, and is ignored
    await settle();
    expect(text(element)).toContain("33 kWh");
    expect(text(element)).not.toContain("older");
  });
});

describe("a capture that spans local midnight", () => {
  it("cannot let an answer accepted before midnight restore the old dates' roles", async () => {
    const hass = new FakeHass();
    // The clock is on the new date, eleven minutes in: the instant the production card showed unknown.
    vi.setSystemTime(AFTER_MIDNIGHT_MS);
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    element.setConfig(configFor("entry_a"));
    await settle();
    expect(hass.messages).toHaveLength(2);

    const base = dashboard();
    // What a pre-midnight read (or a plan horizon calculated before midnight) published: the date the
    // clock has since left, and the date it is in.
    const beforeMidnight = dashboard({
      prices: {
        ...pricesOf(base),
        intervals: [
          ...quarterHourRows("2026-09-22T21:45:00Z", [100], { day: "2026-09-22" }),
          ...quarterHourRows("2026-09-22T22:00:00Z", [200, 300, 400, 500], { day: "2026-09-23" }),
        ],
      },
    });
    // What the backend publishes after midnight: the date the clock is in, and the one after it.
    const afterMidnight = dashboard({
      prices: {
        ...pricesOf(base),
        intervals: [
          ...quarterHourRows("2026-09-22T22:00:00Z", [200, 300, 400, 500], { day: "2026-09-23" }),
          ...quarterHourRows("2026-09-23T22:00:00Z", [600, 700], { day: "2026-09-24" }),
        ],
      },
    });

    hass.resolveAt(1, afterMidnight);
    await settle();
    expect(text(element)).toContain("Now 200 öre/kWh");
    expect(text(element)).toContain(translate("en", "graph.legend.today"));
    expect(text(element)).toContain(translate("en", "graph.legend.tomorrow"));
    expect(text(element)).not.toContain(translate("en", "graph.legend.past"));

    // The older answer finishes last. The accepted post-midnight capture stays, and the expired date's
    // own series does not come back with it.
    hass.resolveAt(0, beforeMidnight);
    await settle();
    expect(text(element)).toContain("Now 200 öre/kWh");
    expect(text(element)).toContain(translate("en", "graph.legend.tomorrow"));
    expect(text(element)).not.toContain(translate("en", "graph.legend.past"));

    // The positive control: the very same pre-midnight capture, accepted on its own, does draw the
    // expired date's series -- so the absence above is the old answer being inert, not a marker that
    // never appears at all.
    const control = new FakeHass();
    const other = mountCard(configFor("entry_a"), control);
    await settle();
    control.resolveNext(beforeMidnight);
    await settle();
    expect(text(other)).toContain(translate("en", "graph.legend.past"));
    expect(text(other)).toContain("Now 200 öre/kWh");

    element.remove();
    other.remove();
  });
});

describe("disconnect", () => {
  it("is final: no timer, no later render, and no timer comes back", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    expect(vi.getTimerCount()).toBe(1);

    element.remove();
    expect(vi.getTimerCount()).toBe(0);

    hass.resolveNext(dashboard());
    await settle();
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS * 5);
    expect(hass.messages).toHaveLength(1);
    expect(text(element)).toContain(translate("en", "state.loading"));
    expect(vi.getTimerCount()).toBe(0);
  });

  it("starts a fresh cycle when it is added back", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    await settle();
    hass.resolveNext(dashboard());
    await settle();

    element.remove();
    document.body.appendChild(element);
    await settle();
    expect(hass.messages).toHaveLength(2);
    hass.resolveNext(dashboard({ charger: charger("entry_a", "Garage v2") }));
    await settle();
    expect(text(element)).toContain("Garage v2"); // a fresh cycle, rendered
  });
});

describe("two cards on one dashboard", () => {
  it("uses its own charger id and never exchanges data", async () => {
    const hass = new FakeHass();
    const first = mountCard(configFor("entry_a"), hass);
    const second = mountCard(configFor("entry_b"), hass);
    await settle();
    expect(hass.messages.map((message) => message.charger_id).sort()).toEqual([
      "entry_a",
      "entry_b",
    ]);

    hass.resolveAt(1, dashboard({ charger: charger("entry_b", "Carport") }));
    await settle();
    expect(text(second)).toContain("Carport");
    expect(text(first)).toContain(translate("en", "state.loading"));

    hass.resolveAt(0, dashboard({ charger: charger("entry_a", "Garage") }));
    await settle();
    expect(text(first)).toContain("Garage");
    expect(text(second)).toContain("Carport");
  });
});
