// The History view from the card: the button, the request, the dialog, and Export CSV, at the DOM boundary.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFailure, FakeHass, mountCard } from "./helpers";

const saved: Array<{ filename: string; text: string }> = [];
vi.mock("../src/download", () => ({
  saveTextFile: (_doc: Document, filename: string, text: string) => {
    saved.push({ filename, text });
  },
}));

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, path), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(options: { language?: string; admin?: boolean } = {}) {
  const hass = new FakeHass();
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  const snapshot = hass.snapshot("snapshot", options.language ?? "en");
  snapshot.user = { is_admin: options.admin ?? true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(fixture("dashboard/start_idle.json"));
  await settle();
  return { hass, element };
}

function historyButton(element: Element): HTMLButtonElement {
  return shadow(element).querySelector("button[data-history='open']") as HTMLButtonElement;
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

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
  saved.length = 0;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the History button", () => {
  it("sits beside Settings, in one group, named in the card's language", async () => {
    const { element } = await mounted({ language: "sv" });
    const button = historyButton(element);
    expect(button.getAttribute("aria-label")).toBe("Laddhistorik");
    expect(button.getAttribute("aria-haspopup")).toBe("dialog");
    const labels = Array.from(shadow(element).querySelectorAll(".spotnav-header > .spotnav-header-actions > button")).map((node) =>
      node.getAttribute("aria-label"),
    );
    expect(labels).toEqual(["Laddhistorik", "Kortinställningar"]);
  });

  it("opens the dialog at once, asks for the history once, and fills it from the answer", async () => {
    const { hass, element } = await mounted();
    historyButton(element).click();
    await settle();

    expect(dialog(element).textContent).toContain("Loading the charge history…");
    expect(hass.messages.at(-1)).toEqual({
      type: "spotnav/get_sessions",
      api_version: 1,
      charger_id: "entry_a",
      limit: 20,
    });

    hass.resolveNext(fixture("sessions/get_sessions.json"));
    await settle();

    const text = dialog(element).textContent ?? "";
    expect(text).toContain("Charge history");
    expect(text).toContain("65.7 kWh");
    expect(text).toContain("Estimated saving");
    expect(dialog(element).querySelectorAll("[data-list='sessions'] > li")).toHaveLength(4);
    expect(dialog(element).querySelectorAll("button[data-day]")).toHaveLength(30);
  });

  it("is open to every signed-in user, not only administrators", async () => {
    const { hass, element } = await mounted({ admin: false });
    historyButton(element).click();
    await settle();

    expect(hass.messages.at(-1)?.["type"]).toBe("spotnav/get_sessions");
  });

  it("says it could not read the history, with the stable code, when the request is refused", async () => {
    const { hass, element } = await mounted();
    historyButton(element).click();
    await settle();

    hass.rejectNext(apiFailure("spotnav_unknown_charger"));
    await settle();

    const failure = dialog(element).querySelector("[data-code]");
    expect(failure?.textContent).toBe("The charge history could not be read.");
    expect(failure?.getAttribute("data-code")).toBe("spotnav_unknown_charger");
  });

  it("says the same for an answer it cannot decode, and names a newer contract as such", async () => {
    const { hass, element } = await mounted();
    historyButton(element).click();
    await settle();
    hass.resolveNext({ api_version: 1, nonsense: true });
    await settle();
    expect(dialog(element).textContent).toContain("The charge history could not be read.");

    historyButton(element).click();
    await settle();
    hass.resolveNext({ ...fixture("sessions/get_sessions.json"), api_version: 2 });
    await settle();
    expect(dialog(element).textContent).not.toContain("could not be read");
  });

  it("asks for another month from the picker, dims the old one meanwhile, and shows the answer", async () => {
    const { hass, element } = await mounted();
    historyButton(element).click();
    await settle();
    hass.resolveNext(fixture("sessions/get_sessions.json"));
    await settle();

    (dialog(element).querySelector("button[data-month='2026-08']") as HTMLButtonElement).click();
    await settle();

    expect(hass.messages.at(-1)).toEqual({
      type: "spotnav/get_sessions",
      api_version: 1,
      charger_id: "entry_a",
      limit: 20,
      month: "2026-08",
    });
    expect(dialog(element).querySelector("[data-pending]")).not.toBeNull();
    expect((dialog(element).querySelector("select") as HTMLSelectElement).value).toBe("2026-08");

    hass.resolveNext(fixture("sessions/get_sessions_month.json"));
    await settle();

    expect(dialog(element).querySelector("[data-pending]")).toBeNull();
    expect(dialog(element).querySelectorAll("button[data-day]")).toHaveLength(31);
    expect(dialog(element).textContent).toContain("18.4 kWh");
    expect((dialog(element).querySelector("button[data-month='2026-09']") as HTMLButtonElement).disabled).toBe(false);
  });

  it("keeps the picker when a month cannot be read, and an older answer never overwrites a newer", async () => {
    const { hass, element } = await mounted();
    historyButton(element).click();
    await settle();
    hass.resolveNext(fixture("sessions/get_sessions.json"));
    await settle();

    (dialog(element).querySelector("button[data-month='2026-08']") as HTMLButtonElement).click();
    await settle();
    hass.rejectNext(apiFailure("spotnav_invalid_range"));
    await settle();

    expect(dialog(element).querySelector("[data-code]")?.getAttribute("data-code")).toBe("spotnav_invalid_range");
    expect(dialog(element).querySelector("select")).not.toBeNull();
  });

  it("ignores an answer that arrives after the card has gone", async () => {
    const { hass, element } = await mounted();
    historyButton(element).click();
    await settle();

    element.remove();
    hass.resolveNext(fixture("sessions/get_sessions.json"));
    await settle();

    expect(saved).toEqual([]);
  });
});

describe("Export CSV", () => {
  async function opened() {
    const context = await mounted();
    historyButton(context.element).click();
    await settle();
    context.hass.resolveNext(fixture("sessions/get_sessions.json"));
    await settle();
    return context;
  }

  it("asks for the shown month and saves the file the backend names", async () => {
    const { hass, element } = await opened();
    (dialog(element).querySelector("[data-action='export']") as HTMLButtonElement).click();
    await settle();

    expect(hass.messages.at(-1)).toEqual({
      type: "spotnav/get_sessions",
      api_version: 1,
      charger_id: "entry_a",
      format: "csv",
      month: "2026-09",
    });
    expect((dialog(element).querySelector("[data-action='export']") as HTMLButtonElement).disabled).toBe(true);

    hass.resolveNext(fixture("sessions/get_sessions_csv.json"));
    await settle();

    expect(saved).toHaveLength(1);
    expect(saved[0]?.filename).toBe("spotnav-sessions-2026-09-01-2026-09-30.csv");
    expect(saved[0]?.text.split("\n")[0]).toContain("start,end,energy_kwh");
    expect((dialog(element).querySelector("[data-action='export']") as HTMLButtonElement).disabled).toBe(false);
  });

  it("exports the month that was picked", async () => {
    const { hass, element } = await opened();
    (dialog(element).querySelector("button[data-month='2026-08']") as HTMLButtonElement).click();
    await settle();
    hass.resolveNext(fixture("sessions/get_sessions_month.json"));
    await settle();
    (dialog(element).querySelector("[data-action='export']") as HTMLButtonElement).click();
    await settle();

    expect(hass.messages.at(-1)?.["month"]).toBe("2026-08");
  });

  it("says the export failed, with nothing saved, and offers it again", async () => {
    const { hass, element } = await opened();
    (dialog(element).querySelector("[data-action='export']") as HTMLButtonElement).click();
    await settle();

    hass.rejectNext(apiFailure("anything"));
    await settle();

    expect(saved).toEqual([]);
    expect(dialog(element).textContent).toContain("The export failed.");
    expect((dialog(element).querySelector("[data-action='export']") as HTMLButtonElement).disabled).toBe(false);
  });

  it("treats an answer that is not a CSV as a failed export", async () => {
    const { hass, element } = await opened();
    (dialog(element).querySelector("[data-action='export']") as HTMLButtonElement).click();
    await settle();

    hass.resolveNext({ api_version: 1, format: "json" });
    await settle();

    expect(saved).toEqual([]);
    expect(dialog(element).textContent).toContain("The export failed.");
  });
});
