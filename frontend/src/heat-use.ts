// The heat card's Use tab (SpotNav 3.0): what the heating used over a day, month or year, one stack per bar, split
// only as far as something measures it (`spotnav/get_heat_history`'s `parts`, `heat/history.py`): the heat pump (its
// meter less the hot water), the hot water (the meter's plateaus, on the days the recorder still has in 5-minute
// detail), each heat zone with a sensor of its own (an air-to-air heat pump) and every other heat counted.
//
// * A bar is an hour of a day, a day of a month or a month of a year, in kWh or in money (a switch, remembered per
//   card in this browser; without a priced hour there is no switch and the bars are kWh).
// * The pool, the spa and the sauna are parts of their own where SpotNav counts them (when they ran, at their power).
// * The parts are legend toggles (pressed: drawn), each with its period sum in the unit shown. Which are hidden is
//   remembered per card in this browser.
// * The headline is what SpotNav saved on heat over the period, the figure the house card's earnings count as "Heat"
//   (`saving`); a period without one has the heat's total as its headline. The total of the parts drawn stands under
//   it.
// Every figure is the backend's; text is written with `textContent` only.

import { kwhText, moneyMinorAsMajor as useMoney } from "./format";
import type { HeatHistory, HeatPart } from "./heat-history";
import type { UseUnit } from "./heat-tabs";
import { bucketWords } from "./house-history";
import { barChart, PERIOD_VIEW_CLASSES, type Bar, type BarPart } from "./house-period-view";
import { HOUSE_CLASSES as H } from "./house-styles";
import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";
import { LOAD_COLOUR_VALUE } from "./load-colours";

export const USE_CLASSES = {
  head: "spotnav-heat-use-head",
  units: "spotnav-heat-use-units",
  swatch: "spotnav-heat-use-swatch",
  legend: "spotnav-heat-use-legend",
  notes: "spotnav-heat-use-notes",
} as const;

const U = USE_CLASSES;

const KIND_WORDS: Record<Exclude<HeatPart["kind"], "zone">, TranslationKey> = {
  pump: "heat.use.part.pump",
  hot_water: "heat.use.part.hotWater",
  other: "heat.use.part.other",
  pool: "heat.use.part.pool",
  spa: "heat.use.part.spa",
  sauna: "heat.use.part.sauna",
};

/** A part's name: a zone's own, else the card's words for its kind. */
export function partWord(language: Language, part: HeatPart): string {
  return part.kind === "zone" ? (part.name ?? translate(language, "heat.use.part.zone")) : translate(language, KIND_WORDS[part.kind]);
}

/** The colour a part is drawn in (`data-series`): its kind, and for the zones their place among them. */
export function partSeries(parts: readonly HeatPart[], part: HeatPart): string {
  if (part.kind !== "zone") {
    return `use-${part.kind}`;
  }
  return `use-zone-${parts.filter((item) => item.kind === "zone").indexOf(part) % 4}`;
}

/** Whether the period can be shown in money: some part has a priced hour. */
export function hasMoney(answer: HeatHistory): boolean {
  return answer.money !== null && answer.parts.some((part) => part.cost !== null);
}

/** The unit drawn: the one chosen, kWh where the period has no money. */
export function shownUnit(answer: HeatHistory, unit: UseUnit): UseUnit {
  return unit === "money" && hasMoney(answer) ? "money" : "kwh";
}

function figure(language: Language, answer: HeatHistory, unit: UseUnit, kwh: number, cost: number | null): string | null {
  if (unit === "kwh") {
    return kwhText(language, kwh);
  }
  return cost === null ? null : useMoney(language, cost, answer.money);
}

/** The sum of the parts drawn in the unit shown; `null` with none drawn (or none priced). */
export function useTotal(answer: HeatHistory, hidden: ReadonlySet<string>, unit: UseUnit): number | null {
  const drawn = answer.parts.filter((part) => !hidden.has(part.key));
  const values = drawn.map((part) => (unit === "kwh" ? part.kwh : part.cost)).filter((value): value is number => value !== null);
  return values.length === 0 ? null : values.reduce((sum, value) => sum + value, 0);
}

/** kWh | money, one pressed; `null` where the period has no money (the bars are kWh). */
export function unitSwitch(doc: Document, language: Language, answer: HeatHistory, unit: UseUnit, onUnit: (unit: UseUnit) => void): HTMLElement | null {
  if (!hasMoney(answer)) {
    return null;
  }
  const row = element(doc, "div", `spotnav-house-picker-kinds ${U.units}`);
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", translate(language, "heat.use.unit"));
  const shown = shownUnit(answer, unit);
  for (const [value, words] of [
    ["kwh", "kWh"],
    ["money", answer.money?.major_unit ?? answer.money?.currency ?? ""],
  ] as const) {
    const button = element(doc, "button", undefined, words);
    button.type = "button";
    button.dataset["unit"] = value;
    button.setAttribute("aria-pressed", String(shown === value));
    if (value === "money") {
      button.setAttribute("aria-label", translate(language, "heat.use.unitMoney", { unit: words }));
    }
    button.addEventListener("click", () => {
      if (shown !== value) {
        onUnit(value);
      }
    });
    row.append(button);
  }
  return row;
}

/** The toggles: a pressed button per part with its swatch, its name and its sum. `onToggle` gets the hidden parts
 * after the change. */
export function useLegend(
  doc: Document,
  language: Language,
  answer: HeatHistory,
  hidden: ReadonlySet<string>,
  unit: UseUnit,
  onToggle: (hidden: string[]) => void,
): HTMLElement {
  const list = element(doc, "ul", `${H.legend} ${U.legend}`);
  list.setAttribute("aria-label", translate(language, "house.earn.parts"));
  for (const part of answer.parts) {
    const entry = element(doc, "li", H.legendItem);
    const button = element(doc, "button", H.legendButton);
    button.type = "button";
    button.dataset["legend"] = `use-${part.key}`;
    button.setAttribute("aria-pressed", String(!hidden.has(part.key)));
    const swatch = element(doc, "span", U.swatch);
    swatch.dataset["series"] = partSeries(answer.parts, part);
    swatch.setAttribute("aria-hidden", "true");
    const sum = figure(language, answer, unit, part.kwh, part.cost);
    const words = partWord(language, part);
    button.append(swatch, element(doc, "span", undefined, sum === null ? words : `${words} ${sum}`));
    button.addEventListener("click", () => {
      const next = new Set(hidden);
      if (!next.delete(part.key)) {
        next.add(part.key);
      }
      // Only parts the answer has are kept, so a zone that went away is not remembered for ever.
      onToggle([...next].filter((key) => answer.parts.some((item) => item.key === key)));
    });
    entry.append(button);
    list.append(entry);
  }
  return list;
}

export interface UseBarsInput {
  doc: Document;
  language: Language;
  answer: HeatHistory;
  hidden: ReadonlySet<string>;
  unit: UseUnit;
  height: number;
  selected: string | null;
  onSelect?: (key: string | null) => void;
  /** In a box of fixed height: no hint under the bars. */
  boxed?: boolean;
}

/** The bars (see the module); `null` when no bucket has a figure for a part drawn. */
export function useBars(input: UseBarsInput): HTMLElement | null {
  const { doc, language, answer, hidden } = input;
  const unit = shownUnit(answer, input.unit);
  const drawn = answer.parts.filter((part) => !hidden.has(part.key));
  const value = (part: { kwh: number; cost: number | null }): number | null => (unit === "kwh" ? part.kwh : part.cost);
  const bars: Bar[] = answer.buckets.map((bucket, index) => {
    const words = bucketWords(language, answer.period, bucket.key, index, answer.buckets.length);
    const lane: BarPart[] = [];
    const said: string[] = [];
    let sum = 0;
    for (const part of drawn) {
      const figures = bucket.parts.find((item) => item.key === part.key);
      const amount = figures === undefined ? null : value(figures);
      if (figures === undefined || amount === null || amount <= 0) {
        continue;
      }
      lane.push({ series: partSeries(answer.parts, part), value: amount });
      sum += amount;
      said.push(`${partWord(language, part)} ${figure(language, answer, unit, figures.kwh, figures.cost) ?? ""}`.trim());
    }
    if (lane.length > 1) {
      said.unshift(unit === "kwh" ? kwhText(language, sum) : useMoney(language, sum, answer.money));
    }
    if (bucket.saving !== null) {
      said.push(translate(language, "heat.use.barSaved", { amount: useMoney(language, bucket.saving, answer.money) }));
    }
    return {
      key: bucket.key,
      lanes: lane.length === 0 ? [] : [lane],
      words: said.length === 0 ? translate(language, bucket.kwh === null ? "hist.heat.barNone" : "heat.use.barNone", { when: words.when }) : `${words.when}: ${said.join(" · ")}`,
      axis: words.axis,
    };
  });
  if (bars.every((bar) => bar.lanes.length === 0)) {
    return null;
  }
  const chart = barChart({
    doc,
    label: translate(language, "heat.use.label"),
    bars,
    height: input.height,
    scale: (top) => (unit === "kwh" ? kwhText(language, top) : useMoney(language, top, answer.money)),
    hint: input.boxed === true ? "" : translate(language, "hist.heat.hint"),
    selected: input.selected,
    ...(input.onSelect === undefined ? {} : { onSelect: input.onSelect }),
  });
  chart.dataset["part"] = "use-bars";
  chart.dataset["unit"] = unit;
  return chart;
}

/** The headline: what SpotNav saved on heat over the period, else the heat's total; and under it (`sub`) the total
 * of the parts drawn where the headline is the saving. */
export function useHeadline(language: Language, answer: HeatHistory, hidden: ReadonlySet<string>, unit: UseUnit): { head: string; sub: string | null; saved: boolean } {
  const shown = shownUnit(answer, unit);
  const total = useTotal(answer, hidden, shown);
  const words = total === null ? null : shown === "kwh" ? kwhText(language, total) : useMoney(language, total, answer.money);
  const totalLine = words === null ? translate(language, "house.earn.noneShown") : translate(language, "heat.use.total", { amount: words });
  if (answer.saving !== null) {
    return { head: translate(language, "heat.use.saved", { amount: useMoney(language, answer.saving.value, answer.money) }), sub: totalLine, saved: true };
  }
  return { head: totalLine, sub: null, saved: false };
}

/** The notes under the bars, each a plain sentence: the days the hot water is split out for, and what the money is. */
export function useNotes(language: Language, answer: HeatHistory, unit: UseUnit): string[] {
  const notes: string[] = [];
  const energy = answer.energy;
  if (energy !== null && energy.days > 0 && energy.split_days < energy.days) {
    notes.push(
      energy.split_days === 0
        ? translate(language, "hist.heat.noSplit")
        : translate(language, "hist.heat.splitDays", { days: String(energy.split_days), total: String(energy.days) }),
    );
  }
  if (shownUnit(answer, unit) === "money") {
    notes.push(translate(language, "hist.heat.footnote"));
  }
  if (answer.parts.some((part) => part.kind === "pool" || part.kind === "spa" || part.kind === "sauna")) {
    notes.push(translate(language, "heat.use.loadsNote"));
  }
  return notes;
}

export interface UsePanelInput {
  doc: Document;
  language: Language;
  answer: HeatHistory;
  hidden: readonly string[];
  unit: UseUnit;
  height: number;
  selected: string | null;
  boxed: boolean;
  onUnit: (unit: UseUnit) => void;
  onToggle: (hidden: string[]) => void;
  onSelect?: (key: string | null) => void;
}

/** The tab's content under the period picker: the headline with the unit switch, the legend, the bars, the notes. */
export function usePanel(input: UsePanelInput): HTMLElement[] {
  const { doc, language, answer } = input;
  const status = (key: TranslationKey, line: string): HTMLElement => {
    const paragraph = element(doc, "p", `${C.muted} ${H.line}`, translate(language, key));
    paragraph.setAttribute("role", "status");
    paragraph.dataset["line"] = line;
    return paragraph;
  };
  if (answer.meter === null) {
    return [status("hist.heat.noMeter", "no-meter")];
  }
  if (answer.parts.length === 0 || answer.energy === null || answer.energy.hours === 0) {
    return [status("hist.heat.noEnergy", "no-energy")];
  }
  const hidden = new Set(input.hidden);
  const unit = shownUnit(answer, input.unit);
  const out: HTMLElement[] = [];
  const headline = useHeadline(language, answer, hidden, unit);
  const head = element(doc, "div", U.head);
  const words = element(doc, "div");
  const top = element(doc, "p", PERIOD_VIEW_CLASSES.total, headline.head);
  top.dataset["line"] = headline.saved ? "use-saved" : "use-total";
  words.append(top);
  if (headline.sub !== null) {
    const sub = element(doc, "p", `${C.muted} ${H.line}`, headline.sub);
    sub.dataset["line"] = "use-total";
    words.append(sub);
  }
  head.append(words);
  const units = unitSwitch(doc, language, answer, unit, input.onUnit);
  if (units !== null) {
    head.append(units);
  }
  out.push(head, useLegend(doc, language, answer, hidden, unit, input.onToggle));
  const bars = useBars({
    doc,
    language,
    answer,
    hidden,
    unit,
    height: input.height,
    selected: input.selected,
    ...(input.onSelect === undefined ? {} : { onSelect: input.onSelect }),
    boxed: input.boxed,
  });
  if (bars !== null) {
    out.push(bars);
  }
  if (!input.boxed) {
    const notes = useNotes(language, answer, unit);
    if (notes.length > 0) {
      const line = element(doc, "p", `${C.muted} ${U.notes}`, notes.join(" "));
      line.dataset["line"] = "use-notes";
      out.push(line);
    }
  }
  return out;
}

export const USE_STYLES = `
  [data-series="use-pump"] { --spotnav-tile-colour: var(--spotnav-heat-colour, var(--spotnav-heat, #e5554b)); }
  [data-series="use-hot_water"] { --spotnav-tile-colour: var(--spotnav-heat-use-water-colour, #2a8fbd); }
  [data-series="use-zone-0"] { --spotnav-tile-colour: var(--spotnav-heat-use-zone-colour, #e0a030); }
  [data-series="use-zone-1"] { --spotnav-tile-colour: #8e6bbf; }
  [data-series="use-zone-2"] { --spotnav-tile-colour: #4c9f70; }
  [data-series="use-zone-3"] { --spotnav-tile-colour: #c06c9a; }
  [data-series="use-pool"] { --spotnav-tile-colour: var(--spotnav-heat-use-pool-colour, ${LOAD_COLOUR_VALUE.pool_heat}); }
  [data-series="use-spa"] { --spotnav-tile-colour: var(--spotnav-heat-use-spa-colour, ${LOAD_COLOUR_VALUE.spa}); }
  [data-series="use-sauna"] { --spotnav-tile-colour: var(--spotnav-heat-use-sauna-colour, ${LOAD_COLOUR_VALUE.sauna}); }
  [data-series="use-other"] { --spotnav-tile-colour: color-mix(in srgb, var(--secondary-text-color, #727272) 70%, transparent); }
  .${U.head} {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }
  .${U.head} p { margin: 0; }
  .${U.units} { flex: none; }
  .${U.swatch} {
    flex: none;
    width: 0.9rem;
    height: 0.7rem;
    border-radius: 2px;
    background: var(--spotnav-tile-colour);
  }
  .${U.legend} { margin: 0; }
  .${U.notes} {
    margin: 0;
    font-size: 0.8rem;
  }
  @media (forced-colors: active) {
    .${U.swatch} { background: CanvasText; }
  }
`;
