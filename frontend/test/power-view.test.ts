// The history dialog's Power view: the decoder on the backend's own fixtures, the month and day charts, and the card
// asking for it from the dialog's second tab.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { decodeSessions, historyBody, powerPickerMonths, type HistoryUi } from "../src/history";
import { LANGUAGES, registerTranslation, translate } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { decodePowerMonth, dayFigures, hourFigures, partNames, powerBody, scaleTop, type PowerMonth, type PowerUi } from "../src/power-view";
import { apiFailure, FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, path), "utf8")) as Record<string, unknown>;
}

function month(path = "power_month/pilot_month.json"): PowerMonth {
  const decoded = decodePowerMonth(read(path));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

const UI: PowerUi = { day: null, pending: false };

function render(answer: PowerMonth, ui: PowerUi = UI, onDay: (day: string | null) => void = () => undefined, language: (typeof LANGUAGES)[number] = "en") {
  return powerBody(document, language, { kind: "ready", answer }, ui, { onDay });
}

describe("decoding the backend's own answer", () => {
  it("reads the running month with its hours and an older month with its days only", () => {
    const current = month();
    expect(current.month).toBe("2026-09");
    expect(current.available_months).toEqual(["2026-09", "2026-08"]);
    expect(current.import_target_kw).toBe(6);
    expect(current.components).toHaveLength(2);
    expect(current.days).toHaveLength(22);
    expect(current.hours).toHaveLength(21 * 24 + 8);
    expect(current.bars).toHaveLength(2);
    const older = month("power_month/pilot_older_month.json");
    expect(older.hours).toBeNull();
    expect(older.days).toHaveLength(31);
  });

  it("reads the webhook's answer as the same document", () => {
    const decoded = decodePowerMonth(read("webhook/get_power_month_success.json"));
    expect(decoded.ok).toBe(true);
  });

  it("refuses what it cannot read and names a newer contract", () => {
    const raw = read("power_month/pilot_month.json");
    expect(decodePowerMonth({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodePowerMonth({ ...raw, unit: "W" }).ok).toBe(false);
    expect(decodePowerMonth({ ...raw, bars: [] }).ok).toBe(false);
    const days = raw["days"] as Array<Record<string, unknown>>;
    expect(decodePowerMonth({ ...raw, days: [{ ...days[0], counted: [5] }] }).ok).toBe(false);
    expect(decodePowerMonth({ ...raw, days: [{ ...days[0], peak_kw: -1 }] }).ok).toBe(false);
    expect(decodePowerMonth({ ...raw, extra: true }).ok).toBe(true);
    expect(decodePowerMonth("nonsense").ok).toBe(false);
  });
});

describe("the month", () => {
  it("has one bar per day of the month, split into house and car, and the target as a line", () => {
    const answer = month();
    const body = render(answer);
    const bars = body.querySelectorAll<HTMLButtonElement>("button[data-key]");
    expect(bars).toHaveLength(30);
    // Days 1-22 measured; the rest of September is empty.
    expect(body.querySelectorAll(".spotnav-power-stack")).toHaveLength(22);
    expect(body.querySelectorAll(".spotnav-power-plot .spotnav-power-car").length).toBeGreaterThan(0);
    expect(body.querySelectorAll(".spotnav-power-plot .spotnav-power-house").length).toBe(22);
    const line = body.querySelector<HTMLElement>(".spotnav-power-target");
    expect(line?.textContent).toBe("6.0 kW");
    expect(line?.style.bottom).toBe(`${(6 / scaleTop(answer.days.map((day) => day.peak_kw), 6)) * 100}%`);
  });

  it("outlines and tags each part's counted peak with the part's number, and names the parts in the legend", () => {
    const answer = month();
    const body = render(answer);
    const counted = Array.from(body.querySelectorAll<HTMLElement>("[data-counted]"));
    expect(counted.map((stack) => stack.dataset["counted"]).sort()).toEqual(["0", "1"]);
    expect(counted.map((stack) => stack.querySelector(".spotnav-power-tag")?.textContent).sort()).toEqual(["1", "2"]);
    const legend = body.querySelector("ul")?.textContent ?? "";
    expect(legend).toContain("Counted peak, day 06–23");
    expect(legend).toContain("Counted peak, night 23–06");
    expect(legend).toContain("Import target 6.0 kW");
    expect(legend).toContain("House");
    expect(legend).toContain("Car");
  });

  it("marks the days the import target held or paused a charger, and says so in the day's figures", () => {
    const answer = month();
    const body = render(answer);
    const marks = Array.from(body.querySelectorAll<HTMLElement>(".spotnav-power-marks > span")).map((mark) => mark.dataset["mark"] ?? "");
    expect(marks).toHaveLength(30);
    expect(marks[14]).toBe("paused");
    expect(marks.filter((mark) => mark === "held").length).toBeGreaterThan(0);
    const paused = answer.days.find((day) => day.date === "2026-09-15");
    expect(dayFigures("en", paused!, ["day 06–23", "night 23–06"])).toContain("the import target paused the charging");
  });

  it("says the month's figure per part above the chart", () => {
    const answer = month();
    const tile = render(answer).querySelector("[data-tile='power']");
    const one = new Intl.NumberFormat("en", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    expect(tile?.textContent).toBe(`Day 06–23: ${one.format(answer.bars[0]!)} kW · Night 23–06: ${one.format(answer.bars[1]!)} kW`);
    expect(render(answer, UI, () => undefined, "sv").querySelector("[data-tile='power']")?.textContent).toMatch(/^Dag 06–23: .* kW · Natt 23–06: .* kW$/);
  });

  it("shows a day's figures on hover and focus, and asks for the day's hours on a tap", () => {
    const answer = month();
    const asked: Array<string | null> = [];
    const body = render(answer, UI, (day) => asked.push(day));
    const readout = body.querySelector(".spotnav-history-readout") as HTMLElement;
    expect(readout.textContent).toBe("Tap a day for its hours; hover or focus a bar for its figures.");
    const bar = body.querySelector<HTMLButtonElement>("button[data-key='2026-09-15']")!;
    bar.dispatchEvent(new Event("mouseenter"));
    expect(readout.textContent).toMatch(/^Tue 15 Sept?: highest .* kW at \d\d:00 · house .* kW, car .* kW/);
    bar.click();
    expect(asked).toEqual(["2026-09-15"]);
    // An empty day only shows that nothing was measured.
    const empty = body.querySelector<HTMLButtonElement>("button[data-key='2026-09-28']")!;
    empty.click();
    expect(asked).toEqual(["2026-09-15"]);
    expect(readout.textContent).toMatch(/nothing measured$/);
  });

  it("keeps an older month to its days: a tap shows the figures, no hours", () => {
    const answer = month("power_month/pilot_older_month.json");
    const asked: Array<string | null> = [];
    const body = render(answer, UI, (day) => asked.push(day));
    expect(body.textContent).toContain("Only the days are kept for this month.");
    expect(body.querySelectorAll("button[data-key]")).toHaveLength(31);
    body.querySelector<HTMLButtonElement>("button[data-key='2026-08-10']")!.click();
    expect(asked).toEqual([]);
    expect(body.querySelector(".spotnav-history-readout")?.textContent).toMatch(/^Mon 10 Aug: highest/);
  });

  it("says when nothing was measured, and when it could not be read", () => {
    const answer = { ...month(), days: [], hours: [] };
    expect(render(answer).textContent).toContain("No hours were measured this month.");
    const failed = powerBody(document, "en", { kind: "failed", sentenceKey: "power.failed", code: "spotnav_no_site" }, UI, {
      onDay: () => undefined,
    });
    expect(failed.querySelector("[data-code='spotnav_no_site']")?.textContent).toBe("The power could not be read.");
  });
});

describe("the scale and the parts' names", () => {
  it("has a y axis of 0, a nice middle step and the top", () => {
    expect(scaleTop([6.6], 6)).toBe(8);
    expect(scaleTop([2.1], null)).toBe(3);
    expect(scaleTop([0], null)).toBe(1);
    expect(scaleTop([11], 6)).toBe(12);
    const body = render(month());
    const top = scaleTop(month().days.map((day) => day.peak_kw), 6);
    expect(Array.from(body.querySelectorAll(".spotnav-power-ticks > span")).map((tick) => tick.textContent)).toEqual([
      "0",
      String(top / 2),
      `${top.toFixed(1)} kW`,
    ]);
    expect(body.querySelectorAll(".spotnav-power-plot > .spotnav-power-grid")).toHaveLength(2);
  });

  it("names a part by its hours, its days and months only where not all, and numbers two that read the same", () => {
    const window = (from_hour: number, to_hour: number, extra: Record<string, unknown> = {}) =>
      ({ days: "all", from_month: 1, to_month: 12, from_hour, to_hour, ...extra }) as never;
    const part = (w: unknown) => ({ window: w as never, top_n: 1 as const, price_per_kw: { winter: null, summer: null } });
    expect(partNames("sv", [part(window(6, 23)), part(window(23, 6))])).toEqual({
      names: ["Dag 06–23", "Natt 23–06"],
      inline: ["dag 06–23", "natt 23–06"],
    });
    expect(partNames("sv", [part(null), part(window(7, 20, { days: "weekdays", from_month: 11, to_month: 3 }))]).names).toEqual([
      "Alla timmar",
      "Dag 07–20, vardagar, nov–mar",
    ]);
    expect(partNames("en", [part(window(7, 20, { days: "mon_sat" }))]).inline).toEqual(["day 07–20, Mon–Sat"]);
    expect(partNames("de", [part(window(6, 23))]).inline).toEqual(["Tag 06–23"]);
    expect(partNames("sv", [part(window(6, 23)), part(window(6, 23, { from_month: 11, to_month: 3 })), part(window(6, 23))])).toEqual({
      names: ["Effektavgift 1", "Effektavgift 2", "Effektavgift 3"],
      inline: ["effektavgift 1", "effektavgift 2", "effektavgift 3"],
    });
    const day = month().days.find((entry) => entry.counted.length > 0)!;
    expect(dayFigures("sv", day, partNames("sv", month().components).inline)).toMatch(/räknas för (dag 06–23|natt 23–06)/);
  });
});

describe("a tapped day", () => {
  it("shows its hours the same way, with the way back to the month", () => {
    const answer = month();
    const asked: Array<string | null> = [];
    const body = render(answer, { day: "2026-09-15", pending: false }, (day) => asked.push(day));
    const hours = body.querySelectorAll<HTMLButtonElement>("button[data-key]");
    expect(hours).toHaveLength(24);
    expect(body.querySelector("h4")?.textContent).toMatch(/^Tue 15 Sept?: highest/);
    expect(Array.from(body.querySelectorAll(".spotnav-history-axis > span")).map((span) => span.textContent).filter(Boolean)).toEqual([
      "00",
      "06",
      "12",
      "18",
    ]);
    const marks = Array.from(body.querySelectorAll<HTMLElement>(".spotnav-power-marks > span")).map((mark) => mark.dataset["mark"] ?? "");
    expect(marks[19]).toBe("paused");
    expect(body.querySelector(".spotnav-power-target")).not.toBeNull();
    const readout = body.querySelector(".spotnav-history-readout") as HTMLElement;
    hours[19]!.click();
    expect(readout.textContent).toMatch(/^19:00: .* kW · house .* kW, car 0\.0 kW · the import target paused the charging$/);
    (body.querySelector("[data-action='power-month']") as HTMLButtonElement).click();
    expect(asked).toEqual([null]);
  });

  it("marks an hour with gaps as such", () => {
    const answer = month();
    const body = render(answer, { day: "2026-09-13", pending: false });
    const stacks = Array.from(body.querySelectorAll<HTMLElement>(".spotnav-power-stack"));
    expect(stacks.filter((stack) => stack.dataset["incomplete"] === "true")).toHaveLength(1);
    const gap = answer.hours!.find((hour) => hour.incomplete && hour.start.startsWith("2026-09-13"))!;
    expect(hourFigures("en", gap)).toMatch(/gaps in the reading$/);
  });
});

describe("the words", () => {
  it("are in every language", () => {
    for (const language of LANGUAGES) {
      registerTranslation(language, TRANSLATIONS[language]);
      const text = render(month(), UI, () => undefined, language).textContent ?? "";
      expect(text.length).toBeGreaterThan(0);
      expect(translate(language, "history.view.power")).not.toBe("");
    }
    expect(translate("sv", "history.view.power")).toBe("Effekt");
    expect(render(month(), UI, () => undefined, "sv").querySelector("ul")?.textContent).toContain("Effektmål 6,0 kW");
  });
});

describe("the tabs in the history body", () => {
  const ui = (): HistoryUi => ({ view: "power", month: null, pending: false, day: null, exporting: false, notice: null });

  it("are there only where the power view is offered, and the power view keeps the month picker", () => {
    const sessions = decodeSessions(read("sessions/get_sessions.json"));
    if (!sessions.ok) {
      throw new Error("the fixture must decode");
    }
    const state = { kind: "ready" as const, answer: sessions.value };
    const handlers = { onMonth: () => undefined, onExport: () => undefined };
    const power = { offered: true, state: { kind: "ready" as const, answer: month() }, ui: UI, handlers: { onDay: () => undefined } };
    const shown = historyBody(document, "en", state, ui(), handlers, undefined, power);
    expect(Array.from(shown.querySelectorAll("button[data-view]")).map((tab) => [tab.textContent, tab.getAttribute("aria-pressed")])).toEqual([
      ["Charges", "false"],
      ["Power", "true"],
    ]);
    expect(shown.querySelector("[data-view='power']")).not.toBeNull();
    expect(Array.from((shown.querySelector("select") as HTMLSelectElement).options).map((option) => option.value)).toEqual(["2026-09", "2026-08"]);
    const hidden = historyBody(document, "en", state, ui(), handlers, undefined, { ...power, offered: false });
    expect(hidden.querySelector("button[data-view]")).toBeNull();
    expect(hidden.querySelector("[data-list='sessions']")).not.toBeNull();
  });

  it("offer the months with measured hours, the current one and the one shown", () => {
    expect(powerPickerMonths(["2026-08"], "2026-09", "2026-07")).toEqual(["2026-09", "2026-08", "2026-07"]);
    expect(powerPickerMonths(["2023-01"], "2026-09", "2026-09")).toEqual(["2026-09"]);
  });
});

// ------------------------------------------------------------------------------------------- the card

const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function dialog(element: Element): HTMLElement {
  const open = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find(
    (candidate) => candidate.closest("[hidden]") === null,
  );
  if (open === undefined) {
    throw new Error("no dialog is open");
  }
  return open;
}

async function opened(dashboard: string, options: { language?: string } = {}) {
  const hass = new FakeHass();
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  element.hass = hass.snapshot("snapshot", options.language ?? "en");
  await settle();
  hass.resolveNext(read(dashboard));
  await settle();
  (shadow(element).querySelector("button[data-history='open']") as HTMLButtonElement).click();
  await settle();
  hass.resolveNext(read("sessions/get_sessions.json"));
  await settle();
  return { hass, element };
}

describe("the card", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("offers no Power view where the site cannot measure its import", async () => {
    const { element } = await opened("dashboard/start_idle.json");
    expect(dialog(element).querySelector("button[data-view]")).toBeNull();
  });

  it("asks for the site's power when the tab is chosen, then for a picked month, and a tapped day shows its hours", async () => {
    const { hass, element } = await opened("dashboard/solar_direct_site_with_total.json");
    const tab = dialog(element).querySelector<HTMLButtonElement>("button[data-view='power']")!;
    tab.click();
    await settle();
    // The month the charges showed.
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_power_month", api_version: 1, charger_id: "entry_a", month: "2026-09" });
    expect(dialog(element).textContent).toContain("Reading the power…");
    hass.resolveNext(read("power_month/pilot_month.json"));
    await settle();
    expect(dialog(element).querySelectorAll(".spotnav-power-stack")).toHaveLength(22);

    dialog(element).querySelector<HTMLButtonElement>("button[data-key='2026-09-15']")!.click();
    await settle();
    expect(dialog(element).querySelectorAll("button[data-key]")).toHaveLength(24);
    dialog(element).querySelector<HTMLButtonElement>("[data-action='power-month']")!.click();
    await settle();
    expect(dialog(element).querySelectorAll("button[data-key]")).toHaveLength(30);

    dialog(element).querySelector<HTMLButtonElement>("button[data-month='2026-08']")!.click();
    await settle();
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_power_month", api_version: 1, charger_id: "entry_a", month: "2026-08" });
    expect(dialog(element).querySelector("[data-view='power'][data-pending]")).not.toBeNull();
    hass.resolveNext(read("power_month/pilot_older_month.json"));
    await settle();
    expect(dialog(element).querySelectorAll("button[data-key]")).toHaveLength(31);

    // Back to the charges: the month picked is read for them too.
    dialog(element).querySelector<HTMLButtonElement>("button[data-view='charges']")!.click();
    await settle();
    expect(hass.messages.at(-1)).toEqual({ type: "spotnav/get_sessions", api_version: 1, charger_id: "entry_a", limit: 20, month: "2026-08" });
  });

  it("says the power could not be read, with the stable code", async () => {
    const { hass, element } = await opened("dashboard/solar_direct_site_with_total.json");
    dialog(element).querySelector<HTMLButtonElement>("button[data-view='power']")!.click();
    await settle();
    hass.rejectNext(apiFailure("spotnav_needs_total_grid_power"));
    await settle();
    const failure = dialog(element).querySelector("[data-code]");
    expect(failure?.textContent).toBe("The power could not be read.");
    expect(failure?.getAttribute("data-code")).toBe("spotnav_needs_total_grid_power");
  });
});

describe("the month's peak and the days from Home Assistant's statistics", () => {
  const imported = (): PowerMonth => month("power_month/pilot_imported_month.json");

  it("reads the additive fields, and an answer without them as before", () => {
    const answer = imported();
    expect(answer.days.filter((day) => day.imported).map((day) => day.date)).toEqual(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]);
    expect(answer.hours!.some((hour) => hour.month_peak_kw === 6.8)).toBe(true);
    const older = month();
    expect(older.days.every((day) => !day.imported && day.month_peak_kw === null)).toBe(true);
    const raw = read("power_month/pilot_imported_month.json");
    const days = raw["days"] as Array<Record<string, unknown>>;
    const { imported: _imported, month_peak_kw: _peak, ...bare } = days[0]!;
    expect(decodePowerMonth({ ...raw, days: [bare] }).ok).toBe(true);
    expect(decodePowerMonth({ ...raw, days: [{ ...days[0], imported: "yes" }] }).ok).toBe(false);
    expect(decodePowerMonth({ ...raw, days: [{ ...days[0], month_peak_kw: -1 }] }).ok).toBe(false);
  });

  it("hatches the imported days, says so in the legend and a note, and in their figures", () => {
    const answer = imported();
    const body = render(answer, UI, () => undefined, "sv");
    expect(body.querySelectorAll(".spotnav-power-stack[data-imported='true']")).toHaveLength(4);
    const legend = body.querySelector("ul")?.textContent ?? "";
    expect(legend).toContain("Från Home Assistants statistik");
    expect(body.querySelector("[data-note='imported']")?.textContent).toBe(
      "Timmarna från Home Assistants statistik är ungefärliga: de räknas i månadens toppar men lyfter aldrig effektmålet.",
    );
    const first = answer.days[0]!;
    expect(dayFigures("sv", first)).toContain("från Home Assistants statistik");
  });

  it("draws the month's peak as a thin line over the days and hours it lifted the target, from the day it was set", () => {
    const answer = imported();
    const body = render(answer, UI, () => undefined, "sv");
    const lifted = answer.days.filter((day) => day.month_peak_kw !== null).map((day) => day.date);
    expect(lifted.length).toBeGreaterThan(0);
    expect(lifted.every((date) => date > "2026-09-08")).toBe(true);
    const lines = Array.from(body.querySelectorAll<HTMLElement>("button[data-key] .spotnav-power-peak"));
    expect(lines).toHaveLength(lifted.length);
    const top = scaleTop(answer.days.map((day) => day.peak_kw), 6);
    expect(lines[0]!.style.bottom).toBe(`${(6.8 / top) * 100}%`);
    expect(body.querySelector("ul")?.textContent).toContain("Månadens topp 6,8 kW");
    // The day view: only the hours a part's free level lifted.
    const day = lifted[0]!;
    const hours = render(answer, { day, pending: false }, () => undefined, "sv");
    const hourLines = hours.querySelectorAll("button[data-key] .spotnav-power-peak").length;
    const expected = answer.hours!.filter((hour) => hour.start.startsWith(day) && hour.month_peak_kw !== null).length;
    expect(hourLines).toBe(expected);
    expect(expected).toBeGreaterThan(0);
    expect(expected).toBeLessThan(24);
    // The month's peak runs just above the target here: the target's label goes under its line, never over the peak's.
    expect(hours.querySelector<HTMLElement>(".spotnav-power-target-label")?.dataset["placement"]).toBe("below");
    expect(render(month(), UI).querySelector<HTMLElement>(".spotnav-power-target-label")?.dataset["placement"]).toBeUndefined();
    // A month where it never lifted has no such line or legend row.
    expect(render(month()).querySelector(".spotnav-power-peak")).toBeNull();
    expect(render(month()).querySelector("ul")?.textContent).not.toContain("The month's peak");
  });
});

