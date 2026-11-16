// The charger card's Settings shows the shared popover's home battery and heat sections (`settings-popover.ts`),
// read for the charger's site when Settings opens, through the real card element.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, unknown> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the charger card's Settings", () => {
  it("reads the charger's site and shows its battery and heat sections", async () => {
    const hass = new FakeHass();
    hass.siteHandler = async (message) =>
      ({
        "spotnav/get_site_settings_view": read("site_view", "villa.json"),
        "spotnav/get_heat": read("heat_card", "villa.json"),
        "spotnav/battery_control": read("battery_control", "checking.json"),
      })[message["type"] as string];
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    const snapshot = hass.snapshot("snapshot", "en");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("dashboard", "cheapest_direct_site_admin.json"));
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(hass.siteMessages.map((message) => message["type"])).toEqual([
      "spotnav/get_site_settings_view", "spotnav/get_heat", "spotnav/battery_control",
    ]);
    expect(hass.siteMessages[0]).toEqual({ type: "spotnav/get_site_settings_view", api_version: 1, charger_id: "entry_a" });
    const battery = root.querySelector<HTMLElement>("[data-slot='battery'] [data-section='battery']")!;
    expect(battery.querySelector("[data-help='battery_control_enabled']")!.textContent).toBe(
      "Checking that the battery obeys (about 40 minutes).",
    );
    expect(battery.querySelector("[data-battery-progress]")!.textContent).toBe("Check running: 1 of 4 steps done.");
    expect(battery.querySelector("[data-note='ai_mode_back']")).not.toBeNull();
    expect(root.querySelector("[data-slot='heat'] [data-section='heat'] button[data-edit='indoor']")).not.toBeNull();
  });
});
