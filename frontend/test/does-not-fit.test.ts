// A planned charge that does not fit under the ceiling by its departure (`src/does-not-fit.ts`): the backend's own
// blocks (`tests/fixtures/does_not_fit/`, `tests/test_does_not_fit.py`) read and worded, the charger card's line and
// its buttons (each a choice for this departure only, never the lasting `if_not_ready`), and the Plan tab's car lane.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { choiceLabel, decodeDoesNotFit, doesNotFitOrNull, doesNotFitText } from "../src/does-not-fit";
import { decodePlan, planBody } from "../src/house-plan";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const EN = { language: "en" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };
const SV = { ...EN, language: "sv" as const };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(dir: string, name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, dir, name), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

describe("the backend's does-not-fit blocks", () => {
  it("reads each and says what does not fit, or what was chosen", () => {
    const ask = decodeDoesNotFit(fixture("does_not_fit", "ask.json"));
    expect(doesNotFitText(SV, ask)).toBe("Ryms inte under taket (Effektmål 6,0 kW): 16,2 kWh till 14:30 – 7,1 kWh hinner.");
    expect(doesNotFitText(EN, ask)).toBe(
      "Does not fit under the ceiling (Import target 6.0 kW): 16.2 kWh by 14:30 – 7.1 kWh fit in time.",
    );
    const money = { major_unit: "kr", minor_unit: "öre" };
    expect(ask.choices.map((choice) => choiceLabel(SV, choice, money))).toEqual([
      "Ta det som ryms (≈ 7,1 kWh)",
      "Senare avresa (17:00)",
      "Tillåt toppen (≈ 12 kW)",
    ]);
    expect(choiceLabel(EN, ask.choices[1]!, money)).toBe("Later departure (17:00)");
    expect(choiceLabel(SV, { code: "accept_peak", kwh: null, peak_kw: 12, cost: 10500, departureMs: null }, money)).toBe(
      "Tillåt toppen (≈ 12 kW, ≈ 105 kr)",
    );
    const hold = decodeDoesNotFit(fixture("does_not_fit", "hold.json"));
    expect(doesNotFitText(SV, hold)).toBe("Bilen tar det som ryms till 14:30 (6,5 kWh).");
    expect(doesNotFitText(EN, hold)).toBe("The car takes what fits by 14:30 (6.5 kWh).");
    expect(hold.choices.map((choice) => choice.code)).toEqual(["later", "accept_peak"]);
    // No later hour fits within the prices held: the editor is the way.
    expect(choiceLabel(SV, hold.choices[0]!, money)).toBe("Senare avresa");
    const release = decodeDoesNotFit(fixture("does_not_fit", "release.json"));
    expect(doesNotFitText(SV, release)).toBe("Taket (Effektmål 6,0 kW) är släppt för den här laddningen till 14:30.");
    expect(release.choices.map((choice) => choice.code)).toEqual(["takes_what_fits"]);
    expect(choiceLabel(SV, release.choices[0]!, money)).toBe("Ta det som ryms");
  });

  it("refuses a block with an unknown key, and the dashboard then shows no line", () => {
    const changed = { ...fixture("does_not_fit", "ask.json"), extra: 1 };
    expect(() => decodeDoesNotFit(changed)).toThrow();
    expect(doesNotFitOrNull(changed)).toBeNull();
    expect(doesNotFitOrNull(undefined)).toBeNull();
  });
});

describe("the charger card's does-not-fit line", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    document.body.replaceChildren();
  });

  function dashboard(block: string): Record<string, unknown> {
    const payload = fixture("dashboard", "start_idle.json");
    payload["status"] = {
      tone: "notice",
      lines: [
        {
          code: "import_target_at_risk",
          params: {
            departure: "2026-11-02T13:30:35+00:00",
            missing_kwh: 9.1,
            target_kw: 6,
            releasable: block === "ask.json",
            set_kw: 6,
            release_until: "2026-11-02T13:30:35+00:00",
            release_peak_kw: 12,
          },
        },
      ],
    };
    payload["does_not_fit"] = fixture("does_not_fit", block);
    return payload;
  }

  async function mounted(block: string): Promise<{ hass: FakeHass; root: ShadowRoot }> {
    const hass = new FakeHass();
    const element = mountCard(CONFIG, hass);
    const snapshot = hass.snapshot("snapshot", "sv");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await settle();
    hass.resolveNext(dashboard(block));
    await settle();
    return { hass, root: shadow(element) };
  }

  const buttons = (root: ShadowRoot): string[] =>
    [...root.querySelectorAll<HTMLButtonElement>("[data-part='does-not-fit'] button")].map((button) => button.dataset["choice"] ?? "");

  it("says it once, with a button per choice, in place of the at-risk line and its release button", async () => {
    const { root } = await mounted("ask.json");
    const line = root.querySelector("[data-part='does-not-fit']");
    expect(line?.textContent).toContain("Ryms inte under taket (Effektmål 6,0 kW): 16,2 kWh till 14:30 – 7,1 kWh hinner.");
    expect(buttons(root)).toEqual(["takes_what_fits", "later", "accept_peak"]);
    expect(root.querySelector("[data-part='release-import-target']")).toBeNull();
    expect(root.textContent).not.toContain("Bilen blir inte klar");
  });

  it("asks for Take what fits for this departure only, never writing the lasting if_not_ready", async () => {
    const { hass, root } = await mounted("ask.json");
    root.querySelector<HTMLButtonElement>("[data-choice='takes_what_fits']")!.click();
    await settle();
    expect(hass.messages.at(-1)).toMatchObject({ type: "spotnav/take_what_fits", api_version: 1, charger_id: "entry_a" });
    expect(hass.messages.some((message) => message["type"] === "spotnav/update_settings")).toBe(false);
  });

  it("offers Take what fits to a user who is not an administrator", async () => {
    const hass = new FakeHass();
    const element = mountCard(CONFIG, hass);
    element.hass = hass.snapshot("snapshot", "sv");
    await settle();
    hass.resolveNext(dashboard("ask.json"));
    await settle();
    expect(buttons(shadow(element))).toContain("takes_what_fits");
  });

  it("moves this departure for Later departure (17:00), never writing the departure's settings", async () => {
    const { hass, root } = await mounted("ask.json");
    const later = root.querySelector<HTMLButtonElement>("[data-choice='later']")!;
    expect(later.textContent).toBe("Senare avresa (17:00)");
    later.click();
    await settle();
    expect(hass.messages.at(-1)).toMatchObject({ type: "spotnav/later_departure", api_version: 1, charger_id: "entry_a" });
    expect(hass.messages.some((message) => message["type"] === "spotnav/update_settings")).toBe(false);
  });

  it("opens the departure's editor for Later departure where no later hour fits", async () => {
    const { hass, root } = await mounted("hold.json");
    const before = hass.messages.length;
    root.querySelector<HTMLButtonElement>("[data-choice='later']")!.click();
    await settle();
    const sent = hass.messages.slice(before).map((message) => message["type"]);
    expect(sent).toContain("spotnav/get_settings");
    expect(sent).not.toContain("spotnav/later_departure");
  });

  it("sets the import target aside for this charge for Allow the peak, confirmed first, also where the plan holds", async () => {
    const { hass, root } = await mounted("hold.json");
    expect(root.querySelector("[data-part='does-not-fit']")?.textContent).toContain("Bilen tar det som ryms till 14:30 (6,5 kWh).");
    expect(buttons(root)).toEqual(["later", "accept_peak"]);
    root.querySelector<HTMLButtonElement>("[data-choice='accept_peak']")!.click();
    await settle();
    const sent = (): number => hass.messages.filter((message) => message["type"] === "spotnav/release_import_target").length;
    expect(sent()).toBe(0);
    root.querySelector<HTMLButtonElement>("[data-action='release']")!.click();
    await settle();
    expect(sent()).toBe(1);
  });

  it("says the plan's own release, with Take what fits still offered for this departure", async () => {
    const { root } = await mounted("release.json");
    expect(root.querySelector("[data-part='does-not-fit'] p")?.textContent).toBe(
      "Taket (Effektmål 6,0 kW) är släppt för den här laddningen till 14:30.",
    );
    expect(buttons(root)).toEqual(["takes_what_fits"]);
  });
});

describe("the Plan tab's car lane", () => {
  function villa() {
    const decoded = decodePlan(fixture("plan", "villa.json"));
    if (!decoded.ok) {
      throw new Error("the villa does not decode");
    }
    return decoded.value;
  }

  it("says the line under the car's lane, and its buttons choose for that charger", () => {
    const choose = vi.fn();
    const body = planBody({ doc: document, language: "sv", plan: villa(), width: 600, onDoesNotFit: choose });
    const line = body.querySelector("[data-load='car'] [data-part='does-not-fit']");
    expect(line?.textContent).toContain("Ryms inte under taket (Effektmål 6,0 kW): 17,8 kWh till 07:00 – 14,8 kWh hinner.");
    const choices = [...line!.querySelectorAll<HTMLButtonElement>("button")];
    // "Later departure" with the hour it all fits by moves this departure there.
    expect(choices.map((button) => button.dataset["choice"])).toEqual(["takes_what_fits", "later", "accept_peak"]);
    expect(choices[1]!.textContent).toBe("Senare avresa (08:00)");
    expect(choices[2]!.textContent).toBe("Tillåt toppen (≈ 13 kW, ≈ 45 kr)");
    choices[0]!.click();
    expect(choose).toHaveBeenCalledWith("charger_garage", "takes_what_fits");
    choices[1]!.click();
    expect(choose).toHaveBeenLastCalledWith("charger_garage", "later");
    // Allowing the peak is asked again before it is chosen.
    choices[2]!.click();
    expect(choose).toHaveBeenCalledTimes(2);
    expect(choices[2]!.textContent).toBe("Ja, tillåt toppen");
    choices[2]!.click();
    expect(choose).toHaveBeenLastCalledWith("charger_garage", "accept_peak");
  });

  it("says no line for a car whose charge fits, and no buttons without a handler", () => {
    const flat = decodePlan(fixture("plan", "car_only.json"));
    expect(flat.ok && flat.value.loads[0]!.does_not_fit).toBeNull();
    const body = planBody({ doc: document, language: "sv", plan: villa(), width: 600 });
    expect(body.querySelector("[data-part='does-not-fit'] button")).toBeNull();
  });
});
