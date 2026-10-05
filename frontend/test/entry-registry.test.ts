import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CARD_EDITOR_ELEMENT, CARD_ELEMENT } from "../src/types";

// The module defines on import; the polyfill flag is set before importing so that run defines at
// once, and each test then drives `defineCardWhenReady()` itself against a fresh registry stub.
window.CustomElementRegistryPolyfill = {};
const entry = await import("../src/index");

describe("defining the card only once Home Assistant's registry is in place", () => {
  let defined: Map<string, CustomElementConstructor>;

  beforeEach(() => {
    vi.useFakeTimers();
    defined = new Map();
    vi.spyOn(customElements, "get").mockImplementation((name: string) => defined.get(name));
    vi.spyOn(customElements, "define").mockImplementation((name: string, ctor) => {
      defined.set(name, ctor);
    });
    delete window.CustomElementRegistryPolyfill;
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("waits while the polyfill is not installed and defines once it is", () => {
    entry.defineCardWhenReady();
    vi.advanceTimersByTime(entry.REGISTRY_POLL_MS * 3);
    expect(defined.has(CARD_ELEMENT)).toBe(false);

    window.CustomElementRegistryPolyfill = {};
    vi.advanceTimersByTime(entry.REGISTRY_POLL_MS);
    expect(defined.has(CARD_ELEMENT)).toBe(true);
    expect(defined.has(CARD_EDITOR_ELEMENT)).toBe(true);
  });

  it("defines at once when the polyfill is already there", () => {
    window.CustomElementRegistryPolyfill = {};
    entry.defineCardWhenReady();
    expect(defined.has(CARD_ELEMENT)).toBe(true);
  });

  it("defines at once on a frontend without the polyfill whose app is already defined", () => {
    defined.set("home-assistant", class extends HTMLElement {});
    entry.defineCardWhenReady();
    expect(defined.has(CARD_ELEMENT)).toBe(true);
  });

  it("still defines after the wait when neither ever appears", () => {
    entry.defineCardWhenReady();
    vi.advanceTimersByTime(entry.REGISTRY_WAIT_MS - entry.REGISTRY_POLL_MS);
    expect(defined.has(CARD_ELEMENT)).toBe(false);
    vi.advanceTimersByTime(entry.REGISTRY_POLL_MS * 2);
    expect(defined.has(CARD_ELEMENT)).toBe(true);
  });
});
