// The dashboard's `grid_tariff` block (the electricity price entry's grid fee as a percentage of spot, its fee in
// high-load hours and the window): the strict decoder, and the rows beside the grid fee in the Electricity price
// section through the real card, with their editors and `spotnav/update_grid_tariff`.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { gridTariffRows, SUGGESTED_WINDOW, windowText, withHigh, withPercent, withWindow } from "../src/grid-tariff";
import { translate } from "../src/i18n";
import { decodeDashboard, decodeGridTariff } from "../src/validate";
import { apiFailure, FakeHass, mountCard } from "./helpers";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

const WINDOW = { weekdays_only: true, from_hour: 7, to_hour: 19, winter_only: true };

/** The block as `api/grid_tariff.py:serialize_grid_tariff` writes it for the owner's tariff. */
const BLOCK = {
  entry_id: "price_a",
  write_revision: 3,
  transfer_percent: { policy: "manual", explicit_value: 5, effective_value: 5, unit: "%" },
  transfer_high: { policy: "off", explicit_value: null, effective_value: null, unit: "öre/kWh" },
  high_load_window: WINDOW,
  writable: true,
};

const OFF = { policy: "off", explicit_value: null, effective_value: null };
/** Nothing stored: what a fresh electricity price carries. */
const NOTHING = {
  ...BLOCK,
  transfer_percent: { ...OFF, unit: "%" },
  transfer_high: { ...OFF, unit: "öre/kWh" },
  high_load_window: null,
};
const INCLUDED = {
  ...BLOCK,
  transfer_percent: { policy: "included", explicit_value: 5, effective_value: null, unit: "%" },
  transfer_high: { policy: "included", explicit_value: null, effective_value: null, unit: "öre/kWh" },
};

const SOURCE = { entry_id: "price_a", title: "Electricity price SE4", charger_count: 2, choices: [], writable: true, duplicate: null };

function dashboard(block: Record<string, unknown> | null): Record<string, any> {
  const payload = read("dashboard", "cheapest_direct_site_admin.json");
  payload["price_source"] = SOURCE;
  if (block !== null) {
    payload["grid_tariff"] = block;
  }
  return payload;
}

describe("the grid_tariff block", () => {
  it("is read when present, and the rest of the dashboard reads as without it", () => {
    for (const fixture of ["cheapest_direct_site_admin.json", "cheapest_no_site.json"]) {
      const plain = decodeDashboard(read("dashboard", fixture));
      const withBlock = decodeDashboard({ ...read("dashboard", fixture), grid_tariff: BLOCK });
      expect(plain.ok, fixture).toBe(true);
      expect(plain.ok && plain.value.grid_tariff, fixture).toBeNull();
      expect(withBlock.ok && withBlock.value.grid_tariff, fixture).toEqual(BLOCK);
      expect(withBlock.ok && { ...withBlock.value, grid_tariff: null }, fixture).toEqual(plain.ok && plain.value);
    }
    expect(decodeGridTariff(NOTHING)).toEqual(NOTHING);
    expect(decodeGridTariff(INCLUDED)).toEqual(INCLUDED);
    expect(decodeGridTariff({ ...BLOCK, transfer_high: { ...BLOCK.transfer_high, unit: null } }).transfer_high.unit).toBeNull();
  });

  it("is dropped, not the whole dashboard, when it is not exactly its shape", () => {
    for (const broken of [
      { ...BLOCK, demand: null },
      { ...BLOCK, writable: "yes" },
      { ...BLOCK, write_revision: -1 },
      { ...BLOCK, write_revision: 1.5 },
      { ...BLOCK, entry_id: "" },
      { ...BLOCK, transfer_percent: { ...BLOCK.transfer_percent, suggested_value: 5 } },
      { ...BLOCK, transfer_percent: { ...BLOCK.transfer_percent, policy: "suggested" } },
      // On, but nothing added; off, but something added.
      { ...BLOCK, transfer_percent: { ...BLOCK.transfer_percent, effective_value: null } },
      { ...BLOCK, transfer_high: { ...BLOCK.transfer_high, effective_value: 12 } },
      { ...BLOCK, high_load_window: { ...WINDOW, to_hour: 7 } },
      { ...BLOCK, high_load_window: { ...WINDOW, from_hour: 24 } },
      { ...BLOCK, high_load_window: { ...WINDOW, to_hour: 25 } },
      { ...BLOCK, high_load_window: { ...WINDOW, months: [11] } },
      { ...BLOCK, high_load_window: { from_hour: 7, to_hour: 19 } },
      "price_a",
    ]) {
      const decoded = decodeDashboard({ ...read("dashboard", "cheapest_no_site.json"), grid_tariff: broken });
      expect(decoded.ok).toBe(true);
      expect(decoded.ok && decoded.value.grid_tariff, JSON.stringify(broken)).toBeNull();
    }
  });
});

describe("the rows and the write's body", () => {
  const block = decodeGridTariff(BLOCK);

  it("shows the hours only while the high-load fee is on or hours are stored", () => {
    expect(gridTariffRows("sv", block).map((row) => [row.key, row.label, row.value])).toEqual([
      ["transfer_percent", "Nätavgift, % av spotpriset", "5 %"],
      ["transfer_high", "Nätavgift under höglasttid", "Av"],
      ["high_load_window", "Höglasttid", "Vardagar 07–19, nov–mar"],
    ]);
    expect(gridTariffRows("sv", decodeGridTariff(NOTHING)).map((row) => row.key)).toEqual(["transfer_percent", "transfer_high"]);
    expect(gridTariffRows("en", null)).toEqual([]);
    expect(windowText("sv", { weekdays_only: false, from_hour: 6, to_hour: 24, winter_only: false })).toBe(
      "Alla dagar 06–24, hela året",
    );
  });

  it("locks the rows where the price already includes the grid fee", () => {
    const rows = gridTariffRows("en", decodeGridTariff(INCLUDED));
    expect(rows.map((row) => [row.key, row.value, row.included])).toEqual([
      ["transfer_percent", "Included in the price", true],
      ["transfer_high", "Included in the price", true],
    ]);
  });

  it("replaces all three parts, keeping what is not changed as stored", () => {
    expect(withPercent(block, 7.5)).toEqual({
      transfer_percent: { enabled: true, value: 7.5 },
      transfer_high: { enabled: false, value: null },
      high_load_window: WINDOW,
    });
    // Off keeps the figure, so on again is one step.
    expect(withPercent(block, null).transfer_percent).toEqual({ enabled: false, value: 5 });
    expect(withHigh(block, 12)).toEqual({
      transfer_percent: { enabled: true, value: 5 },
      transfer_high: { enabled: true, value: 12 },
      high_load_window: WINDOW,
    });
    const evening = { ...WINDOW, from_hour: 16, to_hour: 20 };
    expect(withWindow(block, evening).high_load_window).toEqual(evening);
    // Never a window it was not handed.
    expect(withHigh(decodeGridTariff(NOTHING), 12).high_load_window).toBeNull();
    expect(SUGGESTED_WINDOW).toEqual(WINDOW);
  });
});

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
  openSettings(element, options.language ?? "en");
  await settle();
  return { hass, element };
}

function openSettings(element: Element, language = "en"): void {
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate(language as "en", "header.settings")}"]`)
    ?.click();
}

function openDialog(element: Element): HTMLElement {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null)!;
}
const priceSection = (element: Element) => openDialog(element).querySelector<HTMLElement>("[data-section='market']")!;
const rowKeys = (element: Element) =>
  Array.from(priceSection(element).querySelectorAll<HTMLElement>("[data-row]")).map((row) => row.dataset["row"]);
const sent = (hass: FakeHass, type: string) => hass.messages.filter((m) => m["type"] === type);

async function tap(element: Element, key: string): Promise<void> {
  priceSection(element).querySelector<HTMLButtonElement>(`[data-row='${key}'] button`)!.click();
  await settle();
}

async function save(element: Element): Promise<void> {
  openDialog(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
  await settle();
}

function typeNumber(element: Element, text: string): void {
  const dialog = openDialog(element);
  const none = dialog.querySelector<HTMLInputElement>("[data-value-none]");
  if (none !== null && none.checked) {
    none.checked = false;
    none.dispatchEvent(new Event("change"));
  }
  dialog.querySelector<HTMLInputElement>("[data-value-field='number']")!.value = text;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("the Electricity price section", () => {
  it("puts the tariff's rows right after the grid fee, in Swedish", async () => {
    const { element } = await open(dashboard(BLOCK), { language: "sv" });
    await settle();
    const keys = rowKeys(element);
    const at = keys.indexOf("transfer");
    expect(keys.slice(at, at + 4)).toEqual(["transfer", "transfer_percent", "transfer_high", "high_load_window"]);
    const text = (key: string) => priceSection(element).querySelector(`[data-row='${key}']`)!.textContent;
    expect(text("transfer_percent")).toBe("Nätavgift, % av spotpriset5 %");
    expect(text("high_load_window")).toBe("HöglasttidVardagar 07–19, nov–mar");
  });

  it("hides the hours while the fee is off and none are stored, and shows nothing without the block", async () => {
    const { element } = await open(dashboard(NOTHING));
    expect(rowKeys(element)).toContain("transfer_high");
    expect(rowKeys(element)).not.toContain("high_load_window");
    const without = await open(dashboard(null));
    expect(rowKeys(without.element)).not.toContain("transfer_percent");
  });

  it("locks the rows where the price includes the grid fee", async () => {
    const { element } = await open(dashboard(INCLUDED));
    for (const key of ["transfer_percent", "transfer_high"]) {
      const row = priceSection(element).querySelector(`[data-row='${key}']`)!;
      expect(row.textContent).toContain("Included in the price");
      expect(row.querySelector("button")).toBeNull();
    }
    expect(rowKeys(element)).not.toContain("high_load_window");
  });

  it("shows a price it may not change with its values and no editors", async () => {
    const { element } = await open(dashboard({ ...BLOCK, writable: false }));
    for (const key of ["transfer_percent", "transfer_high", "high_load_window"]) {
      const row = priceSection(element).querySelector(`[data-row='${key}']`)!;
      expect(row, key).not.toBeNull();
      expect(row.querySelector("button"), key).toBeNull();
    }
    expect(priceSection(element).querySelector("[data-row='transfer_percent']")!.textContent).toContain("5 %");
  });

  it("is, like the other fees, not shown to a reader who is not an administrator", async () => {
    const { element } = await open(dashboard(BLOCK), { admin: false });
    const dialog = openDialog(element);
    expect(dialog?.querySelector("[data-row='transfer_percent']") ?? null).toBeNull();
    expect(dialog?.querySelector("[data-row='transfer']") ?? null).toBeNull();
  });
});

describe("writing the tariff", () => {
  it("sets the percentage of spot, naming the write counter, and reads the dashboard again", async () => {
    const { hass, element } = await open(dashboard(NOTHING));
    await tap(element, "transfer_percent");
    typeNumber(element, "5");
    await save(element);
    expect(sent(hass, "spotnav/update_grid_tariff")).toEqual([
      {
        type: "spotnav/update_grid_tariff",
        charger_id: "entry_a",
        expected_write_revision: 3,
        grid_tariff: {
          transfer_percent: { enabled: true, value: 5 },
          transfer_high: { enabled: false, value: null },
          high_load_window: null,
        },
      },
    ]);
    hass.resolveNext({ ok: true, grid_tariff: { ...NOTHING, transfer_percent: BLOCK.transfer_percent } });
    await settle();
    expect(sent(hass, "spotnav/get_dashboard").length).toBe(2);
  });

  it("changes the hours from their row and writes them with the fees as stored", async () => {
    const stored = { ...BLOCK, transfer_high: { policy: "manual", explicit_value: 12, effective_value: 12, unit: "öre/kWh" } };
    const { hass, element } = await open(dashboard(stored));
    await tap(element, "high_load_window");
    const dialog = openDialog(element);
    // Stored hours: no suggestion line.
    expect(dialog.querySelector("[data-window-help='suggestion']")).toBeNull();
    expect(dialog.querySelector<HTMLSelectElement>("[data-window='from']")!.value).toBe("7");
    dialog.querySelector<HTMLSelectElement>("[data-window='from']")!.value = "16";
    dialog.querySelector<HTMLSelectElement>("[data-window='to']")!.value = "20";
    dialog.querySelector<HTMLInputElement>("[data-window='winter']")!.checked = false;
    await save(element);
    expect(sent(hass, "spotnav/update_grid_tariff").map((m) => m["grid_tariff"])).toEqual([
      {
        transfer_percent: { enabled: true, value: 5 },
        transfer_high: { enabled: true, value: 12 },
        high_load_window: { weekdays_only: true, from_hour: 16, to_hour: 20, winter_only: false },
      },
    ]);
  });

  it("refuses hours that end before they start, and writes nothing", async () => {
    const { hass, element } = await open(dashboard(BLOCK));
    await tap(element, "high_load_window");
    const dialog = openDialog(element);
    dialog.querySelector<HTMLSelectElement>("[data-window='from']")!.value = "19";
    dialog.querySelector<HTMLSelectElement>("[data-window='to']")!.value = "7";
    await save(element);
    expect(openDialog(element).textContent).toContain(translate("en", "gridFee.window.order"));
    expect(sent(hass, "spotnav/update_grid_tariff")).toEqual([]);
  });

  it("asks for the hours first when the high-load fee is turned on with none stored, then writes both at once", async () => {
    const { hass, element } = await open(dashboard(NOTHING), { language: "sv" });
    await settle();
    await tap(element, "transfer_high");
    let dialog = openDialog(element);
    // The window editor, prefilled with the suggestion and saying it is one; nothing written yet.
    expect(dialog.querySelector("[data-value-editor='window']")).not.toBeNull();
    expect(dialog.textContent).toContain(translate("sv", "gridFee.window.first"));
    expect(dialog.textContent).toContain(translate("sv", "gridFee.window.suggestion"));
    expect(dialog.querySelector(".spotnav-settings-save")!.textContent).toBe("Nästa");
    expect(dialog.querySelector<HTMLSelectElement>("[data-window='from']")!.value).toBe("7");
    expect(dialog.querySelector<HTMLSelectElement>("[data-window='to']")!.value).toBe("19");
    expect(dialog.querySelector<HTMLInputElement>("[data-window='weekdays']")!.checked).toBe(true);
    expect(dialog.querySelector<HTMLInputElement>("[data-window='winter']")!.checked).toBe(true);
    dialog.querySelector<HTMLSelectElement>("[data-window='to']")!.value = "20";
    await save(element);
    expect(sent(hass, "spotnav/update_grid_tariff")).toEqual([]);
    // Then the fee's figure, with no "off" to choose.
    dialog = openDialog(element);
    expect(dialog.querySelector("[data-value-editor='number']")).not.toBeNull();
    expect(dialog.querySelector("[data-value-none]")).toBeNull();
    typeNumber(element, "12,5");
    await save(element);
    expect(sent(hass, "spotnav/update_grid_tariff").map((m) => m["grid_tariff"])).toEqual([
      {
        transfer_percent: { enabled: false, value: null },
        transfer_high: { enabled: true, value: 12.5 },
        high_load_window: { weekdays_only: true, from_hour: 7, to_hour: 20, winter_only: true },
      },
    ]);
  });

  it("writes nothing when the turn-on is cancelled at either step", async () => {
    const { hass, element } = await open(dashboard(NOTHING));
    await tap(element, "transfer_high");
    await save(element);
    openDialog(element).querySelector<HTMLButtonElement>(".spotnav-settings-actions button:not(.spotnav-settings-save)")!.click();
    await settle();
    expect(sent(hass, "spotnav/update_grid_tariff")).toEqual([]);
  });

  it("goes straight to the figure when hours are stored, and turning it off keeps the hours and the figure", async () => {
    const { hass, element } = await open(dashboard(BLOCK));
    await tap(element, "transfer_high");
    expect(openDialog(element).querySelector("[data-value-editor='number']")).not.toBeNull();
    typeNumber(element, "12");
    await save(element);
    hass.resolveNext({ ok: true, grid_tariff: BLOCK });
    await settle();
    const on = { ...BLOCK, write_revision: 4, transfer_high: { policy: "manual", explicit_value: 12, effective_value: 12, unit: "öre/kWh" } };
    hass.resolveNext(dashboard(on));
    await settle();
    await tap(element, "transfer_high");
    const none = openDialog(element).querySelector<HTMLInputElement>("[data-value-none]")!;
    none.checked = true;
    none.dispatchEvent(new Event("change"));
    await save(element);
    const writes = sent(hass, "spotnav/update_grid_tariff");
    expect(writes.map((m) => [m["expected_write_revision"], m["grid_tariff"]])).toEqual([
      [3, { transfer_percent: { enabled: true, value: 5 }, transfer_high: { enabled: true, value: 12 }, high_load_window: WINDOW }],
      [4, { transfer_percent: { enabled: true, value: 5 }, transfer_high: { enabled: false, value: 12 }, high_load_window: WINDOW }],
    ]);
  });

  it("prefills a figure kept while the fee is off", async () => {
    const kept = { ...BLOCK, transfer_percent: { policy: "off", explicit_value: 5, effective_value: null, unit: "%" } };
    const { element } = await open(dashboard(kept));
    expect(priceSection(element).querySelector("[data-row='transfer_percent']")!.textContent).toContain("Off");
    await tap(element, "transfer_percent");
    const dialog = openDialog(element);
    expect(dialog.querySelector<HTMLInputElement>("[data-value-none]")!.checked).toBe(true);
    expect(dialog.querySelector<HTMLInputElement>("[data-value-field='number']")!.value).toBe("5");
  });

  it("on a conflict reads the dashboard again, says so, and the next Save names the new write counter", async () => {
    const { hass, element } = await open(dashboard(BLOCK));
    await tap(element, "transfer_percent");
    typeNumber(element, "6");
    await save(element);
    hass.rejectNext(apiFailure("revision_conflict"));
    await settle();
    expect(sent(hass, "spotnav/get_dashboard").length).toBe(2);
    hass.resolveNext(dashboard({ ...BLOCK, write_revision: 5 }));
    await settle();
    const dialog = openDialog(element);
    expect(dialog.querySelector("[data-value-editor='number']")).not.toBeNull();
    expect(dialog.textContent).toContain(translate("en", "gridFee.error.conflict"));
    await save(element);
    expect(sent(hass, "spotnav/update_grid_tariff").map((m) => m["expected_write_revision"])).toEqual([3, 5]);
  });

  it("says a refused body in the editor", async () => {
    const { hass, element } = await open(dashboard(BLOCK));
    await tap(element, "transfer_percent");
    typeNumber(element, "6");
    await save(element);
    hass.rejectNext(apiFailure("invalid_fiscal"));
    await settle();
    expect(openDialog(element).textContent).toContain(translate("en", "settings.error.invalid"));
  });
});
