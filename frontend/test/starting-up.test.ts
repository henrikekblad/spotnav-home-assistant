// The start-up grace: "Starting up…" as the status line, and no Start or Stop, while the backend says so.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { decodeDashboard } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "soc_charger" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const fixture = (name: string): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as Record<string, any>;

const settle = (): Promise<void> => vi.advanceTimersByTimeAsync(0).then(() => undefined);

async function mounted(payload: Record<string, unknown>, language = "en") {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

function starting(name: string, waiting: string[] = ["charger"]): Record<string, any> {
  const payload = fixture(name);
  payload["starting_up"] = { active: true, until: "2026-10-03T12:03:00Z", waiting_for: waiting };
  payload["status"] = { tone: "normal", lines: [{ code: "starting_up", params: {} }] };
  return payload;
}

const action = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>("button[data-action='start'], button[data-action='stop']");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the start-up grace in the card", () => {
  it("decodes the block, and an older backend without it as null", () => {
    const withBlock = decodeDashboard(starting("start_idle.json"));
    expect(withBlock.ok && withBlock.value.starting_up).toEqual({
      active: true,
      until: "2026-10-03T12:03:00Z",
      waiting_for: ["charger"],
    });
    const older = fixture("start_idle.json");
    delete older["starting_up"];
    const result = decodeDashboard(older);
    expect(result.ok && result.value.starting_up).toBeNull();
  });

  it.each([
    ["en", "Starting up…"],
    ["sv", "Startar upp…"],
    ["nb", "Starter opp…"],
    ["da", "Starter op…"],
    ["fi", "Käynnistyy…"],
  ])("says it in the status line in %s and offers no Start", async (language, words) => {
    const { element } = await mounted(starting("start_idle.json"), language);
    expect(shadow(element).textContent).toContain(words);
    const button = action(element);
    expect(button).not.toBeNull();
    expect(button!.disabled).toBe(true);
    expect(button!.dataset["renderedDisabled"]).toBe("true");
    const planner = shadow(element).querySelector<HTMLButtonElement>("button[data-action='pause'], button[data-action='resume']");
    expect(planner).not.toBeNull();
    expect(planner!.disabled).toBe(true);
  });

  it("disables Stop too while a running charge is not yet confirmed", async () => {
    const { element } = await mounted(starting("stop_charging.json"));
    const button = action(element)!;
    expect(button.dataset["action"]).toBe("stop");
    expect(button.disabled).toBe(true);
  });

  it("is a normal card once the backend says the grace is over", async () => {
    const payload = fixture("start_idle.json");
    payload["starting_up"] = { active: false, until: null, waiting_for: [] };
    const { element } = await mounted(payload);
    expect(shadow(element).textContent).not.toContain("Starting up…");
    expect(action(element)!.disabled).toBe(false);
    const planner = shadow(element).querySelector<HTMLButtonElement>("button[data-action='pause'], button[data-action='resume']");
    expect(planner!.disabled).toBe(false);
  });
});
