// The card's states and its rendered facts, with a faked `hass` at the public boundary.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import type { SpotnavCard } from "../src/card";
import { translate } from "../src/i18n";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { apiFailure, charger, chargerList, dashboard, FakeHass, mountCard } from "./helpers";
import { API_VERSION } from "../src/types";

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";
const card = (config: unknown = { type: "custom:spotnav-card", charger: "entry_a" }) =>
  mountCard(config);

/** Let every microtask and every zero-delay timer run, deterministically. */
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

describe("configuration validation", () => {
  it("refuses a wrong type and a wrong charger type", () => {
    expect(() => card({ type: "custom:weather" })).toThrow(/type/);
    expect(() => card({ type: "custom:spotnav-card", charger: 7 })).toThrow(/charger/);
  });

  it("shows the unconfigured state without asking the backend anything", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card" }, hass);
    await settle();
    expect(text(element)).toContain(translate("en", "state.unconfigured"));
    expect(hass.messages).toEqual([]);
  });

  describe("the picker's stub config", () => {
    type Stub = (hass?: unknown) => Promise<unknown>;
    const stubOf = (): Stub =>
      (card().constructor as unknown as { getStubConfig: Stub }).getStubConfig;
    const empty = { type: "custom:spotnav-card", charger: "" };

    it("names the one charger there is, so the preview renders the real card", async () => {
      const hass = new FakeHass();
      const pending = stubOf()(hass);
      hass.resolveNext(chargerList([charger("only", "Garage")]));
      expect(await pending).toEqual({ type: "custom:spotnav-card", charger: "only" });
    });

    it("stays unconfigured with no charger or several", async () => {
      const none = new FakeHass();
      const a = stubOf()(none);
      none.resolveNext(chargerList([]));
      expect(await a).toEqual(empty);
      const several = new FakeHass();
      const b = stubOf()(several);
      several.resolveNext(chargerList([charger("a", "A"), charger("b", "B")]));
      expect(await b).toEqual(empty);
    });

    it("stays unconfigured on a failure, a missing hass, and a backend that never answers", async () => {
      expect(await stubOf()(undefined)).toEqual(empty);
      const failing = new FakeHass();
      const c = stubOf()(failing);
      failing.rejectNext({ code: "boom" });
      expect(await c).toEqual(empty);
      const silent = new FakeHass();
      const d = stubOf()(silent);
      await vi.advanceTimersByTimeAsync(3000);
      expect(await d).toEqual(empty);
    });
  });

  it("shows the SpotNav mark above the unconfigured sentence", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card" }, hass);
    await settle();
    const root = (element as HTMLElement).shadowRoot!;
    const svg = root.querySelector("svg");
    const sentence = root.querySelector("p");
    expect(svg).not.toBeNull();
    expect(sentence).not.toBeNull();
    expect(svg!.compareDocumentPosition(sentence!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("creates its editor element through the documented static hook", () => {
    const element = card();
    const created = (
      element.constructor as unknown as { getConfigElement(): HTMLElement }
    ).getConfigElement();
    expect(created.tagName.toLowerCase()).toBe("spotnav-card-editor");
  });
});

describe("the ready state", () => {
  it("renders typed backend facts, with each count from its own section", async () => {
    const hass = new FakeHass();
    const element = card();
    element.hass = hass;
    await settle();
    expect(hass.messages).toEqual([
      { type: "spotnav/get_dashboard", api_version: API_VERSION, charger_id: "entry_a" },
    ]);
    expect(text(element)).toContain(translate("en", "state.loading"));

    hass.resolveNext(dashboard());
    await settle();

    const rendered = text(element);
    // The accepted read-only view: the charger's own name, the localized status sentence and the
    // graph -- never the prototype's proof table of backend codes. The quiet area/currency line now
    // lives in the Settings popover's market section instead (see `settings-overview.test.ts`).
    expect(rendered).toContain("Garage");
    expect(rendered).toContain("Planned from");
    expect(shadow(element).querySelector("svg")).not.toBeNull();
    expect(shadow(element).querySelector("[role='img']")).not.toBeNull();
    expect(rendered).not.toContain("Prototype");
    expect(rendered).not.toContain("auto_price");
    expect(rendered).not.toContain("proposal_ready");
    expect(rendered).not.toContain('"api_version"');
  });

  it("escapes what the backend says instead of injecting it as markup", async () => {
    const hass = new FakeHass();
    const element = card();
    element.hass = hass;
    await settle();
    hass.resolveNext(
      dashboard({
        charger: {
          charger_id: "a",
          charger_name: "<img src=x>",
          available: true,
          capabilities: {
            auto_price: true,
            current_limit: false,
            set_current: false,
            regulated_current: false,
            target_soc: false,
            load_balancing: false,
            refresh_vehicle: false,
            set_charge_limit: false,
            target_stop: false,
          },
        },
      }),
    );
    await settle();
    // The hostile name stays a text node: no element, no attribute, and the SVG chart is our own.
    expect(shadow(element).querySelector("[src]")).toBeNull();
    expect(shadow(element).querySelector("script, iframe, object, embed")).toBeNull();
    expect(text(element)).toContain("<img src=x>");
  });
});

describe("the states that are not ready", () => {
  it("shows the unsupported-version state when the integration refuses the version, and asks once", async () => {
    const hass = new FakeHass();
    const element = card();
    element.hass = hass;
    await settle();
    hass.rejectNext(apiFailure("spotnav_unsupported_api_version"));
    await settle();
    expect(text(element)).toContain(translate("en", "state.unsupported"));
    expect(shadow(element).querySelector("button")).toBeNull();
    // No fall-back: one request, for the one version this card speaks.
    expect(hass.messages.map((message) => message["api_version"])).toEqual([API_VERSION]);
  });

  it.each(["spotnav_unknown_charger", "spotnav_charger_unloaded", "spotnav_site_not_charger"])(
    "keeps a %s charger visible and selects nothing in its place",
    async (code) => {
      const hass = new FakeHass();
      const element = card({ type: "custom:spotnav-card", charger: "entry_gone" });
      element.hass = hass;
      await settle();
      hass.rejectNext(apiFailure(code));
      await settle();
      const rendered = text(element);
      expect(rendered).toContain(translate("en", "state.chargerMissing"));
      // The accepted shell names the state and does not echo the configured id: nothing from the
      // config reaches the screen except the charger's own name, which this answer never carried.
      expect(rendered).not.toContain("entry_gone");
      expect(shadow(element).querySelector("button")).not.toBeNull();
      expect(hass.messages).toHaveLength(1);
    },
  );

  it("shows a failed request with a labelled, focusable retry that asks again", async () => {
    const hass = new FakeHass();
    const element = card();
    element.hass = hass;
    await settle();
    hass.rejectNext(new Error("Connection lost"));
    await settle();

    expect(text(element)).toContain(translate("en", "state.requestFailed"));
    expect(text(element)).toContain("The request failed. Check the connection");
    expect(text(element)).not.toContain("Connection lost");
    const retry = shadow(element).querySelector("button") as HTMLButtonElement;
    expect(retry).not.toBeNull();
    expect(retry.getAttribute("aria-label")).toBe(translate("en", "state.retry"));
    retry.focus();
    expect(shadow(element).activeElement).toBe(retry);

    retry.click();
    await settle();
    expect(hass.messages).toHaveLength(2);
    hass.resolveNext(dashboard());
    await settle();
    expect(text(element)).toContain("Garage");
  });
});

describe("what the card hands to the dashboard", () => {
  it("reports a size and the documented grid options", () => {
    const element = card();
    const asCard = element as unknown as {
      getCardSize(): number;
      getGridOptions(): Record<string, unknown>;
    };
    expect(asCard.getCardSize()).toBe(3);
    expect(asCard.getGridOptions()).toEqual({ rows: "auto", columns: "full" });
  });

  it("uses theme variables with fallbacks, and nothing that overflows horizontally", () => {
    const element = card();
    const styles = shadow(element).querySelector("style")?.textContent ?? "";
    expect(styles).toContain("var(--primary-text-color, #212121)");
    expect(styles).toContain("var(--error-color, #db4437)");
    expect(styles).toContain("max-width: 100%");
    expect(styles).toContain("min-width: 0");
    expect(styles).toContain("overflow-wrap: anywhere");
    expect(styles).not.toMatch(/[^-]width:\s*\d+px/);
    // jsdom cannot measure layout, so the 320 px guarantee is asserted structurally: no fixed
    // widths anywhere, and every long value wraps instead of pushing the card wider.
    expect(REFRESH_INTERVAL_MS).toBe(30_000);
  });
});

describe("the one call this card is allowed to make", () => {
  it("never reaches for fetch, XHR, a socket, or storage", async () => {
    const fetchSpy = vi.fn(() => {
      throw new Error("fetch must not be used");
    });
    vi.stubGlobal("fetch", fetchSpy);
    vi.stubGlobal(
      "XMLHttpRequest",
      class {
        constructor() {
          throw new Error("XMLHttpRequest must not be used");
        }
      },
    );
    vi.stubGlobal(
      "EventSource",
      class {
        constructor() {
          throw new Error("EventSource must not be used");
        }
      },
    );
    const setItem = vi.spyOn(Storage.prototype, "setItem");

    const hass = new FakeHass();
    const element = card();
    element.hass = hass;
    await settle();
    hass.resolveNext(dashboard());
    await settle();

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
    expect(text(element)).toContain("Garage");
    const [message] = hass.messages;
    expect(Object.keys(message ?? {}).sort()).toEqual(["api_version", "charger_id", "type"]);
  });
});

describe("the vehicle-side advisory", () => {
  const ADVISORY = {
    state: "vehicle_not_requesting_current",
    reason: "suspended_ev_zero_current",
    since: "2026-09-27T12:00:00+00:00",
  };

  async function rendered(progress: unknown): Promise<SpotnavCard> {
    const hass = new FakeHass();
    const element = card();
    element.hass = hass;
    await settle();
    hass.resolveNext(dashboard({ charge_progress: progress }));
    await settle();
    return element;
  }

  it("words a charger judged by its power, not by a connector status, in its own sentence", async () => {
    const element = await rendered({ ...ADVISORY, reason: "power_below_threshold" });

    const node = shadow(element).querySelector(`.${VISUAL_CLASSES.advisory}`);
    expect(node?.textContent).toBe(translate("en", "advisory.powerBelowThreshold"));
    expect(node?.textContent).toContain("draws almost no power");
    expect(translate("sv", "advisory.powerBelowThreshold")).toContain("laddaren drar nästan ingen effekt");
    expect(node?.textContent).not.toContain("power_below_threshold");
  });

  it("renders one subdued sentence for the backend's own advisory, and no action beside it", async () => {
    const element = await rendered(ADVISORY);

    const node = shadow(element).querySelector(`.${VISUAL_CLASSES.advisory}`);
    expect(node).not.toBeNull();
    expect(node?.getAttribute("role")).toBe("status");
    expect(node?.textContent).toContain(translate("en", "advisory.vehicleNotRequestingCurrent"));
    // The backend's stable code is not shown -- nor the raw OCPP status, a schedule claim or a promise
    // about the cable.
    expect(node?.textContent).not.toContain("suspended_ev_zero_current");
    expect(node?.textContent).not.toContain("Technical detail");
    expect(node?.textContent).not.toContain("SuspendedEV");
    expect(node?.textContent).not.toContain(ADVISORY.since);
    // An observation offers nothing to press: this diagnostic has no recovery command on either side.
    expect(node?.querySelector("button")).toBeNull();
    expect(node?.querySelector("a")).toBeNull();
  });

  it.each([
    ["normal", "suspended_ev_zero_current_pending"],
    ["normal", "charge_not_expected"],
    ["unknown", "connector_status_unavailable"],
  ])("says nothing at all for a %s observation (%s)", async (state, reason) => {
    const element = await rendered({ state, reason, since: null });
    expect(shadow(element).querySelector(`.${VISUAL_CLASSES.advisory}`)).toBeNull();
  });

  it.each([
    ["at its target", { value: 80, target_percent: 80, vehicle_max_percent: null, need_kwh: null }, true],
    ["at its own maximum", { value: 90, target_percent: 100, vehicle_max_percent: 90, need_kwh: null }, true],
    ["with no energy left to need", { value: 70, target_percent: 80, vehicle_max_percent: null, need_kwh: 0 }, true],
    ["below its target", { value: 60, target_percent: 80, vehicle_max_percent: null, need_kwh: 5 }, false],
    ["with no reading", { value: null, target_percent: 80, vehicle_max_percent: null, need_kwh: null }, false],
  ])("words a full car calmly when the car is %s: %s", async (_label, facts, full) => {
    const hass = new FakeHass();
    const element = card();
    element.hass = hass;
    await settle();
    const soc = {
      age_s: 30, capacity_kwh: 77, efficiency: 0.9, estimated: false, missing: [], source: "vehicle",
      vehicle_id: "ev6", vehicle_name: "EV6", vehicles: [],
    };
    hass.resolveNext(dashboard({ charge_progress: ADVISORY, soc: { ...soc, ...facts } }));
    await settle();

    const node = shadow(element).querySelector(`.${VISUAL_CLASSES.advisory}`);
    expect(node?.textContent).toBe(translate("en", full ? "advisory.carFull" : "advisory.vehicleNotRequestingCurrent"));
  });

  it("says nothing when the block is unreadable, and still renders the rest", async () => {
    const element = await rendered({ state: "SuspendedEV", reason: "x", since: null });
    expect(shadow(element).querySelector(`.${VISUAL_CLASSES.advisory}`)).toBeNull();
    expect(text(element)).toContain("Garage");
    expect(shadow(element).querySelector("svg")).not.toBeNull();
  });
});
