// Every dialog keeps its title and close in view: the header is the panel's own top row and only the part
// below it (the intro and the body) scrolls, in the shell and in each dialog the card opens.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createDialog } from "../src/dialog";
import { translate } from "../src/i18n";
import { VISUAL_CLASSES as C, VISUAL_STYLES } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";
import { mountPoint } from "./visual-fixtures";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, any>;

function rule(className: string): string {
  return VISUAL_STYLES.match(new RegExp(`^\\s*\\.${className} \\{([^}]*)\\}`, "m"))?.[1] ?? "";
}

/** The header outside the scrolling part, the content inside it, for one dialog panel. */
function expectHeaderInView(panel: HTMLElement): void {
  const header = panel.querySelector<HTMLElement>(`.${C.dialogHeader}`)!;
  const scroller = panel.querySelector<HTMLElement>(`.${C.dialogScroll}`)!;
  expect(header).not.toBeNull();
  expect(scroller).not.toBeNull();
  expect(Array.from(panel.children)).toEqual([header, scroller]);
  expect(scroller.contains(header)).toBe(false);
  expect(header.querySelector(`.${C.dialogTitle}`)).not.toBeNull();
  expect(header.querySelector(`.${C.dialogClose}`)).not.toBeNull();
  expect(scroller.querySelector(`.${C.dialogBody}`)).not.toBeNull();
  expect(scroller.querySelector(`.${C.dialogIntro}`)).not.toBeNull();
}

describe("the shell", () => {
  it("scrolls only the intro and the body, under a header that is not part of the scroll", () => {
    const mounted = mountPoint("in-view");
    const handle = createDialog({ owner: mounted.root, idPrefix: "in-view", labels: { close: "Close" } });
    const body = document.createElement("p");
    handle.show({ title: "A long dialog", body, intro: "Intro" });
    const panel = handle.element.querySelector<HTMLElement>(`.${C.dialog}`)!;
    expectHeaderInView(panel);
    expect(panel.querySelector(`.${C.dialogScroll}`)?.contains(body)).toBe(true);
    // Labelling and the description still point at the same nodes.
    expect(panel.getAttribute("aria-labelledby")).toBe(panel.querySelector(`.${C.dialogTitle}`)?.id);
    expect(panel.getAttribute("aria-describedby")).toBe(panel.querySelector(`.${C.dialogIntro}`)?.id);
    handle.destroy();
  });

  it("draws a divider under the header only while the content is scrolled", () => {
    const mounted = mountPoint("in-view-divider");
    const handle = createDialog({ owner: mounted.root, idPrefix: "divider", labels: { close: "Close" } });
    handle.show({ title: "T", body: document.createElement("p") });
    const panel = handle.element.querySelector<HTMLElement>(`.${C.dialog}`)!;
    const scroller = panel.querySelector<HTMLElement>(`.${C.dialogScroll}`)!;
    expect(panel.classList.contains(C.dialogScrolled)).toBe(false);
    scroller.scrollTop = 120;
    scroller.dispatchEvent(new Event("scroll"));
    expect(panel.classList.contains(C.dialogScrolled)).toBe(true);
    scroller.scrollTop = 0;
    scroller.dispatchEvent(new Event("scroll"));
    expect(panel.classList.contains(C.dialogScrolled)).toBe(false);
    handle.destroy();
  });

  it("is a flex column whose body scrolls, without fixed or sticky positioning inside the panel", () => {
    const panel = rule(C.dialog);
    expect(panel).toContain("flex-direction: column");
    expect(panel).toContain("overflow: hidden");
    const header = rule(C.dialogHeader);
    expect(header).toContain("flex: none");
    expect(header).toContain("background: inherit");
    expect(header).not.toMatch(/position: (fixed|sticky)/);
    const scroller = rule(C.dialogScroll);
    expect(scroller).toContain("overflow-y: auto");
    expect(scroller).toContain("min-height: 0");
    expect(VISUAL_STYLES).toMatch(new RegExp(`\\.${C.dialogScrolled} > \\.${C.dialogHeader} \\{[^}]*--divider-color`));
  });
});

// ------------------------------------------------------------------------------------------------
// The card's dialogs: every one the card builds, and the ones a reader opens.
// ------------------------------------------------------------------------------------------------

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function twoCars(): Record<string, any> {
  const payload = read("dashboard", "cheapest_direct_site_admin.json");
  const two = read("dashboard", "target_soc_two_vehicles.json");
  payload["vehicles"] = two["vehicles"];
  payload["vehicle_choices"] = [{ id: "vehicle_ev6", name: "EV6" }, { id: "vehicle_niro", name: "Testbil" }];
  payload["target_vehicle_id"] = "vehicle_ev6";
  payload["soc"] = two["soc"];
  payload["settings"]["identify_mode"] = "automatic";
  return payload;
}

async function mounted() {
  const hass = new FakeHass();
  const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(twoCars());
  await settle();
  return { hass, element };
}

function openPanel(element: Element): HTMLElement {
  return Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find((dialog) => dialog.closest("[hidden]") === null)!;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the card's dialogs", () => {
  it("builds every dialog with its header outside the scrolling part", async () => {
    const { element } = await mounted();
    const panels = Array.from(shadow(element).querySelectorAll<HTMLElement>(`.${C.overlay} > .${C.dialog}`));
    // Issues, capabilities, pause, strategy, vehicle, plan, price, entities, Settings, history and value editors.
    expect(panels.length).toBeGreaterThanOrEqual(11);
    panels.forEach(expectHeaderInView);
  });

  it("keeps the header out of the scroll in Settings, a value editor on top of it, history and the plan", async () => {
    const { element } = await mounted();
    shadow(element).querySelector<HTMLButtonElement>(`[aria-label='${translate("en", "header.settings")}']`)!.click();
    await settle();
    const settings = openPanel(element);
    expect(settings).toBeDefined();
    expectHeaderInView(settings);
    settings.querySelector<HTMLButtonElement>("[data-edit='identify_mode']")!.click();
    await settle();
    const editor = openPanel(element);
    expect(editor).not.toBe(settings);
    expectHeaderInView(editor);
    editor.querySelector<HTMLButtonElement>(`.${C.dialogClose}`)!.click();
    await settle();
    openPanel(element).querySelector<HTMLButtonElement>(`.${C.dialogClose}`)!.click();
    await settle();

    shadow(element).querySelector<HTMLButtonElement>("button[data-history='open']")!.click();
    await settle();
    expectHeaderInView(openPanel(element));
    openPanel(element).querySelector<HTMLButtonElement>(`.${C.dialogClose}`)!.click();
    await settle();

    shadow(element).querySelector<HTMLButtonElement>(".spotnav-settings-trigger[data-setting='plan']")!.click();
    await settle();
    expectHeaderInView(openPanel(element));
  });
});
