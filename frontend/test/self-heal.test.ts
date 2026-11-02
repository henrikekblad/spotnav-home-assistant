// The card heals itself after SpotNav is updated: an answer it cannot read, or a language file it cannot
// fetch, makes it ask which card is served. A newer one reloads the page once (never twice for one hash),
// or offers a button where a reload could loop or lose input; the same one is retried before the card says
// it cannot read.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetPageReloadForTest } from "../src/card";
import { setOwnCardBundleHashForTest } from "../src/card-identity";
import { LANGUAGES, forgetLanguages, translate } from "../src/i18n";
import {
  CARD_INFO_TIMEOUT_MS,
  MALFORMED_RETRY_DELAYS_MS,
  RELOAD_DELAY_MS,
  RELOAD_KEY_PREFIX,
  UPDATE_WORDS,
  claimReload,
  isMalformed,
  setReloadForTest,
} from "../src/self-heal";
import { API_VERSION, SETTINGS_API_VERSION } from "../src/types";
import { dashboard, FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const OWN = "11111111";
const SERVED = "22222222";
const MALFORMED = { api_version: API_VERSION, charger: { charger_id: "entry_a" } };

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";
const notice = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>("[data-card-updated]");

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function cardInfo(served: string): Record<string, unknown> {
  return { api_version: 1, ok: true, error: null, spotnav_version: "2.3.0", card_bundle_hash: served };
}

let reloads = 0;

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
  sessionStorage.clear();
  reloads = 0;
  setReloadForTest(() => {
    reloads += 1;
  });
  forgetPageReloadForTest();
  setOwnCardBundleHashForTest(OWN);
});

afterEach(() => {
  setOwnCardBundleHashForTest(null);
  setReloadForTest(null);
  forgetPageReloadForTest();
  vi.useRealTimers();
});

/** A Swedish reader's card, its card-info question answered with `served`, its first read still open. */
async function mounted(served: string = SERVED): Promise<{ hass: FakeHass; element: Element }> {
  const hass = new FakeHass();
  hass.cardInfoHandler = async () => cardInfo(served);
  const element = mountCard(CONFIG, null);
  const snapshot = hass.snapshot("snapshot", "sv");
  snapshot.user = { is_admin: true };
  (element as unknown as { hass: unknown }).hass = snapshot;
  await settle();
  return { hass, element };
}

describe("the pieces", () => {
  it("tells an unreadable answer from another version and from a good one", () => {
    expect(isMalformed({ ok: false, failure: "malformed" })).toBe(true);
    expect(isMalformed({ ok: false, failure: "unsupported" })).toBe(false);
    expect(isMalformed({ ok: true, value: {} })).toBe(false);
    expect(isMalformed(null)).toBe(false);
  });

  it("claims one reload per served hash, and none where storage fails", () => {
    expect(claimReload(sessionStorage, "aaaaaaaa")).toBe(true);
    expect(claimReload(sessionStorage, "aaaaaaaa")).toBe(false);
    expect(claimReload(sessionStorage, "bbbbbbbb")).toBe(true);
    expect(sessionStorage.getItem(`${RELOAD_KEY_PREFIX}aaaaaaaa`)).toBe("1");
    expect(claimReload(null, "cccccccc")).toBe(false);
    const broken = {
      getItem: () => null,
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    };
    expect(claimReload(broken, "cccccccc")).toBe(false);
    // A store that forgets what it was told cannot guard a reload either.
    expect(claimReload({ getItem: () => null, setItem: () => undefined }, "dddddddd")).toBe(false);
  });

  it("has the line and the button in every language", () => {
    for (const language of LANGUAGES) {
      const words = UPDATE_WORDS[language];
      expect(words.reloading.length, language).toBeGreaterThan(0);
      expect(words.updated.length, language).toBeGreaterThan(0);
      expect(words.reload.length, language).toBeGreaterThan(0);
    }
    expect(UPDATE_WORDS.sv).toEqual({
      reloading: "SpotNav har uppdaterats – laddar om…",
      updated: "SpotNav har uppdaterats.",
      reload: "Ladda om",
    });
  });
});

describe("an unreadable dashboard from an outdated card", () => {
  it("says so, reloads once, and never twice for the same served hash", async () => {
    const { hass, element } = await mounted();
    hass.resolveNext(MALFORMED);
    await settle();

    expect(hass.cardInfoMessages).toHaveLength(1);
    expect(notice(element)?.dataset["cardUpdated"]).toBe("reloading");
    expect(text(element)).toContain(UPDATE_WORDS.sv.reloading);
    expect(text(element)).not.toContain(translate("sv", "state.malformed"));
    expect(reloads).toBe(0);
    await vi.advanceTimersByTimeAsync(RELOAD_DELAY_MS);
    expect(reloads).toBe(1);
    expect(sessionStorage.getItem(`${RELOAD_KEY_PREFIX}${SERVED}`)).toBe("1");

    // After the reload the old card is still here (the browser kept it): the button, no second reload.
    document.body.innerHTML = "";
    forgetPageReloadForTest();
    const again = await mounted();
    again.hass.resolveNext(MALFORMED);
    await settle();
    await vi.advanceTimersByTimeAsync(RELOAD_DELAY_MS * 4);
    expect(reloads).toBe(1);
    expect(notice(again.element)?.dataset["cardUpdated"]).toBe("button");
    expect(text(again.element)).toContain(UPDATE_WORDS.sv.updated);
    const button = notice(again.element)?.querySelector<HTMLButtonElement>("button");
    expect(button?.textContent).toBe(UPDATE_WORDS.sv.reload);
    button?.click();
    expect(reloads).toBe(2);
  });

  it("reloads again for a newer served hash", async () => {
    sessionStorage.setItem(`${RELOAD_KEY_PREFIX}${SERVED}`, "1");
    const { hass, element } = await mounted("33333333");
    hass.resolveNext(MALFORMED);
    await settle();
    expect(notice(element)?.dataset["cardUpdated"]).toBe("reloading");
    await vi.advanceTimersByTimeAsync(RELOAD_DELAY_MS);
    expect(reloads).toBe(1);
  });

  it("offers the button and never reloads when storage throws", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    const { hass, element } = await mounted();
    hass.resolveNext(MALFORMED);
    await settle();
    await vi.advanceTimersByTimeAsync(RELOAD_DELAY_MS * 4);
    expect(reloads).toBe(0);
    expect(notice(element)?.dataset["cardUpdated"]).toBe("button");
  });

  it("keeps a dashboard already shown under the line when a later read cannot be read", async () => {
    const { hass, element } = await mounted();
    hass.resolveNext(dashboard());
    await settle();
    expect(notice(element)).toBeNull();
    const card = shadow(element).querySelector(".spotnav-card");
    expect(card).not.toBeNull();

    await vi.advanceTimersByTimeAsync(30_000);
    hass.resolveNext(MALFORMED);
    await settle();
    expect(shadow(element).querySelector(".spotnav-card")).toBe(card);
    expect(card?.firstElementChild?.getAttribute("data-card-updated")).toBe("reloading");
  });
});

describe("an unreadable dashboard from the card that is served", () => {
  it("is retried twice, a little apart, before the card says it cannot read", async () => {
    const { hass, element } = await mounted(OWN);
    const reads = (): number => hass.messages.filter((m) => m["type"] === "spotnav/get_dashboard").length;
    expect(reads()).toBe(1);
    hass.resolveNext(MALFORMED);
    await settle();
    expect(text(element)).toContain(translate("sv", "state.loading"));
    expect(notice(element)).toBeNull();

    await vi.advanceTimersByTimeAsync(MALFORMED_RETRY_DELAYS_MS[0]!);
    expect(reads()).toBe(2);
    hass.resolveNext(MALFORMED);
    await settle();
    expect(text(element)).toContain(translate("sv", "state.loading"));

    await vi.advanceTimersByTimeAsync(MALFORMED_RETRY_DELAYS_MS[1]!);
    expect(reads()).toBe(3);
    hass.resolveNext(MALFORMED);
    await settle();
    expect(text(element)).toContain(translate("sv", "state.malformed"));
    expect(reloads).toBe(0);
  });

  it("shows the dashboard when a retry reads it", async () => {
    const { hass, element } = await mounted(OWN);
    hass.resolveNext(MALFORMED);
    await settle();
    await vi.advanceTimersByTimeAsync(MALFORMED_RETRY_DELAYS_MS[0]!);
    hass.resolveNext(dashboard());
    await settle();
    expect(shadow(element).querySelector(".spotnav-card")).not.toBeNull();
    expect(text(element)).not.toContain(translate("sv", "state.malformed"));
  });

  it("says it cannot read at once when the integration does not answer which card it serves", async () => {
    const { hass, element } = await mounted();
    hass.cardInfoHandler = null;
    hass.resolveNext(MALFORMED);
    await vi.advanceTimersByTimeAsync(CARD_INFO_TIMEOUT_MS);
    expect(text(element)).toContain(translate("sv", "state.malformed"));
    expect(reloads).toBe(0);
  });
});

describe("a dialog with unsaved input", () => {
  const RECORD = {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 3,
    amps: 10,
    requested_kwh: 20.5,
    max_periods: 4,
    departure_enabled: true,
    departure_time: "08:00",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: null },
  };

  it("gets the button, never a reload, when the Plan dialog's answer cannot be read", async () => {
    const { hass, element } = await mounted();
    hass.resolveNext(JSON.parse(readFileSync(join(FIXTURES, "dashboard", "proposal_ready.json"), "utf8")));
    await settle();
    shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
    await settle();
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: { ...RECORD },
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const preview = JSON.parse(readFileSync(join(FIXTURES, "plan_preview", "ready.json"), "utf8"));
    hass.previewHandler = async () => ({ ...preview, added_later: true });
    await vi.advanceTimersByTimeAsync(2_000);
    expect(hass.previewMessages.length).toBeGreaterThan(0);
    expect(hass.cardInfoMessages).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(RELOAD_DELAY_MS * 4);
    expect(reloads).toBe(0);
    expect(notice(element)?.dataset["cardUpdated"]).toBe("button");
    // The reload for this hash was not spent: it is still there for when nothing would be lost.
    expect(sessionStorage.getItem(`${RELOAD_KEY_PREFIX}${SERVED}`)).toBeNull();
  });
});

describe("a language file that cannot be had", () => {
  it("takes the same path when a newer card is served, in the reader's own words", async () => {
    forgetLanguages();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("not found", { status: 404 })),
    );
    const { hass, element } = await mounted();
    await settle();
    expect(hass.cardInfoMessages).toHaveLength(1);
    expect(notice(element)?.dataset["cardUpdated"]).toBe("reloading");
    expect(text(element)).toContain(UPDATE_WORDS.sv.reloading);
    await vi.advanceTimersByTimeAsync(RELOAD_DELAY_MS);
    expect(reloads).toBe(1);
  });

  it("stays in English, with no line, when the same card is served", async () => {
    forgetLanguages();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("not found", { status: 404 })),
    );
    const { hass, element } = await mounted(OWN);
    await settle();
    expect(hass.cardInfoMessages).toHaveLength(1);
    hass.resolveNext(dashboard());
    await settle();
    expect(notice(element)).toBeNull();
    await vi.advanceTimersByTimeAsync(RELOAD_DELAY_MS * 4);
    expect(reloads).toBe(0);
  });
});
