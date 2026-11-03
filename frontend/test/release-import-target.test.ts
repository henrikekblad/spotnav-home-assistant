// "Release the import target for this charge" beside the held or paused line, for any user, confirmed before it is sent.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FakeHass, mountCard } from "./helpers";

const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function dashboard(): Record<string, unknown> {
  const payload = JSON.parse(readFileSync(join(DASHBOARD_DIR, "start_idle.json"), "utf8")) as Record<string, unknown>;
  payload["status"] = {
    tone: "normal",
    lines: [
      {
        code: "import_target_held",
        params: {
          limit_a: 7,
          target_kw: 6,
          hour_kw: 5.1,
          set_kw: 6,
          releasable: true,
          release_until: null,
          release_peak_kw: 11,
        },
      },
    ],
  };
  return payload;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  document.body.replaceChildren();
});

describe("Release the import target for this charge", () => {
  it("is offered with the held line to a user who is not an administrator, and only Release sends it", async () => {
    const hass = new FakeHass();
    const element = mountCard(CONFIG, hass);
    element.hass = hass.snapshot("snapshot", "sv");
    await settle();
    hass.resolveNext(dashboard());
    await settle();
    const root = shadow(element);
    const offer = root.querySelector<HTMLButtonElement>("[data-part='release-import-target'] button");
    expect(offer?.textContent).toBe("Släpp effektmålet för den här laddningen");
    offer!.click();
    await settle();
    const dialogText = (): string =>
      [...root.querySelectorAll<HTMLElement>("[role='dialog']")]
        .map((dialog) => ((dialog.parentElement as HTMLElement).hidden ? "" : dialog.textContent))
        .join("");
    expect(dialogText()).toContain("Släppa effektmålet?");
    expect(dialogText()).toContain("Månadens topp kan bli upp till 11 kW, mot 6,0 kW i dag.");
    const sent = (): number => hass.messages.filter((message) => message["type"] === "spotnav/release_import_target").length;
    root.querySelector<HTMLButtonElement>("[data-action='cancel']")!.click();
    await settle();
    expect(sent()).toBe(0);
    expect(dialogText()).toBe("");
    offer!.click();
    await settle();
    root.querySelector<HTMLButtonElement>("[data-action='release']")!.click();
    await settle();
    expect(sent()).toBe(1);
    expect(hass.messages.find((message) => message["type"] === "spotnav/release_import_target")).toMatchObject({
      api_version: 1,
      charger_id: "entry_a",
    });
  });
});
