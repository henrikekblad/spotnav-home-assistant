// The year review in the cards: one read per mount, the band under the header of the house card and the charger card
// while the answer says `banner`, opening it says it was viewed and its cross that it was dismissed (and the band is
// gone), and the charger card's Settings → Support row while the answer says `row`.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HOUSE_CARD_ELEMENT, SpotnavHouseCard } from "../src/house-card";
import { translate } from "../src/i18n";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function read(...parts: string[]): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, unknown>;
}

const YEAR = (overrides: Record<string, unknown> = {}) => ({ ...read("year_review", "v1", "full_year.json"), ...overrides });

function answering(hass: FakeHass, answer: Record<string, unknown>): void {
  hass.yearReviewHandler = async (message) => (message["type"] === "spotnav/get_year_review" ? answer : { ok: true });
}

const seen = (hass: FakeHass) => hass.yearReviewMessages.filter((message) => message["type"] === "spotnav/year_review_seen");

describe("the year review's band in the house card", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: () => undefined, removeEventListener: () => undefined }));
    if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
      customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
    }
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function mount(hass: FakeHass): Promise<ShadowRoot> {
    const element = document.createElement(HOUSE_CARD_ELEMENT) as SpotnavHouseCard;
    element.setConfig({ type: "custom:spotnav-house-card", site: "site_villa" });
    element.hass = hass.snapshot("s", "sv");
    document.body.append(element);
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("house", "villa_battery_sun.json"));
    await vi.advanceTimersByTimeAsync(0);
    return element.shadowRoot!;
  }

  it("asks once, and shows the band under the header while the answer says so", async () => {
    const hass = new FakeHass();
    answering(hass, YEAR());
    const root = await mount(hass);
    expect(hass.yearReviewMessages).toEqual([{ type: "spotnav/get_year_review", api_version: 1, site_id: "site_villa" }]);
    const band = root.querySelector<HTMLElement>("[data-year-banner]")!;
    expect(band).not.toBeNull();
    expect(band.previousElementSibling?.querySelector(".spotnav-header")).not.toBeNull();
    expect(band.textContent).toContain(translate("sv", "yearReview.banner.title"));
    expect(band.textContent).toContain("Se vad 2026 gav – tar bara en minut");
    expect(band.textContent).not.toContain("GitHub");
    await vi.advanceTimersByTimeAsync(300_000);
    expect(hass.yearReviewMessages).toHaveLength(1);
  });

  it("opens the story from the band, says it was viewed, and the band is gone for good", async () => {
    const hass = new FakeHass();
    answering(hass, YEAR());
    const root = await mount(hass);
    root.querySelector<HTMLButtonElement>("[data-year-banner-open]")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(seen(hass)).toEqual([{ type: "spotnav/year_review_seen", api_version: 1, site_id: "site_villa", year: 2026, action: "viewed" }]);
    const story = root.querySelector<HTMLElement>(".spotnav-yr-story")!;
    expect(story.closest("[hidden]")).toBeNull();
    expect(root.querySelector("[data-year-banner]")).toBeNull();
    story.querySelector<HTMLButtonElement>("[data-nav='close']")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(story.closest("[hidden]")).not.toBeNull();
    expect(root.querySelector("[data-year-banner]")).toBeNull();
    expect(seen(hass)).toHaveLength(1);
  });

  it("closes the band with its cross, saying it was dismissed, without opening the story", async () => {
    const hass = new FakeHass();
    answering(hass, YEAR());
    const root = await mount(hass);
    root.querySelector<HTMLButtonElement>("[data-year-banner-close]")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(seen(hass)).toEqual([{ type: "spotnav/year_review_seen", api_version: 1, site_id: "site_villa", year: 2026, action: "dismissed" }]);
    expect(root.querySelector("[data-year-banner]")).toBeNull();
    expect(root.querySelector(".spotnav-yr-story")).toBeNull();
  });

  it("shows no band once seen, outside the window, or against a backend without the command", async () => {
    for (const answer of [YEAR({ banner: false }), read("year_review", "v1", "state_none.json")]) {
      document.body.innerHTML = "";
      const hass = new FakeHass();
      answering(hass, answer);
      expect((await mount(hass)).querySelector("[data-year-banner]")).toBeNull();
    }
    document.body.innerHTML = "";
    const old = new FakeHass();
    old.yearReviewHandler = async () => {
      throw Object.assign(new Error("unknown"), { code: "unknown_command" });
    };
    expect((await mount(old)).querySelector("[data-year-banner]")).toBeNull();
  });
});

describe("the year review in the charger card", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: () => undefined, removeEventListener: () => undefined }));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function mounted(answer: Record<string, unknown>) {
    const hass = new FakeHass();
    answering(hass, answer);
    const element = mountCard({ type: "custom:spotnav-card", charger: "entry_a" }, hass);
    const snapshot = hass.snapshot("snapshot", "en");
    snapshot.user = { is_admin: true };
    element.hass = snapshot;
    await vi.advanceTimersByTimeAsync(0);
    hass.resolveNext(read("dashboard", "start_idle.json"));
    await vi.advanceTimersByTimeAsync(0);
    return { hass, element, root: element.shadowRoot! };
  }

  it("asks by the charger, and shows the band under the header", async () => {
    const { hass, root } = await mounted(YEAR());
    expect(hass.yearReviewMessages[0]).toEqual({ type: "spotnav/get_year_review", api_version: 1, charger_id: "entry_a" });
    const band = root.querySelector<HTMLElement>("[data-year-banner]")!;
    expect(band).not.toBeNull();
    expect(band.previousElementSibling?.classList.contains("spotnav-header")).toBe(true);
    expect(band.textContent).toContain(translate("en", "yearReview.banner.title"));
    root.querySelector<HTMLButtonElement>("[data-year-banner-open]")!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(seen(hass)[0]).toMatchObject({ site_id: "site_villa", year: 2026, action: "viewed" });
    expect(root.querySelector(".spotnav-yr-story")!.closest("[hidden]")).toBeNull();
    // A refresh while the story is open waits for it to close.
    await vi.advanceTimersByTimeAsync(30_000);
    hass.resolveNext(read("dashboard", "start_idle.json"));
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector(".spotnav-yr-story")!.closest("[hidden]")).toBeNull();
  });

  it("offers the story under Settings → Support before the sponsor row, and closes Settings for it", async () => {
    const { hass, root } = await mounted(YEAR({ banner: false }));
    expect(root.querySelector("[data-year-banner]")).toBeNull();
    root.querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    const rows = [...root.querySelectorAll<HTMLElement>("[data-section='support'] [data-row]")].map((row) => row.dataset["row"]);
    expect(rows.slice(-2)).toEqual(["year_review", "sponsor"]);
    root.querySelector<HTMLButtonElement>("[data-row='year_review'] button")!.click();
    await vi.advanceTimersByTimeAsync(0);
    const story = root.querySelector<HTMLElement>(".spotnav-yr-story")!;
    expect(story.closest("[hidden]")).toBeNull();
    expect(root.querySelector("[data-section='support']")?.closest("[hidden]")).not.toBeNull();
    expect(seen(hass)).toEqual([]);
  });

  it("has no band and no row outside the window", async () => {
    const { root } = await mounted(read("year_review", "v1", "state_none.json"));
    expect(root.querySelector("[data-year-banner]")).toBeNull();
    root.querySelector<HTMLButtonElement>(`[aria-label="${translate("en", "header.settings")}"]`)!.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector("[data-row='year_review']")).toBeNull();
  });
});
