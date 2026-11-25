// The shared settings popover: the strict decoders on the backend's own fixtures (`tests/fixtures/site_view/`,
// `tests/fixtures/battery_control/`), the battery section's words, and the popover opened from the house card's
// gear: Get started without a charger, the battery switch and the AI-mode explanation, and heat as one line until
// something is found.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { registerTranslation, translate } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { batteryLine, batteryNeed, brandName, batteryOutsideClearable, outcomeVerdict, preheatLine } from "../src/settings-popover";
import { decodeBatteryControl, decodeSiteList, decodeSiteView } from "../src/site-model";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

describe("decoding the site's answers", () => {
  it("reads the site views and the battery statuses, and refuses an unknown key", () => {
    for (const name of ["villa.json", "no_charger.json", "with_price.json"]) {
      expect(decodeSiteView(read("site_view", name)).ok).toBe(true);
    }
    const priced = read("site_view", "with_price.json");
    const settings = priced["price_settings"] as Record<string, unknown>;
    const tariff = settings["grid_tariff"] as Record<string, unknown>;
    expect(decodeSiteView({ ...priced, price_settings: { ...settings, surprise: 1 } }).ok).toBe(false);
    expect(decodeSiteView({ ...priced, price_settings: { ...settings, grid_tariff: { ...tariff, surprise: 1 } } }).ok).toBe(false);
    expect(decodeSiteView({ ...priced, site: { ...(priced["site"] as object), surprise: 1 } }).ok).toBe(false);
    expect(decodeSiteView({ ...priced, price: null }).ok).toBe(false);
    for (const name of ["needs_ack.json", "checking.json", "needs_enable.json"]) {
      expect(decodeBatteryControl(read("battery_control", name)).ok).toBe(true);
    }
    expect(decodeBatteryControl(read("battery_control", "live.json")).ok).toBe(true);
    expect(decodeSiteView({ ...read("site_view", "villa.json"), extra: 1 })).toEqual({ ok: false, failure: "malformed" });
    expect(decodeSiteView({ ...read("site_view", "villa.json"), api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    const status = read("battery_control", "checking.json");
    expect(decodeBatteryControl({ ...status, status: { ...(status["status"] as object), surprise: true } }).ok).toBe(false);
    expect(decodeSiteList({ api_version: 1, sites: [{ site_id: "s", name: "Hemma", charger_ids: [] }] }).ok).toBe(true);
    const enable = read("battery_control", "needs_enable.json");
    const found = (enable["status"] as Record<string, unknown>)["enable_entities"] as Record<string, unknown>;
    const withFound = (value: unknown) => ({ ...enable, status: { ...(enable["status"] as object), enable_entities: value } });
    expect(decodeBatteryControl(withFound({ ...found, surprise: 1 })).ok).toBe(false);
    expect(decodeBatteryControl(withFound({ ...found, entities: [{ entity_id: "number.x" }] })).ok).toBe(false);
    const withFacts = (value: unknown) => ({ ...enable, status: { ...(enable["status"] as object), enable_facts: value } });
    expect(decodeBatteryControl(withFacts({ integration: "SolaX", entities: [], needs: ["size"] })).ok).toBe(true);
    expect(decodeBatteryControl(withFacts({ integration: "SolaX", entities: [], needs: ["size"], surprise: 1 })).ok).toBe(false);
    const before = decodeBatteryControl(enable);
    expect(before.ok && before.value.status?.enable_facts).toBeNull();
  });

  it("reads the preheating line, refuses an unknown key in it, and reads a Home Assistant before it as none", () => {
    const live = read("battery_control", "live.json");
    const withPreheat = (value: unknown) => ({ ...live, status: { ...(live["status"] as object), preheat: value } });
    const decoded = decodeBatteryControl(withPreheat(PREHEAT));
    expect(decoded.ok && decoded.value.status?.preheat).toEqual(PREHEAT);
    expect(decodeBatteryControl(withPreheat({ ...PREHEAT, surprise: 1 })).ok).toBe(false);
    const before = { ...live, status: { ...(live["status"] as Record<string, unknown>) } };
    delete (before.status as Record<string, unknown>)["preheat"];
    const old = decodeBatteryControl(before);
    expect(old.ok && old.value.status?.preheat).toBeNull();
  });
});

const SIZE_FACT = {
  integration: "SolaX",
  entities: [{ entity_id: "sensor.solax_battery_install_capacity", name: "Battery Install Capacity" }],
  needs: ["size"],
};

const PREHEAT = { code: "off", cell_c: 2.4, entity_id: "switch.sigen_plant_ess_preheating_enable", device_id: "0f4c2b7e9a1d4c3e8b6a5f2d1c0e9b8a" };

describe("the preheating line", () => {
  it("says the cells are cold and where to turn preheating on, or that the switch must be enabled first", () => {
    expect(preheatLine("sv", PREHEAT)).toBe(
      "Batteriets celler är kalla (2,4 °C) och förvärmningen är avstängd, så batteriet tar emot mindre laddning under de billiga timmarna. Slå på ”ESS Preheating Enable” på batteriets enhet i Home Assistant.",
    );
    expect(preheatLine("en", { ...PREHEAT, code: "disabled" }, "Plant ESS Preheating")).toBe(
      "The battery's cells are cold (2.4 °C). Its preheating switch “Plant ESS Preheating” is disabled in Home Assistant, so SpotNav cannot see whether it is on. Enable it on the battery's device.",
    );
    expect(preheatLine("sv", { ...PREHEAT, code: "unavailable" })).toBeNull();
    expect(preheatLine("sv", null)).toBeNull();
  });

  it("has both lines in every language, with the temperature and the switch's name", () => {
    for (const language of Object.keys(TRANSLATIONS) as (keyof typeof TRANSLATIONS)[]) {
      for (const key of ["battery.preheat.off", "battery.preheat.disabled"] as const) {
        expect(TRANSLATIONS[language][key]).toContain("{temp} °C");
        expect(TRANSLATIONS[language][key]).toContain("{name}");
      }
    }
  });
});

describe("the battery's words", () => {
  it("words each line, need and outcome", () => {
    expect(batteryLine("sv", "shadow_only")).toBe("Test: SpotNav styr inget än och loggar bara vad den skulle göra.");
    expect(batteryLine("sv", "waiting:car_charging")).toBe(
      "Väntar på ett lämpligt tillfälle för kontrollen. Den väntar tills bilen slutar ladda.",
    );
    expect(batteryLine("sv", "waiting:start_wait")).toBe(
      "Väntar på ett lämpligt tillfälle för kontrollen. Väntar några minuter efter start innan SpotNav styr batteriet.",
    );
    expect(batteryLine("sv", "failed:hold:ignored")).toBe(
      "Kontrollen misslyckades: ”Hålla” fungerade inte. Slå av och på igen för att kontrollera på nytt.",
    );
    expect(batteryLine("sv", "blocked:daily_write_cap")).toBe("Stoppad för i dag: dagens skrivningar är förbrukade.");
    expect(batteryNeed("sv", "control_disabled:remote_ems")).toBe(
      "Aktivera batteriets styrning Remote EMS i Home Assistant (den är avstängd från början).",
    );
    expect(batteryNeed("en", "needs_hacs_integration:owner/some-integration")).toBe(
      "This battery's control needs the HACS integration (owner/some-integration); Home Assistant's built-in integration lacks it.",
    );
    expect(batteryNeed("sv", "needs_hacs_integration:owner/some-integration")).toContain("HACS-integrationen (owner/some-integration)");
    expect(batteryNeed("en", "ai_takeover_ack")).toBe("Accept that SpotNav takes over from the battery's AI mode.");
    expect(batteryNeed("en", "strategy_ack")).toBe(
      "Accept that SpotNav's commands override the strategy the battery runs in its own cloud.",
    );
    expect(batteryNeed("en", "hardware_test_first")).toContain("until it has passed once on this battery");
    expect(batteryNeed("en", "hardware_test_again")).toContain("Run the hardware test again");
    expect(batteryNeed("sv", "hardware_test_again")).toContain("Kör hårdvarutestet igen");
    expect(batteryNeed("de", "recipe_blocked")).toContain("pausiert");
    const byHand = batteryNeed(
      "en", "restore_by_hand:select.sigen_mode=Maximum Self Consumption|number.sigen_charge_limit=20.6",
    );
    expect(byHand).toContain("select.sigen_mode → Maximum Self Consumption, number.sigen_charge_limit → 20.6");
    expect(batteryNeed("sv", "hardware_test_first")).toContain("Kör hårdvarutestet");
    expect(batteryNeed("sv", "inverter_power_zero")).toContain("\"Max inverter power\" är 0 i Victrons GX");
    expect(batteryNeed("en", "inverter_power_zero")).toContain("SpotNav cannot run a discharge");
    expect(batteryNeed("en", "grid_setpoint_selling")).toContain("far below 0");
    expect(batteryNeed("sv", "grid_setpoint_selling")).toContain("långt under 0");
    expect(["obeyed", "aborted:inverter_power_zero"].map(outcomeVerdict)).toEqual(["passed", "pending"]);
    expect(batteryLine("sv", "watching:strategy")).toBe("Styr inget än: godkänn att SpotNav går före batteriets egen strategi.");
    expect(["obeyed", "restored", "ignored", "aborted:no_effect", "aborted:fuse", "not_judgeable:nearly_full"].map(outcomeVerdict)).toEqual([
      "passed", "passed", "failed", "failed", "pending", "pending",
    ]);
  });
});

/** The heat answer with nothing heat-related found. */
function bareHeat(): Record<string, unknown> {
  const villa = read("heat_card", "villa.json");
  const config = { ...(villa["config"] as Record<string, unknown>), control_path: "none", indoor_sensors: [], zones: [], heat_meter_entity: null };
  const choices = { ...(villa["choices"] as Record<string, unknown>), zones: [], pool: null, spa: [], sauna: [] };
  return { ...villa, pump: null, meter: null, zones: [], config, choices };
}

describe("the popover from the house card", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
      customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
    }
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  interface Options {
    admin?: boolean;
    heat?: Record<string, unknown>;
    battery?: string;
    view?: Record<string, unknown>;
    entity?: Record<string, unknown>;
    preheat?: Record<string, unknown>;
    /** Merged into the battery status. */
    status?: Record<string, unknown>;
  }

  function fakeHass(options: Options = {}) {
    const messages: Record<string, unknown>[] = [];
    let battery = read("battery_control", options.battery ?? "needs_ack.json");
    if (options.preheat !== undefined) {
      battery = { ...battery, status: { ...(battery["status"] as object), preheat: options.preheat } };
    }
    if (options.status !== undefined) {
      battery = { ...battery, status: { ...(battery["status"] as object), ...options.status } };
    }
    const hass = {
      language: "sv",
      locale: { language: "sv" },
      user: { is_admin: options.admin ?? true },
      callWS: async (message: Record<string, unknown>): Promise<unknown> => {
        messages.push(message);
        switch (message["type"]) {
          case "spotnav/get_house":
            return read("house", "villa_battery_sun.json");
          case "spotnav/get_site_settings_view":
            return options.view ?? read("site_view", "villa.json");
          case "spotnav/get_heat":
            return options.heat ?? read("heat_card", "villa.json");
          case "spotnav/battery_control": {
            const changes = message["changes"] as Record<string, boolean> | undefined;
            if (message["clear_outside_control"] === true) {
              battery = { ...battery, status: { ...(battery["status"] as object), needs: [] } };
            }
            if (message["enable_fact_entities"] === true) {
              battery = { ...battery, status: { ...(battery["status"] as object), enable_facts: null } };
            }
            if (message["enable_control_entities"] === true) {
              battery = { ...battery, status: { ...(battery["status"] as object), enable_entities: null } };
            }
            if (message["tester_signup"] !== undefined) {
              const status = battery["status"] as Record<string, unknown>;
              const tester = { ...(status["tester"] as object), stage: "asked", sent_at: "2026-11-25T10:00:00+01:00" };
              battery = { ...battery, status: { ...status, tester } };
            }
            if (changes !== undefined) {
              const status = battery["status"] as Record<string, unknown>;
              battery = { ...battery, status: { ...status, options: { ...(status["options"] as object), ...changes } } };
            }
            return battery;
          }
          case "spotnav/get_entity_config":
            return options.entity ?? { api_version: 1, ok: false, error: "spotnav_unknown_site", field_errors: [], config: null };
          case "spotnav/update_site_settings":
            return { api_version: 1, ok: true, error: null, site: null, restore: null };
          case "spotnav/disconnect_phones":
            return { api_version: 1, ok: true, error: null, chargers: 0, sites: 1 };
          case "spotnav/beta":
            return JSON.parse(
              readFileSync(join(__dirname, "..", "..", "tests", "fixtures", "beta", "on_done_failed.json"), "utf-8"),
            ) as unknown;
          default:
            return {};
        }
      },
    };
    return { hass, messages };
  }

  async function mount(options: Options = {}) {
    const { hass, messages } = fakeHass(options);
    const element = document.createElement(HOUSE_CARD_ELEMENT) as SpotnavHouseCard;
    element.setConfig({ type: "custom:spotnav-house-card", site: "site_villa" });
    element.hass = hass as never;
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    return { element, messages, root: element.shadowRoot! };
  }

  async function open(root: ShadowRoot): Promise<HTMLElement> {
    root.querySelector<HTMLButtonElement>("button[data-action='settings']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    return root.querySelector<HTMLElement>("[data-popover='site']")!;
  }

  it("hides the import target on a site that cannot measure it while none is set, and keeps a set one to clear", async () => {
    const entity = read("entity_config", "v1", "get_direct.json");
    const unmeasured = { ...read("site_view", "villa.json"), import_target: { available: false, reason: "needs_total_grid_power" } };
    const hidden = await open((await mount({ view: unmeasured, entity })).root);
    const site = hidden.querySelector<HTMLElement>("[data-section='site']")!;
    expect(site.querySelector("[data-row='main_fuse_a']")).not.toBeNull();
    expect(site.querySelector("[data-row='import_target_kw']")).toBeNull();
    document.body.innerHTML = "";
    const config = entity["config"] as { fields: Record<string, unknown>[] };
    const set = {
      ...entity,
      config: { ...config, fields: config.fields.map((field) => (field["field"] === "import_target_kw" ? { ...field, value: 6 } : field)) },
    };
    const kept = await open((await mount({ view: unmeasured, entity: set })).root);
    expect(kept.querySelector("[data-section='site'] [data-row='import_target_kw']")).not.toBeNull();
    document.body.innerHTML = "";
    const measured = await open((await mount({ entity })).root);
    expect(measured.querySelector("[data-section='site'] [data-row='import_target_kw']")).not.toBeNull();
  });

  it("shows the priority list in the site's settings and saves a new order with what was shown", async () => {
    const priority = {
      order: [
        { id: "heat", name: null },
        { id: "charger:garage", name: "Garage" },
        { id: "battery", name: null },
      ],
      writable: true,
    };
    const view = { ...read("site_view", "villa.json"), priority };
    const { root, messages } = await mount({ view });
    const popover = await open(root);
    const row = popover.querySelector<HTMLElement>("[data-section='site'] [data-row='priority']")!;
    expect(row.textContent).toContain("Prioritet");
    expect(row.textContent).toContain("Husets värme först");
    expect(popover.querySelector("[data-section='site']")!.textContent).toContain("Överst får energi först och backar sist.");
    row.querySelector<HTMLButtonElement>("[data-edit='priority']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    const form = root.querySelector<HTMLFormElement>("[data-value-editor='priority']")!;
    const names = () => [...form.querySelectorAll<HTMLElement>("[data-priority-row]")].map((node) => node.dataset["priorityRow"]);
    expect(names()).toEqual(["heat", "charger:garage", "battery"]);
    expect(form.textContent).toContain("Garage");
    expect(form.textContent).toContain("Husbatteri");
    // The top row has no up, the bottom no down.
    expect(form.querySelector<HTMLButtonElement>("[data-priority-row='heat'] [data-move='up']")!.disabled).toBe(true);
    expect(form.querySelector<HTMLButtonElement>("[data-priority-row='battery'] [data-move='down']")!.disabled).toBe(true);
    form.querySelector<HTMLButtonElement>("[data-priority-row='battery'] [data-move='up']")!.click();
    expect(names()).toEqual(["heat", "battery", "charger:garage"]);
    root.querySelector<HTMLButtonElement>(`button[type='submit'][form='${form.id}']`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["type"] === "spotnav/update_site_settings")).toEqual([
      {
        type: "spotnav/update_site_settings",
        api_version: 1,
        site_id: "site_villa",
        expected: { priority: ["heat", "charger:garage", "battery"] },
        changes: { priority: ["heat", "battery", "charger:garage"] },
      },
    ]);
  });

  it("shows the priority list read-only where it cannot be written, and nothing from a Home Assistant before it", async () => {
    const priority = { order: [{ id: "heat", name: null }, { id: "pool_heat", name: null }], writable: false };
    const readOnly = await open((await mount({ view: { ...read("site_view", "villa.json"), priority } })).root);
    const row = readOnly.querySelector<HTMLElement>("[data-section='site'] [data-row='priority']")!;
    expect(row.querySelector("[data-edit='priority']")).toBeNull();
    document.body.innerHTML = "";
    const older = { ...read("site_view", "villa.json") };
    delete older["priority"];
    const before = await open((await mount({ view: older })).root);
    expect(before.querySelector("[data-row='priority']")).toBeNull();
  });

  it("names every row of the priority list in every language", () => {
    for (const language of Object.keys(TRANSLATIONS) as (keyof typeof TRANSLATIONS)[]) {
      for (const key of [
        "site.priority.title", "site.priority.intro", "site.priority.summary", "site.priority.up", "site.priority.down",
        "site.priority.row.heat", "site.priority.row.charger", "site.priority.row.spa", "site.priority.row.sauna",
        "site.priority.row.cooling", "site.priority.row.waterHeater", "site.priority.row.battery",
        "site.priority.row.spaIdle", "site.priority.row.poolCirculation", "site.priority.row.poolHeat",
      ] as const) {
        expect(TRANSLATIONS[language][key], `${language} ${key}`).toBeTruthy();
      }
    }
    expect(translate("sv", "site.priority.intro")).toBe("Överst får energi först och backar sist.");
  });

  function exportView(more: Record<string, unknown> = {}): Record<string, unknown> {
    const villa = read("site_view", "villa.json");
    return { ...villa, export_curtail: { ...(villa["export_curtail"] as object), ...more } };
  }

  it("shows No export at negative prices in Solar with what it cost, and an administrator turns it on", async () => {
    const { root, messages } = await mount({ view: exportView() });
    const popover = await open(root);
    const solar = popover.querySelector<HTMLElement>("[data-section='solar']")!;
    const row = solar.querySelector<HTMLElement>("[data-row='export_curtail_enabled']")!;
    expect(row.textContent).toContain("Ingen export vid negativa priser");
    expect(row.textContent).toContain("Av");
    expect(solar.querySelector("[data-help='export_curtail_enabled']")!.textContent).toContain("Gäller hela anläggningen.");
    // Off: what exporting at negative prices cost in the last 30 days, from the rule's own count.
    expect(solar.querySelector("[data-export-saving]")!.textContent).toBe(
      "De senaste 30 dagarna kostade export vid negativa priser 12 kr (41,2 kWh).",
    );
    expect(solar.querySelector("[data-help='export_curtail_beta']")!.textContent).toBe(
      "Testas fortfarande med Sigenergy: än så länge bara för betatestare.",
    );
    expect(solar.querySelector("[data-export-need]")).toBeNull();
    row.querySelector<HTMLButtonElement>("button[data-edit='export_curtail_enabled']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    const form = root.querySelector<HTMLFormElement>("form")!;
    expect(form.textContent).toContain("Om någon annan ändrar gränsen under tiden låter SpotNav den vara.");
    form.querySelector<HTMLInputElement>("input[value='on']")!.click();
    root.querySelector<HTMLButtonElement>(`button[type='submit'][form='${form.id}']`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["changes"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", changes: { export_curtail_enabled: true } },
    ]);
  });

  it("shows the export switch only where an export control is found, read-only for others", async () => {
    const none = await open((await mount({ view: exportView({ found: false, brand: null, state: "no_control" }) })).root);
    expect(none.querySelector("[data-section='solar']")).not.toBeNull();
    expect(none.querySelector("[data-row='export_curtail_enabled']")).toBeNull();
    document.body.innerHTML = "";
    // A Home Assistant before the switch sends no block at all.
    const older = { ...read("site_view", "villa.json") };
    delete older["export_curtail"];
    expect(decodeSiteView(older).ok).toBe(true);
    const before = await open((await mount({ view: older })).root);
    expect(before.querySelector("[data-row='export_curtail_enabled']")).toBeNull();
    document.body.innerHTML = "";
    // Written over the WebSocket by an administrator only: without `writable` the row is read.
    const read_only = await open((await mount({ view: exportView({ writable: false, enabled: true, state: "on" }) })).root);
    const row = read_only.querySelector<HTMLElement>("[data-row='export_curtail_enabled']")!;
    expect(row.textContent).toContain("På");
    expect(row.querySelector("button")).toBeNull();
    // On: no line about what it would have saved.
    expect(read_only.querySelector("[data-export-saving]")).toBeNull();
  });

  it("says the one thing the person can act on under the export switch", async () => {
    const needs: [string, string][] = [
      ["limit_unset", "Växelriktaren har ingen exportgräns inställd."],
      ["not_obeyed", "Växelriktaren följde inte gränsen"],
      ["not_taken", "Växelriktaren tog inte emot inställningen"],
      ["import_rose", "SpotNav försöker igen av sig självt i morgon."],
      ["needs_hacs_integration:mletenay/home-assistant-goodwe-inverter", "(mletenay/home-assistant-goodwe-inverter)"],
      ["beta_only", "Med Sigenergy fungerar det här än så länge bara för betatestare."],
      ["no_grid_reading", "elmätarens totala effekt mot nätet"],
    ];
    for (const [need, words] of needs) {
      const popover = await open((await mount({ view: exportView({ need, enabled: true, state: "on" }) })).root);
      const line = popover.querySelector<HTMLElement>("[data-export-need]")!;
      expect(line.dataset["exportNeed"], need).toBe(need.split(":")[0]);
      expect(line.textContent, need).toContain(words);
      expect(line.querySelector("button"), need).toBeNull();
      document.body.innerHTML = "";
    }
    // A disabled entity: the line opens it in Home Assistant, where it can be enabled.
    const entity_id = "number.sigen_plant_grid_export_limitation";
    const { root } = await mount({ view: exportView({ need: "control_disabled", entity_id }) });
    const popover = await open(root);
    const line = popover.querySelector<HTMLElement>("[data-export-need='control_disabled']")!;
    expect(line.textContent).toContain("Exportgränsen är avstängd i Home Assistant.");
    const opened: unknown[] = [];
    root.addEventListener("hass-more-info", (event) => opened.push((event as CustomEvent).detail));
    line.querySelector<HTMLButtonElement>("button[data-action='open-export-limit-entity']")!.click();
    expect(opened).toEqual([{ entityId: entity_id }]);
    document.body.innerHTML = "";
    // A code a newer Home Assistant sends is no line.
    const unknown = await open((await mount({ view: exportView({ need: "something_new" }) })).root);
    expect(unknown.querySelector("[data-export-need]")).toBeNull();
  });

  it("refuses an unknown key in the export block and names the switch in every language", () => {
    const villa = read("site_view", "villa.json");
    const block = villa["export_curtail"] as Record<string, unknown>;
    expect(decodeSiteView({ ...villa, export_curtail: { ...block, surprise: 1 } }).ok).toBe(false);
    expect(decodeSiteView({ ...villa, export_curtail: { ...block, saving: { ...(block["saving"] as object), surprise: 1 } } }).ok).toBe(false);
    expect(decodeSiteView({ ...villa, export_curtail: null }).ok).toBe(true);
    for (const language of Object.keys(TRANSLATIONS) as (keyof typeof TRANSLATIONS)[]) {
      for (const key of [
        "exportCurtail.title", "exportCurtail.help", "exportCurtail.intro", "exportCurtail.saving", "exportCurtail.beta",
        "exportCurtail.need.controlDisabled", "exportCurtail.need.limitUnset", "exportCurtail.need.notObeyed",
        "exportCurtail.need.notTaken", "exportCurtail.need.needsHacs", "exportCurtail.need.betaOnly",
        "exportCurtail.need.noGridReading", "exportCurtail.need.importRose",
      ] as const) {
        expect(TRANSLATIONS[language][key], `${language} ${key}`).toBeTruthy();
      }
      expect(TRANSLATIONS[language]["exportCurtail.saving"]).toContain("{amount}");
      expect(TRANSLATIONS[language]["exportCurtail.need.needsHacs"]).toContain("{repo}");
    }
    expect(translate("en", "exportCurtail.title")).toBe("No export at negative prices");
  });

  it("has no gear for a household member", async () => {
    const { root } = await mount({ admin: false });
    expect(root.querySelector("button[data-action='settings']")).toBeNull();
  });

  it("opens the site's sections, Get started first with adding a charger answerable as not needed", async () => {
    const { root, messages } = await mount();
    const popover = await open(root);
    expect([...popover.children].map((node) => (node as HTMLElement).dataset["section"])).toEqual([
      "setup", "price", "site", "solar", "battery", "heat", "pool", "spa", "sauna", "notifications", "support",
    ]);
    const charger = popover.querySelector<HTMLElement>("[data-setup='charger']")!;
    expect(charger.textContent).toContain("Lägg till laddbox");
    expect(charger.textContent).toContain("Behövs inte");
    expect(popover.querySelector("[data-setup='battery']")).not.toBeNull();
    expect(messages.find((message) => message["type"] === "spotnav/get_site_settings_view")).toEqual({
      type: "spotnav/get_site_settings_view", api_version: 1, site_id: "site_villa",
    });
    charger.querySelector<HTMLButtonElement>("button[data-setup-action='dismiss']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.find((message) => message["type"] === "spotnav/dismiss_setup_item")).toEqual({
      type: "spotnav/dismiss_setup_item", item: "charger",
    });
  });

  it("offers Disconnect all phones in Support, and disconnects only on the confirming press", async () => {
    const { root, messages } = await mount();
    const popover = await open(root);
    const support = popover.querySelector<HTMLElement>("[data-section='support']")!;
    support.querySelector<HTMLButtonElement>("[data-disconnect-phones='open']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    const form = root.querySelector<HTMLFormElement>("[data-value-editor='disconnect-phones']")!;
    expect(form.textContent).toContain(translate("sv", "phones.disconnect.text"));
    expect(messages.some((message) => message["type"] === "spotnav/disconnect_phones")).toBe(false);
    root.querySelector<HTMLButtonElement>(`button[type='submit'][form='${form.id}']`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["type"] === "spotnav/disconnect_phones")).toEqual([
      { type: "spotnav/disconnect_phones", api_version: 1 },
    ]);
  });

  it("goes back to the page from an editor by the cross, and closes from the page itself", async () => {
    const { root } = await mount();
    await open(root);
    root.querySelector<HTMLButtonElement>("[data-section='support'] [data-disconnect-phones='open']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-value-editor='disconnect-phones']")).not.toBeNull();
    root.querySelector<HTMLButtonElement>(".spotnav-dialog-close")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-value-editor='disconnect-phones']")).toBeNull();
    expect(root.querySelector("[data-popover='site']")).not.toBeNull();
    root.querySelector<HTMLButtonElement>(".spotnav-dialog-close")!.click();
    await vi.advanceTimersByTimeAsync(0);
    const overlay = root.querySelector<HTMLElement>("[data-popover='site']")!.closest<HTMLElement>(".spotnav-dialog-overlay")!;
    expect(overlay.hidden).toBe(true);
    // Settings is the wide dialog on a wide screen.
    expect(overlay.classList.contains("spotnav-settings-wide")).toBe(true);
  });

  it("offers Disconnect all phones to no household member", async () => {
    const { root } = await mount({ admin: false });
    expect(root.querySelector("[data-disconnect-phones='open']")).toBeNull();
  });

  it("lists the beta tester row in Support by the site, and opens its box", async () => {
    const { root, messages } = await mount();
    const popover = await open(root);
    await vi.advanceTimersByTimeAsync(0);
    const support = popover.querySelector<HTMLElement>("[data-section='support']")!;
    const value = support.querySelector<HTMLButtonElement>("[data-row='beta'] [data-edit='beta']")!;
    expect(messages.find((message) => message["type"] === "spotnav/beta")).toEqual({
      type: "spotnav/beta", api_version: 1, site_id: "site_villa", action: "get",
    });
    expect(value.textContent).toMatch(/^Betaperiod till /);
    value.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-beta='box']")).not.toBeNull();
    // The hardware test is its own row and dialog, not part of the beta box.
    expect(root.querySelector("[data-beta='test']")).toBeNull();
    root.querySelector<HTMLButtonElement>("[data-beta='close']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    const again = root.querySelector<HTMLElement>("[data-section='support']")!;
    const test = again.querySelector<HTMLElement>("[data-row='hardware_test']")!;
    expect(test.hidden).toBe(false);
    expect(test.querySelector("[data-edit='hardware_test']")?.textContent).toMatch(/^Problem · /);
    test.querySelector<HTMLButtonElement>("[data-edit='hardware_test']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-beta='box']")).toBeNull();
    expect(root.querySelectorAll("[data-beta='steps'] li")).toHaveLength(5);
  });

  it("draws Support as the charger card does: heading icon, out-of-date line, rows, then the actions as rows", async () => {
    const { root } = await mount();
    const popover = await open(root);
    await vi.advanceTimersByTimeAsync(0);
    const support = popover.querySelector<HTMLElement>("[data-section='support']")!;
    expect(support.querySelector("h4 svg")).not.toBeNull();
    expect(support.querySelector("[data-card-outdated]")).not.toBeNull();
    const order = [...support.children].map((child) =>
      (child as HTMLElement).dataset["row"] ?? (child.querySelector("button") !== null && child.tagName === "DIV" ? "buttons" : child.tagName.toLowerCase()),
    );
    expect(order).toEqual(["h4", "p", "automations", "beta", "hardware_test", "docs", "download_debug", "p", "send_debug", "disconnect_phones", "sponsor", "p"]);
  });

  it("shows the battery's line, checklist and report, and turns the switch off", async () => {
    const { root, messages } = await mount();
    const popover = await open(root);
    const battery = popover.querySelector<HTMLElement>("[data-section='battery']")!;
    expect(battery.querySelector("[data-help='battery_control_enabled']")!.textContent).toBe(
      "Styr inget än: batteriet ligger kvar i AI-läget tills du godkänner övertagandet.",
    );
    expect(battery.querySelector("[data-need='ai_takeover_ack']")!.textContent).toContain("Godkänn att SpotNav tar över");
    expect(battery.querySelector("[data-row='battery_mode_hold']")!.textContent).toBe("HållaGodkänd");
    expect(battery.querySelector("[data-row='battery_mode_discharge_house']")!.textContent).toBe("Täcka husetInte bedömd ännu");
    expect(battery.textContent).toContain("SpotNav får: hålla, ladda från nätet.");
    expect(battery.textContent).not.toContain("shadow_only");
    expect(battery.querySelector("[data-row='battery_control_allow_sell']")).toBeNull();
    expect(battery.textContent).not.toContain("Tillåt försäljning från batteriet");

    expect(battery.querySelector("[data-battery-mode-shadow]")).toBeNull();
    battery.querySelector<HTMLButtonElement>("button[data-edit='battery_control_enabled']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor]")!;
    form.querySelector<HTMLInputElement>("input[value='off']")!.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["changes"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", changes: { battery_control_enabled: false } },
    ]);
    expect(root.querySelector("[data-row='battery_control_enabled']")!.textContent).toContain("Av");
  });

  it("shows a cold battery's preheating line with the device's page for an administrator, and none without it", async () => {
    const { root } = await mount({ preheat: PREHEAT });
    const popover = await open(root);
    const note = popover.querySelector<HTMLElement>("[data-section='battery'] [data-note='preheat_off']")!;
    expect(note.getAttribute("role")).toBe("status");
    expect(note.textContent).toContain("Batteriets celler är kalla (2,4 °C)");
    const pushed: string[] = [];
    window.addEventListener("location-changed", () => pushed.push(window.location.pathname), { once: true });
    note.querySelector<HTMLButtonElement>("button[data-action='open-preheat-device']")!.click();
    expect(pushed).toEqual(["/config/devices/device/0f4c2b7e9a1d4c3e8b6a5f2d1c0e9b8a"]);
    document.body.innerHTML = "";
    const plain = await open((await mount()).root);
    expect(plain.querySelector("[data-note^='preheat_']")).toBeNull();
  });

  it("puts what is left in numbered steps: the disabled controls as one step with the device's page, then the takeover", async () => {
    const { root } = await mount({ battery: "live.json" });
    const popover = await open(root);
    const box = popover.querySelector<HTMLElement>("[data-section='battery'] [data-battery-needs]")!;
    expect(box.querySelector(".spotnav-battery-needs-heading")!.textContent).toBe("Två saker kvar innan SpotNav kan styra batteriet");
    const steps = [...box.querySelectorAll<HTMLElement>("ol > li")];
    expect(steps.map((step) => step.dataset["need"])).toEqual(["control_disabled", "ai_takeover_ack"]);
    expect(steps[0]!.textContent).toContain(
      "Slå på tre avstängda inställningar på batteriets enhet i Home Assistant: Remote EMS, Control mode och Charge limit.",
    );
    const pushed: string[] = [];
    window.addEventListener("location-changed", () => pushed.push(window.location.pathname), { once: true });
    steps[0]!.querySelector<HTMLButtonElement>("button[data-action='open-battery-device']")!.click();
    expect(pushed).toEqual(["/config/devices/device/0f4c2b7e9a1d4c3e8b6a5f2d1c0e9b8a"]);
    expect(steps[1]!.querySelector("button[data-edit='battery_control_ai_takeover_ack']")).not.toBeNull();
  });

  it("says the one step the plant's per-phase import limit still needs, with its button for an administrator", async () => {
    const checking = read("battery_control", "checking.json")["status"] as Record<string, any>;
    const withPhase = (phase: Record<string, unknown>, more: Record<string, unknown> = {}): Record<string, unknown> => ({
      allowed_actions: ["grid_charge"],
      options: { ...checking["options"], battery_control_enabled: true },
      grid_charge_cap: {
        ...checking["grid_charge_cap"],
        import_limit: { ...checking["grid_charge_cap"]["import_limit"], per_phase: { max_kw: 13.16, set_kw: 13.1, holds: false, entity_id: null, ...phase } },
      },
      ...more,
    });
    // Not said to act, the limit (13.8 kW) higher than guards the fuse (13.16): one tap sets 13.1 kW and says it acts.
    const high = await mount({ battery: "checking.json", status: withPhase({ why: "not_said_to_act" }) });
    let popover = await open(high.root);
    let note = popover.querySelector<HTMLElement>("[data-note='import_limit_phase']")!;
    expect(note.dataset["code"]).toBe("limit");
    expect(note.textContent).toContain("men den står på 13,8 kW. Med högst 13,1 kW skyddar den huvudsäkringen");
    // The total rule's advice (far under the fuse) is not given beside it.
    expect(popover.querySelector("[data-import-limit]")).toBeNull();
    const setButton = note.querySelector<HTMLButtonElement>("button[data-action='import-limit-set']")!;
    expect(setButton.textContent).toBe("Ställ in 13,1 kW…");
    setButton.click();
    let form = high.root.querySelector<HTMLFormElement>("form[data-battery-import-limit='set']")!;
    expect(form.textContent).toContain("SpotNav sätter Sigenergys importgräns till 13,1 kW");
    expect(form.textContent).toContain("SpotNav höjer aldrig gränsen.");
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(high.messages.filter((message) => message["set_import_limit"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", set_import_limit: true },
    ]);
    high.element.remove();
    // Control only watches: nothing is written to the plant, so no button: the limit is set in the app.
    const watching = withPhase({ why: "not_said_to_act" }) as Record<string, any>;
    watching["options"]["battery_control_shadow_only"] = true;
    const shadow = await mount({ battery: "checking.json", status: watching });
    popover = await open(shadow.root);
    note = popover.querySelector<HTMLElement>("[data-note='import_limit_phase']")!;
    expect(note.querySelector("button")).toBeNull();
    expect(note.textContent).toContain("Ställ in den i Sigenergy-appen.");
    shadow.element.remove();
    // A limit that reads nothing is never written over: no button either.
    const unread = withPhase({ why: "not_said_to_act", set_kw: null }) as Record<string, any>;
    unread["grid_charge_cap"]["import_limit"]["value_kw"] = null;
    const blank = await mount({ battery: "checking.json", status: unread });
    popover = await open(blank.root);
    note = popover.querySelector<HTMLElement>("[data-note='import_limit_phase']")!;
    expect(note.dataset["code"]).toBe("limit_unread");
    expect(note.querySelector("button")).toBeNull();
    blank.element.remove();
    // Not said to act, the limit already low enough: the tap only says it acts (same command, nothing to lower).
    const low = withPhase({ why: "not_said_to_act", set_kw: null }) as Record<string, any>;
    low["grid_charge_cap"]["import_limit"]["value_kw"] = 12.0;
    const said = await mount({ battery: "checking.json", status: low });
    popover = await open(said.root);
    note = popover.querySelector<HTMLElement>("[data-note='import_limit_phase']")!;
    expect(note.dataset["code"]).toBe("not_said_to_act");
    expect(note.textContent).toContain("och den står redan lågt nog för din huvudsäkring");
    note.querySelector<HTMLButtonElement>("button[data-action='import-limit-acts']")!.click();
    form = said.root.querySelector<HTMLFormElement>("form[data-battery-import-limit='acts']")!;
    expect(form.textContent).toMatch(/importgräns \(12(,0)? kW\)/);
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(said.messages.filter((message) => message["set_import_limit"] !== undefined)).toHaveLength(1);
    said.element.remove();
    // The phase switch off: Home Assistant's own dialog for the entity.
    const entityId = "switch.sigen_plant_independent_phase_power_control";
    const off = await mount({ battery: "checking.json", status: withPhase({ why: "phase_control_off", entity_id: entityId }) });
    popover = await open(off.root);
    note = popover.querySelector<HTMLElement>("[data-note='import_limit_phase']")!;
    expect(note.textContent).toContain("Oberoende effektstyrning per fas (Independent phase power control) är av");
    const opened: unknown[] = [];
    off.element.addEventListener("hass-more-info", (event) => opened.push((event as CustomEvent).detail));
    note.querySelector<HTMLButtonElement>("button[data-action='open-import-limit-entity']")!.click();
    expect(opened).toEqual([{ entityId }]);
    off.element.remove();
    // A code nobody here can act on: nothing.
    const tested = await mount({ battery: "checking.json", status: withPhase({ why: "not_field_tested" }) });
    popover = await open(tested.root);
    expect(popover.querySelector("[data-note='import_limit_phase']")).toBeNull();
  });

  it("offers a battery brand still being tested a tester row for an administrator, and sends the sign-up", async () => {
    const tester = { stage: "none", brand: "foxess", sent_at: null, result_sent_at: null, test_passed: false, code_entered_at: null };
    const status = { tester, needs: ["tester_signup"] };
    // Not for a brand that takes no code.
    const plain = await mount({ battery: "checking.json" });
    expect((await open(plain.root)).querySelector("[data-row='battery_tester']")).toBeNull();
    plain.element.remove();
    const { root, messages, element } = await mount({ battery: "checking.json", status });
    const popover = await open(root);
    const battery = popover.querySelector<HTMLElement>("[data-section='battery']")!;
    expect(battery.querySelector("[data-row='battery_tester']")!.textContent).toBe("TestareBli testare");
    expect(battery.querySelector("[data-help='battery_tester']")!.textContent).toBe(
      "Styrningen av FoxESS är inte påslagen än. SpotNav slår bara på styrning av ett batterimärke när den har provats under ordnade former på minst en riktig anläggning. Du kan vara den som gör det möjligt: som testare kör du ett kort hårdvarutest som sköter sig självt, och hjälper därmed alla andra med FoxESS att få styrning. Till dess läser SpotNav batteriet och planerar bara vad den skulle ha gjort, i skuggläge.",
    );
    expect(battery.textContent).toContain(translate("sv", "battery.need.testerSignup"));
    battery.querySelector<HTMLButtonElement>("button[data-edit='battery_tester']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-battery-tester]")!;
    expect(form.textContent).toContain("Styrningen av FoxESS-batterier testas tillsammans med användare");
    const email = form.querySelector<HTMLInputElement>("input[data-field='tester_email']")!;
    const note = form.querySelector<HTMLTextAreaElement>("textarea[data-field='tester_note']")!;
    const consent = form.querySelector<HTMLInputElement>("input[data-field='tester_consent']")!;
    // The address is required, and the consent.
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    // The dialog's footer holds the error line and the buttons: read the whole card.
    expect(root.textContent).toContain(translate("sv", "sendDebug.error.email"));
    email.value = "tester@example.com";
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.textContent).toContain(translate("sv", "sendDebug.error.consent"));
    expect(messages.filter((message) => message["tester_signup"] !== undefined)).toHaveLength(0);
    note.value = " H3-10, tre faser ";
    consent.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["tester_signup"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa",
        tester_signup: { email: "tester@example.com", note: "H3-10, tre faser" } },
    ]);
    const after = root.querySelector<HTMLElement>("[data-popover='site'] [data-section='battery']")!;
    expect(after.querySelector("[data-row='battery_tester']")!.textContent).toBe("TestareAnmäld");
    expect(after.querySelector("[data-help='battery_tester']")!.textContent).toContain("Kör hårdvarutestet under Support → Hårdvarutest");
    element.remove();
  });

  it("offers the Home Assistant down acceptance to an administrator only where it would change something, and writes it", async () => {
    const checking = read("battery_control", "checking.json")["status"] as Record<string, any>;
    const withHaDown = (haDown: Record<string, unknown> | null, more: Record<string, unknown> = {}): Record<string, unknown> => ({
      options: { ...checking["options"], battery_control_enabled: true, ...more },
      grid_charge_cap: { ...checking["grid_charge_cap"], ha_down: haDown },
    });
    // Not binding (or a Home Assistant without the block): no line at all.
    for (const block of [{ offered: false, held_kw: 9.4, empty_house_kw: 13.16 }, null]) {
      const quiet = await mount({ battery: "checking.json", status: withHaDown(block) });
      const popover = await open(quiet.root);
      expect(popover.querySelector("[data-row='battery_control_accept_ha_down']")).toBeNull();
      expect(popover.querySelector("[data-ha-down]")).toBeNull();
      quiet.element.remove();
    }
    // Binding: the row, its short line, and the editor with the explanation and the checkbox.
    const offered = await mount({ battery: "checking.json", status: withHaDown({ offered: true, held_kw: 0.4, empty_house_kw: 13.16 }) });
    let popover = await open(offered.root);
    const battery = popover.querySelector<HTMLElement>("[data-section='battery']")!;
    expect(battery.querySelector("[data-row='battery_control_accept_ha_down']")!.textContent).toBe("Om Home Assistant stannarInte godkänt");
    expect(battery.querySelector("[data-help='battery_control_accept_ha_down']")!.textContent).toContain(
      "Nätladdningen av husbatteriet hålls till högst 0,4 kW",
    );
    battery.querySelector<HTMLButtonElement>("button[data-edit='battery_control_accept_ha_down']")!.click();
    const form = offered.root.querySelector<HTMLFormElement>("form[data-battery-ha-down]")!;
    expect(form.textContent).toContain("batteriet fortsätter ladda med den effekt det fick sist");
    expect(form.querySelectorAll("li")).toHaveLength(3);
    expect(form.textContent).toContain(
      "Jag förstår och accepterar att en säkring kan lösa ut om Home Assistant stannar eller tappar kontakten med batteriet eller elmätaren under en nätladdning av husbatteriet.",
    );
    const box = form.querySelector<HTMLInputElement>("input[type='checkbox'][data-field='accept_ha_down']")!;
    expect(box.checked).toBe(false);
    box.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(offered.messages.filter((message) => message["changes"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", changes: { battery_control_accept_ha_down: true } },
    ]);
    offered.element.remove();
    // Accepted: the row says so and who can turn it off, and stays to turn it off.
    const accepted = await mount({
      battery: "checking.json",
      status: withHaDown({ offered: true, held_kw: 0.4, empty_house_kw: 13.16 }, { battery_control_accept_ha_down: true }),
    });
    popover = await open(accepted.root);
    expect(popover.querySelector("[data-row='battery_control_accept_ha_down']")!.textContent).toBe("Om Home Assistant stannarGodkänt");
    expect(popover.querySelector("[data-help='battery_control_accept_ha_down']")!.textContent).toBe(
      "Godkänt att en säkring kan lösa ut om Home Assistant stannar eller tappar kontakten med batteriet eller elmätaren under en nätladdning av husbatteriet. Bara en administratör kan ändra det.",
    );
  });

  const SEK_COSTS = {
    wear_minor_per_kwh: 15,
    wear_default_minor_per_kwh: 15,
    balanced_margin_minor_per_kwh: 100,
    major_unit: "kr",
    minor_unit: "öre",
  };

  it("shows only the mode's row on the page; its dialog has the help, the wear, the margin, the link and the shadow note, and writes the choice", async () => {
    const { root, messages } = await mount({ battery: "checking.json", status: { plan_costs: SEK_COSTS } });
    const popover = await open(root);
    const battery = popover.querySelector<HTMLElement>("[data-section='battery']")!;
    const row = battery.querySelector("[data-row='battery_mode']")!;
    expect(row.textContent).toBe("LägeBalanserad");
    expect(battery.querySelector("[data-help='battery_mode']")).toBeNull();
    expect(battery.querySelector("[data-battery-mode-shadow]")).toBeNull();
    expect(battery.querySelector("[data-plan-costs]")).toBeNull();
    expect(battery.textContent).not.toContain("Slitage");
    battery.querySelector<HTMLButtonElement>("button[data-edit='battery_mode']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor='battery-mode']")!;
    expect(
      [...form.querySelectorAll<HTMLInputElement>("input[type='radio']")].map((radio) => [radio.value, radio.checked]),
    ).toEqual([["self_consumption", false], ["balanced", true], ["max_profit", false]]);
    expect(form.textContent).toContain("Batteriet laddas av solen och täcker huset. Ingen handel med nätet.");
    expect(form.textContent).toContain("Som Balanserad, men säljer till nätet så fort det lönar sig, med slitaget inräknat.");
    expect(form.textContent).toContain("Slitage (öre/kWh åt varje håll)");
    expect(form.textContent).toContain("Vad det kostar i slitage att ladda och ladda ur en kWh. Standard 15 öre.");
    const wear = form.querySelector<HTMLInputElement>("input[data-field='battery_wear']")!;
    expect(wear.value).toBe("");
    expect(form.querySelector("[data-plan-costs]")!.textContent).toBe("Balanserad säljer vid minst 1 kr/kWh netto Batteriplanen");
    const link = form.querySelector<HTMLAnchorElement>("[data-plan-costs] a")!;
    expect(link.target).toBe("_blank");
    expect(link.href).toContain("battery-plan");
    expect(form.querySelector("[data-battery-mode-shadow]")!.textContent).toBe(
      "Medan SpotNav inte styr batteriet flyttar valet bara vilken rad som är markerad som vald i jämförelsen.",
    );
    form.querySelector<HTMLInputElement>("input[value='max_profit']")!.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["changes"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", changes: { battery_mode: "max_profit" } },
    ]);
    expect(root.querySelector("[data-row='battery_mode']")!.textContent).toContain("Max vinst");
  });

  it("writes the mode and the wear together, only what changed, and Default writes null", async () => {
    const { root, messages } = await mount({ battery: "checking.json", status: { plan_costs: SEK_COSTS } });
    let popover = await open(root);
    popover.querySelector<HTMLButtonElement>("button[data-edit='battery_mode']")!.click();
    let form = root.querySelector<HTMLFormElement>("form[data-value-editor='battery-mode']")!;
    form.querySelector<HTMLInputElement>("input[value='self_consumption']")!.click();
    form.querySelector<HTMLInputElement>("input[data-field='battery_wear']")!.value = "22,5";
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    const written = (): unknown[] => messages.filter((message) => message["changes"] !== undefined).map((message) => message["changes"]);
    expect(written()).toEqual([{ battery_mode: "self_consumption", battery_wear_minor_per_kwh: 22.5 }]);
    popover = root.querySelector<HTMLElement>("[data-popover='site']")!;
    popover.querySelector<HTMLButtonElement>("button[data-edit='battery_mode']")!.click();
    form = root.querySelector<HTMLFormElement>("form[data-value-editor='battery-mode']")!;
    const wear = form.querySelector<HTMLInputElement>("input[data-field='battery_wear']")!;
    expect(wear.value).toBe("22,5");
    // Too much: refused in the dialog, nothing written.
    wear.value = "151";
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[role='alert']")!.textContent).toBe("Mellan 0 och 150.");
    expect(written()).toHaveLength(1);
    form.querySelector<HTMLButtonElement>("button[data-wear-default]")!.click();
    expect(wear.value).toBe("");
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(written()).toEqual([
      { battery_mode: "self_consumption", battery_wear_minor_per_kwh: 22.5 },
      { battery_wear_minor_per_kwh: null },
    ]);
  });

  it("names the wear in a euro area's cents with its default", async () => {
    const eur = { wear_minor_per_kwh: 1.4, wear_default_minor_per_kwh: 1.4, balanced_margin_minor_per_kwh: 10, major_unit: "EUR", minor_unit: "cent" };
    const { root } = await mount({ battery: "checking.json", status: { plan_costs: eur } });
    const popover = await open(root);
    popover.querySelector<HTMLButtonElement>("button[data-edit='battery_mode']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor='battery-mode']")!;
    expect(form.textContent).toContain("Slitage (cent/kWh åt varje håll)");
    expect(form.textContent).toContain("Standard 1,4 cent.");
    expect(form.textContent).not.toContain("öre/");
    expect(form.querySelector<HTMLInputElement>("input[data-field='battery_wear']")!.placeholder).toBe("1,4");
  });

  it("offers no wear field where Home Assistant cannot take one", async () => {
    const older = { ...SEK_COSTS } as Record<string, unknown>;
    delete older["wear_default_minor_per_kwh"];
    const { root } = await mount({ battery: "checking.json", status: { plan_costs: older } });
    const popover = await open(root);
    popover.querySelector<HTMLButtonElement>("button[data-edit='battery_mode']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor='battery-mode']")!;
    expect(form.querySelector("input[data-field='battery_wear']")).toBeNull();
    expect(form.querySelector("[data-plan-costs]")!.textContent).toContain("Balanserad säljer vid minst 1 kr/kWh netto");
  });

  it("has the mode's words and no sell row in every language", () => {
    for (const language of Object.keys(TRANSLATIONS) as Array<keyof typeof TRANSLATIONS>) {
      const words = TRANSLATIONS[language] as Record<string, string>;
      const mode = [
        "battery.modeLabel", "battery.modeShadow", "battery.modeHelp.self_consumption", "battery.modeHelp.balanced",
        "battery.modeHelp.max_profit", "battery.planMargin", "battery.wear.label", "battery.wear.help", "battery.wear.default",
      ].map((key) => words[key]);
      expect(mode.every((word) => typeof word === "string" && word.length > 3)).toBe(true);
      expect(new Set(mode).size).toBe(mode.length);
      expect(words["battery.sell"]).toBeUndefined();
      expect(words["battery.sellHelp"]).toBeUndefined();
    }
  });

  it("explains the AI-mode takeover in the owner's words and writes the acknowledgement", async () => {
    const { root, messages } = await mount();
    const popover = await open(root);
    popover.querySelector<HTMLButtonElement>("button[data-edit='battery_control_ai_takeover_ack']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-battery-ack]")!;
    expect(form.textContent).toContain(
      "SpotNav tar över styrningen av batteriet. När du stänger av hamnar batteriet i Maximal egenförbrukning — slå på AI-läget i Sigenergy-appen igen om du vill tillbaka.",
    );
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["changes"] !== undefined).map((message) => message["changes"])).toEqual([
      { battery_control_ai_takeover_ack: true },
    ]);
  });

  it("explains that SpotNav overrides a battery's own strategy it cannot read and writes the acknowledgement", async () => {
    const { root, messages } = await mount({ battery: "needs_strategy_ack.json" });
    const popover = await open(root);
    const battery = popover.querySelector<HTMLElement>("[data-section='battery']")!;
    expect(battery.querySelector("[data-help='battery_control_enabled']")!.textContent).toBe(
      "Styr inget än: godkänn att SpotNav går före batteriets egen strategi.",
    );
    expect(battery.querySelector("[data-need='strategy_ack']")!.textContent).toContain("Godkänn att SpotNavs kommandon");
    battery.querySelector<HTMLButtonElement>("button[data-edit='battery_control_ai_takeover_ack']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-battery-ack]")!;
    expect(form.textContent).toContain("SpotNav kan inte se vilken strategi batteriet kör i sitt eget moln");
    expect(form.textContent).not.toContain("Sigenergy");
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["changes"] !== undefined).map((message) => message["changes"])).toEqual([
      { battery_control_ai_takeover_ack: true },
    ]);
  });

  it("asks before it turns on the brand's disabled control entities, with the list folded, and turns on nothing before the confirm", async () => {
    const { root, messages } = await mount({ battery: "needs_enable.json" });
    const popover = await open(root);
    popover.querySelector<HTMLButtonElement>("button[data-edit='battery_control_enabled']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-battery-enable]")!;
    expect(form.textContent).toContain("SpotNav behöver slå på 3 entiteter i Sigenergy.");
    const folded = form.querySelector<HTMLDetailsElement>("details[data-enable-entities]")!;
    expect(folded.open).toBe(false);
    expect([...folded.querySelectorAll("li")].map((item) => (item as HTMLElement).dataset["entityId"])).toEqual([
      "switch.sigen_plant_remote_ems_controlled_by_home_assistant",
      "select.sigen_plant_remote_ems_control_mode",
      "number.sigen_plant_ess_max_charging_limit",
    ]);
    expect(messages.filter((message) => message["changes"] !== undefined || message["enable_control_entities"] !== undefined)).toEqual([]);
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["changes"] !== undefined)).toEqual([
      {
        type: "spotnav/battery_control", api_version: 1, site_id: "site_villa",
        changes: { battery_control_enabled: true }, enable_control_entities: true,
      },
    ]);
    expect(root.querySelector("[data-row='battery_control_enabled']")!.textContent).toContain("På");
  });

  it("turns them on from the step that names the disabled controls, without turning control on", async () => {
    const { root, messages } = await mount({ battery: "needs_enable.json" });
    const popover = await open(root);
    popover.querySelector<HTMLButtonElement>("button[data-action='enable-battery-entities']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-battery-enable]")!;
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["enable_control_entities"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", enable_control_entities: true },
    ]);
  });

  it("shows a disabled size as one line, and its button turns on just that entity", async () => {
    const { root, messages } = await mount({ battery: "live.json", status: { enable_facts: SIZE_FACT } });
    const popover = await open(root);
    const notice = popover.querySelector<HTMLElement>("[data-section='battery'] [data-enable-facts]")!;
    expect(notice.textContent).toContain("Batteriets storlek finns i en avstängd sensor i SolaX.");
    const button = notice.querySelector<HTMLButtonElement>("button[data-action='enable-battery-facts']")!;
    expect(button.textContent).toBe("Slå på");
    expect(messages.filter((message) => message["enable_fact_entities"] !== undefined)).toEqual([]);
    button.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["enable_fact_entities"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", enable_fact_entities: true },
    ]);
    expect(root.querySelector("[data-enable-facts]")).toBeNull();
  });

  it("words the size and the lowest level together", async () => {
    const both = { ...SIZE_FACT, needs: ["size", "floor"] };
    const { root } = await mount({ battery: "live.json", status: { enable_facts: both } });
    const popover = await open(root);
    expect(popover.querySelector("[data-enable-facts]")!.textContent).toContain(
      "Batteriets storlek och lägsta nivå finns i avstängda entiteter i SolaX.",
    );
  });

  it("lets an administrator clear an outside-control mark from the battery's own settings: it was them", async () => {
    const { root, messages } = await mount({ battery: "outside_control.json" });
    const popover = await open(root);
    const battery = popover.querySelector<HTMLElement>("[data-section='battery']")!;
    expect(battery.querySelector("[data-need='outside_control']")!.textContent).toBe(translate("sv", "battery.need.outsideControl"));
    expect(battery.querySelector("[data-need-help='outside_control']")!.textContent).toBe(translate("sv", "battery.outsideClear.help"));
    const clear = battery.querySelector<HTMLButtonElement>("button[data-action='clear-outside-control']")!;
    expect(clear.textContent).toBe("Det var jag – rensa");
    clear.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["clear_outside_control"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", clear_outside_control: true },
    ]);
    expect(root.querySelector("[data-need='outside_control']")).toBeNull();
  });

  it("offers no clear for a planner's or an integration's mark", () => {
    expect(batteryOutsideClearable("outside_control:settings_changed")).toBe(true);
    expect(batteryOutsideClearable("outside_control:grid_charge_behaviour")).toBe(true);
    // A force another system left standing (Solis's RC force), seen once: the person may clear it too.
    expect(batteryOutsideClearable("outside_control:foreign_command")).toBe(true);
    expect(translate("en", "battery.outsideClear.help")).toMatch(/48 hours/);
    expect(batteryOutsideClearable("outside_control:planner:predbat")).toBe(false);
    expect(batteryOutsideClearable("outside_control:integration:checkwatt")).toBe(false);
  });

  it("keeps the plain on/off choice when no control entity is disabled", async () => {
    const { root } = await mount({ battery: "needs_ack.json" });
    const popover = await open(root);
    popover.querySelector<HTMLButtonElement>("button[data-edit='battery_control_enabled']")!.click();
    expect(root.querySelector("form[data-battery-enable]")).toBeNull();
    expect(root.querySelector("form[data-value-editor]")).not.toBeNull();
  });

  it("shows heat as one line when nothing is found, which opens the section", async () => {
    const { root } = await mount({ heat: bareHeat() });
    const popover = await open(root);
    const heat = popover.querySelector<HTMLElement>("[data-section='heat']")!;
    expect(heat.dataset["heatNone"]).toBe("true");
    expect(heat.textContent).toBe(`${translate("sv", "heat.settings.title")}Inget hittat`);
    heat.querySelector<HTMLButtonElement>("button[data-edit='heat_none']")!.click();
    const opened = root.querySelector<HTMLElement>("[data-popover='site'] [data-section='heat']")!;
    expect(opened.dataset["heatNone"]).toBeUndefined();
    expect(opened.querySelector("button[data-edit='path']")).not.toBeNull();
    expect(opened.textContent).toContain("En varmvattenberedare på ett relä kan läggas till här.");
  });
});

describe("brandName", () => {
  it("writes each brand whose control is still being tested as people write it", () => {
    expect(brandName("solaredge")).toBe("SolarEdge");
    expect(brandName("solax")).toBe("SolaX");
    expect(brandName("goodwe")).toBe("GoodWe");
    expect(brandName("tesla")).toBe("Tesla Powerwall");
    expect(brandName("kostal")).toBe("Kostal Plenticore");
    expect(brandName("deye")).toBe("Deye / Sunsynk");
    expect(brandName("fronius")).toBe("Fronius");
    expect(brandName("huawei")).toBe("Huawei");
    expect(brandName("victron")).toBe("Victron");
  });
});
