// The heat card (SpotNav 3.0 preview): the strict decoder on the backend's own fixtures, the body's dial, lines,
// band and notes, the settings section, and the element reading `spotnav/get_heat` and writing `spotnav/update_heat`.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { HEAT_CARD_ELEMENT, instantToLocal, localToInstant, parseHeatConfig, SpotnavHeatCard, writeError } from "../src/heat-card";
import { decodeHeat, decodeHeatWrite, type HeatAnswer } from "../src/heat-model";
import { heatBody, heatSettingsSection, measuredLine, noteLines, pumpLine, todayLine, wouldLine, type HeatState } from "../src/heat-view";
import { registerTranslation } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { defineCard, HEAT_PICKER_ENTRY } from "../src/index";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "heat_card");
const SITE_FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as Record<string, unknown>;
}

function answer(name = "villa.json"): HeatAnswer {
  const decoded = decodeHeat(read(name));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

/** The fixtures' moment: 16 November 2026, 17:20 in Stockholm. */
const NOW = Date.parse("2026-11-16T16:20:00+00:00");

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

function body(value: HeatAnswer, language: "en" | "sv" = "sv", handlers = { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn() }) {
  const state: HeatState = { kind: "ready", answer: value };
  return heatBody({ doc: document, language, state, ui: { pendingTarget: null, error: null }, handlers, width: 330, nowMs: NOW });
}

describe("decoding the backend's own answer", () => {
  it("reads both fixtures", () => {
    const villa = answer();
    expect(villa.band.hours).toHaveLength(24);
    expect(villa.band.hours[0]!.startMs).toBe(Date.parse("2026-11-15T23:00:00+00:00"));
    expect(answer("network_pump.json").notes.map((note) => note.code)).toEqual(["pump_price_control", "readings_disabled", "other_controller"]);
  });

  it("refuses an unknown key, a missing one, a mistyped one and another version", () => {
    const raw = read("villa.json");
    expect(decodeHeat({ ...raw, extra: 1 })).toEqual({ ok: false, failure: "malformed" });
    const comfort = { ...(raw["comfort"] as Record<string, unknown>), surprise: true };
    expect(decodeHeat({ ...raw, comfort }).ok).toBe(false);
    const { today: _today, ...missing } = raw;
    expect(decodeHeat(missing).ok).toBe(false);
    expect(decodeHeat({ ...raw, writable: "yes" }).ok).toBe(false);
    expect(decodeHeat({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    const hours = (raw["band"] as { hours: Record<string, unknown>[] }).hours.map((hour, index) => (index === 3 ? { ...hour, start: "soon" } : hour));
    expect(decodeHeat({ ...raw, band: { ...(raw["band"] as object), hours } }).ok).toBe(false);
  });

  it("reads a write's answer", () => {
    const config = read("villa.json")["config"];
    expect(decodeHeatWrite({ api_version: 1, ok: true, error: null, field_errors: [], config }).ok).toBe(true);
    expect(decodeHeatWrite({ api_version: 1, ok: false, error: "spotnav_conflict", field_errors: [], config, more: 1 }).ok).toBe(false);
  });
});

describe("the front", () => {
  it("shows the dial, the coldest room, the pump and what SpotNav would do", () => {
    const villa = answer();
    const card = body(villa);
    expect(card.querySelector(".spotnav-heat-dial-figure")!.textContent).toBe("21,0°");
    expect(card.querySelector("[data-line='band']")!.textContent).toBe("21,0 °C · 20,5–21,5");
    expect(card.querySelector("[data-measured]")).not.toBeNull();
    expect(measuredLine("sv", villa)).toBe("20,4 °C · kallast: Toalett NV temperatur · Ute 6,2 °C");
    expect(pumpLine("sv", villa)).toBe("Värmepumpen värmer · 1,1 kW");
    expect(wouldLine("sv", villa)).toBe("Skulle sänka till 20,5 °C till 20:00: dyra timmar");
    expect(card.querySelector("[data-line='shadow']")!.textContent).toBe("Temperaturen och Hemma/Borta som du ställer in här följer SpotNav när den börjar styra värmen.");
    expect(card.querySelector("button[data-action='test']")!.textContent).toBe("Test · styr inget än");
    expect(card.querySelector("button[data-mode='home']")!.getAttribute("aria-pressed")).toBe("true");
    expect(card.querySelector("button[data-action='settings']")).not.toBeNull();
  });

  it("draws the day's band with the hot water, the car and the now line", () => {
    const card = body(answer());
    const cells = [...card.querySelectorAll<SVGRectElement>(".spotnav-heat-band-cell")];
    expect(cells).toHaveLength(24);
    expect(cells[17]!.dataset["action"]).toBe("lower");
    expect(cells[0]!.dataset["action"]).toBe("night");
    expect(card.querySelectorAll("[data-mark='hot_water']")).toHaveLength(2);
    expect(card.querySelectorAll("[data-mark='car']")).toHaveLength(4);
    expect(card.querySelector(".spotnav-heat-band-now")).not.toBeNull();
    const legend = [...card.querySelectorAll(".spotnav-heat-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual(["Sänkt", "Förvärmning", "Natt", "Varmvatten", "Bilen laddar"]);
  });

  it("says today's heat and hot water and their cost", () => {
    expect(todayLine("sv", answer())).toBe("I dag · värme 13,9 kWh · varmvatten 1,7 kWh · 19 kr");
    expect(todayLine("en", answer("network_pump.json"))).toBeNull();
  });

  it("shows away, the notes, and no settings button for a household member", () => {
    const pump = answer("network_pump.json");
    const card = body(pump);
    expect(card.querySelector("button[data-mode='away']")!.getAttribute("aria-pressed")).toBe("true");
    expect(card.querySelector("[data-line='away']")!.textContent).toBe("Borta till fre 20 nov. 16:00Ändra");
    expect(card.querySelector("button[data-action='settings']")).toBeNull();
    expect(noteLines("sv", pump).map((note) => note.text)).toEqual([
      "NIBE F750 styr själv efter elpriset. Stäng av det i värmepumpen (Smart Price Adaption) om SpotNav ska styra.",
      "Mätvärden som SpotNav behöver är avstängda i värmepumpens integration: gradminuter. Slå på dem på enheten.",
      "EffektGuard styr värmepumpen; SpotNav räknar den som huslast och tittar bara på.",
    ]);
    expect(pumpLine("en", pump)).toBe("Heat pump: heating");
    expect(measuredLine("en", pump)).toBe("No indoor sensor chosen · Outdoors 4.5 °C");
    expect(wouldLine("en", pump)).toBe("Would keep 17.5 °C until Fri 20 Nov 14:00 while you are away");
  });

  it("keeps every language's words", () => {
    for (const language of ["en", "sv"] as const) {
      expect(body(answer(), language).textContent).not.toContain("heat.");
    }
  });
});

describe("the settings section", () => {
  it("lists each setting with its value", () => {
    const edits: string[] = [];
    const section = heatSettingsSection(document, "sv", answer(), (key) => edits.push(key));
    const rows = [...section.querySelectorAll<HTMLElement>("[data-row]")].map((row) => [row.dataset["row"], row.textContent]);
    expect(rows).toEqual([
      ["indoor", "Inomhusgivare3 valda"],
      ["outdoor", "UtegivareAutomatisk"],
      ["meter", "Värmepumpens mätareElmätare värme (Nibe + panna) Effekt"],
      ["path", "StyrningIngen än"],
      ["hotWater", "VarmvattenIngen"],
      ["zones", "Zoner4"],
      ["zone:climate.golvvarme_toalett_nere", "Golvvärme toalett nedervåning0 °C"],
      ["zone:climate.golvvarme_toalett_uppe", "Golvvärme toalett ovanvåning0 °C"],
      ["zone:climate.golvvarme_tvattstuga", "Golvvärme tvättstuga−1,0 °C"],
      ["zone:climate.ovanvaning", "Värmepump ovanvåning−0,5 °C"],
      ["night", "Svalare natt−1,0 °C 23:00–06:00"],
    ]);
    section.querySelector<HTMLButtonElement>("button[data-edit='zone:climate.ovanvaning']")!.click();
    expect(edits).toEqual(["zone:climate.ovanvaning"]);
    expect(section.querySelector("[data-help='zone:climate.ovanvaning']")!.textContent).toBe(
      "Luft-luftvärmepump: SpotNav ändrar bara börvärdet, slår aldrig av eller på.",
    );
  });
});

describe("the words of a refused write and the away time", () => {
  it("words each refusal", () => {
    expect(writeError("sv", "spotnav_conflict", [])).toBe("Någon annan ändrade samtidigt; det senaste visas.");
    expect(writeError("sv", "spotnav_invalid_value", ["not_a_step"])).toBe("Välj halva grader.");
    expect(writeError("en", "spotnav_invalid_value", ["away_in_past"])).toBe("Choose a time ahead.");
    expect(writeError("en", "spotnav_invalid_value", ["entity_not_found"])).toBe("Could not save.");
  });

  it("turns a local return time into an instant and back, across the clock change", () => {
    const winter = localToInstant("2026-11-20", "16:00", "Europe/Stockholm");
    expect(new Date(winter!).toISOString()).toBe("2026-11-20T15:00:00.000Z");
    const summer = localToInstant("2027-06-20", "16:00", "Europe/Stockholm");
    expect(new Date(summer!).toISOString()).toBe("2027-06-20T14:00:00.000Z");
    expect(instantToLocal(winter!, "Europe/Stockholm")).toEqual({ date: "2026-11-20", time: "16:00" });
    expect(localToInstant("20/11", "16:00", "Europe/Stockholm")).toBeNull();
  });
});

describe("the element", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: NOW });
    defineCard();
  });
  afterEach(() => {
    vi.useRealTimers();
    document.body.replaceChildren();
  });

  function fakeHass(language = "sv") {
    const messages: Record<string, unknown>[] = [];
    let current = read("villa.json");
    const hass = {
      language,
      locale: { language },
      states: {},
      user: { is_admin: true },
      callWS: async (message: Record<string, unknown>): Promise<unknown> => {
        messages.push(message);
        if (message["type"] === "spotnav/get_heat") {
          return current;
        }
        if (message["type"] === "spotnav/get_site_settings_view") {
          return JSON.parse(readFileSync(join(SITE_FIXTURES, "site_view", "villa.json"), "utf8"));
        }
        if (message["type"] === "spotnav/battery_control") {
          return JSON.parse(readFileSync(join(SITE_FIXTURES, "battery_control", "needs_ack.json"), "utf8"));
        }
        if (message["type"] === "spotnav/update_heat") {
          const changes = message["changes"] as Record<string, unknown>;
          current = { ...current, config: { ...(current["config"] as object), ...changes } };
          return { api_version: 1, ok: true, error: null, field_errors: [], config: current["config"] };
        }
        return {};
      },
    };
    return { hass, messages };
  }

  async function mount(language = "sv") {
    const { hass, messages } = fakeHass(language);
    const element = document.createElement(HEAT_CARD_ELEMENT) as SpotnavHeatCard;
    element.setConfig({ type: "custom:spotnav-heat-card", site: "site_villa" });
    element.hass = hass as never;
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    return { element, messages, root: element.shadowRoot! };
  }

  it("is in the card picker", () => {
    expect(window.customCards?.some((entry) => entry.type === HEAT_PICKER_ENTRY.type)).toBe(true);
    expect(parseHeatConfig({ type: "custom:spotnav-heat-card", charger: "c1" })).toEqual({ type: "custom:spotnav-heat-card", charger: "c1" });
    expect(() => parseHeatConfig({ site: 3 })).toThrow();
  });

  it("writes a run of dial taps once, against the target it last saw", async () => {
    const { messages, root } = await mount();
    expect(messages[0]).toEqual({ type: "spotnav/get_heat", api_version: 1, site_id: "site_villa" });
    root.querySelector<HTMLButtonElement>("button[data-step='up']")!.click();
    root.querySelector<HTMLButtonElement>("button[data-step='up']")!.click();
    expect(root.querySelector(".spotnav-heat-dial-figure")!.textContent).toBe("22,0°");
    await vi.advanceTimersByTimeAsync(800);
    const writes = messages.filter((message) => message["type"] === "spotnav/update_heat");
    expect(writes).toEqual([
      { type: "spotnav/update_heat", api_version: 1, site_id: "site_villa", changes: { target_c: 22 }, expected: { target_c: 21 } },
    ]);
  });

  it("goes away through the away editor and comes home with one tap", async () => {
    const { messages, root } = await mount();
    root.querySelector<HTMLButtonElement>("button[data-mode='away']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor='away']")!;
    expect(form.querySelector<HTMLInputElement>("input[data-field='date']")!.value).toBe("2026-11-17");
    form.querySelector<HTMLInputElement>("input[data-field='date']")!.value = "2026-11-20";
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    const away = messages.find((message) => message["type"] === "spotnav/update_heat")!;
    expect(away["changes"]).toEqual({ mode: "away", away_until: "2026-11-20T15:00:00.000Z" });
    expect(away["expected"]).toEqual({ mode: "home", away_until: null });
  });

  it("opens the settings, edits the indoor sensors and writes them", async () => {
    const { messages, root } = await mount();
    root.querySelector<HTMLButtonElement>("button[data-action='settings']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("button[data-edit='indoor']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor='multi']")!;
    const boxes = [...form.querySelectorAll<HTMLInputElement>("input[type='checkbox']")];
    expect(boxes.map((box) => box.checked)).toEqual([true, true, true, false]);
    boxes[3]!.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    const write = messages.find((message) => message["type"] === "spotnav/update_heat")!;
    expect(write["changes"]).toEqual({
      indoor_sensors: ["sensor.inomhus_kok_temperatur", "sensor.ovanvaning_temperatur", "sensor.toalett_nv_temperatur", "sensor.garage_temperatur"],
    });
    // Back on the shared popover's heat section after the write, the other sections beside it.
    expect(root.querySelector("button[data-edit='indoor']")).not.toBeNull();
    expect(root.querySelector("[data-popover='site'] [data-section='battery']")).not.toBeNull();
  });

  it("refuses an offset off the half degree before writing", async () => {
    const { messages, root } = await mount();
    root.querySelector<HTMLButtonElement>("button[data-action='settings']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("button[data-edit='zone:climate.ovanvaning']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor='number']")!;
    const input = form.querySelector<HTMLInputElement>("input")!;
    input.value = "0,3";
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.some((message) => message["type"] === "spotnav/update_heat")).toBe(false);
    input.value = "1";
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    const write = messages.find((message) => message["type"] === "spotnav/update_heat")!;
    expect((write["changes"] as { zones: { entity_id: string; offset_c: number }[] }).zones[3]).toEqual({ entity_id: "climate.ovanvaning", offset_c: 1 });
  });
});
