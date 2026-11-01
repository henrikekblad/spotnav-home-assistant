// The Plan dialog's live preview: the debounce and the sequence that drops a stale answer, the line in the card's
// own words, the strip of the hours to the departure, its phone-width geometry, the strict decoder, and the dialog
// asking (an administrator's) or not (a reader's).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FormatContext } from "../src/format";
import {
  PREVIEW_DEBOUNCE_MS,
  PreviewScheduler,
  previewLines,
  previewStripModel,
  renderPlanPreview,
  type PreviewState,
} from "../src/plan-preview";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { decodePlanPreview, type PlanPreview } from "../src/validate";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const answer = (name: string): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, "plan_preview", `${name}.json`), "utf8")) as Record<string, any>;

function preview(name: string): PlanPreview {
  const decoded = decodePlanPreview(answer(name));
  if (!decoded.ok || decoded.value.preview === null) {
    throw new Error(`fixture ${name} does not decode`);
  }
  return decoded.value.preview;
}

const SV: FormatContext = {
  language: "sv",
  timeZone: "Europe/Stockholm",
  unit: "öre/kWh",
  currency: "SEK",
  majorUnit: "kr",
};
/** 08:00 in Stockholm on the fixtures' day, when the backend wrote them. */
const NOW = Date.parse("2026-09-22T06:00:00Z");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the answer's decoder", () => {
  it("reads the backend's own answers", () => {
    for (const name of ["ready", "shortfall", "no_area"]) {
      expect(decodePlanPreview(answer(name)).ok, name).toBe(true);
    }
    expect(preview("no_area").plan).toBeNull();
    expect(preview("shortfall").plan?.short_of_deadline).toBe(true);
  });

  it("refuses a key it does not know, anywhere", () => {
    const top = { ...answer("ready"), extra: 1 };
    expect(decodePlanPreview(top)).toEqual({ ok: false, failure: "malformed" });
    const inner = answer("ready");
    inner["preview"]["plan"]["extra"] = 1;
    expect(decodePlanPreview(inner).ok).toBe(false);
    const quarter = answer("ready");
    quarter["preview"]["strip"]["quarters"][0]["extra"] = 1;
    expect(decodePlanPreview(quarter).ok).toBe(false);
  });

  it("tells another version from a malformed answer, and a refusal from a preview", () => {
    expect(decodePlanPreview({ ...answer("ready"), api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    const refused = decodePlanPreview({ api_version: 1, ok: false, error: "invalid_amps", preview: null });
    expect(refused).toEqual({ ok: true, value: { ok: false, error: "invalid_amps", preview: null } });
    expect(decodePlanPreview({ api_version: 1, ok: true, error: null, preview: null }).ok).toBe(false);
  });
});

describe("the scheduler", () => {
  function scheduler() {
    const asked: string[] = [];
    const answers: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> = [];
    const painted: PreviewState[] = [];
    const logged: string[] = [];
    const subject = new PreviewScheduler<string>({
      ask: (body) => {
        asked.push(body);
        return new Promise((resolve, reject) => answers.push({ resolve, reject }));
      },
      decode: decodePlanPreview,
      paint: (state) => painted.push(state),
      log: (message) => logged.push(message),
    });
    return { subject, asked, answers, painted, logged };
  }

  it("asks once, the latest draft, a moment after the last change", async () => {
    const { subject, asked } = scheduler();
    subject.request("a");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS - 50);
    subject.request("b");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS - 50);
    subject.request("c");
    expect(asked).toEqual([]);
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    expect(asked).toEqual(["c"]);
  });

  it("drops an answer to an older draft, whenever it comes", async () => {
    const { subject, asked, answers, painted } = scheduler();
    subject.request("a");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    subject.request("b");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    expect(asked).toEqual(["a", "b"]);
    answers[1]!.resolve(answer("shortfall"));
    await vi.advanceTimersByTimeAsync(0);
    answers[0]!.resolve(answer("ready"));
    await vi.advanceTimersByTimeAsync(0);
    expect(painted).toHaveLength(1);
    expect(painted[0]).toMatchObject({ kind: "shown", pending: false });
    expect((painted[0] as { preview: PlanPreview }).preview.plan?.short_of_deadline).toBe(true);

    // And one that arrives while a newer draft only waits for its moment is dropped too.
    subject.request("c");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    subject.request("d");
    answers[2]!.resolve(answer("ready"));
    await vi.advanceTimersByTimeAsync(0);
    expect(painted.filter((state) => state.kind === "shown" && !state.pending)).toHaveLength(1);
  });

  it("dims the old result while it waits, and keeps it on an error", async () => {
    const { subject, answers, painted, logged } = scheduler();
    subject.request("a");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    answers[0]!.resolve(answer("ready"));
    await vi.advanceTimersByTimeAsync(0);
    subject.request("b");
    expect(painted.at(-1)).toMatchObject({ kind: "shown", pending: true });
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    answers[1]!.reject(new Error("socket closed"));
    await vi.advanceTimersByTimeAsync(0);
    // Nothing new: the old preview, still dimmed, and the failure in the log.
    expect(painted).toHaveLength(2);
    expect((painted[1] as { preview: PlanPreview }).preview).toBe((painted[0] as { preview: PlanPreview }).preview);
    expect(logged).toEqual(["plan preview failed: socket closed"]);
    // A refusal (the Save's own code) and a malformed answer are not shown either.
    subject.request("c");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    answers[2]!.resolve({ api_version: 1, ok: false, error: "invalid_amps", preview: null });
    await vi.advanceTimersByTimeAsync(0);
    subject.request("d");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    answers[3]!.resolve({ ...answer("ready"), surprise: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(painted.every((state) => state.kind === "shown" && (state === painted[0] || state.pending))).toBe(true);
    expect(logged).toEqual([
      "plan preview failed: socket closed",
      "plan preview refused: invalid_amps",
      "plan preview answer refused: malformed",
    ]);
  });

  it("asks nothing for a draft the card refuses, and nothing once disposed", async () => {
    const { subject, asked } = scheduler();
    subject.request(null);
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    subject.request("a");
    subject.dispose();
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    expect(asked).toEqual([]);
  });
});

describe("the line", () => {
  it("says the periods, the energy, the cost and when it is ready, in the card's formats", () => {
    expect(previewLines(preview("ready"), SV, NOW)).toEqual({
      plan: "08:00–13:00 · 11,5 kWh · 12,97 kr · klar 13:00",
      notes: [],
    });
  });

  it("adds the plan's own status lines: a departure it does not make", () => {
    const lines = previewLines(preview("shortfall"), SV, NOW);
    // The plan alone; "ready 09:00" is the shortfall's own time, so it is said there only, on a line of its own.
    expect(lines.plan).toBe("08:00–09:00 · 2,3 kWh · 2,59 kr");
    expect(lines.notes).toEqual([
      { text: "Hinner inte bli klar till avresan: cirka 2,3 av 60 kWh till 09:00", warning: true },
    ]);
  });

  it("says why there is no plan, as the status words it", () => {
    const lines = previewLines(preview("no_area"), SV, NOW);
    expect(lines.plan).toBeNull();
    expect(lines.notes).toHaveLength(1);
    expect(lines.notes[0]!.text).toMatch(/elområde|prisområde/i);
  });
});

describe("the strip", () => {
  it("marks the planned quarters and colours each by price against its day's mean", () => {
    const shortfall = preview("shortfall");
    const model = previewStripModel(shortfall.strip!, SV);
    expect(model.cells).toHaveLength(4);
    expect(model.cells.every((cell) => cell.planned)).toBe(true);
    const ready = preview("ready");
    const cells = previewStripModel(ready.strip!, SV).cells;
    expect(cells.filter((cell) => cell.planned)).toHaveLength(ready.strip!.quarters.filter((q) => q.planned).length);
    // A rising day: the first quarters are below the mean, the last above it.
    expect(cells[0]!.cheap).toBe(true);
    expect(cells.at(-1)!.cheap).toBe(false);
  });

  it("renders the cells and a few hour labels, all in shares of the width", () => {
    const slot = document.createElement("div");
    renderPlanPreview(document, slot, { kind: "shown", preview: preview("ready"), pending: false }, SV, NOW);
    const cells = Array.from(slot.querySelectorAll<HTMLElement>(`.${C.planPreviewCell}`));
    expect(cells).toHaveLength(preview("ready").strip!.quarters.length);
    expect(cells.filter((cell) => cell.dataset["planned"] === "true")).toHaveLength(20);
    for (const cell of cells) {
      expect(cell.style.left).toMatch(/%$/);
      expect(cell.style.width).toMatch(/%$/);
    }
    expect(slot.querySelector(`.${C.planPreviewLine}`)?.textContent).toContain("klar 13:00");
    expect(slot.dataset["pending"]).toBe("false");
    renderPlanPreview(document, slot, { kind: "shown", preview: preview("shortfall"), pending: false }, SV, NOW);
    const note = slot.querySelector<HTMLElement>(`.${C.planPreviewNote}`)!;
    expect(note.dataset["tone"]).toBe("warning");
    expect(note.textContent).toContain("Hinner inte bli klar");
    expect(slot.querySelector(`.${C.planPreviewLine}`)?.textContent).not.toContain("klar");
    renderPlanPreview(document, slot, { kind: "shown", preview: preview("ready"), pending: true }, SV, NOW);
    expect(slot.dataset["pending"]).toBe("true");
  });

  it("fits a phone: its start and end, and never more than seven labels, whatever the span", () => {
    const strip = preview("ready").strip!;
    for (const minutes of [45, 75, 120, 300, 480, 780, 1200, 1440, 2160, 2880, 3600]) {
      const longer = { ...strip, endMs: strip.startMs + minutes * 60_000 };
      const ticks = previewStripModel(longer, SV).ticks;
      expect(ticks.length, `${minutes} min`).toBeLessThanOrEqual(7);
      expect(ticks[0]).toMatchObject({ position: 0, edge: "start", label: "08:00" });
      expect(ticks.at(-1)).toMatchObject({ position: 1, edge: "end" });
      const positions = ticks.map((tick) => tick.position);
      expect(positions).toEqual([...positions].sort((a, b) => a - b));
      for (let index = 1; index < positions.length; index += 1) {
        expect(positions[index]! - positions[index - 1]!, `${minutes} min`).toBeGreaterThanOrEqual(0.12);
      }
    }
    // A short horizon (23:30 to 00:45) still names its ends and the midnight between them.
    const short = { ...strip, startMs: Date.parse("2026-09-22T21:30:00Z"), endMs: Date.parse("2026-09-22T22:45:00Z") };
    expect(previewStripModel(short, SV).ticks.map((tick) => tick.label)).toEqual(["23:30", "00", "00:45"]);
    // A whole day: evenly spaced hours between the ends.
    const day = { ...strip, endMs: strip.startMs + 24 * 3_600_000 };
    const labels = previewStripModel(day, SV).ticks.map((tick) => tick.label);
    expect(labels).toEqual(["08:00", "12", "16", "20", "00", "04", "08:00"]);
  });

  it("draws the planned runs as one band each, apart from the price colours", () => {
    const ready = preview("ready");
    const model = previewStripModel(ready.strip!, SV);
    expect(model.runs).toHaveLength(1);
    const planned = model.cells.filter((cell) => cell.planned);
    expect(model.runs[0]!.left).toBeCloseTo(planned[0]!.left);
    expect(model.runs[0]!.width).toBeCloseTo(planned.reduce((sum, cell) => sum + cell.width, 0));
    const slot = document.createElement("div");
    renderPlanPreview(document, slot, { kind: "shown", preview: ready, pending: false }, SV, NOW);
    expect(slot.querySelectorAll(`.${C.planPreviewBand} .${C.planPreviewRun}`)).toHaveLength(1);
    expect(slot.querySelector(`.${C.planPreviewTick}[data-edge='start']`)?.textContent).toBe("08:00");
  });
});

describe("in the Plan dialog", () => {
  const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
  const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
  const RECORD: SettingsRecord = {
    revision: 7,
    area_id: "SE4",
    overrides: [],
    phases: 1,
    amps: 10,
    requested_kwh: 6,
    fill_to_limit: false,
    max_periods: 1,
    departure_enabled: false,
    departure_time: "08:00",
    departure_date: null,
    departure_weekdays: [1, 2, 3, 4, 5, 6, 7],
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: null },
  };
  const dashboard = (): Record<string, unknown> =>
    JSON.parse(readFileSync(join(FIXTURES, "dashboard", "proposal_ready.json"), "utf8")) as Record<string, unknown>;

  async function openPlan(admin: boolean) {
    vi.setSystemTime(NOW);
    const hass = new FakeHass();
    const element = mountCard(CONFIG, hass);
    const snapshot = hass.snapshot("snapshot", "sv");
    snapshot.user = { is_admin: admin };
    element.hass = snapshot;
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(dashboard());
    await vi.advanceTimersByTimeAsync(0);
    shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION,
      ok: true,
      error: null,
      settings: { ...RECORD },
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await vi.advanceTimersByTimeAsync(0);
    const dialog = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find(
      (node) => node.closest("[hidden]") === null,
    )!;
    return { hass, element, dialog };
  }

  it("asks what a Save of the draft would plan, and shows it under the fields", async () => {
    const { hass, dialog } = await openPlan(true);
    hass.previewHandler = async () => answer("ready");
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    expect(hass.previewMessages).toHaveLength(1);
    const slider = dialog.querySelector<HTMLInputElement>("[data-part='energy'] input[type='range']")!;
    slider.value = "11.5";
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS);
    expect(hass.previewMessages).toHaveLength(2);
    const sent = hass.previewMessages[1]!;
    expect(sent["type"]).toBe("spotnav/plan_preview");
    expect(sent["charger_id"]).toBe("entry_a");
    expect((sent["settings"] as Record<string, unknown>)["requested_kwh"]).toBe(11.5);
    expect(sent).not.toHaveProperty("expected_revision");
    const slot = dialog.querySelector<HTMLElement>(`.${C.planPreview}`)!;
    expect(slot.hidden).toBe(false);
    expect(slot.querySelector(`.${C.planPreviewLine}`)?.textContent).toContain("klar 13:00");
    // Nothing was saved.
    expect(hass.messages.filter((message) => message["type"] === "spotnav/update_settings")).toEqual([]);
  });

  it("is not there for a reader who has no Save", async () => {
    const { hass, dialog } = await openPlan(false);
    await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS * 2);
    expect(hass.previewMessages).toEqual([]);
    expect(dialog.querySelector(`.${C.planPreview}`)).toBeNull();
  });
});

describe("the line's days", () => {
  it("names a day once, and never today", () => {
    const ready = preview("ready");
    const tomorrow = NOW - 86_400_000;
    expect(previewLines(ready, SV, tomorrow).plan).toBe("tis 22 08:00–13:00 · 11,5 kWh · 12,97 kr · klar 13:00");
    const late = {
      ...ready,
      plan: {
        ...ready.plan!,
        periods: [{ start: "", startMs: Date.parse("2026-09-22T21:15:00Z"), end: "", endMs: Date.parse("2026-09-22T22:30:00Z") }],
        finishMs: Date.parse("2026-09-22T22:30:00Z"),
      },
    };
    expect(previewLines(late, SV, NOW).plan).toBe("23:15–ons 23 00:30 · 11,5 kWh · 12,97 kr · klar 00:30");
  });
});
