// The days against their perfect plans in hindsight (`site/perfect_view.py`), in the house card's history: the
// headline (each way's share of the perfect plan, the battery's own and the chosen mode first), the share rows with a
// bar each (a way that lost money against no battery: "loss 47 kr", not "0 %"), the plain notes (what "perfect" is,
// the energy left at midnight, a figure clamped at 100 %), and one table of what
// each way saved and the battery's work, with the cells' temperature. Every figure is the
// backend's; text is written with `textContent` only.

import { formatFixed, formatNumber, moneyMinorAsMajor as money, percentWhole } from "./format";
import { MODES, PERFECT_WAYS, type BatteryMode, type HouseAnswer, type PerfectFigures, type PerfectWay } from "./house-model";
import { HOUSE_CLASSES as H } from "./house-styles";
import { numberWord, pluralForm, translate, type Language } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

type Money = HouseAnswer["money"];

/** A day's share needs the perfect plan to save at least this much (minor units: `hindsight.MIN_PERFECT_SAVING`). */
const MIN_PERFECT_SAVING = 500;

/** Whether there is a perfect plan to show: a whole day's, or only incomplete days (which say so). */
export function hasPerfect(perfect: PerfectFigures | null | undefined): perfect is PerfectFigures {
  return perfect !== null && perfect !== undefined && ((perfect.days > 0 && perfect.saving !== null) || perfect.partial > 0);
}

/** Whether only incomplete days are behind it: no share to show. */
export function onlyPartial(perfect: PerfectFigures): boolean {
  return perfect.days === 0 || perfect.saving === null;
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

/** Whether a way lost money against no battery: its share is then no share ("0 %") but a loss with its amount. */
function lost(perfect: PerfectFigures, way: PerfectWay): boolean {
  const saving = perfect.savings[way];
  return saving !== null && saving < 0;
}

/** A way's figure beside its bar: its share, or "loss 47 kr" where it lost money against no battery. */
function shareText(language: Language, perfect: PerfectFigures, way: PerfectWay, value: number, money_: Money): string {
  return lost(perfect, way) ? translate(language, "perfect.loss", { amount: wholeMoney(language, Math.abs(perfect.savings[way]!), money_) }) : percentWhole(language, value);
}

/** "Sigenergy AI 82 % of perfect · SpotNav Balanced 88 %" (a way that lost money: "loss 47 kr"); `null` when neither
 * has a share. */
export function perfectHeadline(language: Language, perfect: PerfectFigures, ownName: string, money_: Money = null): string | null {
  const own = perfect.shares.own;
  const mode = PERFECT_WAYS.find((way) => way !== "own" && way === perfect.mode) ?? null;
  const plan = mode === null ? null : perfect.shares[mode];
  if (own !== null && mode !== null && plan !== null) {
    return translate(language, "perfect.headline", { own: ownName, ownShare: shareText(language, perfect, "own", own, money_), plan: wayName(language, mode, ownName), planShare: shareText(language, perfect, mode, plan, money_) });
  }
  const [way, value] = own !== null ? (["own", own] as const) : mode !== null && plan !== null ? ([mode, plan] as const) : [null, null];
  return way === null ? null : translate(language, "perfect.headlineOne", { name: wayName(language, way, ownName), share: shareText(language, perfect, way, value!, money_) });
}

/** The share rows (a bar each, 0-100 %; a way that lost money says so with its amount), the chosen mode marked; none
 * over incomplete days alone. */
export function perfectRows(doc: Document, language: Language, perfect: PerfectFigures, ownName: string, inForce: string | null, money_: Money = null): HTMLElement {
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
    const figure = value !== null ? shareText(language, perfect, way, value, money_) : translate(language, ran ? "perfect.nothing" : "house.compare.none");
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
  // Clamped at 100 % only: a way clamped at 0 % lost money and says so with its amount in its row.
  const clamped = perfect.clamped.filter((way): way is PerfectWay => (PERFECT_WAYS as readonly string[]).includes(way) && !lost(perfect, way as PerfectWay));
  if (clamped.length > 0) {
    add("perfect-clamped", translate(language, "perfect.clamped", { names: clamped.map((way) => wayName(language, way, ownName)).join(", ") }));
  }
  return notes;
}

/** The figures behind the shares, once each: a block per way (its name on a line of its own, so the table fits a
 * phone) and the perfect plan last, with what it saved against no battery and the battery's work. `null` without a
 * whole day. */
export function perfectTable(doc: Document, language: Language, perfect: PerfectFigures, money_: Money, ownName: string): HTMLElement | null {
  if (onlyPartial(perfect)) {
    return null;
  }
  const columns = ["perfect.money.saved", "perfect.work.cycles", "perfect.work.switches", "perfect.work.fromGrid", "perfect.work.toGrid"] as const;
  const table = element(doc, "table", `${H.table} ${H.wayTable}`);
  const head = element(doc, "thead");
  const headRow = element(doc, "tr");
  for (const key of columns) {
    const cell = element(doc, "th", undefined, translate(language, key));
    cell.scope = "col";
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);
  const rows: Array<{ way: PerfectWay | "perfect"; name: string; saving: number | null }> = order(perfect.mode)
    .filter((way) => perfect.savings[way] !== null)
    .map((way) => ({ way, name: wayName(language, way, ownName), saving: perfect.savings[way] }));
  rows.push({ way: "perfect", name: translate(language, "perfect.money.perfect"), saving: perfect.saving });
  for (const { way, name, saving } of rows) {
    const work = perfect.work[way];
    const group = element(doc, "tbody");
    group.dataset["way"] = way;
    const nameRow = element(doc, "tr");
    const nameCell = element(doc, "th", undefined, name);
    nameCell.scope = "rowgroup";
    nameCell.colSpan = columns.length;
    nameRow.append(nameCell);
    const figures = element(doc, "tr");
    figures.append(
      element(doc, "td", undefined, saving === null ? "–" : money(language, saving, money_)),
      element(doc, "td", undefined, work === null ? "–" : formatFixed(language, work.cycles, 2)),
      element(doc, "td", undefined, work === null ? "–" : formatNumber(language, work.switches, 0)),
      element(doc, "td", undefined, work === null ? "–" : formatFixed(language, work.grid_charge_kwh, 1)),
      element(doc, "td", undefined, work === null ? "–" : formatFixed(language, work.to_grid_kwh, 1)),
    );
    group.append(nameRow, figures);
    table.append(group);
  }
  return table;
}

/** What the table's columns count: the saving (with the energy left at midnight) and the battery's work. */
export function perfectTableNotes(doc: Document, language: Language, money_: Money): HTMLElement[] {
  return [
    element(doc, "p", `${C.muted} ${H.note}`, translate(language, "perfect.money.note", { amount: wholeMoney(language, MIN_PERFECT_SAVING, money_) })),
    element(doc, "p", `${C.muted} ${H.note}`, translate(language, "perfect.work.note")),
  ];
}

/** A way's summed saving against no battery as the table words it (for a sentence that names it). */
export function perfectSaving(language: Language, perfect: PerfectFigures, way: PerfectWay, money_: Money): string | null {
  const value = perfect.savings[way];
  return value === null ? null : money(language, value, money_);
}

/** The cells' temperature over the days that have it; `null` where the battery does not report it. */
export function perfectTemperature(doc: Document, language: Language, perfect: PerfectFigures): HTMLElement | null {
  const temperature = perfect.temperature;
  if (temperature === null) {
    return null;
  }
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
  return line;
}
