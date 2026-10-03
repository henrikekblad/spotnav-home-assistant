// The charge history: the decoder, the export range and the dialog body, on the backend's own fixtures.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  dayFigures,
  dayPricePositions,
  decodeCsv,
  decodeSessions,
  historyBody,
  monthLabel,
  pickerMonths,
  savingsLine,
  shiftMonth,
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

const UI: HistoryUi = { month: null, pending: false, day: null, exporting: false, notice: null };
const NOOP = { onMonth: () => undefined, onExport: () => undefined };

function august(): SessionsAnswer {
  const decoded = decodeSessions(read("sessions/get_sessions_month.json"));
  if (!decoded.ok) {
    throw new Error("the fixture must decode");
  }
  return decoded.value;
}

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
    expect(value.month).toBe("2026-09");
    expect(value.month_summary.sessions).toBe(4);
    expect(value.month_days).toHaveLength(30);
    expect(value.month_days.filter((day) => day.sessions > 0).map((day) => day.period.slice(-2))).toEqual(["02", "14", "20", "21"]);
    expect(value.month_sessions).toHaveLength(4);
    expect(value.available_months).toEqual(["2026-09", "2026-08"]);
    expect(august().month_days).toHaveLength(31);
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
    expect(decodeSessions({ ...raw, month: "september" }).ok).toBe(false);
    expect(decodeSessions({ ...raw, month_days: null }).ok).toBe(false);
    expect(decodeSessions({ ...raw, available_months: ["2026-13"] }).ok).toBe(false);
    expect(decodeSessions({ ...raw, api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    expect(decodeSessions(null)).toEqual({ ok: false, failure: "malformed" });
  });
});

describe("the months", () => {
  it("steps over year ends and offers the months with data, this month and the one shown, newest first", () => {
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
    expect(shiftMonth("2026-12", 1)).toBe("2027-01");
    expect(shiftMonth("2026-09", -24)).toBe("2024-09");
    expect(pickerMonths(answer(), "2026-09")).toEqual(["2026-09", "2026-08"]);
    expect(pickerMonths({ ...answer(), available_months: ["2026-08"] }, "2026-07")).toEqual(["2026-09", "2026-08", "2026-07"]);
    expect(pickerMonths({ ...answer(), available_months: ["2020-01"] }, "2026-09")).toEqual(["2026-09"]);
  });

  it("puts the cheapest day at 0 and the dearest at 1, a day with no price at none, and equal prices cheap", () => {
    const days = answer().month_days;
    const positions = dayPricePositions(days);
    const byDay = Object.fromEntries(days.map((day, index) => [day.period.slice(-2), positions[index]]));
    expect(byDay["02"]).toBe(0);
    expect(byDay["14"]).toBe(1);
    expect(byDay["20"]).toBeNull();
    expect(byDay["21"]).toBeCloseTo((41.364 - 19.524) / (67.355 - 19.524), 5);
    expect(byDay["03"]).toBeNull();
    const same = days.filter((day) => day.sessions > 0).slice(0, 1);
    expect(dayPricePositions(same)).toEqual([0]);
  });

  it("words a day's energy to a tenth and sets the date apart from the figures", () => {
    const day = { ...(answer().month_days[13] as SessionsAnswer["month_days"][0]), energy_kwh: 18.1234 };
    expect(dayFigures("en", day)).toMatch(/^\w{3} 14 Sep\w* · 18\.1 kWh · /);
    expect(dayFigures("sv", day)).toContain(" · 18,1 kWh · ");
  });

  it("words a day's figures, and a day without a charge", () => {
    const days = answer().month_days;
    expect(dayFigures("en", days[13] as SessionsAnswer["month_days"][0])).toContain("24.2 kWh");
    expect(dayFigures("en", days[0] as SessionsAnswer["month_days"][0])).toMatch(/^Tue 1 Sep\w*: no charge$/);
    expect(dayFigures("sv", days[0] as SessionsAnswer["month_days"][0])).toMatch(/^tis 1 sep\.?: ingen laddning$/);
  });
});

describe("the dialog body", () => {
  it("says it is loading, and says it failed with the stable code beside the sentence", () => {
    expect(body({ kind: "loading" }).textContent).toBe("Loading the charge history…");
    const failed = body({ kind: "failed", sentenceKey: "history.failed", code: "spotnav_unknown_charger" });
    expect(failed.textContent).toBe("The charge history could not be read.");
    expect(failed.querySelector("[data-code]")?.getAttribute("data-code")).toBe("spotnav_unknown_charger");
  });

  it("shows the month's summary, one bar per day, the month's charges and the open one", () => {
    const text = body({ kind: "ready", answer: answer() });
    const tile = text.querySelector("[data-tile='month']");
    expect(tile?.textContent).toContain("65.7 kWh");
    expect(tile?.textContent).toContain("24.95 kr");
    expect(tile?.textContent).toContain("4 charges");
    expect(tile?.textContent).toContain("49 % solar");
    expect(text.querySelectorAll("button[data-day]")).toHaveLength(30);
    expect(text.querySelectorAll("[data-list='sessions'] > li")).toHaveLength(4);
    expect(text.querySelector("[role='status']")?.textContent).toBe("Charging now since 11:30: 3.2 kWh");
  });

  it("makes a bar's height its energy and its colour its price against the month", () => {
    const bars = body({ kind: "ready", answer: answer() });
    const fill = (day: string) => bars.querySelector(`button[data-day='2026-09-${day}'] > span`) as HTMLElement;
    expect(fill("14").style.height).toBe("100%");
    expect(parseFloat(fill("02").style.height)).toBeCloseTo((21 / 24.2) * 100, 0);
    expect(fill("02").style.getPropertyValue("--spotnav-day-dear")).toBe("0%");
    expect(fill("14").style.getPropertyValue("--spotnav-day-dear")).toBe("100%");
    expect(fill("20").dataset["price"]).toBe("none");
    expect(fill("03").dataset["empty"]).toBe("true");
    expect(bars.querySelector("button[data-day='2026-09-14']")?.getAttribute("aria-label")).toContain("24.2 kWh");
  });

  it("shows a day's figures in the readout on hover, focus and tap, and keeps the tapped day", () => {
    const text = body({ kind: "ready", answer: answer() });
    const readout = text.querySelector("[aria-live]") as HTMLElement;
    expect(readout.textContent).toContain("Tap or hover a bar");
    const bar = text.querySelector("button[data-day='2026-09-14']") as HTMLButtonElement;
    bar.dispatchEvent(new Event("mouseenter"));
    expect(readout.textContent).toContain("24.2 kWh");
    expect(readout.textContent).toContain("kr");
    expect(readout.textContent).toContain("% solar");
    text.querySelector("[role='group']")?.dispatchEvent(new Event("mouseleave"));
    expect(readout.textContent).toContain("Tap or hover a bar");
    bar.click();
    expect(bar.getAttribute("aria-pressed")).toBe("true");
    text.querySelector("[role='group']")?.dispatchEvent(new Event("mouseleave"));
    expect(readout.textContent).toContain("24.2 kWh");
  });

  it("shows the tapped day again after a repaint", () => {
    const text = body({ kind: "ready", answer: answer() }, "en", { ...UI, day: "2026-09-02" });
    expect(text.querySelector("[aria-live]")?.textContent).toContain("21 kWh");
    expect(text.querySelector("button[data-day='2026-09-02']")?.getAttribute("aria-pressed")).toBe("true");
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

  it("says costs are calculated with the current taxes and fees, in every language, and marks an imported charge", () => {
    const plain = answer();
    expect(plain.cost_basis).toBe("current_settings");
    for (const language of LANGUAGES) {
      expect(body({ kind: "ready", answer: plain }, language).textContent, language).toContain(
        translate(language, "history.costBasis"),
      );
    }
    expect(translate("sv", "history.costBasis")).toBe("Kostnaden beräknas med nuvarande skatter och avgifter.");
    expect(translate("en", "history.costBasis")).toBe("Costs are calculated with your current taxes and fees.");

    const older = { ...plain, cost_basis: null };
    expect(body({ kind: "ready", answer: older }).textContent).not.toContain("current taxes and fees");

    const imported = {
      ...plain,
      month_sessions: plain.month_sessions.map((session, index) =>
        index === 0 ? { ...session, source: "imported_hourly", started_by: "unknown" } : session,
      ),
    };
    const rows = body({ kind: "ready", answer: imported }).querySelectorAll("[data-list='sessions'] > li");
    expect(rows[0]?.textContent).toContain("imported (hourly)");
    for (const language of LANGUAGES) {
      expect(translate(language, "history.by.imported").length, language).toBeGreaterThan(3);
    }
  });

  it("writes a charge across midnight with both days, and local times as the backend wrote them", () => {
    const rows = Array.from(body({ kind: "ready", answer: answer() }).querySelectorAll("[data-list='sessions'] > li"));
    const across = rows.find((row) => row.textContent?.includes("23:00"));
    expect(across?.textContent).toMatch(/23:00.+01:00/);
    expect(across?.querySelector("span")?.textContent).toMatch(/20.+23:00.+21.+01:00/);
  });

  it("has a month picker: arrows, and a select of the months with data, this month default", () => {
    const text = body({ kind: "ready", answer: answer() });
    const select = text.querySelector("select[data-month-select]") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual(["2026-09", "2026-08"]);
    expect(select.value).toBe("2026-09");
    expect(select.options[0]?.textContent).toBe("September 2026");
    expect((text.querySelector("button[data-month='2026-08']") as HTMLButtonElement).disabled).toBe(false);
    expect((text.querySelector("button[data-month='2026-10']") as HTMLButtonElement).disabled).toBe(true);
    expect(text.querySelector("button[data-month='2026-08']")?.getAttribute("aria-label")).toBe("Previous month");
  });

  it("asks for a month from the arrows and the select, and stops at 24 months back and at now", () => {
    const asked: string[] = [];
    const handlers = { onMonth: (month: string) => asked.push(month), onExport: () => undefined };
    const text = historyBody(document, "en", { kind: "ready", answer: answer() }, UI, handlers);
    (text.querySelector("button[data-month='2026-08']") as HTMLButtonElement).click();
    const select = text.querySelector("select") as HTMLSelectElement;
    select.value = "2026-08";
    select.dispatchEvent(new Event("change"));
    expect(asked).toEqual(["2026-08", "2026-08"]);
    const oldest = historyBody(document, "en", { kind: "ready", answer: { ...answer(), month: "2024-09" } }, { ...UI, month: "2024-09" }, handlers);
    expect((oldest.querySelector("button[data-month='2024-08']") as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows a month with no charge as one sentence, with no chart, and keeps the picker", () => {
    const empty: SessionsAnswer = {
      ...august(),
      month: "2026-07",
      month_summary: { ...august().month_summary, sessions: 0, energy_kwh: 0, cost: null },
      month_sessions: [],
    };
    const text = body({ kind: "ready", answer: empty }, "en", { ...UI, month: "2026-07" });
    expect(text.querySelector("[data-tile='month']")?.textContent).toBe("No charges in this month.");
    expect(text.querySelector("button[data-day]")).toBeNull();
    expect((text.querySelector("select") as HTMLSelectElement).value).toBe("2026-07");
    expect(text.querySelector("[data-action='export']")).not.toBeNull();
  });

  it("dims the old month while a new one is read, and keeps the picker through a failure", () => {
    const pending = body({ kind: "ready", answer: answer() }, "en", { ...UI, month: "2026-08", pending: true });
    expect(pending.querySelector("[data-pending]")?.getAttribute("aria-busy")).toBe("true");
    expect((pending.querySelector("[data-action='export']") as HTMLButtonElement).disabled).toBe(true);
    const failed = body({ kind: "failed", sentenceKey: "history.failed", code: null, answer: answer() }, "en", { ...UI, month: "2026-08" });
    expect(failed.querySelector("select")).not.toBeNull();
    expect(failed.textContent).toContain("The charge history could not be read.");
  });

  it("disables the export while one runs and shows the failure sentence", () => {
    const busy = body({ kind: "ready", answer: answer() }, "en", { ...UI, exporting: true, notice: "history.exportFailed" });
    expect((busy.querySelector("[data-action='export']") as HTMLButtonElement).disabled).toBe(true);
    expect(busy.textContent).toContain("The export failed.");
  });

  it("writes a vehicle name as text, never as markup", () => {
    const hostile = answer();
    hostile.month_sessions[0] = { ...(hostile.month_sessions[0] as SessionsAnswer["sessions"][0]), vehicle: "<img src=x onerror=alert(1)>" };
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
