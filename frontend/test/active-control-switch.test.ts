// The active load-balancing switch in the Settings popover's site section, through the real card.
//
// Available and enabled are separate facts (four combinations), the write is never optimistic, the
// answer's site block is adopted in place while the popover stays open, and a disable says in words
// what became of any current balancing had lowered -- a failed restore never reads as restored.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { decodeSiteSettingsAnswer, activeControlNotice } from "../src/site-settings";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function dashboard(
  active: { available: boolean; enabled: boolean; reason: string | null },
  writable = true,
  switchWritable = writable,
) {
  const payload = JSON.parse(
    readFileSync(join(ROOT, "dashboard", "cheapest_direct_site_admin.json"), "utf8"),
  ) as { site: { active_control: unknown; writable: boolean } };
  // The backend states the switch's own writability beside the site's: an admin reader may both.
  payload.site.active_control = { ...active, writable: switchWritable };
  payload.site.writable = writable;
  return payload;
}

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(ROOT, "site_settings", "v1", name), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function open(
  active: { available: boolean; enabled: boolean; reason: string | null },
  options: { admin?: boolean; writable?: boolean; switchWritable?: boolean; language?: string } = {},
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", options.language ?? "en");
  if (options.admin !== false) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  await settle();
  hass.resolveNext(dashboard(active, options.writable ?? true, options.switchWritable ?? options.writable ?? true));
  await settle();
  const language = options.language ?? "en";
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate(language as "en", "header.settings")}"]`)
    ?.click();
  await settle();
  return { hass, element };
}

const control = (element: Element): HTMLInputElement | null =>
  shadow(element).querySelector<HTMLInputElement>(`input.${VISUAL_CLASSES.switchControl}`);
const slot = (element: Element): HTMLElement =>
  shadow(element).querySelector<HTMLElement>("[data-slot='active-control']") as HTMLElement;
const siteWrites = (hass: FakeHass) => hass.messages.filter((m) => m["type"] === "spotnav/update_site_settings");

const ON = { available: true, enabled: true, reason: null };
const OFF = { available: true, enabled: false, reason: null };
const ENABLED_UNAVAILABLE = { available: false, enabled: true, reason: "site_measurement_stale" };
const UNAVAILABLE_OFF = { available: false, enabled: false, reason: "site_measurement_stale" };

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the switch in each available x enabled state", () => {
  it("available and on: on, and can be turned off", async () => {
    const { element } = await open(ON);
    expect(control(element)?.checked).toBe(true);
    expect(control(element)?.disabled).toBe(false);
    expect(control(element)?.getAttribute("role")).toBe("switch");
  });

  it("available and off: off, and can be turned on", async () => {
    const { element } = await open(OFF);
    expect(control(element)?.checked).toBe(false);
    expect(control(element)?.disabled).toBe(false);
  });

  it("enabled but unavailable: stays on, can be turned off, and shows the reason", async () => {
    const { element } = await open(ENABLED_UNAVAILABLE);
    expect(control(element)?.checked).toBe(true);
    expect(control(element)?.disabled).toBe(false);
    expect(slot(element).textContent).toContain(translate("en", "site.activeControl.reason.measurement"));
  });

  it("unavailable and off: disabled, cannot be turned on, and shows the reason", async () => {
    const { hass, element } = await open(UNAVAILABLE_OFF);
    expect(control(element)?.checked).toBe(false);
    expect(control(element)?.disabled).toBe(true);
    expect(slot(element).textContent).toContain(translate("en", "site.activeControl.reason.measurement"));
    control(element)?.click();
    await settle();
    expect(siteWrites(hass)).toHaveLength(0);
  });

  it("says it is best effort and not a protective device, in every state", async () => {
    for (const state of [ON, OFF, ENABLED_UNAVAILABLE, UNAVAILABLE_OFF]) {
      document.body.innerHTML = "";
      const { element } = await open(state);
      expect(slot(element).textContent).toContain(translate("en", "site.activeControl.note"));
    }
    expect(translate("en", "site.activeControl.note")).toContain("not a protective device");
  });

  it("is read-only for a reader who may not write: a state, no control, the reason kept", async () => {
    const { element } = await open(ENABLED_UNAVAILABLE, { admin: false, writable: false });
    expect(control(element)).toBeNull();
    expect(slot(element).textContent).toContain(translate("en", "site.activeControl.on"));
    expect(slot(element).textContent).toContain(translate("en", "site.activeControl.reason.measurement"));
  });
});

describe("the backend's own answer about the switch", () => {
  it("offers no switch when the site is writable but load balancing is not (a webhook reader)", async () => {
    const { element } = await open(ON, { writable: true, switchWritable: false });
    expect(control(element)).toBeNull();
    expect(slot(element).textContent).toContain(translate("en", "site.activeControl.on"));
  });
});

describe("a press is never optimistic", () => {
  it("stays on the confirmed value and pending while the call runs, sending it alone with its expectation", async () => {
    const { hass, element } = await open(OFF);
    const input = control(element) as HTMLInputElement;
    input.click();
    await settle();
    expect(siteWrites(hass)).toEqual([
      {
        type: "spotnav/update_site_settings",
        api_version: 1,
        charger_id: "entry_a",
        expected: { active_control_enabled: false },
        changes: { active_control_enabled: true },
      },
    ]);
    const pending = control(element) as HTMLInputElement;
    expect(pending.checked).toBe(false); // still the confirmed value
    expect(pending.disabled).toBe(true);
    expect(pending.getAttribute("aria-busy")).toBe("true");
    expect(slot(element).textContent).toContain(translate("en", "site.activeControl.pending"));
    // A second press while pending sends nothing.
    pending.click();
    await settle();
    expect(siteWrites(hass)).toHaveLength(1);

    hass.resolveNext(fixture("enable.json"));
    await settle();
    const after = control(element) as HTMLInputElement;
    expect(after.checked).toBe(true);
    expect(after.disabled).toBe(false);
    expect(after.getAttribute("aria-busy")).toBeNull();
    // The popover stayed open across the confirming re-read.
    expect(shadow(element).querySelector("[data-slot='active-control']")).not.toBeNull();
  });

  it("a refused turn-on keeps it off and says why in its own sentence", async () => {
    const { hass, element } = await open(OFF);
    control(element)?.click();
    await settle();
    hass.resolveNext(fixture("enable_unavailable.json"));
    await settle();
    expect(control(element)?.checked).toBe(false);
    expect(control(element)?.disabled).toBe(true); // the adopted block says unavailable-and-off
    expect(slot(element).textContent).toContain(translate("en", "site.activeControl.error.unavailable"));
  });

  it("a transport failure keeps the confirmed value and says so", async () => {
    const { hass, element } = await open(ON);
    control(element)?.click();
    await settle();
    hass.rejectNext(new Error("boom"));
    await settle();
    expect(control(element)?.checked).toBe(true);
    expect(control(element)?.disabled).toBe(false);
  });
});

describe("what a disable says", () => {
  it("nothing needed restoring", async () => {
    const { hass, element } = await open(ON);
    control(element)?.click();
    await settle();
    expect(siteWrites(hass)[0]?.["changes"]).toEqual({ active_control_enabled: false });
    hass.resolveNext(fixture("disable_not_needed.json"));
    await settle();
    expect(control(element)?.checked).toBe(false);
    const text = slot(element).textContent ?? "";
    expect(text).toContain(translate("en", "site.activeControl.restore.off"));
    expect(text).toContain(translate("en", "site.activeControl.restore.notNeeded"));
  });

  it("restored, with the current per charger", async () => {
    const { hass, element } = await open(ON);
    control(element)?.click();
    await settle();
    hass.resolveNext(fixture("disable_restored.json"));
    await settle();
    expect(slot(element).textContent).toContain("The charger was restored to 16 A.");
    expect(slot(element).querySelector(`.${VISUAL_CLASSES.activeNoticeWarning}`)).toBeNull();
  });

  it("a failed restore says the charger may still be limited, in a warning, and keeps the code", async () => {
    const { hass, element } = await open(ON);
    control(element)?.click();
    await settle();
    hass.resolveNext(fixture("disable_restore_failed.json"));
    await settle();
    expect(control(element)?.checked).toBe(false); // the opt-in itself is off
    const notice = slot(element).querySelector<HTMLElement>(`.${VISUAL_CLASSES.activeNoticeWarning}`);
    expect(notice).not.toBeNull();
    expect(notice?.textContent).toContain("may still be limited");
    expect(notice?.textContent).not.toContain("was restored");
    expect(notice?.dataset["code"]).toBeTruthy();
  });
});

describe("the sentences, in five locales", () => {
  const raw = (name: string) => {
    const decoded = decodeSiteSettingsAnswer(fixture(name));
    if (!decoded.ok) {
      throw new Error(name);
    }
    return decoded.value;
  };

  it("word every fixture outcome, without leaking a placeholder, in every language", () => {
    for (const language of LANGUAGES) {
      for (const [name, disabling] of [
        ["disable_not_needed.json", true],
        ["disable_restored.json", true],
        ["disable_restore_failed.json", true],
        ["enable.json", false],
        ["enable_unavailable.json", false],
        ["conflict.json", false],
      ] as const) {
        const answer = name === "conflict.json" ? { ok: false as const, code: "spotnav_conflict", site: null, restore: null } : raw(name);
        const notice = activeControlNotice(language, answer, disabling);
        for (const line of notice?.lines ?? []) {
          expect(line, `${language} ${name}`).not.toMatch(/[{}]/);
          expect(line.length).toBeGreaterThan(0);
        }
      }
    }
  });

  it("gives the failed restore its own sentence per code, each saying the charger may still be limited", () => {
    const codes = [
      "write_failed",
      "unconfirmed",
      "assigned_current_unreadable",
      "no_authoritative_current",
      "charger_not_loaded",
      "membership_conflict",
      "probe_in_flight",
      "below_minimum",
      "no_connector_target",
      "external_balancer",
    ];
    const sentences = new Set<string>();
    for (const code of codes) {
      const answer = {
        ok: true as const,
        site: null as never,
        restore: {
          outcome: "failed" as const,
          chargers: [{ chargerId: "c", chargerName: "Charger 1", outcome: "failed" as const, code, fromA: 10, toA: 16 }],
        },
      };
      const notice = activeControlNotice("en", answer, true);
      const sentence = notice?.lines[1] ?? "";
      expect(sentence).toContain("may still be limited");
      sentences.add(sentence);
      expect(notice?.code).toBe(code);
    }
    expect(sentences.size).toBe(codes.length);
  });

  it("names each charger when the site has more than one", () => {
    const notice = activeControlNotice(
      "sv",
      {
        ok: true,
        site: null as never,
        restore: {
          outcome: "failed",
          chargers: [
            { chargerId: "a", chargerName: "Garaget", outcome: "restored", code: null, fromA: 10, toA: 16 },
            { chargerId: "b", chargerName: "Carporten", outcome: "failed", code: "write_failed", fromA: 10, toA: 16 },
          ],
        },
      },
      true,
    );
    expect(notice?.lines[1]).toBe("Garaget återställdes till 16 A.");
    expect(notice?.lines[2]).toContain("Carporten:");
    expect(notice?.lines[2]).toContain("begränsad");
  });
});

describe("the strict decoder", () => {
  it("refuses a restore whose overall outcome disagrees with its chargers", () => {
    const answer = fixture("disable_restore_failed.json") as { restore: { outcome: string } };
    answer.restore.outcome = "restored";
    expect(decodeSiteSettingsAnswer(answer)).toEqual({ ok: false, failure: "malformed" });
  });

  it("refuses another version as unsupported, and an answer without restore as malformed", () => {
    const other = { ...fixture("enable.json"), api_version: 2 };
    expect(decodeSiteSettingsAnswer(other)).toEqual({ ok: false, failure: "unsupported" });
    const missing = fixture("enable.json");
    delete missing["restore"];
    expect(decodeSiteSettingsAnswer(missing)).toEqual({ ok: false, failure: "malformed" });
  });
});
