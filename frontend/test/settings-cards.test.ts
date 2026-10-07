// The Settings popover as an overview, through the real card: the Solar card and its dialog, the
// strategy dialog's link to it, and what the overview never shows (raw reason codes, controls that
// save by themselves).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { FakeHass, mountCard } from "./helpers";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

function dashboard(patch: (payload: Record<string, any>) => void = () => undefined): Record<string, any> {
  const payload = read("dashboard", "cheapest_direct_site_admin.json");
  patch(payload);
  return payload;
}

const withSources = (selected: string[] = []) => (payload: Record<string, any>) => {
  payload["site"]["solar_forecast"] = {
    choices: [
      { id: "forecast_solar:roof", title: "Roof" },
      { id: "forecast_solar:garage", title: "Garage" },
    ],
    selected,
  };
};

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function open(
  payload: Record<string, any>,
  options: { admin?: boolean; language?: string } = {},
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  hass.entityHandler = async () => read("entity_config", "v1", "get_direct.json");
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", options.language ?? "en");
  if (options.admin !== false) {
    snapshot.user = { is_admin: true };
  }
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate((options.language ?? "en") as "en", "header.settings")}"]`)
    ?.click();
  await settle();
  return { hass, element };
}

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}
const dlg = (element: Element): HTMLElement => openDialog(element)!;
const solarCard = (element: Element) => dlg(element).querySelector<HTMLElement>("[data-section='solar']");
const siteWrites = (hass: FakeHass) => hass.messages.filter((m) => m["type"] === "spotnav/update_site_settings");
const saveButton = (element: Element) => dlg(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!;
const cancelButton = (element: Element) =>
  Array.from(dlg(element).querySelectorAll<HTMLButtonElement>("form button")).find(
    (node) => node.textContent === translate("en", "settings.cancel"),
  )!;
const choose = (element: Element, label: RegExp): HTMLInputElement =>
  Array.from(dlg(element).querySelectorAll<HTMLLabelElement>("label"))
    .find((node) => label.test(node.textContent ?? ""))!
    .querySelector<HTMLInputElement>("input")!;

/** Open the Solar section's value: the priority (one of two) or the forecast sources (several). */
async function openSolar(
  which: "solar_priority" | "solar_forecast" = "solar_priority",
  payload = dashboard(withSources(["forecast_solar:roof"])),
  options = {},
) {
  const opened = await open(payload, options);
  solarCard(opened.element)!.querySelector<HTMLButtonElement>(`[data-edit='${which}']`)!.click();
  await settle();
  return opened;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("the Solar section", () => {
  it("summarises the priority and the forecast sources in words, saying nothing of the one charger it applies to", async () => {
    const { element } = await open(dashboard(withSources(["forecast_solar:roof", "forecast_solar:garage"])));
    const card = solarCard(element)!;
    expect(card.querySelector("h4")?.textContent).toBe(translate("en", "settings.section.solar"));
    expect(card.querySelector("h4 svg")?.getAttribute("data-icon")).toBe("solar");
    expect(card.textContent).not.toContain(translate("en", "site.applies.one"));
    expect(card.querySelector("[data-row='solar_priority']")?.textContent).toContain(translate("en", "site.solarPriority.carFirst"));
    expect(card.querySelector("[data-row='solar_forecast']")?.textContent).toContain("Roof, Garage");
    // Each value is its own button, and nothing on the page saves by itself.
    expect(Array.from(card.querySelectorAll<HTMLElement>("button")).map((node) => node.dataset["edit"])).toEqual([
      "solar_priority",
      "solar_forecast",
    ]);
    expect(card.querySelector("input")).toBeNull();
  });

  it("says none when no forecast source is chosen", async () => {
    const { element } = await open(dashboard(withSources([])));
    expect(solarCard(element)!.querySelector("[data-row='solar_forecast']")?.textContent).toContain(
      translate("en", "settings.value.none"),
    );
  });

  it("is absent for a charger with no site", async () => {
    const { element } = await open(read("dashboard", "cheapest_no_site.json"));
    expect(solarCard(element)).toBeNull();
  });

  it("is read-only for a non-administrator", async () => {
    const { element } = await open(dashboard(withSources()), { admin: false });
    expect(solarCard(element)!.querySelector("button")).toBeNull();
  });
});

describe("the Solar editors", () => {
  it("open with the stored choice, a positive button and Cancel, and no request", async () => {
    const { hass, element } = await openSolar();
    expect(dlg(element).parentElement?.textContent).toContain(translate("en", "site.solarPriority.title"));
    expect(choose(element, /Car first/).checked).toBe(true);
    const buttons = Array.from(dlg(element).querySelectorAll<HTMLButtonElement>("form button")).map((node) => node.textContent);
    expect(buttons).toEqual([translate("en", "identify.choose"), translate("en", "settings.cancel")]);
    expect(siteWrites(hass)).toHaveLength(0);
    cancelButton(element).click();
    await settle();
    await openSolarAgain(element, "solar_forecast");
    expect(choose(element, /Roof/).checked).toBe(true);
    expect(choose(element, /Garage/).checked).toBe(false);
  });

  it("sends only the priority, with what was shown", async () => {
    const { hass, element } = await openSolar();
    choose(element, /Battery first/).click();
    expect(siteWrites(hass)).toHaveLength(0);
    saveButton(element).click();
    await settle();
    expect(siteWrites(hass)).toHaveLength(1);
    expect(siteWrites(hass)[0]).toMatchObject({ type: "spotnav/update_site_settings", charger_id: "entry_a" });
    expect(siteWrites(hass)[0]?.["changes"]).toEqual({ solar_priority: "battery_first" });
    expect(siteWrites(hass)[0]?.["expected"]).toEqual({ solar_priority: "car_first" });
  });

  it("sends only the forecast sources, with what was shown", async () => {
    const { hass, element } = await openSolar("solar_forecast");
    choose(element, /Garage/).click();
    saveButton(element).click();
    await settle();
    expect(siteWrites(hass)[0]?.["changes"]).toEqual({ solar_forecast: ["forecast_solar:roof", "forecast_solar:garage"] });
    expect(siteWrites(hass)[0]?.["expected"]).toEqual({ solar_forecast: ["forecast_solar:roof"] });
  });

  it("returns to Settings after a committed write, with one read of the dashboard", async () => {
    const { hass, element } = await openSolar();
    choose(element, /Battery first/).click();
    saveButton(element).click();
    await settle();
    hass.resolveNext(read("site_settings", "v1", "success.json"));
    await settle();
    expect(hass.messages.filter((m) => m["type"] === "spotnav/get_dashboard").length).toBeGreaterThan(1);
    hass.resolveNext(dashboard(withSources(["forecast_solar:roof"])));
    await settle();
    expect(dlg(element).querySelector("[data-section='solar']")).not.toBeNull();
    expect(dlg(element).querySelector("form[data-value-editor]")).toBeNull();
  });

  it("sends nothing when nothing changed, and returns to Settings", async () => {
    const { hass, element } = await openSolar();
    saveButton(element).click();
    await settle();
    expect(siteWrites(hass)).toHaveLength(0);
    expect(dlg(element).querySelector("[data-section='solar']")).not.toBeNull();
  });

  it("returns to Settings on Cancel without a request", async () => {
    const { hass, element } = await openSolar();
    choose(element, /Battery first/).click();
    cancelButton(element).click();
    await settle();
    expect(siteWrites(hass)).toHaveLength(0);
    expect(dlg(element).querySelector("[data-section='solar']")).not.toBeNull();
    expect(solarCard(element)!.querySelector("[data-row='solar_priority']")?.textContent).toContain(
      translate("en", "site.solarPriority.carFirst"),
    );
  });

  it("stays open with the sentence for a refusal, keeping the choice", async () => {
    const { hass, element } = await openSolar();
    choose(element, /Battery first/).click();
    saveButton(element).click();
    await settle();
    hass.resolveNext(read("site_settings", "v1", "invalid_value.json"));
    await settle();
    expect(dlg(element).querySelector("form[data-value-editor='single']")).not.toBeNull();
    expect(dlg(element).textContent).toContain(translate("en", "settings.error.invalid"));
    expect(dlg(element).textContent).not.toContain("spotnav_invalid_value");
    expect(choose(element, /Battery first/).checked).toBe(true);
    expect(saveButton(element).disabled).toBe(false);
  });
});

async function openSolarAgain(element: Element, which: string): Promise<void> {
  solarCard(element)!.querySelector<HTMLButtonElement>(`[data-edit='${which}']`)!.click();
  await settle();
}

describe("the overview's words", () => {
  it("shows no raw reason code and no technical-detail label, only sentences", async () => {
    const payload = dashboard((p) => {
      p["site"]["active_control"] = {
        available: false,
        enabled: false,
        writable: true,
        reason: "site_measurement_configured_missing",
      };
    });
    const { element } = await open(payload);
    const text = dlg(element).textContent ?? "";
    expect(text).toContain(translate("en", "site.activeControl.reason.measurement"));
    expect(text).not.toContain("site_measurement_configured_missing");
    expect(text).not.toContain("Technical detail");
    expect(dlg(element).querySelector(".spotnav-issue-technical")).toBeNull();
  });

  it("words an unknown reason code as the generic sentence, never as the code", async () => {
    const payload = dashboard((p) => {
      p["site"]["active_control"] = { available: false, enabled: false, writable: true, reason: "some_future_reason" };
    });
    const { element } = await open(payload);
    const text = dlg(element).textContent ?? "";
    expect(text).toContain(translate("en", "issue.unknown"));
    expect(text).not.toContain("some_future_reason");
  });

  it("saves nothing by itself: the overview has no input at all, only values that open their editors", async () => {
    const { hass, element } = await open(dashboard(withSources(["forecast_solar:roof"])));
    expect(dlg(element).querySelectorAll("input, select, textarea")).toHaveLength(0);
    expect(dlg(element).querySelector("[data-action='save-phases']")).toBeNull();
    expect(siteWrites(hass)).toHaveLength(0);
  });

  it("lists no unset optional site entity", async () => {
    const { element } = await open(dashboard());
    const site = dlg(element).querySelector<HTMLElement>("[data-section='site']")!;
    const rows = Array.from(site.querySelectorAll<HTMLElement>("[data-row]")).map((row) => row.dataset["row"]);
    expect(rows).not.toContain("direct_L1");
    expect(rows).not.toContain("battery_discharge_power_entity");
    expect(site.textContent).not.toContain(translate("en", "entity.notSet"));
  });
});

describe("the strategy dialog's way to Solar", () => {
  async function strategy(payload: Record<string, any>) {
    const hass = new FakeHass();
    hass.entityHandler = async () => read("entity_config", "v1", "get_direct.json");
    const element = mountCard(CONFIG, hass);
    const snapshot = hass.snapshot("snapshot", "en");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await settle();
    hass.resolveNext(payload);
    await settle();
    shadow(element).querySelector<HTMLButtonElement>("[data-cell='strategy']")!.click();
    await settle();
    return element;
  }

  const heldForSolarSettings = (payload: Record<string, any>) => {
    payload["strategy"]["available"] = [
      { strategy: "cheapest", available: true, reason: null },
      { strategy: "solar", available: false, reason: "needs_solar_surplus_measurement" },
      { strategy: "hybrid", available: false, reason: "needs_solar_and_price_control" },
    ];
  };

  it("links a solar strategy held back for lack of solar settings to the Solar card", async () => {
    const element = await strategy(dashboard(heldForSolarSettings));
    const link = dlg(element).querySelector<HTMLButtonElement>("[data-action='setup-solar']");
    expect(link?.textContent).toBe(translate("en", "strategy.setupSolar"));
    link!.click();
    await settle();
    expect(dlg(element).querySelector("[data-section='solar']")).not.toBeNull();
    expect(dlg(element).textContent).toContain(translate("en", "settings.overview.titleNamed", { name: "cheapest_direct_admin" }));
  });

  it("says a direct site lacks the meter's total grid power and links to the site's entities, not to Solar", async () => {
    const element = await strategy(dashboard());
    expect(dlg(element).textContent).toContain("Solar needs the meter's total grid power");
    expect(dlg(element).querySelector("[data-action='setup-solar']")).toBeNull();
    const link = dlg(element).querySelector<HTMLButtonElement>("[data-action='setup-site']");
    expect(link?.textContent).toBe(translate("en", "strategy.setupSite"));
    link!.click();
    await settle();
    await settle();
    expect(dlg(element).querySelector("[data-entity-editor='site']")).not.toBeNull();
    expect(dlg(element).querySelector("[data-field-block='grid_power_source_power']")).not.toBeNull();
  });

  it("offers an administrator only that link, none to a reader", async () => {
    const hass = new FakeHass();
    hass.entityHandler = async () => read("entity_config", "v1", "get_direct.json");
    const element = mountCard(CONFIG, hass);
    element.hass = hass.snapshot("snapshot", "en");
    await settle();
    hass.resolveNext(dashboard());
    await settle();
    shadow(element).querySelector<HTMLButtonElement>("[data-cell='strategy']")!.click();
    await settle();
    expect(dlg(element).textContent).toContain("Solar needs the meter's total grid power");
    expect(dlg(element).querySelector("[data-action='setup-site']")).toBeNull();
  });

  it("offers no link when the charger has no site to set solar up on", async () => {
    const element = await strategy(read("dashboard", "cheapest_no_site.json"));
    expect(dlg(element).querySelector("[data-action='setup-solar']")).toBeNull();
  });
});
