// The departure calendar: the settings record's field, the dashboard's block, the Plan dialog's one picker and what
// it says the plan made of the chosen calendar, and the Plan cell showing the calendar's departure.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { calendarDepartureOf } from "../src/model";
import { settingsEditorBody } from "../src/settings-editor";
import { decodeSettingsRecord, formFromRecord, planSummaryParts, replacementFor } from "../src/settings";
import { departureCalendarOrNull, type DepartureCalendarBlock, type Dashboard } from "../src/validate";

const FIXTURE = join(__dirname, "..", "..", "tests", "fixtures", "settings", "v1", "success.json");

/** The fixture's record as a backend without the calendar sends it, with `changes`. */
function olderSettings(): Record<string, unknown> {
  const {
    departure_calendar: _left,
    departure_calendar_only: _alone,
    ...settings
  } = JSON.parse(readFileSync(FIXTURE, "utf8")).settings;
  return settings;
}

function storedRecord(changes: Record<string, unknown> = {}) {
  return decodeSettingsRecord({ ...olderSettings(), ...changes });
}

function block(changes: Partial<DepartureCalendarBlock> = {}): DepartureCalendarBlock {
  return {
    calendars: [
      { entity_id: "calendar.family", name: "Family" },
      { entity_id: "calendar.trips", name: "Trips" },
    ],
    entity_id: "calendar.trips",
    state: "used",
    departure: "2026-10-02T08:00:00+00:00",
    summary: "Malmö",
    all_day: false,
    target_percent: 90,
    target_kwh: null,
    target_used: "percent",
    ...changes,
  };
}

const HANDLERS = { onSave: () => undefined, onReload: () => undefined, onReapply: () => undefined };

function dialog(language: "en" | "sv", calendar: DepartureCalendarBlock | null, changes: Record<string, unknown> = {}) {
  const record = storedRecord({ departure_enabled: true, departure_calendar: "calendar.trips", ...changes });
  const form = {
    kind: "plan" as const,
    readOnly: false,
    values: formFromRecord(record),
    energyReadOnly: false,
    phases: 3,
    currentRange: { min: 6, max: 16 },
    conflict: null,
    soc: null,
    vehicles: [],
    days: { today: "2026-10-01", max: "2026-10-08", nextOccurrence: () => "2026-10-02" },
    calendar,
    calendarDeparture: { date: "2026-10-02", time: "10:00" },
  };
  return { record, built: settingsEditorBody(document, language, form as never, HANDLERS as never, "cal") };
}

describe("the settings record's departure calendar", () => {
  it("is read when present, null or a calendar, and refused when it is not one", () => {
    expect(storedRecord().departure_calendar).toBeUndefined();
    expect(storedRecord({ departure_calendar: null }).departure_calendar).toBeNull();
    expect(storedRecord({ departure_calendar: "calendar.trips" }).departure_calendar).toBe("calendar.trips");
    expect(() => storedRecord({ departure_calendar: "sensor.trips" })).toThrow();
  });

  it("is sent only to a backend that has it, and only when the reader moved it", () => {
    const older = storedRecord();
    const unsent = replacementFor("plan", older, { ...formFromRecord(older), departureCalendar: "calendar.trips" });
    expect(unsent.ok && "departure_calendar" in unsent.body).toBe(false);

    const record = storedRecord({ departure_calendar: null });
    const kept = replacementFor("plan", record, formFromRecord(record));
    expect(kept.ok && kept.body.departure_calendar).toBeNull();
    const chosen = replacementFor("plan", record, { ...formFromRecord(record), departureCalendar: "calendar.trips" });
    expect(chosen.ok && chosen.body.departure_calendar).toBe("calendar.trips");
    expect(chosen.ok && chosen.changed).toBe(true);
    const back = storedRecord({ departure_calendar: "calendar.trips" });
    const cleared = replacementFor("plan", back, { ...formFromRecord(back), departureCalendar: "" });
    expect(cleared.ok && cleared.body.departure_calendar).toBeNull();
  });
});

describe("the settings record's calendar-only switch", () => {
  it("is read when present and refused when it is not a boolean", () => {
    expect(storedRecord().departure_calendar_only).toBeUndefined();
    expect(storedRecord({ departure_calendar_only: true }).departure_calendar_only).toBe(true);
    expect(() => storedRecord({ departure_calendar_only: "yes" })).toThrow();
  });

  it("is sent only to a backend that has it, and only when the reader moved it", () => {
    const older = storedRecord();
    const unsent = replacementFor("plan", older, { ...formFromRecord(older), departureCalendarOnly: true });
    expect(unsent.ok && "departure_calendar_only" in unsent.body).toBe(false);
    const record = storedRecord({ departure_calendar: "calendar.trips", departure_calendar_only: false });
    const kept = replacementFor("plan", record, formFromRecord(record));
    expect(kept.ok && kept.body.departure_calendar_only).toBe(false);
    expect(kept.ok && kept.changed).toBe(false);
    const ticked = replacementFor("plan", record, { ...formFromRecord(record), departureCalendarOnly: true });
    expect(ticked.ok && ticked.body.departure_calendar_only).toBe(true);
    expect(ticked.ok && ticked.changed).toBe(true);
  });
});

describe("the dashboard's departure_calendar block", () => {
  it("is read whole, or not at all", () => {
    expect(departureCalendarOrNull(block())).toEqual(block());
    expect(departureCalendarOrNull(undefined)).toBeNull();
    expect(departureCalendarOrNull({ ...block(), extra: 1 })).toBeNull();
    expect(departureCalendarOrNull({ ...block(), state: "sometimes" })).toBeNull();
    expect(departureCalendarOrNull({ ...block(), target_used: "miles" })).toBeNull();
  });

  it("gives the plan's departure in the market's zone only while the calendar is used", () => {
    const dashboard = { departure_calendar: block(), settings: { departure_enabled: true } } as unknown as Dashboard;
    expect(calendarDepartureOf(dashboard, "Europe/Stockholm")).toEqual({ date: "2026-10-02", time: "10:00" });
    const daily = { ...dashboard, departure_calendar: block({ state: "daily", departure: null }) } as Dashboard;
    expect(calendarDepartureOf(daily, "Europe/Stockholm")).toBeNull();
    const off = { ...dashboard, settings: { departure_enabled: false } } as unknown as Dashboard;
    expect(calendarDepartureOf(off, "Europe/Stockholm")).toBeNull();
  });
});

describe("the Plan dialog's calendar", () => {
  it("offers no calendar, then each one by name, with the chosen one selected", () => {
    const { built } = dialog("en", block());
    const part = built.body.querySelector<HTMLElement>("[data-part='departure-calendar']");
    expect(part).not.toBeNull();
    const select = part!.querySelector("select")!;
    expect([...select.options].map((option) => option.textContent)).toEqual(["No calendar", "Family", "Trips"]);
    expect(select.value).toBe("calendar.trips");
    expect(part!.textContent).toContain("Calendar");
  });

  it("says the calendar's next departure and the target it uses", () => {
    const { built } = dialog("en", block());
    const note = built.body.querySelector("[data-note='departure-calendar']");
    expect(note?.textContent).toBe("Next departure from the calendar: tomorrow 10:00, 90 %.");
    const sv = dialog("sv", block()).built.body.querySelector("[data-note='departure-calendar']");
    expect(sv?.textContent).toBe("Nästa avresa enligt kalendern: imorgon 10:00, 90 %.");
  });

  it("says when the event's target is of the other kind", () => {
    const { built } = dialog("en", block({ target_used: null }));
    expect(built.body.querySelector("[data-note='departure-calendar']")?.textContent).toBe(
      "Next departure from the calendar: tomorrow 10:00. The event's target is of the other kind (level or energy) and is not used.",
    );
  });

  it("says a calendar that cannot be read leaves the daily departure, and a chosen date goes first", () => {
    const unread = dialog("en", block({ state: "unavailable" })).built;
    expect(unread.body.querySelector("[data-note='departure-calendar']")?.textContent).toBe(
      "The calendar cannot be read: the daily departure is used until it can.",
    );
    const dated = dialog("en", block({ state: "date" })).built;
    expect(dated.body.querySelector("[data-note='departure-calendar']")?.textContent).toBe(
      "The date you chose is used before the calendar.",
    );
  });

  it("says nothing about a calendar the reader has just picked, and writes it on Save", () => {
    const { built } = dialog("en", block());
    const select = built.body.querySelector<HTMLSelectElement>("[data-part='departure-calendar'] select")!;
    select.value = "calendar.family";
    select.dispatchEvent(new Event("change"));
    const note = built.body.querySelector<HTMLElement>("[data-note='departure-calendar']")!;
    expect(note.hidden).toBe(true);
    expect(built.values().departureCalendar).toBe("calendar.family");
    select.value = "";
    select.dispatchEvent(new Event("change"));
    expect(built.values().departureCalendar).toBe("");
    expect(built.body.querySelector<HTMLElement>("[data-note='departure-calendar-help']")!.hidden).toBe(true);
  });

  it("is not offered without the block, against a backend without the field, or for a guest", () => {
    expect(dialog("en", null).built.body.querySelector("[data-part='departure-calendar']")).toBeNull();
    const older = storedRecord({ departure_enabled: true });
    const form = {
      kind: "plan" as const,
      readOnly: false,
      values: formFromRecord(older),
      energyReadOnly: false,
      phases: 3,
      currentRange: { min: 6, max: 16 },
      conflict: null,
      soc: null,
      vehicles: [],
      calendar: block(),
    };
    const built = settingsEditorBody(document, "en", form as never, HANDLERS as never, "old");
    expect(built.body.querySelector("[data-part='departure-calendar']")).toBeNull();
    const record = storedRecord({ departure_enabled: true, departure_calendar: "calendar.trips" });
    const guest = settingsEditorBody(
      document,
      "en",
      { ...form, values: formFromRecord(record), guest: true } as never,
      HANDLERS as never,
      "guest",
    );
    expect(guest.body.querySelector("[data-part='departure-calendar']")).toBeNull();
  });

  it("offers 'only the calendar decides' under the picker once a calendar is chosen, and writes it on Save", () => {
    const { built } = dialog("sv", block(), { departure_calendar_only: false });
    const row = built.body.querySelector<HTMLElement>("[data-part='departure-calendar-only']")!;
    expect(row).not.toBeNull();
    expect(row.textContent).toBe("Styr bara med kalendern");
    expect(row.hidden).toBe(false);
    const check = row.querySelector<HTMLInputElement>("input[type='checkbox']")!;
    expect(check.checked).toBe(false);
    check.checked = true;
    expect(built.values().departureCalendarOnly).toBe(true);
    const select = built.body.querySelector<HTMLSelectElement>("[data-part='departure-calendar'] select")!;
    select.value = "";
    select.dispatchEvent(new Event("change"));
    expect(row.hidden).toBe(true);
    expect(built.body.querySelector<HTMLElement>("[data-note='departure-calendar-only-help']")!.hidden).toBe(true);
  });

  it("is not offered against a backend without the switch", () => {
    const { built } = dialog("en", block());
    expect(built.body.querySelector("[data-part='departure-calendar-only']")).toBeNull();
  });

  it("says when the calendar alone decides and has no event ahead", () => {
    const { built } = dialog("en", block({ state: "no_event", departure: null }), { departure_calendar_only: true });
    expect(built.body.querySelector("[data-note='departure-calendar']")?.textContent).toBe(
      "No event in the next 7 days: there is no departure, so the plan uses the cheapest hours known.",
    );
    expect(built.body.querySelector<HTMLInputElement>("[data-part='departure-calendar-only'] input")!.checked).toBe(true);
  });

  it("hides the weekdays and the date while only the calendar decides, and calls the time the fallback", () => {
    const { built } = dialog("sv", block(), { departure_calendar_only: false, departure_date: "2026-10-03" });
    const controls = built.body.querySelector<HTMLElement>("[data-part='departure-day-controls']")!;
    const label = built.body.querySelector("label[for$='deadline-time']")!;
    expect(controls.hidden).toBe(false);
    expect(controls.querySelector("[data-part='departure-weekdays']")).not.toBeNull();
    expect(label.textContent).toBe("Avresetid");
    const check = built.body.querySelector<HTMLInputElement>("[data-part='departure-calendar-only'] input")!;
    check.checked = true;
    check.dispatchEvent(new Event("change"));
    expect(controls.hidden).toBe(true);
    expect(label.textContent).toBe("Reservtid om kalendern inte går att läsa");
    // No date is written while it is hidden.
    expect(built.values().departureDate).toBe("");
    check.checked = false;
    check.dispatchEvent(new Event("change"));
    expect(controls.hidden).toBe(false);
    expect(label.textContent).toBe("Avresetid");
    expect(built.values().departureDate).toBe("2026-10-03");
  });

  it("shows the day controls again when the calendar is unchosen", () => {
    const { built } = dialog("en", block(), { departure_calendar_only: true });
    const controls = built.body.querySelector<HTMLElement>("[data-part='departure-day-controls']")!;
    expect(controls.hidden).toBe(true);
    const select = built.body.querySelector<HTMLSelectElement>("[data-part='departure-calendar'] select")!;
    select.value = "";
    select.dispatchEvent(new Event("change"));
    expect(controls.hidden).toBe(false);
  });

  it("says to add a calendar in Home Assistant when it has none, only against a backend with the field", () => {
    const { built } = dialog("sv", null);
    expect(built.body.querySelector("[data-note='departure-calendar-missing']")?.textContent).toBe(
      "Lägg till en kalender i Home Assistant för att styra avresan med den.",
    );
    const older = storedRecord({ departure_enabled: true });
    const form = {
      kind: "plan" as const,
      readOnly: false,
      values: formFromRecord(older),
      energyReadOnly: false,
      phases: 3,
      currentRange: { min: 6, max: 16 },
      conflict: null,
      soc: null,
      vehicles: [],
      calendar: null,
    };
    const old = settingsEditorBody(document, "en", form as never, HANDLERS as never, "older");
    expect(old.body.querySelector("[data-note='departure-calendar-missing']")).toBeNull();
    expect(dialog("en", block()).built.body.querySelector("[data-note='departure-calendar-missing']")).toBeNull();
  });

  it("keeps a chosen calendar that is gone, named by its id", () => {
    const { built } = dialog("en", block({ calendars: [], state: "unavailable" }));
    const select = built.body.querySelector<HTMLSelectElement>("[data-part='departure-calendar'] select")!;
    expect([...select.options].map((option) => option.value)).toEqual(["", "calendar.trips"]);
    expect(select.value).toBe("calendar.trips");
  });
});

describe("the Plan cell", () => {
  it("shows the calendar's departure in place of the stored time", () => {
    const record = storedRecord({ departure_enabled: true, departure_time: "08:00" });
    expect(planSummaryParts("en", record, "2026-10-01")).toContain("08:00");
    expect(planSummaryParts("en", record, "2026-10-01", { date: "2026-10-02", time: "10:00" })).toContain(
      "tomorrow 10:00",
    );
  });
});
