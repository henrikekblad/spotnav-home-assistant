// The bundle audit: what the committed asset may and may not contain, and that it really loads.
//
// The string greps below are a tripwire, not the privacy proof. The structural proof is the
// runtime one: `card.test.ts` mounts the card against a fake `hass`, asserts the exact two v1
// messages on the wire, and trips over `fetch`, `XMLHttpRequest`, `EventSource` and storage if the
// card ever reaches for them; `api.test.ts` pins the messages themselves.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, it } from "vitest";

import { translate } from "../src/i18n";
import { API_VERSION } from "../src/types";

const ASSET = resolve(__dirname, "../../custom_components/spotnav/www/spotnav-card.js");
const SOURCE = readFileSync(ASSET, "utf8");

/**
 * The compiled asset, evaluated as a module exactly as a browser would load it.
 *
 * A data URL keeps the evaluation honest and keeps Vite's own resolver out of it: the file the
 * route serves is the module that runs, byte for byte, and a second, differently-encoded URL is a
 * second evaluation of it (which is what a reload racing the cache does).
 */
function evaluate(tag: string): Promise<unknown> {
  const encoded = encodeURIComponent(`${SOURCE}
// ${tag}`);
  return import(/* @vite-ignore */ `data:text/javascript;charset=utf-8,${encoded}`);
}

/** Things a card that went behind the backend's back would have to mention. */
const FORBIDDEN = [
  "sensnology",
  "spotnav.sensnology.se",
  "/v1/",
  "localStorage",
  "sessionStorage",
  "session_storage",
  "webhook",
  "pairing",
  "access_token",
  "Authorization",
  // `http://` and `https://` are asserted separately below: the compiled SVG code carries the W3C
  // namespace `http://www.w3.org/2000/svg`, which is an identifier rather than a network address.
  "fetch(",
  "XMLHttpRequest",
  "EventSource",
  "new WebSocket",
  "eval(",
];

describe("the committed asset", () => {
  it("is a real file of a sane size, and mentions the element it defines", () => {
    expect(SOURCE.length).toBeGreaterThan(500);
    expect(SOURCE).toContain("spotnav-card");
    expect(SOURCE).toContain("customElements.define");
    expect(SOURCE).toContain("callWS");
  });

  it.each(FORBIDDEN)("does not mention %s", (needle) => {
    expect(SOURCE).not.toContain(needle);
  });

  it("identifies itself with the integration version it was built from", () => {
    const version = JSON.parse(
      readFileSync(resolve(__dirname, "../../custom_components/spotnav/manifest.json"), "utf8"),
    ).version;
    expect(SOURCE).toContain(`Integration version ${version}`);
  });
});

describe("loading the compiled asset", () => {
  beforeEach(() => {
    delete window.customCards;
    // What Home Assistant's core script sets when it installs its element registry, which the bundle
    // waits for before it defines anything (`src/index.ts`).
    window.CustomElementRegistryPolyfill = {};
  });

  it("defines the elements and publishes one picker entry, even if evaluated twice", async () => {
    await evaluate("first");
    const first = customElements.get("spotnav-card");
    expect(first).toBeDefined();
    expect(customElements.get("spotnav-card-editor")).toBeDefined();
    const cards = window.customCards ?? [];
    const mine = cards.filter((entry) => entry.type === "spotnav-card");
    expect(mine).toHaveLength(1);
    expect(mine[0]?.name).toBe("SpotNav");
    expect(mine[0]?.description).toBe("Charging plan and electricity prices from SpotNav.");
    expect(mine[0]?.preview).toBe(true);
    expect(mine[0]?.documentationURL).toBe("https://github.com/henrikekblad/spotnav-home-assistant");

    // A second evaluation: what a reload racing a cached resource does. No throw, no duplicate.
    await evaluate("again");
    expect(customElements.get("spotnav-card")).toBe(first);
    expect(cards.filter((entry) => entry.type === "spotnav-card")).toHaveLength(1);
    expect(window.customCards?.filter((entry) => entry.type === "spotnav-card")).toHaveLength(1);
  });

  it("renders the unconfigured state from the compiled bundle alone", async () => {
    await evaluate("render");
    const element = document.createElement("spotnav-card");
    (element as unknown as { setConfig(c: unknown): void }).setConfig({
      type: "custom:spotnav-card",
    });
    document.body.appendChild(element);
    // The compiled element carries the accepted view: the localized unconfigured sentence, and no
    // prototype note at all.
    expect(element.shadowRoot?.textContent).toContain(
      translate("en", "state.unconfigured"),
    );
    expect(element.shadowRoot?.textContent).not.toContain("Prototype");
    document.body.removeChild(element);
  });
});

describe("the asset's only absolute URLs", () => {
  it("are the W3C namespace and the documentation link, and nothing that could be a network address", () => {
    const asset = readFileSync(ASSET, "utf8");
    const found = new Set(asset.match(/https?:\/\/[^"'`)\s]+/g) ?? []);
    expect(Array.from(found).sort()).toEqual([
      "http://www.w3.org/2000/svg",
      "https://github.com/henrikekblad/spotnav-home-assistant",
    ]);
  });
});
