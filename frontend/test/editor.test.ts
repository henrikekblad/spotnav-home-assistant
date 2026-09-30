// The minimal visual editor: choices by display name, the id as the stored value, and exactly
// one kind of event.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { charger, chargerList, FakeHass, mountEditor } from "./helpers";
import { API_VERSION } from "../src/types";

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const select = (element: Element): HTMLSelectElement =>
  shadow(element).querySelector("select") as HTMLSelectElement;
const options = (element: Element): string[] =>
  Array.from(shadow(element).querySelectorAll("option")).map((option) => option.textContent ?? "");

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

describe("choosing a charger", () => {
  it("offers the chargers the backend lists, by display name, and preselects nothing", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "" }, hass);
    hass.resolveNext(chargerList([charger("a", "Garage"), charger("b", "Carport")]));
    await settle();

    expect(options(element)).toEqual(["Select a charger", "Garage", "Carport"]);
    await settle();
    expect(select(element).value).toBe("");
  });

  it("asks for the charger list with the exact v1 message, once", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "a" }, hass);
    await settle();
    for (let i = 0; i < 10; i += 1) {
      element.hass = hass;
    }
    await settle();
    expect(hass.messages).toEqual([{ type: "spotnav/list_chargers", api_version: API_VERSION }]);
  });

  it("stores what it emits as exactly {type, charger}", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "a" }, hass);
    const seen = events(element);
    hass.resolveNext(chargerList([charger("a", "Garage"), charger("b", "Carport")]));
    await settle();

    select(element).value = "b";
    select(element).dispatchEvent(new Event("change"));
    await settle();
    expect(seen).toEqual([{ type: "custom:spotnav-card", charger: "b" }]);
    expect(Object.keys(seen[0] as object).sort()).toEqual(["charger", "type"]);
  });

  it("keeps a configured charger the backend no longer offers, as unresolved", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "gone" }, hass);
    hass.resolveNext(chargerList([charger("a", "Garage")]));
    await settle();

    expect(select(element).value).toBe("gone");
    expect(options(element)).toEqual(["Select a charger", "gone (unavailable)", "Garage"]);
  });

  it("says so when the installation has no charger at all", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "" }, hass);
    hass.resolveNext(chargerList([]));
    await settle();
    expect(options(element)).toEqual(["Select a charger"]);
    expect(shadow(element).textContent).toContain("No SpotNav charger is configured");
  });
});

describe("automatic selection", () => {
  it("selects the only available charger, and announces it", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "" }, hass);
    const seen = events(element);
    hass.resolveNext(chargerList([charger("a", "Garage")]));
    await settle();
    expect(seen).toEqual([{ type: "custom:spotnav-card", charger: "a" }]);
    expect(select(element).value).toBe("a");
  });

  it("selects nothing when there is a choice to make, or none available", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "" }, hass);
    const seen = events(element);
    hass.resolveNext(chargerList([charger("a", "Garage"), charger("b", "Carport")]));
    await settle();
    expect(seen).toEqual([]);
    expect(select(element).value).toBe("");

    const other = new FakeHass();
    const second = mountEditor({ type: "custom:spotnav-card", charger: "" }, other);
    const otherSeen = events(second);
    other.resolveNext(chargerList([charger("a", "Garage", false)]));
    await settle();
    expect(otherSeen).toEqual([]);
    expect(select(second).value).toBe("");
  });
});

describe("errors", () => {
  it("reports a refusal without losing the current config, and retries on demand", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "a" }, hass);
    await settle();
    hass.rejectNext(new Error("Connection lost"));
    await settle();

    expect(shadow(element).textContent).toContain("Could not load the SpotNav chargers");
    expect(shadow(element).textContent).not.toContain("Connection lost");
    expect(select(element).value).toBe("a");

    const retry = shadow(element).querySelector("button") as HTMLButtonElement;
    expect(retry.getAttribute("aria-label")).toBe("Retry loading the charger list");
    retry.focus();
    expect(shadow(element).activeElement).toBe(retry);

    retry.click();
    await settle();
    expect(hass.messages).toHaveLength(2);
    hass.resolveNext(chargerList([charger("a", "Garage")]));
    await settle();
    expect(select(element).value).toBe("a");
    expect(options(element)).toEqual(["Select a charger", "Garage"]);
  });

  it("has one labelled control and no settings or action buttons of its own", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "a" }, hass);
    hass.resolveNext(chargerList([charger("a", "Garage")]));
    await settle();
    expect(shadow(element).querySelectorAll("select, button")).toHaveLength(1);
    expect(select(element).getAttribute("aria-label")).toBe("SpotNav charger");
    expect(shadow(element).querySelector("label")?.getAttribute("for")).toBe("spotnav-charger");
  });
});
