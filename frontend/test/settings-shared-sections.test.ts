// The charger card's Settings shows the shared popover's home battery and heat sections (`settings-popover.ts`),
// read for the charger's site when Settings opens, and "Get started" at the top, through the real card element.

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

  it("starts with Get started, whose battery row opens the battery section's switch", async () => {
    const hass = new FakeHass();
    hass.siteHandler = async (message) =>
      ({
        "spotnav/get_site_settings_view": read("site_view", "villa.json"),
        "spotnav/get_heat": read("heat_card", "villa.json"),
        "spotnav/battery_control": read("battery_control", "needs_ack.json"),
      })[message["type"] as string];
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    const snapshot = hass.snapshot("snapshot", "sv");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await vi.advanceTimersByTimeAsync(0);
    const payload = read("dashboard", "cheapest_direct_site_admin.json");
    payload["setup"] = {
      items: [
        { id: "battery", dismissible: true },
        { id: "phone", dismissible: true },
      ],
    };
    hass.resolveNext(payload);
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>(`[aria-label="${translate("sv", "header.settings")}"]`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    const sections = Array.from(root.querySelectorAll<HTMLElement>(".spotnav-settings-section[data-section]"));
    const setup = sections[0]!;
    expect(setup.dataset["section"]).toBe("setup");
    expect(setup.querySelector("h3, h4, h2")?.textContent).toContain("Kom igång");
    expect(Array.from(setup.querySelectorAll<HTMLElement>("[data-setup]")).map((row) => row.dataset["setup"])).toEqual([
      "battery",
      "phone",
    ]);
    setup.querySelector<HTMLButtonElement>("[data-setup='battery'] [data-setup-action='open']")!.click();
    await vi.advanceTimersByTimeAsync(1000);
    const dialogs = Array.from(root.querySelectorAll<HTMLElement>("[role='dialog']"));
    const shown = dialogs.filter((dialog) => dialog.closest("[hidden]") === null);
    expect(shown.map((dialog) => dialog.querySelector(".spotnav-dialog-title")?.textContent)).toContain(
      translate("sv", "battery.switch"),
    );
  });

  it("leaves Get started out when nothing is left", async () => {
    const hass = new FakeHass();
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    const snapshot = hass.snapshot("snapshot", "en");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await vi.advanceTimersByTimeAsync(0);
    const payload = read("dashboard", "cheapest_direct_site_admin.json");
    payload["setup"] = { items: [] };
    hass.resolveNext(payload);
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-section='setup']")).toBeNull();
  });
});
