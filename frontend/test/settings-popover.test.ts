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
import { batteryLine, batteryNeed, batteryOutsideClearable, outcomeVerdict, preheatLine } from "../src/settings-popover";
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
  const choices = { ...(villa["choices"] as Record<string, unknown>), zones: [] };
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
            if (message["enable_control_entities"] === true) {
              battery = { ...battery, status: { ...(battery["status"] as object), enable_entities: null } };
            }
            if (changes !== undefined) {
              const status = battery["status"] as Record<string, unknown>;
              battery = { ...battery, status: { ...status, options: { ...(status["options"] as object), ...changes } } };
            }
            return battery;
          }
          case "spotnav/get_entity_config":
            return options.entity ?? { api_version: 1, ok: false, error: "spotnav_unknown_site", field_errors: [], config: null };
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

  it("has no gear for a household member", async () => {
    const { root } = await mount({ admin: false });
    expect(root.querySelector("button[data-action='settings']")).toBeNull();
  });

  it("opens the site's sections, Get started first with adding a charger answerable as not needed", async () => {
    const { root, messages } = await mount();
    const popover = await open(root);
    expect([...popover.children].map((node) => (node as HTMLElement).dataset["section"])).toEqual([
      "setup", "price", "site", "solar", "battery", "heat", "support",
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
    expect(root.querySelectorAll("[data-beta='steps'] li")).toHaveLength(5);
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
        import_limit: { ...checking["grid_charge_cap"]["import_limit"], per_phase: { max_kw: 13.16, holds: false, entity_id: null, ...phase } },
      },
      ...more,
    });
    // Not said to act: the button opens the site's own word and writes it.
    const said = await mount({ battery: "checking.json", status: withPhase({ why: "not_said_to_act" }) });
    let popover = await open(said.root);
    let note = popover.querySelector<HTMLElement>("[data-note='import_limit_phase']")!;
    expect(note.dataset["code"]).toBe("not_said_to_act");
    expect(note.textContent).toContain("Slå på ”Importgränsen verkar” för att lita på den.");
    // The total rule's advice (far under the fuse) is not given beside it.
    expect(popover.querySelector("[data-import-limit]")).toBeNull();
    note.querySelector<HTMLButtonElement>("button[data-action='import-limit-acts']")!.click();
    const form = said.root.querySelector<HTMLFormElement>("form[data-value-editor]")!;
    expect(form.textContent).toContain("högst 3 × (säkring − säkerhetsmarginal) × spänning");
    form.querySelector<HTMLInputElement>("input[value='on']")!.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(said.messages.filter((message) => message["changes"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", changes: { battery_control_import_limit_acts: true } },
    ]);
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
      "Jag förstår och accepterar att en säkring kan lösa ut om Home Assistant stannar under en nätladdning av husbatteriet.",
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
      "Godkänt att en säkring kan lösa ut om Home Assistant stannar under en nätladdning av husbatteriet. Bara en administratör kan ändra det.",
    );
  });

  it("offers the battery mode with a help line each, says what it does while control is off, and writes the choice", async () => {
    const { root, messages } = await mount({ battery: "checking.json" });
    const popover = await open(root);
    const battery = popover.querySelector<HTMLElement>("[data-section='battery']")!;
    const row = battery.querySelector("[data-row='battery_mode']")!;
    expect(row.textContent).toBe("LägeBalanserad");
    expect(battery.querySelector("[data-help='battery_mode']")!.textContent).toBe(
      "Laddar också från nätet när det är billigt och används när det är dyrt. Säljer bara när det är mycket lönsamt.",
    );
    expect(battery.querySelector("[data-battery-mode-shadow]")!.textContent).toBe(
      "Medan SpotNav inte styr batteriet flyttar valet bara vilken rad som är markerad som vald i jämförelsen.",
    );
    battery.querySelector<HTMLButtonElement>("button[data-edit='battery_mode']")!.click();
    const form = root.querySelector<HTMLFormElement>("form[data-value-editor]")!;
    expect(
      [...form.querySelectorAll<HTMLInputElement>("input[type='radio']")].map((radio) => [radio.value, radio.checked]),
    ).toEqual([["self_consumption", false], ["balanced", true], ["max_profit", false]]);
    expect(form.textContent).toContain("Batteriet laddas av solen och täcker huset. Ingen handel med nätet.");
    expect(form.textContent).toContain("Som Balanserad, men säljer till nätet så fort det lönar sig, med slitaget inräknat.");
    form.querySelector<HTMLInputElement>("input[value='max_profit']")!.click();
    form.requestSubmit();
    await vi.advanceTimersByTimeAsync(0);
    expect(messages.filter((message) => message["changes"] !== undefined)).toEqual([
      { type: "spotnav/battery_control", api_version: 1, site_id: "site_villa", changes: { battery_mode: "max_profit" } },
    ]);
    expect(root.querySelector("[data-row='battery_mode']")!.textContent).toContain("Max vinst");
  });

  it("has the mode's words and no sell row in every language", () => {
    for (const language of Object.keys(TRANSLATIONS) as Array<keyof typeof TRANSLATIONS>) {
      const words = TRANSLATIONS[language] as Record<string, string>;
      const mode = ["battery.modeLabel", "battery.modeShadow", "battery.modeHelp.self_consumption", "battery.modeHelp.balanced", "battery.modeHelp.max_profit"].map((key) => words[key]);
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
    expect(form.textContent).toContain("SpotNav behöver slå på 3 styrentiteter i Sigenergy.");
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
