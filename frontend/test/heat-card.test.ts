// The heat card (SpotNav 3.0 preview): the strict decoder on the backend's own fixtures, the body's dial, lines,
// band and notes, the settings section, and the element reading `spotnav/get_heat` and writing `spotnav/update_heat`.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { HEAT_CARD_ELEMENT, instantToLocal, localToInstant, parseHeatConfig, SpotnavHeatCard, writeError } from "../src/heat-card";
import { decodeHeat, decodeHeatWrite, type HeatAnswer } from "../src/heat-model";
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

  it("reads SpotNav's own control and refuses an unknown key at any depth of every fixture", () => {
    const controlled = answer("controlled.json");
    expect(controlled.control!.state).toBe("running");
    expect(controlled.band.hours[7]!.executed_min).toBe(60);
    for (const name of ["villa.json", "network_pump.json", "controlled.json"]) {
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
    const legend = [...card.querySelectorAll("[data-block='band'] .spotnav-heat-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual(["Sänkt", "Förvärmning", "Natt", "Reläet sänkte", "Reläet väntas sänka", "Varmvatten", "Bilen laddar"]);
    // The relay's row under SpotNav's plan: closed 07-09 and since 16:00, expected until 19:00.
    expect(card.querySelectorAll("[data-mark='relay']")).toHaveLength(3);
    expect(card.querySelectorAll("[data-mark='relay_expected']")).toHaveLength(2);
    expect(cells[17]!.querySelector("title")!.textContent).toBe("17:00: Sänkt, 20,5 °C · reläet väntas sänka 60 min");
    // A tap on an hour says it in words under the band.
    const readout = card.querySelector("[data-line='band-readout']")!;
    expect(readout.textContent).toBe("");
    cells[17]!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(readout.textContent).toBe(cells[17]!.querySelector("title")!.textContent);
  });

  it("says when a lowering is for a house that is too warm", () => {
    const villa = answer();
    const hours = villa.band.hours.map((hour, index) => (index === 17 ? { ...hour, reason: "room_warm" } : hour));
    const warm: HeatAnswer = { ...villa, band: { ...villa.band, hours }, status: { ...villa.status, would: { ...villa.status.would!, action: "lower", reason: "room_warm" } } };
    const cells = [...body(warm).querySelectorAll<SVGRectElement>(".spotnav-heat-band-cell")];
    expect(cells[17]!.querySelector("title")!.textContent).toBe("17:00: Sänkt (för varmt inne), 20,5 °C · reläet väntas sänka 60 min");
    expect(wouldLine("sv", warm)).toBe("Skulle sänka till 20,5 °C till 19:00: för varmt inne");
    expect(wouldLine("en", warm)).toBe("Would lower to 20.5 °C until 19:00: too warm indoors");
    const controlled = answer("controlled.json");
    const lowered = { ...controlled.control!, state: "running", reason: "planned", lowered: true, until: controlled.status.would?.until ?? controlled.now };
    const line = controlLine("sv", { ...controlled, control: lowered, status: { ...controlled.status, would: { action: "lower", target_c: 20.5, until: null, reason: "room_warm" } } });
    expect(line).toMatch(/^Styr: sänker värmen till .+, för varmt inne\.$/);
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
    expect(card.querySelector("[data-line='review-plan']")).not.toBeNull();
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
    for (const key of ["indoor", "outdoor", "path", "hotWater", "zones", "night"]) {
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
    const card = body(controlled);
    expect(card.querySelector("[data-line='controlling']")!.textContent).toBe("Styr värmen");
    expect(card.querySelector("[data-action='test']")).toBeNull();
    expect(card.querySelector("[data-line='test-explain']")).toBeNull();
    expect(card.querySelector("[data-line='shadow']")).toBeNull();
    expect(controlLine("sv", controlled)).toBe("Styr: sänker värmen till 19:00.");
    // SpotNav's own lowerings in the relay's row, said in words and in the legend.
    const marks = [...card.querySelectorAll<SVGElement>("[data-mark='executed']")];
    expect(marks).toHaveLength(3);
    expect(card.textContent).toContain("07:00: Sänkt, 20,5 °C · SpotNav sänkte 60 min");
    expect([...card.querySelectorAll("li")].map((item) => item.textContent)).toContain("SpotNav sänkte");
    // The villa before: the test chip, nothing executed.
    const villa = body(answer());
    expect(villa.querySelector("[data-action='test']")).not.toBeNull();
    expect(villa.querySelectorAll("[data-mark='executed']")).toHaveLength(0);
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

  it("keeps the switch away from a path it cannot steer", () => {
    const pump = answer("network_pump.json");
    const section = heatSettingsSection(document, "sv", pump, () => undefined);
    expect(section.querySelector("[data-row='steer']")).toBeNull();
  });
});

describe("the indoor curve under the band", () => {
  it("draws the rooms up to now, the coldest room, the band's setpoint as a step and the comfort band", () => {
    const value = answer();
    expect(value.indoor_day?.points.length).toBe(69);
    const block = body(value).querySelector("[data-block='indoor']")!;
    expect(block.previousElementSibling?.getAttribute("data-block")).toBe("band");
    const mean = block.querySelector("[data-line='mean']")!.getAttribute("d")!;
    expect(mean.startsWith("M")).toBe(true);
    expect(mean.match(/M/g)).toHaveLength(1);
    expect(block.querySelector("[data-line='coldest']")).not.toBeNull();
    // The setpoint follows the band's hours over the whole day: 24 levels, dropping in the lowered hours.
    const setpoint = block.querySelector("[data-line='setpoint']")!.getAttribute("d")!;
    expect(setpoint.match(/L/g)).toHaveLength(24 * 2 - 1);
    expect(block.querySelector(".spotnav-heat-indoor-comfort")).not.toBeNull();
    expect(block.querySelector(".spotnav-heat-band-now")).not.toBeNull();
    const legend = [...block.querySelectorAll(".spotnav-heat-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual(["Rummen, medel", "Kallaste rummet", "Börvärde (SpotNav sänker)", "Komfortband 20,5–21,5 °C"]);
  });

  it("says plain setpoint when nothing is lowered, leaves out a coldest room that never differs, and draws nothing without points", () => {
    const value = answer();
    const flat = {
      ...value,
      band: { ...value.band, hours: value.band.hours.map((hour) => ({ ...hour, action: "normal", target_c: 21 })) },
      indoor_day: { ...value.indoor_day!, points: value.indoor_day!.points.map((point) => ({ ...point, coldest_c: point.mean_c })) },
    };
    const block = body(flat, "en").querySelector("[data-block='indoor']")!;
    const legend = [...block.querySelectorAll(".spotnav-heat-legend-item")].map((item) => item.textContent);
    expect(legend).toEqual(["Rooms, mean", "Setpoint", "Comfort band 20.5–21.5 °C"]);
    expect(block.querySelector("[data-line='coldest']")).toBeNull();
    expect(body({ ...value, indoor_day: { ...value.indoor_day!, points: [] } }).querySelector("[data-block='indoor']")).toBeNull();
    expect(body({ ...value, indoor_day: null }).querySelector("[data-block='indoor']")).toBeNull();
  });

  it("refuses an unknown key in the curve", () => {
    const raw = read("villa.json");
    const day = raw["indoor_day"] as Record<string, unknown>;
    expect(decodeHeat({ ...raw, indoor_day: { ...day, outdoor_c: 4 } }).ok).toBe(false);
  });
});
