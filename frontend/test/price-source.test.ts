// Where a charger's electricity price lives, through the real card: the `price_source` block's decoder, the
// Electricity price section's source line and shared note, the "Price from" choice and the same-area question.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { translate } from "../src/i18n";
import { decodeDashboard } from "../src/validate";
import { apiFailure, FakeHass, mountCard } from "./helpers";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

const ONE = { entry_id: "price_a", title: "Electricity price SE4" };
const COMPANY = { entry_id: "price_b", title: "Electricity price SE4 (company)" };

function priceSource(patch: Record<string, unknown> = {}): Record<string, unknown> {
  return { ...ONE, charger_count: 2, choices: [], writable: true, duplicate: null, ...patch };
}

/** The site fixture (two chargers listed) with a `price_source` block, or with none. */
function dashboard(block: Record<string, unknown> | null, fixture = "cheapest_direct_site_admin.json"): Record<string, any> {
  const payload = read("dashboard", fixture);
  if (block !== null) {
    payload["price_source"] = block;
  }
  return payload;
}

const DUPLICATE = { area_id: "SE4", entry_ids: ["price_a", "price_b"], titles: [ONE.title, COMPANY.title] };

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

function openDialog(element: Element): HTMLElement {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null)!;
}
const priceSection = (element: Element) => openDialog(element).querySelector<HTMLElement>("[data-section='market']")!;
const sent = (hass: FakeHass, type: string) => hass.messages.filter((m) => m["type"] === type);

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("the price_source block", () => {
  it("is read when present and null when absent", () => {
    const decoded = decodeDashboard(dashboard(priceSource({ choices: [ONE, COMPANY], duplicate: DUPLICATE })));
    expect(decoded.ok && decoded.value.price_source).toEqual({
      ...ONE,
      charger_count: 2,
      choices: [ONE, COMPANY],
      writable: true,
      duplicate: DUPLICATE,
    });
    const without = decodeDashboard(dashboard(null));
    expect(without.ok && without.value.price_source).toBeNull();
  });

  it("is dropped, not the whole dashboard, when it cannot be read", () => {
    for (const broken of [
      priceSource({ charger_count: -1 }),
      priceSource({ extra: true }),
      priceSource({ duplicate: { ...DUPLICATE, titles: [ONE.title] } }),
      "price_a",
    ]) {
      const payload = dashboard(null);
      payload["price_source"] = broken;
      const decoded = decodeDashboard(payload);
      expect(decoded.ok).toBe(true);
      expect(decoded.ok && decoded.value.price_source).toBeNull();
    }
  });
});

describe("the Electricity price section", () => {
  it("names the price and says every charger uses it, with the shared note", async () => {
    const { element } = await open(dashboard(priceSource()));
    const section = priceSection(element);
    expect(section.querySelector("[data-price-source]")?.textContent).toBe("Electricity price SE4 · used by all 2 chargers");
    expect(section.querySelector("[data-price-shared]")?.textContent).toBe(translate("en", "price.shared"));
    // One price: no choice, no question.
    expect(section.querySelector("[data-row='price_from']")).toBeNull();
    expect(section.querySelector("[data-price-duplicate]")).toBeNull();
    // The area and fees stay where they were.
    expect(section.querySelector("[data-row='area']")).not.toBeNull();
  });

  it("names only the price for one charger, in the reader's language", async () => {
    const { element } = await open(dashboard(priceSource({ charger_count: 1 })), { language: "sv" });
    await settle();
    const section = priceSection(element);
    expect(section.querySelector("[data-price-source]")?.textContent).toBe("Elpris SE4");
    expect(section.querySelector("[data-price-shared]")).toBeNull();
  });

  it("speaks Swedish", async () => {
    const { element } = await open(dashboard(priceSource({ choices: [ONE, COMPANY], duplicate: DUPLICATE })), {
      language: "sv",
    });
    await settle();
    const section = priceSection(element);
    expect(section.querySelector("[data-price-source]")?.textContent).toBe("Elpris SE4 · används av alla 2 laddare");
    expect(section.querySelector("[data-row='price_from']")?.textContent).toContain("Pris från");
    const question = section.querySelector<HTMLElement>("[data-price-duplicate]")!;
    expect(question.textContent).toContain("Två elpriser för SE4 med olika avgifter. Använd ett?");
    expect(Array.from(question.querySelectorAll("button")).map((button) => button.textContent)).toEqual([
      "Använd Elpris SE4",
      "Använd Electricity price SE4 (company)",
      "Behåll båda",
    ]);
  });

  it("shows nothing new for a charger with its own settings", async () => {
    const { element } = await open(dashboard(null));
    const section = priceSection(element);
    expect(section.querySelector("[data-price-source]")).toBeNull();
    expect(section.querySelector("[data-row='area']")).not.toBeNull();
  });
});

describe("Price from", () => {
  it("writes the chosen price and returns to Settings", async () => {
    const { hass, element } = await open(dashboard(priceSource({ choices: [ONE, COMPANY] })));
    const row = priceSection(element).querySelector<HTMLElement>("[data-row='price_from']")!;
    expect(row.textContent).toContain("Electricity price SE4");
    row.querySelector<HTMLButtonElement>("button")!.click();
    await settle();
    // On a site, the choice says it applies to the whole site.
    expect(openDialog(element).textContent).toContain(translate("en", "price.from.helpSite"));
    Array.from(openDialog(element).querySelectorAll<HTMLLabelElement>("label"))
      .find((label) => label.textContent?.includes("(company)"))!
      .querySelector<HTMLInputElement>("input")!
      .click();
    openDialog(element).querySelector<HTMLButtonElement>(".spotnav-settings-save")!.click();
    await settle();
    expect(sent(hass, "spotnav/choose_price_source")).toEqual([
      expect.objectContaining({ type: "spotnav/choose_price_source", entry_id: "entry_a", price_entry_id: "price_b" }),
    ]);
    hass.resolveNext({ ok: true });
    await settle();
    expect(sent(hass, "spotnav/get_dashboard").length).toBe(2);
  });

  it("tells two prices with the same name apart", async () => {
    const twin = { entry_id: "price_b", title: "Electricity price SE4" };
    const { element } = await open(
      dashboard(priceSource({ choices: [ONE, twin], duplicate: { ...DUPLICATE, titles: [ONE.title, twin.title] } })),
    );
    const buttons = Array.from(priceSection(element).querySelectorAll("[data-price-duplicate] button"));
    expect(buttons.map((button) => button.textContent)).toEqual([
      "Use Electricity price SE4 (used here)",
      "Use Electricity price SE4 (the other)",
      "Keep both",
    ]);
  });

  it("is not offered to change for a reader who may not", async () => {
    const { element } = await open(dashboard(priceSource({ choices: [ONE, COMPANY], writable: false })));
    const row = priceSection(element).querySelector<HTMLElement>("[data-row='price_from']")!;
    expect(row.querySelector("button")).toBeNull();
  });
});

describe("the same-area question", () => {
  it("uses one price, then says the other can be removed", async () => {
    const { hass, element } = await open(dashboard(priceSource({ choices: [ONE, COMPANY], duplicate: DUPLICATE })));
    const question = priceSection(element).querySelector<HTMLElement>("[data-price-duplicate='SE4']")!;
    expect(question.textContent).toContain("Two electricity prices for SE4 with different fees. Use one?");
    question.querySelector<HTMLButtonElement>("[data-keep='price_b']")!.click();
    await settle();
    expect(sent(hass, "spotnav/answer_price_duplicate")).toEqual([
      expect.objectContaining({ area_id: "SE4", keep_entry_id: "price_b" }),
    ]);
    hass.resolveNext({ ok: true });
    await settle();
    hass.resolveNext(dashboard(priceSource({ ...COMPANY, choices: [ONE, COMPANY] })));
    await settle();
    const section = priceSection(element);
    expect(section.querySelector("[data-price-duplicate]")).toBeNull();
    expect(section.querySelector("[data-price-removable]")?.textContent).toBe(
      "The other electricity price can be removed under Devices & services.",
    );
  });

  it("keeps both without a note", async () => {
    const { hass, element } = await open(dashboard(priceSource({ choices: [ONE, COMPANY], duplicate: DUPLICATE })));
    priceSection(element).querySelector<HTMLButtonElement>("[data-price-duplicate] [data-keep='']")!.click();
    await settle();
    expect(sent(hass, "spotnav/answer_price_duplicate")[0]).toMatchObject({ area_id: "SE4", keep_entry_id: null });
    hass.resolveNext({ ok: true });
    await settle();
    hass.resolveNext(dashboard(priceSource({ choices: [ONE, COMPANY] })));
    await settle();
    expect(priceSection(element).querySelector("[data-price-removable]")).toBeNull();
  });

  it("says why when the answer is refused, and may be answered again", async () => {
    const { hass, element } = await open(dashboard(priceSource({ choices: [ONE, COMPANY], duplicate: DUPLICATE })));
    const question = priceSection(element).querySelector<HTMLElement>("[data-price-duplicate]")!;
    question.querySelector<HTMLButtonElement>("[data-keep='price_a']")!.click();
    await settle();
    expect(Array.from(question.querySelectorAll("button")).every((button) => button.disabled)).toBe(true);
    hass.rejectNext(apiFailure("spotnav_unknown_price_entry"));
    await settle();
    expect(question.querySelector("[role='alert']")?.textContent).toBe(translate("en", "settings.error.generic"));
    expect(Array.from(question.querySelectorAll("button")).some((button) => button.disabled)).toBe(false);
  });

  it("is not shown without the block's question", async () => {
    const { element } = await open(dashboard(priceSource({ choices: [ONE, COMPANY], writable: false, duplicate: DUPLICATE })));
    expect(priceSection(element).querySelector("[data-price-duplicate]")).toBeNull();
  });
});
