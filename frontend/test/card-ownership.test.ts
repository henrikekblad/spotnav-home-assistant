// The card's snapshot and lifecycle ownership: no work while disconnected, exactly one cycle per
// connected life however many `hass` state snapshots arrive, and nothing that can be resurrected
// by assigning config or `hass` afterwards.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import { charger, dashboard, defineElements, FakeHass, mountCard } from "./helpers";

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";
const config = (charger: string) => ({ type: "custom:spotnav-card", charger });

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

describe("before it is in the document", () => {
  it("waits: config and hass alone start no request and no timer", async () => {
    const hass = new FakeHass();
    defineElements();
    const element = document.createElement("spotnav-card") as HTMLElement;
    (element as unknown as { setConfig(c: unknown): void }).setConfig(config("entry_a"));
    (element as unknown as { hass: unknown }).hass = hass;
    await settle();

    expect(hass.messages).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);

    document.body.appendChild(element);
    await settle();
    expect(hass.messages).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(1);
  });

  it("works from either order of the two assignments", async () => {
    const hass = new FakeHass();
    defineElements();
    const element = document.createElement("spotnav-card") as HTMLElement;
    (element as unknown as { hass: unknown }).hass = hass;
    (element as unknown as { setConfig(c: unknown): void }).setConfig(config("entry_a"));
    document.body.appendChild(element);
    await settle();
    expect(hass.messages).toHaveLength(1);
  });
});

describe("while it is connected", () => {
  it("refreshes immediately on a config change, exactly once", async () => {
    const hass = new FakeHass();
    const element = mountCard(config("entry_a"), hass);
    await settle();
    expect(hass.messages).toHaveLength(1);

    (element as unknown as { setConfig(c: unknown): void }).setConfig(config("entry_b"));
    await settle();
    expect(hass.messages.map((message) => message.charger_id)).toEqual(["entry_a", "entry_b"]);
  });

  it("does nothing more when fresh state snapshots keep arriving", async () => {
    const hass = new FakeHass();
    const element = mountCard(config("entry_a"), hass.snapshot("initial"));
    await settle();
    hass.resolveNext(dashboard());
    await settle();

    for (const snapshot of hass.snapshots(20, "update")) {
      element.hass = snapshot;
    }
    await settle();
    expect(hass.messages).toHaveLength(1);
    expect(text(element)).toContain("Garage");
  });

  it("keeps a pending answer through 20 distinct state snapshots, and renders it", async () => {
    const hass = new FakeHass();
    const first = hass.snapshot("first");
    const element = mountCard(config("entry_a"), first);
    await settle();
    expect(hass.messages).toHaveLength(1);

    const later = hass.snapshots(20, "update");
    for (const snapshot of later) {
      element.hass = snapshot;
    }
    await settle();
    // Home Assistant hands over a new object per state update: not one of those is a reason to
    // cancel the request, start another one, or clear what the card is showing.
    expect(hass.messages).toHaveLength(1);
    expect(hass.callers).toEqual([first]);

    hass.resolveAt(0, dashboard());
    await settle();
    expect(text(element)).toContain("Garage");
  });

  it("asks the next timer tick through the latest snapshot", async () => {
    const hass = new FakeHass();
    const element = mountCard(config("entry_a"), hass.snapshot("first"));
    await settle();
    hass.resolveNext(dashboard());
    await settle();

    const latest = hass.snapshot("latest");
    element.hass = latest;
    await settle();
    expect(hass.messages).toHaveLength(1); // the assignment itself is not a request

    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    expect(hass.messages).toHaveLength(2);
    expect(hass.callers[1]).toBe(latest);

    hass.resolveNext(dashboard({ charger: charger("entry_a", "Second") }));
    await settle();
    expect(text(element)).toContain("Second");
  });

  it("still invalidates a late answer on a config change, snapshots or not", async () => {
    const hass = new FakeHass();
    const element = mountCard(config("entry_a"), hass.snapshot("first"));
    await settle();

    for (const snapshot of hass.snapshots(5, "update")) {
      element.hass = snapshot;
    }
    (element as unknown as { setConfig(c: unknown): void }).setConfig(config("entry_b"));
    await settle();
    expect(hass.messages.map((message) => message.charger_id)).toEqual(["entry_a", "entry_b"]);

    hass.resolveAt(0, dashboard({ charger: charger("entry_a", "Stale") }));
    await settle();
    expect(text(element)).not.toContain("Stale");
  });
});

describe("after it is disconnected", () => {
  it("cannot be resurrected by assigning config or hass", async () => {
    const hass = new FakeHass();
    const element = mountCard(config("entry_a"), hass);
    await settle();
    expect(vi.getTimerCount()).toBe(1);

    element.remove();
    expect(vi.getTimerCount()).toBe(0);

    (element as unknown as { setConfig(c: unknown): void }).setConfig(config("entry_b"));
    for (const snapshot of hass.snapshots(20, "after-disconnect")) {
      element.hass = snapshot;
    }
    await settle();
    await vi.advanceTimersByTimeAsync(10 * 60_000);

    expect(hass.messages).toHaveLength(1); // nothing new, ever
    expect(vi.getTimerCount()).toBe(0);
  });

  it("starts exactly one fresh cycle when it is connected again", async () => {
    const hass = new FakeHass();
    const element = mountCard(config("entry_a"), hass);
    await settle();
    hass.resolveNext(dashboard());
    await settle();

    element.remove();
    document.body.appendChild(element);
    await settle();
    expect(hass.messages).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(1);

    hass.resolveNext(dashboard({ charger: charger("entry_a", "Fresh") }));
    await settle();
    expect(text(element)).toContain("Fresh");
  });
});
