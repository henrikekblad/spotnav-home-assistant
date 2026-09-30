// The editor's async ownership, deterministically: deferred promises, fake timers, no sleeps.
//
// Every case is one of the rules the editor's header states: a load only while connected, a
// generation per attempt, an inert late completion, one load per connected life however many
// state snapshots arrive, and no timer at all.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFailure, charger, chargerList, defineElements, FakeHass, mountEditor } from "./helpers";
import { API_VERSION } from "../src/types";

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";
const options = (element: Element): string[] =>
  Array.from(shadow(element).querySelectorAll("option")).map((option) => option.textContent ?? "");
const config = { type: "custom:spotnav-card", charger: "" };
const LIST_MESSAGE = { type: "spotnav/list_chargers", api_version: API_VERSION };

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function events(element: Element): unknown[] {
  const seen: unknown[] = [];
  element.addEventListener("config-changed", (event) => {
    // Home Assistant reads `ev.detail.config`: the detail holds exactly that one key.
    const detail = (event as CustomEvent).detail as { config: unknown };
    expect(Object.keys(detail)).toEqual(["config"]);
    seen.push(detail.config);
  });
  return seen;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("one load per life", () => {
  it("starts nothing before it is in the document, then exactly one load", async () => {
    const hass = new FakeHass();
    defineElements();
    const element = document.createElement("spotnav-card-editor") as HTMLElement;
    (element as unknown as { hass: unknown }).hass = hass;
    (element as unknown as { setConfig(c: unknown): void }).setConfig(config);
    await settle();
    expect(hass.messages).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);

    document.body.appendChild(element);
    await settle();
    expect(hass.messages).toEqual([LIST_MESSAGE]);
  });

  it("creates no timer at all", async () => {
    const hass = new FakeHass();
    const element = mountEditor(config, hass);
    await settle();
    expect(vi.getTimerCount()).toBe(0);

    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(hass.messages).toHaveLength(1);
  });

  it("does not storm when fresh state snapshots keep arriving", async () => {
    const hass = new FakeHass();
    const element = mountEditor(config, hass.snapshot("initial"));
    for (const snapshot of hass.snapshots(20, "update")) {
      element.hass = snapshot;
    }
    await settle();
    expect(hass.messages).toHaveLength(1);
  });
});


describe("a late completion is inert", () => {
  it("ignores a list that arrives after the editor is disconnected, and emits nothing", async () => {
    const hass = new FakeHass();
    const element = mountEditor(config, hass);
    const seen = events(element);
    await settle();
    expect(hass.messages).toEqual([LIST_MESSAGE]);

    element.remove();
    hass.resolveAt(0, chargerList([charger("entry_a", "Garage")]));
    await settle();

    expect(seen).toEqual([]); // no auto-selection from a result nobody is waiting for
    expect(text(element)).not.toContain("Garage");
    expect(options(element)).toEqual(["Select a charger"]);
  });

  it("keeps a pending list through 20 distinct state snapshots, then renders and selects once", async () => {
    const first = new FakeHass();
    const initial = first.snapshot("initial");
    const element = mountEditor(config, initial);
    const seen = events(element);
    await settle();
    expect(first.messages).toEqual([LIST_MESSAGE]);

    for (const snapshot of first.snapshots(20, "update")) {
      element.hass = snapshot;
    }
    await settle();
    // A new object is a state snapshot, not a new connection: no second request, no invalidation.
    expect(first.messages).toEqual([LIST_MESSAGE]);
    expect(first.callers).toEqual([initial]);

    first.resolveAt(0, chargerList([charger("entry_a", "Garage")]));
    await settle();
    expect(options(element)).toEqual(["Select a charger", "Garage"]);
    expect(seen).toEqual([{ type: "custom:spotnav-card", charger: "entry_a" }]);
  });

  it("does not clear a valid loaded list when later snapshots arrive", async () => {
    const hass = new FakeHass();
    const element = mountEditor(config, hass.snapshot("initial"));
    hass.resolveNext(chargerList([charger("entry_a", "Garage")]));
    await settle();

    for (const snapshot of hass.snapshots(20, "update")) {
      element.hass = snapshot;
    }
    await settle();
    expect(hass.messages).toHaveLength(1);
    expect(options(element)).toEqual(["Select a charger", "Garage"]);
    expect(text(element)).toContain("Selected: entry_a");
  });

  it("starts a new generation on retry, and the failed attempt cannot come back", async () => {
    const hass = new FakeHass();
    const element = mountEditor(config, hass);
    await settle();
    hass.rejectNext(apiFailure("spotnav_http_error", "Connection lost"));
    await settle();
    expect(text(element)).toContain("Could not load the SpotNav chargers");

    const latest = hass.snapshot("latest");
    element.hass = latest;
    const retry = shadow(element).querySelector("button") as HTMLButtonElement;
    retry.click();
    await settle();
    expect(hass.messages).toHaveLength(2); // a retry is a new attempt
    expect(hass.callers[1]).toBe(latest); // through the newest snapshot, not the stale one
    expect(text(element)).not.toContain("Could not load");

    hass.resolveAt(0, chargerList([charger("entry_a", "Garage")]));
    await settle();
    expect(options(element)).toEqual(["Select a charger", "Garage"]);
    expect(shadow(element).querySelector("button")).toBeNull();
  });
});

describe("reconnecting", () => {
  it("loads once more when there is no valid list, through the latest snapshot", async () => {
    const hass = new FakeHass();
    const element = mountEditor(config, hass.snapshot("first"));
    await settle();

    element.remove();
    const latest = hass.snapshot("latest");
    element.hass = latest; // stored, and inert while disconnected
    expect(hass.messages).toHaveLength(1);
    document.body.appendChild(element);
    await settle();
    expect(hass.messages).toHaveLength(2); // one fresh attempt
    expect(hass.callers[1]).toBe(latest);

    hass.resolveAt(1, chargerList([charger("entry_a", "Garage")]));
    await settle();
    expect(options(element)).toEqual(["Select a charger", "Garage"]);
    expect(text(element)).toContain("Selected: entry_a");
  });

  it("loads nothing when a valid list is already held", async () => {
    const hass = new FakeHass();
    const element = mountEditor(config, hass);
    hass.resolveNext(chargerList([charger("entry_a", "Garage")]));
    await settle();
    expect(options(element)).toEqual(["Select a charger", "Garage"]);

    element.remove();
    document.body.appendChild(element);
    await settle();
    expect(hass.messages).toHaveLength(1); // the held list is still the answer
    expect(options(element)).toEqual(["Select a charger", "Garage"]);
  });
});
