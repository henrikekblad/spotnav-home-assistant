// The house card's Earnings tab (SpotNav 3.0): what SpotNav saved over a day, month or year, one stack per bar with a
// part each for the battery's arbitrage, the heat and the car (`site/spotnav_saving.py`), from the same history
// answer the other period tabs read (`spotnav/get_house_history`).
//
// * A bar is an hour of a day, a day of a month or a month of a year. A part that cost money goes below the line.
// * The parts are legend toggles (pressed: drawn), each with its period sum; the headline is the sum of the parts
//   drawn. Which are hidden is remembered per card in this browser, guarded like the chart preferences.
// * Before SpotNav controls the battery (test, or the battery's own optimiser), the battery has two parts of its own
//   in place of SpotNav's: what the battery's own control earned (`own`, counted in the headline: it is money the
//   house got) and, beside it in a lane of its own and hatched, what SpotNav's plan would have earned on the same days
//   (`would`, never in the headline: nothing was done). The plan's figure is the nightly review's, a whole day's, so a
//   day's hours have the battery's own bars and the plan's figure in words.
// * Where the nightly reviews value the sun, the sun has a part of its own (`sun`): the same figure as Details → Money's
//   (each reviewed day's `sun_value`: the sun's energy the house used at the price then, plus what was sold). It is
//   money the house got, so it is in the headline, but it is not SpotNav's doing. On a battery site it is a whole
//   day's figure, like the heat; on a site without a battery (`sun`, the night's quarter-hour figure or an hourly
//   estimate) a day's hours have their own, drawn in the hour's bar, and an estimate is said in the notes.
// * The car's saving is counted where its charge started: on that day, and in a day's hours in that hour. The heat's
//   is an estimate for a whole day: in a day's view it is in the headline and its toggle, not in an hour's bar.
// * Hours are kept 40 days: an older day has its sums and says its hours are no longer kept.
// Every figure is the backend's; text is written with `textContent` only.

import { browserStore, type PreferenceStore } from "./chart-preference";
import type { HouseBucket, HouseHistory } from "./house-history";
import { bucketWords } from "./house-history";
import { barChart, flowWords, type Bar, type BarPart } from "./house-period-view";
import { HOUSE_CLASSES as H } from "./house-styles";
import { translate, type Language, type TranslationKey } from "./i18n";
import { element } from "./dom";

export type EarnPart = "battery" | "own" | "would" | "heat" | "car" | "sun";
/** In the order they stack, bottom up; the plan's figure stands beside the stack. */
export const EARN_PARTS: readonly EarnPart[] = ["battery", "own", "heat", "car", "sun", "would"];

const PART_WORDS: Record<EarnPart, TranslationKey> = {
  battery: "house.earn.part.battery",
  own: "house.earn.part.own",
  would: "house.earn.part.would",
  heat: "house.earn.part.heat",
  car: "house.earn.part.car",
  sun: "house.earn.part.sun",
};

/** What a part counts, as the toggle's tooltip, where it needs saying. */
const PART_INFO: Partial<Record<EarnPart, TranslationKey>> = {
  sun: "house.earn.part.sunInfo",
};

/** The parts that are a whole day's figure: in a day's view in the headline and the toggles, never in an hour's bar. */
const DAY_PARTS: ReadonlySet<EarnPart> = new Set(["heat", "sun", "would"]);

export function isEarnPart(value: unknown): value is EarnPart {
  return typeof value === "string" && (EARN_PARTS as readonly string[]).includes(value);
}

export function earnKey(target: string): string {
  return `spotnav-house-card.earn.hidden.${target}`;
}

/** The parts this browser last hid on the card naming `target`; none when nothing usable is stored. */
export function readEarnHidden(target: string, store: () => PreferenceStore | null = browserStore): EarnPart[] {
  try {
    const raw = store()?.getItem(earnKey(target)) ?? null;
    const parsed: unknown = raw === null ? null : JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isEarnPart) : [];
  } catch {
    return [];
  }
}

export function writeEarnHidden(target: string, hidden: readonly string[], store: () => PreferenceStore | null = browserStore): void {
  try {
    store()?.setItem(earnKey(target), JSON.stringify(hidden.filter(isEarnPart)));
  } catch {
    // Nothing is remembered in this browser; the choice holds while the card lives.
  }
}

/** A bucket's figure for a part (minor unit); `null` where the bucket has none. */
export function bucketPart(bucket: HouseBucket, part: EarnPart): number | null {
  switch (part) {
    case "battery":
      return bucket.saving_battery;
    case "own":
      return bucket.own_battery;
    case "would":
      return bucket.would_battery;
    case "heat":
      return bucket.saving_heat;
    case "car":
      return bucket.saving_car;
    case "sun":
      return bucket.sun_gave;
  }
}

/** The period's sum of a part: the answer's own sums (a day's heat is in them and in no hour); `null` without it. */
export function partTotal(answer: HouseHistory, part: EarnPart): number | null {
  switch (part) {
    case "battery":
      return answer.saving?.battery ?? null;
    case "own":
      return answer.own_control?.earned ?? null;
    case "would":
      return answer.own_control?.would ?? null;
    case "heat":
      return answer.saving?.heat ?? null;
    case "car":
      return answer.saving?.car ?? null;
    case "sun":
      return answer.review?.sun_gave ?? answer.sun?.gave ?? null;
  }
}

/** The parts the period has, in their order: a part with neither a sum nor a bucket's figure is left out. */
export function earnParts(answer: HouseHistory): EarnPart[] {
  return EARN_PARTS.filter((part) => partTotal(answer, part) !== null || answer.buckets.some((bucket) => bucketPart(bucket, part) !== null));
}

/** The headline's sum: the parts drawn, never the plan's would-be figure; `null` with none of them. */
export function earnTotal(answer: HouseHistory, hidden: ReadonlySet<string>): number | null {
  const values = earnParts(answer)
    .filter((part) => part !== "would" && !hidden.has(part))
    .map((part) => partTotal(answer, part))
    .filter((value): value is number => value !== null);
  return values.length === 0 ? null : values.reduce((sum, value) => sum + value, 0);
}

/** Whether any bucket has a figure for a part drawn (else the period has only its sums). */
export function hasBars(answer: HouseHistory, parts: readonly EarnPart[]): boolean {
  return answer.buckets.some((bucket) => parts.some((part) => bucketPart(bucket, part) !== null));
}

/** The toggles: a pressed button per part with its swatch, its name and its sum. `onToggle` gets the hidden parts
 * after the change. */
export function earnLegend(
  doc: Document,
  language: Language,
  answer: HouseHistory,
  hidden: ReadonlySet<string>,
  money: (minor: number) => string,
  onToggle: (hidden: string[]) => void,
): HTMLElement {
  const list = element(doc, "ul", `${H.legend} spotnav-house-earn-legend`);
  list.setAttribute("aria-label", translate(language, "house.earn.parts"));
  // The plan's would-be figure is a toggle only where the period draws it (a day's hours have it in words alone).
  const wouldDrawn = answer.buckets.some((bucket) => bucket.would_battery !== null);
  for (const part of earnParts(answer).filter((item) => item !== "would" || wouldDrawn)) {
    const entry = element(doc, "li", H.legendItem);
    const button = element(doc, "button", H.legendButton);
    button.type = "button";
    button.dataset["legend"] = `earn-${part}`;
    button.setAttribute("aria-pressed", String(!hidden.has(part)));
    const info = PART_INFO[part];
    if (info !== undefined) {
      button.title = translate(language, info);
    }
    const swatch = element(doc, "span", "spotnav-house-earn-swatch");
    swatch.dataset["series"] = `earn-${part}`;
    swatch.setAttribute("aria-hidden", "true");
    const total = partTotal(answer, part);
    const words = translate(language, PART_WORDS[part]);
    button.append(swatch, element(doc, "span", undefined, total === null ? words : `${words} ${money(total)}`));
    button.addEventListener("click", () => {
      const next = new Set(hidden);
      if (!next.delete(part)) {
        next.add(part);
      }
      onToggle([...next]);
    });
    entry.append(button);
    list.append(entry);
  }
  return list;
}

export interface EarnBarsInput {
  doc: Document;
  language: Language;
  answer: HouseHistory;
  hidden: ReadonlySet<string>;
  height: number;
  money: (minor: number) => string;
  selected: string | null;
  onSelect?: (key: string | null) => void;
  /** In a box of fixed height: no hint under the bars. */
  boxed?: boolean;
}

/** The bars (see the module); `null` when no bucket has a figure for a part drawn. */
export function earnBars(input: EarnBarsInput): HTMLElement | null {
  const { doc, language, answer, hidden, money } = input;
  const drawn = earnParts(answer).filter((part) => !hidden.has(part));
  if (!hasBars(answer, drawn)) {
    return null;
  }
  const stacked = drawn.filter((part) => part !== "would");
  // The plan's figure has a lane of its own beside the stack wherever the period draws it at all.
  const beside = drawn.includes("would") && answer.buckets.some((bucket) => bucket.would_battery !== null);
  const buckets = answer.buckets;
  const bars: Bar[] = buckets.map((bucket, index) => {
    const words = bucketWords(language, answer.period, bucket.key, index, buckets.length);
    const figures = drawn.map((part) => [part, bucketPart(bucket, part)] as const).filter((item): item is readonly [EarnPart, number] => item[1] !== null);
    const lanes: BarPart[][] = [];
    const slots: number[] = [];
    const up = stacked.map((part) => ({ series: `earn-${part}`, value: Math.max(0, bucketPart(bucket, part) ?? 0) })).filter((part) => part.value > 0);
    const down = stacked.map((part) => ({ series: `earn-${part}`, value: Math.min(0, bucketPart(bucket, part) ?? 0) })).filter((part) => part.value < 0);
    for (const lane of [up, down]) {
      if (lane.length > 0) {
        lanes.push(lane);
        slots.push(0);
      }
    }
    if (beside && bucket.would_battery !== null && bucket.would_battery !== 0) {
      lanes.push([{ series: "earn-would", value: bucket.would_battery }]);
      slots.push(1);
    }
    // The battery's figure explained (what the maker's cash flow can be compared with) in place of its two parts,
    // where the backend kept it; a charge-only hour says what it charged.
    const flow = bucket.battery_flow === null || !(drawn.includes("battery") || drawn.includes("own")) ? null : flowWords(language, bucket.battery_flow, money);
    const said = figures
      .filter(([part]) => flow === null || (part !== "battery" && part !== "own"))
      .map(([part, value]) => `${translate(language, PART_WORDS[part])} ${money(value)}`);
    if (flow !== null) {
      said.unshift(flow);
    }
    return {
      key: bucket.key,
      lanes,
      slots,
      slotCount: beside ? 2 : 1,
      words: said.length === 0 ? translate(language, "house.earn.barNone", { when: words.when }) : `${words.when}: ${said.join(" · ")}`,
      axis: words.axis,
    };
  });
  const chart = barChart({
    doc,
    label: translate(language, "house.earn.label"),
    bars,
    height: input.height,
    scale: (value) => money(value),
    hint: input.boxed === true ? "" : translate(language, "hist.house.hint"),
    selected: input.selected,
    ...(input.onSelect === undefined ? {} : { onSelect: input.onSelect }),
  });
  chart.dataset["part"] = "earn-bars";
  return chart;
}

/** The notes under the bars, each a plain sentence: where the car's saving is counted, the heat's whole-day estimate,
 * what the sun's part counts, the plan's would-be figure for a day, and an older day's hours. */
export function earnNotes(language: Language, answer: HouseHistory, hidden: ReadonlySet<string>, money: (minor: number) => string): string[] {
  const parts = earnParts(answer).filter((part) => !hidden.has(part));
  const day = answer.period === "day";
  const running = answer.today >= answer.date && answer.today < answer.end;
  const notes: string[] = [];
  const hourly = parts.filter((part) => !DAY_PARTS.has(part));
  if (day && !hasBars(answer, hourly) && hourly.length > 0 && !running) {
    notes.push(translate(language, "house.earn.note.noHours"));
  }
  if (parts.includes("own")) {
    const control = answer.own_control;
    if (control !== null && control.would !== null) {
      if (day) {
        notes.push(translate(language, "house.earn.note.wouldDay", { amount: money(control.would) }));
      } else if (control.would_days < control.days) {
        notes.push(translate(language, "house.earn.note.wouldDays", { reviewed: String(control.would_days), days: String(control.days) }));
      }
    } else if (control !== null) {
      notes.push(translate(language, running && day ? "house.earn.note.wouldTonight" : "house.earn.note.wouldNone"));
    }
  }
  if (parts.includes("car")) {
    notes.push(translate(language, day ? "house.earn.note.carHour" : "house.earn.note.carDay"));
  }
  if (parts.includes("heat")) {
    notes.push(translate(language, day ? "house.earn.note.heatDay" : "house.earn.note.heat"));
  }
  if (parts.includes("sun")) {
    // A day's hours with their own sun figure (a site without a battery) are drawn: no whole-day wording then.
    const sunHours = day && answer.buckets.some((bucket) => bucket.sun_gave !== null);
    notes.push(translate(language, day && !sunHours ? "house.earn.note.sunDay" : "house.earn.note.sun"));
    if ((answer.sun?.estimated_days ?? 0) > 0) {
      notes.push(translate(language, "house.sun.estimated"));
    }
  }
  return notes;
}

export const EARN_STYLES = `
  [data-series="earn-battery"] { --spotnav-tile-colour: var(--spotnav-battery-colour); }
  [data-series="earn-own"] { --spotnav-tile-colour: color-mix(in srgb, var(--spotnav-battery-colour) 50%, var(--secondary-text-color, #727272)); }
  [data-series="earn-heat"] { --spotnav-tile-colour: var(--spotnav-heat-colour); }
  [data-series="earn-car"] { --spotnav-tile-colour: var(--spotnav-car-colour, var(--spotnav-energy-car)); }
  [data-series="earn-sun"] { --spotnav-tile-colour: var(--spotnav-sun-colour); }
  [data-series="earn-would"] { --spotnav-tile-colour: var(--spotnav-battery-colour); }
  /* What the plan would have earned: hatched and lighter, never a solid bar (nothing was done). */
  .spotnav-house-pbar-lane > .spotnav-house-pbar-part[data-series="earn-would"], .spotnav-house-earn-swatch[data-series="earn-would"] {
    background: repeating-linear-gradient(135deg, var(--spotnav-tile-colour) 0 2px, transparent 2px 4px);
    outline: 1px solid var(--spotnav-tile-colour);
    outline-offset: -1px;
  }
  .spotnav-house-earn-swatch {
    flex: none;
    width: 0.9rem;
    height: 0.7rem;
    border-radius: 2px;
    background: var(--spotnav-tile-colour);
  }
  .spotnav-house-earn-legend { margin: 0; }
  .spotnav-house-earn-notes {
    margin: 0;
    font-size: 0.8rem;
  }
  @media (forced-colors: active) {
    .spotnav-house-earn-swatch { background: CanvasText; }
  }
`;
