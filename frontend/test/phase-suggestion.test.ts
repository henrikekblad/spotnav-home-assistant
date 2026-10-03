// What charges showed about the planned car: "This car seems to charge on one phase", a question with a
// one-tap answer on the card (`update_vehicle` with `onboard_phases`), never a change made by itself.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "soc_charger" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, any>;

/** The two-vehicle dashboard with the planned car (EV6) suggested to be one-phase. */
function suggested(value: 1 | null = 1, planned = "vehicle_ev6"): Record<string, any> {
  const payload = read("dashboard", "target_soc_two_vehicles.json");
  for (const row of payload["vehicles"]) {
    row["suggested_onboard_phases"] = row["id"] === planned ? value : null;
  }
  return payload;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(payload: Record<string, unknown>, language = "en", admin = true) {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: admin };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

const box = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>("[data-suggestion='onboard-phases']");
const updates = (hass: FakeHass) => hass.entityMessages.filter((message) => message["type"] === "spotnav/update_vehicle");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the onboard charger suggestion on the card", () => {
  it("asks the administrator, in words, with two answers", async () => {
    const { element } = await mounted(suggested());
    expect(box(element)?.textContent).toContain("This car seems to charge on one phase. Set its onboard charger to 1-phase?");
    expect(Array.from(box(element)!.querySelectorAll("button")).map((button) => button.textContent)).toEqual([
      "Set to 1-phase",
      "Keep 3-phase",
    ]);
  });

  it("is worded in every language", async () => {
    for (const language of LANGUAGES) {
      document.body.innerHTML = "";
      const { element } = await mounted(suggested(), language);
      const text = box(element)?.textContent ?? "";
      expect(text, language).toContain(translate(language, "suggestion.onboardOne.text"));
      expect(text, language).toContain(translate(language, "suggestion.onboardOne.accept"));
      expect(text, language).toContain(translate(language, "suggestion.onboardOne.dismiss"));
    }
    expect(translate("sv", "suggestion.onboardOne.text")).not.toBe(translate("en", "suggestion.onboardOne.text"));
  });

  it("is not shown without a suggestion, to a reader who may not change it, or for a car that is not planned", async () => {
    expect(box((await mounted(suggested(null))).element)).toBeNull();
    expect(box((await mounted(suggested(1), "en", false)).element)).toBeNull();
    expect(box((await mounted(suggested(1, "vehicle_niro"))).element)).toBeNull();
  });

  it("accepts with one tap: one update under compare-and-set, then the dashboard is read again", async () => {
    const { hass, element } = await mounted(suggested());
    hass.entityHandler = async () => read("vehicle", "v1", "update_vehicle_success.json");
    box(element)!.querySelector<HTMLButtonElement>("[data-action='accept']")!.click();
    await settle();
    expect(updates(hass)).toEqual([
      {
        type: "spotnav/update_vehicle",
        api_version: 1,
        charger_id: "soc_charger",
        vehicle_id: "vehicle_ev6",
        changes: { onboard_phases: 1 },
        expected: { onboard_phases: 3 },
      },
    ]);
    expect(hass.messages.filter((message) => message.type === "spotnav/get_dashboard").length).toBeGreaterThan(1);
    hass.resolveNext(suggested(null));
    await settle();
    expect(box(element)).toBeNull();
  });

  it("dismisses by keeping three phases, which is an answer as well", async () => {
    const { hass, element } = await mounted(suggested());
    hass.entityHandler = async () => read("vehicle", "v1", "update_vehicle_success.json");
    box(element)!.querySelector<HTMLButtonElement>("[data-action='dismiss']")!.click();
    await settle();
    expect(updates(hass)[0]).toMatchObject({ changes: { onboard_phases: 3 }, expected: { onboard_phases: 3 } });
  });
});
