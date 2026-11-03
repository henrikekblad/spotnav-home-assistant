// The demand charge in the Electricity price section and the import target in the site's rows (the dashboard's
// `grid_tariff.demand` block): the strict decoder against the backend's own fixture of the pilot's site, the rows
// and lines, the component editor and the write it sends, and the import target beside the main fuse.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { demandRows, hourLine } from "../src/demand-charge";
import { storedBody, withComponent } from "../src/grid-tariff";
import { translate } from "../src/i18n";
import { decodeDashboard, decodeDemand, decodeGridTariff, type DemandBlock } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

/** The pilot's site at 10:20 CEST, as `tests/test_demand_site.py` captured it. */
const PILOT = read("grid_tariff", "pilot_demand.json");
const DEMAND = PILOT["demand"] as Record<string, any>;
const NONE = { ...DEMAND, components: [], per_component: [], hour: null, import_target_kw: null };
const FORMAT = { language: "sv" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };
const SOURCE = { entry_id: "price_a", title: "Electricity price SE4", charger_count: 1, choices: [], writable: true, duplicate: null };

function dashboard(block: Record<string, unknown>): Record<string, any> {
  const payload = read("dashboard", "cheapest_direct_site_admin.json");
  payload["price_source"] = SOURCE;
  payload["grid_tariff"] = block;
  return payload;
}

describe("the demand block", () => {
  it("reads the backend's own block exactly", () => {
    const decoded = decodeGridTariff(PILOT);
    expect(decoded.demand).toEqual(DEMAND);
    expect(decodeDemand(NONE)).toEqual(NONE);
  });

  it("drops the tariff block, not the dashboard, when the demand part is not its shape", () => {
    const broken = [
      { ...DEMAND, extra: 1 },
      { ...DEMAND, per_component: DEMAND["per_component"].slice(0, 1) },
      { ...DEMAND, components: [...DEMAND["components"], ...DEMAND["components"]] },
      { ...DEMAND, components: [{ ...DEMAND["components"][0], top_n: 2 }] , per_component: [] },
      { ...DEMAND, components: [{ ...DEMAND["components"][0], price_per_kw: { winter: -1, summer: null } }], per_component: [] },
      { ...DEMAND, components: [{ ...DEMAND["components"][0], window: { ...DEMAND["components"][0]["window"], to_hour: 6 } }], per_component: [] },
      { ...DEMAND, hour: { start: "x", kw_so_far: 1 } },
      { ...DEMAND, import_target_kw: -2 },
      { ...DEMAND, effective_target_kw: "6" },
    ];
    for (const demand of broken) {
      const decoded = decodeDashboard(dashboard({ ...PILOT, demand }));
      expect(decoded.ok).toBe(true);
      expect(decoded.ok && decoded.value.grid_tariff, JSON.stringify(demand)).toBeNull();
    }
  });

  it("words each component, its prices and this month's peak, in Swedish", () => {
    const rows = demandRows(FORMAT, decodeDemand(DEMAND));
    expect(rows.map((row) => [row.key, row.label, row.value])).toEqual([
      [0, "Effektavgift 1", "Högsta timmen, Alla dagar 06–23, hela året"],
      [1, "Effektavgift 2", "Högsta timmen, Alla dagar 23–06, hela året"],
      ["add", "Ytterligare en del av effektavgiften", "Lägg till"],
    ]);
    expect(rows[0]!.lines).toEqual(["45 / 23 kr/kW (vinter / sommar)", "Den här månaden: 7,0 kW – tis 22 sep. 09:00 · 7,0 kW"]);
    expect(rows[1]!.lines).toEqual(["12 / 8 kr/kW (vinter / sommar)", "Ingen topp än den här månaden"]);
    // Nothing set: one row, "None".
    expect(demandRows(FORMAT, decodeDemand(NONE)).map((row) => [row.key, row.label, row.value])).toEqual([
      ["add", "Effektavgift", "Inget"],
    ]);
    // A site that cannot measure says why, once.
    const blind = demandRows(FORMAT, decodeDemand({ ...DEMAND, available: false, reason: "needs_total_grid_power", per_component: [], hour: null }));
    expect(blind[1]!.lines).toEqual(["12 / 8 kr/kW (vinter / sommar)", translate("sv", "demand.reason.needs_total_grid_power")]);
    expect(demandRows(FORMAT, null)).toEqual([]);
  });

  it("shows the running hour against the import target, and nothing without a target", () => {
    // At 10:20 the day part's peak (7,0 kW at 09:00) lifts the 6 kW target for the rest of the month's day hours.
    expect(hourLine("sv", decodeDemand(DEMAND))).toBe("Den här timmen: 3,1 av 6,8 kW – månadens topp (effektmål 6,0 kW)");
    expect(hourLine("en", decodeDemand(DEMAND))).toBe("This hour: 3.1 of 6.8 kW – the month's peak (import target 6.0 kW)");
    expect(hourLine("sv", decodeDemand({ ...DEMAND, effective_target_kw: 6 }))).toBe("Den här timmen: 3,1 av 6,0 kW");
    // A backend before the level was stated: the target alone.
    const { effective_target_kw: _level, ...older } = DEMAND;
    expect(decodeDemand(older).effective_target_kw).toBeNull();
    expect(hourLine("sv", decodeDemand(older))).toBe("Den här timmen: 3,1 av 6,0 kW");
    expect(hourLine("en", decodeDemand({ ...DEMAND, hour: null }))).toBe("This hour: not measured yet");
    expect(hourLine("en", decodeDemand(NONE))).toBeNull();
    expect(hourLine("en", decodeDemand({ ...NONE, import_target_kw: 6, available: false, reason: "no_site" }))).toBe(
      translate("en", "demand.reason.no_site"),
    );
  });

  it("writes the components with the tariff, and never to a backend that does not state them", () => {
    const block = decodeGridTariff(PILOT);
    const components = (block.demand as DemandBlock).components;
    expect(storedBody(block).demand_components).toEqual(components);
    const one = { window: null, top_n: 3 as const, price_per_kw: { winter: 49, summer: 49 } };
    expect(withComponent(block, 2, one).demand_components).toEqual([...components, one]);
    expect(withComponent(block, 0, one).demand_components).toEqual([one, components[1]]);
    expect(withComponent(block, 0, null).demand_components).toEqual([components[1]]);
    const { demand: _demand, ...withoutDemand } = PILOT;
    const older = decodeGridTariff(withoutDemand);
    expect("demand_components" in storedBody(older)).toBe(false);
  });
});

// ------------------------------------------------------------------------------------------------ in the card

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function entityConfig(target: number | null): Record<string, any> {
  const config = read("entity_config", "v1", "get_direct.json");
  for (const field of config["config"]?.["fields"] ?? config["fields"] ?? []) {
    if (field["field"] === "import_target_kw") {
      field["value"] = target;
    }
  }
  return config;
}

async function open(
  block: Record<string, unknown>,
  options: { language?: string; target?: number | null } = {},
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  hass.entityHandler = async () => entityConfig(options.target ?? null);
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", options.language ?? "en");
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(dashboard(block));
  await settle();
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate((options.language ?? "en") as "en", "header.settings")}"]`)
    ?.click();
  await settle();
  return { hass, element };
}

function openDialog(element: Element): HTMLElement {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null)!;
}
const section = (element: Element, name: string) => openDialog(element).querySelector<HTMLElement>(`[data-section='${name}']`)!;
const rowKeys = (element: Element, name: string) =>
  Array.from(section(element, name).querySelectorAll<HTMLElement>("[data-row]")).map((row) => row.dataset["row"]);
const sent = (hass: FakeHass, type: string) => hass.messages.filter((m) => m["type"] === type);

async function tap(element: Element, name: string, key: string): Promise<void> {
  section(element, name).querySelector<HTMLButtonElement>(`[data-row='${key}'] button`)!.click();
  await settle();
}

async function save(element: Element): Promise<void> {
  openDialog(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
  await settle();
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("the demand charge in the Electricity price section", () => {
  it("shows one row, None, until a component is added", async () => {
    const { element } = await open({ ...PILOT, demand: NONE });
    const keys = rowKeys(element, "market");
    expect(keys.filter((key) => key!.startsWith("demand"))).toEqual(["demand_add"]);
    expect(section(element, "market").querySelector("[data-row='demand_add']")!.textContent).toBe("Demand chargeNone");
  });

  it("lists each component with its month beneath, after the fees", async () => {
    const { element } = await open(PILOT, { language: "sv" });
    const keys = rowKeys(element, "market");
    expect(keys.slice(-3)).toEqual(["demand_0", "demand_1", "demand_add"]);
    const help = section(element, "market").querySelector("[data-help='demand_0_1']")!;
    expect(help.textContent).toContain("Den här månaden: 7,0 kW");
  });

  it("adds a component with its own hours, peaks and prices in one write", async () => {
    const { hass, element } = await open({ ...PILOT, demand: NONE });
    await tap(element, "market", "demand_add");
    const dialog = openDialog(element);
    expect(dialog.querySelector("[data-value-editor='demand']")).not.toBeNull();
    // All hours by default: the window's fields are hidden until chosen.
    expect(dialog.querySelector<HTMLElement>("[data-demand='window']")!.hidden).toBe(true);
    const counted = dialog.querySelector<HTMLSelectElement>("[data-demand='counted']")!;
    counted.value = "window";
    counted.dispatchEvent(new Event("change"));
    expect(dialog.querySelector<HTMLElement>("[data-demand='window']")!.hidden).toBe(false);
    dialog.querySelector<HTMLSelectElement>("[data-window='days']")!.value = "all";
    dialog.querySelector<HTMLSelectElement>("[data-window='fromMonth']")!.value = "1";
    dialog.querySelector<HTMLSelectElement>("[data-window='toMonth']")!.value = "12";
    dialog.querySelector<HTMLSelectElement>("[data-window='from']")!.value = "23";
    dialog.querySelector<HTMLSelectElement>("[data-window='to']")!.value = "6";
    dialog.querySelector<HTMLInputElement>("[data-demand='winter']")!.value = "12";
    dialog.querySelector<HTMLInputElement>("[data-demand='summer']")!.value = "8,5";
    expect(dialog.querySelector("[data-demand='remove']")).toBeNull();
    await save(element);
    expect(sent(hass, "spotnav/update_grid_tariff").map((m) => m["grid_tariff"])).toEqual([
      {
        transfer_percent: { enabled: false, value: null },
        transfer_high: { enabled: false, value: null },
        high_load_window: null,
        demand_components: [
          {
            window: { days: "all", from_month: 1, to_month: 12, from_hour: 23, to_hour: 6 },
            top_n: 1,
            price_per_kw: { winter: 12, summer: 8.5 },
          },
        ],
      },
    ]);
  });

  it("refuses a negative price, and removes a component with Remove", async () => {
    const { hass, element } = await open(PILOT);
    await tap(element, "market", "demand_1");
    const dialog = openDialog(element);
    expect(dialog.querySelector<HTMLSelectElement>("[data-demand='counted']")!.value).toBe("window");
    expect(dialog.querySelector<HTMLSelectElement>("[data-window='from']")!.value).toBe("23");
    dialog.querySelector<HTMLInputElement>("[data-demand='winter']")!.value = "-1";
    await save(element);
    expect(openDialog(element).textContent).toContain(translate("en", "demand.error.price"));
    expect(sent(hass, "spotnav/update_grid_tariff")).toEqual([]);
    openDialog(element).querySelector<HTMLButtonElement>("[data-demand='remove']")!.click();
    await settle();
    const written = sent(hass, "spotnav/update_grid_tariff").map((m) => (m["grid_tariff"] as Record<string, unknown>)["demand_components"]);
    expect(written).toEqual([[DEMAND["components"][0]]]);
  });
});

describe("the import target beside the main fuse", () => {
  it("shows the running hour against the target", async () => {
    const { element } = await open(PILOT, { language: "sv", target: 6 });
    expect(rowKeys(element, "site").slice(0, 2)).toEqual(["main_fuse_a", "import_target_kw"]);
    expect(section(element, "site").querySelector("[data-row='import_target_kw']")!.textContent).toBe("Effektmål6,0 kW");
    expect(section(element, "site").querySelector("[data-help='import_target_hour']")!.textContent).toBe(
      "Den här timmen: 3,1 av 6,8 kW – månadens topp (effektmål 6,0 kW)",
    );
  });

  it("sets it, and clears it with Off", async () => {
    const { hass, element } = await open(PILOT, { target: null });
    await tap(element, "site", "import_target_kw");
    let dialog = openDialog(element);
    const none = dialog.querySelector<HTMLInputElement>("[data-value-none]")!;
    expect(none.checked).toBe(true);
    none.checked = false;
    none.dispatchEvent(new Event("change"));
    dialog.querySelector<HTMLInputElement>("[data-value-field='number']")!.value = "6";
    await save(element);
    const updates = hass.entityMessages.filter((m) => m["type"] === "spotnav/update_entity_config");
    expect(updates.map((m) => [m["scope"], m["changes"], m["expected"]])).toEqual([["site", { import_target_kw: 6 }, {}]]);

    const cleared = await open(PILOT, { target: 6 });
    await tap(cleared.element, "site", "import_target_kw");
    dialog = openDialog(cleared.element);
    const off = dialog.querySelector<HTMLInputElement>("[data-value-none]")!;
    off.checked = true;
    off.dispatchEvent(new Event("change"));
    await save(cleared.element);
    const clears = cleared.hass.entityMessages.filter((m) => m["type"] === "spotnav/update_entity_config");
    expect(clears.map((m) => [m["changes"], m["expected"]])).toEqual([[{ import_target_kw: "" }, { import_target_kw: 6 }]]);
  });
});

describe("the suggested import target in its editor", () => {
  const SUGGESTION = read("target_suggestion", "pilot.json");

  it("reads it when the editor opens, words it, and Use fills the field without saving", async () => {
    const { hass, element } = await open(PILOT, { language: "sv", target: null });
    hass.suggestHandler = async () => SUGGESTION;
    await tap(element, "site", "import_target_kw");
    expect(hass.suggestMessages).toEqual([{ type: "spotnav/suggest_import_target", api_version: 1, charger_id: "entry_a" }]);
    await settle();
    const dialog = openDialog(element);
    const advice = dialog.querySelector<HTMLElement>("[data-value-advice]")!;
    expect(advice.dataset["valueAdvice"]).toBe("suggestion");
    expect(advice.textContent).toContain(
      "Huset ensamt: oftast högst 2,4 kW en timme per dygn, som mest 7,5 kW (fre 4 sep.). Förslag: 7,0 kW – bilen får då omkring 6,4 kW de flesta timmar.",
    );
    expect(advice.textContent).toContain("Laddarens egen effekt finns inte i statistiken");
    const use = dialog.querySelector<HTMLButtonElement>("[data-value-advice-use]")!;
    expect(use.textContent).toBe("Använd");
    use.click();
    const field = dialog.querySelector<HTMLInputElement>("[data-value-field='number']")!;
    expect(field.value).toBe("7");
    expect(dialog.querySelector<HTMLInputElement>("[data-value-none]")!.checked).toBe(false);
    expect(hass.entityMessages.filter((m) => m["type"] === "spotnav/update_entity_config")).toEqual([]);
    await save(element);
    const updates = hass.entityMessages.filter((m) => m["type"] === "spotnav/update_entity_config");
    expect(updates.map((m) => m["changes"])).toEqual([{ import_target_kw: 7 }]);
  });

  it("says why there is none, and leaves nothing behind when it cannot be read", async () => {
    const { hass, element } = await open(PILOT, { language: "en", target: 6 });
    hass.suggestHandler = async () => ({ ...SUGGESTION, available: false, reason: "too_few_days", days: 3, suggestion_kw: null });
    await tap(element, "site", "import_target_kw");
    await settle();
    const advice = openDialog(element).querySelector<HTMLElement>("[data-value-advice]")!;
    expect(advice.textContent).toBe("No suggestion yet: Home Assistant's statistics only go back 3 days.");
    expect(openDialog(element).querySelector("[data-value-advice-use]")).toBeNull();

    const failing = await open(PILOT, { target: 6 });
    failing.hass.suggestHandler = async () => {
      throw new Error("unknown command");
    };
    await tap(failing.element, "site", "import_target_kw");
    await settle();
    expect(openDialog(failing.element).querySelector("[data-value-advice]")).toBeNull();
  });

  it("shows that it is reading while the statistics are read", async () => {
    const { element } = await open(PILOT, { target: 6 });
    await tap(element, "site", "import_target_kw");
    const advice = openDialog(element).querySelector<HTMLElement>("[data-value-advice]")!;
    expect(advice.dataset["valueAdvice"]).toBe("loading");
    expect(advice.textContent).toBe("Reading Home Assistant's statistics …");
  });
});
