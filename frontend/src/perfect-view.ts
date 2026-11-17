// The days against their perfect plans in hindsight (`site/perfect_view.py`), shared by the house card and its history:
// the headline (each way's share of the perfect plan, the battery's own and the chosen mode first), the share rows
// with a bar each, the plain notes (what "perfect" is, the energy left at midnight, a figure clamped), and behind
// "Show details" the money behind the shares, the battery's work and the cells' temperature. Every figure is the
// backend's; text is written with `textContent` only.

import { formatFixed, formatNumber } from "./format";
import { MODES, PERFECT_WAYS, type BatteryMode, type HouseAnswer, type PerfectFigures, type PerfectWay } from "./house-model";
import { HOUSE_CLASSES as H } from "./house-styles";
import { numberWord, pluralForm, translate, type Language } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

type Money = HouseAnswer["money"];

/** A day's share needs the perfect plan to save at least this much (minor units: `hindsight.MIN_PERFECT_SAVING`). */
const MIN_PERFECT_SAVING = 500;

function element<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, className?: string, content?: string): HTMLElementTagNameMap[K] {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (content !== undefined) {
    created.textContent = content;
  }
  return created;
}

/** Whether there is a perfect plan to show: a whole day's, or only incomplete days (which say so). */
export function hasPerfect(perfect: PerfectFigures | null | undefined): perfect is PerfectFigures {
  return perfect !== null && perfect !== undefined && ((perfect.days > 0 && perfect.saving !== null) || perfect.partial > 0);
}

/** Whether only incomplete days are behind it: no share to show. */
export function onlyPartial(perfect: PerfectFigures): boolean {
  return perfect.days === 0 || perfect.saving === null;
}

function share(language: Language, value: number): string {
  return `${formatNumber(language, value, 0)} %`;
}

function money(language: Language, minor: number, unit: Money): string {
  if (unit === null || unit.major_unit === null) {
    return `${formatNumber(language, minor, 0)}${unit?.minor_unit ? ` ${unit.minor_unit}` : ""}`;
  }
  const major = minor / 100;
  return `${formatFixed(language, major, Math.abs(major) >= 10 || major === 0 ? 0 : 1)} ${unit.major_unit}`;
}

/** A round sum in the major unit ("5 kr"), else in minor units. */
function wholeMoney(language: Language, minor: number, unit: Money): string {
  if (unit === null || unit.major_unit === null) {
    return money(language, minor, unit);
  }
  return `${formatNumber(language, minor / 100, 0)} ${unit.major_unit}`;
}

/** A way's name: the battery's own control (its optimiser where known), or SpotNav and the mode. */
export function wayName(language: Language, way: PerfectWay, ownName: string): string {
  if (way === "own") {
    return ownName;
  }
  return translate(language, "house.compare.spotnav", { mode: translate(language, `house.mode.${way as BatteryMode}`) });
}

/** The ways in the order shown: the battery's own, the chosen mode, the other modes, self-consumption last. */
function order(mode: string | null): PerfectWay[] {
  const modes = MODES.filter((item) => item !== "self_consumption") as PerfectWay[];
  const chosen = modes.filter((item) => item === mode);
  return ["own", ...chosen, ...modes.filter((item) => item !== mode), "self_consumption"];
}

/** "Sigenergy AI 82 % of perfect · SpotNav Balanced 88 %"; `null` when neither has a share. */
export function perfectHeadline(language: Language, perfect: PerfectFigures, ownName: string): string | null {
  const own = perfect.shares.own;
  const mode = PERFECT_WAYS.find((way) => way !== "own" && way === perfect.mode) ?? null;
  const plan = mode === null ? null : perfect.shares[mode];
  if (own !== null && mode !== null && plan !== null) {
    return translate(language, "perfect.headline", { own: ownName, ownShare: share(language, own), plan: wayName(language, mode, ownName), planShare: share(language, plan) });
  }
  const [way, value] = own !== null ? (["own", own] as const) : mode !== null && plan !== null ? ([mode, plan] as const) : [null, null];
  return way === null ? null : translate(language, "perfect.headlineOne", { name: wayName(language, way, ownName), share: share(language, value!) });
}

/** The share rows (a bar each, 0-100 %), the chosen mode marked; none over incomplete days alone. */
export function perfectRows(doc: Document, language: Language, perfect: PerfectFigures, ownName: string, inForce: string | null): HTMLElement {
  const grid = element(doc, "div", H.compareGrid);
  if (onlyPartial(perfect)) {
    return grid;
  }
  for (const way of order(perfect.mode)) {
    const value = perfect.shares[way];
    const ran = perfect.savings[way] !== null;
    if (!ran && way !== "own") {
      continue; // a mode not planned beside the battery on these days
    }
    const name = element(doc, "span", H.compareName, wayName(language, way, ownName));
    if (way === inForce) {
      name.append(" ", element(doc, "span", H.chip, translate(language, "house.compare.inForce")));
    }
    const track = element(doc, "span", H.compareTrack);
    track.setAttribute("aria-hidden", "true");
    if (value !== null && value > 0) {
      const fill = element(doc, "span", H.compareFill);
      fill.style.width = `${Math.round(Math.min(100, value))}%`;
      track.append(fill);
    }
    const figure = value !== null ? share(language, value) : translate(language, ran ? "perfect.nothing" : "house.compare.none");
    const line = element(doc, "div", H.compareRow);
    line.dataset["row"] = way;
    if (way === inForce) {
      line.dataset["inForce"] = "true";
    }
    line.append(name, track, element(doc, "span", value === null ? `${H.compareValue} ${C.muted}` : H.compareValue, figure));
    grid.append(line);
  }
  return grid;
}

/** What "perfect" is, by the rules it was measured with (`hindsight.RULES`): allowed to sell, not selling, or (days
 * stored before) the same rules as the mode. */
function explainKey(rules: string | null): "perfect.explainSell" | "perfect.explainNoSell" | "perfect.explain" {
  return rules === "max_profit" ? "perfect.explainSell" : rules === "no_sell" ? "perfect.explainNoSell" : "perfect.explain";
}

/** The plain notes under the shares: an incomplete day, what perfect is, a single day's midnight, the days still on
 * their own value, the incomplete days left out, a share clamped. */
export function perfectNotes(doc: Document, language: Language, perfect: PerfectFigures, ownName: string, singleDay: boolean): HTMLElement[] {
  const notes: HTMLElement[] = [];
  const add = (key: string, text: string): void => {
    const line = element(doc, "p", `${C.muted} ${H.baseNote}`, text);
    line.dataset["line"] = key;
    notes.push(line);
  };
  if (onlyPartial(perfect)) {
    add(
      "perfect-partial",
      singleDay ? translate(language, "perfect.partialDay") : translate(language, `perfect.partialOnly.${pluralForm(language, perfect.partial)}`, { days: String(perfect.partial) }),
    );
    return notes;
  }
  add("perfect-explain", translate(language, explainKey(perfect.rules), { mode: translate(language, "house.mode.max_profit") }));
  if (singleDay) {
    add("perfect-midnight", translate(language, "perfect.midnight"));
  }
  if (perfect.provisional > 0) {
    add(
      "perfect-provisional",
      singleDay ? translate(language, "perfect.provisionalDay") : translate(language, `perfect.provisional.${pluralForm(language, perfect.provisional)}`, { days: numberWord(language, perfect.provisional) }),
    );
  }
  if (perfect.partial > 0) {
    add("perfect-partial", translate(language, `perfect.partial.${pluralForm(language, perfect.partial)}`, { days: String(perfect.partial) }));
  }
  const clamped = perfect.clamped.filter((way): way is PerfectWay => (PERFECT_WAYS as readonly string[]).includes(way));
  if (clamped.length > 0) {
    add("perfect-clamped", translate(language, "perfect.clamped", { names: clamped.map((way) => wayName(language, way, ownName)).join(", ") }));
  }
  return notes;
}

/** Behind "Show details": the money behind the shares, the battery's work per way and the cells' temperature. */
export function perfectDetails(doc: Document, language: Language, perfect: PerfectFigures, money_: Money, ownName: string): HTMLElement {
  const section = element(doc, "section", H.section);
  section.dataset["part"] = "perfect-details";
  const ways = order(perfect.mode).filter((way) => perfect.savings[way] !== null);
  if (perfect.saving !== null) {
    section.append(element(doc, "h3", H.heading, translate(language, "perfect.money.heading")));
    const table = element(doc, "table", H.table);
    const head = element(doc, "thead");
    const headRow = element(doc, "tr");
    for (const key of ["perfect.work.way", "perfect.money.saved", "perfect.money.share"] as const) {
      headRow.append(element(doc, "th", undefined, translate(language, key)));
    }
    head.append(headRow);
    const body = element(doc, "tbody");
    const perfectRow = element(doc, "tr");
    perfectRow.dataset["way"] = "perfect";
    perfectRow.append(
      element(doc, "td", undefined, translate(language, "perfect.money.perfect")),
      element(doc, "td", undefined, money(language, perfect.saving, money_)),
      element(doc, "td", undefined, "100 %"),
    );
    body.append(perfectRow);
    for (const way of ways) {
      const row = element(doc, "tr");
      row.dataset["way"] = way;
      const value = perfect.shares[way];
      row.append(
        element(doc, "td", undefined, wayName(language, way, ownName)),
        element(doc, "td", undefined, money(language, perfect.savings[way]!, money_)),
        element(doc, "td", undefined, value === null ? "–" : share(language, value)),
      );
      body.append(row);
    }
    table.append(head, body);
    section.append(table, element(doc, "p", `${C.muted} ${H.note}`, translate(language, "perfect.money.note", { amount: wholeMoney(language, MIN_PERFECT_SAVING, money_) })));
  }
  const worked = [...ways, "perfect" as const].filter((way) => perfect.work[way] !== null);
  if (worked.length > 0) {
    section.append(element(doc, "h3", H.heading, translate(language, "perfect.work.heading")));
    const table = element(doc, "table", H.table);
    const head = element(doc, "thead");
    const headRow = element(doc, "tr");
    for (const key of ["perfect.work.way", "perfect.work.cycles", "perfect.work.switches", "perfect.work.fromGrid", "perfect.work.toGrid"] as const) {
      headRow.append(element(doc, "th", undefined, translate(language, key)));
    }
    head.append(headRow);
    const body = element(doc, "tbody");
    for (const way of worked) {
      const work = perfect.work[way]!;
      const row = element(doc, "tr");
      row.dataset["way"] = way;
      row.append(
        element(doc, "td", undefined, way === "perfect" ? translate(language, "perfect.money.perfect") : wayName(language, way, ownName)),
        element(doc, "td", undefined, formatFixed(language, work.cycles, 2)),
        element(doc, "td", undefined, formatNumber(language, work.switches, 0)),
        element(doc, "td", undefined, formatFixed(language, work.grid_charge_kwh, 1)),
        element(doc, "td", undefined, formatFixed(language, work.to_grid_kwh, 1)),
      );
      body.append(row);
    }
    table.append(head, body);
    section.append(table, element(doc, "p", `${C.muted} ${H.note}`, translate(language, "perfect.work.note")));
  }
  const temperature = perfect.temperature;
  if (temperature !== null) {
    const line = element(
      doc,
      "p",
      `${C.muted} ${H.note}`,
      translate(language, `perfect.temperature.${pluralForm(language, temperature.days)}`, {
        mean: `${formatFixed(language, temperature.mean_c, 1)} °C`,
        max: `${formatFixed(language, temperature.max_c, 1)} °C`,
        minutes: formatNumber(language, temperature.minutes_above, 0),
        days: numberWord(language, temperature.days),
      }),
    );
    line.dataset["line"] = "cell-temperature";
    section.append(line);
  }
  return section;
}
