// What the house used (`house-loads.ts`): the decoder on the backend's own fixtures, the pie's slices (small meters
// into Other, the clamp, no meters), its accessible names, and the house card opening it from House in the energy
// flow by a tap or the keyboard, on the front and in the history.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { renderFlow } from "../src/house-flow";
import {
  decodeHouseLoads,
  loadsBody,
  LoadsPopover,
  periodCaption,
  pieModel,
  SLOTS,
  SMALL_SHARE,
  type HouseLoads,
  type LoadsControls,
  type LoadsRequest,
} from "../src/house-loads";
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
    for (const name of ["villa_day.json", "meters_over_house.json", "no_devices.json", "heat_villa_day.json", "moved_and_implausible_month.json"]) {
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
  it("draws the meters over 1.5 %, puts the smaller ones into Other metered and the rest into Unknown", () => {
    const model = pieModel("en", loads());
    expect(model.whole).toBe(14.62);
    expect(model.slices.map((slice) => slice.name)).toEqual(["Torktumlare", "Diskmaskin", "Tvättmaskin", "Other metered", "Unknown"]);
    expect(model.merged.map((load) => load.name)).toEqual(["Frys", "Router"]);
    expect(model.merged.every((load) => load.kwh / model.whole < SMALL_SHARE)).toBe(true);
    // Other metered is the small meters; Unknown is the house less every meter.
    expect(model.slices.at(-2)!.kwh).toBeCloseTo(0.21 + 0.12);
    expect(model.slices.at(-1)!.kwh).toBeCloseTo(9.6);
    expect(model.slices.reduce((sum, slice) => sum + slice.kwh, 0)).toBeCloseTo(14.62);
    // A meter's colour follows its statistic id, not its size.
    expect(model.slices[0]!.colour).toBe("var(--spotnav-pie-2)");
    expect(model.slices.at(-2)!.colour).toBe("var(--spotnav-pie-other-metered)");
    expect(model.slices.at(-1)!.colour).toBe("var(--spotnav-pie-unknown)");
  });

  const meters = (kwhs: number[], house: number): HouseLoads => {
    const base = loads();
    const list = kwhs.map((kwh, index) => ({ changed: false, kwh, name: `M${index}`, source: "meter", statistic_id: `sensor.m${index}` }));
    return { ...base, house_kwh: house, meters_kwh: kwhs.reduce((a, b) => a + b, 0), other_kwh: Math.max(0, house - kwhs.reduce((a, b) => a + b, 0)), loads: list } as HouseLoads;
  };

  it("puts the line at 1.5 % of the whole", () => {
    // 100 kWh house: 1.5 is drawn, 1.49 is not.
    const model = pieModel("en", meters([1.5, 1.49, 0], 100));
    expect(model.slices.map((slice) => slice.name)).toEqual(["M0", "Other metered", "Unknown"]);
    expect(model.merged.map((load) => load.name)).toEqual(["M1", "M2"]);
  });

  it("names at most eight meters and puts the rest into Other metered", () => {
    expect(SLOTS).toBe(8);
    const model = pieModel("en", meters([10, 9, 8, 7, 6, 5, 4, 3, 2.5, 2], 100));
    expect(model.slices.filter((slice) => slice.key.startsWith("sensor."))).toHaveLength(8);
    expect(model.merged.map((load) => load.name)).toEqual(["M8", "M9"]);
    expect(model.slices.at(-2)!.kwh).toBeCloseTo(4.5);
    expect(new Set(model.slices.map((slice) => slice.colour)).size).toBe(model.slices.length);
  });

  it("labels each slice with kWh and %, and names them all for a screen reader", () => {
    const body = loadsBody(document, "en", TODAY, { kind: "ready", answer: loads() });
    const rows = [...body.querySelectorAll("li")].map((row) => row.textContent);
    expect(rows[0]).toBe("Torktumlare2.3 kWh · 16 %");
    expect(rows.at(-2)).toBe("Other metered0.33 kWh · 2 %");
    expect(rows.at(-1)).toBe("Unknown9.6 kWh · 66 %");
    const pie = body.querySelector("svg[role='img']")!;
    expect(pie.getAttribute("aria-label")).toBe(
      translate("en", "house.loads.chart", {
        list: "Torktumlare 2.3 kWh (16 %), Diskmaskin 1.4 kWh (10 %), Tvättmaskin 0.96 kWh (7 %), Other metered 0.33 kWh (2 %), Unknown 9.6 kWh (66 %)",
      }),
    );
    expect(pie.querySelectorAll("path")).toHaveLength(5);
    expect(body.querySelector("[data-line='merged']")!.textContent).toBe(translate("en", "house.loads.otherHolds", { list: "Frys 0.21 kWh, Router 0.12 kWh" }));
    expect(body.querySelector("[data-line='apart']")!.textContent).toBe(translate("en", "house.loads.carHeat"));
    expect(body.querySelector(".spotnav-house-loads-caption")!.textContent).toBe(translate("en", "house.loads.today"));
    expect(pie.querySelector(".spotnav-house-loads-centre")!.textContent).toBe("14.6 kWh");
  });

  it("clamps Unknown at 0 with a line when the meters exceed the house", () => {
    const answer = loads("meters_over_house.json");
    const model = pieModel("en", answer);
    expect(model.whole).toBe(43.5);
    expect(model.slices.map((slice) => slice.key)).not.toContain("unknown");
    const body = loadsBody(document, "en", { ...TODAY, period: "month", running: false, heat: false }, { kind: "ready", answer });
    expect(body.querySelector("[data-line='over']")!.textContent).toBe(translate("en", "house.loads.over", { kwh: "3.5 kWh" }));
    // The middle says the house, the ring the meters that exceed it.
    expect(body.querySelector(".spotnav-house-loads-centre")!.textContent).toBe("40.0 kWh");
    expect(body.querySelector("[data-line='apart']")!.textContent).toBe(translate("en", "house.loads.car"));
  });

  it("shows Unknown alone and where to add meters when there are none", () => {
    const body = loadsBody(document, "en", { ...TODAY, car: false, heat: false }, { kind: "ready", answer: loads("no_devices.json") });
    expect([...body.querySelectorAll("li")].map((row) => row.dataset["key"])).toEqual(["unknown"]);
    expect(body.querySelector("[data-line='none']")!.textContent).toBe(translate("en", "house.loads.none"));
    expect(body.querySelector("[data-line='apart']")).toBeNull();
    // One slice is the whole ring.
    expect(body.querySelector("path")).toBeNull();
    expect(body.querySelector("circle")!.dataset["key"]).toBe("unknown");
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

describe("the heat pie, where a meter belongs and meters that jump", () => {
  const controls = (overrides: Partial<LoadsControls> = {}): LoadsControls => ({
    chosen: null,
    saving: false,
    failure: null,
    onChoose: vi.fn(),
    onBelong: vi.fn(),
    ...overrides,
  });

  it("decodes an older answer as the house's with nothing moved", () => {
    const raw = read("house_loads", "villa_day.json");
    const { kind: _kind, moved: _moved, belongs: _belongs, implausible: _implausible, ...older } = raw;
    const decoded = decodeHouseLoads({ ...older, loads: (raw["loads"] as Record<string, unknown>[]).map(({ statistic_id, name, kwh }) => ({ statistic_id, name, kwh })) });
    expect(decoded.ok && decoded.value.kind).toBe("house");
    expect(decoded.ok && [decoded.value.moved, decoded.value.belongs, decoded.value.implausible]).toEqual([[], {}, []]);
    expect(decoded.ok && decoded.value.loads[0]!.source).toBe("meter");
    expect(decodeHouseLoads({ ...raw, kind: "garage" }).ok).toBe(false);
    expect(decodeHouseLoads({ ...raw, moved: [{ statistic_id: "a", name: "A", belongs: "kitchen" }] }).ok).toBe(false);
  });

  it("shows the heat pump and each zone by name, Heat in its middle, and no house lines", () => {
    const body = loadsBody(document, "sv", { ...TODAY, kind: "heat" }, { kind: "ready", answer: loads("heat_villa_day.json") });
    const names = [...body.querySelectorAll(".spotnav-house-loads-name")].map((name) => name.textContent);
    expect(names).toEqual([
      "Elmätare värme (Nibe + panna) Effekt",
      "Golvvärme tvättstuga",
      "Golvvärme toalett nedervåning",
      `Shelly golvvärme hall${translate("sv", "house.loads.changed")}`,
    ]);
    expect(body.querySelector(".spotnav-house-loads-centre-small")!.textContent).toBe(translate("sv", "house.flow.heat"));
    expect(body.querySelector("[data-line='apart']")).toBeNull();
    expect(body.querySelector("[data-line='none']")).toBeNull();
    // A household member sees no menu.
    expect(body.querySelector("button")).toBeNull();
    // An administrator may move only the dashboard's meter, never the heat pump or a zone.
    const admin = loadsBody(document, "sv", { ...TODAY, kind: "heat" }, { kind: "ready", answer: loads("heat_villa_day.json") }, controls());
    expect([...admin.querySelectorAll<HTMLButtonElement>("button[data-choose]")].map((button) => button.dataset["choose"])).toEqual(["sensor.shelly_golv_energy"]);
  });

  it("offers House (default), Heat, Car and Don't count for a meter, by its slice or its row", () => {
    const answer = loads("moved_and_implausible_month.json");
    const onChoose = vi.fn();
    const closed = loadsBody(document, "en", TODAY, { kind: "ready", answer }, controls({ onChoose }));
    const row = closed.querySelector<HTMLButtonElement>("button[data-choose='sensor.diskmaskin_energi']")!;
    expect(row.getAttribute("aria-expanded")).toBe("false");
    row.click();
    expect(onChoose).toHaveBeenLastCalledWith("sensor.diskmaskin_energi");
    closed.querySelector<SVGPathElement>("path[data-key='sensor.torktumlare_energi']")!.dispatchEvent(new MouseEvent("click"));
    expect(onChoose).toHaveBeenLastCalledWith("sensor.torktumlare_energi");
    const onBelong = vi.fn();
    const open = loadsBody(document, "en", TODAY, { kind: "ready", answer }, controls({ chosen: "sensor.diskmaskin_energi", onBelong }));
    const options = [...open.querySelectorAll<HTMLButtonElement>("[data-part='belongs'] button")];
    expect(options.map((button) => [button.textContent, button.getAttribute("aria-pressed")])).toEqual([
      [translate("en", "house.loads.belongHouse"), "true"],
      [translate("en", "house.loads.belongHeat"), "false"],
      [translate("en", "house.loads.belongCar"), "false"],
      [translate("en", "house.loads.belongNone"), "false"],
    ]);
    options[3]!.click();
    expect(onBelong).toHaveBeenCalledWith("sensor.diskmaskin_energi", "none");
    // A meter too small for its own slice is moved from its name under the pie.
    const small = loadsBody(document, "en", TODAY, { kind: "ready", answer: loads() }, controls());
    expect([...small.querySelectorAll<HTMLButtonElement>("[data-line='merged-choose'] button")].map((button) => button.textContent)).toEqual(["Frys", "Router"]);
    const saving = loadsBody(document, "en", TODAY, { kind: "ready", answer }, controls({ chosen: "sensor.diskmaskin_energi", saving: true }));
    expect([...saving.querySelectorAll<HTMLButtonElement>("[data-part='belongs'] button")].every((button) => button.disabled)).toBe(true);
  });

  it("lists the meters moved out of the house with a way back, and the jumping meter's line", () => {
    const answer = loads("moved_and_implausible_month.json");
    const onBelong = vi.fn();
    const body = loadsBody(document, "sv", TODAY, { kind: "ready", answer }, controls({ onBelong }));
    const moved = body.querySelector("[data-line='moved']")!;
    expect([...moved.querySelectorAll("li > span")].map((item) => item.textContent)).toEqual([
      translate("sv", "house.loads.movedHeat", { name: "Shelly golvvärme hall" }),
      translate("sv", "house.loads.movedNone", { name: "UPS" }),
    ]);
    moved.querySelector<HTMLButtonElement>("button[data-back='sensor.ups_energy']")!.click();
    expect(onBelong).toHaveBeenCalledWith("sensor.ups_energy", "house");
    expect(body.querySelector("[data-line='implausible']")!.textContent).toBe(
      "NAS: 12 timmar med orimliga värden räknas inte (mätaren hoppar). Rätta statistiken i Home Assistant under Utvecklarverktyg → Statistik.",
    );
    // A household member sees the list without the buttons.
    const member = loadsBody(document, "sv", TODAY, { kind: "ready", answer });
    expect(member.querySelector("[data-line='moved'] button")).toBeNull();
    const out = { ...answer, implausible: [{ ...answer.implausible[0]!, left_out: true }] };
    expect(loadsBody(document, "en", TODAY, { kind: "ready", answer: out }).querySelector("[data-line='implausible']")!.textContent).toBe(
      translate("en", "house.loads.implausibleOut", { name: "NAS" }),
    );
  });

  it("writes an administrator's choice compare-and-set and reads the pie again", async () => {
    vi.useFakeTimers();
    try {
      const hass = new FakeHass();
      const snapshot = hass.snapshot("s", "en");
      snapshot.user = { is_admin: true };
      const owner = document.createElement("div");
      document.body.append(owner);
      const popover = new LoadsPopover({
        doc: document,
        owner,
        background: () => null,
        idPrefix: "t",
        hass: () => snapshot,
        target: () => ({ site: "site_villa" }),
        language: () => "en",
      });
      popover.open({ ...TODAY, kind: "house", period: "month", date: "2026-10-01", running: false }, null);
      await vi.advanceTimersByTimeAsync(0);
      hass.resolveNext(read("house_loads", "moved_and_implausible_month.json"));
      await vi.advanceTimersByTimeAsync(0);
      owner.querySelector<HTMLButtonElement>("button[data-choose='sensor.diskmaskin_energi']")!.click();
      owner.querySelector<HTMLButtonElement>("[data-part='belongs'] button[data-belongs='heat']")!.click();
      await vi.advanceTimersByTimeAsync(0);
      expect(hass.messages.at(-1)).toEqual({
        type: "spotnav/update_site_settings",
        api_version: 1,
        site_id: "site_villa",
        expected: { meter_belongs: { "sensor.shelly_golv_energy": "heat", "sensor.ups_energy": "none" } },
        changes: { meter_belongs: { "sensor.diskmaskin_energi": "heat" } },
      });
      hass.resolveNext({ api_version: 1, ok: false, error: "spotnav_conflict", site: null, restore: null });
      await vi.advanceTimersByTimeAsync(0);
      expect(owner.querySelector("[data-line='write']")!.textContent).toBe(translate("en", "house.loads.writeConflict"));
      expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_house_loads", api_version: 1, site_id: "site_villa", period: "month", date: "2026-10-01" });
      popover.destroy();
      owner.remove();
    } finally {
      vi.useRealTimers();
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
    expect(opened).toHaveBeenCalledWith(house, "house");
  });

  it("makes Heat a button too only when the heat holds more than one source", () => {
    const heatFlow = {
      ...flow,
      nodes: [...flow.nodes.filter((node) => node.role !== "heat"), { id: "use.heat", side: "use" as const, role: "heat", kwh: 6, soc_pct: null }],
      links: [...flow.links.filter((link) => link.to !== "use.heat"), { from: "source.grid", to: "use.heat", kwh: 6 }],
    };
    const opened = vi.fn();
    const one = renderFlow(document, "en", { ...heatFlow, heat_sources: 1 }, 340, "t", false, opened)!;
    expect([...one.querySelectorAll<SVGGElement>("[role='button']")].map((button) => button.dataset["role"])).toEqual(["house"]);
    const two = renderFlow(document, "en", { ...heatFlow, heat_sources: 3 }, 340, "t", false, opened)!;
    const heat = two.querySelector<SVGGElement>("[role='button'][data-role='heat']")!;
    expect(heat.getAttribute("aria-label")).toContain(translate("en", "house.loads.openHeat", { figures: "" }).slice(2));
    heat.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(opened).toHaveBeenCalledWith(heat, "heat");
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
    // Today's energy flow is in the Energy flow tab.
    element.shadowRoot!.querySelector<HTMLButtonElement>("[role='tab'][data-tab='flow']")!.click();
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
    expect(dialog.querySelectorAll("li")).toHaveLength(5);
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
