// Where the area's prices come from, through the real card: the `prices.provider` decoder, the Electricity price
// section's source line, the notice rows and what their buttons do, and the ENTSO-E key's editor.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate, type Language } from "../src/i18n";
import {
  addIntegrationPath,
  integrationPath,
  OCTOPUS_HACS_PATH,
  OCTOPUS_REPOSITORY_URL,
  priceProviderNodes,
} from "../src/price-provider";
import { decodeDashboard, priceProviderOrNull, type PriceProvider } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";
import { TRANSLATIONS } from "../src/i18n/all";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;

const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, any>;

const RELAY = { key: "relay", name: "SpotNav relay", url: "https://spotnav.sensnology.se" };
const NONE = { key: null, name: null, url: null };

function provider(base: Record<string, unknown>, code: string | null, params: Record<string, string> = {}) {
  return { ...base, notice: code === null ? null : { code, params } };
}

/** The site fixture (area SE4, "Malmö") with the given `prices.provider`. */
function dashboard(block: unknown): Record<string, any> {
  const payload = read("dashboard", "cheapest_direct_site_admin.json");
  payload["prices"]["provider"] = block;
  return payload;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function open(
  payload: Record<string, any>,
  options: { components?: string[]; language?: Language } = {},
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  hass.entityHandler = async () => read("entity_config", "v1", "get_direct.json");
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", options.language ?? "en");
  snapshot.user = { is_admin: true };
  (snapshot as unknown as { config: unknown }).config = { components: options.components ?? [] };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate(options.language ?? "en", "header.settings")}"]`)
    ?.click();
  await settle();
  return { hass, element };
}

function openDialog(element: Element): HTMLElement {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null)!;
}
const priceSection = (element: Element) => openDialog(element).querySelector<HTMLElement>("[data-section='market']")!;
const notice = (element: Element) => priceSection(element).querySelector<HTMLElement>("[data-price-notice]");
const labels = (node: HTMLElement | null) =>
  Array.from(node?.querySelectorAll(".spotnav-choice-label") ?? []).map((label) => label.textContent);
const action = (node: HTMLElement, name: string) => node.querySelector<HTMLButtonElement>(`[data-price-action="${name}"]`)!;
const sent = (hass: FakeHass, type: string) => hass.messages.filter((m) => m["type"] === type);

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

describe("the prices.provider block", () => {
  it("is read with its notice and parameters", () => {
    const decoded = decodeDashboard(dashboard(provider(RELAY, "octopus_region_mismatch", { region: "A" })));
    expect(decoded.ok && decoded.value.prices.provider).toEqual({
      ...RELAY,
      notice: { code: "octopus_region_mismatch", params: { region: "A" } },
    });
    const none = decodeDashboard(dashboard(provider(NONE, "add_entsoe_key")));
    expect(none.ok && none.value.prices.provider).toEqual({ ...NONE, notice: { code: "add_entsoe_key", params: {} } });
  });

  it("is null, not a refused dashboard, when it is absent or cannot be read", () => {
    const absent = read("dashboard", "cheapest_direct_site_admin.json");
    delete absent["prices"]["provider"];
    for (const payload of [
      absent,
      dashboard(null),
      dashboard("relay"),
      dashboard({ ...provider(RELAY, null), extra: true }),
      dashboard({ ...RELAY }),
      dashboard(provider(RELAY, "add_nordpool", { region: 3 } as unknown as Record<string, string>)),
      dashboard({ ...RELAY, notice: { code: "add_nordpool" } }),
      dashboard({ ...RELAY, key: 4, notice: null }),
    ]) {
      const decoded = decodeDashboard(payload);
      expect(decoded.ok).toBe(true);
      expect(decoded.ok && decoded.value.prices.provider).toBeNull();
    }
  });

  it("keeps a name but drops an address that is not http(s)", () => {
    expect(priceProviderOrNull({ key: "nordpool", name: "Nord Pool", url: "javascript:alert(1)", notice: null })).toEqual({
      key: "nordpool",
      name: "Nord Pool",
      url: null,
      notice: null,
    });
  });

  it("is in every committed dashboard fixture", () => {
    const decoded = decodeDashboard(read("dashboard", "proposal_ready.json"));
    expect(decoded.ok && decoded.value.prices.provider?.key).toBe("relay");
  });
});

describe("the source line", () => {
  it.each([
    ["nordpool", "Prices from Nord Pool (via Home Assistant)"],
    ["entsoe", "Prices from ENTSO-E (your key)"],
    ["pvpc", "Prices from PVPC (Home Assistant)"],
    ["octopus", "Prices from Octopus Energy (your Agile tariff)"],
    ["relay", "Prices from the SpotNav relay"],
  ])("names %s", async (key, text) => {
    const { element } = await open(dashboard(provider({ key, name: "x", url: "https://example.org/" }, null)));
    const line = priceSection(element).querySelector<HTMLElement>("[data-price-provider]");
    expect(line?.textContent).toBe(text);
    expect(line?.dataset["priceProvider"]).toBe(key);
    expect(notice(element)).toBeNull();
  });

  it("is absent with no source, and nothing is shown without the block", async () => {
    const { element } = await open(dashboard(provider(NONE, "source_missing")));
    expect(priceSection(element).querySelector("[data-price-provider]")).toBeNull();
    expect(notice(element)?.textContent).toContain("There is no source for prices in Malmö · SE4");
    const plain = await open(dashboard(null));
    expect(priceSection(plain.element).querySelector("[data-price-provider]")).toBeNull();
    expect(notice(plain.element)).toBeNull();
  });

  it("speaks Swedish", async () => {
    const { element } = await open(dashboard(provider(RELAY, "add_nordpool")), { language: "sv" });
    await settle();
    expect(priceSection(element).querySelector("[data-price-provider]")?.textContent).toBe("Priser från SpotNav-reläet");
    expect(labels(notice(element))).toEqual(["Lägg till Nord Pool", "Använd min ENTSO-E-nyckel i stället"]);
  });
});

describe("the notices", () => {
  it("add_nordpool opens Home Assistant's own flow for Nord Pool and offers the key", async () => {
    const { element } = await open(dashboard(provider(RELAY, "add_nordpool")));
    const box = notice(element)!;
    expect(box.dataset["priceNotice"]).toBe("add_nordpool");
    expect(box.querySelector(".spotnav-suggestion-text")?.textContent).toBe(
      "Get the prices for Malmö · SE4 through Home Assistant's built-in Nord Pool integration. The area you pick when adding it doesn't matter – SpotNav fetches the one it needs.",
    );
    expect(labels(box)).toEqual(["Add Nord Pool", "Use my ENTSO-E key instead"]);
    const changed = vi.fn();
    window.addEventListener("location-changed", changed);
    action(box, addIntegrationPath("nordpool")).click();
    window.removeEventListener("location-changed", changed);
    expect(pushed).toEqual(["/config/integrations/dashboard/add?domain=nordpool"]);
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["nordpool_not_loaded", ["Open Nord Pool", "Use my ENTSO-E key instead"], integrationPath("nordpool")],
    ["add_pvpc", ["Add PVPC"], addIntegrationPath("pvpc_hourly_pricing")],
    ["pvpc_not_loaded", ["Open PVPC"], integrationPath("pvpc_hourly_pricing")],
    ["octopus_not_loaded", ["Open Octopus Energy"], integrationPath("octopus_energy")],
    ["octopus_no_agile", ["Open Octopus Energy"], integrationPath("octopus_energy")],
  ])("%s opens its page", async (code, buttons, path) => {
    const { element } = await open(dashboard(provider(RELAY, code)));
    const box = notice(element)!;
    expect(labels(box)).toEqual(buttons);
    action(box, path).click();
    expect(pushed).toEqual([path]);
  });

  it("the integrations' pages are Home Assistant's own routes", () => {
    expect(addIntegrationPath("pvpc_hourly_pricing")).toBe("/config/integrations/dashboard/add?domain=pvpc_hourly_pricing");
    expect(integrationPath("octopus_energy")).toBe("/config/integrations/integration/octopus_energy");
  });

  it("add_octopus opens HACS when it is there, else the repository's own page", async () => {
    const without = await open(dashboard(provider(NONE, "add_octopus")));
    expect(labels(notice(without.element))).toEqual(["Add Octopus Energy (HACS)"]);
    action(notice(without.element)!, "octopus").click();
    expect(opened).toEqual([OCTOPUS_REPOSITORY_URL]);
    expect(pushed).toEqual([]);

    const withHacs = await open(dashboard(provider(NONE, "add_octopus")), { components: ["hacs"] });
    action(notice(withHacs.element)!, "octopus").click();
    expect(pushed).toEqual([OCTOPUS_HACS_PATH]);
  });

  it("the region mismatch names the meter's region and opens the area", async () => {
    const { hass, element } = await open(
      dashboard(provider({ key: "octopus", name: "Octopus Energy (Agile)", url: "https://octopus.energy/" }, "octopus_region_mismatch", { region: "A" })),
    );
    const box = notice(element)!;
    expect(box.textContent).toContain("Your Octopus meter is in region A; its rates are used for Malmö · SE4.");
    const before = hass.messages.length;
    action(box, "area").click();
    await settle();
    expect(hass.messages.length).toBeGreaterThan(before);
  });

  it("add_entsoe_key and entsoe_key_rejected open the key's editor", async () => {
    const first = await open(dashboard(provider(NONE, "add_entsoe_key")));
    expect(notice(first.element)?.textContent).toContain("Prices for Malmö · SE4 come from ENTSO-E with your own free key.");
    expect(labels(notice(first.element))).toEqual(["Add your ENTSO-E key"]);
    const rejected = await open(dashboard(provider(NONE, "entsoe_key_rejected")));
    expect(labels(notice(rejected.element))).toEqual(["Enter a new key"]);
  });

  it("an unknown notice is not shown", async () => {
    const { element } = await open(dashboard(provider(RELAY, "something_new")));
    expect(notice(element)).toBeNull();
    expect(priceSection(element).querySelector("[data-price-provider]")).not.toBeNull();
  });

  it("a reader who is not an administrator gets the sentence without its buttons", () => {
    const nodes = priceProviderNodes(document, {
      language: "en",
      provider: { ...RELAY, notice: { code: "add_nordpool", params: {} } } as PriceProvider,
      areaLabel: "SE4",
      areaId: "SE4",
      isAdmin: false,
      idPrefix: "t",
      hasComponent: () => false,
      navigate: () => undefined,
      openExternal: () => undefined,
      openArea: () => undefined,
      openKeyEditor: () => undefined,
    });
    expect(nodes.map((node) => node.dataset["priceProvider"] ?? node.dataset["priceNotice"])).toEqual(["relay", "add_nordpool"]);
    expect(nodes[1]!.querySelector("button")).toBeNull();
  });

  it("puts the platform's link in every language's how-to", () => {
    for (const language of LANGUAGES) {
      expect((TRANSLATIONS[language] as Record<string, string>)["price.key.howto"], language).toContain("{site}");
    }
  });

  it("has its words in every language", () => {
    const keys = Object.keys(TRANSLATIONS.en).filter((key) => key.startsWith("price.notice.") || key.startsWith("price.key.") || key.startsWith("price.provider."));
    expect(keys.length).toBe(32);
    for (const language of LANGUAGES) {
      const words = TRANSLATIONS[language] as Record<string, string>;
      for (const key of keys) {
        expect(words[key], `${language} ${key}`).toBeTruthy();
      }
    }
  });
});

describe("the ENTSO-E key's editor", () => {
  async function editor(code = "add_entsoe_key") {
    const opened = await open(dashboard(provider(NONE, code)));
    action(notice(opened.element)!, "entsoe-key").click();
    await settle();
    const form = openDialog(opened.element).querySelector<HTMLFormElement>("[data-value-editor='entsoeKey']")!;
    return { ...opened, form };
  }

  it("says how to get a key and keeps the field a password", async () => {
    const { form } = await editor();
    const field = form.querySelector<HTMLInputElement>("[data-value-field='entsoeKey']")!;
    expect(field.type).toBe("password");
    expect(field.value).toBe("");
    expect(form.textContent).toContain("transparency@entsoe.eu");
    expect(form.textContent).toContain("Restful API access");
    expect(form.textContent).toContain("Register at transparency.entsoe.eu, then e-mail");
    const link = form.querySelector<HTMLAnchorElement>("a[href='https://transparency.entsoe.eu/']")!;
    expect(link.textContent).toBe("transparency.entsoe.eu");
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noopener noreferrer");
  });

  it("asks for a key before sending anything", async () => {
    const { hass, form } = await editor();
    form.querySelector<HTMLButtonElement>("button[type='submit']")!.click();
    await settle();
    expect(sent(hass, "spotnav/set_entsoe_key")).toEqual([]);
    expect(form.querySelector("[role='alert']")?.textContent).toBe("Enter the key first.");
  });

  it("sends the key for the area, keeps the editor open on a rejection and reads the dashboard after it took", async () => {
    const { hass, form } = await editor();
    const field = form.querySelector<HTMLInputElement>("[data-value-field='entsoeKey']")!;
    field.value = "  the-key \n";
    form.querySelector<HTMLButtonElement>("button[type='submit']")!.click();
    await settle();
    expect(sent(hass, "spotnav/set_entsoe_key")).toEqual([
      { type: "spotnav/set_entsoe_key", key: "the-key", area_id: "SE4" },
    ]);
    hass.resolveNext({ ok: false, error: "entsoe_key_rejected", state: null });
    await settle();
    expect(form.querySelector("[role='alert']")?.textContent).toBe(translate("en", "price.key.rejected"));
    expect(sent(hass, "spotnav/get_dashboard").length).toBe(1);

    form.querySelector<HTMLButtonElement>("button[type='submit']")!.click();
    await settle();
    hass.resolveNext({ ok: true, error: null, state: "ok" });
    await settle();
    expect(sent(hass, "spotnav/get_dashboard").length).toBe(2);
  });

  it("takes a key the platform could not be asked about (it is kept and proved later)", async () => {
    const { hass, form } = await editor("entsoe_key_rejected");
    form.querySelector<HTMLInputElement>("[data-value-field='entsoeKey']")!.value = "another";
    form.querySelector<HTMLButtonElement>("button[type='submit']")!.click();
    await settle();
    hass.resolveNext({ ok: false, error: "entsoe_unreachable", state: "unchecked" });
    await settle();
    expect(sent(hass, "spotnav/get_dashboard").length).toBe(2);
  });
});
