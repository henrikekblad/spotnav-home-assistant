// "Get started" through the real card: the `setup` decoder, the attention line it gives an administrator when
// nothing more serious is shown, the dialog's section, what its buttons do, and "Not needed".

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate, type Language } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { addIntegrationPath } from "../src/price-provider";
import { APP_STORE_URL, INTEGRATIONS_PATH, setupLineText, setupRowsOf } from "../src/setup-checklist";
import { decodeDashboard } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

type Row = { id: string; dismissible: boolean };
const ALL: Row[] = [
  { id: "car_level", dismissible: false },
  { id: "site", dismissible: true },
  { id: "phone", dismissible: true },
  { id: "app", dismissible: true },
];

/** The no-site fixture (nothing to review in its status) with the given `setup` rows. */
function dashboard(items: Row[] | null, fixture = "cheapest_no_site.json"): Record<string, any> {
  const payload = read("dashboard", fixture);
  if (items === null) {
    delete payload["setup"];
  } else {
    payload["setup"] = { items };
  }
  return payload;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function open(
  payload: Record<string, any>,
  options: { admin?: boolean; language?: Language } = {},
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", options.language ?? "en");
  snapshot.user = { is_admin: options.admin ?? true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

const line = (element: Element) => shadow(element).querySelector<HTMLButtonElement>("[data-banner='setup']");
function openDialog(element: Element): HTMLElement {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null)!;
}
const title = (element: Element) => openDialog(element).querySelector(".spotnav-dialog-title")?.textContent ?? null;
const section = (element: Element) => openDialog(element).querySelector<HTMLElement>("[data-section='setup']");
const row = (element: Element, id: string) => section(element)!.querySelector<HTMLElement>(`[data-setup="${id}"]`)!;
const button = (node: HTMLElement, action: string) =>
  node.querySelector<HTMLButtonElement>(`[data-setup-action="${action}"]`);

let pushed: string[] = [];
let opened: string[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
  pushed = [];
  opened = [];
  vi.spyOn(window.history, "pushState").mockImplementation((_data, _unused, url) => {
    pushed.push(String(url));
  });
  vi.spyOn(window, "open").mockImplementation((url) => {
    opened.push(String(url));
    return null;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("the setup block", () => {
  it("is read with its rows", () => {
    const decoded = decodeDashboard(dashboard(ALL));
    expect(decoded.ok && decoded.value.setup).toEqual({ items: ALL });
  });

  it("is null, not a refused dashboard, when it is absent or cannot be read", () => {
    for (const block of [null, "x", { items: [{ id: "site" }] }, { items: [], more: 1 }, { items: [{ id: 3, dismissible: true }] }]) {
      const payload = dashboard(null);
      if (block !== null) {
        payload["setup"] = block;
      }
      const decoded = decodeDashboard(payload);
      expect(decoded.ok).toBe(true);
      expect(decoded.ok && decoded.value.setup).toBeNull();
    }
  });

  it("keeps the rows the card knows, in its own order", () => {
    const decoded = decodeDashboard(
      dashboard([{ id: "app", dismissible: true }, { id: "prices", dismissible: true }, { id: "car_level", dismissible: false }]),
    );
    expect(setupRowsOf(decoded.ok ? decoded.value.setup : null).map((entry) => entry.id)).toEqual(["car_level", "app"]);
  });
});

describe("the attention line", () => {
  it("says how many things are left, for an administrator", async () => {
    const { element } = await open(dashboard(ALL.slice(1)));
    expect(line(element)?.textContent).toBe("Get started: 3 things left");
  });

  it("is in the reader's language and plural", async () => {
    const { element } = await open(dashboard([ALL[1]!]), { language: "sv" });
    expect(line(element)?.textContent).toBe("Kom igång: 1 sak kvar");
    expect(setupLineText("sv", 3)).toBe("Kom igång: 3 saker kvar");
    expect(setupLineText("en", 1)).toBe("Get started: 1 thing left");
  });

  it("is not there when nothing is left, without the block, or for anyone else", async () => {
    expect(line((await open(dashboard([]))).element)).toBeNull();
    expect(line((await open(dashboard(null))).element)).toBeNull();
    expect(line((await open(dashboard(ALL), { admin: false })).element)).toBeNull();
  });

  it("gives way to a more serious line, and the dialog still lists what is left", async () => {
    const payload = dashboard(ALL);
    payload["status"] = { tone: "blocking", lines: [{ code: "price_data_unavailable", params: { reason: null } }] };
    const { element } = await open(payload);
    expect(line(element)).toBeNull();
    const banner = shadow(element).querySelector<HTMLButtonElement>(".spotnav-banner")!;
    expect(banner.textContent).toContain(translate("en", "issue.banner.blocking"));
    banner.click();
    await settle();
    expect(openDialog(element).querySelector("[data-code='price_data_unavailable']")).not.toBeNull();
    expect(section(element)).not.toBeNull();
    // Beside issues: the dialog keeps its title, and Get started is a section of its own under its heading.
    expect(title(element)).toBe(translate("en", "dialog.issues"));
    expect(section(element)!.querySelector("h3")?.textContent).toBe("Get started");
  });
});

describe("the dialog's Get started section", () => {
  it("is called Kom igång in Swedish when it is alone", async () => {
    const { element } = await open(dashboard(ALL), { language: "sv" });
    line(element)!.click();
    await settle();
    expect(title(element)).toBe("Kom igång");
  });

  it("puts each row in its own box, the sentence first and its buttons below", async () => {
    const { element } = await open(dashboard(ALL));
    line(element)!.click();
    await settle();
    const boxes = Array.from(section(element)!.querySelectorAll<HTMLElement>("[data-setup]"));
    expect(boxes).toHaveLength(ALL.length);
    for (const box of boxes) {
      expect(box.classList.contains("spotnav-suggestion")).toBe(true);
      expect(box.dataset["banner"]).toBe("setup-row");
      expect(Array.from(box.children).map((child) => child.className)).toEqual([
        "spotnav-suggestion-text",
        "spotnav-suggestion-answers",
      ]);
    }
  });

  it("lists each row with its button, and Not needed only where it may be dismissed", async () => {
    const { element } = await open(dashboard(ALL));
    line(element)!.click();
    await settle();
    // Only setup rows: the dialog itself is "Get started", with no heading inside repeating it.
    expect(title(element)).toBe("Get started");
    expect(section(element)!.querySelector("h3")).toBeNull();
    // Nothing else to review: no sentence about issues above it.
    expect(openDialog(element).querySelector(".spotnav-dialog-intro:not([hidden])")).toBeNull();
    const ids = Array.from(section(element)!.querySelectorAll<HTMLElement>("[data-setup]")).map((node) => node.dataset["setup"]);
    expect(ids).toEqual(["car_level", "site", "phone", "app"]);
    expect(button(row(element, "car_level"), "dismiss")).toBeNull();
    for (const id of ["site", "phone", "app"]) {
      expect(button(row(element, id), "dismiss")?.textContent).toBe("Not needed");
    }
    expect(row(element, "site").querySelector(".spotnav-suggestion-text")?.textContent).toBe(
      translate("en", "setup.site.text"),
    );
  });

  it("opens the place where each thing is done", async () => {
    const { hass, element } = await open(dashboard(ALL));
    line(element)!.click();
    await settle();
    button(row(element, "site"), "open")!.click();
    expect(pushed).toEqual([addIntegrationPath("spotnav")]);

    line(element)!.click();
    await settle();
    button(row(element, "app"), "open")!.click();
    expect(opened).toEqual([APP_STORE_URL]);

    // The car's level: the Plan popover reads the settings record first.
    const before = hass.messages.length;
    line(element)!.click();
    await settle();
    button(row(element, "car_level"), "open")!.click();
    await settle();
    expect(hass.messages.slice(before).some((message) => message["type"] === "spotnav/get_settings")).toBe(true);
  });

  it("opens the phones' own editor in Settings for the phone row", async () => {
    const { element } = await open(dashboard(ALL));
    line(element)!.click();
    await settle();
    button(row(element, "phone"), "open")!.click();
    await settle();
    expect(shadow(element).querySelector("[data-section='notifications']")).not.toBeNull();
    expect(openDialog(element).textContent).toContain(translate("en", "notifications.phones"));
  });

  it("sends the car, the forecast and phase switching rows where each is done", async () => {
    const rows: Row[] = [
      { id: "vehicle", dismissible: true },
      { id: "solar_forecast", dismissible: true },
      { id: "phase_switching", dismissible: true },
    ];
    const { element } = await open(dashboard(rows));
    expect(line(element)?.textContent).toBe("Get started: 3 things left");
    line(element)!.click();
    await settle();
    expect(row(element, "vehicle").querySelector(".spotnav-suggestion-text")?.textContent).toBe(
      "Add your car, so a target in percent and the range are right.",
    );
    // No forecast integration yet (the fixture's charger has no site to choose one on): adding Forecast.Solar comes first.
    expect(button(row(element, "solar_forecast"), "open")?.textContent).toBe("Add Forecast.Solar");
    button(row(element, "vehicle"), "open")!.click();
    expect(pushed).toEqual([INTEGRATIONS_PATH]);
    line(element)!.click();
    await settle();
    button(row(element, "solar_forecast"), "open")!.click();
    expect(pushed.at(-1)).toBe(addIntegrationPath("forecast_solar"));
    line(element)!.click();
    await settle();
    button(row(element, "phase_switching"), "open")!.click();
    await settle();
    expect(shadow(element).querySelector("[data-section='entities']")).not.toBeNull();
    for (const id of ["vehicle", "solar_forecast", "phase_switching"]) {
      expect(setupRowsOf({ items: rows }).find((entry) => entry.id === id)).toBeDefined();
    }
  });

  it("sends the demand charge's row to the site's measurement, with no Not needed", async () => {
    const rows: Row[] = [{ id: "demand_grid_power", dismissible: false }];
    const { element } = await open(dashboard(rows, "cheapest_direct_site_admin.json"));
    line(element)!.click();
    await settle();
    expect(row(element, "demand_grid_power").querySelector(".spotnav-suggestion-text")?.textContent).toBe(
      translate("en", "setup.demandGridPower.text"),
    );
    expect(button(row(element, "demand_grid_power"), "dismiss")).toBeNull();
    button(row(element, "demand_grid_power"), "open")!.click();
    await settle();
    expect(shadow(element).querySelector("[data-section='site']")).not.toBeNull();
  });

  it("asks the integration to keep a row away, then reads the dashboard again", async () => {
    const { hass, element } = await open(dashboard(ALL.slice(1)));
    line(element)!.click();
    await settle();
    button(row(element, "app"), "dismiss")!.click();
    await settle();
    expect(hass.messages.at(-1)).toMatchObject({ type: "spotnav/dismiss_setup_item", item: "app" });
    hass.resolveNext({ ok: true });
    await settle();
    expect(hass.messages.at(-1)).toMatchObject({ type: "spotnav/get_dashboard" });
    hass.resolveNext(dashboard(ALL.slice(1, 3)));
    await settle();
    expect(line(element)?.textContent).toBe("Get started: 2 things left");
  });
});

describe("the grid sign rows", () => {
  type WriteRow = { id: string; dismissible: boolean; field: string; value: boolean };
  const POWER: WriteRow = { id: "grid_power_sign", dismissible: false, field: "grid_power_inverted", value: false };
  const CURRENT: WriteRow = { id: "grid_current_sign", dismissible: false, field: "site_current_signed", value: false };
  const withRows = (rows: object[]): Record<string, any> => {
    const payload = dashboard(null, "cheapest_direct_site_admin.json");
    payload["setup"] = { items: rows };
    return payload;
  };

  it("are read with the option their button writes, and a row that names half of it is refused", () => {
    const decoded = decodeDashboard(withRows([POWER]));
    const setup = decoded.ok ? decoded.value.setup : null;
    expect(setup).toEqual({ items: [{ id: "grid_power_sign", dismissible: false, write: { field: "grid_power_inverted", value: false } }] });
    expect(setupRowsOf(setup)).toEqual([{ id: "grid_power_sign", dismissible: false, write: { field: "grid_power_inverted", value: false } }]);
    for (const half of [{ id: "grid_power_sign", dismissible: false, field: "grid_power_inverted" }, { ...POWER, value: "no" }]) {
      const refused = decodeDashboard(withRows([half]));
      expect(refused.ok && refused.value.setup).toBeNull();
    }
    // Without the option to write the card has no button for it: the row is left out.
    expect(setupRowsOf({ items: [{ id: "grid_power_sign", dismissible: false }] })).toEqual([]);
  });

  it("say what the steps showed, with no Not needed, and write only that option when pressed", async () => {
    const { hass, element } = await open(withRows([POWER]));
    line(element)!.click();
    await settle();
    expect(row(element, "grid_power_sign").querySelector(".spotnav-suggestion-text")?.textContent).toBe(
      "The grid meter's sign looks reversed: when the charger's draw changed, the grid moved the other way. " +
        "Solar and load balancing read export as import.",
    );
    expect(button(row(element, "grid_power_sign"), "dismiss")).toBeNull();
    expect(button(row(element, "grid_power_sign"), "open")).toBeNull();
    const write = button(row(element, "grid_power_sign"), "write")!;
    expect(write.textContent).toBe("Turn off “The sensor shows export as positive”");
    // Nothing is written by showing it.
    expect(hass.entityMessages).toEqual([]);
    hass.entityHandler = async () => ({ api_version: 1, ok: true, error: null, field_errors: [], config: null });
    const reads = hass.messages.length;
    write.click();
    await settle();
    expect(hass.entityMessages).toEqual([
      {
        type: "spotnav/update_entity_config",
        api_version: 1,
        charger_id: "entry_a",
        scope: "site",
        expected: { grid_power_inverted: true },
        changes: { grid_power_inverted: false },
      },
    ]);
    expect(hass.messages.slice(reads).map((message) => message["type"])).toEqual(["spotnav/get_dashboard"]);
  });

  it("offer to turn the option on when the steps say it should be on, in Swedish too", async () => {
    const { element } = await open(withRows([{ ...POWER, value: true }]), { language: "sv" });
    line(element)!.click();
    await settle();
    expect(button(row(element, "grid_power_sign"), "write")?.textContent).toBe("Slå på ”Sensorn visar export som positiv”");
  });

  it("turn Grid current is signed off for the phase currents", async () => {
    const { hass, element } = await open(withRows([CURRENT]));
    line(element)!.click();
    await settle();
    const write = button(row(element, "grid_current_sign"), "write")!;
    expect(write.textContent).toBe("Turn off “Grid current is signed”");
    write.click();
    await settle();
    expect(hass.entityMessages.at(-1)).toMatchObject({
      type: "spotnav/update_entity_config",
      scope: "site",
      expected: { site_current_signed: true },
      changes: { site_current_signed: false },
    });
  });
});

describe("the words", () => {
  it("are in every language", () => {
    for (const language of LANGUAGES) {
      const words = TRANSLATIONS[language] as Record<string, string>;
      for (const key of [
        "setup.line.one",
        "setup.line.other",
        "setup.heading",
        "setup.dismiss",
        "setup.app.text",
        "setup.vehicle.text",
        "setup.solarForecast.text",
        "setup.solarForecast.add",
        "setup.phaseSwitching.text",
        "setup.phone.text",
        "setup.gridPowerSign.text",
        "setup.gridPowerSign.turnOff",
        "setup.gridPowerSign.turnOn",
        "setup.gridCurrentSign.text",
        "setup.gridCurrentSign.turnOff",
      ]) {
        expect(words[key], `${language} ${key}`).toBeTruthy();
      }
      expect(words["setup.line.other"]).toContain("{count}");
    }
  });
});
