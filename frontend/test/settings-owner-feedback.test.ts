// The owner's feedback on the Settings page: a value's editor returns to where the page was, the AI Task entities
// that share a name are told apart, the frame is "Beskär bild laddplats", a stale reference picture says what to
// do, the texts the owner found redundant are gone, and two or more cars share one "Bil" section with tabs.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { entityLabels, staleText } from "../src/camera-editor";
import { LANGUAGES, translate } from "../src/i18n";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, any>;

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function twoCars(): Record<string, any> {
  const payload = read("dashboard", "cheapest_direct_site_admin.json");
  const two = read("dashboard", "target_soc_two_vehicles.json");
  payload["vehicles"] = two["vehicles"];
  payload["vehicles"][0]["name"] = "EV6";
  payload["vehicles"][1]["name"] = "Testbil";
  payload["vehicles"][0]["target_percent"] = 80;
  payload["vehicle_choices"] = [{ id: "vehicle_ev6", name: "EV6" }, { id: "vehicle_niro", name: "Testbil" }];
  payload["target_vehicle_id"] = "vehicle_ev6";
  payload["soc"] = two["soc"];
  payload["settings"]["identify_mode"] = "automatic";
  payload["settings"]["vehicle_ids"] = null;
  payload["settings"]["identify_camera"] = { camera_entity_id: "camera.norr", ai_task_entity_id: "ai_task.ollama_ai_task_2", frame: null };
  payload["camera_identification"] = {
    cameras: [{ entity_id: "camera.norr", name: "Norr" }],
    ai_tasks: [
      { entity_id: "ai_task.ollama_ai_task", name: "Ollama AI Task", model: "qwen3-vl:4b-instruct" },
      { entity_id: "ai_task.ollama_ai_task_2", name: "Ollama AI Task", model: "qwen2.5vl:7b" },
      { entity_id: "ai_task.ollama_ai_task_3", name: "Ollama AI Task", model: null },
      { entity_id: "ai_task.openai", name: "OpenAI", model: "gpt-5-mini" },
    ],
    references: { vehicle_ev6: [], vehicle_niro: [] },
  };
  return payload;
}

async function openSettings(payload: Record<string, unknown>, language = "sv") {
  const hass = new FakeHass();
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config" ? read("entity_config", "v1", "get_direct.json") : new Promise(() => undefined);
  shadow(element).querySelector<HTMLButtonElement>(`[aria-label='${translate(language as "sv", "header.settings")}']`)!.click();
  await settle();
  return { hass, element };
}

function page(element: Element): HTMLElement {
  return Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find((dialog) => dialog.closest("[hidden]") === null)!;
}

/** The Settings page's scrolling panel, with a scroll position jsdom keeps. */
function scrollable(element: Element): HTMLElement {
  const panel = shadow(element).querySelector<HTMLElement>(".spotnav-dialog-overlay:not([hidden]) .spotnav-dialog")!;
  return panel;
}

let scrolls: WeakMap<Element, number>;
const original = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
  scrolls = new WeakMap();
  Object.defineProperty(Element.prototype, "scrollTop", {
    configurable: true,
    get(this: Element) {
      return scrolls.get(this) ?? 0;
    },
    set(this: Element, value: number) {
      scrolls.set(this, value);
    },
  });
});

afterEach(() => {
  vi.useRealTimers();
  if (original !== undefined) {
    Object.defineProperty(Element.prototype, "scrollTop", original);
  }
});

describe("a value's editor returns to where the page was", () => {
  it("after Cancel: scrolled as it was, the row that opened it focused", async () => {
    const { element } = await openSettings(twoCars());
    scrollable(element).scrollTop = 640;
    const opener = page(element).querySelector<HTMLButtonElement>("[data-edit='identify_mode']")!;
    opener.focus();
    opener.click();
    await settle();
    page(element).querySelector<HTMLButtonElement>("form[data-value-editor] button[type='button']")!.click();
    await settle();
    expect(scrollable(element).scrollTop).toBe(640);
    expect(shadow(element).activeElement?.getAttribute("data-edit")).toBe("identify_mode");
  });

  it("after a save, across the card's re-render", async () => {
    const payload = twoCars();
    const { hass, element } = await openSettings(payload);
    scrollable(element).scrollTop = 420;
    page(element).querySelector<HTMLButtonElement>("[data-edit='identify_ai_task']")!.click();
    await settle();
    const form = page(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    form.querySelector<HTMLInputElement>("[data-value-option='ai_task.openai']")!.click();
    form.requestSubmit();
    await settle();
    hass.resolveNext({ api_version: 1, ok: true, error: null, settings: payload["settings"], pause: { choice: null, admitted_at: null, expires_at: null } });
    await settle();
    hass.resolveNext({ api_version: 1, ok: true, error: null, settings: payload["settings"], pause: { choice: null, admitted_at: null, expires_at: null } });
    await settle();
    hass.resolveNext(payload);
    await settle();
    expect(page(element).querySelector("[data-section='entities']")).not.toBeNull();
    expect(scrollable(element).scrollTop).toBe(420);
  });

  it("from the top when Settings is opened afresh", async () => {
    const { element } = await openSettings(twoCars());
    page(element).querySelector<HTMLButtonElement>("[data-edit='identify_mode']")!.click();
    await settle();
    page(element).querySelector<HTMLButtonElement>("button[aria-label]")?.click();
    await settle();
    shadow(element).querySelector<HTMLButtonElement>(`[aria-label='${translate("sv", "header.settings")}']`)!.click();
    await settle();
    expect(scrollable(element).scrollTop).toBe(0);
  });
});

describe("AI Task entities that share a name", () => {
  it("are told apart by their model, else their entity id; a unique name stands alone", () => {
    const labels = entityLabels(twoCars()["camera_identification"]["ai_tasks"]);
    expect(labels.get("ai_task.ollama_ai_task")).toEqual({ name: "Ollama AI Task", detail: "qwen3-vl:4b-instruct" });
    expect(labels.get("ai_task.ollama_ai_task_3")).toEqual({ name: "Ollama AI Task", detail: "ai_task.ollama_ai_task_3" });
    expect(labels.get("ai_task.openai")).toEqual({ name: "OpenAI", detail: null });
    const shared = entityLabels([
      { entity_id: "ai_task.a", name: "X", model: "m" },
      { entity_id: "ai_task.b", name: "X", model: "m" },
    ]);
    expect(shared.get("ai_task.a")?.detail).toBe("ai_task.a");
  });

  it("show it in the row and under each choice", async () => {
    const { element } = await openSettings(twoCars());
    expect(page(element).querySelector("[data-row='identify_ai_task']")?.textContent).toContain("Ollama AI Task · qwen2.5vl:7b");
    page(element).querySelector<HTMLButtonElement>("[data-edit='identify_ai_task']")!.click();
    await settle();
    const text = page(element).querySelector("form")!.textContent ?? "";
    expect(text).toContain("qwen3-vl:4b-instruct");
    expect(text).toContain("ai_task.ollama_ai_task_3");
    expect(text).not.toContain("ai_task.openai");
  });
});

describe("the owner's wording", () => {
  it("crops the parking spot, and says what a stale picture means and what to do", async () => {
    const { element } = await openSettings(twoCars());
    const row = page(element).querySelector("[data-row='identify_frame']")!;
    expect(row.textContent).toBe("Beskär bild laddplatsHela bilden");
    expect(translate("sv", "camera.frame.drawn")).toBe("Beskuren");
    expect(staleText("sv", [{ kind: "day", taken_at: "x", colour: true, stale: true }])).toBe(
      "Dagbild: tagen innan beskärningen ändrades – ta en ny.",
    );
    for (const language of LANGUAGES) {
      expect(translate(language, "reference.stale")).not.toBe("");
      expect(translate(language, "camera.frame.label")).not.toBe("");
    }
  });

  it("leaves out the texts the owner found redundant, and keeps the sources' note", async () => {
    const { element } = await openSettings(twoCars());
    const text = page(element).textContent ?? "";
    expect(text).not.toContain(translate("sv", "settings.vehicle.plannedHere"));
    expect(text).not.toContain(translate("sv", "site.applies.one"));
    expect(text).not.toContain(translate("sv", "settings.vehicle.targetHelp"));
    expect(text).not.toContain(translate("sv", "entity.help.chargerPriority"));
    expect(text).not.toContain(translate("sv", "camera.help"));
    expect(text).toContain(translate("sv", "settings.vehicle.sourcesHelp"));
    page(element).querySelector<HTMLButtonElement>("[data-vehicle='vehicle_ev6'] [data-edit='target']")!.click();
    await settle();
    expect(page(element).textContent).toContain(translate("sv", "settings.vehicle.targetHelp"));
  });

  it("still says which chargers a site applies to when it has several", async () => {
    const payload = twoCars();
    payload["site"]["charger_count"] = 2;
    const { element } = await openSettings(payload);
    expect(page(element).querySelector("[data-section='site']")?.textContent).toContain("Gäller alla 2 laddare");
  });
});

describe("two or more cars", () => {
  it("share one Bil section with a tab per car, the planned car's open", async () => {
    const { element } = await openSettings(twoCars());
    const sections = page(element).querySelectorAll<HTMLElement>("[data-section='vehicle']");
    expect(sections).toHaveLength(1);
    expect(sections[0]!.querySelector("h4")?.textContent).toBe("Bil");
    const tabs = Array.from(page(element).querySelectorAll<HTMLButtonElement>("[role='tab']"));
    expect(tabs.map((tab) => tab.textContent)).toEqual(["EV6", "Testbil"]);
    expect(tabs[0]!.getAttribute("aria-selected")).toBe("true");
    tabs[1]!.click();
    expect(page(element).querySelector("[data-section='vehicle']")?.getAttribute("data-vehicle")).toBe("vehicle_niro");
    page(element).querySelector<HTMLButtonElement>("[data-vehicle-tab='vehicle_niro']")!.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
    );
    expect(page(element).querySelector("[data-section='vehicle']")?.getAttribute("data-vehicle")).toBe("vehicle_ev6");
  });

  it("keep the open car's tab after a value's editor", async () => {
    const { element } = await openSettings(twoCars());
    page(element).querySelector<HTMLButtonElement>("[data-vehicle-tab='vehicle_niro']")!.click();
    page(element).querySelector<HTMLButtonElement>("[data-vehicle='vehicle_niro'] [data-edit='capacity']")!.click();
    await settle();
    page(element).querySelector<HTMLButtonElement>("form[data-value-editor] button[type='button']")!.click();
    await settle();
    expect(page(element).querySelector("[data-section='vehicle']")?.getAttribute("data-vehicle")).toBe("vehicle_niro");
  });

  it("with one car: Bil · EV6, no tabs", async () => {
    const payload = twoCars();
    payload["vehicles"] = payload["vehicles"].slice(0, 1);
    payload["vehicle_choices"] = payload["vehicle_choices"].slice(0, 1);
    const { element } = await openSettings(payload);
    expect(page(element).querySelector("[data-section='vehicle'] h4")?.textContent).toBe("Bil · EV6");
    expect(page(element).querySelector("[role='tab']")).toBeNull();
  });
});
