// The charge history: the decoder, the export range and the dialog body, on the backend's own fixtures.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  HISTORY_RANGES,
  decodeCsv,
  decodeSessions,
  exportDates,
  historyBody,
  monthLabel,
  savingsLine,
  type HistoryState,
  type HistoryUi,
  type SessionsAnswer,
} from "../src/history";
import { LANGUAGES, translate, type TranslationKey } from "../src/i18n";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, path), "utf8")) as Record<string, unknown>;
}

function answer(): SessionsAnswer {
  const decoded = decodeSessions(read("sessions/get_sessions.json"));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

const UI: HistoryUi = { list: "days", range: "thisMonth", exporting: false, notice: null };
const NOOP = { onList: () => undefined, onRange: () => undefined, onExport: () => undefined };

function body(state: HistoryState, language: (typeof LANGUAGES)[number] = "en", ui: HistoryUi = UI): HTMLElement {
  return historyBody(document, language, state, ui, NOOP);
}

describe("decoding the backend's own answer", () => {
  it("reads every part of the committed fixture", () => {
    const value = answer();
    expect(value.this_month.period).toBe("2026-09");
    expect(value.this_month.sessions).toBe(4);
    expect(value.last_month.period).toBe("2026-08");
    expect(value.months.map((month) => month.period)).toEqual(["2026-09", "2026-08"]);
    expect(value.days.length).toBe(5);
    expect(value.open?.end).toBeNull();
    expect(value.sessions[0]?.estimated).toBe(false);
    expect(value.sessions.some((session) => session.estimated && session.cost === null)).toBe(true);
  });

  it("reads the CSV answer's text and file name", () => {
    expect(decodeCsv(read("sessions/get_sessions_csv.json"))?.filename).toBe(
      "spotnav-sessions-2026-09-01-2026-09-30.csv",
    );
    expect(decodeCsv({ api_version: 1, format: "json" })).toBeNull();
    expect(decodeCsv({ api_version: 2, format: "csv", filename: "a", csv: "b" })).toBeNull();
  });

  it("accepts extra keys and refuses a missing, mistyped or out-of-range one", () => {
    const raw = read("sessions/get_sessions.json");
    expect(decodeSessions({ ...raw, added_later: 1 }).ok).toBe(true);
    const { open: _open, ...withoutOpen } = raw;
    expect(decodeSessions(withoutOpen)).toEqual({ ok: false, failure: "malformed" });
    expect(decodeSessions({ ...raw, sessions: "none" })).toEqual({ ok: false, failure: "malformed" });
    const bucket = raw.this_month as Record<string, unknown>;
    expect(decodeSessions({ ...raw, this_month: { ...bucket, solar_share: 1.5 } }).ok).toBe(false);
    expect(decodeSessions({ ...raw, this_month: { ...bucket, energy_kwh: "3" } }).ok).toBe(false);
    expect(decodeSessions({ ...raw, this_month: { ...bucket, sessions: 1.5 } }).ok).toBe(false);
    expect(decodeSessions({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeSessions(null)).toEqual({ ok: false, failure: "malformed" });
  });

  it("finds the same bucket shape in the dashboard's additive sessions_summary", () => {
    const directory = join(FIXTURES, "dashboard");
    const names = readdirSync(directory).filter((name) => name.endsWith(".json"));
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      const summary = (JSON.parse(readFileSync(join(directory, name), "utf8")) as Record<string, unknown>)
        .sessions_summary as Record<string, unknown>;
      expect(Object.keys(summary).sort(), name).toEqual(["last_month", "this_month"]);
      const composed = {
        api_version: 1,
        this_month: summary.this_month,
        last_month: summary.last_month,
        months: [],
        days: [],
        open: null,
        sessions: [],
      };
      expect(decodeSessions(composed).ok, name).toBe(true);
    }
  });
});

describe("the export range", () => {
  const value = answer();

  it("is the backend's own local months, so the card needs no clock", () => {
    expect(exportDates("thisMonth", value)).toEqual({ from: "2026-09-01", to: "2026-09-30" });
    expect(exportDates("lastMonth", value)).toEqual({ from: "2026-08-01", to: "2026-08-31" });
    expect(exportDates("last12", value)).toEqual({ from: "2025-10-01", to: null });
    expect(exportDates("all", value)).toEqual({ from: null, to: null });
  });

  it("handles a leap February and a year boundary", () => {
    const edge = {
      ...value,
      this_month: { ...value.this_month, period: "2028-03" },
      last_month: { ...value.last_month, period: "2028-02" },
    };
    expect(exportDates("lastMonth", edge)).toEqual({ from: "2028-02-01", to: "2028-02-29" });
    const january = {
      ...value,
      this_month: { ...value.this_month, period: "2027-01" },
      last_month: { ...value.last_month, period: "2026-12" },
    };
    expect(exportDates("last12", january)).toEqual({ from: "2026-02-01", to: null });
    expect(exportDates("lastMonth", january)).toEqual({ from: "2026-12-01", to: "2026-12-31" });
  });

  it("offers four ranges, each worded in every language", () => {
    expect(HISTORY_RANGES).toHaveLength(4);
    for (const language of LANGUAGES) {
      for (const range of HISTORY_RANGES) {
        expect(translate(language, `history.range.${range}` as TranslationKey), `${language} ${range}`).not.toBe("");
      }
    }
  });
});

describe("the dialog body", () => {
  it("says it is loading, and says it failed with the stable code beside the sentence", () => {
    expect(body({ kind: "loading" }).textContent).toBe("Loading the charge history…");
    const failed = body({ kind: "failed", sentenceKey: "history.failed", code: "spotnav_unknown_charger" });
    expect(failed.textContent).toBe("The charge history could not be read.");
    expect(failed.querySelector("[data-code]")?.getAttribute("data-code")).toBe("spotnav_unknown_charger");
  });

  it("shows this and last month, the days, the latest charges and the open one", () => {
    const text = body({ kind: "ready", answer: answer() });
    const tiles = text.querySelectorAll("[data-tile]");
    expect(Array.from(tiles).map((tile) => (tile as HTMLElement).dataset["tile"])).toEqual(["thisMonth", "lastMonth"]);
    expect(tiles[0]?.textContent).toContain("65.7 kWh");
    expect(tiles[0]?.textContent).toContain("24.95 kr");
    expect(tiles[0]?.textContent).toContain("4 charges");
    expect(tiles[0]?.textContent).toContain("49 % solar");
    expect(tiles[1]?.textContent).toContain("1 charge");
    expect(text.querySelectorAll("[data-list='days'] > li")).toHaveLength(5);
    expect(text.querySelectorAll("[data-list='sessions'] > li")).toHaveLength(5);
    expect(text.querySelector("[role='status']")?.textContent).toBe("Charging now since 11:30: 3.2 kWh");
  });

  it("writes savings as an estimate against the day's average price, positive or negative, or not at all", () => {
    const unit = { currency: "SEK", major_unit: "kr", minor_unit: "öre" };
    expect(savingsLine("en", { savings: 6.35, ...unit })).toBe(
      "Estimated saving: 6.35 kr against the day's average price",
    );
    expect(savingsLine("en", { savings: -1.3, ...unit })).toBe(
      "Estimated 1.3 kr more than the day's average price",
    );
    expect(savingsLine("en", { savings: 0.001, ...unit })).toBeNull();
    expect(savingsLine("en", { savings: null, ...unit })).toBeNull();
    const text = body({ kind: "ready", answer: answer() }).textContent ?? "";
    expect(text).toContain("Savings are an estimate: the same energy at each day's average price.");
  });

  it("marks estimated energy and a charge with no price, and names how each started", () => {
    const rows = body({ kind: "ready", answer: answer() }).querySelectorAll("[data-list='sessions'] > li");
    const estimated = Array.from(rows).find((row) => row.textContent?.includes("estimated energy"));
    expect(estimated?.textContent).toContain("no price");
    expect(estimated?.textContent).toContain("started by hand");
    const all = Array.from(rows).map((row) => row.textContent ?? "").join("\n");
    expect(all).toContain("solar surplus");
    expect(all).toContain("hybrid");
  });

  it("writes a charge across midnight with both days, and local times as the backend wrote them", () => {
    const rows = Array.from(body({ kind: "ready", answer: answer() }).querySelectorAll("[data-list='sessions'] > li"));
    const across = rows.find((row) => row.textContent?.includes("23:00"));
    expect(across?.textContent).toMatch(/23:00.+01:00/);
    expect(across?.querySelector("span")?.textContent).toMatch(/20.+23:00.+21.+01:00/);
  });

  it("toggles between days and months", () => {
    const months = body({ kind: "ready", answer: answer() }, "en", { ...UI, list: "months" });
    expect(months.querySelectorAll("[data-list='months'] > li")).toHaveLength(2);
    expect(months.querySelector("[data-list='months'] > li")?.textContent).toContain("September 2026");
    expect(months.querySelector("button[data-list='months']")?.getAttribute("aria-pressed")).toBe("true");
    expect(months.querySelector("button[data-list='days']")?.getAttribute("aria-pressed")).toBe("false");
  });

  it("says there is nothing yet when there is nothing, and still offers the export", () => {
    const empty: SessionsAnswer = { ...answer(), sessions: [], open: null, days: [], months: [] };
    const text = body({ kind: "ready", answer: empty });
    expect(text.textContent).toContain("No charges recorded yet.");
    expect(text.querySelector("[data-action='export']")).not.toBeNull();
  });

  it("disables the export while one runs and shows the failure sentence", () => {
    const busy = body({ kind: "ready", answer: answer() }, "en", { ...UI, exporting: true, notice: "history.exportFailed" });
    expect((busy.querySelector("[data-action='export']") as HTMLButtonElement).disabled).toBe(true);
    expect(busy.textContent).toContain("The export failed.");
  });

  it("writes a vehicle name as text, never as markup", () => {
    const hostile = answer();
    hostile.sessions[0] = { ...(hostile.sessions[0] as SessionsAnswer["sessions"][0]), vehicle: "<img src=x onerror=alert(1)>" };
    const text = body({ kind: "ready", answer: hostile });
    expect(text.querySelector("img")).toBeNull();
    expect(text.textContent).toContain("<img src=x onerror=alert(1)>");
  });

  it("is worded in every language, with no placeholder left unfilled", () => {
    for (const language of LANGUAGES) {
      const text = body({ kind: "ready", answer: answer() }, language).textContent ?? "";
      expect(text, language).not.toMatch(/\{\w+\}/);
      expect(text.length, language).toBeGreaterThan(200);
    }
    expect(translate("sv", "history.savings.saved", { amount: "6,35 kr" })).toBe(
      "Uppskattad besparing: 6,35 kr mot dagens snittpris",
    );
    expect(monthLabel("sv", "2026-09")).toBe("september 2026");
  });
});
