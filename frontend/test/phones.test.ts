// "Disconnect all phones" in Support and "Make a home of this" for a charger without a home (`phones.ts`), through
// the real card element: administrators only, never without the confirming press, the answers read strictly.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { decodeDisconnectAnswer, decodeMakeHomeAnswer, makeHomeEditor } from "../src/phones";
import { FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function mounted(
  payload: Record<string, unknown>,
  admin = true,
  homes: (message: Record<string, unknown>) => Promise<unknown> = async () => ({ api_version: 1, sites: [] }),
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  hass.homeHandler = homes;
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  if (admin) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

async function openSettings(element: Element): Promise<void> {
  shadow(element).querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)?.click();
  await settle();
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the answers", () => {
  it("are read strictly", () => {
    expect(decodeDisconnectAnswer({ api_version: 1, ok: true, error: null, chargers: 2, sites: 1 })).toEqual({
      ok: true,
      chargers: 2,
      sites: 1,
      notificationsNotCleared: 0,
    });
    expect(
      decodeDisconnectAnswer({ api_version: 1, ok: true, error: null, chargers: 2, sites: 1, notifications_not_cleared: 1 }),
    ).toMatchObject({ ok: true, notificationsNotCleared: 1 });
    expect(decodeDisconnectAnswer({ api_version: 1, ok: false, error: "spotnav_not_admin", chargers: 0, sites: 0 })).toEqual({
      ok: false,
      code: "spotnav_not_admin",
    });
    expect(decodeDisconnectAnswer({ api_version: 2, ok: true, chargers: 2, sites: 1 })).toBeNull();
    expect(decodeDisconnectAnswer({ api_version: 1, ok: true, chargers: -1, sites: 1 })).toBeNull();
    expect(decodeMakeHomeAnswer({ api_version: 1, ok: true, error: null, site_id: "abc" })).toEqual({ ok: true, siteId: "abc" });
    expect(decodeMakeHomeAnswer({ api_version: 1, ok: true, error: null, site_id: null })).toBeNull();
    expect(decodeMakeHomeAnswer({ api_version: 1, ok: false, error: "spotnav_home_exists", site_id: null })).toEqual({
      ok: false,
      code: "spotnav_home_exists",
    });
  });
});

describe("Disconnect all phones", () => {
  it("is in Support for an administrator, says what it does and disconnects only on the confirming press", async () => {
    const { hass, element } = await mounted(fixture("start_idle"), true, async (message) =>
      message.type === "spotnav/disconnect_phones"
        ? { api_version: 1, ok: true, error: null, chargers: 1, sites: 1 }
        : { api_version: 1, sites: [] },
    );
    await openSettings(element);
    const open = shadow(element).querySelector<HTMLButtonElement>("[data-disconnect-phones='open']");
    expect(open?.textContent).toBe(translate("en", "phones.disconnect.open"));
    open?.click();
    await settle();
    const dialog = openDialog(element);
    expect(dialog?.textContent).toContain(translate("en", "phones.disconnect.text"));
    expect(hass.homeMessages.filter((message) => message.type === "spotnav/disconnect_phones")).toEqual([]);
    const form = dialog?.querySelector<HTMLFormElement>("[data-value-editor='disconnect-phones']");
    // The dialog shows the form's buttons in its own action row.
    const confirm = dialog?.querySelector<HTMLButtonElement>("button[type='submit']");
    expect(form).not.toBeNull();
    expect(confirm?.textContent).toBe(translate("en", "phones.disconnect.confirm"));
    confirm?.click();
    await settle();
    expect(hass.homeMessages.filter((message) => message.type === "spotnav/disconnect_phones")).toEqual([
      { type: "spotnav/disconnect_phones", api_version: 1 },
    ]);
    expect(openDialog(element)?.querySelector("[data-value-editor='disconnect-phones']") ?? null).toBeNull();
  });

  it("says when it did not work", async () => {
    const { element } = await mounted(fixture("start_idle"), true, async (message) =>
      message.type === "spotnav/disconnect_phones" ? { api_version: 1, ok: false, error: "x", chargers: 0, sites: 0 } : { api_version: 1, sites: [] },
    );
    await openSettings(element);
    shadow(element).querySelector<HTMLButtonElement>("[data-disconnect-phones='open']")?.click();
    await settle();
    openDialog(element)?.querySelector<HTMLButtonElement>("button[type='submit']")?.click();
    await settle();
    expect(openDialog(element)?.querySelector("[role='alert']")?.textContent).toBe(translate("en", "phones.error.generic"));
  });

  it("says truthfully when some notifications could not be turned off", async () => {
    const { element } = await mounted(fixture("start_idle"), true, async (message) =>
      message.type === "spotnav/disconnect_phones"
        ? { api_version: 1, ok: true, error: null, chargers: 2, sites: 1, notifications_not_cleared: 1 }
        : { api_version: 1, sites: [] },
    );
    await openSettings(element);
    shadow(element).querySelector<HTMLButtonElement>("[data-disconnect-phones='open']")?.click();
    await settle();
    openDialog(element)?.querySelector<HTMLButtonElement>("button[type='submit']")?.click();
    await settle();
    expect(openDialog(element)?.querySelector("[role='alert']")?.textContent).toBe(
      translate("en", "phones.disconnect.notificationsLeft", { count: "1" }),
    );
  });

  it("is not offered to anyone else", async () => {
    const { element } = await mounted(fixture("start_idle"), false);
    await openSettings(element);
    expect(shadow(element).querySelector("[data-disconnect-phones='open']")).toBeNull();
  });
});

describe("Make a home of this", () => {
  const button = (element: Element): HTMLButtonElement | null =>
    shadow(element).querySelector<HTMLButtonElement>("[data-make-home='open']");

  it("is offered for a charger without a site when the installation has no home", async () => {
    const { hass, element } = await mounted(fixture("cheapest_no_site"));
    await openSettings(element);
    expect(hass.homeMessages.map((message) => message.type)).toContain("spotnav/list_sites");
    expect(button(element)?.textContent).toBe(translate("en", "makeHome.open"));
  });

  it("is not offered when the installation has a home, to a reader, or for a charger with a site", async () => {
    const withHome = await mounted(fixture("cheapest_no_site"), true, async () => ({
      api_version: 1,
      sites: [{ site_id: "s", name: "Villa", charger_ids: [] }],
    }));
    await openSettings(withHome.element);
    expect(button(withHome.element)).toBeNull();
    const reader = await mounted(fixture("cheapest_no_site"), false);
    await openSettings(reader.element);
    expect(button(reader.element)).toBeNull();
    const inSite = await mounted(fixture("cheapest_direct_site_admin"));
    await openSettings(inSite.element);
    expect(button(inSite.element)).toBeNull();
  });

  it("asks for the main fuse and makes the home only on the confirming press", async () => {
    const { hass, element } = await mounted(fixture("cheapest_no_site"), true, async (message) =>
      message.type === "spotnav/make_home"
        ? { api_version: 1, ok: true, error: null, site_id: "new_home" }
        : { api_version: 1, sites: [] },
    );
    await openSettings(element);
    button(element)?.click();
    await settle();
    const dialog = openDialog(element);
    expect(dialog?.textContent).toContain(translate("en", "makeHome.text"));
    const form = dialog?.querySelector<HTMLFormElement>("[data-value-editor='make-home']");
    const fuse = form?.querySelector<HTMLInputElement>("[data-value-field='main_fuse_a']");
    const confirm = dialog?.querySelector<HTMLButtonElement>("button[type='submit']");
    expect(confirm?.textContent).toBe(translate("en", "makeHome.confirm"));
    const makes = () => hass.homeMessages.filter((message) => message.type === "spotnav/make_home");

    // Nothing is made without a usable fuse.
    fuse!.value = "";
    confirm?.click();
    await settle();
    expect(openDialog(element)?.querySelector("[role='alert']")?.textContent).toBe(
      translate("en", "makeHome.error.range", { min: "6", max: "1,000" }),
    );
    expect(makes()).toEqual([]);

    fuse!.value = "20";
    confirm?.click();
    await settle();
    expect(makes()).toEqual([{ type: "spotnav/make_home", api_version: 1, charger_id: "entry_a", main_fuse_a: 20 }]);
  });

  it("says why when the installation has a home already", async () => {
    const { element } = await mounted(fixture("cheapest_no_site"), true, async (message) =>
      message.type === "spotnav/make_home"
        ? { api_version: 1, ok: false, error: "spotnav_home_exists", site_id: null }
        : { api_version: 1, sites: [] },
    );
    await openSettings(element);
    button(element)?.click();
    await settle();
    const dialog = openDialog(element);
    const form = dialog?.querySelector<HTMLFormElement>("[data-value-editor='make-home']");
    form!.querySelector<HTMLInputElement>("[data-value-field='main_fuse_a']")!.value = "25";
    dialog?.querySelector<HTMLButtonElement>("button[type='submit']")?.click();
    await settle();
    expect(openDialog(element)?.querySelector("[role='alert']")?.textContent).toBe(translate("en", "makeHome.error.exists"));
  });

  it("builds its editor on its own", () => {
    const form = makeHomeEditor(document, "sv", "t", async () => null, { onDone: () => undefined, onCancel: () => undefined });
    expect(form.textContent).toContain(translate("sv", "makeHome.fuse"));
  });
});
