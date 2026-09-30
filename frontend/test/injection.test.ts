// The injection boundary, driven from both sides: a hostile charger title or id, and a hostile
// rejection message. Everything here asserts the same two things -- the value is *visible as
// text* -- and nothing in the shadow DOM became an element or an attribute because of it.
//
// The card renders its proof view as an HTML string, so every dynamic value goes through the
// shared `escapeHtml`. The editor builds nodes and never needs escaping, which is checked here by
// construction (no element, no attribute) and structurally (its source has no `innerHTML`).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { escapeHtml } from "../src/dom";
import { translate } from "../src/i18n";
import { apiFailure, charger, chargerList, dashboard, FakeHass, mountCard, mountEditor } from "./helpers";

/** `<`, `>`, `&`, both quote kinds, and an element with an event handler. */
const HOSTILE_NAME = `<img src=x onerror="window.__spotnav_pwned=1">&'"<script>alert(1)</script>`;
const HOSTILE_ID = `entry"><svg onload=alert(1)>&<script>alert(2)</script>`;
const HOSTILE_ERROR =
  "Bearer eyJhbGciOiJIUzI1NiJ9.secret.token <img src=x onerror=alert(1)> " +
  "/config/.storage/core.config_entries: KeyError('secret')";

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

/** No element, and no attribute value, may come from the hostile input. */
function assertNothingWasInjected(element: Element, markers: string[]): void {
  const root = shadow(element);
  // Our own SVG chart is expected and is not an injection; the hostile markers are asserted below
  // through every attribute value, and the executable elements must simply not exist.
  expect(root.querySelector("img, script, iframe, object, embed")).toBeNull();
  expect(root.querySelector("[onload], [onerror], [src]")).toBeNull();
  for (const node of Array.from(root.querySelectorAll("*"))) {
    for (const attribute of Array.from(node.attributes)) {
      if (attribute.name === "value") {
        // An `<option>`'s value *is* the config-entry id, so it holds whatever the id is. It is a
        // value, not markup, and the tests below assert it exactly rather than by marker.
        continue;
      }
      for (const marker of markers) {
        expect(attribute.value).not.toContain(marker);
      }
    }
  }
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
  delete (window as unknown as { __spotnav_pwned?: number }).__spotnav_pwned;
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the escaping helper", () => {
  it("escapes both text and attribute metacharacters, and escapes ampersands once", () => {
    expect(escapeHtml(`<a href="x" onerror='y'>&`)).toBe(
      "&lt;a href=&quot;x&quot; onerror=&#39;y&#39;&gt;&amp;",
    );
    expect(escapeHtml("&lt;script&gt;")).toBe("&amp;lt;script&amp;gt;");
    expect(escapeHtml("plain")).toBe("plain");
  });
});

describe("the card", () => {
  it("renders a hostile charger title as text, never as an element", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    await settle();
    hass.resolveNext(
      dashboard({ charger: charger("entry_a", HOSTILE_NAME) }),
    );
    await settle();

    expect(shadow(element).textContent).toContain(HOSTILE_NAME);
    assertNothingWasInjected(element, ["<img", "onerror", "<script", "onload"]);
    expect((window as unknown as { __spotnav_pwned?: number }).__spotnav_pwned).toBeUndefined();
  });

  it("renders a hostile configured id as text in the charger-missing state", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card", charger: HOSTILE_ID }, hass);
    await settle();
    hass.rejectNext(apiFailure("spotnav_unknown_charger", HOSTILE_ERROR));
    await settle();

    // The accepted shell names the state, not the configured id: nothing echoes a config value.
    expect(shadow(element).textContent).toContain(translate("en", "state.chargerMissing"));
    assertNothingWasInjected(element, ["<svg", "onload", "<script", "onerror"]);
  });

  it("shows a static failure for a hostile rejection message, and still offers retry", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    await settle();
    hass.rejectNext(apiFailure("spotnav_http_error", HOSTILE_ERROR));
    await settle();

    const rendered = shadow(element).textContent ?? "";
    expect(rendered).toContain(translate("en", "state.requestFailed"));
    for (const marker of ["Bearer", "eyJhbGciOiJIUzI1NiJ9", "<img", "onerror", "core.config_entries", "KeyError"]) {
      expect(rendered).not.toContain(marker);
      expect(shadow(element).innerHTML).not.toContain(marker);
    }
    assertNothingWasInjected(element, ["Bearer", "<img", "onerror"]);
    const retry = shadow(element).querySelector("button") as HTMLButtonElement;
    expect(retry).not.toBeNull();
    expect(retry.getAttribute("aria-label")).not.toContain("Bearer");
  });
});

function select(element: Element): HTMLSelectElement {
  return shadow(element).querySelector("select") as HTMLSelectElement;
}

describe("the editor", () => {
  it("renders a hostile charger title as option text, never as an element", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "" }, hass);
    hass.resolveNext(chargerList([charger("entry_a", HOSTILE_NAME)]));
    await settle();

    const options = select(element).options;
    expect(options[1]?.textContent).toBe(HOSTILE_NAME);
    expect(options[1]?.value).toBe("entry_a");
    expect(shadow(element).textContent).toContain(HOSTILE_NAME);
    assertNothingWasInjected(element, ["<img", "onerror", "<script", "onload"]);
  });

  it("keeps a hostile configured id as an option value and shows it as text", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: HOSTILE_ID }, hass);
    hass.resolveNext(chargerList([charger("entry_a", "Garage")]));
    await settle();

    const control = select(element);
    expect(control.value).toBe(HOSTILE_ID);
    const unresolved = Array.from(control.options).find((item) => item.value === HOSTILE_ID);
    expect(unresolved?.textContent).toContain(HOSTILE_ID);
    expect(shadow(element).textContent).toContain(`Selected: ${HOSTILE_ID}`);
    assertNothingWasInjected(element, ["<svg", "onload", "<script", "onerror"]);
  });

  it("shows a static failure for a hostile rejection message, and still offers retry", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    await settle();
    hass.rejectNext(apiFailure("spotnav_http_error", HOSTILE_ERROR));
    await settle();

    const rendered = shadow(element).textContent ?? "";
    expect(rendered).toContain("Could not load the SpotNav chargers");
    for (const marker of ["Bearer", "<img", "onerror", "core.config_entries", "KeyError"]) {
      expect(rendered).not.toContain(marker);
      expect(shadow(element).innerHTML).not.toContain(marker);
    }
    expect(shadow(element).querySelector("button")).not.toBeNull();
    expect(select(element).value).toBe("entry_a");
    assertNothingWasInjected(element, ["Bearer", "<img", "onerror"]);
  });

  it("builds its content from nodes, and keeps one escaping implementation for the card", () => {
    const source = readFileSync(resolve(__dirname, "../src/editor.ts"), "utf8");
    expect(source).not.toContain("innerHTML");
    expect(source).not.toContain("insertAdjacentHTML");
    expect(source).not.toContain("outerHTML");
    // The production element builds nodes and writes `textContent`: it has no markup sink at all
    // and it hands the decoded answer to the accepted view rather than formatting it itself.
    const cardSource = readFileSync(resolve(__dirname, "../src/card.ts"), "utf8");
    expect(cardSource).not.toContain("innerHTML");
    expect(cardSource).not.toContain("insertAdjacentHTML");
    expect(cardSource).not.toContain("outerHTML");
    expect(cardSource).toContain('from "./validate"');
    expect(cardSource).toContain('from "./card-view"');
    expect(cardSource).not.toContain("function escapeHtml");
  });

  it("still emits exactly the type and charger keys", async () => {
    const hass = new FakeHass();
    const element = mountEditor({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    const seen: unknown[] = [];
    const details: unknown[] = [];
    element.addEventListener("config-changed", (event) => {
      details.push((event as CustomEvent).detail);
      seen.push(((event as CustomEvent).detail as { config: unknown }).config);
    });
    hass.resolveNext(chargerList([charger("entry_a", "Garage"), charger("entry_b", "Carport")]));
    await settle();

    const control = select(element);
    control.value = "entry_b";
    control.dispatchEvent(new Event("change"));
    await settle();
    expect(seen).toEqual([{ type: "custom:spotnav-card", charger: "entry_b" }]);
    expect(Object.keys(details[0] as object)).toEqual(["config"]);
    expect(Object.keys(seen[0] as object).sort()).toEqual(["charger", "type"]);
  });
});
