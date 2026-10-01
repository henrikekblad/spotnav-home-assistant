// The production boundary: the real custom element, the real decoder, the real visual card.
//
// Every case here drives public element behaviour -- mounting, `hass`, config, the visible actions and
// the timer -- with fake timers and deferred promises. No sleeps, no private fields, no second model.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import { translate } from "../src/i18n";
import { decodeDashboard } from "../src/validate";
import { API_VERSION } from "../src/types";
import { plannedStatus, rawBare, statusLine } from "./dashboard-fixtures";
import {
  charger,
  chargerOf,
  dashboard,
  FakeHass,
  mountCard,
  planOf,
  pricesOf,
  settingsOf,
} from "./helpers";

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";
const configFor = (chargerId: string) => ({ type: "custom:spotnav-card", charger: chargerId });
const retryButton = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector("button");
const dialogs = (element: Element): number =>
  shadow(element).querySelectorAll("[role='dialog']").length;

/** One macrotask with fake timers on: the card's own `.finally()` and renders have all run. */
async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

/**
 * Drain microtasks only, until `done()` holds -- never a timer, so this lands *inside* the window
 * between a failed render and the old promise's cleanup, which is the race Retry must survive.
 */
async function drainMicrotasksUntil(done: () => boolean): Promise<void> {
  for (let step = 0; step < 50; step += 1) {
    if (done()) {
      return;
    }
    await Promise.resolve();
  }
  throw new Error("the expected state never became visible");
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("1. the whole production path, end to end", () => {
  it("turns an unknown WebSocket answer into the visual card", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    expect(hass.messages).toEqual([
      { type: "spotnav/get_dashboard", api_version: API_VERSION, charger_id: "entry_a" },
    ]);

    const payload = dashboard();
    // The transport hands over exactly this object, untouched, and the decoder is what reads it.
    expect(decodeDashboard(payload).ok).toBe(true);
    hass.resolveNext(payload);
    await settle();

    const card = shadow(element).querySelector(".spotnav-card");
    expect(card).not.toBeNull();
    expect(text(element)).toContain("Garage");
    expect(text(element)).toContain("Planned from");
    // The visual layer, not the prototype's proof table of backend codes.
    expect(shadow(element).querySelector("svg")).not.toBeNull();
    expect(shadow(element).querySelector("[role='img']")).not.toBeNull();
    expect(shadow(element).querySelector(".spotnav-readout")).not.toBeNull();
    expect(text(element)).not.toContain("auto_price");
    expect(text(element)).not.toContain("Prototype");
    expect(text(element)).not.toContain('"api_version"');
  });
});

describe("2. accepted states render read-only", () => {
  it("shows the bare no-settings state with no mutation control anywhere", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    hass.resolveNext(rawBare());
    await settle();

    expect(text(element)).toContain(translate("en", "status.noPlan"));
    expect(shadow(element).querySelector("svg")).not.toBeNull();
    // The controls: the help action, the settings popover and the Plan trigger. A charger with no
    // settings record has no action on either axis and no strategy to choose. None of them mutates
    // anything on press: the trigger opens a read-only dialog until a record has been read, and the
    // dialogs are overlays outside the card, so their controls are not card controls either.
    const cardNode = shadow(element).querySelector(".spotnav-card") as HTMLElement;
    const buttons = Array.from(cardNode.querySelectorAll("button"));
    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual([
      translate("en", "header.info"),
      translate("en", "header.settings"),
      `${translate("en", "bar.plan")}: Not set \u00b7 No deadline \u00b7 Not set. ${translate("en", "bar.change")}`,
    ]);
    // No control button on either axis: with no settings there is nothing to start, pause or resume.
    expect(cardNode.querySelectorAll(".spotnav-action-button").length).toBe(0);
    expect(cardNode.querySelectorAll(".spotnav-planner-button").length).toBe(0);
    expect(cardNode.querySelectorAll(".spotnav-choice-button").length).toBe(0);
    for (const forbidden of ["switch", "select", "input", "form", "textarea"]) {
      expect(shadow(element).querySelectorAll(forbidden).length, forbidden).toBe(0);
    }
  });

  it("renders the Auto golden state without claiming to be a settings form", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    hass.resolveNext(dashboard());
    await settle();
    expect(text(element)).toContain("Cheapest charging period");
    const cardNode = shadow(element).querySelector(".spotnav-card") as HTMLElement;
    // The row is the backend's own two axes, and nothing here is a settings form: the golden state is
    // an idle Auto charger, so it offers Start now *and* the Pause beside it.
    const actions = cardNode.querySelectorAll(".spotnav-action-button");
    expect(actions.length).toBe(1);
    expect((actions[0] as HTMLElement).dataset["action"]).toBe("start");
    const planners = cardNode.querySelectorAll(".spotnav-planner-button");
    expect(planners.length).toBe(1);
    expect((planners[0] as HTMLElement).dataset["action"]).toBe("pause");
    expect((planners[0] as HTMLElement).textContent).not.toBe(
      (actions[0] as HTMLElement).textContent,
    );
    expect(cardNode.querySelectorAll("input, select, form, textarea").length).toBe(0);
  });
});

describe("3. three failure shells, no partial data", () => {
  it("keeps unsupported, malformed and transport failure apart", async () => {
    const cases: Array<[string, (hass: FakeHass) => void, string]> = [
      [
        "unsupported",
        (hass) => hass.resolveNext({ ...dashboard(), api_version: 7 }),
        translate("en", "state.unsupported"),
      ],
      [
        "malformed",
        (hass) => hass.resolveNext({ api_version: API_VERSION, charger: { charger_id: "entry_a" } }),
        translate("en", "state.malformed"),
      ],
      ["transport", (hass) => hass.rejectNext(new Error("Connection lost")), translate("en", "state.requestFailed")],
    ];
    for (const [name, fail, sentence] of cases) {
      document.body.innerHTML = "";
      const hass = new FakeHass();
      const element = mountCard(configFor("entry_a"), hass);
      element.hass = hass;
      await settle();
      fail(hass);
      await settle();
      const rendered = text(element);
      expect(rendered, name).toContain(sentence);
      // No partial answer: no chart, no figures, no names from the payload, no exception prose.
      expect(shadow(element).querySelector("svg"), name).toBeNull();
      expect(shadow(element).querySelector("dl"), name).toBeNull();
      expect(rendered, name).not.toContain("Garage");
      expect(rendered, name).not.toContain("öre");
      expect(rendered, name).not.toContain("Connection lost");
      // Only a retry can help, and only for the two recoverable ones.
      const recoverable = name !== "unsupported" && name !== "malformed";
      expect(retryButton(element) !== null, name).toBe(recoverable);
    }
  });
});

describe("4. Retry: the race, the counts, and a stale answer", () => {
  it("starts one fresh attempt from the window before the old one is cleaned up", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    expect(hass.messages).toHaveLength(1);

    hass.rejectNext(new Error("Connection lost"));
    // Microtasks only: the failed state is rendered, and the old promise's `.finally()` has *not*
    // yet cleared the in-flight attempt. This is exactly the window a reader clicks Retry in.
    await drainMicrotasksUntil(() => retryButton(element) !== null);
    const retry = retryButton(element);
    expect(retry).not.toBeNull();
    retry?.click();

    // The click entered loading (so the action is gone) and started one genuinely new request.
    expect(retryButton(element)).toBeNull();
    expect(text(element)).toContain(translate("en", "state.loading"));
    expect(hass.messages).toHaveLength(2);
    expect(hass.messages[1]).toEqual({
      type: "spotnav/get_dashboard",
      api_version: API_VERSION,
      charger_id: "entry_a",
    });

    // The forced attempt's success wins, and the failed answer never comes back.
    hass.resolveNext(dashboard());
    await settle();
    expect(text(element)).toContain("Garage");
    expect(text(element)).not.toContain(translate("en", "state.requestFailed"));
    expect(hass.messages).toHaveLength(2);
  });

  it("does not let an answer that arrives after Retry overwrite the retried one", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    // Two overlapping attempts for the *same* charger and generation: the first is forced past by
    // the second, and only the newest-started one may update the card.
    expect(hass.outstanding).toBe(1);
    // A forced refresh while the first attempt is still outstanding: two attempts, same generation
    // and same charger, because nothing about the configuration changed.
    void element.refresh({ force: true });
    await settle();
    expect(hass.messages).toHaveLength(2);

    const stale = dashboard({
      plan: { ...planOf(dashboard()), proposal: { ...(planOf(dashboard()).proposal as Record<string, unknown>), planned_kwh: 11 } },
      status: plannedStatus(11),
    });
    const fresh = dashboard({
      plan: { ...planOf(dashboard()), proposal: { ...(planOf(dashboard()).proposal as Record<string, unknown>), planned_kwh: 33 } },
      status: plannedStatus(33),
    });
    // The newer attempt answers first, then the older one arrives late.
    hass.resolveAt(1, fresh);
    await settle();
    expect(text(element)).toContain("33 kWh");
    hass.resolveAt(0, stale);
    await settle();
    expect(text(element)).toContain("33 kWh");
    expect(text(element)).not.toContain("11 kWh");
  });

  it("coalesces repeated retry clicks into the one forced attempt", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    hass.rejectNext(new Error("Connection lost"));
    await settle();
    const retry = retryButton(element) as HTMLButtonElement;
    retry.click();
    // The action is gone after the first click, so the UI cannot produce a second one; a programmatic
    // repeat coalesces into the forced attempt instead of stacking another request.
    void element.refresh({ force: true });
    void element.refresh({ force: true });
    await settle();
    expect(hass.messages).toHaveLength(2);
    hass.resolveNext(dashboard());
    await settle();
    expect(text(element)).toContain("Garage");
  });
});

describe("5-6. late and overlapping answers", () => {
  it("never paints charger A's answer onto charger B", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    expect(hass.messages.map((message) => message.charger_id)).toEqual(["entry_a"]);

    element.setConfig(configFor("entry_b"));
    await settle();
    expect(hass.messages.map((message) => message.charger_id)).toEqual(["entry_a", "entry_b"]);

    // A's answer arrives after the config moved on: inert, and the loading state stays.
    hass.resolveAt(0, dashboard({ charger: charger("entry_a", "Garage") }));
    await settle();
    expect(text(element)).not.toContain("Garage");
    expect(text(element)).toContain(translate("en", "state.loading"));

    hass.resolveAt(0, dashboard({ charger: charger("entry_b", "Carport") }));
    await settle();
    expect(text(element)).toContain("Carport");
    expect(text(element)).not.toContain("Garage");
  });
});

describe("7-8. disconnect and reconnect", () => {
  it("makes a late answer inert and releases the timer, view and listeners", async () => {
    const adds = vi.spyOn(document, "addEventListener");
    const removes = vi.spyOn(document, "removeEventListener");
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    expect(hass.messages).toHaveLength(1);

    hass.resolveNext(dashboard());
    await settle();
    const cardNode = shadow(element).querySelector(".spotnav-card");
    expect(cardNode).not.toBeNull();
    // Eight overlays are created with the view and stay out of the card's height: issues,
    // capabilities, pause, strategy, the planning editor, the area/fiscal editor, the entity editors
    // and the general Settings popover.
    expect(dialogs(element)).toBe(9);

    // One refresh is in flight when the card leaves the document.
    vi.advanceTimersByTime(REFRESH_INTERVAL_MS);
    await settle();
    expect(hass.messages).toHaveLength(2);
    expect(hass.outstanding).toBe(1);

    element.remove();
    // The view is released: its node and both dialogs are gone from the shadow root.
    expect(shadow(element).querySelector(".spotnav-card")).toBeNull();
    expect(dialogs(element)).toBe(0);

    const before = hass.messages.length;
    // The in-flight answer arrives late: inert, and no timer is left to fire again.
    hass.resolveNext(dashboard());
    await settle();
    vi.advanceTimersByTime(REFRESH_INTERVAL_MS * 3);
    await settle();
    expect(hass.messages).toHaveLength(before);
    expect(shadow(element).querySelector(".spotnav-card")).toBeNull();
    expect(text(element)).not.toContain("Garage");
    // The view's own document listener was removed exactly as often as it was added.
    const pointerAdds = adds.mock.calls.filter((call) => call[0] === "pointerdown").length;
    const pointerRemoves = removes.mock.calls.filter((call) => call[0] === "pointerdown").length;
    expect(pointerAdds).toBeGreaterThan(0);
    expect(pointerRemoves).toBe(pointerAdds);
  });

  it("establishes exactly one request cycle, timer and view when it is added back", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    hass.resolveNext(dashboard());
    await settle();
    element.remove();
    await settle();

    document.body.append(element);
    await settle();
    // One fresh cycle: one request, and one view.
    expect(hass.messages).toHaveLength(2);
    hass.resolveNext(dashboard());
    await settle();
    expect(shadow(element).querySelectorAll(".spotnav-card").length).toBe(1);
    expect(dialogs(element)).toBe(9);

    // And exactly one timer: one interval means one more request, not two.
    vi.advanceTimersByTime(REFRESH_INTERVAL_MS);
    await settle();
    expect(hass.messages).toHaveLength(3);
    expect(hass.outstanding).toBe(1);
  });
});

describe("9. a language change stays local", () => {
  it("rerenders the held snapshot with translated labels and zero new calls", async () => {
    const hass = new FakeHass();
    const english = hass.snapshot("en", "en");
    const element = mountCard(configFor("entry_a"), english);
    await settle();
    hass.resolveNext(dashboard());
    await settle();
    expect(text(element)).toContain("Planned from");

    vi.advanceTimersByTime(1_000);
    const before = hass.messages.length;
    const swedish = hass.snapshot("sv", "sv");
    element.hass = swedish;
    await settle();

    // Translated from the same immutable snapshot: no request, nothing outstanding, and the timer was
    // not restarted (the interval still ends when it always would have).
    expect(hass.messages).toHaveLength(before);
    expect(hass.outstanding).toBe(0);
    expect(text(element)).toContain("Planerat från");
    // The area line lives in the Settings popover's market section.
    shadow(element)
      .querySelector<HTMLButtonElement>(`[aria-label="${translate("sv", "header.settings")}"]`)
      ?.click();
    expect(text(element)).toContain(translate("sv", "context.area"));
    expect(text(element)).toContain("SE4");
    vi.advanceTimersByTime(REFRESH_INTERVAL_MS - 1_000);
    await settle();
    expect(hass.messages).toHaveLength(before + 1);
  });

  it("ignores an unrelated snapshot replacement", async () => {
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass.snapshot("one", "en"));
    await settle();
    hass.resolveNext(dashboard());
    await settle();
    const before = hass.messages.length;
    for (const label of ["two", "three", "four"]) {
      element.hass = hass.snapshot(label);
    }
    await settle();
    expect(hass.messages).toHaveLength(before);
    expect(hass.outstanding).toBe(0);
    expect(text(element)).toContain("Garage");
  });
});

describe("10-11. two cards, and one destroyed view per replacement", () => {
  it("keeps ids, selection, dialogs and lifecycles apart", async () => {
    const hass = new FakeHass();
    const first = mountCard(configFor("entry_a"), hass.snapshot("a", "en"));
    const second = mountCard(configFor("entry_b"), hass.snapshot("b", "en"));
    await settle();
    hass.resolveAt(0, dashboard({ charger: charger("entry_a", "Garage") }));
    hass.resolveAt(0, dashboard({ charger: charger("entry_b", "Carport") }));
    await settle();

    expect(text(first)).toContain("Garage");
    expect(text(second)).toContain("Carport");
    expect(hass.messages.map((message) => message.charger_id)).toEqual(["entry_a", "entry_b"]);

    // Distinct ARIA ids: the viewport names its own title and description.
    const firstLabel = shadow(first).querySelector(`[role='img']`)?.getAttribute("aria-labelledby");
    const secondLabel = shadow(second).querySelector(`[role='img']`)?.getAttribute("aria-labelledby");
    expect(firstLabel).not.toBe(secondLabel);
    expect(shadow(first).querySelector(`#${firstLabel}`)).not.toBeNull();
    expect(shadow(second).querySelector(`#${firstLabel}`)).toBeNull();

    // Isolated selection: walking one chart leaves the other's readout alone.
    const viewport = shadow(first).querySelector(`[role='img']`) as HTMLElement;
    const before = shadow(second).querySelector(".spotnav-readout")?.textContent;
    viewport.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    await settle();
    expect(shadow(first).querySelector(".spotnav-readout")?.textContent).not.toBe(before);
    expect(shadow(second).querySelector(".spotnav-readout")?.textContent).toBe(before);

    // Isolated dialogs and independent destruction.
    const secondDialogs = dialogs(second);
    first.remove();
    expect(shadow(first).querySelectorAll("[role='dialog']").length).toBe(0);
    expect(shadow(second).querySelectorAll("[role='dialog']").length).toBe(secondDialogs);
    expect(text(second)).toContain("Carport");
  });

  it("destroys the previous view on a language rerender and on a state replacement", async () => {
    const adds = vi.spyOn(document, "addEventListener");
    const removes = vi.spyOn(document, "removeEventListener");
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass.snapshot("en", "en"));
    await settle();
    hass.resolveNext(dashboard());
    await settle();

    // 1. ready -> ready (language): the old card node and its dialogs are gone, one set remains.
    const oldCard = shadow(element).querySelector(".spotnav-card");
    const oldDialogs = Array.from(shadow(element).querySelectorAll("[role='dialog']"));
    element.hass = hass.snapshot("sv", "sv");
    await settle();
    expect(shadow(element).querySelector(".spotnav-card")).not.toBe(oldCard);
    expect(oldCard?.isConnected).toBe(false);
    for (const dialog of oldDialogs) {
      expect(dialog.isConnected).toBe(false);
    }
    expect(dialogs(element)).toBe(oldDialogs.length);
    expect(shadow(element).querySelectorAll(".spotnav-chart-viewport svg").length).toBe(1);

    // 2. ready -> loading (a new charger): the view is released with its listeners.
    const beforeAdds = adds.mock.calls.filter((call) => call[0] === "pointerdown").length;
    const beforeRemoves = removes.mock.calls.filter((call) => call[0] === "pointerdown").length;
    element.setConfig(configFor("entry_b"));
    await settle();
    expect(shadow(element).querySelector(".spotnav-card")).toBeNull();
    expect(dialogs(element)).toBe(0);
    expect(text(element)).toContain(translate("sv", "state.loading"));
    const afterRemoves = removes.mock.calls.filter((call) => call[0] === "pointerdown").length;
    expect(afterRemoves).toBeGreaterThan(beforeRemoves);
    expect(beforeAdds).toBeGreaterThanOrEqual(afterRemoves);
  });
});

describe("12-13. hostile input and the audits", () => {
  it("keeps hostile names and codes as text through the real element", async () => {
    const hostileName = '<img src=x onerror="boom()">ACME" onmouseover="x"';
    const base = dashboard();
    const hass = new FakeHass();
    const element = mountCard(configFor("entry_a"), hass);
    element.hass = hass;
    await settle();
    hass.resolveNext(
      dashboard({
        charger: { ...chargerOf(base), charger_name: hostileName },
        prices: { ...pricesOf(base), state: "unavailable", reason: '<script>alert(1)</script>' },
        status: { tone: "blocking", lines: [statusLine("price_data_unavailable", { reason: "<script>alert(1)</script>" })] },
      }),
    );
    await settle();

    expect(text(element)).toContain(hostileName);
    expect(shadow(element).querySelector("img, script, iframe, object, embed")).toBeNull();
    expect(shadow(element).querySelector("[onerror], [onmouseover], [onload], [src]")).toBeNull();
    // The chart is ours, and the hostile code is never shown as text, not even in the issue dialog.
    expect(shadow(element).querySelectorAll(".spotnav-chart-viewport svg").length).toBe(1);
    const banner = shadow(element).querySelector(".spotnav-banner") as HTMLButtonElement;
    banner.click();
    await settle();
    expect(text(element)).not.toContain("<script>alert(1)</script>");
    expect(shadow(element).querySelector("script")).toBeNull();
  });

  it("has no forbidden operation in the element sources or the built bundle", () => {
    const sources = [
      "card.ts",
      "card-view.ts",
      "chart-interaction.ts",
      "chart-render.ts",
      "dialog.ts",
      "visual-styles.ts",
    ].map((name) => readFileSync(resolve(__dirname, "../src", name), "utf8"));
    const bundle = readFileSync(
      resolve(__dirname, "../../custom_components/spotnav/www/spotnav-card.js"),
      "utf8",
    );
    const forbidden = [
      "callService",
      "execute_script",
      "localStorage",
      "sessionStorage",
      "webhook",
      "pairing",
      "access_token",
      "fetch(",
      "XMLHttpRequest",
      "EventSource",
      "new WebSocket",
      "spotnav.sensnology.se",
      "innerHTML",
      "insertAdjacentHTML",
      "outerHTML",
    ];
    // Comments are stripped first: a file that *explains* "nothing is written with innerHTML" is
    // not a file that writes it, and an audit that cannot tell them apart is not an audit.
    const stripComments = (value: string): string =>
      value.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
    const code = sources.map(stripComments);
    const bundleCode = stripComments(bundle);
    for (const source of code) {
      for (const entry of forbidden) {
        expect(source.includes(entry), `source contains ${entry}`).toBe(false);
      }
    }
    for (const entry of forbidden) {
      expect(bundleCode.includes(entry), `bundle contains ${entry}`).toBe(false);
    }
    // The one backend operation, and only it: the element calls the API helper for the one version
    // it speaks, and the compiled bundle carries exactly that one command.
    expect(sources[0]).toContain("getDashboard(hass, charger, API_VERSION)");
    expect(sources[0]).not.toContain("callWS");
    expect(bundle).toContain("spotnav/get_dashboard");
    expect(bundle).not.toContain("spotnav/set_");
    expect(bundle).not.toContain("spotnav/pause");
  });
});
