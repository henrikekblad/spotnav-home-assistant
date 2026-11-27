// The year review's story: its pages in words from the backend's own fixtures, the story dialog (pages, keys, swipe,
// reduced motion, every frame loop stopped), and the picture to share (its layout as pure functions, the canvas a fake).

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import { TRANSLATIONS } from "../src/i18n/all";
import { ART, artDataUrl, artElement } from "../src/year-review-art";
import { countUp, easeOut, Fireworks, type FrameClock } from "../src/year-review-fx";
import { decodeYearReview, type YearReview } from "../src/year-review-model";
import { storyPages, type StoryPage } from "../src/year-review-pages";
import {
  drawShare,
  fitFontSize,
  shareReview,
  shareSubtitle,
  shareTiles,
  SHARE_HEIGHT,
  SHARE_WIDTH,
  tileBoxes,
  wrapLines,
} from "../src/year-review-share";
import { YearReviewStory } from "../src/year-review-view";

/** Text with Intl's narrow and no-break spaces as plain ones (Swedish groups thousands with them). */
const plain = (text: string): string => text.replace(/[\u00a0\u202f]/g, " ");
const plainAll = (texts: string[]): string[] => texts.map(plain);

const DIR = join(__dirname, "..", "..", "tests", "fixtures", "year_review", "v1");

function review(name: string): YearReview {
  const decoded = decodeYearReview(JSON.parse(readFileSync(join(DIR, name), "utf8")));
  if (!decoded.ok || decoded.value.review === null) {
    throw new Error(`${name} must decode with a review`);
  }
  return decoded.value.review;
}

function pagesOf(name: string, language: (typeof LANGUAGES)[number] = "sv", reachable = true): StoryPage[] {
  return storyPages(language, review(name), { supportReachable: reachable });
}

function find(pages: StoryPage[], id: string): StoryPage {
  const found = pages.find((entry) => entry.id === id);
  if (found === undefined) {
    throw new Error(`no ${id} page`);
  }
  return found;
}

/** Every word a page says, joined. */
function words(entry: StoryPage): string {
  const big = typeof entry.big === "string" ? entry.big : entry.big === null ? "" : `${entry.big.approx ? "≈ " : ""}${entry.big.value} ${entry.big.unit}`;
  return [entry.eyebrow, big, entry.say ?? "", ...entry.chips, ...entry.foot, entry.badge ?? ""].join(" | ");
}

/** A frame clock the test turns by hand. */
class ManualClock implements FrameClock {
  time = 0;
  next = 1;
  readonly waiting = new Map<number, (time: number) => void>();
  requested = 0;
  request(callback: (time: number) => void): number {
    this.requested += 1;
    const handle = this.next++;
    this.waiting.set(handle, callback);
    return handle;
  }
  cancel(handle: number): void {
    this.waiting.delete(handle);
  }
  now(): number {
    return this.time;
  }
  tick(ms: number): void {
    this.time += ms;
    const due = [...this.waiting.entries()];
    this.waiting.clear();
    for (const [, callback] of due) {
      callback(this.time);
    }
  }
}

/** A 2D context that records what is drawn and measures text at 0.5 px per character per px of font size. */
function fakeContext() {
  const texts: { text: string; x: number; y: number; font: string }[] = [];
  const images: { x: number; y: number; w: number; h: number }[] = [];
  const context = {
    font: "10px sans-serif",
    fillStyle: "#000",
    globalAlpha: 1,
    shadowColor: "",
    shadowBlur: 0,
    shadowOffsetY: 0,
    textBaseline: "alphabetic",
    imageSmoothingEnabled: true,
    imageSmoothingQuality: "low",
    createLinearGradient: () => ({ addColorStop: () => undefined }),
    fillRect: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    roundRect: vi.fn(),
    setTransform: vi.fn(),
    drawImage: (_image: unknown, x: number, y: number, w: number, h: number) => images.push({ x, y, w, h }),
    measureText(text: string) {
      const size = Number(/(\d+)px/.exec(this.font)?.[1] ?? 10);
      return { width: text.length * size * 0.5 };
    },
    fillText(text: string, x: number, y: number) {
      texts.push({ text, x, y, font: this.font });
    },
  };
  return { context, texts, images };
}

function reducedMotionIs(on: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: on && query.includes("reduce"), media: query, addEventListener: () => undefined, removeEventListener: () => undefined }));
}

describe("the pages in words", () => {
  it("are the backend's pages in its order, and say the year", () => {
    const pages = pagesOf("full_year.json");
    expect(pages.map((entry) => entry.id)).toEqual(["cover", "saved", "car", "sun", "battery", "fuse", "expensive", "thanks"]);
    expect(find(pages, "cover").big).toBe("Ditt 2026");
    expect(find(pages, "cover").badge).toBeNull();
    expect(find(pages, "cover").say).toBe(translate("sv", "yearReview.cover.say"));
  });

  it("leave out what the backend left out: the fuse in a car-only home, the car without one", () => {
    expect(pagesOf("car_only.json").map((entry) => entry.id)).toEqual(["cover", "saved", "car", "expensive", "thanks"]);
    expect(pagesOf("sun_battery_no_car.json").map((entry) => entry.id)).not.toContain("car");
    expect(pagesOf("fresh_install.json").map((entry) => entry.id)).not.toContain("fuse");
  });

  it("say since when on a part of the year, with the badge", () => {
    const cover = find(pagesOf("fresh_install.json"), "cover");
    expect(cover.say).toBe(translate("sv", "yearReview.cover.saySince", { date: "20 november" }));
    expect(cover.badge).toBe("Delvis år");
    const car = find(pagesOf("fresh_install.json"), "car");
    expect(car.foot).toContain("Räknat sedan 21 november, så långt priserna räcker.");
    const expensive = find(pagesOf("nord_pool_55_days.json"), "expensive");
    expect(expensive.foot).toContain("Sedan 21 oktober.");
  });

  it("mark an estimated figure ≈, and only that", () => {
    const saved = find(pagesOf("full_year.json"), "saved");
    expect(saved.big).toEqual({ value: 1483.4, digits: 0, unit: "kr", approx: true });
    expect(saved.foot).toEqual([translate("sv", "yearReview.saved.footEstimated")]);
    expect(find(pagesOf("full_year.json"), "car").big).toMatchObject({ value: 86, unit: "%", approx: false });
    expect(find(pagesOf("fresh_install.json"), "car").big).toMatchObject({ approx: true });
    expect(find(pagesOf("sun_battery_no_car.json"), "saved").big).toMatchObject({ approx: false });
    expect(find(pagesOf("sun_battery_no_car.json"), "saved").foot).toEqual([translate("sv", "yearReview.saved.foot")]);
  });

  it("break the saving down by part with each part's start", () => {
    const saved = find(pagesOf("full_year.json"), "saved");
    expect(plainAll(saved.chips)).toEqual(["Batteriet 305 kr sedan 15 oktober", "Bilen 1 156 kr", "Värmen 22 kr sedan 1 november"]);
  });

  it("word the car's charges, energy and cheapest charge, with the site's fees", () => {
    const car = find(pagesOf("full_year.json"), "car");
    expect(plainAll(car.chips)).toEqual(["209 laddningar", "2 438 kWh", "billigast 0,50 kr/kWh, 1 januari"]);
    expect(car.meter).toBe(86);
    expect(car.foot).toContain(translate("sv", "yearReview.fees"));
  });

  it("show a car without a cheap share as its charges and energy, with no big figure", () => {
    const source = review("car_only.json");
    const pages = source.pages.map((entry) => (entry.id === "car" ? { ...entry, cheap_share_pct: null } : entry));
    const car = find(storyPages("sv", { ...source, pages }, { supportReachable: true }), "car");
    expect(car.big).toBeNull();
    expect(car.meter).toBeNull();
    expect(car.say).toBe("Bilen laddade hemma i år.");
    expect(plainAll(car.chips.slice(0, 2))).toEqual(["209 laddningar", "2 438 kWh"]);
  });

  it("say the rest was sold only where export is paid", () => {
    const source = review("full_year.json");
    const sun = (paid: boolean | null) =>
      find(storyPages("sv", { ...source, pages: source.pages.map((entry) => (entry.id === "sun" ? { ...entry, export_paid: paid } : entry)) }, { supportReachable: true }), "sun").say;
    expect(sun(true)).toBe("kom från dina solceller. 92 % användes hemma, resten såldes.");
    expect(sun(false)).toBe("kom från dina solceller. 92 % användes hemma, resten gick ut på nätet.");
    expect(sun(null)).toBe("kom från dina solceller. 92 % användes hemma, resten gick ut på nätet.");
    expect(sun(false)).not.toContain("såld");
  });

  it("show the battery's money from its own count, and its energy before one", () => {
    const battery = find(pagesOf("full_year.json"), "battery");
    expect(battery.big).toMatchObject({ value: 366, unit: "kr" });
    expect(battery.chips).toEqual(["≈ 104 varv", "köpte för 0,62 kr/kWh", "använde vid 2,00 kr/kWh"]);
    expect(battery.foot).toEqual(["Pengarna räknas från granskade dagar sedan 15 oktober."]);
    const source = review("full_year.json");
    const early = source.pages.map((entry) =>
      entry.id === "battery" ? { ...entry, earned: null, earned_since: null, saved: null, buy_price: null, use_price: null } : entry,
    );
    const kwh = find(storyPages("sv", { ...source, pages: early }, { supportReachable: true }), "battery");
    expect(kwh.big).toMatchObject({ value: 1044, unit: "kWh" });
    expect(plainAll(kwh.chips)).toEqual(["≈ 104 varv", "laddade 1 044 kWh"]);
    expect(words(kwh)).not.toContain("kr");
    const none = source.pages.map((entry) => (entry.id === "battery" ? { ...entry, earned: null, saved: null, kwh_out: null, kwh_in: null, cycles: null } : entry));
    expect(storyPages("sv", { ...source, pages: none }, { supportReachable: true }).map((entry) => entry.id)).not.toContain("battery");
  });

  it("show what a battery SpotNav does not control saved at each hour's price, marked ≈", () => {
    const battery = find(pagesOf("inverter_battery.json"), "battery");
    expect(battery.big).toEqual({ value: 2175, digits: 0, unit: "kr", approx: true });
    expect(battery.say).toBe("sparade batteriet på elräkningen i år.");
    expect(plainAll(battery.chips)).toEqual(["≈ 104 varv", "laddade 1 044 kWh", "gav tillbaka 1 044 kWh"]);
    expect(battery.foot).toEqual(["≈ Räknat på varje timmes pris, oavsett vem som styrde batteriet."]);
    const source = review("inverter_battery.json");
    const later = source.pages.map((entry) => (entry.id === "battery" ? { ...entry, saved_since: "2026-10-21" } : entry));
    expect(find(storyPages("sv", { ...source, pages: later }, { supportReachable: true }), "battery").foot).toContain("Sedan 21 oktober.");
    // SpotNav's own count wins where it has one.
    expect(find(pagesOf("full_year.json"), "battery").big).toMatchObject({ value: 366, approx: false });
    for (const language of LANGUAGES) {
      const words = find(pagesOf("inverter_battery.json", language), "battery");
      expect(words.foot[0]?.startsWith("≈ ")).toBe(true);
      expect(words.say).not.toMatch(/\b(smart|AI|we|us|vi|oss)\b/i);
    }
  });

  it("word the most expensive hour with its day, its hour and what was bought then", () => {
    const expensive = find(pagesOf("full_year.json"), "expensive");
    expect(expensive.big).toEqual({ value: 7.93, digits: 2, unit: "kr/kWh", approx: false });
    expect(expensive.say).toBe("28 januari kl. 08:00. Då köpte du bara 0,3 kWh.");
    const source = review("full_year.json");
    const bought = (kwh: number, language: (typeof LANGUAGES)[number] = "sv"): string | null => {
      const pages = source.pages.map((entry) => (entry.id === "expensive" ? { ...entry, bought_kwh: kwh } : entry));
      return find(storyPages(language, { ...source, pages }, { supportReachable: true }), "expensive").say;
    };
    expect(bought(0)).toBe("28 januari kl. 08:00. Då köpte du ingen el från nätet.");
    expect(bought(0.04)).toBe("28 januari kl. 08:00. Då köpte du ingen el från nätet.");
    expect(bought(0.05)).toBe("28 januari kl. 08:00. Då köpte du bara 0,1 kWh.");
    expect(bought(2.4)).toBe("28 januari kl. 08:00. Då köpte du 2,4 kWh.");
    expect(bought(0, "en")).toBe("28 January at 08:00. You bought no electricity from the grid then.");
    expect(find(pagesOf("full_year.json", "en"), "expensive").eyebrow).toBe("The most expensive hour");
  });

  it("ask for support on the thanks page only, and say the story stays under Support where the reader can reach it", () => {
    for (const language of LANGUAGES) {
      const pages = pagesOf("full_year.json", language);
      for (const entry of pages.filter((page) => page.id !== "thanks")) {
        expect(words(entry)).not.toContain("GitHub");
      }
    }
    const thanks = find(pagesOf("full_year.json"), "thanks");
    expect(thanks.foot).toEqual(["Berättelsen finns kvar under Inställningar → Support till 31 januari."]);
    expect(words(thanks)).not.toContain("stänga av");
    expect(find(pagesOf("full_year.json", "sv", false), "thanks").foot).toEqual([]);
  });

  it("fill every placeholder in every language", () => {
    for (const language of LANGUAGES) {
      for (const name of readdirSync(DIR).filter((file) => file !== "state_none.json")) {
        for (const entry of pagesOf(name, language)) {
          expect(words(entry), `${language} ${name} ${entry.id}`).not.toMatch(/\{\w+\}/);
        }
      }
    }
  });
});

describe("the copy", () => {
  it("never says smart, AI, dear, or we and us", () => {
    const banned: Record<string, RegExp> = {
      en: /\b(smart|AI|dear\w*|we|us|our)\b/i,
      sv: /\b(smart|AI|vi|oss|vår|våra|avgång)\b/i,
      nb: /\b(smart|AI|vi|oss|vår)\b/i,
      da: /\b(smart|AI|vi|os|vores)\b/i,
      fi: /\b(älykäs|AI|me|meidän)\b/i,
      de: /\b(smart|KI|AI|wir|uns|unser)\b/i,
      nl: /\b(slim|AI|wij|we|ons|onze)\b/i,
      fr: /\b(intelligent|IA|AI|nous|notre)\b/i,
      es: /\b(inteligente|IA|AI|nosotros|nuestro)\b/i,
    };
    for (const language of LANGUAGES) {
      const copy = Object.entries(TRANSLATIONS[language]).filter(([key]) => key.startsWith("yearReview."));
      expect(copy.length).toBeGreaterThan(40);
      for (const [key, text] of copy) {
        expect(text, `${language} ${key}`).not.toMatch(banned[language]!);
      }
    }
  });
});

describe("the illustrations", () => {
  it("are one per page, parsed from constant text, with no text on the coins", () => {
    for (const id of Object.keys(ART) as (keyof typeof ART)[]) {
      const svg = artElement(document, id);
      expect(svg?.localName, id).toBe("svg");
      expect(svg?.getAttribute("aria-hidden")).toBe("true");
    }
    expect(ART.saved).not.toContain("<text");
    expect(artElement(document, "saved")!.querySelectorAll("ellipse").length).toBe(8);
  });

  it("are drawn on the picture from a data URL at 600 × 600", () => {
    const url = artDataUrl("sun");
    expect(url.startsWith("data:image/svg+xml;charset=utf-8,")).toBe(true);
    const svg = decodeURIComponent(url.slice(url.indexOf(",") + 1));
    expect(svg).toContain('width="600" height="600"');
    expect(svg).not.toContain("foreignObject");
  });
});

describe("the motion", () => {
  it("counts a figure up over 900 ms, eased out, and ends on the figure", () => {
    const clock = new ManualClock();
    const node = document.createTextNode("");
    countUp(node, 1000, (value) => String(Math.round(value)), clock, false);
    expect(node.data).toBe("0");
    clock.tick(450);
    expect(Number(node.data)).toBe(Math.round(1000 * easeOut(0.5)));
    clock.tick(500);
    expect(node.data).toBe("1000");
    expect(clock.waiting.size).toBe(0);
  });

  it("writes the figure at once and asks for no frame under reduced motion", () => {
    const clock = new ManualClock();
    const node = document.createTextNode("");
    countUp(node, 1000, (value) => String(Math.round(value)), clock, true);
    expect(node.data).toBe("1000");
    expect(clock.requested).toBe(0);
  });

  it("starts no fireworks under reduced motion, and stops them on stop", () => {
    const { context } = fakeContext();
    const canvas = document.createElement("canvas");
    vi.spyOn(canvas, "getContext").mockReturnValue(context as never);
    const clock = new ManualClock();
    const still = new Fireworks(canvas, clock, true);
    still.start();
    expect(still.running).toBe(false);
    expect(clock.requested).toBe(0);
    const moving = new Fireworks(canvas, clock, false, () => 0.5);
    moving.start();
    expect(moving.running).toBe(true);
    clock.tick(16);
    expect(context.fillRect).toHaveBeenCalled();
    moving.stop();
    expect(moving.running).toBe(false);
    expect(clock.waiting.size).toBe(0);
  });
});

describe("the story", () => {
  let clock: ManualClock;
  let host: HTMLElement;

  beforeEach(() => {
    document.body.innerHTML = "";
    clock = new ManualClock();
    host = document.createElement("div");
    document.body.append(host);
    reducedMotionIs(false);
    const { context } = fakeContext();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function open(name = "full_year.json", share?: () => Promise<never>) {
    const onClose = vi.fn();
    const story = new YearReviewStory({
      doc: document,
      owner: host,
      idPrefix: "t",
      language: () => "sv",
      supportReachable: true,
      clock,
      onClose,
      ...(share === undefined ? {} : { share }),
    });
    story.open(review(name));
    const dialog = host.querySelector<HTMLElement>("[role='dialog']")!;
    return { story, dialog, onClose };
  }

  const panelText = (dialog: HTMLElement): string => dialog.querySelector("[data-panel]")!.textContent ?? "";

  it("opens on the cover with fireworks, its words read out, Next focused and Back disabled", () => {
    const { story, dialog } = open();
    expect(story.page).toBe(0);
    expect(dialog.querySelector("[data-panel]")!.getAttribute("aria-live")).toBe("polite");
    expect(panelText(dialog)).toContain("Ditt 2026");
    expect(story.animating).toBe(true);
    expect(dialog.querySelector<HTMLButtonElement>("[data-nav='back']")!.disabled).toBe(true);
    expect(host.ownerDocument.activeElement).toBe(dialog.querySelector("[data-nav='next']"));
    expect(dialog.querySelectorAll(".spotnav-yr-bar")).toHaveLength(8);
    expect(dialog.querySelectorAll(".spotnav-yr-bar[data-on]")).toHaveLength(1);
  });

  it("draws each page's picture above its words in one block, and each nav label in its own centred line", () => {
    const { story, dialog } = open();
    for (let k = 0; k < story.pageIds.length; k += 1) {
      story.go(k);
      const panel = dialog.querySelector<HTMLElement>("[data-panel]")!;
      expect(panel.children).toHaveLength(1);
      const content = panel.firstElementChild!;
      expect(content.classList.contains("spotnav-yr-content")).toBe(true);
      expect(content.firstElementChild!.classList.contains("spotnav-yr-art")).toBe(true);
      expect(content.firstElementChild!.querySelector("svg")).not.toBeNull();
      expect(dialog.querySelectorAll(".spotnav-yr-art")).toHaveLength(1);
    }
    for (const key of ["back", "close", "next"]) {
      const button = dialog.querySelector<HTMLButtonElement>(`[data-nav='${key}']`)!;
      expect(button.children).toHaveLength(1);
      expect(button.firstElementChild!.tagName).toBe("SPAN");
    }
  });

  it("turns pages with Next, Back, the arrow keys and a swipe; the fireworks stop off the cover", () => {
    const { story, dialog } = open();
    dialog.querySelector<HTMLButtonElement>("[data-nav='next']")!.click();
    expect(story.page).toBe(1);
    expect(story.animating).toBe(false);
    expect(panelText(dialog)).toContain("Sparat");
    dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(story.page).toBe(2);
    dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    expect(story.page).toBe(1);
    const surface = dialog.querySelector<HTMLElement>(".spotnav-yr-story")!;
    surface.dispatchEvent(new MouseEvent("pointerdown", { clientX: 300, clientY: 100 }) as PointerEvent);
    surface.dispatchEvent(new MouseEvent("pointerup", { clientX: 200, clientY: 110 }) as PointerEvent);
    expect(story.page).toBe(2);
    surface.dispatchEvent(new MouseEvent("pointerdown", { clientX: 100, clientY: 100 }) as PointerEvent);
    surface.dispatchEvent(new MouseEvent("pointerup", { clientX: 110, clientY: 300 }) as PointerEvent);
    expect(story.page).toBe(2);
    dialog.querySelector<HTMLButtonElement>("[data-nav='back']")!.click();
    expect(story.page).toBe(1);
  });

  it("counts the figure up and ends on it", () => {
    const { dialog } = open();
    dialog.querySelector<HTMLButtonElement>("[data-nav='next']")!.click();
    const big = dialog.querySelector<HTMLElement>(".spotnav-yr-big")!;
    expect(big.textContent).toContain("≈ 0");
    clock.tick(1000);
    expect(plain(big.textContent ?? "")).toBe("≈ 1 483 kr");
  });

  it("ends on the thanks page, with the sponsor link and the picture to share", () => {
    const { story, dialog } = open();
    story.go(99);
    expect(story.page).toBe(7);
    expect(story.animating).toBe(true);
    expect(dialog.querySelector<HTMLButtonElement>("[data-nav='next']")!.disabled).toBe(true);
    const sponsor = dialog.querySelector<HTMLAnchorElement>("a[data-sponsor]")!;
    expect(sponsor.href).toBe("https://github.com/sponsors/henrikekblad");
    expect(sponsor.target).toBe("_blank");
    expect(dialog.querySelector("[data-share]")).not.toBeNull();
    expect(dialog.querySelectorAll("a[data-sponsor]")).toHaveLength(1);
  });

  it("shows the picture made to share, to touch and hold where nothing else saves it", async () => {
    const share = vi.fn(async () => ({ kind: "saved" as const, preview: "data:image/png;base64,AAAA" }));
    const { story, dialog } = open("full_year.json", share as never);
    story.go(99);
    dialog.querySelector<HTMLButtonElement>("[data-share]")!.click();
    await vi.waitFor(() => expect(dialog.querySelector("img.spotnav-yr-preview")).not.toBeNull());
    const image = dialog.querySelector<HTMLImageElement>("img.spotnav-yr-preview")!;
    expect(image.getAttribute("src")).toBe("data:image/png;base64,AAAA");
    expect(image.alt).toBe(translate("sv", "yearReview.share.alt"));
    expect(share).toHaveBeenCalledTimes(1);
  });

  it("stops every frame loop when it closes, by Close or by Escape", () => {
    const { story, dialog, onClose } = open();
    expect(clock.waiting.size).toBeGreaterThan(0);
    dialog.querySelector<HTMLButtonElement>("[data-nav='close']")!.click();
    expect(story.isOpen()).toBe(false);
    expect(story.animating).toBe(false);
    expect(clock.waiting.size).toBe(0);
    expect(onClose).toHaveBeenCalledTimes(1);
    story.open(review("full_year.json"));
    expect(story.animating).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(story.isOpen()).toBe(false);
    expect(clock.waiting.size).toBe(0);
  });

  it("stops the fireworks while the tab is hidden, and starts them again when it is back", () => {
    const { story } = open();
    let state: DocumentVisibilityState = "hidden";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => state);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(story.animating).toBe(false);
    expect(clock.waiting.size).toBe(0);
    state = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    expect(story.animating).toBe(true);
  });

  it("moves nothing under reduced motion: no fireworks, no count-up, no frame asked for", () => {
    reducedMotionIs(true);
    const { story, dialog } = open();
    expect(story.animating).toBe(false);
    story.go(1);
    expect(plain(dialog.querySelector(".spotnav-yr-big")!.textContent ?? "")).toBe("≈ 1 483 kr");
    story.go(99);
    expect(story.animating).toBe(false);
    expect(clock.requested).toBe(0);
  });
});

describe("the picture to share", () => {
  it("has up to four tiles, the first four present: saved, the car's share, the sun, the battery's money", () => {
    expect(shareTiles("sv", review("full_year.json")).map((tile) => [tile.art, tile.label, plain(tile.value)])).toEqual([
      ["saved", "Sparat", "≈ 1 483 kr"],
      ["car", "Bilen laddad i billiga timmar", "86 %"],
      ["sun", "Från solen", "2 242 kWh"],
      ["battery", "Batteriet tjänade", "366 kr"],
    ]);
    expect(shareTiles("sv", review("car_only.json")).map((tile) => tile.art)).toEqual(["saved", "car"]);
    // A battery SpotNav does not control: what it saved at each hour's price, else the energy it gave back.
    const inverter = review("inverter_battery.json");
    expect(shareTiles("sv", inverter).map((tile) => [tile.art, tile.label, plain(tile.value)])).toEqual([
      ["saved", "Sparat", "≈ 1 156 kr"],
      ["car", "Bilen laddad i billiga timmar", "86 %"],
      ["sun", "Från solen", "2 242 kWh"],
      ["battery", "Batteriet sparade", "≈ 2 175 kr"],
    ]);
    const kwhOnly = inverter.pages.map((entry) => (entry.id === "battery" ? { ...entry, saved: null } : entry));
    expect(shareTiles("sv", { ...inverter, pages: kwhOnly }).map((tile) => [tile.label, plain(tile.value)])[3]).toEqual(["Batteriet gav tillbaka", "1 044 kWh"]);
    expect(shareTiles("sv", review("fresh_install.json"))[1]?.value).toBe("≈ 83 %");
  });

  it("says since which month for a part of the year, never a day or a time", () => {
    expect(shareSubtitle("sv", review("full_year.json"))).toBe("med SpotNav");
    expect(shareSubtitle("sv", review("fresh_install.json"))).toBe("med SpotNav, sedan november");
  });

  it("lays the tiles out two by two, the picture top right and the label beside it", () => {
    const boxes = tileBoxes(4);
    expect(boxes.map((box) => [box.x, box.y])).toEqual([[80, 400], [560, 400], [80, 800], [560, 800]]);
    for (const box of boxes) {
      expect(box.x + box.width).toBeLessThanOrEqual(SHARE_WIDTH - 80);
      expect(box.y + box.height).toBeLessThan(1270 - 34);
      expect(box.art.x + box.art.size).toBeLessThanOrEqual(box.x + box.width);
      expect(box.label.x + box.label.maxWidth).toBeLessThanOrEqual(box.art.x);
      expect(box.label.y).toBeLessThan(box.value.y);
    }
    expect(tileBoxes(7)).toHaveLength(4);
  });

  it("never leaves a hole: three as two above and one across, two across, one over the whole space", () => {
    const span = (count: number) => tileBoxes(count).map((box) => [box.x, box.y, box.width, box.height]);
    expect(span(3)).toEqual([[80, 400, 440, 360], [560, 400, 440, 360], [80, 800, 920, 360]]);
    expect(span(2)).toEqual([[80, 400, 920, 360], [80, 800, 920, 360]]);
    expect(span(1)).toEqual([[80, 400, 920, 760]]);
    expect(span(0)).toEqual([]);
    for (const count of [1, 2, 3]) {
      for (const box of tileBoxes(count)) {
        expect(box.x + box.width).toBeLessThanOrEqual(SHARE_WIDTH - 80);
        expect(box.y + box.height).toBeLessThan(1270 - 34);
        expect(box.art.x + box.art.size).toBeLessThanOrEqual(box.x + box.width);
        expect(box.label.x + box.label.maxWidth).toBeLessThanOrEqual(box.art.x);
        // The figure never runs under the picture where the two share a height.
        if (box.art.y + box.art.size > box.value.y - box.value.px) {
          expect(box.value.x + box.value.maxWidth).toBeLessThanOrEqual(box.art.x);
        }
      }
    }
  });

  it("wraps a label to two lines at most and fits the figure to the tile", () => {
    const measure = (text: string) => text.length * 10;
    expect(wrapLines(measure, "Bilen laddad i billiga timmar", 160)).toEqual(["Bilen laddad i", "billiga timmar"]);
    expect(wrapLines(measure, "one two three four five six", 80)).toEqual(["one two", "three…"]);
    const width = (text: string, size: number) => text.length * size * 0.6;
    expect(fitFontSize(width, "1 483 kr", 368)).toBe(76);
    expect(fitFontSize(width, "≈ 148 340 000 kr", 368)).toBe(48);
    expect(fitFontSize(width, "86 %", 368)).toBe(84);
  });

  it("draws the title, the tiles with their pictures, and no time of day", async () => {
    const { context, texts, images } = fakeContext();
    const canvas = document.createElement("canvas");
    vi.spyOn(canvas, "getContext").mockReturnValue(context as never);
    const drawn = await drawShare(canvas, review("full_year.json"), "sv", { fontFamily: "Roboto", image: async () => ({}) as never });
    expect(drawn).toBe(true);
    expect(canvas.width).toBe(SHARE_WIDTH);
    expect(canvas.height).toBe(SHARE_HEIGHT);
    const all = texts.map((entry) => plain(entry.text));
    expect(all).toContain("Mitt 2026");
    expect(all).toContain("med SpotNav");
    expect(all).toContain("≈ 1 483 kr");
    expect(all).toContain("SpotNav – gratis för Home Assistant");
    expect(all.join(" ")).not.toMatch(/\d{1,2}:\d{2}/);
    expect(images).toHaveLength(4);
    const value = texts.find((entry) => plain(entry.text) === "≈ 1 483 kr")!;
    expect(value.font).toContain("Roboto");
    expect(Number(/(\d+)px/.exec(value.font)![1]) * 0.5 * value.text.length).toBeLessThanOrEqual(368);
  });

  it("is shared as a file where the browser can, else saved through a link, and comes back to show", async () => {
    const { context } = fakeContext();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue("data:image/png;base64,AAAA");
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(function (callback: BlobCallback) {
      callback(new Blob(["png"], { type: "image/png" }));
    });
    vi.spyOn(window, "Image").mockImplementation(function (this: { onload?: () => void }) {
      const image = { onload: null as null | (() => void), onerror: null, set src(_value: string) { queueMicrotask(() => image.onload?.()); } };
      return image as never;
    } as never);
    const shared: ShareData[] = [];
    Object.assign(navigator, { canShare: () => true, share: async (data: ShareData) => void shared.push(data) });
    const outcome = await shareReview(review("full_year.json"), "sv", document, "Roboto");
    expect(outcome).toEqual({ kind: "shared", preview: "data:image/png;base64,AAAA" });
    expect((shared[0]?.files ?? [])[0]?.name).toBe("spotnav-2026.png");
    Object.assign(navigator, { canShare: () => false });
    const clicks: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this.download);
    });
    Object.assign(window.URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => undefined });
    const saved = await shareReview(review("full_year.json"), "sv", document, "Roboto");
    expect(saved.kind).toBe("saved");
    expect(clicks).toEqual(["spotnav-2026.png"]);
    Object.assign(navigator, { canShare: undefined, share: undefined });
  });

  it("fails plainly where there is no canvas to draw on", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    expect(await shareReview(review("full_year.json"), "sv", document)).toEqual({ kind: "failed" });
  });
});
