// The year review's pages in words: pure, from the decoded review (`year-review-model.ts`) to what each panel says,
// so the story (`year-review-view.ts`) and the share picture (`year-review-share.ts`) word the same figures alike and
// the tests read them without a DOM. A page the backend left out is not drawn; a page with nothing to say on this
// card (a battery with neither money nor energy) is skipped. "≈" where the payload says a figure is estimated;
// "since {date}" where its record starts after the year's first days.

import type { EnergyMeaning } from "./energy-palette";
import { clockOf, formatFixed, formatNumber } from "./format";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import type { YearReview, YearReviewMoney, YearReviewPage, YearReviewPageId } from "./year-review-model";

/** A panel's big figure: counted up from zero (`digits` decimals) with its unit, `approx` marked "≈". */
export interface BigFigure {
  value: number;
  digits: number;
  unit: string;
  approx: boolean;
}

export interface StoryPage {
  id: YearReviewPageId;
  /** The palette meaning the panel is tinted with. */
  tint: EnergyMeaning;
  eyebrow: string;
  /** A figure, or a line of words in its place (the cover, the thanks page), or nothing (a car without a share). */
  big: BigFigure | string | null;
  say: string | null;
  /** A share of 100 drawn as a bar. */
  meter: number | null;
  chips: string[];
  foot: string[];
  badge: string | null;
  fireworks: boolean;
}

export interface PageOptions {
  /** Whether this reader can reach Settings → Support to open the story again. */
  supportReachable: boolean;
}

/** A calendar date as "14 October" in the language (zone-free, formatted at noon UTC). */
export function dayMonth(language: Language, iso: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (match === null) {
    return "";
  }
  const instant = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12);
  return new Intl.DateTimeFormat(language === "en" ? "en-GB" : language, { day: "numeric", month: "long", timeZone: "UTC" }).format(
    new Date(instant),
  );
}

/** A date's month alone, "October" ("oktober"), for the share picture. */
export function monthName(language: Language, iso: string): string {
  const match = /^(\d{4})-(\d{2})/.exec(iso);
  if (match === null) {
    return "";
  }
  return new Intl.DateTimeFormat(language === "en" ? "en-GB" : language, { month: "long", timeZone: "UTC" }).format(
    new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 15, 12)),
  );
}

/** The unit an amount in minor units is shown in: the major one where the area names it. */
export function moneyUnit(money: YearReviewMoney | null): { unit: string; divisor: number } {
  if (money?.major_unit) {
    return { unit: money.major_unit, divisor: 100 };
  }
  return { unit: money?.minor_unit ?? "", divisor: 1 };
}

/** An amount in minor units, whole, in the major unit: "1 483 kr". */
export function moneyText(language: Language, minor: number, money: YearReviewMoney | null): string {
  const { unit, divisor } = moneyUnit(money);
  const figure = formatNumber(language, Math.round(minor / divisor), 0);
  return unit === "" ? figure : `${figure} ${unit}`;
}

/** A price per kWh given in minor units: "0,50 kr/kWh", or "50 öre/kWh" without a major unit. */
export function priceText(language: Language, minorPerKwh: number, money: YearReviewMoney | null): string {
  if (money?.major_unit) {
    return `${formatFixed(language, minorPerKwh / 100, 2)} ${money.major_unit}/kWh`;
  }
  const unit = money?.minor_unit ?? "";
  return `${formatNumber(language, minorPerKwh, 1)}${unit === "" ? "" : ` ${unit}`}/kWh`;
}

export function kwhWhole(language: Language, kwh: number): string {
  return `${formatNumber(language, Math.round(kwh), 0)} kWh`;
}

/** A small energy with a decimal below 10 kWh ("0,3 kWh"), whole above. */
function kwhSmall(language: Language, kwh: number): string {
  return `${formatNumber(language, kwh, kwh < 10 ? 1 : 0)} kWh`;
}

function plural(language: Language, base: string, count: number): string {
  return translate(language, `${base}.${pluralForm(language, count)}` as TranslationKey, { count: formatNumber(language, count, 0) });
}

function sinceLine(language: Language, date: string | null): string[] {
  return date === null ? [] : [translate(language, "yearReview.since", { date: dayMonth(language, date) })];
}

function page(
  base: Pick<StoryPage, "id" | "tint" | "eyebrow"> & Partial<StoryPage>,
): StoryPage {
  return { big: null, say: null, meter: null, chips: [], foot: [], badge: null, fireworks: false, ...base };
}

/** One page in words, or `null` for one with nothing to say. */
export function storyPage(language: Language, review: YearReview, source: YearReviewPage, options: PageOptions): StoryPage | null {
  const L = language;
  const money = review.money;
  const { unit: major, divisor } = moneyUnit(money);
  const title = translate(L, "yearReview.title");
  switch (source.id) {
    case "cover":
      return page({
        id: "cover",
        tint: "house",
        eyebrow: title,
        big: translate(L, "yearReview.cover.big", { year: String(review.year) }),
        say:
          review.since === null
            ? translate(L, "yearReview.cover.say")
            : translate(L, "yearReview.cover.saySince", { date: dayMonth(L, review.since) }),
        badge: review.partial ? translate(L, "yearReview.cover.partial") : null,
        fireworks: true,
      });
    case "saved": {
      const chips: string[] = [];
      for (const part of ["battery", "car", "heat"] as const) {
        const value = source[part];
        if (value === null) {
          continue;
        }
        const amount = translate(L, `yearReview.saved.part.${part}`, { amount: moneyText(L, value, money) });
        const start = source.starts[part];
        chips.push(start === null ? amount : translate(L, "yearReview.saved.partSince", { part: amount, date: dayMonth(L, start) }));
      }
      return page({
        id: "saved",
        tint: "export",
        eyebrow: translate(L, "yearReview.saved.eyebrow"),
        big: { value: source.total / divisor, digits: 0, unit: major, approx: source.estimated },
        say: translate(L, "yearReview.saved.say"),
        // One part alone needs no breakdown.
        chips: chips.length > 1 || source.starts.battery !== null || source.starts.car !== null || source.starts.heat !== null ? chips : [],
        foot: [translate(L, source.estimated ? "yearReview.saved.footEstimated" : "yearReview.saved.foot")],
      });
    }
    case "car": {
      const chips = [plural(L, "yearReview.car.charges", source.charges), kwhWhole(L, source.kwh)];
      if (source.cheapest !== null) {
        chips.push(
          translate(L, "yearReview.car.cheapest", {
            price: priceText(L, source.cheapest.price, money),
            date: dayMonth(L, source.cheapest.date),
          }),
        );
      }
      const foot: string[] = [];
      if (source.since !== null) {
        foot.push(translate(L, "yearReview.car.since", { date: dayMonth(L, source.since) }));
      }
      if (source.estimated) {
        foot.push(translate(L, "yearReview.car.estimated"));
      }
      if (source.cheapest !== null) {
        foot.push(translate(L, "yearReview.fees"));
      }
      const share = source.cheap_share_pct;
      return page({
        id: "car",
        tint: "car",
        eyebrow: translate(L, "yearReview.car.eyebrow"),
        big: share === null ? null : { value: share, digits: 0, unit: "%", approx: source.estimated },
        say: translate(L, share === null ? "yearReview.car.sayCount" : "yearReview.car.say"),
        meter: share,
        chips,
        foot,
      });
    }
    case "sun": {
      const pct = source.home_pct;
      const say =
        pct === null
          ? translate(L, "yearReview.sun.say")
          : translate(L, source.export_paid === true ? "yearReview.sun.saySold" : "yearReview.sun.sayOut", {
              pct: `${formatNumber(L, pct, 0)} %`,
            });
      return page({
        id: "sun",
        tint: "sun",
        eyebrow: translate(L, "yearReview.sun.eyebrow"),
        big: { value: source.kwh, digits: 0, unit: "kWh", approx: false },
        say,
        meter: pct,
        foot: sinceLine(L, source.since),
      });
    }
    case "battery": {
      if (source.earned === null && source.saved === null && source.kwh_out === null) {
        return null;
      }
      const chips: string[] = [];
      if (source.cycles !== null) {
        chips.push(plural(L, "yearReview.battery.cycles", source.cycles));
      }
      const foot: string[] = [];
      let big: BigFigure;
      let say: string;
      if (source.earned !== null) {
        // SpotNav's own count of what it earned, as before.
        if (source.buy_price !== null) {
          chips.push(translate(L, "yearReview.battery.buy", { price: priceText(L, source.buy_price, money) }));
        }
        if (source.use_price !== null) {
          chips.push(translate(L, "yearReview.battery.use", { price: priceText(L, source.use_price, money) }));
        }
        big = { value: source.earned / divisor, digits: 0, unit: major, approx: false };
        say = translate(L, "yearReview.battery.say");
        if (source.earned_since !== null) {
          foot.push(translate(L, "yearReview.battery.earnedSince", { date: dayMonth(L, source.earned_since) }));
        }
      } else {
        if (source.saved !== null) {
          // What it saved at each hour's price, whoever controlled it.
          big = { value: source.saved / divisor, digits: 0, unit: major, approx: true };
          say = translate(L, "yearReview.battery.saySaved");
          foot.push(translate(L, "yearReview.battery.savedFoot"), ...sinceLine(L, source.saved_since));
        } else {
          big = { value: source.kwh_out ?? 0, digits: 0, unit: "kWh", approx: false };
          say = translate(L, "yearReview.battery.sayKwh");
        }
      }
      return page({ id: "battery", tint: "battery", eyebrow: translate(L, "yearReview.battery.eyebrow"), big, say, chips, foot });
    }
    case "fuse":
      return page({
        id: "fuse",
        tint: "import",
        eyebrow: translate(L, "yearReview.fuse.eyebrow"),
        big: {
          value: source.count,
          digits: 0,
          unit: translate(L, `yearReview.fuse.times.${pluralForm(L, source.count)}`),
          approx: false,
        },
        say: translate(L, "yearReview.fuse.say"),
        foot: sinceLine(L, source.since),
      });
    case "expensive": {
      const params = { date: dayMonth(L, source.start), time: clockOf(source.start) };
      const bought = source.bought_kwh;
      // An amount that would be told as 0,0 kWh is told as none bought.
      const say =
        bought === null
          ? translate(L, "yearReview.expensive.when", params)
          : bought < 0.05
            ? translate(L, "yearReview.expensive.boughtNone", params)
            : translate(L, bought < 1 ? "yearReview.expensive.boughtLittle" : "yearReview.expensive.bought", {
                ...params,
                kwh: kwhSmall(L, bought),
              });
      const { unit } = moneyUnit(money);
      return page({
        id: "expensive",
        tint: "expensive",
        eyebrow: translate(L, "yearReview.expensive.eyebrow"),
        big: money?.major_unit
          ? { value: source.price / 100, digits: 2, unit: `${unit}/kWh`, approx: false }
          : { value: source.price, digits: 1, unit: `${unit === "" ? "" : unit}/kWh`, approx: false },
        say,
        foot: [...sinceLine(L, source.since), translate(L, "yearReview.fees")],
      });
    }
    case "thanks":
      return page({
        id: "thanks",
        tint: "house",
        eyebrow: translate(L, "yearReview.thanks.eyebrow"),
        big: translate(L, "yearReview.thanks.big"),
        say: translate(L, "yearReview.thanks.say"),
        foot: options.supportReachable ? [translate(L, "yearReview.thanks.foot")] : [],
        fireworks: true,
      });
  }
}

/** Every page the review has, in words, in the backend's order. */
export function storyPages(language: Language, review: YearReview, options: PageOptions): StoryPage[] {
  return review.pages.map((source) => storyPage(language, review, source, options)).filter((entry): entry is StoryPage => entry !== null);
}

/** The big figure's number as text, at its decimals. */
export function figureText(language: Language, figure: BigFigure, value = figure.value): string {
  return formatFixed(language, value, figure.digits);
}
