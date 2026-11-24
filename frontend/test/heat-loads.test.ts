// The heat card's Pool, Spa and Sauna tabs (`pool-view.ts`, `spa-view.ts`), their place in the Plan tab, their settings
// rows and editors, and the card's presses through `spotnav/pool`, `spotnav/spa` and `spotnav/sauna`. The answer is the
// backend's contract fixture (`tests/fixtures/heat_card/controlled.json`: a pool, a Balboa spa and a relay sauna, all
// watched).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { HEAT_CARD_ELEMENT, loadErrorKey, type SpotnavHeatCard } from "../src/heat-card";
import type { HeatPart } from "../src/heat-history";
import { partSeries, partWord } from "../src/heat-use";
import { heatEditor, sessionsEditor } from "../src/heat-editors";
import { decodeHeat, decodeLoadAnswer, heatFound, type HeatAnswer } from "../src/heat-model";
import { heatBody, heatSettingsSection, heatTabsOf, loadFound, loadSettingsSections, powerWords, type HeatHandlers, type HeatUi } from "../src/heat-view";
import { registerTranslation } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { defineCard } from "../src/index";
import { poolExtraLine, poolPlanLine, poolSourceLine, poolStatusLine, poolTodayLines, poolWouldLine } from "../src/pool-view";
import { deadlineControlLine, deadlineStatusLine, deadlineWouldLine, loadControlLine, notReadyChoices, notReadyLine, sessionsText, weekdaysText } from "../src/spa-view";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
/** The fixtures' moment: 16 November 2026, 17:20 in Stockholm. */
const NOW = Date.parse("2026-11-16T16:20:00+00:00");

function raw(name = "controlled.json"): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, "heat_card", name), "utf8")) as Record<string, unknown>;
}

function answer(change: (value: Record<string, unknown>) => void = () => undefined): HeatAnswer {
  const value = raw();
  change(value);
  const decoded = decodeHeat(value);
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

function handlers(extra: Partial<HeatHandlers> = {}): HeatHandlers {
  return { onStep: vi.fn(), onHome: vi.fn(), onAway: vi.fn(), onSettings: vi.fn(), ...extra };
}

function body(ui: Partial<HeatUi>, value: HeatAnswer = answer(), extra: Partial<HeatHandlers> = {}): HTMLElement {
  return heatBody({
    doc: document,
    language: "sv",
    state: { kind: "ready", answer: value },
    ui: { pendingTarget: null, error: null, ...ui },
    handlers: handlers(extra),
    width: 330,
    nowMs: NOW,
    idPrefix: "t",
  });
}

function lineOf(root: ParentNode, key: string): string | null {
  return root.querySelector<HTMLElement>(`[data-line='${key}']`)?.textContent ?? null;
}

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

describe("the tabs a site has", () => {
  it("adds Pool, Spa and Sauna where they are set up, in that order, and a remembered one gone falls back to Now", () => {
    const value = answer();
    expect(heatTabsOf(value)).toEqual(["now", "plan", "use", "pool", "spa", "sauna"]);
    const none = answer((v) => {
      v["pool"] = null;
      v["sauna"] = null;
      (v["config"] as Record<string, unknown>)["pool"] = null;
      (v["config"] as Record<string, unknown>)["sauna"] = null;
      v["choices"] = null;
    });
    expect(heatTabsOf(none)).toEqual(["now", "plan", "use", "spa"]);
    const card = body({ tab: "pool" }, none);
    expect(card.dataset["tab"]).toBe("now");
    // Locked by the YAML to a load that is not there: one line, no dial.
    const locked = body({ locked: "pool" }, none);
    expect(lineOf(locked, "pool-none")).toBe("Ingen pool är inställd.");
  });

  it("shows a detected load, not set up yet, to an administrator with the way to set it up", () => {
    const detected = answer((v) => {
      v["pool"] = null;
      (v["config"] as Record<string, unknown>)["pool"] = null;
    });
    expect(loadFound(detected, "pool")).toBe("detected");
    expect(heatFound(detected)).toBe(true);
    const onSettings = vi.fn();
    const card = body({ tab: "pool" }, detected, { onSettings });
    expect(lineOf(card, "pool-none")).toBe("SpotNav hittade en poolvärmepump. Ställ in den i inställningarna under Värme.");
    card.querySelector<HTMLButtonElement>("button[data-action='pool-set-up']")!.click();
    expect(onSettings).toHaveBeenCalledOnce();
  });
});

describe("the Pool tab", () => {
  it("says what the pool does, what SpotNav would do, today's hours and the plan", () => {
    const value = answer();
    const pool = value.pool!;
    expect(poolStatusLine("sv", pool)).toBe("Cirkulation och värmepump står still");
    expect(poolWouldLine("sv", value, pool)).toBe("Skulle stå still till 22:00");
    expect(poolTodayLines("sv", pool).map((line) => line.text)).toEqual([
      "Filtrering i dag: 3 h klart, 4 h planerat av 8,3 h",
      "Värmepumpen: 2 h planerat",
    ]);
    expect(poolPlanLine("sv", value, pool)).toBe("Filtrerar 22:00–02:00 · Värmer tis 17 nov. 12:00–14:00");
    expect(poolExtraLine("sv", value, pool)).toBeNull();
    // Where the water was read and when: the heat pump's own first, a standalone sensor's otherwise; kept while the
    // circulation stands still.
    expect(poolSourceLine("sv", value, pool)).toBe("Från värmepumpen, 07:40");
    expect(poolSourceLine("sv", value, { ...pool, water_source: "sensor", water_at: "2026-11-15T20:05:00+00:00" })).toBe("Från poolgivaren, sön 15 nov. 21:05");
    expect(poolSourceLine("sv", value, { ...pool, water_c: null, water_source: null, water_at: null })).toBeNull();
  });

  it("draws the dial, the season, the extra filtering and the band, and sends each press", () => {
    const onPool = vi.fn();
    const onLoadStep = vi.fn();
    const card = body({ tab: "pool" }, answer(), { onPool, onLoadStep });
    const panel = card.querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(panel.querySelector("svg[role='img']")!.getAttribute("aria-label")).toBe("Poolvattnets mål, 27,0 °C, 24,6 °C");
    expect(lineOf(panel, "band")).toBe("Vattnet 24,6 °C");
    expect(lineOf(panel, "pool-source")).toBe("Från värmepumpen, 07:40");
    panel.querySelector<HTMLButtonElement>("button[data-step='up']")!.click();
    expect(onLoadStep).toHaveBeenCalledWith("pool", 0.5);
    expect(panel.querySelector("button[data-mode='season-open']")!.getAttribute("aria-pressed")).toBe("true");
    panel.querySelector<HTMLButtonElement>("button[data-mode='season-closed']")!.click();
    expect(onPool).toHaveBeenLastCalledWith({ action: "season", open: false });
    panel.querySelector<HTMLButtonElement>("button[data-action='pool-extra-4']")!.click();
    expect(onPool).toHaveBeenLastCalledWith({ action: "extra_filtering", hours: 4 });
    // The band from the running hour: idle until 22:00, then filtering.
    const cells = [...panel.querySelectorAll<HTMLElement>("[data-block='pool-band'] .spotnav-heat-ahead-cell")];
    expect(cells).toHaveLength(24);
    expect(cells.slice(0, 5).every((cell) => cell.dataset["run"] === undefined)).toBe(true);
    expect(cells[5]!.dataset["run"]).toBe("filter");
    // The circulation is SpotNav's: its line in place of the watched note, with the write that did not read back.
    expect(lineOf(panel, "pool-watched")).toBeNull();
    expect(lineOf(panel, "pool-control-circulation")).toBe("Cirkulationen: Styrs av SpotNav. Ändringar som inte lästes tillbaka: 1.");
    expect(lineOf(panel, "pool-control-heat-pump")).toBeNull();
  });

  it("with the season closed says SpotNav leaves it alone, and offers no extra filtering or plan", () => {
    const closed = answer((v) => {
      (v["pool"] as Record<string, unknown>)["season_open"] = false;
      (v["pool"] as Record<string, unknown>)["would"] = null;
    });
    const panel = body({ tab: "pool" }, closed).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(lineOf(panel, "pool-status")).toBe("Säsongen är stängd: SpotNav rör inte poolen.");
    expect(lineOf(panel, "pool-would")).toBeNull();
    expect(panel.querySelector("[data-block='pool-extra']")).toBeNull();
    expect(panel.querySelector("[data-block='pool-band']")).toBeNull();
  });

  it("shows an extra filtering asked for with when it would run and its cancel, and the season hint", () => {
    const onPool = vi.fn();
    const extra = answer((v) => {
      const pool = v["pool"] as Record<string, unknown>;
      pool["extra"] = { hours: 2, until: "2026-11-17T16:20:00+00:00" };
      pool["planned"] = [...(pool["planned"] as unknown[]), { start: "2026-11-17T02:00:00+00:00", end: "2026-11-17T04:00:00+00:00", what: "extra", reason: "extra" }];
      pool["season_hint"] = true;
    });
    const panel = body({ tab: "pool" }, extra, { onPool }).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(lineOf(panel, "pool-extra")).toBe("Extra filtrering 2 h: tis 17 nov. 03:00–05:00");
    panel.querySelector<HTMLButtonElement>("button[data-action='pool-extra-cancel']")!.click();
    expect(onPool).toHaveBeenLastCalledWith({ action: "cancel_extra" });
    expect(lineOf(panel, "pool-season-hint")).toBe("Vattnet har legat runt 10 °C i tre dagar – dags att stänga poolsäsongen?");
  });

  it("leaves the explaining lines out in a box of fixed height, the dial taking the height", () => {
    const panel = body({ tab: "pool", fill: 180 }).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(panel.querySelector<SVGElement>("svg[role='img']")!.style.height).toBe("180px");
    expect(lineOf(panel, "pool-today")).toBeNull();
    expect(lineOf(panel, "pool-watched")).toBeNull();
    expect(lineOf(panel, "pool-status")).not.toBeNull();
  });
});

describe("the Spa and Sauna tabs", () => {
  it("words weekdays and times like departures", () => {
    expect(weekdaysText("sv", [1, 2, 3, 4, 5])).toBe("mån–fre");
    expect(weekdaysText("sv", [5, 6])).toBe("fre, lör");
    expect(weekdaysText("sv", [1, 2, 3, 4, 5, 6, 7])).toBe("varje dag");
    const value = answer();
    expect(sessionsText("sv", value, "spa", [{ weekdays: [5, 6], time: "19:00" }], "2026-11-17T17:00:00+00:00")).toBe("fre, lör 19:00 · en gång tis 17 nov. 18:00");
    expect(sessionsText("sv", value, "sauna", [], null)).toBe("Inga bastupass");
  });

  it("says the spa's status with its learnt rate and what SpotNav would do", () => {
    const value = answer();
    expect(deadlineStatusLine("sv", value, "spa", value.spa!)).toBe("Klar 18:40, värmer 2 °C/h");
    expect(deadlineWouldLine("sv", value, "spa", value.spa!)).toBe("Skulle värma till 18:45: billiga timmar");
  });

  it("draws the spa's dial, Bada nu, the bath times and the idle temperature, and sends each press", () => {
    const onDeadline = vi.fn();
    const onLoadEdit = vi.fn();
    const onLoadStep = vi.fn();
    const panel = body({ tab: "spa" }, answer(), { onDeadline, onLoadEdit, onLoadStep }).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(panel.querySelector("svg[role='img']")!.getAttribute("aria-label")).toBe("Badtemperatur, 38,0 °C, 36,5 °C");
    panel.querySelector<HTMLButtonElement>("button[data-step='down']")!.click();
    expect(onLoadStep).toHaveBeenCalledWith("spa", -0.5);
    panel.querySelector<HTMLButtonElement>("button[data-action='spa-now']")!.click();
    expect(onDeadline).toHaveBeenLastCalledWith("spa", "bath_now");
    expect(lineOf(panel, "spa-times")).toBe("Badtider: fre, lör 19:00Ändra");
    panel.querySelector<HTMLButtonElement>("button[data-action='spa-times-change']")!.click();
    expect(onLoadEdit).toHaveBeenLastCalledWith("spa", "times");
    panel.querySelector<HTMLButtonElement>("button[data-action='spa-idle-change']")!.click();
    expect(onLoadEdit).toHaveBeenLastCalledWith("spa", "idle");
    expect(lineOf(panel, "spa-band")).toBe("Värms 17:20–18:45 · Hålls varmt till 21:00");
  });

  it("offers a bath pressed now its end and a cancel", () => {
    const onDeadline = vi.fn();
    const now = answer((v) => {
      (v["spa"] as Record<string, unknown>)["bath_now"] = { until: "2026-11-16T19:00:00+00:00" };
    });
    const panel = body({ tab: "spa" }, now, { onDeadline }).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(lineOf(panel, "spa-now")).toBe("Bada nu: skulle värma och hålla till 20:00");
    panel.querySelector<HTMLButtonElement>("button[data-action='spa-cancel-now']")!.click();
    expect(onDeadline).toHaveBeenLastCalledWith("spa", "cancel_now");
  });

  it("says a sauna session that will not be ready, with only the choices its cause allows, and the relay's safety plainly", () => {
    const value = answer();
    const sauna = value.sauna!;
    expect(notReadyLine("sv", value, "sauna", sauna.not_ready!)).toBe(
      "Bastupasset tis 17 nov. 17:00 blir inte klart i tid: det når 60,0 °C. Säkringen ger för lite utrymme.",
    );
    // The fuse: no peak to allow.
    expect(notReadyChoices("sv", value, "sauna", sauna.not_ready!).map((choice) => choice.action)).toEqual(["take_what_fits", "later_session"]);
    const onDeadline = vi.fn();
    const panel = body({ tab: "sauna" }, value, { onDeadline }).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(panel.querySelector("svg[role='img']")!.getAttribute("aria-label")).toBe("Bastutemperatur, 80 °C");
    panel.querySelector<HTMLButtonElement>("button[data-action='sauna-later_session']")!.click();
    expect(onDeadline).toHaveBeenLastCalledWith("sauna", "later_session");
    expect(lineOf(panel, "sauna-control")).toContain("SpotNav startar aldrig ett aggregat på relä utan eget säkerhetsskydd");
    expect(panel.querySelector("[data-action='sauna-now']")!.textContent).toBe("Värm nu");
  });

  it("says a locked sauna and one that must be started by hand plainly", () => {
    const locked = answer((v) => {
      const sauna = v["sauna"] as Record<string, unknown>;
      sauna["locked"] = true;
      sauna["would"] = { action: "blocked", until: null, reason: "lock" };
    });
    const panel = body({ tab: "sauna" }, locked).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(lineOf(panel, "sauna-locked")).toBe("Låst: dörren eller säkerhetsbrytaren hindrar bastun från att starta.");
    expect(lineOf(panel, "sauna-would")).toBe("Skulle inte starta: dörren eller säkerhetslåset hindrar");
    const byHand = answer((v) => {
      const sauna = v["sauna"] as Record<string, unknown>;
      sauna["would"] = { action: "start_by_hand", until: null, reason: "no_safety" };
      sauna["next_session"] = { at: "2026-11-16T18:00:00+00:00", source: "weekly" };
    });
    expect(deadlineWouldLine("sv", byHand, "sauna", byHand.sauna!)).toBe("Starta för hand nu för att vara klar 19:00: SpotNav säger bara när");
  });
});

describe("the Plan tab", () => {
  it("draws the pool's, the spa's and the sauna's next hours under the day's chart, each named", () => {
    const panel = body({ tab: "plan" }).querySelector<HTMLElement>("[role='tabpanel']")!;
    expect(lineOf(panel, "plan-pool")).toBe("Pool: Filtrerar 22:00–02:00 · Värmer tis 17 nov. 12:00–14:00");
    expect(lineOf(panel, "plan-spa")).toBe("Spa: Värms 17:20–18:45 · Hålls varmt till 21:00");
    expect(lineOf(panel, "plan-sauna")).toBe("Bastu: Inget planerat de närmaste 24 timmarna");
  });
});

describe("the Use tab", () => {
  it("names and colours the pool, the spa and the sauna as parts of their own", () => {
    const parts: HeatPart[] = (["pump", "pool", "spa", "sauna"] as const).map((kind) => ({ key: kind, kind, name: null, kwh: 1, cost: null }));
    expect(parts.map((part) => partWord("sv", part))).toEqual(["Värmepump", "Pool", "Spa", "Bastu"]);
    expect(parts.map((part) => partSeries(parts, part))).toEqual(["use-pump", "use-pool", "use-spa", "use-sauna"]);
  });
});

describe("the settings rows and editors", () => {
  it("lists the pool's, the spa's and the sauna's settings, with what SpotNav would do (their phones are the Notifications section's)", () => {
    // The house's heat keeps "Värme"; the pool, the spa and the sauna each have their own section.
    expect(heatSettingsSection(document, "sv", answer(), () => undefined).querySelector("[data-row^='pool'], [data-row='spa']")).toBeNull();
    const sections = loadSettingsSections(document, "sv", answer(), () => undefined);
    const rowsOf = (node: HTMLElement): (string | undefined)[] => [...node.querySelectorAll<HTMLElement>("[data-row]")].map((row) => row.dataset["row"]);
    expect(sections.map((node) => [node.dataset["section"], node.querySelector("h4")!.textContent, rowsOf(node)])).toEqual([
      [
        "pool", "Pool",
        ["poolHeatPump", "poolCirculation", "poolSensor", "poolFilterHours", "poolHeatPumpKw", "poolCirculationKw", "poolCirculationControl", "poolHeatPumpControl"],
      ],
      ["spa", "Spa", ["spa", "spaFloor", "spaKw", "spaControl"]],
      ["sauna", "Bastu", ["sauna", "saunaKw", "saunaSafety"]],
    ]);
    const rows = sections.flatMap(rowsOf);
    const section = document.createElement("div");
    section.append(...sections);
    expect(section.querySelector("[data-row='poolCirculationControl']")!.textContent).toBe("Styr cirkulationenPå");
    expect(section.querySelector("[data-row='poolHeatPumpControl']")!.textContent).toBe("Styr poolens värmepumpAv");
    // A relay sauna without its declared safety has no opt-in row.
    expect(rows).not.toContain("saunaControl");
    expect(section.querySelector("[data-help='pool']")!.textContent).toBe("Skulle stå still till 22:00");
    expect(section.querySelector("[data-row='poolFilterHours']")!.textContent).toBe("Filtrering per dygnEfter vattentemperaturen");
    expect(section.querySelector("[data-row='saunaSafety']")!.textContent).toBe("Aggregatets eget säkerhetsskyddNej, eller vet inte");
    // Each power says where it comes from: entered, or learned from the load's own switching.
    expect(section.querySelector("[data-row='poolHeatPumpKw']")!.textContent).toMatch(/1,6\s*kW, angiven$/);
    expect(section.querySelector("[data-row='poolCirculationKw']")!.textContent).toMatch(/0,4\s*kW, inlärd$/);
  });

  it("words a load's power as entered, learned or typical until learned", () => {
    expect(powerWords("sv", 8.7, { kw: 8.7, kw_source: "entered" })).toMatch(/^8,7\s*kW, angiven$/);
    expect(powerWords("sv", null, { kw: 8.7, kw_source: "learned" })).toMatch(/^8,7\s*kW, inlärd$/);
    expect(powerWords("sv", null, { kw: 9, kw_source: "typical" })).toMatch(/kW, vanligt värde tills den är inlärd$/);
    expect(powerWords("en", null, null)).toBe("Not given");
  });

  it("offers the heat pump's own reading first for the water, a standalone sensor as the second choice", () => {
    const built = heatEditor({ doc: document, language: "sv", idPrefix: "t", answer: answer(), write: vi.fn(async () => null) }, "poolSensor", { onDone: vi.fn(), onCancel: vi.fn() })!;
    const options = [...built.body.querySelectorAll<HTMLInputElement>("input[type='radio']")].map((input) => input.value);
    expect(options[0]).toBe("");
    expect(built.body.textContent).toContain("Bara värmepumpens egen");
    expect(built.body.textContent).toContain("Värmepumpens egen avläsning används först och en vald givare i andra hand.");
  });

  it("confirms a detected pool once as a group", async () => {
    const detected = answer((v) => {
      v["pool"] = null;
      (v["config"] as Record<string, unknown>)["pool"] = null;
      (v["choices"] as Record<string, unknown>)["pool"] = (raw("villa.json")["choices"] as Record<string, unknown>)["pool"];
    });
    const write = vi.fn(async () => null);
    const built = heatEditor({ doc: document, language: "sv", idPrefix: "t", answer: detected, write }, "poolSuggested", { onDone: vi.fn(), onCancel: vi.fn() })!;
    expect(built.title).toBe("Pool");
    expect(built.body.textContent).toContain("SpotNav hittade poolvärmepumpen Poolpump.");
    document.body.append(built.body);
    built.body.querySelector<HTMLInputElement>("input[value='yes']")!.click();
    built.body.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(write).toHaveBeenCalled());
    expect(write).toHaveBeenCalledWith({
      pool: {
        heat_pump: { entity_id: "climate.poolpump", shape: "climate", switch_id: null, number_id: null },
        circulation_id: "switch.pool_cirkulation",
        water_sensor: "sensor.pool_vatten",
      },
    });
    document.body.replaceChildren();
  });

  it("edits the bath times as weekly times and one date", async () => {
    const value = answer();
    const save = vi.fn(async () => null);
    const form = sessionsEditor(document, "sv", value, "spa", { weekly: value.spa!.baths, once: null }, save, { onDone: vi.fn(), onCancel: vi.fn() });
    document.body.append(form);
    const days = [...form.querySelectorAll<HTMLInputElement>("input[data-weekday]")];
    expect(days.filter((day) => day.checked).map((day) => day.dataset["weekday"])).toEqual(["5", "6"]);
    days[6]!.click();
    form.querySelector<HTMLButtonElement>("button[data-action='add-time']")!.click();
    expect(form.querySelectorAll("[data-part='weekly-time']")).toHaveLength(2);
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(save).toHaveBeenCalled());
    expect(save).toHaveBeenCalledWith({
      weekly: [
        { weekdays: [5, 6, 7], time: "19:00" },
        { weekdays: [1, 2, 3, 4, 5, 6, 7], time: "19:00" },
      ],
      once: null,
    });
    document.body.replaceChildren();
  });
});

describe("the card's presses", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(NOW);
    defineCard();
  });
  afterEach(() => {
    vi.useRealTimers();
    document.body.replaceChildren();
  });

  function mount(reply: (message: Record<string, unknown>) => unknown = () => ({ api_version: 1, ok: true, error: null })) {
    const messages: Record<string, unknown>[] = [];
    const hass = {
      language: "sv",
      locale: { language: "sv" },
      states: {},
      user: { is_admin: false },
      callWS: async (message: Record<string, unknown>): Promise<unknown> => {
        messages.push(message);
        if (message["type"] === "spotnav/get_heat") {
          return raw();
        }
        return reply(message);
      },
    };
    const element = document.createElement(HEAT_CARD_ELEMENT) as SpotnavHeatCard;
    element.store = () => null;
    element.setConfig({ type: "custom:spotnav-heat-card", site: "site_villa", tab: "pool" });
    element.hass = hass as never;
    document.body.append(element);
    const sent = (type: string) => messages.filter((message) => message["type"] === type);
    return { element, sent, root: element.shadowRoot! };
  }

  it("writes the pool's dial once after a run of taps, and sends the extra filtering", async () => {
    const { root, sent } = mount(() => ({ api_version: 1, ok: true, error: null, pool: null }));
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("button[data-step='up']")!.click();
    root.querySelector<HTMLButtonElement>("button[data-step='up']")!.click();
    expect(root.querySelector(".spotnav-heat-dial-figure")!.textContent).toBe("28,0°");
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent("spotnav/pool")).toEqual([{ type: "spotnav/pool", api_version: 1, site_id: "site_villa", action: "target", target_c: 28 }]);
    root.querySelector<HTMLButtonElement>("button[data-action='pool-extra-2']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(sent("spotnav/pool").at(-1)).toEqual({ type: "spotnav/pool", api_version: 1, site_id: "site_villa", action: "extra_filtering", hours: 2 });
  });

  it("says a refused press in the tab", async () => {
    const { root } = mount(() => ({ api_version: 1, ok: false, error: "spotnav_rate_limited", pool: null }));
    await vi.advanceTimersByTimeAsync(0);
    root.querySelector<HTMLButtonElement>("button[data-action='pool-extra-2']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[role='alert']")!.textContent).toBe("Trycktes nyss; försök igen om några minuter.");
  });

  it("decodes the presses' answers and words their refusals", () => {
    expect(decodeLoadAnswer("spa", { api_version: 1, ok: true, error: null, spa: null })).toEqual({ ok: true, value: { ok: true, error: null } });
    expect(decodeLoadAnswer("spa", { api_version: 1, ok: true, error: null }).ok).toBe(false);
    expect(decodeLoadAnswer("pool", { api_version: 2, ok: true, error: null, pool: null })).toEqual({ ok: false, failure: "unsupported" });
    expect(loadErrorKey("spotnav_no_later_bath")).toBe("heat.load.noLater");
    expect(loadErrorKey("spotnav_no_later_session")).toBe("heat.load.noLater");
    expect(loadErrorKey("other")).toBe("heat.error.generic");
  });
});

describe("SpotNav's control of the pool, the spa and the sauna", () => {
  const control = (state: string, reason: string | null = null, paused_until: string | null = null, failures = 0) => ({ state, reason, paused_until, failures });

  it("words each state with its reason: controlled, paused, handed back, through the cloud, and the writes not read back", () => {
    const value = answer();
    const tomorrow = "2026-11-16T23:00:00+00:00";
    expect(loadControlLine("sv", value, control("watched"))).toBeNull();
    expect(loadControlLine("sv", value, control("controlled"))).toBe("Styrs av SpotNav.");
    expect(loadControlLine("sv", value, control("controlled", "interlock"))).toBe("Styrs av SpotNav: väntar på att cirkulationen går.");
    expect(loadControlLine("sv", value, control("controlled", "cloud_slow"))).toBe("Styrs av SpotNav via molnet: bara långsamma, planerade ändringar.");
    expect(loadControlLine("sv", value, control("paused", "write_cap", tomorrow))).toMatch(/^Pausad till .+: dagens ändringar är använda\.$/);
    expect(loadControlLine("sv", value, control("paused", "automation"))).toBe("Pausad: en automation styr den.");
    expect(loadControlLine("sv", value, control("handed_back", "foreign_change", tomorrow))).toMatch(/^Återlämnad till dig till .+: någon annan ändrade den/);
    expect(loadControlLine("sv", value, control("paused", "no_read_back", tomorrow, 2))).toMatch(/Ändringar som inte lästes tillbaka: 2\.$/);
    expect(loadControlLine("en", value, control("controlled", "floor"))).toBe("Controlled by SpotNav: below its lowest temperature, heating.");
    expect(loadControlLine("sv", value, control("controlled", "lock_unreadable"))).toBe("Styrs av SpotNav: låset går inte att läsa, startar inte.");
    expect(loadControlLine("sv", value, control("controlled", "stale_plan"))).toBe("Styrs av SpotNav: planen görs inte just nu, inget ändras.");
    expect(loadControlLine("sv", value, control("paused", "restore_waits"))).toBe("Pausad: din inställning Värme skrivs tillbaka när cirkulationen går.");
    expect(loadControlLine("en", value, control("controlled", "pump_not_ours"))).toContain("turn on Control the pool heat pump");
  });

  it("says the spa's control in its tab in place of the watched note", () => {
    const value = answer((v) => {
      (v["spa"] as Record<string, unknown>)["control"] = control("controlled", "floor");
    });
    expect(deadlineControlLine("sv", "spa", value.spa!, value)).toBe("Styrs av SpotNav: under lägsta temperaturen, värmer.");
    // A sauna SpotNav only watches keeps its own note.
    expect(deadlineControlLine("sv", "sauna", value.sauna!, value)).toBe(deadlineControlLine("sv", "sauna", value.sauna!));
  });

  it("writes each opt-in in one tap, with what SpotNav does and what is tested", async () => {
    const write = vi.fn(async () => null);
    const value = answer();
    const built = heatEditor({ doc: document, language: "sv", idPrefix: "t", answer: value, write }, "poolHeatPumpControl", { onDone: vi.fn(), onCancel: vi.fn() })!;
    expect(built.title).toBe("Styr poolens värmepump");
    expect(built.body.textContent).toContain("bara när cirkulationen har gått en minut");
    expect(built.body.textContent).toContain("andra vägar är otestade");
    document.body.append(built.body);
    built.body.querySelector<HTMLInputElement>("input[value='on']")!.click();
    built.body.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(write).toHaveBeenCalledWith({ pool: { control_heat_pump: true } }));
    const spa = heatEditor({ doc: document, language: "sv", idPrefix: "t", answer: value, write }, "spaControl", { onDone: vi.fn(), onCancel: vi.fn() })!;
    expect(spa.title).toBe("Styr spat");
    document.body.append(spa.body);
    spa.body.querySelector<HTMLInputElement>("input[value='on']")!.click();
    spa.body.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(write).toHaveBeenCalledWith({ spa: { control: true } }));
    // The sauna's row shows once a relay's safety is declared.
    const declared = answer((v) => {
      ((v["config"] as Record<string, unknown>)["sauna"] as Record<string, unknown>)["safety_declared"] = true;
    });
    const rows = loadSettingsSections(document, "sv", declared, () => undefined).flatMap((node) => [...node.querySelectorAll<HTMLElement>("[data-row]")].map((row) => row.dataset["row"]));
    expect(rows).toContain("saunaControl");
    const sauna = heatEditor({ doc: document, language: "sv", idPrefix: "t", answer: declared, write }, "saunaControl", { onDone: vi.fn(), onCancel: vi.fn() })!;
    expect(sauna.body.textContent).toContain("aldrig när dörren eller låset säger nej");
  });
});
