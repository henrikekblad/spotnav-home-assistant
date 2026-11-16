// The shared settings popover's price and solar sections, opened from the house and heat cards of a home with or
// without a charger: the charger card's own editors (`price-section.ts`) over the site's view
// (`tests/fixtures/site_view/`), each write by `site_id`: the grid tariff and the supplier's figures
// (`spotnav/update_grid_tariff`), a fee and the area (`spotnav/update_price`), the price entry
// (`spotnav/choose_price_source`) and the solar forecast sources (`spotnav/update_site_settings`).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { HEAT_CARD_ELEMENT, SpotnavHeatCard } from "../src/heat-card";
import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { registerTranslation } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

const MARKET = {
  api_version: 1,
  state: "ready",
  reason: null,
  configured_area: "SE4",
  areas: [
    {
      area_id: "SE3",
      name: "Stockholm",
      countries: ["SE"],
      timezone: "Europe/Stockholm",
      currency: "SEK",
      major_unit: "kr",
      minor_unit: "öre",
      suggestions: { vat_percent: 25, tax_minor: 36, transfer_minor: 25 },
    },
    {
      area_id: "SE4",
      name: "Malmö",
      countries: ["SE"],
      timezone: "Europe/Stockholm",
      currency: "SEK",
      major_unit: "kr",
      minor_unit: "öre",
      suggestions: { vat_percent: 25, tax_minor: 36, transfer_minor: 30 },
    },
  ],
};

beforeAll(() => {
  registerTranslation("sv", TRANSLATIONS.sv);
});

type Card = "house" | "heat";

describe("the popover's price and solar sections", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
      customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
    }
    if (customElements.get(HEAT_CARD_ELEMENT) === undefined) {
      customElements.define(HEAT_CARD_ELEMENT, SpotnavHeatCard);
    }
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function fakeHass(view: Record<string, unknown>, refusal: string | null = null) {
    const messages: Record<string, unknown>[] = [];
    const hass = {
      language: "sv",
      locale: { language: "sv" },
      states: {},
      user: { is_admin: true },
      callWS: async (message: Record<string, unknown>): Promise<unknown> => {
        messages.push(message);
        switch (message["type"]) {
          case "spotnav/get_house":
            return read("house", "villa_battery_sun.json");
          case "spotnav/get_heat":
            return read("heat_card", "villa.json");
          case "spotnav/get_site_settings_view":
            return view;
          case "spotnav/battery_control":
            return read("battery_control", "needs_ack.json");
          case "spotnav/get_entity_config":
            return { api_version: 1, ok: false, error: "spotnav_unknown_site", field_errors: [], config: null };
          case "spotnav/get_market_options":
            return MARKET;
          case "spotnav/update_price":
          case "spotnav/update_grid_tariff":
            if (refusal !== null) {
              throw { code: refusal, message: "refused" };
            }
            return { ok: true };
          case "spotnav/choose_price_source":
            return { ok: true };
          case "spotnav/update_site_settings": {
            const site = view["site"] as Record<string, unknown>;
            const changes = message["changes"] as Record<string, unknown>;
            const forecast = site["solar_forecast"] as Record<string, unknown>;
            return {
              api_version: 1,
              ok: true,
              error: null,
              site: { ...site, solar_forecast: { ...forecast, selected: changes["solar_forecast"] } },
              restore: null,
            };
          }
          default:
            return {};
        }
      },
    };
    return { hass, messages };
  }

  async function open(card: Card, view: Record<string, unknown> = read("site_view", "villa.json"), refusal: string | null = null) {
    const { hass, messages } = fakeHass(view, refusal);
    const tag = card === "house" ? HOUSE_CARD_ELEMENT : HEAT_CARD_ELEMENT;
    const element = document.createElement(tag) as SpotnavHouseCard | SpotnavHeatCard;
    element.setConfig({ type: `custom:${tag}`, site: "site_villa" });
    element.hass = hass as never;
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    const root = element.shadowRoot!;
    root.querySelector<HTMLButtonElement>("button[data-action='settings']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    const sent = (type: string) => messages.filter((message) => message["type"] === type);
    const popover = () => root.querySelector<HTMLElement>("[data-popover='site']")!;
    const section = (name: string) => popover().querySelector<HTMLElement>(`[data-section='${name}']`)!;
    const tap = async (name: string, key: string): Promise<void> => {
      section(name).querySelector<HTMLButtonElement>(`[data-row='${key}'] button`)!.click();
      await vi.advanceTimersByTimeAsync(0);
    };
    const form = () => root.querySelector<HTMLFormElement>("form[data-value-editor]")!;
    const typeNumber = (text: string): void => {
      const none = form().querySelector<HTMLInputElement>("[data-value-none]");
      if (none !== null && none.checked) {
        none.checked = false;
        none.dispatchEvent(new Event("change"));
      }
      form().querySelector<HTMLInputElement>("[data-value-field='number']")!.value = text;
    };
    const save = async (): Promise<void> => {
      root.querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
      await vi.advanceTimersByTimeAsync(0);
    };
    return { root, messages, sent, popover, section, tap, form, typeNumber, save };
  }

  const rowKeys = (section: HTMLElement) =>
    Array.from(section.querySelectorAll<HTMLElement>("[data-row]")).map((row) => row.dataset["row"]);

  for (const card of ["house", "heat"] as const) {
    it(`shows the charger card's price rows and solar from the ${card} card`, async () => {
      const { section, popover } = await open(card);
      expect([...popover().children].map((node) => (node as HTMLElement).dataset["section"])).toContain("solar");
      const price = section("price");
      expect(price.querySelector("[data-price-source]")!.textContent).toBe("Elpris SE4");
      expect(rowKeys(price)).toEqual([
        "area", "vat", "tax", "transfer", "transfer_percent", "transfer_high",
        "supplier_markup", "sell_adjustment", "demand_add",
      ]);
      expect(price.querySelector("[data-row='area']")!.textContent).toContain("SE4");
      expect(price.querySelector("[data-row='supplier_markup']")!.textContent).toBe("Påslag från elhandlaren4,5 öre/kWh");
      expect(price.querySelector("[data-row='sell_adjustment']")!.textContent).toBe("Såld kWh: tillägg eller avdrag−2 öre/kWh");
      expect(section("solar").querySelector("[data-row='solar_forecast']")!.textContent).toContain("Forecast.Solar Tak");
    });
  }

  it("hides the sold kWh where nothing is sold and the figure is off and unset", async () => {
    const view = read("site_view", "villa.json");
    const settings = view["price_settings"] as Record<string, unknown>;
    const tariff = settings["grid_tariff"] as Record<string, unknown>;
    const off = { policy: "off", explicit_value: null, effective_value: null, unit: "öre/kWh" };
    const quiet = { ...view, price_settings: { ...settings, grid_tariff: { ...tariff, sells: false, sell_adjustment: off } } };
    const { section } = await open("house", quiet);
    expect(rowKeys(section("price"))).toContain("supplier_markup");
    expect(rowKeys(section("price"))).not.toContain("sell_adjustment");
  });

  it("writes the supplier's markup to the site's price, against the price's write counter", async () => {
    const { tap, typeNumber, save, sent } = await open("house");
    await tap("price", "supplier_markup");
    typeNumber("3,25");
    await save();
    const [write] = sent("spotnav/update_grid_tariff");
    expect(write).toMatchObject({ type: "spotnav/update_grid_tariff", site_id: "site_villa", expected_write_revision: 1 });
    expect(write!["charger_id"]).toBeUndefined();
    expect((write!["grid_tariff"] as Record<string, unknown>)["supplier_markup"]).toEqual({ enabled: true, value: 3.25 });
    expect(sent("spotnav/get_site_settings_view").length).toBeGreaterThan(1);
  });

  it("says a conflict and stays open", async () => {
    const { root, tap, typeNumber, save } = await open("house", read("site_view", "villa.json"), "revision_conflict");
    await tap("price", "transfer_percent");
    typeNumber("7");
    await save();
    expect(root.querySelector("form[data-value-editor]")).not.toBeNull();
    expect(root.querySelector(".spotnav-settings-error:not([hidden])")!.textContent).toContain("Elpriset ändrades någon annanstans");
  });

  it("writes one fee as the area's row, reading the area's suggestion first", async () => {
    const { tap, typeNumber, save, sent } = await open("house");
    await tap("price", "transfer");
    expect(sent("spotnav/get_market_options")).toEqual([{ type: "spotnav/get_market_options", api_version: 1, site_id: "site_villa" }]);
    typeNumber("28");
    await save();
    expect(sent("spotnav/update_price")).toEqual([
      {
        type: "spotnav/update_price",
        site_id: "site_villa",
        expected_write_revision: 1,
        price: {
          area_id: "SE4",
          vat: { enabled: true, value: null },
          tax: { enabled: true, value: null },
          transfer: { enabled: true, value: 28 },
        },
      },
    ]);
  });

  it("changes the area in the charger card's area editor", async () => {
    const { root, tap, save, sent } = await open("heat");
    await tap("price", "area");
    const select = root.querySelector<HTMLSelectElement>("select")!;
    expect(select.value).toBe("SE4");
    select.value = "SE3";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector<HTMLSelectElement>("select")!.value).toBe("SE3");
    await save();
    const [write] = sent("spotnav/update_price");
    expect(write!["site_id"]).toBe("site_villa");
    expect((write!["price"] as Record<string, unknown>)["area_id"]).toBe("SE3");
  });

  it("chooses the price entry by site where there are several", async () => {
    const view = read("site_view", "villa.json");
    const settings = view["price_settings"] as Record<string, unknown>;
    const source = settings["price_source"] as Record<string, unknown>;
    const choices = [
      { entry_id: "price_se4", title: "Elpris SE4" },
      { entry_id: "price_other", title: "Elpris SE4 (stuga)" },
    ];
    const several = { ...view, price_settings: { ...settings, price_source: { ...source, choices } } };
    const { root, tap, save, sent } = await open("house", several);
    await tap("price", "price_from");
    root.querySelector<HTMLInputElement>("input[value='price_other']")!.click();
    await save();
    expect(sent("spotnav/choose_price_source")).toEqual([
      { type: "spotnav/choose_price_source", site_id: "site_villa", price_entry_id: "price_other" },
    ]);
  });

  it("writes the solar forecast sources by site", async () => {
    const { root, tap, save, sent } = await open("house");
    await tap("solar", "solar_forecast");
    const boxes = [...root.querySelectorAll<HTMLInputElement>("form[data-value-editor='multi'] input[type='checkbox']")];
    expect(boxes.map((box) => box.checked)).toEqual([true, false]);
    boxes[1]!.click();
    await save();
    expect(sent("spotnav/update_site_settings")).toEqual([
      {
        type: "spotnav/update_site_settings",
        api_version: 1,
        site_id: "site_villa",
        expected: { solar_forecast: ["forecast_solar_roof"] },
        changes: { solar_forecast: ["forecast_solar_roof", "solcast_a"] },
      },
    ]);
  });

  it("offers adding an electricity price where none applies, and still the solar section", async () => {
    const { section } = await open("house", read("site_view", "no_charger.json"));
    expect(rowKeys(section("price"))).toEqual(["price_area"]);
    expect(section("price").textContent).toContain("Lägg till ett elpris");
    expect(section("solar").querySelector("[data-row='solar_forecast']")!.textContent).toBe("Källor för solprognosInget");
  });
});
