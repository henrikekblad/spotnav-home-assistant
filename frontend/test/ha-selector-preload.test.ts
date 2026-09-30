// The entity editor asks Home Assistant to define its lazily loaded picker before it opens.

import { describe, expect, it } from "vitest";

import { ensureHaSelector } from "../src/entity-editor";

describe("ensureHaSelector", () => {
  it("loads the entities card editor so ha-selector becomes defined", async () => {
    const calls: string[] = [];
    const win = {
      customElements: window.customElements,
      loadCardHelpers: async () => ({
        createCardElement: () => ({
          constructor: {
            getConfigElement: async () => {
              calls.push("getConfigElement");
              if (window.customElements.get("ha-selector") === undefined) {
                window.customElements.define("ha-selector", class extends HTMLElement {});
              }
            },
          },
        }),
      }),
    } as unknown as Window;

    await ensureHaSelector(win, 50);

    expect(calls).toEqual(["getConfigElement"]);
    expect(window.customElements.get("ha-selector")).toBeDefined();
  });

  it("returns quietly without card helpers, leaving the plain-input fallback", async () => {
    const win = { customElements: { get: () => undefined } } as unknown as Window;
    await expect(ensureHaSelector(win, 10)).resolves.toBeUndefined();
  });

  it("does not wait forever when the picker never appears", async () => {
    const win = {
      customElements: { get: () => undefined, whenDefined: () => new Promise(() => undefined) },
      loadCardHelpers: async () => ({ createCardElement: () => ({}) }),
    } as unknown as Window;
    const started = Date.now();
    await ensureHaSelector(win, 20);
    expect(Date.now() - started).toBeLessThan(1000);
  });
});
