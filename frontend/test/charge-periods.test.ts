// Charge periods: one value row in the charger's section ("Automatic", or "1 period" to "8 periods"), changed in a
// one-of-a-few editor and written as a settings replacement; `max_periods` null is automatic. The plan editor no
// longer has a periods slider.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chargePeriodsLabel, chargePeriodsOptions, chargePeriodsReplacement } from "../src/charge-periods";
import { LANGUAGES, translate } from "../src/i18n";
import { decodeSettingsRecord } from "../src/settings";
import { SETTINGS_API_VERSION } from "../src/types";
import { FakeHass, mountCard } from "./helpers";

const FIX = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, any> => JSON.parse(readFileSync(join(FIX, ...parts), "utf8"));
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
};
const PAUSE = { choice: null, admitted_at: null, expires_at: null };

function payload(maxPeriods: number | null = null): Record<string, any> {
  const d = read("dashboard", "cheapest_direct_site_admin.json");
  d.settings.max_periods = maxPeriods;
  return d;
}

async function openSettings(language = "en", admin = true, body: Record<string, any> = payload()) {
  const hass = new FakeHass();
  const entities = read("entity_config", "v1", "get_direct.json");
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config" ? entities : new Promise<unknown>(() => undefined);
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: admin };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(body);
  await settle();
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label='${translate(language as "en", "header.settings")}']`)!
    .click();
  await settle();
  return { hass, element };
}

function dialog(element: Element): HTMLElement {
  return Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find(
    (node) => node.closest("[hidden]") === null,
  )!;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the record", () => {
  it("reads null as automatic and keeps a number", () => {
    const base = read("settings", "v1", "success.json")["settings"];
    expect(decodeSettingsRecord({ ...base, max_periods: null }).max_periods).toBeNull();
    expect(decodeSettingsRecord({ ...base, max_periods: 3 }).max_periods).toBe(3);
    expect(() => decodeSettingsRecord({ ...base, max_periods: "auto" })).toThrow();
  });

  it("writes the choice and nothing else, and sends nothing when it did not move", () => {
    const record = decodeSettingsRecord({ ...read("settings", "v1", "success.json")["settings"], max_periods: null });
    const three = chargePeriodsReplacement(record, "3");
    expect(three).toMatchObject({ ok: true, changed: true });
    expect(three.ok && three.body.max_periods).toBe(3);
    expect(three.ok && three.body.requested_kwh).toBe(record.requested_kwh);
    expect(chargePeriodsReplacement(record, "auto")).toMatchObject({ ok: true, changed: false });
    const back = chargePeriodsReplacement({ ...record, max_periods: 3 }, "auto");
    expect(back.ok && back.body.max_periods).toBeNull();
    expect(chargePeriodsReplacement(record, "9").ok).toBe(false);
  });
});

describe("the words", () => {
  it("says Laddperioder and Automatiskt in Swedish, with the plural rule for the numbers", () => {
    expect(translate("sv", "settings.periods.label")).toBe("Laddperioder");
    expect(chargePeriodsLabel("sv", null)).toBe("Automatiskt");
    expect(chargePeriodsLabel("sv", 1)).toBe("1 period");
    expect(chargePeriodsLabel("sv", 3)).toBe("3 perioder");
    expect(chargePeriodsOptions("en").map((option) => option.value)).toEqual(["auto", "1", "2", "3", "4", "5", "6", "7", "8"]);
  });

  it("has every word in every language", () => {
    for (const language of LANGUAGES) {
      for (const key of ["settings.periods.label", "settings.periods.auto", "settings.periods.help", "settings.periods.autoHelp"] as const) {
        expect(translate(language, key)).not.toBe(key);
      }
    }
  });
});

describe("the charger section", () => {
  it("shows the row, opens the one-of-a-few editor and writes the choice", async () => {
    const { hass, element } = await openSettings("sv");
    const section = dialog(element).querySelector<HTMLElement>("[data-section='entities']")!;
    const row = section.querySelector<HTMLElement>("[data-row='charge_periods']")!;
    expect(row.textContent).toBe("LaddperioderAutomatiskt");
    row.querySelector<HTMLButtonElement>("[data-edit='charge_periods']")!.click();
    await settle();
    const form = dialog(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    expect(form.querySelectorAll("[data-value-option]")).toHaveLength(9);
    form.querySelector<HTMLInputElement>("[data-value-option='2']")!.click();
    form.requestSubmit();
    await settle();
    // The write reads the record fresh first, then replaces it.
    hass.resolveNext({ api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: payload()["settings"], pause: PAUSE });
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings") as Record<string, any>;
    expect(update["settings"]["max_periods"]).toBe(2);
  });

  it("is not shown to a reader who is not an administrator", async () => {
    const { element } = await openSettings("en", false);
    expect(dialog(element).querySelector("[data-row='charge_periods']")).toBeNull();
  });

  it("names a stored number in the reader's plural", async () => {
    const { element } = await openSettings("en", true, payload(4));
    expect(dialog(element).querySelector("[data-row='charge_periods']")?.textContent).toBe("Charge periods4 periods");
  });
});
