// The heat card (SpotNav 3.0 preview): the strict decoder on the backend's own fixtures, the body's dial, lines,
// band and notes, the settings section, and the element reading `spotnav/get_heat` and writing `spotnav/update_heat`.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { HEAT_CARD_ELEMENT, instantToLocal, localToInstant, parseHeatConfig, SpotnavHeatCard, writeError } from "../src/heat-card";
import { decodeHeat, decodeHeatWrite, type HeatAnswer } from "../src/heat-model";
import { heatEditor } from "../src/heat-editors";
import { controlLine, heatBody, heatSettingsSection, measuredLine, noteLines, pumpLine, relayLine, reviewLines, todayLine, wouldLine, type HeatState } from "../src/heat-view";
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

function body(value: HeatAnswer, language: "en" | "sv" = "sv", handlers = { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn() }, tab: "now" | "plan" | "use" = "now") {
  const state: HeatState = { kind: "ready", answer: value };
  return heatBody({ doc: document, language, state, ui: { pendingTarget: null, error: null, tab }, handlers, width: 330, nowMs: NOW });
}

/** The Plan tab: the day's chart. */
function planBody(value: HeatAnswer, language: "en" | "sv" = "sv", handlers = { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn() }) {
  return body(value, language, handlers, "plan");
}

describe("decoding the backend's own answer", () => {
  it("reads both fixtures", () => {
    const villa = answer();
    expect(villa.band.hours).toHaveLength(24);
    expect(villa.band.hours[0]!.startMs).toBe(Date.parse("2026-11-15T23:00:00+00:00"));
    expect(answer("network_pump.json").notes.map((note) => note.code)).toEqual(["pump_price_control", "readings_disabled", "other_controller"]);
  });

  it("reads SpotNav's own control and refuses an unknown key at any depth of every fixture", () => {
    const controlled = answer("controlled.json");
    expect(controlled.control!.state).toBe("running");
    expect(controlled.band.hours[7]!.executed_min).toBe(60);
    for (const name of ["villa.json", "network_pump.json", "controlled.json", "sg_api_pump.json"]) {
      const raw = read(name);
      const paths: (string | number)[][] = [];
      const walk = (value: unknown, path: (string | number)[]): void => {
        if (Array.isArray(value)) {
          value.forEach((item, index) => walk(item, [...path, index]));
        } else if (typeof value === "object" && value !== null) {
          // A note's params are free-form by design; every other object is exact-key.
          if (path[path.length - 1] !== "params") {
            paths.push(path);
          }
          for (const [key, item] of Object.entries(value)) {
            walk(item, [...path, key]);
          }
        }
      };
      walk(raw, []);
      expect(paths.length).toBeGreaterThan(20);
      for (const path of paths) {
        const copy = JSON.parse(JSON.stringify(raw)) as Record<string, unknown>;
        let target: Record<string, unknown> = copy;
        for (const step of path) {
          target = (target as Record<string | number, unknown>)[step] as Record<string, unknown>;
        }
        target["surprise"] = 1;
        expect(decodeHeat(copy).ok, `${name} ${path.join(".")}`).toBe(false);
      }
    }
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
    expect(card.querySelector("[data-line='band']")!.textContent).toBe("inom 20,5–21,5 °C");
    expect(card.querySelector("[data-measured]")).not.toBeNull();
    // The sensor's "temperatur" left off the room's name; a figure and its unit kept together.
    expect(measuredLine("sv", villa)).toBe("Inne 20,6\u00a0°C (kallast: Toalett NV) · Ute 6,2\u00a0°C");
    expect(pumpLine("sv", villa)).toBe("Värmepumpen värmer · 1,1 kW");
    expect(wouldLine("sv", villa)).toBe("Skulle sänka till 20,5 °C till 19:00: dyra timmar");
    expect(card.querySelector("[data-line='shadow']")!.textContent).toBe("Temperaturen och Hemma/Borta som du ställer in här följer SpotNav när den börjar styra värmen.");
    // The charger card's header: the mark, "Värme · Test", the buttons at the end; tapping Test says what it means.
    const header = card.querySelector<HTMLElement>(".spotnav-header")!;
    expect(header.querySelector(".spotnav-identity")).not.toBeNull();
    expect(header.querySelector("h3.spotnav-name")!.textContent).toBe("Värme");
    const status = header.querySelector<HTMLElement>("[data-line='header-status']")!;
    expect(status.dataset["status"]).toBe("test");
    // After the test chip, what the heat pump does now.
    expect(status.textContent).toBe("· Test · Värmer 1,1\u00a0kW");
    expect(status.dataset["live"]).toBe("heating");
    const test = status.querySelector<HTMLButtonElement>("button[data-action='test']")!;
    expect(test.title).toBe("SpotNav styr inget än.");
    const explain = card.querySelector<HTMLElement>("[data-line='test-explain']")!;
    expect(explain.hidden).toBe(true);
    test.click();
    expect(explain.hidden).toBe(false);
    expect(explain.textContent).toMatch(/^SpotNav styr inget än\. SpotNav räknar ut/);
    expect(header.querySelector(".spotnav-header-actions button[data-action='settings']")).not.toBeNull();
    expect(card.querySelector(".spotnav-house-chip")).toBeNull();
    expect(card.querySelector("button[data-mode='home']")!.getAttribute("aria-pressed")).toBe("true");
    expect(card.querySelector("button[data-action='settings']")).not.toBeNull();
  });

  it("draws one day chart with the lowering, the hot water, the car and the now line, and no hour squares", () => {
    const card = planBody(answer());
    const chart = card.querySelector("[data-block='band'] [data-block='day']")!;
    expect(chart.querySelectorAll("svg")).toHaveLength(1);
    expect(card.querySelector("[data-block='indoor']")).toBeNull();
    // The plan's lowered hours as light columns, the relay as anyone closed it (and is expected to) in teal.
    expect(chart.querySelectorAll("[data-mark='planned']")).toHaveLength(7);
    expect(chart.querySelectorAll("[data-mark='relay']")).toHaveLength(3);
    expect(chart.querySelectorAll("[data-mark='relay_expected']")).toHaveLength(2);
    expect(chart.querySelectorAll("[data-mark='hot_water']")).toHaveLength(2);
    // The car's four hours in a row, one bar.
    expect(chart.querySelectorAll("[data-mark='car']")).toHaveLength(1);
    expect(chart.querySelector(".spotnav-heat-day-now")).not.toBeNull();
    const legend = [...chart.querySelectorAll(".spotnav-heat-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual([
      "Rummen, medel", "Kallaste rummet", "Börvärde (SpotNav sänker)", "Komfortband 20,5–21,5 °C",
      "Planerad sänkning", "Reläet sänkte", "Reläet väntas sänka", "Varmvatten", "Bilen laddar",
    ]);
  });

  it("says a chosen time's figures under the chart, by the keys too", () => {
    const onChartSelect = vi.fn();
    const handlers = { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn(), onChartSelect };
    const card = planBody(answer(), "sv", handlers);
    const surface = card.querySelector<HTMLElement>(".spotnav-heat-day-surface")!;
    const readout = card.querySelector("[data-line='day-readout']")!;
    expect(surface.tabIndex).toBe(0);
    expect(readout.textContent).toBe("Tryck eller peka i diagrammet för en tids siffror.");
    expect(card.querySelector(".spotnav-heat-day-cross")!.getAttribute("display")).toBe("none");
    // From the now line (17:15) a step on: the rooms, the coldest one by name, the setpoint and the relay.
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(onChartSelect).toHaveBeenLastCalledWith(Date.parse("2026-11-16T16:30:00+00:00"));
    expect(card.querySelector(".spotnav-heat-day-cross")!.hasAttribute("display")).toBe(false);
    expect(readout.textContent).toBe("17:30 · börvärde 20,5 °C · reläet väntas sänka 60 min · planerad sänkning");
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    expect(readout.textContent).toMatch(/^00:00 · rummen 21,2 °C · kallast Toalett NV 20,7 °C · börvärde 20,0 °C · natt$/);
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(onChartSelect).toHaveBeenLastCalledWith(null);
    expect(readout.textContent).toBe("Tryck eller peka i diagrammet för en tids siffror.");
    // A time chosen before the card's redraw stays chosen.
    const kept = heatBody({
      doc: document, language: "sv", state: { kind: "ready", answer: answer() }, handlers, width: 330, nowMs: NOW,
      ui: { pendingTarget: null, error: null, tab: "plan", chartSelectedMs: Date.parse("2026-11-16T03:00:00+00:00") },
    });
    expect(kept.querySelector("[data-line='day-readout']")!.textContent).toMatch(/^04:00 · rummen /);
  });

  it("lets a touch drag across the day chart update the readout, and a mouse pick by x alone", () => {
    const onChartSelect = vi.fn();
    const handlers = { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn(), onChartSelect };
    const card = planBody(answer(), "sv", handlers);
    document.body.append(card);
    const surface = card.querySelector<HTMLElement>(".spotnav-heat-day-surface")!;
    const picture = surface.querySelector("svg")!;
    const readout = card.querySelector("[data-line='day-readout']")!;
    const hint = readout.textContent;
    // jsdom lays nothing out: the chart is given a box at the page's origin.
    picture.getBoundingClientRect = () => ({ left: 0, top: 0, right: 300, bottom: 150, width: 300, height: 150, x: 0, y: 0, toJSON: () => ({}) });
    let captured = 0;
    surface.setPointerCapture = () => void (captured += 1);
    surface.releasePointerCapture = () => undefined;
    const send = (type: string, clientX: number, clientY: number, pointerType = "touch"): PointerEvent => {
      const event = new PointerEvent(type, { clientX, clientY, pointerType, pointerId: 1, bubbles: true, cancelable: true });
      surface.dispatchEvent(event);
      return event;
    };
    send("pointerdown", 30, 75);
    expect(send("pointermove", 40, 75).defaultPrevented).toBe(true);
    expect(captured).toBe(1);
    const early = readout.textContent;
    expect(early).not.toBe(hint);
    send("pointermove", 200, 75);
    expect(readout.textContent).not.toBe(early);
    expect(onChartSelect).not.toHaveBeenCalled();
    send("pointerup", 200, 75);
    expect(onChartSelect).toHaveBeenCalledTimes(1);
    const kept = readout.textContent;
    // A mouse: the same x at the top and the bottom is the same time; leaving shows the kept one again.
    send("pointermove", 120, 2, "mouse");
    const top = readout.textContent;
    send("pointermove", 120, 148, "mouse");
    expect(readout.textContent).toBe(top);
    expect(top).not.toBe(kept);
    surface.dispatchEvent(new PointerEvent("pointerleave", { pointerType: "mouse" }));
    expect(readout.textContent).toBe(kept);
    card.remove();
  });

  it("says when a lowering is for a house that is too warm", () => {
    const villa = answer();
    const hours = villa.band.hours.map((hour, index) => (index === 17 ? { ...hour, reason: "room_warm" } : hour));
    const warm: HeatAnswer = { ...villa, band: { ...villa.band, hours }, status: { ...villa.status, would: { ...villa.status.would!, action: "lower", reason: "room_warm" } } };
    // The chart's readout names the hour's lowering (a lowering column like a dear hour's).
    const card = planBody(warm);
    card.querySelector<HTMLElement>(".spotnav-heat-day-surface")!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(card.querySelector("[data-line='day-readout']")!.textContent).toBe(
      "17:30 · börvärde 20,5 °C · reläet väntas sänka 60 min · planerad sänkning · för varmt inne",
    );
    expect(wouldLine("sv", warm)).toBe("Skulle sänka till 20,5 °C till 19:00: för varmt inne");
    expect(wouldLine("en", warm)).toBe("Would lower to 20.5 °C until 19:00: too warm indoors");
    const controlled = answer("controlled.json");
    const lowered = { ...controlled.control!, state: "running", reason: "planned", lowered: true, until: controlled.status.would?.until ?? controlled.now };
    const line = controlLine("sv", { ...controlled, control: lowered, status: { ...controlled.status, would: { action: "lower", target_c: 20.5, until: null, reason: "room_warm" } } });
    expect(line).toMatch(/^Styr: sänker värmen till .+, för varmt inne\.$/);
  });

  it("says today's heat (the zones' own meters in it) and hot water and their cost", () => {
    expect(todayLine("sv", answer())).toBe("I dag · värme 17,5 kWh · varav zoner 3,6 kWh · varmvatten 1,7 kWh · 22 kr");
    const plain = answer();
    plain.today = { ...plain.today!, zones_kwh: null };
    expect(todayLine("en", plain)).not.toContain("zones");
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
    expect(measuredLine("en", pump)).toBe("No indoor sensor chosen · Outdoors 4.5\u00a0°C");
    expect(wouldLine("en", pump)).toBe("Would keep 17.5 °C until Fri 20 Nov 14:00 while you are away");
  });

  it("says what the relay does beside SpotNav's plan, yesterday's review and the automations that steer", () => {
    const villa = answer();
    const card = body(villa);
    expect(relayLine("sv", villa, NOW)).toBe("Reläet sänker värmen sedan 16:00, väntas öppna 19:00");
    expect(card.querySelector("[data-line='relay']")!.textContent).toBe("Reläet sänker värmen sedan 16:00, väntas öppna 19:00");
    // Open, with a closing ahead: when it is expected.
    const hours = villa.band.hours.map((hour, index) =>
      index === 21 ? { ...hour, relay_min: 60, relay_expected: true } : index >= 16 && index <= 18 ? { ...hour, relay_min: 0, relay_expected: false } : hour,
    );
    const open = { ...villa, relay: { ...villa.relay!, closed: false }, band: { ...villa.band, hours } };
    expect(relayLine("sv", open, NOW)).toBe("Reläet väntas sänka värmen 21:00–22:00");
    expect(relayLine("sv", { ...villa, relay: null }, NOW)).toBeNull();
    expect(reviewLines("sv", villa).map((line) => line.text)).toEqual([
      "I går sänkte reläet 3 h: 1,58 kr · kallast 20,4\u00a0°C · 20 min under 20,5\u00a0°C",
      "SpotNavs plan 5 h: 3,19 kr · kallast 20,4\u00a0°C · 20 min under 20,5\u00a0°C · 2 h samma som reläet",
    ]);
    // Yesterday's review stands under today's chart, in Plan.
    expect(card.querySelector("[data-line='review-plan']")).toBeNull();
    expect(planBody(villa).querySelector("[data-line='review-plan']")).not.toBeNull();
    expect(noteLines("sv", villa).map((note) => note.text)).toEqual([
      "Automationen 'Golvvärme – börvärde efter elpris och larm' styr också Golvvärme toalett nedervåning: stäng av den när SpotNav ska styra.",
      // Styr värmen is on: the automation on the relay is what it waits for, one note that says what to do.
      "Stäng av automationen 'Värmepanna: sänk värmekurvan när elen är dyr' så att SpotNav kan styra värmen.",
    ]);
    const before = { ...villa, notes: villa.notes.map((note) => ({ ...note, params: { ...note.params, blocks: false } })) };
    expect(noteLines("sv", before)[1]!.text).toBe(
      "Automationen 'Värmepanna: sänk värmekurvan när elen är dyr' styr reläet: stäng av den när SpotNav ska styra värmen.",
    );
  });

  it("refuses a relay or review with a key too many or missing, deep down", () => {
    const raw = read("villa.json");
    const relay = raw["relay"] as Record<string, unknown>;
    expect(decodeHeat({ ...raw, relay: { ...relay, effect: { ...(relay["effect"] as object), extra: 1 } } }).ok).toBe(false);
    const review = raw["review"] as Record<string, unknown>;
    const { overlap_hours: _overlap, ...plan } = review["plan"] as Record<string, unknown>;
    expect(decodeHeat({ ...raw, review: { ...review, plan } }).ok).toBe(false);
    expect(decodeHeat({ ...raw, relay: null, review: null }).ok).toBe(true);
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
      ["steer", "Styr värmenPå"],
      ["indoor", "Inomhusgivare3 valda"],
      ["sensor:sensor.inomhus_kok_temperatur", "Inomhus kök0 °C"],
      ["sensor:sensor.ovanvaning_temperatur", "Ovanvåning−1,0 °C"],
      ["sensor:sensor.toalett_nv_temperatur", "Toalett NV0 °C"],
      ["outdoor", "UtegivareAutomatisk"],
      ["meter", "Värmepumpens mätareElmätare värme (Nibe + panna) Effekt"],
      ["path", "StyrningRelä (sänkning)"],
      ["control", "StyrutgångLarm utgång 1"],
      ["hotWater", "VarmvattenIngen"],
      ["zones", "Zoner4"],
      ["zone:climate.golvvarme_toalett_nere", "Golvvärme toalett nedervåning0 °C"],
      ["zone:climate.golvvarme_toalett_uppe", "Golvvärme toalett ovanvåning0 °C"],
      ["zone:climate.golvvarme_tvattstuga", "Golvvärme tvättstuga−1,0 °C"],
      ["zone:climate.ovanvaning", "Värmepump ovanvåning−0,5 °C"],
      ["night", "Svalare natt−1,0 °C 23:00–06:00"],
      ["awayDelta", "Sänkning när borta−3,0 °C"],
    ]);
    section.querySelector<HTMLButtonElement>("button[data-edit='zone:climate.ovanvaning']")!.click();
    expect(edits).toEqual(["zone:climate.ovanvaning"]);
    expect(section.querySelector("[data-help='zone:climate.ovanvaning']")!.textContent).toBe(
      "Luft-luftvärmepump: SpotNav ändrar bara börvärdet, slår aldrig av eller på.",
    );
    // The switch's line: what it waits for, and no "test" intro while it is on.
    expect(section.querySelector("[data-help='steer']")!.textContent).toBe(
      "Väntar: stäng av automationen 'Värmepanna: sänk värmekurvan när elen är dyr' så att SpotNav kan styra värmen.",
    );
    expect(section.textContent).not.toContain("styr ingen värme än");
    // A row that opens an editor says its help there, not under the row.
    for (const key of ["indoor", "outdoor", "path", "hotWater", "zones", "night", "awayDelta"]) {
      expect(section.querySelector(`[data-help='${key}']`), key).toBeNull();
    }
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

  it("writes an indoor sensor's offset with the others kept, a zero left out", async () => {
    const { messages, root } = await mount();
    root.querySelector<HTMLButtonElement>("button[data-action='settings']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("button[data-edit='sensor:sensor.toalett_nv_temperatur']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor='number']")!;
    expect(form.textContent).toContain("Rummets eget mål");
    const input = form.querySelector<HTMLInputElement>("input")!;
    input.value = "0,5";
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    const write = messages.find((message) => message["type"] === "spotnav/update_heat")!;
    expect(write["changes"]).toEqual({
      indoor_offsets: [
        { entity_id: "sensor.ovanvaning_temperatur", offset_c: -1 },
        { entity_id: "sensor.toalett_nv_temperatur", offset_c: 0.5 },
      ],
    });
    expect(write["expected"]).toEqual({ indoor_offsets: [{ entity_id: "sensor.ovanvaning_temperatur", offset_c: -1 }] });
  });

  it("turns the heat control on through its own editor", async () => {
    const { messages, root } = await mount();
    root.querySelector<HTMLButtonElement>("button[data-action='settings']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("button[data-edit='steer']")!.click();
    const form = root.querySelector<HTMLFormElement>("form")!;
    expect(form.textContent).toContain("minst 45 minuter");
    form.querySelector<HTMLInputElement>("input[value='off']")!.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    const write = messages.find((message) => message["type"] === "spotnav/update_heat")!;
    expect(write["changes"]).toEqual({ control_enabled: false });
    expect(write["expected"]).toEqual({ control_enabled: true });
  });
});

describe("SpotNav controlling the heat", () => {
  it("says so in the header, under the switch and in the day's band", () => {
    const controlled = answer("controlled.json");
    const card = planBody(controlled);
    // The header says what the control does now; no "Styr värmen" pill, that is the setting's.
    const status = card.querySelector<HTMLElement>(".spotnav-header [data-line='header-status']")!;
    expect(status.dataset["status"]).toBe("lower");
    expect(status.textContent).toBe("· Sänkt till 19:00");
    expect(card.querySelector(".spotnav-header")!.textContent).not.toContain("Styr värmen");
    expect(card.querySelector("[data-action='test']")).toBeNull();
    expect(card.querySelector("[data-line='test-explain']")).toBeNull();
    expect(card.querySelector("[data-line='shadow']")).toBeNull();
    expect(controlLine("sv", controlled)).toBe("Styr: sänker värmen till 19:00.");
    // SpotNav's own lowerings as solid columns in the chart, said in words and in the legend.
    const marks = [...card.querySelectorAll<SVGElement>("[data-mark='executed']")];
    expect(marks).toHaveLength(3);
    const surface = card.querySelector<HTMLElement>(".spotnav-heat-day-surface")!;
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    for (let step = 0; step < 7 * 4; step += 1) {
      surface.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    }
    expect(card.querySelector("[data-line='day-readout']")!.textContent).toContain("07:00 · ");
    expect(card.querySelector("[data-line='day-readout']")!.textContent).toContain("börvärde 20,5 °C · SpotNav sänkte 60 min");
    expect([...card.querySelectorAll("li")].map((item) => item.textContent)).toContain("SpotNav sänkte");
    // The villa before: the test chip, nothing executed.
    const villa = planBody(answer());
    expect(villa.querySelector("[data-action='test']")).not.toBeNull();
    expect(villa.querySelectorAll("[data-mark='executed']")).toHaveLength(0);
  });

  it("says in the header what happens now: lowered or raised until when, else what the heat pump does; Test before it while SpotNav does not control", () => {
    const controlled = answer("controlled.json");
    const at = (control: Partial<NonNullable<HeatAnswer["control"]>>, status: Partial<HeatAnswer["status"]> = {}) => {
      const value = { ...controlled, control: { ...controlled.control!, ...control }, status: { ...controlled.status, ...status } };
      const line = body(value).querySelector<HTMLElement>(".spotnav-header [data-line='header-status']");
      return line === null ? null : `${line.dataset["status"]}/${line.dataset["live"]}: ${line.textContent}`;
    };
    expect(at({})).toBe("lower/lower: · Sänkt till 19:00");
    expect(at({ until: null })).toBe("lower/lower: · Sänkt");
    expect(at({ lowered: false, raised: true, raise_until: "2026-11-16T17:00:00+00:00" })).toBe("raise/raise: · Höjd till 18:00");
    expect(at({ lowered: false, raised: true })).toBe("raise/raise: · Höjd");
    // Controlling, the pump's own curve: what the heat pump does, by its facts or its meter.
    expect(at({ lowered: false, reason: "normal" })).toBe("heating/heating: · Värmer 1,1\u00a0kW");
    expect(at({ lowered: false }, { pump_state: "hot_water", kw: 2.94 })).toBe("hot_water/hot_water: · Varmvatten 2,9\u00a0kW");
    expect(at({ lowered: false }, { pump_state: "idle", kw: 0.02 })).toBe("idle/idle: · Vilar");
    expect(at({ lowered: false }, { pump_state: "heating", kw: null })).toBe("heating/heating: · Värmer");
    expect(at({ lowered: false }, { pump_state: null })).toBeNull();
    for (const control of [{ enabled: false, state: "off" }, { state: "verifying" }, { state: "waiting", reason: "automation" }]) {
      expect(at({ ...control, lowered: false }), JSON.stringify(control)).toBe("test/heating: · Test · Värmer 1,1\u00a0kW");
    }
    expect(at({ enabled: false, state: "off", lowered: false }, { pump_state: null })).toBe("test/undefined: · Test");
    expect(body({ ...controlled, control: null }).querySelector("[data-line='header-status']")!.textContent).toBe("· Test · Värmer 1,1\u00a0kW");
    const en = body(controlled, "en");
    expect(en.querySelector("[data-line='header-status']")!.textContent).toBe("· Lowered until 19:00");
    // The pump's line stands in Now only while the header is taken by SpotNav's own lowering.
    expect(body(controlled).querySelector("[data-line='pump']")!.textContent).toBe("Värmepumpen värmer · 1,1 kW");
    expect(body(answer()).querySelector("[data-line='pump']")).toBeNull();
  });

  it("words every state of the switch's line", () => {
    const controlled = answer("controlled.json");
    const at = (control: Partial<NonNullable<HeatAnswer["control"]>>): string | null =>
      controlLine("sv", { ...controlled, control: { ...controlled.control!, ...control } });
    expect(at({ enabled: false, state: "off" })).toBe("Av: SpotNav visar bara vad den skulle göra.");
    expect(at({ state: "verifying", reason: "wait", lowered: false })).toBe("Provar att värmepumpen svarar på reläet: väntar på en stund när den värmer.");
    expect(at({ state: "verifying", reason: "closed" })).toBe("Provar nu att värmepumpen svarar på reläet.");
    expect(at({ lowered: false, reason: "normal" })).toBe("Styr: värmepumpen går på sin vanliga kurva just nu.");
    expect(at({ lowered: false, reason: "room_cold" })).toBe("Styr: ett rum ligger under bandet, ingen sänkning.");
    expect(at({ state: "waiting", reason: "foreign_change", lowered: false })).toBe("Väntar: någon annan ändrade reläet. SpotNav styr igen i morgon.");
    expect(at({ state: "waiting", reason: "check_failed", lowered: false })).toBe("Värmepumpen svarade inte på reläet. Kontrollera kopplingen och slå på igen.");
    expect(at({ state: "waiting", reason: "no_signal", lowered: false })).toBe("Väntar: välj värmepumpens mätare så att SpotNav kan se att reläet verkar.");
    expect(at({ state: "waiting", reason: "something_new", lowered: false })).toBe("Väntar.");
    expect(controlLine("sv", { ...controlled, control: null })).toBeNull();
    for (const language of ["en", "sv"] as const) {
      for (const reason of ["automation", "other_controller", "pump_price_control", "relay_unavailable", "relay_closed", "no_relay", "no_read_back"]) {
        expect(controlLine(language, { ...controlled, control: { ...controlled.control!, state: "waiting", reason } })).not.toContain("heat.");
      }
    }
  });

  it("shows SG Ready's raise: would raise in shadow, raised hours and the line while controlling", () => {
    const sgReady = (value: HeatAnswer, raised: (index: number) => number): HeatAnswer => ({
      ...value,
      config: { ...value.config, control_path: "sg_ready", control_entity: "switch.sg_a", control_entity_b: "switch.sg_b" },
      band: { ...value.band, hours: value.band.hours.map((hour, index) => ({ ...hour, raised_min: raised(index) })) },
    });
    // In shadow: the pre-heat hour is a planned raise, "Skulle höja".
    const villa = sgReady(answer(), () => 0);
    const shadow = planBody(villa);
    expect(shadow.querySelectorAll("[data-mark='raise_planned']")).toHaveLength(1);
    expect([...shadow.querySelectorAll("li")].map((item) => item.textContent)).toContain("Skulle höja");
    const surface = shadow.querySelector<HTMLElement>(".spotnav-heat-day-surface")!;
    surface.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    for (let step = 0; step < 6 * 4; step += 1) {
      surface.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    }
    expect(shadow.querySelector("[data-line='day-readout']")!.textContent).toContain("skulle höja");
    const preheat = { ...villa, status: { ...villa.status, would: { action: "preheat", target_c: 21.5, until: "2026-11-16T17:00:00+00:00", reason: "cheap_before_dear" } } };
    expect(wouldLine("sv", preheat)).toBe("Skulle höja värmen (SG Ready) till 21,5 °C till 18:00: billigt före dyra timmar");
    // The relay path keeps its pre-heat words and draws no raise.
    expect(wouldLine("sv", { ...preheat, config: answer().config })).toMatch(/^Skulle förvärma/);
    expect(planBody(answer()).querySelectorAll("[data-mark^='raise']")).toHaveLength(0);
    // Controlling: SpotNav raised 06:00-06:40, said in the chart, the legend and the switch's line.
    const controlled = sgReady(answer("controlled.json"), (index) => (index === 6 ? 40 : 0));
    const card = planBody(controlled);
    expect(card.querySelectorAll("[data-mark='raised']")).toHaveLength(1);
    const legend = [...card.querySelectorAll("li")].map((item) => item.textContent);
    expect(legend).toContain("SpotNav höjde");
    expect(legend).toContain("Planerad höjning");
    const raising = { ...controlled, control: { ...controlled.control!, lowered: false, raised: true, raise_until: "2026-11-16T17:00:00+00:00" } };
    expect(controlLine("sv", raising)).toBe("Styr: höjer värmen till 18:00.");
    expect(controlLine("en", { ...raising, control: { ...raising.control, raise_until: null } })).toBe("Controlling: raising the heat now.");
  });

  it("explains SG Ready's two contacts in their editors", () => {
    const value = answer();
    const config = { ...value.config, control_path: "sg_ready", control_entity: "switch.sg_a", control_entity_b: "switch.sg_b" };
    const input = { doc: document, language: "sv" as const, idPrefix: "t", answer: { ...value, config }, write: vi.fn(async () => null) };
    const handlers = { onCancel: vi.fn(), onDone: vi.fn() };
    const a = heatEditor(input as never, "control", handlers as never)!;
    const b = heatEditor(input as never, "controlB", handlers as never)!;
    expect(a.body.textContent).toContain("blockerar eller sänker");
    expect(b.body.textContent).toContain("höja värmen i billiga timmar");
    expect(b.body.textContent).toContain("aldrig båda samtidigt");
  });

  it("writes the away setback from its editor and refuses a value off its range or step", async () => {
    const value = answer();
    expect(value.config.away_delta_c).toBe(3);
    const write = vi.fn(async () => null);
    const input = { doc: document, language: "sv" as const, idPrefix: "t", answer: value, write };
    const handlers = { onCancel: vi.fn(), onDone: vi.fn() };
    const editor = heatEditor(input as never, "awayDelta", handlers as never)!;
    expect(editor.title).toBe("Sänkning när borta");
    const form = editor.body as HTMLFormElement;
    const delta = form.querySelector<HTMLInputElement>("input[data-field='delta']")!;
    expect([delta.min, delta.max, delta.step, delta.value]).toEqual(["1", "6", "0.5", "3"]);
    for (const bad of ["0.5", "6.5", "2.2"]) {
      delta.value = bad;
      form.dispatchEvent(new Event("submit", { cancelable: true }));
      await vi.waitFor(() => expect(form.querySelector<HTMLButtonElement>("button[type='submit']")!.disabled).toBe(false));
    }
    expect(write).not.toHaveBeenCalled();
    delta.value = "4.5";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(write).toHaveBeenCalledWith({ away_delta_c: 4.5 }));
  });

  it("keeps the switch away from a path it cannot steer", () => {
    const pump = answer("network_pump.json");
    const section = heatSettingsSection(document, "sv", pump, () => undefined);
    expect(section.querySelector("[data-row='steer']")).toBeNull();
  });
});

describe("the control paths through the pump's own settings", () => {
  const handlers = { onCancel: vi.fn(), onDone: vi.fn() };
  const editorInput = (value: HeatAnswer, write = vi.fn(async () => null)) => ({ doc: document, language: "sv" as const, idPrefix: "t", answer: value, write });
  const choose = (form: HTMLFormElement, value: string): void => {
    const radio = form.querySelector<HTMLInputElement>(`input[type='radio'][value='${value}']`)!;
    radio.checked = true;
    radio.dispatchEvent(new Event("change"));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
  };

  it("reads the paths found on the pump, the path steered and the entities to enable", () => {
    const value = answer("sg_api_pump.json");
    expect(value.pump!.paths.map((item) => item.path)).toEqual(["sg_ready_api", "offset"]);
    expect(value.control!.path).toBe("sg_ready_api");
    expect(value.control!.untested).toBe(true);
    expect(value.control!.enable_entities!.entities).toHaveLength(2);
    // The older fixtures carry the same keys: nothing found, nothing to enable, a relay that is tried.
    expect(answer("network_pump.json").pump!.paths).toEqual([]);
    expect([answer("controlled.json").control!.path, answer("controlled.json").control!.untested]).toEqual(["relay", false]);
  });

  it("shows the switch, the path's two entities and that the path is untried", () => {
    const section = heatSettingsSection(document, "sv", answer("sg_api_pump.json"), () => undefined);
    const rows = Object.fromEntries([...section.querySelectorAll<HTMLElement>("[data-row]")].map((row) => [row.dataset["row"], row.textContent]));
    expect(rows["steer"]).toBe("Styr värmenAv");
    expect(rows["path"]).toBe("StyrningSG Ready via integrationen (NIBE S)");
    expect(rows["control"]).toBe("StyrutgångRequested operating mode (SG Ready)");
    expect(rows["controlB"]).toBe("AktiveringActivate SG Ready via API");
    expect(section.querySelector("[data-help='untested']")!.textContent).toContain("inte provat på en riktig värmepump");
    // A relay says nothing of the kind.
    expect(heatSettingsSection(document, "sv", answer("controlled.json"), () => undefined).querySelector("[data-help='untested']")).toBeNull();
  });

  it("marks the paths found on the pump and fills a chosen one's entities in", async () => {
    const value = answer("sg_api_pump.json");
    const write = vi.fn(async () => null);
    const form = heatEditor(editorInput(value, write) as never, "path", handlers as never)!.body as HTMLFormElement;
    const labels = [...form.querySelectorAll<HTMLInputElement>("input[type='radio']")].map((radio) => [radio.value, radio.closest("label")!.nextElementSibling?.textContent ?? ""]);
    expect(labels.map(([path]) => path)).toEqual(["none", "relay", "sg_ready", "sg_ready_api", "offset", "setpoint"]);
    expect(labels.filter(([, text]) => text!.includes("Hittad på värmepumpen")).map(([path]) => path)).toEqual(["sg_ready_api", "offset"]);
    choose(form, "offset");
    await vi.waitFor(() =>
      expect(write).toHaveBeenCalledWith({ control_path: "offset", control_entity: "number.s1155_heating_offset_climate_system_1_40031", control_entity_b: null }),
    );
    const again = heatEditor(editorInput(value, write) as never, "path", handlers as never)!.body as HTMLFormElement;
    choose(again, "relay");
    await vi.waitFor(() => expect(write).toHaveBeenLastCalledWith({ control_path: "relay" }));
    // A pump without it is not offered SG Ready through the integration.
    const plain = heatEditor(editorInput(answer()) as never, "path", handlers as never)!.body as HTMLFormElement;
    expect([...plain.querySelectorAll<HTMLInputElement>("input[type='radio']")].map((radio) => radio.value)).toEqual(["none", "relay", "sg_ready", "offset", "setpoint"]);
  });

  it("offers the mode's number and the activation switch each in its own editor", () => {
    const value = answer("sg_api_pump.json");
    const values = (key: "control" | "controlB"): string[] =>
      [...(heatEditor(editorInput(value) as never, key, handlers as never)!.body as HTMLFormElement).querySelectorAll<HTMLInputElement>("input[type='radio']")].map((radio) => radio.value);
    expect(values("control")).toEqual(["", "number.s1155_requested_operating_mode_sg_ready_46009"]);
    expect(values("controlB")).toEqual(["", "switch.s1155_activate_sg_ready_via_api_43033"]);
    const mode = heatEditor(editorInput(value) as never, "control", handlers as never)!;
    expect(mode.body.textContent).toContain("aldrig till det tvingade läget");
    const activation = heatEditor(editorInput(value) as never, "controlB", handlers as never)!;
    expect(activation.title).toBe("Aktivering");
    expect(activation.body.textContent).toContain("om den inte redan var på");
    const offset = { ...value, config: { ...value.config, control_path: "offset", control_entity: "number.s1155_heating_offset_climate_system_1_40031", control_entity_b: null } };
    expect(heatEditor(editorInput(offset) as never, "control", handlers as never)!.body.textContent).toContain("sätter sedan tillbaka värdet den hittade");
  });

  it("lists the entities it turns on with the switch and sends the confirm with the write", async () => {
    const value = answer("sg_api_pump.json");
    const write = vi.fn(async () => null);
    const editor = heatEditor(editorInput(value, write) as never, "steer", handlers as never)!;
    const text = editor.body.textContent!;
    expect(text).toContain("genom värmepumpens egen inställning");
    expect(text).toContain("inte provat på en riktig värmepump");
    expect(text).toContain("avstängda i S1155: Activate SG Ready via API och Requested operating mode (SG Ready)");
    choose(editor.body as HTMLFormElement, "on");
    await vi.waitFor(() => expect(write).toHaveBeenCalledWith({ control_enabled: true }, { enable: true }));
    // Nothing to enable: no confirm is sent, and the relay keeps its own words.
    const relayWrite = vi.fn(async () => null);
    const off = { ...answer("controlled.json"), config: { ...answer("controlled.json").config, control_enabled: false } };
    const relay = heatEditor(editorInput(off, relayWrite) as never, "steer", handlers as never)!;
    expect(relay.body.textContent).toContain("sluter värmepumpens relä");
    expect(relay.body.textContent).not.toContain("avstängda i");
    choose(relay.body as HTMLFormElement, "on");
    await vi.waitFor(() => expect(relayWrite).toHaveBeenCalledWith({ control_enabled: true }, { enable: false }));
  });

  it("asks to put back an offset that was not put back, and sends the answer", async () => {
    const value = answer("sg_api_pump.json");
    const line = { entity_id: "number.s1155_heating_offset_climate_system_1_40031", value: -2, home: 0 };
    const shown: HeatAnswer = {
      ...value,
      config: { ...value.config, control_path: "offset", control_entity: line.entity_id, control_entity_b: null, control_enabled: true },
      control: { ...value.control!, path: "offset", offset_home: line },
    };
    const section = heatSettingsSection(document, "sv", shown, () => undefined);
    const rows = Object.fromEntries([...section.querySelectorAll<HTMLElement>("[data-row]")].map((row) => [row.dataset["row"], row.textContent]));
    expect(rows["offsetHome"]).toBe("Kurvjusteringen är inte tillbaka−2 (var 0)");
    const write = vi.fn(async () => null);
    const editor = heatEditor(editorInput(shown, write) as never, "offsetHome", handlers as never)!;
    expect(editor.body.textContent).toContain("Kurvjusteringen står på −2; den var 0 innan SpotNav.");
    choose(editor.body as HTMLFormElement, "restore");
    await vi.waitFor(() => expect(write).toHaveBeenCalledWith({}, { offsetHome: "restore" }));
    // Once the two agree there is no row and nothing to open.
    expect(heatSettingsSection(document, "sv", value, () => undefined).querySelector("[data-row='offsetHome']")).toBeNull();
    expect(heatEditor(editorInput(value) as never, "offsetHome", handlers as never)).toBeNull();
    expect(value.control!.offset_home).toBeNull();
  });

  it("words the waits for the pump's own setting, not for a relay", () => {
    const value = answer("sg_api_pump.json");
    const waiting = (reason: string, state = "waiting"): HeatAnswer => ({ ...value, control: { ...value.control!, enabled: true, state, reason } });
    expect(controlLine("sv", waiting("relay_unavailable"))).toBe("Väntar: värmepumpens inställning går inte att läsa.");
    expect(controlLine("sv", waiting("foreign_change"))).toBe("Väntar: någon annan ändrade inställningen. SpotNav styr igen i morgon.");
    expect(controlLine("en", waiting("check_failed"))).toBe("The heat pump did not answer. Check the setting and turn on again.");
    expect(controlLine("sv", waiting("wait", "verifying"))).toBe("Provar att värmepumpen svarar: väntar på en stund när den värmer.");
    // What is not about the relay keeps its words, and so does the relay's own path.
    expect(controlLine("sv", waiting("pump_price_control"))).toBe("Väntar: värmepumpens egen prisstyrning är på.");
    const relay = answer("controlled.json");
    expect(controlLine("sv", { ...relay, control: { ...relay.control!, state: "waiting", reason: "relay_unavailable" } })).toBe("Väntar: reläet går inte att läsa.");
  });

  it("calls a pre-heat a raise by SG Ready through the API, and keeps the pre-heat's words on an offset", () => {
    const value = answer("sg_api_pump.json");
    const preheat = { ...value, status: { ...value.status, would: { action: "preheat", target_c: 21.5, until: "2026-11-16T17:00:00+00:00", reason: "cheap_before_dear" } } };
    expect(wouldLine("sv", preheat)).toMatch(/^Skulle höja värmen \(SG Ready\)/);
    const offset = { ...preheat, config: { ...preheat.config, control_path: "offset", control_entity_b: null } };
    expect(wouldLine("sv", offset)).toMatch(/^Skulle förvärma/);
  });
});

describe("the rooms in the day chart", () => {
  it("draws the rooms up to now, the coldest room, the band's setpoint as a step and the comfort band", () => {
    const value = answer();
    expect(value.indoor_day?.points.length).toBe(69);
    const block = planBody(value).querySelector("[data-block='day']")!;
    const mean = block.querySelector("[data-line='mean']")!.getAttribute("d")!;
    expect(mean.startsWith("M")).toBe(true);
    expect(mean.match(/M/g)).toHaveLength(1);
    expect(block.querySelector("path[data-line='coldest']")).not.toBeNull();
    // The setpoint follows the band's hours over the whole day: 24 levels, dropping in the lowered hours.
    const setpoint = block.querySelector("[data-line='setpoint']")!.getAttribute("d")!;
    expect(setpoint.match(/L/g)).toHaveLength(24 * 2 - 1);
    expect(block.querySelector(".spotnav-heat-indoor-comfort")).not.toBeNull();
  });

  it("says plain setpoint when nothing is lowered, leaves out a coldest room that never differs, and draws the plan without rooms", () => {
    const value = answer();
    const flat = {
      ...value,
      band: { ...value.band, hours: value.band.hours.map((hour) => ({ ...hour, action: "normal", target_c: 21, relay_min: null, hot_water: false, car: false })) },
      indoor_day: { ...value.indoor_day!, points: value.indoor_day!.points.map((point) => ({ ...point, coldest_c: point.mean_c })) },
    };
    const block = planBody(flat, "en").querySelector("[data-block='day']")!;
    const legend = [...block.querySelectorAll(".spotnav-heat-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual(["Rooms, mean", "Setpoint", "Comfort band 20.5–21.5 °C"]);
    expect(block.querySelector("path[data-line='coldest']")).toBeNull();
    // Without indoor sensors the plan is still drawn, against the comfort band as set now.
    const bare = planBody({ ...value, indoor_day: null }).querySelector("[data-block='day']")!;
    expect(bare.querySelector("path[data-line='mean']")).toBeNull();
    expect(bare.querySelector("[data-line='setpoint']")).not.toBeNull();
    expect(planBody({ ...value, indoor_day: null, band: { ...value.band, hours: [] } }).querySelector("[data-block='day']")).toBeNull();
  });

  it("refuses an unknown key in the curve", () => {
    const raw = read("villa.json");
    const day = raw["indoor_day"] as Record<string, unknown>;
    expect(decodeHeat({ ...raw, indoor_day: { ...day, outdoor_c: 4 } }).ok).toBe(false);
  });
});
