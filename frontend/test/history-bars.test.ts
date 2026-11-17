// The house and heat history's top chart in money (`period-history.ts` `periodBars`): the whole bar is what the period
// would have cost without the saving, the filled part what it cost, the hatched part the saving on top; a saving below
// zero is the top of the cost itself hatched in the warning colour. And the Support rows (send and download the debug
// file) in the settings popover opened from the house and the heat card.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { HEAT_CARD_ELEMENT, SpotnavHeatCard } from "../src/heat-card";
import { decodeHeatHistory, heatHistoryBody, type HeatHistory } from "../src/heat-history";
import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { decodeHouseHistory, houseHistoryBody, type HouseHistory } from "../src/house-history";
import { registerTranslation } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { moneyParts, type PeriodUi } from "../src/period-history";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

function house(change: (answer: HouseHistory) => void = () => undefined, name = "villa_month.json"): HouseHistory {
  const decoded = decodeHouseHistory(read("house_history", name));
  if (!decoded.ok) {
    throw new Error("must decode");
  }
  change(decoded.value);
  return decoded.value;
}

function heat(change: (answer: HeatHistory) => void = () => undefined): HeatHistory {
  const decoded = decodeHeatHistory(read("heat_history", "villa_month.json"));
  if (!decoded.ok) {
    throw new Error("must decode");
  }
  change(decoded.value);
  return decoded.value;
}

const ui: PeriodUi = { period: "month", date: null, detailed: false, pending: false, selected: null };
const noHandlers = { onPeriod: () => undefined, onDetailed: () => undefined };

function houseBody(answer: HouseHistory): HTMLElement {
  return houseHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui, handlers: noHandlers, width: 330, idPrefix: "t", today: "2026-11-16" });
}

function heatBody(answer: HeatHistory): HTMLElement {
  return heatHistoryBody({ doc: document, language: "sv", state: { kind: "ready", answer }, ui, handlers: noHandlers, width: 330, idPrefix: "t", today: "2026-11-16" });
}

function bar(body: HTMLElement, key: string): HTMLButtonElement {
  return body.querySelector<HTMLButtonElement>(`.spotnav-history-bar[data-bar='${key}']`)!;
}

function grows(button: HTMLElement): Record<string, number> {
  const out: Record<string, number> = {};
  button.querySelectorAll<HTMLElement>(".spotnav-history-bar-part").forEach((piece) => {
    out[piece.dataset["part"]!] = Number(piece.style.flexGrow);
  });
  return out;
}

function height(button: HTMLElement): number {
  return parseFloat(button.querySelector<HTMLElement>(".spotnav-history-bar-fill")!.style.height);
}

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

afterEach(() => {
  vi.useRealTimers();
  document.body.replaceChildren();
});

describe("a money bar's parts", () => {
  it("is the cost filled and the saving hatched on top, the whole being the cost without the saving", () => {
    expect(moneyParts(3000, 400)).toEqual({ solid: 3000, hatch: 400, warn: 0, whole: 3400 });
  });

  it("is the filled cost alone without a saving figure", () => {
    expect(moneyParts(3000, null)).toEqual({ solid: 3000, hatch: 0, warn: 0, whole: 3000 });
    expect(moneyParts(null, null)).toEqual({ solid: 0, hatch: 0, warn: 0, whole: 0 });
  });

  it("marks a saving below zero as the top of the cost, so the bar is never taller than what was paid", () => {
    expect(moneyParts(3000, -250)).toEqual({ solid: 2750, hatch: 0, warn: 250, whole: 3000 });
    // More than the whole cost made dearer: all of it is extra.
    expect(moneyParts(100, -300)).toEqual({ solid: 0, hatch: 0, warn: 100, whole: 100 });
  });

  it("draws a period that earned money as a saving on its own", () => {
    expect(moneyParts(-200, 500)).toEqual({ solid: 0, hatch: 300, warn: 0, whole: 300 });
  });
});

describe("the house history's chart", () => {
  it("draws whole, filled and hatched heights, with no price colour, and says what each is", () => {
    const body = houseBody(house());
    const reviewed = bar(body, "2026-11-05");
    expect(grows(reviewed)).toEqual({ saving: 400, cost: 3342 });
    expect(height(reviewed)).toBe(100);
    expect(reviewed.querySelector<HTMLElement>(".spotnav-history-bar-fill")!.style.getPropertyValue("--spotnav-day-dear")).toBe("");
    expect(reviewed.getAttribute("aria-label")).toBe("tors 5 nov.: kostnad 33 kr, utan batteri 37 kr, sparat 4,0 kr");
    // A swatch per part the bars hold.
    const legendParts = (root: HTMLElement): string[] => Array.from(root.querySelectorAll("[data-part='legend'] li")).map((item) => `${item.querySelector("span")!.dataset["part"]}:${item.textContent}`);
    expect(legendParts(body)).toEqual(["cost:Kostnad", "saving:Sparat av batteriet"]);
    expect(body.querySelector(".spotnav-history-scale")!.textContent).toBe("37 kr");
  });

  it("shows a period without a battery review as the filled cost alone and says it is not evaluated", () => {
    const body = houseBody(house());
    const plain = bar(body, "2026-11-02");
    expect(grows(plain)).toEqual({ cost: 3342 });
    expect(height(plain)).toBeCloseTo((3342 / 3742) * 100, 5);
    expect(plain.getAttribute("aria-label")).toBe("mån 2 nov.: kostnad 33 kr, ej utvärderad");
    const none = houseBody(house((answer) => answer.buckets.forEach((item) => (item.battery_saving = null))));
    expect(grows(bar(none, "2026-11-05"))).toEqual({ cost: 3342 });
    // Nothing hatched: no hatched entry in the legend.
    expect(Array.from(none.querySelectorAll("[data-part='legend'] li span[data-part]")).map((swatch) => (swatch as HTMLElement).dataset["part"])).toEqual(["cost"]);
  });

  it("shows a saving below zero hatched in the warning colour at the top of the cost", () => {
    const body = houseBody(house((answer) => (answer.buckets.find((item) => item.key === "2026-11-05")!.battery_saving = -342)));
    const worse = bar(body, "2026-11-05");
    expect(grows(worse)).toEqual({ extra: 342, cost: 3000 });
    expect(worse.getAttribute("aria-label")).toBe("tors 5 nov.: kostnad 33 kr, utan batteri 30 kr, batteriet gjorde det 3,4 kr dyrare");
    expect(Array.from(body.querySelectorAll("[data-part='legend'] li")).map((item) => item.textContent)).toContain("Dyrare med batteriet");
  });

  it("does not claim a saving for the hours of a day: the review is a day's", () => {
    const body = houseHistoryBody({
      doc: document,
      language: "sv",
      state: { kind: "ready", answer: house(undefined, "villa_day.json") },
      ui: { ...ui, period: "day" },
      handlers: noHandlers,
      width: 330,
      idPrefix: "t",
      today: "2026-11-16",
    });
    expect(body.querySelectorAll(".spotnav-history-bar-part[data-part='saving']")).toHaveLength(0);
    expect(body.querySelector(".spotnav-history-bar")!.getAttribute("aria-label")).toMatch(/sparat visas per dygn$/);
  });

  it("falls back to the energy in one colour where nothing in the period is priced", () => {
    const body = houseBody(house((answer) => answer.buckets.forEach((item) => (item.cost = null))));
    expect(body.querySelector("[data-part='legend']")).toBeNull();
    expect(body.querySelector(".spotnav-history-bar-part")).toBeNull();
  });

  it("is in every language", () => {
    for (const key of ["hist.legend.cost", "hist.house.legend.saving", "hist.house.legend.extra", "hist.house.barMoney", "hist.house.barWorse", "hist.house.barUnreviewed", "hist.house.barHourly", "hist.heat.legend.saving", "hist.heat.barSaving"] as const) {
      for (const [language, words] of Object.entries(TRANSLATIONS)) {
        expect(words[key], `${language} ${key}`).toBeTruthy();
      }
    }
  });
});

describe("the heat history's chart", () => {
  it("draws the cost filled and what SpotNav's plan would have saved hatched, labelled as such", () => {
    const answer = heat();
    const item = answer.buckets.find((candidate) => candidate.shadow_value !== null && candidate.cost !== null)!;
    const body = heatBody(answer);
    const drawn = bar(body, item.key);
    expect(Object.keys(grows(drawn))).toEqual(["saving", "cost"]);
    expect(grows(drawn)["saving"]).toBeCloseTo(item.shadow_value!, 3);
    expect(grows(drawn)["cost"]).toBeCloseTo(item.cost!, 3);
    expect(drawn.getAttribute("aria-label")).toContain("skulle ha sparat ");
    expect(Array.from(body.querySelectorAll("[data-part='legend'] li")).map((item) => item.textContent)).toEqual(["Kostnad", "Skulle ha sparat"]);
  });

  it("draws a day the shadow did not evaluate as the cost alone", () => {
    const answer = heat();
    const item = answer.buckets.find((candidate) => candidate.shadow_value === null && candidate.cost !== null)!;
    const drawn = bar(heatBody(answer), item.key);
    expect(Object.keys(grows(drawn))).toEqual(["cost"]);
    expect(drawn.getAttribute("aria-label")).not.toContain("skulle ha sparat");
  });
});

describe("Support in the settings popover", () => {
  function hass(messages: Record<string, unknown>[]) {
    return {
      language: "sv",
      locale: { language: "sv" },
      states: {},
      user: { is_admin: true },
      callWS: async (message: Record<string, unknown>): Promise<unknown> => {
        messages.push(message);
        switch (message["type"]) {
          case "spotnav/get_house":
            return read("house", "villa_battery_sun.json");
          case "spotnav/get_heat":
            return read("heat_card", "villa.json");
          case "spotnav/get_site_settings_view":
            return read("site_view", "no_charger.json");
          case "spotnav/battery_control":
            return read("battery_control", "needs_ack.json");
          case "spotnav/get_card_info":
            return { api_version: 1, ok: true, error: null, spotnav_version: "3.0.0", card_bundle_hash: "abc" };
          case "spotnav/send_debug_info":
            return { api_version: 1, ok: true, error: null, ref: "SN-TEST", retry_after_s: null };
          case "spotnav/get_debug_bundle":
            return { api_version: 1, ok: true, error: null, bundle: { versions: { spotnav: "3.0.0" } } };
          default:
            return {};
        }
      },
    };
  }

  async function open(tag: string, config: Record<string, unknown>, ctor: CustomElementConstructor) {
    vi.useFakeTimers();
    if (customElements.get(tag) === undefined) {
      customElements.define(tag, ctor);
    }
    const messages: Record<string, unknown>[] = [];
    const element = document.createElement(tag) as HTMLElement & { setConfig(config: object): void; hass: unknown };
    element.setConfig(config);
    element.hass = hass(messages);
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("button[data-action='settings']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    return { root, messages, support: root.querySelector<HTMLElement>("[data-section='support']")! };
  }

  for (const [name, tag, config, ctor] of [
    ["house", HOUSE_CARD_ELEMENT, { type: "custom:spotnav-house-card", site: "site_villa" }, SpotnavHouseCard],
    ["heat", HEAT_CARD_ELEMENT, { type: "custom:spotnav-heat-card", site: "site_villa" }, SpotnavHeatCard],
  ] as const) {
    it(`has send and download rows from the ${name} card, and the send needs no charger`, async () => {
      const { root, messages, support } = await open(tag, config, ctor as unknown as CustomElementConstructor);
      expect(support.querySelector("button[data-debug='download']")!.textContent).toBe("Ladda ner felsökningsinfo");
      const send = support.querySelector<HTMLButtonElement>("button[data-debug='send']")!;
      expect(send.textContent).toBe("Skicka felsökningsinfo");
      send.click();
      const form = root.querySelector<HTMLFormElement>("form[data-send-debug='form']")!;
      form.querySelector<HTMLTextAreaElement>("[data-send-debug-field='message']")!.value = "Något är fel";
      form.querySelector<HTMLInputElement>("[data-send-debug-field='consent']")!.checked = true;
      form.requestSubmit();
      await vi.advanceTimersByTimeAsync(0);
      const sent = messages.find((message) => message["type"] === "spotnav/send_debug_info")!;
      expect(sent["message"]).toBe("Något är fel");
      expect(sent).not.toHaveProperty("charger_id");
      expect((sent["client"] as Record<string, unknown>)["card_bundle_hash"]).toBeDefined();
      expect(root.querySelector("[data-send-debug-sent]")!.getAttribute("data-send-debug-sent")).toBe("SN-TEST");
    });
  }
});

describe("what SpotNav saved in the house history", () => {
  it("has the total and a row per part over the days that have it, and the day's figure on its bar", () => {
    const body = houseBody(house());
    const block = body.querySelector("[data-part='spotnav-saving']")!;
    const rows = [...block.querySelectorAll("[data-row]")].map((row) => row.textContent);
    expect(block.querySelector("[data-row='total']")?.textContent).toContain("21 kr");
    expect(block.querySelector("[data-row='car']")?.textContent).toContain("20 kr");
    // The heat on two of the seven days, said to be an estimate; no battery row (its own control).
    expect(block.querySelector("[data-row='heat']")?.textContent).toContain("Värme (uppskattat)");
    expect(block.querySelector("[data-row='heat']")?.textContent).toContain("2 dygn");
    expect(block.querySelector("[data-row='battery']")).toBeNull();
    expect(rows.length).toBe(3);
    expect(bar(body, "2026-11-12").getAttribute("aria-label") ?? bar(body, "2026-11-12").textContent).toContain("SpotNav sparade 3,3 kr");
  });

  it("is left out without any part, and an older answer without it still decodes", () => {
    expect(houseBody(house((answer) => (answer.saving = null))).querySelector("[data-part='spotnav-saving']")).toBeNull();
    const raw = read("house_history", "villa_month.json");
    delete raw["saving"];
    for (const item of raw["buckets"] as Record<string, unknown>[]) {
      for (const key of ["saving", "saving_battery", "saving_car", "saving_heat"]) {
        delete item[key];
      }
    }
    const decoded = decodeHouseHistory(raw);
    expect(decoded.ok && decoded.value.saving === null && decoded.value.buckets[0]!.saving === null).toBe(true);
  });
});
