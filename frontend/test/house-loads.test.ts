// What the house used (`house-loads.ts`): the decoder on the backend's own fixtures, the pie's slices (small meters
// into Other, the clamp, no meters), its accessible names, and the house card opening it from House in the energy
// flow by a tap or the keyboard, on the front and in the history.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { renderFlow } from "../src/house-flow";
import { decodeHouseLoads, loadsBody, periodCaption, pieModel, SMALL_SHARE, type HouseLoads, type LoadsRequest } from "../src/house-loads";
import { decodeHouse } from "../src/house-model";
import { LANGUAGES, translate } from "../src/i18n";
import { FakeHass } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

function loads(name = "villa_day.json"): HouseLoads {
  const decoded = decodeHouseLoads(read("house_loads", name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

const TODAY: LoadsRequest = { period: "day", date: null, running: true, car: true, heat: true };

describe("decoding the backend's own answer", () => {
  it("reads every fixture and refuses another version or a mistyped field", () => {
    for (const name of ["villa_day.json", "meters_over_house.json", "no_devices.json"]) {
      expect(decodeHouseLoads(read("house_loads", name)).ok).toBe(true);
    }
    const raw = read("house_loads", "villa_day.json");
    expect(decodeHouseLoads({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeHouseLoads({ ...raw, house_kwh: "14" })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeHouseLoads({ ...raw, loads: [{ name: "x", kwh: 1 }] })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeHouseLoads({ ...raw, added: true }).ok).toBe(true);
  });
});

describe("the pie", () => {
  it("draws the meters over 3 % and puts the smaller ones into Other, named under the pie", () => {
    const model = pieModel("en", loads());
    expect(model.whole).toBe(14.62);
    expect(model.slices.map((slice) => slice.name)).toEqual(["Torktumlare", "Diskmaskin", "Tvättmaskin", "Other"]);
    expect(model.merged.map((load) => load.name)).toEqual(["Frys", "Router"]);
    expect(model.merged.every((load) => load.kwh / model.whole < SMALL_SHARE)).toBe(true);
    // Other is the house less every meter, the small ones' energy in it.
    expect(model.slices.at(-1)!.kwh).toBeCloseTo(9.6 + 0.21 + 0.12);
    expect(model.slices.reduce((sum, slice) => sum + slice.kwh, 0)).toBeCloseTo(14.62);
    // A meter's colour follows its statistic id, not its size.
    expect(model.slices[0]!.colour).toBe("var(--spotnav-pie-2)");
    expect(model.slices.at(-1)!.colour).toBe("var(--spotnav-pie-other)");
  });

  it("labels each slice with kWh and %, and names them all for a screen reader", () => {
    const body = loadsBody(document, "en", TODAY, { kind: "ready", answer: loads() });
    const rows = [...body.querySelectorAll("li")].map((row) => row.textContent);
    expect(rows[0]).toBe("Torktumlare2.3 kWh · 16 %");
    expect(rows.at(-1)).toBe("Other9.9 kWh · 68 %");
    const pie = body.querySelector("svg[role='img']")!;
    expect(pie.getAttribute("aria-label")).toBe(
      translate("en", "house.loads.chart", {
        list: "Torktumlare 2.3 kWh (16 %), Diskmaskin 1.4 kWh (10 %), Tvättmaskin 0.96 kWh (7 %), Other 9.9 kWh (68 %)",
      }),
    );
    expect(pie.querySelectorAll("path")).toHaveLength(4);
    expect(body.querySelector("[data-line='merged']")!.textContent).toBe(translate("en", "house.loads.otherHolds", { list: "Frys 0.21 kWh, Router 0.12 kWh" }));
    expect(body.querySelector("[data-line='apart']")!.textContent).toBe(translate("en", "house.loads.carHeat"));
    expect(body.querySelector(".spotnav-house-loads-caption")!.textContent).toBe(translate("en", "house.loads.today"));
    expect(pie.querySelector(".spotnav-house-loads-centre")!.textContent).toBe("14.6 kWh");
  });

  it("clamps Other at 0 with a line when the meters exceed the house", () => {
    const answer = loads("meters_over_house.json");
    const model = pieModel("en", answer);
    expect(model.whole).toBe(43.5);
    expect(model.slices.map((slice) => slice.key)).not.toContain("other");
    const body = loadsBody(document, "en", { ...TODAY, period: "month", running: false, heat: false }, { kind: "ready", answer });
    expect(body.querySelector("[data-line='over']")!.textContent).toBe(translate("en", "house.loads.over", { kwh: "3.5 kWh" }));
    // The middle says the house, the ring the meters that exceed it.
    expect(body.querySelector(".spotnav-house-loads-centre")!.textContent).toBe("40.0 kWh");
    expect(body.querySelector("[data-line='apart']")!.textContent).toBe(translate("en", "house.loads.car"));
  });

  it("shows Other alone and where to add meters when there are none", () => {
    const body = loadsBody(document, "en", { ...TODAY, car: false, heat: false }, { kind: "ready", answer: loads("no_devices.json") });
    expect([...body.querySelectorAll("li")].map((row) => row.dataset["key"])).toEqual(["other"]);
    expect(body.querySelector("[data-line='none']")!.textContent).toBe(translate("en", "house.loads.none"));
    expect(body.querySelector("[data-line='apart']")).toBeNull();
    // One slice is the whole ring.
    expect(body.querySelector("path")).toBeNull();
    expect(body.querySelector("circle")!.dataset["key"]).toBe("other");
  });

  it("says the period in words in every language", () => {
    for (const language of LANGUAGES) {
      expect(periodCaption(language, { period: "month", date: "2026-11-01" }, false)).toMatch(/2026/);
      expect(periodCaption(language, { period: "year", date: "2026-01-01" }, true)).toBe("2026");
      expect(periodCaption(language, { period: "day", date: "2026-11-17" }, true)).toBe(translate(language, "house.loads.today"));
      for (const state of [{ kind: "loading" } as const, { kind: "ready", answer: loads() } as const]) {
        expect(loadsBody(document, language, TODAY, state).textContent).not.toMatch(/house\.loads\./);
      }
    }
  });
});

describe("House in the flow", () => {
  const flow = (() => {
    const decoded = decodeHouse(read("house", "villa_battery_sun.json"));
    if (!decoded.ok || decoded.value.energy_flow === null) {
      throw new Error("the fixture must decode");
    }
    return decoded.value.energy_flow;
  })();

  it("is a named button only when the card can open the pie", () => {
    expect(renderFlow(document, "en", flow, 340, "t")!.querySelector("[role='button']")).toBeNull();
    const opened = vi.fn();
    const diagram = renderFlow(document, "en", flow, 340, "t", false, opened)!;
    const buttons = diagram.querySelectorAll<SVGGElement>("[role='button']");
    expect(buttons).toHaveLength(1);
    const house = buttons[0]!;
    expect(house.dataset["role"]).toBe("house");
    expect(house.getAttribute("tabindex")).toBe("0");
    expect(house.getAttribute("aria-label")).toBe(translate("en", "house.loads.open", { figures: "House 16.7 kWh (18 %)" }));
    house.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    house.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    house.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    house.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(opened).toHaveBeenCalledTimes(3);
    expect(opened).toHaveBeenCalledWith(house);
  });
});

describe("the house card", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
      customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
    }
  });

  afterEach(() => {
    document.body.replaceChildren();
    vi.useRealTimers();
  });

  async function mounted(hass: FakeHass): Promise<{ element: SpotnavHouseCard; root: ShadowRoot }> {
    const element = document.createElement(HOUSE_CARD_ELEMENT) as SpotnavHouseCard;
    element.setConfig({ type: "custom:spotnav-house-card", site: "site_villa" });
    element.hass = hass.snapshot("s", "en");
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house", "villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    return { element, root: element.shadowRoot! };
  }

  it("opens the pie for today from a tap, reads it once and gives focus back on close", async () => {
    const hass = new FakeHass();
    const { element, root } = await mounted(hass);
    const house = root.querySelector<SVGGElement>("[data-part='energy-flow'] [role='button']")!;
    expect(root.textContent).toContain(translate("en", "house.loads.tap"));
    house.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_house_loads", api_version: 1, site_id: "site_villa", period: "day" });
    const dialog = [...root.querySelectorAll<HTMLElement>("[role='dialog']")].find((item) => item.textContent!.includes(translate("en", "house.loads.title")))!;
    expect(dialog.textContent).toContain(translate("en", "house.loads.reading"));
    hass.resolveNext(read("house_loads", "villa_day.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(dialog.querySelectorAll("li")).toHaveLength(4);
    expect(dialog.querySelector("svg[role='img']")).not.toBeNull();
    dialog.querySelector<HTMLButtonElement>("button[aria-label]")!.click();
    expect(root.activeElement).toBe(house);
    element.remove();
  });

  it("opens from the keyboard, and says so when the backend is older", async () => {
    const hass = new FakeHass();
    const { element, root } = await mounted(hass);
    const house = root.querySelector<SVGGElement>("[data-part='energy-flow'] [role='button']")!;
    house.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    hass.rejectNext(Object.assign(new Error("unknown"), { code: "unknown_command" }));
    await vi.advanceTimersByTimeAsync(0);
    expect(root.textContent).toContain(translate("en", "house.loads.unsupported"));
    element.remove();
  });

  it("opens from the history's flow for the shown period and returns to the history", async () => {
    const hass = new FakeHass();
    const { element, root } = await mounted(hass);
    root.querySelector<HTMLButtonElement>("button[data-history='open']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house_history", "villa_month.json"));
    await vi.advanceTimersByTimeAsync(0);
    const history = root.querySelector<HTMLElement>("[role='dialog']")!;
    const house = history.querySelector<SVGGElement>("[data-part='energy-flow'] [role='button']")!;
    house.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    const month = read("house_history", "villa_month.json");
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_house_loads", api_version: 1, site_id: "site_villa", period: "month", date: month["date"] });
    // The history steps aside while the pie is open.
    expect(history.closest<HTMLElement>("[hidden]")).not.toBeNull();
    hass.resolveNext(read("house_loads", "meters_over_house.json"));
    await vi.advanceTimersByTimeAsync(0);
    const pie = [...root.querySelectorAll<HTMLElement>("[role='dialog']")].find((item) => item !== history)!;
    expect(pie.querySelector("[data-line='over']")).not.toBeNull();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await vi.advanceTimersByTimeAsync(0);
    expect(history.closest<HTMLElement>("[hidden]")).toBeNull();
    expect(root.activeElement?.getAttribute("role")).toBe("button");
    expect((root.activeElement as HTMLElement).dataset["role"]).toBe("house");
    // The history is shown again without being read again.
    expect(hass.messages.filter((message) => message.type === "spotnav/get_house_history")).toHaveLength(1);
    element.remove();
  });
});
